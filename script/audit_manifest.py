#!/usr/bin/env python3
# script/audit_manifest.py
"""Block PRs that ship an integration-owned dependency with a fixable CVE.

Home Assistant installs the packages listed in ``manifest.json`` ``requirements``
into its own environment; that list, not ``poetry.lock``, is what reaches an end
user. The repository's ``pip-audit`` workflow audits the Poetry dev/test lock and
is deliberately report-only, so a vulnerability in a shipped runtime dependency
never blocks a merge. This script closes that gap with a narrow, *actionable*
gate.

Scope and policy:
- Every manifest requirement is audited; Home Assistant governance decides only
  *how* a finding is classified, not whether it is audited. Each package is
  audited at the version an end user actually runs:
    * Integration-owned packages are audited at their declared *floor*: each
      ``pkg>=X`` is pinned to ``pkg==X`` before auditing. Auditing the floor
      rather than the resolver's newest pick is what makes the gate demand a
      floor bump when the *minimum* a user may install is vulnerable; a plain
      ``>=`` audit would resolve to a newer clean release and silently hide a
      vulnerable floor.
    * Packages Home Assistant pins in its ``package_constraints.txt`` for the
      declared-minimum HA version (e.g. aiohttp, cryptography) are audited at
      Home Assistant's own ``==`` pin, not at the manifest floor. Home Assistant
      overrides the floor at install time, so auditing the floor would report
      vulnerabilities in a version no user ever runs; auditing HA's pin reflects
      what the declared-minimum HA actually installs.
- Blocking (exit 1) is reserved for *fixable* findings the integration can act
  on:
    * an integration-owned package whose floor carries a CVE with a
      ``fix_versions`` entry -> bump the manifest floor;
    * a transitive dependency (not a direct manifest entry) whose finding is
      fixable -> bump the direct parent dependency or add a constraint that
      pulls in the fix.
  Unfixable (won't-fix) integration-owned findings such as the ``ecdsa`` Minerva
  side-channel, unfixable transitive findings, and *all* Home Assistant-governed
  findings are reported but never block. A finding in a package HA pins
  *exactly* (``name==X``) never blocks because the integration cannot lower Home
  Assistant's runtime ``==`` pin; the only remediation for a fixable one is to
  raise the ``hacs.json`` Home Assistant floor to a release whose pin ships the
  fix. A finding in a package HA governs with only a *range* floor (e.g.
  ``urllib3>=2.0``) is separated into its own ``governed_range`` report bucket:
  it is likewise non-blocking, but honestly labelled as actionable, since the
  integration *could* declare a tighter manifest floor -- the gate deliberately
  defers these HA-ecosystem transitives to Home Assistant rather than making a
  silent "already resolved" claim.

The floor and declared-minimum-HA pins are the lower edge of the installable
span. Auditing only that edge can miss a CVE introduced above it, so the online
gate widens coverage with two extra passes (both need the network and are
skipped by the offline ``--audit-json`` preview, which stays floor-only and says
so):
- integration-owned packages are additionally audited *as declared* (``>=X``),
  which the resolver resolves to the newest installable release, catching a CVE
  that lives above the floor but within the permitted range;
- governed packages are additionally audited at every *additional* supported HA
  version's pins (pass one snapshot per ``--ha-constraints``): Home Assistant may
  pin, say, ``cryptography`` or ``aiohttp`` at a newer version on a newer core,
  and a CVE living solely in that newer pin would otherwise be invisible.

The gate turns red the first time a shipped package a user actually installs
carries a fixable CVE the integration can act on, which is exactly the case the
report-only job waves through.

Usage:
    python script/audit_manifest.py --ha-constraints ha_constraints.txt
    python script/audit_manifest.py --ha-constraints ha_constraints.txt --verbose

    # Preview classification against a pre-generated pip-audit report (no
    # network, used by the test suite):
    python script/audit_manifest.py --ha-constraints ha_constraints.txt \
        --audit-json audit.json

Exit status:
    0  no blocking findings (may still print warnings)
    1  at least one fixable vulnerability the integration can act on, in an
       integration-owned floor or a transitive dependency -> block the PR
    2  tooling error (missing input, pip-audit failure, malformed report)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

DEFAULT_MANIFEST = "custom_components/googlefindmy/manifest.json"

# Specifier operators that establish an *inclusive* lower bound, i.e. the bound
# version itself is installable and is therefore the floor we audit. ``>`` is
# deliberately excluded: ``>X`` excludes X, and the exact next release is not
# offline-computable, so such a requirement has no determinable floor.
_LOWER_BOUND_OPERATORS = frozenset({">=", "==", "~="})

# A single finding record: {"name", "version", "id", "fix_versions"}.
Record = dict[str, Any]


def load_manifest_requirements(manifest_path: Path) -> list[str]:
    """Return the raw requirement specifiers from a manifest.json file."""
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    requirements = data.get("requirements", [])
    if not isinstance(requirements, list):
        raise ValueError(f"'requirements' in {manifest_path} is not a list")
    return [str(req) for req in requirements]


def requirement_project_name(requirement: str) -> str:
    """Return the canonical (PEP 503) project name of a requirement specifier."""
    return canonicalize_name(Requirement(requirement).name)


def _specifier_floor(specifier: SpecifierSet) -> str | None:
    """Return the lowest version a specifier set allows, or None.

    Considers inclusive lower-bound operators (``>=``, ``==``, ``~=``) and
    returns the most restrictive (highest) such bound as a version string. A
    specifier set with no inclusive lower bound (empty, an upper-bound-only
    specifier, or an exclusive ``>X``) has no determinable floor and returns
    None; the caller then audits at the resolver's newest pick and flags it,
    rather than fabricating a wrong ``==X`` pin. Every real manifest entry uses
    ``>=``.

    The chosen bound must also satisfy the *complete* specifier set: a
    combined constraint such as ``foo>=1.0,!=1.0`` names ``1.0`` as its lower
    bound yet explicitly excludes it, so pinning ``foo==1.0`` would be a version
    the constraint forbids and pip-audit would fail to resolve (tooling exit 2).
    When the candidate floor is excluded by another specifier, no exact floor is
    derivable and None is returned so the caller audits it as declared instead.
    This is why a package Home Assistant lists more than once (its combined
    specifiers, not any single line) determines the floor.
    """
    best: Version | None = None
    best_text: str | None = None
    for spec in specifier:
        if spec.operator not in _LOWER_BOUND_OPERATORS:
            continue
        try:
            candidate = Version(spec.version)
        except InvalidVersion:
            continue
        if best is None or candidate > best:
            best = candidate
            best_text = spec.version
    if best_text is not None and not specifier.contains(best_text, prereleases=True):
        # Another specifier (e.g. ``!=floor``) excludes the lower bound; no exact
        # floor can be derived. prereleases=True so a legitimate prerelease floor
        # is not itself rejected -- only a genuine exclusion returns None.
        return None
    return best_text


def _requirement_floor(requirement: Requirement) -> str | None:
    """Return the lowest version the requirement's specifiers allow, or None.

    Thin wrapper over :func:`_specifier_floor` for a single :class:`Requirement`
    (the manifest floor-pinning path); the shared floor logic lives in
    :func:`_specifier_floor` so the manifest and the combined-HA-constraint paths
    can never diverge.
    """
    return _specifier_floor(requirement.specifier)


def _specifier_is_exact(specifier: SpecifierSet) -> bool:
    """Return whether a combined specifier set is a lone concrete ``==`` pin.

    ``True`` only when every specifier in the set is ``==`` (a zero-width floor
    the integration cannot tighten), which the floor gate in the callers pairs
    with a derivable concrete version. A range floor (``>=``/``~=``), an ``==``
    accompanied by any other operator (``==1.0,<2.0`` -- not a lone ``==``), or a
    non-concrete ``==1.*`` prefix is therefore not exact and routes to the
    actionable ``governed_range`` bucket. Deriving exactness from the *combined*
    set is the conservative choice for a package Home Assistant constrains on
    more than one line: an exact/range duplicate keeps the additional operator
    and stays actionable rather than being silently suppressed as unfixable.
    """
    operators = {spec.operator for spec in specifier}
    return operators == {"=="}


def _pin_requirement(requirement: Requirement, version: str) -> str:
    """Rebuild ``requirement`` pinned to ``version``, preserving extras and marker.

    Only the version specifier is replaced. Extras (``foo[crypto]``) and an
    environment marker (``; sys_platform == 'linux'``) are retained so pip-audit
    still pulls extra-only transitive dependencies and still skips a
    platform-inapplicable entry, rather than auditing a bare ``foo==version``
    unconditionally (which would drop extra-only vulnerabilities and produce
    false blocks for entries the marker excludes). Reconstruction goes through
    :class:`Requirement` so the rendering matches packaging's own canonical form;
    a bare ``name>=x`` entry is therefore still rendered exactly as ``name==x``.
    """
    pinned = Requirement(str(requirement))
    pinned.specifier = SpecifierSet(f"=={version}")
    return str(pinned)


def floor_pin_requirements(requirements: list[str]) -> tuple[list[str], list[str]]:
    """Pin each manifest requirement to its declared floor for auditing.

    Returns ``(audit_specifiers, unbounded_names)``:
      - ``audit_specifiers``: one entry per requirement. A requirement with a
        determinable floor becomes ``name==floor`` (extras and marker preserved,
        see :func:`_pin_requirement`) so pip-audit evaluates the minimum a user
        may install rather than the resolver's newest pick. A
        requirement without a floor, or one that cannot be parsed, is passed
        through unchanged (audited as declared) rather than dropped.
      - ``unbounded_names``: the canonical names whose floor could not be pinned;
        their audit still reflects the resolver's newest pick, so the gap is
        surfaced in the report instead of hidden.
    """
    audit_specifiers: list[str] = []
    unbounded: list[str] = []
    for raw in requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:
            audit_specifiers.append(raw)
            continue
        floor = _requirement_floor(requirement)
        if floor is None:
            audit_specifiers.append(raw)
            unbounded.append(canonicalize_name(requirement.name))
            continue
        audit_specifiers.append(_pin_requirement(requirement, floor))
    return audit_specifiers, unbounded


def ha_pin_requirements(
    requirements: list[str], governed_pins: dict[str, str]
) -> list[str]:
    """Pin each HA-governed requirement to Home Assistant's own ``==`` version.

    ``requirements`` are the manifest entries Home Assistant pins itself;
    ``governed_pins`` is the ``name -> version`` map from
    :func:`parse_ha_governed_pins`. Each entry becomes ``name==pin`` (extras and
    marker preserved, see :func:`_pin_requirement`) so the audit reflects the
    version the declared-minimum Home Assistant installs, not
    the manifest floor (which HA overrides at install time). This is what lets
    the report state truthfully whether HA's pinned version is itself
    vulnerable, instead of blindly assuming the floor's finding is "resolved".
    """
    specifiers: list[str] = []
    for raw in requirements:
        try:
            requirement = Requirement(raw)
        except InvalidRequirement:  # pragma: no cover - governed entries parse
            specifiers.append(raw)
            continue
        pin = governed_pins.get(canonicalize_name(requirement.name))
        if pin is None:  # pragma: no cover - governed set is derived from pins
            specifiers.append(raw)
            continue
        specifiers.append(_pin_requirement(requirement, pin))
    return specifiers


def _marker_applies(marker: Marker) -> bool:
    """Return whether an HA constraint's environment marker holds on this runtime.

    Home Assistant's constraints file may gate an entry with a PEP 508
    environment marker (for example ``python_version < '3.13'``). The pin only
    applies where the marker is true, so a governed classification must respect
    it. The marker is evaluated against the current interpreter -- the same
    environment pip-audit resolves and audits in -- so a constraint Home
    Assistant does not impose here is never mistaken for one it does.

    ``Marker.evaluate`` does not raise for a marker that survived
    :class:`Requirement` parsing: an undefined marker variable is already
    rejected there as ``InvalidRequirement`` (routing the line to the salvage
    path below), and a defined-but-empty variable such as ``extra`` evaluates to
    a boolean. There is therefore no unresolvable-marker branch to guard.
    """
    return bool(marker.evaluate())


def _parse_governed_line(raw_line: str) -> tuple[str, SpecifierSet] | None:
    """Parse one HA constraints line into ``(canonical_name, specifier_set)``.

    Shared single-line parser for the governed-constraint helpers so the pin
    map, the name set, and the exact-name subset can never diverge. Returns
    ``None`` for a comment, a blank line, or a valid PEP 508 constraint whose
    environment marker is false on the auditing runtime (Home Assistant does not
    impose it here, see :func:`_marker_applies`). The floor and exactness are
    deliberately NOT decided here: the caller combines every line for a package
    first (pip applies all of a package's constraints together) and derives them
    from the combined set, so a package listed more than once is governed by the
    intersection of its lines rather than whichever line comes last.

    A non-PEP 508 line is salvaged as a bare ``name==version`` so a non-standard
    exact constraint still governs; a range floor cannot be recovered without a
    parseable specifier, so such a line is dropped rather than guessed. An
    environment marker is deliberately NOT evaluated on the salvage path: the
    marker filter applies only to valid PEP 508 lines, and a marker-gated
    constraint whose *name* is not PEP 508 valid cannot occur in Home Assistant's
    machine-generated constraints file (nor could such a package be a manifest
    dependency).
    """
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    try:
        requirement = Requirement(line)
    except InvalidRequirement:
        name, separator, rhs = line.partition("==")
        name = name.strip()
        version = rhs.split(";", 1)[0].split(",", 1)[0].strip()
        if not separator or not name or not version:
            return None
        try:
            salvaged = SpecifierSet(f"=={version}")
        except InvalidSpecifier:  # pragma: no cover - HA versions are concrete
            return None
        return canonicalize_name(name), salvaged
    if requirement.marker is not None and not _marker_applies(requirement.marker):
        # Home Assistant only pins this package where the environment marker
        # holds. When it is false on the auditing runtime the pin is absent, so
        # the package must not enter the non-blocking governed maps: an
        # integration-owned manifest dependency then stays in the blocking
        # bucket and is audited at its own floor, instead of being suppressed at
        # a version Home Assistant never imposes here.
        return None
    return canonicalize_name(requirement.name), requirement.specifier


def _merged_governed_constraints(constraints_text: str) -> dict[str, SpecifierSet]:
    """Combine every HA constraint line per canonical package into one set.

    Home Assistant may list the same package more than once (the committed
    snapshot pins ``multidict`` at both ``>=6.0.2`` and ``>=6.4.2``); pip applies
    those lines together, so the effective governance is their intersection, not
    whichever line comes last. Accumulating each package's lines into a single
    :class:`SpecifierSet` makes the derived floor (:func:`_specifier_floor`) and
    exactness (:func:`_specifier_is_exact`) reflect what HA actually enforces
    regardless of line order. Comments, blank lines, and marker-false lines are
    excluded (see :func:`_parse_governed_line`).
    """
    merged: dict[str, SpecifierSet] = {}
    for raw_line in constraints_text.splitlines():
        parsed = _parse_governed_line(raw_line)
        if parsed is None:
            continue
        name, specifier = parsed
        merged[name] = merged.get(name, SpecifierSet()) & specifier
    return merged


def parse_ha_governed_pins(constraints_text: str) -> dict[str, str]:
    """Return the canonical ``name -> audit-floor`` map from an HA constraints file.

    Each governed package maps to the lowest version Home Assistant permits: an
    exact ``name==X`` pin yields ``X`` (a zero-width floor), while a range such
    as ``urllib3>=2.0`` yields its inclusive lower bound ``2.0``. That floor is
    the worst-case version a user under the declared-minimum Home Assistant may
    actually run, so auditing it -- exactly as :func:`floor_pin_requirements`
    audits manifest floors -- surfaces a CVE that lives in an older, still-
    permitted version instead of letting the resolver's newest pick hide it.
    Comments, blank lines, and constraints with no inclusive lower bound (a bare
    name, a pure ``!=`` exclusion, an upper-bound-only ``<`` cap, or a
    non-concrete ``==1.*`` prefix / ``===`` arbitrary equality) are ignored: no
    worst-case version can be derived from them. A package listed more than once
    is governed by the *combination* of its lines (see
    :func:`_merged_governed_constraints`), not whichever line comes last, so a
    looser bound ordered after a tighter one cannot mask the tighter floor.

    The floor is retained for *every* governed package, exact or range, because
    the governed-transitive re-audit re-pins each reachable package to this
    worst-case version. Whether a *finding* against that package blocks or how it
    is labelled is a separate concern handled in :func:`classify_audit`.
    """
    pins: dict[str, str] = {}
    for name, specifier in _merged_governed_constraints(constraints_text).items():
        floor = _specifier_floor(specifier)
        if floor is not None:
            pins[name] = floor
    return pins


def parse_ha_governed_names(constraints_text: str) -> set[str]:
    """Return the canonical names Home Assistant pins in its constraints file.

    Thin wrapper over :func:`parse_ha_governed_pins`; the name set is derived
    from the pin mapping so the two never diverge.
    """
    return set(parse_ha_governed_pins(constraints_text))


def parse_ha_exact_governed_names(constraints_text: str) -> set[str]:
    """Return the governed names Home Assistant pins with an *exact* ``==`` version.

    A strict subset of :func:`parse_ha_governed_names`: only a single concrete
    ``==`` pin qualifies (a zero-width floor the integration cannot tighten in
    its own manifest). Range-governed packages such as ``urllib3>=2.0`` are
    governed but excluded here, because HA sets only a lower bound the
    integration *could* raise (``urllib3>=<fixed>``); their findings are routed
    to the actionable ``governed_range`` bucket in :func:`classify_audit`
    instead of the unfixable-by-the-integration ``governed`` bucket. A non-PEP
    508 line salvaged as a bare ``name==version`` counts as exact. Exactness is
    derived from the *combined* per-package specifier set (see
    :func:`_merged_governed_constraints`): a package HA constrains on one line as
    ``==`` and on another with an extra bound is no longer a lone ``==`` and
    conservatively routes to the actionable range bucket rather than being
    silently suppressed as unfixable.
    """
    exact: set[str] = set()
    for name, specifier in _merged_governed_constraints(constraints_text).items():
        if _specifier_floor(specifier) is not None and _specifier_is_exact(specifier):
            exact.add(name)
    return exact


def additional_governed_reaudit_pins(
    additional_pins: dict[str, str],
    primary_pins: dict[str, str],
    audit: dict[str, Any],
    relevant_names: set[str],
) -> dict[str, str]:
    """Return governed pins from an additional HA snapshot that need a re-audit.

    The declared-minimum audit only covers Home Assistant's pins for the oldest
    supported version. A newer supported release may pin a governed package (for
    example ``cryptography``, ``aiohttp``, ``yarl``) at a different version whose
    CVE would otherwise be invisible. This selects, from one additional
    snapshot's ``name -> pin`` map, the governed packages that

      - are actually part of this manifest's footprint (``relevant_names`` = the
        direct governed manifest entries plus the governed transitives that
        appeared in the pass-1 ``audit``); auditing every package HA pins would
        drag in hundreds of unrelated dependencies, and
      - are pinned at a version not already audited -- neither the declared-
        minimum pin (``primary_pins``, at which pass 1 and the primary re-audit
        already audited the package) nor the version pass 1 resolved for it.

    The caller runs a deterministic ``--no-deps`` pip-audit over the resulting
    ``name==pin`` set and folds the findings into the governed buckets (adding,
    not replacing, so a finding present only at the newer pin is surfaced
    alongside the declared-minimum result).
    """
    resolved: dict[str, str] = {
        str(canonicalize_name(dependency.get("name", ""))): dependency.get(
            "version", ""
        )
        for dependency in audit.get("dependencies", [])
    }
    result: dict[str, str] = {}
    for name, pin in additional_pins.items():
        if name not in relevant_names:
            continue
        if pin in {primary_pins.get(name), resolved.get(name)}:
            continue
        result[name] = pin
    return result


def partition_requirements(
    requirements: list[str], governed: set[str]
) -> tuple[list[str], list[str]]:
    """Split manifest requirements into (integration_owned, ha_governed).

    Requirement strings that cannot be parsed are conservatively treated as
    integration-owned so they are audited rather than silently dropped.
    """
    owned: list[str] = []
    ha_governed: list[str] = []
    for requirement in requirements:
        try:
            name = requirement_project_name(requirement)
        except InvalidRequirement:
            owned.append(requirement)
            continue
        if name in governed:
            ha_governed.append(requirement)
        else:
            owned.append(requirement)
    return owned, ha_governed


def classify_audit(
    audit: dict[str, Any],
    owned_names: set[str],
    governed_names: set[str] | None = None,
    exact_governed_names: set[str] | None = None,
) -> dict[str, list[Record]]:
    """Classify a pip-audit JSON report against the manifest name sets.

    Returns a mapping with six deterministic, sorted lists of findings, each a
    ``{"name","version","id","fix_versions"}`` record:
      - ``blocking``: integration-owned package with a fixable vulnerability
        (remediation: bump the manifest floor).
      - ``unfixable``: integration-owned package with no available fix.
      - ``governed``: a package Home Assistant pins *exactly* (``name==X``),
        whether a direct manifest entry (audited at HA's own pin) or a
        transitive dependency that HA still governs; reported for transparency,
        never blocking. HA fixes the version with a zero-width pin, so the
        integration cannot tighten it -- blocking would be a permanent red the
        contributor cannot clear; a fixable one is actionable only by raising
        the ``hacs.json`` HA floor to a release whose pin ships the fix.
      - ``governed_range``: a package Home Assistant governs with only a *range*
        lower bound (e.g. ``urllib3>=2.0``), never blocking. Unlike the exact
        case this finding IS actionable by the integration -- HA fixes only a
        floor, so the manifest *could* declare a tighter ``name>=<fixed>`` -- but
        the gate deliberately defers HA-ecosystem transitives to Home Assistant
        and reports it for visibility rather than blocking.
      - ``transitive_blocking``: a *fixable* vulnerability in a package that is
        neither a direct manifest entry nor HA-governed (a truly transitive
        dependency the integration can reach). Unlike the governed cases this IS
        blocked, by bumping the direct parent dependency or adding a constraint.
      - ``transitive``: an *unfixable*, non-governed transitive-dependency
        vulnerability; reported, never blocking.

    ``owned_names`` and ``governed_names`` decide the bucket. ``governed_names``
    is the *full* set of names Home Assistant pins (not only the HA-governed
    manifest entries), so a transitive package HA governs is never mistaken for
    an actionable transitive finding. ``exact_governed_names`` is the subset of
    ``governed_names`` HA pins with an exact ``==`` version; a governed name in
    it routes to ``governed``, a governed name outside it (a range constraint)
    routes to ``governed_range``. When ``exact_governed_names`` is ``None`` the
    whole governed set is treated as exact (``governed_range`` stays empty),
    preserving the pre-Option-B behaviour for callers that do not distinguish.
    Whether a non-owned, non-governed (truly transitive) finding blocks then
    depends on whether it carries a fix, mirroring the owned split.
    """
    governed = governed_names or set()
    exact_governed = governed if exact_governed_names is None else exact_governed_names
    blocking: list[Record] = []
    unfixable: list[Record] = []
    governed_findings: list[Record] = []
    governed_range: list[Record] = []
    transitive_blocking: list[Record] = []
    transitive: list[Record] = []
    for dependency in audit.get("dependencies", []):
        name = canonicalize_name(dependency.get("name", ""))
        version = dependency.get("version", "")
        for vuln in dependency.get("vulns", []):
            record = {
                "name": name,
                "version": version,
                "id": vuln.get("id", ""),
                "fix_versions": list(vuln.get("fix_versions", [])),
            }
            if name in owned_names:
                if record["fix_versions"]:
                    blocking.append(record)
                else:
                    unfixable.append(record)
            elif name in exact_governed:
                governed_findings.append(record)
            elif name in governed:
                governed_range.append(record)
            elif record["fix_versions"]:
                transitive_blocking.append(record)
            else:
                transitive.append(record)

    def sort_key(record: Record) -> tuple[str, str]:
        return (record["name"], record["id"])

    return {
        "blocking": sorted(blocking, key=sort_key),
        "unfixable": sorted(unfixable, key=sort_key),
        "governed": sorted(governed_findings, key=sort_key),
        "governed_range": sorted(governed_range, key=sort_key),
        "transitive_blocking": sorted(transitive_blocking, key=sort_key),
        "transitive": sorted(transitive, key=sort_key),
    }


def blocking_records(findings: dict[str, list[Record]]) -> list[Record]:
    """Return every finding that must fail the gate (owned + transitive fixable).

    Both buckets are fixable and actionable, so the exit decision and the
    ``--verbose`` trigger treat them alike; the report and annotations still
    name the distinct remediation per bucket.
    """
    return findings["blocking"] + findings["transitive_blocking"]


def reachable_governed_transitive_pins(
    audit: dict[str, Any],
    governed_pins: dict[str, str],
    direct_governed_names: set[str],
    owned_names: set[str],
) -> dict[str, str]:
    """Return governed transitive packages whose pass-1 version misses HA's pin.

    A package Home Assistant governs but that reaches the manifest only
    transitively (e.g. ``yarl`` pulled in by ``aiohttp``) is not pinned by
    :func:`ha_pin_requirements`, which pins only direct manifest entries. The
    resolver therefore audits it at the newest compatible release rather than at
    Home Assistant's own ``==`` pin, so a CVE that lives in HA's pinned version
    but is fixed in the newer one silently drops out of the report.

    This function scans the pass-1 ``audit`` and returns the ``name -> pin`` map
    of exactly those governed transitive packages that must be re-audited at
    Home Assistant's pin. For each dependency in ``audit["dependencies"]`` the
    canonical name is taken and skipped when it is integration-owned
    (``owned_names``) or a direct governed manifest entry
    (``direct_governed_names``); those are already audited at the right version.
    A package is included only when Home Assistant pins it
    (``governed_pins`` has an entry) and the pass-1 audit resolved it to a
    *different* version than that pin; a package already at HA's pin needs no
    re-audit. The returned mapping drives a second, ``--no-deps`` pip-audit pass
    over ``name==pin`` specifiers.
    """
    result: dict[str, str] = {}
    for dependency in audit.get("dependencies", []):
        name = canonicalize_name(dependency.get("name", ""))
        if name in owned_names or name in direct_governed_names:
            continue
        pin = governed_pins.get(name)
        if pin is None or dependency.get("version", "") == pin:
            continue
        result[name] = pin
    return result


def run_pip_audit(
    requirements: list[str], output_path: Path, *, no_deps: bool = False
) -> int:
    """Run pip-audit on the given requirement specifiers, writing JSON output.

    Returns the pip-audit exit code: 0 (clean), 1 (vulnerabilities found) or
    >1 (tooling error). Callers decide what to block on from the JSON, not from
    the exit code, because a vulnerable-but-unfixable finding still yields 1.

    When ``no_deps`` is true, ``--no-deps`` is appended so pip-audit audits
    exactly the listed ``==`` pins without resolving transitive dependencies.
    This is what lets the governed-transitive re-audit pin a package to Home
    Assistant's own version instead of the resolver's newest compatible pick.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        handle.write("\n".join(requirements) + "\n")
        requirements_path = handle.name
    args = [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        requirements_path,
        "-f",
        "json",
        "-o",
        str(output_path),
    ]
    if no_deps:
        args.append("--no-deps")
    try:
        completed = subprocess.run(args, check=False)
    finally:
        Path(requirements_path).unlink(missing_ok=True)
    return completed.returncode


def _finding_line(record: Record, *, show_fix_state: bool) -> str:
    """Render one finding as a report bullet."""
    head = f"  - {record['name']} {record['version']}: {record['id']}"
    if not show_fix_state:
        return head
    state = "fixable" if record["fix_versions"] else "no fix"
    return f"{head} ({state})"


def _info_section(title: str, records: list[Record]) -> list[str]:
    """Render a non-blocking INFO section, or nothing when it is empty."""
    if not records:
        return []
    lines = [title]
    lines.extend(_finding_line(record, show_fix_state=True) for record in records)
    lines.append("")
    return lines


def render_report(
    owned: list[str],
    ha_governed: list[str],
    unbounded: list[str],
    findings: dict[str, list[Record]],
    *,
    offline_preview_gap: bool = False,
) -> str:
    """Return a human-facing summary of the audit result.

    ``offline_preview_gap`` is set when the run used a pre-generated report
    (``--audit-json``) and therefore skipped the online-only passes -- auditing
    integration-owned packages as declared (beyond their floor) and governed
    packages at additional supported HA versions' pins. A note then documents
    that the offline preview covers only the declared-minimum floor/HA-pin
    dimension, so a reader does not mistake a clean preview for the full gate.
    """
    lines: list[str] = [
        "Manifest pip-audit (integration-owned at declared floor, "
        "HA-governed at Home Assistant's pin)",
        "",
    ]

    lines.append(f"Integration-owned packages (block on fixable): {len(owned)}")
    lines.extend(f"  - {req}" for req in sorted(owned))
    lines.append("")
    lines.append(f"Home Assistant-governed packages (report only): {len(ha_governed)}")
    lines.extend(
        f"  - {req}  (pinned by Home Assistant)" for req in sorted(ha_governed)
    )
    lines.append("")

    if unbounded:
        lines.append(
            f"NOTE ({len(unbounded)}) - requirement(s) without a floor, audited at "
            "the resolver's newest pick:"
        )
        lines.extend(f"  - {name}" for name in sorted(unbounded))
        lines.append("")

    if offline_preview_gap:
        lines.append(
            "NOTE - offline preview (--audit-json): coverage is limited to the "
            "declared-minimum HA snapshot at each package's floor/HA-pin. The "
            "online gate additionally audits integration-owned packages as "
            "declared (catching a CVE above the floor but within the permitted "
            "range) and governed packages at every supported HA version's pins; "
            "those passes need the network and are skipped here."
        )
        lines.append("")

    owned_blocking = findings["blocking"]
    transitive_blocking = findings["transitive_blocking"]
    total_blocking = len(owned_blocking) + len(transitive_blocking)
    if total_blocking:
        lines.append(
            f"BLOCKING ({total_blocking}) - fixable vulnerabilities that must be "
            "resolved before merge:"
        )
        for record in owned_blocking:
            fixes = ", ".join(record["fix_versions"])
            lines.append(
                f"  - {record['name']} {record['version']}: {record['id']} "
                f"(fix: {fixes}; bump the manifest floor)"
            )
        for record in transitive_blocking:
            fixes = ", ".join(record["fix_versions"])
            lines.append(
                f"  - {record['name']} {record['version']}: {record['id']} "
                f"(fix: {fixes}; bump the direct parent dependency or add a "
                "constraint)"
            )
    else:
        lines.append("BLOCKING (0) - no fixable owned or transitive vulnerability.")
    lines.append("")

    unfixable = findings["unfixable"]
    if unfixable:
        lines.append(f"WARN ({len(unfixable)}) - integration-owned, no fix available:")
        lines.extend(
            _finding_line(record, show_fix_state=False) for record in unfixable
        )
        lines.append("")

    lines.extend(
        _info_section(
            f"INFO ({len(findings.get('governed', []))}) - vulnerabilities in "
            "Home Assistant's own minimum-version pins; the integration cannot "
            "bump these. Fixable ones require raising the hacs.json Home "
            "Assistant floor to a release whose pin contains the fix:",
            findings.get("governed", []),
        )
    )
    lines.extend(
        _info_section(
            f"INFO ({len(findings.get('governed_range', []))}) - vulnerabilities "
            "in Home Assistant range-governed dependencies (HA permits an older "
            "version satisfying its range, e.g. name>=X). A fixable one the "
            "integration COULD pin tighter via name>=<fixed> (an unfixable one, "
            "shown as no fix, has no such floor), but the gate deliberately "
            "defers HA-ecosystem transitives to Home Assistant. Reported for "
            "visibility, non-blocking:",
            findings.get("governed_range", []),
        )
    )
    lines.extend(
        _info_section(
            f"INFO ({len(findings['transitive'])}) - unfixable transitive "
            "dependency vulnerabilities (not a direct manifest entry, no fix "
            "available):",
            findings["transitive"],
        )
    )

    return "\n".join(lines).rstrip() + "\n"


def emit_github_annotations(findings: dict[str, list[Record]]) -> None:
    """Emit GitHub Actions annotations so findings surface in the PR checks."""
    for record in findings["blocking"]:
        fixes = ", ".join(record["fix_versions"])
        print(
            f"::error title=Fixable vulnerability in shipped dependency::"
            f"{record['name']} {record['version']} is affected by "
            f"{record['id']}; bump the manifest floor to {fixes}."
        )
    for record in findings["transitive_blocking"]:
        fixes = ", ".join(record["fix_versions"])
        print(
            f"::error title=Fixable vulnerability in transitive dependency::"
            f"{record['name']} {record['version']} is affected by "
            f"{record['id']}; it is not a direct manifest entry, so bump the "
            f"direct parent dependency or add a constraint to reach {fixes}."
        )
    for record in findings["unfixable"]:
        print(
            f"::warning title=Unfixable vulnerability in shipped dependency::"
            f"{record['name']} {record['version']} is affected by "
            f"{record['id']} with no available fix."
        )
    governed = findings.get("governed", [])
    if governed:
        # Summarize rather than emit one annotation per finding: HA's own pins
        # carry many CVEs, and GitHub caps notices at ten per step. The full
        # list is in the report.
        packages = ", ".join(sorted({record["name"] for record in governed}))
        print(
            f"::notice title=Vulnerabilities in Home Assistant-pinned dependencies::"
            f"{len(governed)} finding(s) in {packages} at Home Assistant's own "
            f"minimum-version pin; the integration cannot bump these. Raising the "
            f"hacs.json Home Assistant floor to a release whose pin ships the fix "
            f"is the only remediation."
        )
    governed_range = findings.get("governed_range", [])
    if governed_range:
        # Same ten-notice cap applies; the full list is in the report. Distinct
        # remediation from the exact case: HA fixes only a floor, so the manifest
        # COULD tighten it -- deliberately deferred, hence a notice, not a block.
        packages = ", ".join(sorted({record["name"] for record in governed_range}))
        print(
            f"::notice title=Vulnerabilities in Home Assistant range-governed "
            f"dependencies::{len(governed_range)} finding(s) in {packages}; Home "
            f"Assistant permits an older version satisfying its range. The "
            f"integration could declare a tighter manifest floor name>=<fixed>, "
            f"but deliberately defers these HA-ecosystem transitives to Home "
            f"Assistant. Reported for visibility, non-blocking."
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Block a PR when a manifest.json runtime dependency has a fixable "
            "CVE, excluding packages Home Assistant pins itself."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(DEFAULT_MANIFEST),
        help=f"Path to manifest.json (default: {DEFAULT_MANIFEST}).",
    )
    parser.add_argument(
        "--ha-constraints",
        type=Path,
        action="append",
        required=True,
        metavar="PATH",
        help=(
            "Path to a Home Assistant package_constraints.txt snapshot; repeat "
            "to cover several supported HA versions. The first is the declared-"
            "minimum and drives the floor/HA-pin audit; each additional snapshot "
            "adds an online re-audit of governed packages at that version's pins "
            "(a CVE living only in a newer HA pin would otherwise be invisible). "
            "Packages pinned in any snapshot are excluded from the blocking "
            "decision."
        ),
    )
    parser.add_argument(
        "--audit-json",
        type=Path,
        default=None,
        help=(
            "Use a pre-generated pip-audit JSON report instead of invoking "
            "pip-audit (no network; used by the test suite). When governed "
            "transitive packages resolve away from Home Assistant's pin, "
            "--governed-audit-json must also be supplied to stay offline."
        ),
    )
    parser.add_argument(
        "--governed-audit-json",
        type=Path,
        default=None,
        help=(
            "Use a pre-generated pip-audit JSON report for the governed "
            "transitive re-audit instead of invoking pip-audit (no network; "
            "used by the test suite). Required alongside --audit-json when pass "
            "1 reaches governed transitives resolved away from HA's pin."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the full report even when there are no blocking findings.",
    )
    return parser.parse_args(argv)


def _validate_audit_shape(data: object) -> str | None:
    """Return an error string if ``data`` is not a usable pip-audit report.

    Downstream code dereferences the report type-specifically, so every field
    that reaches such a call must be validated or a malformed pre-generated
    report would raise ``AttributeError``/``TypeError`` deep inside processing
    instead of yielding the documented tooling-error exit code 2. The exhaustive
    set of type-specific leaf uses is:
      - a dependency ``name`` -> ``canonicalize_name`` and the classification
        bucket decision (needs a *non-empty* string: an absent/empty name
        canonicalizes to ``""``, matches no owned/governed set, and a fixable
        vuln on it is misrouted into ``transitive_blocking`` -- a spurious
        exit 1 under an empty package name instead of the tooling-error exit 2);
      - a vuln ``id`` -> ``sort_key`` order comparison and the
        :func:`_dedup_sorted_records` ``set`` key (needs a *non-empty*,
        hashable, mutually comparable string; empty ids collapse distinct
        advisories into one dedup key);
      - a vuln ``fix_versions`` -> ``list(...)`` and ``", ".join(...)`` in the
        report (needs a list of strings).
    ``version`` is the only stored field never coerced (it is compared only with
    ``==`` in :func:`reachable_governed_transitive_pins`, an order-/hash-free
    operation), so it is deliberately not policed and stays tolerated when
    absent. The required identifiers ``name`` and ``id`` are rejected when
    absent, empty, or wrong-typed; every other absent optional key stays
    tolerated exactly as the consumers tolerate it through ``dict.get``
    defaults. Validating at the
    single JSON boundary both the pass-1 and the governed re-audit share keeps a
    malformed report from silently degrading either pass.
    """
    if not isinstance(data, dict):
        return f"root is {type(data).__name__}, expected a JSON object"
    dependencies = data.get("dependencies", [])
    if not isinstance(dependencies, list):
        return f"'dependencies' is {type(dependencies).__name__}, expected a list"
    for index, dependency in enumerate(dependencies):
        error = _validate_dependency(index, dependency)
        if error is not None:
            return error
    return None


def _validate_dependency(index: int, dependency: object) -> str | None:
    """Return an error string if one ``dependencies`` entry is unusable.

    Split out of :func:`_validate_audit_shape` so each function keeps a single,
    readable responsibility (and none trips the return-count lint); together they
    enforce exactly the shape the consumers dereference.
    """
    if not isinstance(dependency, dict):
        return (
            f"dependencies[{index}] is {type(dependency).__name__}, "
            "expected a JSON object"
        )
    name = dependency.get("name", "")
    if not isinstance(name, str):
        return f"dependencies[{index}].name is {type(name).__name__}, expected a string"
    if not name:
        return (
            f"dependencies[{index}].name is missing or empty, "
            "expected a non-empty string"
        )
    vulns = dependency.get("vulns", [])
    if not isinstance(vulns, list):
        return f"dependencies[{index}].vulns is {type(vulns).__name__}, expected a list"
    for vuln_index, vuln in enumerate(vulns):
        error = _validate_vuln(index, vuln_index, vuln)
        if error is not None:
            return error
    return None


def _validate_vuln(index: int, vuln_index: int, vuln: object) -> str | None:
    """Return an error string if one ``vulns`` entry is unusable.

    Guards the two vuln leaves the consumers dereference type-specifically: an
    ``id`` used as an order/``set`` key (needs a non-empty string) and
    ``fix_versions`` joined into the report (needs a list of strings).
    """
    where = f"dependencies[{index}].vulns[{vuln_index}]"
    if not isinstance(vuln, dict):
        return f"{where} is {type(vuln).__name__}, expected a JSON object"
    vuln_id = vuln.get("id", "")
    if not isinstance(vuln_id, str):
        return f"{where}.id is {type(vuln_id).__name__}, expected a string"
    if not vuln_id:
        return f"{where}.id is missing or empty, expected a non-empty string"
    fix_versions = vuln.get("fix_versions", [])
    if not isinstance(fix_versions, list):
        return f"{where}.fix_versions is {type(fix_versions).__name__}, expected a list"
    for fix_index, fix in enumerate(fix_versions):
        if not isinstance(fix, str):
            return (
                f"{where}.fix_versions[{fix_index}] is "
                f"{type(fix).__name__}, expected a string"
            )
    return None


def obtain_audit(
    audit_json: Path | None,
    audit_requirements: list[str],
    *,
    no_deps: bool = False,
) -> tuple[dict[str, Any] | None, int]:
    """Return ``(audit, 0)`` or ``(None, exit_code)`` on a tooling error.

    Reads a pre-generated report when ``audit_json`` is given; otherwise runs
    pip-audit over ``audit_requirements`` (integration-owned entries pinned to
    their floor, HA-governed entries pinned to Home Assistant's own version). A
    missing/malformed report or a pip-audit exit code > 1 yields ``(None, 2)``
    (tooling error), never a spurious block.

    ``no_deps`` is forwarded to :func:`run_pip_audit` so the governed-transitive
    re-audit pass evaluates exactly the listed pins without transitive
    resolution.
    """
    if audit_json is not None:
        if not audit_json.is_file():
            print(f"error: audit JSON not found: {audit_json}", file=sys.stderr)
            return None, 2
        source = audit_json
        label = f"audit JSON {audit_json}"
    elif not audit_requirements:
        return {"dependencies": []}, 0
    else:
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as handle:
            audit_path = Path(handle.name)
        exit_code = run_pip_audit(audit_requirements, audit_path, no_deps=no_deps)
        if exit_code > 1:
            audit_path.unlink(missing_ok=True)
            print(
                f"error: pip-audit failed with exit code {exit_code}", file=sys.stderr
            )
            return None, 2
        source = audit_path
        label = "pip-audit report"

    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"error: malformed {label}: {exc}", file=sys.stderr)
        return None, 2
    finally:
        if audit_json is None:
            source.unlink(missing_ok=True)

    shape_error = _validate_audit_shape(data)
    if shape_error is not None:
        print(f"error: malformed {label}: {shape_error}", file=sys.stderr)
        return None, 2
    return data, 0


def _dedup_sorted_records(records: list[Record]) -> list[Record]:
    """Return ``records`` deduplicated by ``(name, id)`` and sorted the same way.

    Mirrors :func:`classify_audit`'s ``sort_key`` so a merged governed bucket
    keeps identical ordering semantics, and drops a finding that appears in both
    the pass-1 and the re-audit result.
    """
    seen: set[tuple[str, str]] = set()
    deduped: list[Record] = []
    for record in records:
        key = (record["name"], record["id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return sorted(deduped, key=lambda record: (record["name"], record["id"]))


def _canonical_names(requirements: list[str]) -> set[str]:
    """Return the canonical names of ``requirements``, skipping unparseable ones.

    Mirrors the owned-name derivation in :func:`main`: an entry with no valid
    PEP 508 name has no canonical form and is simply omitted rather than raising.
    """
    names: set[str] = set()
    for requirement in requirements:
        try:
            names.add(requirement_project_name(requirement))
        except InvalidRequirement:  # pragma: no cover - governed entries parse
            continue
    return names


def apply_governed_transitive_reaudit(  # noqa: PLR0913 - 7 irreducible inputs; see docstring
    findings: dict[str, list[Record]],
    reachable: dict[str, str],
    *,
    governed_audit_json: Path | None,
    offline: bool = False,
    governed: set[str],
    exact_governed: set[str],
    owned_names: set[str],
) -> int | None:
    """Fold a governed-transitive re-audit into the governed buckets in place.

    ``reachable`` is the ``name -> pin`` map from
    :func:`reachable_governed_transitive_pins`: governed packages that reach the
    manifest only transitively and whose pass-1 version differs from Home
    Assistant's pin. Pass 1 audited them at the resolver's newest pick, so a CVE
    living in HA's pinned version but fixed in the newer release would drop out
    of the report. This runs a second, deterministic ``--no-deps`` pip-audit pass
    over ``name==pin`` specifiers for exactly those packages and merges the
    result authoritatively into the governed buckets (re-audited names replace
    their pass-1 entries).

    Because a re-audited package may be exact-governed (``governed``) or
    range-governed (``governed_range``), ``exact_governed`` is forwarded to
    :func:`classify_audit` and *both* governed buckets are merged: a range-
    governed transitive such as ``urllib3>=2.0`` lands in ``governed_range``, and
    dropping either merge would silently lose its finding.

    Returns ``None`` on success (``findings`` possibly updated in place), or a
    tooling exit code when the second pass could not run, mirroring
    :func:`obtain_audit` so a re-audit failure never becomes a spurious block.
    An empty ``reachable`` is a no-op: the second pass is skipped entirely.

    ``offline`` is set when the caller ran pass 1 from a pre-generated report
    (``--audit-json``), whose documented contract is "no network". In that mode a
    live second pass would break the contract, and silently *skipping* it would
    reopen the false negative this function exists to prevent (a CVE living in
    HA's pin dropping out) behind an offline flag. So when a second pass is
    required (non-empty ``reachable``) but no pre-generated governed report is
    supplied (``governed_audit_json is None``), return tooling exit 2 with an
    actionable message instead of going live. Exit 2 mirrors the tooling-error
    contract above, never a spurious block (exit 1) or a silent pass (exit 0).
    """
    if not reachable:
        return None
    if offline and governed_audit_json is None:
        print(
            "error: offline audit (--audit-json) reached governed transitive "
            "packages that require a re-audit at Home Assistant's pins "
            f"({', '.join(sorted(reachable))}), but --governed-audit-json was "
            "not supplied. Provide a pre-generated governed re-audit report to "
            "keep the preview offline.",
            file=sys.stderr,
        )
        return 2
    reaudit_reqs = sorted(f"{name}=={pin}" for name, pin in reachable.items())
    reaudit, reaudit_exit = obtain_audit(
        governed_audit_json, reaudit_reqs, no_deps=True
    )
    if reaudit is None:
        return reaudit_exit
    # Every re-audited name is HA-governed; classify_audit routes each finding
    # into ``governed`` (exact pin) or ``governed_range`` (range floor) at HA's
    # own worst-case version.
    reaudit_findings = classify_audit(reaudit, owned_names, governed, exact_governed)
    reaudited = set(reachable)
    for bucket in ("governed", "governed_range"):
        findings[bucket] = _dedup_sorted_records(
            [
                record
                for record in findings.get(bucket, [])
                if record["name"] not in reaudited
            ]
            + reaudit_findings[bucket]
        )
    return None


def apply_additional_ha_version_reaudit(  # noqa: PLR0913 - findings + audit context + classification sets; see docstring
    findings: dict[str, list[Record]],
    additional_texts: list[str],
    primary_pins: dict[str, str],
    audit: dict[str, Any],
    *,
    offline: bool,
    owned_names: set[str],
    ha_governed: list[str],
    governed: set[str],
    exact_governed: set[str],
) -> int | None:
    """Audit governed packages at each additional supported HA version's pins.

    The declared-minimum audit only covers Home Assistant's pins for the oldest
    supported version. A newer supported release may pin a governed package at a
    different version whose CVE would otherwise be invisible. For every
    additional snapshot in ``additional_texts`` this re-audits, with a
    deterministic ``--no-deps`` pass, the governed packages this manifest
    actually reaches (its direct governed entries plus the governed transitives
    pass 1 resolved) at any pin not already audited, and folds the findings into
    the governed buckets by *adding* -- a finding present only at a newer pin is
    surfaced alongside the declared-minimum result rather than replacing it.

    Needs the network, so it is a no-op when ``offline`` (the ``--audit-json``
    preview documents the gap instead). Returns a tooling exit code when an
    additional pass could not run (mirroring :func:`obtain_audit`), else ``None``.
    """
    resolved_governed = {
        canonicalize_name(dependency.get("name", ""))
        for dependency in audit.get("dependencies", [])
    } & governed
    relevant_governed = _canonical_names(ha_governed) | resolved_governed
    for text in additional_texts:
        to_reaudit = additional_governed_reaudit_pins(
            parse_ha_governed_pins(text), primary_pins, audit, relevant_governed
        )
        if not to_reaudit or offline:
            continue
        extra_reqs = sorted(f"{name}=={pin}" for name, pin in to_reaudit.items())
        extra_audit, extra_exit = obtain_audit(None, extra_reqs, no_deps=True)
        if extra_audit is None:
            return extra_exit
        extra_findings = classify_audit(
            extra_audit, owned_names, governed, exact_governed
        )
        for bucket in ("governed", "governed_range"):
            findings[bucket] = _dedup_sorted_records(
                findings.get(bucket, []) + extra_findings[bucket]
            )
    return None


def apply_owned_as_declared_audit(  # noqa: PLR0913 - findings + owned + classification sets; see docstring
    findings: dict[str, list[Record]],
    owned: list[str],
    *,
    offline: bool,
    owned_names: set[str],
    governed: set[str],
    exact_governed: set[str],
) -> int | None:
    """Audit the integration-owned requirements *as declared* and fold results in.

    The floor pass audits the minimum a user may install (``pkg>=X`` ->
    ``pkg==X``); this audits ``owned`` unchanged so the resolver picks the newest
    installable release, catching a CVE introduced above the floor but still
    within the permitted range (advisory ranges are not monotonic from a
    package's first release). Only the actionable owned/transitive buckets are
    merged: a governed package the resolver drags in at its newest pick is
    audited authoritatively at Home Assistant's own pins elsewhere, not here.

    Needs the network, so it is a no-op when ``offline`` or there are no owned
    requirements. Returns a tooling exit code when the pass could not run
    (mirroring :func:`obtain_audit`), else ``None``.
    """
    if not owned or offline:
        return None
    latest_audit, latest_exit = obtain_audit(None, owned)
    if latest_audit is None:
        return latest_exit
    latest_findings = classify_audit(
        latest_audit, owned_names, governed, exact_governed
    )
    for bucket in ("blocking", "unfixable", "transitive_blocking", "transitive"):
        findings[bucket] = _dedup_sorted_records(
            findings.get(bucket, []) + latest_findings[bucket]
        )
    return None


def apply_audit_span_passes(  # noqa: PLR0913 - orchestration seam; see docstring
    findings: dict[str, list[Record]],
    audit: dict[str, Any],
    constraint_texts: list[str],
    primary_pins: dict[str, str],
    owned: list[str],
    ha_governed: list[str],
    *,
    owned_names: set[str],
    governed: set[str],
    exact_governed: set[str],
    audit_json: Path | None,
    governed_audit_json: Path | None,
) -> int | None:
    """Fold every re-audit and span-widening pass into ``findings`` in order.

    Thin orchestration seam over the three pass helpers so :func:`main` carries a
    single tooling-exit guard rather than one per pass. Runs, in sequence:
      1. the declared-minimum governed-transitive re-audit at Home Assistant's
         own pins (:func:`apply_governed_transitive_reaudit`);
      2. governed re-audits at each additional supported HA version's pins
         (:func:`apply_additional_ha_version_reaudit`);
      3. an as-declared audit of the integration-owned requirements
         (:func:`apply_owned_as_declared_audit`).
    Each pass mutates ``findings`` in place. Returns the first tooling exit code
    a pass reports (mirroring :func:`obtain_audit`), or ``None`` when all pass.
    The offline-preview coverage gap is derived by the caller from its inputs and
    is not returned here.
    """
    offline = audit_json is not None
    reachable = reachable_governed_transitive_pins(
        audit, primary_pins, _canonical_names(ha_governed), owned_names
    )
    reaudit_exit = apply_governed_transitive_reaudit(
        findings,
        reachable,
        governed_audit_json=governed_audit_json,
        offline=offline,
        governed=governed,
        exact_governed=exact_governed,
        owned_names=owned_names,
    )
    if reaudit_exit is not None:
        return reaudit_exit
    additional_exit = apply_additional_ha_version_reaudit(
        findings,
        constraint_texts[1:],
        primary_pins,
        audit,
        offline=offline,
        owned_names=owned_names,
        ha_governed=ha_governed,
        governed=governed,
        exact_governed=exact_governed,
    )
    if additional_exit is not None:
        return additional_exit
    return apply_owned_as_declared_audit(
        findings,
        owned,
        offline=offline,
        owned_names=owned_names,
        governed=governed,
        exact_governed=exact_governed,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.manifest.is_file():
        print(f"error: manifest not found: {args.manifest}", file=sys.stderr)
        return 2
    for constraints_path in args.ha_constraints:
        if not constraints_path.is_file():
            print(
                f"error: HA constraints not found: {constraints_path}",
                file=sys.stderr,
            )
            return 2

    try:
        requirements = load_manifest_requirements(args.manifest)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"error: cannot read manifest {args.manifest}: {exc}", file=sys.stderr)
        return 2

    constraint_texts = [
        path.read_text(encoding="utf-8") for path in args.ha_constraints
    ]
    # The blocking decision is the worst case for the integration's own floors:
    # the declared-minimum Home Assistant a user may run. Governance is therefore
    # derived from the PRIMARY (first) snapshot only. A package the minimum HA
    # does not pin is integration-owned there -- the user installs the manifest
    # floor, which no newer HA pin overrides -- so a fixable floor CVE must still
    # block even if a *newer* supported HA governs that package. Unioning the
    # governed names across snapshots would flip such a package to non-blocking
    # and hide the finding (a false green). Additional snapshots therefore do NOT
    # widen the non-blocking governed set; they only add re-audits of packages
    # already governed at the minimum, at their newer pins (see below).
    primary_pins = parse_ha_governed_pins(constraint_texts[0])
    governed = parse_ha_governed_names(constraint_texts[0])
    exact_governed = parse_ha_exact_governed_names(constraint_texts[0])
    owned, ha_governed = partition_requirements(requirements, governed)

    owned_names: set[str] = set()
    for requirement in owned:
        try:
            owned_names.add(requirement_project_name(requirement))
        except InvalidRequirement:
            # An unparseable manifest entry has no canonical name, so it can
            # never match a pip-audit dependency name and is not classified as
            # owned. It is still audited (below) and surfaces as a transitive
            # INFO finding rather than a silent drop.
            continue

    # Audit each package at the version a user actually runs: integration-owned
    # requirements at their declared floor, HA-governed requirements at Home
    # Assistant's own == pin (which overrides the floor at install time). This
    # is the core of Fund 2: auditing the governed floor would report CVEs in a
    # version no user runs and let a blind "resolved by HA" label hide whether
    # HA's pinned version is itself vulnerable.
    owned_audit, unbounded = floor_pin_requirements(owned)
    governed_audit = ha_pin_requirements(ha_governed, primary_pins)
    audit_requirements = owned_audit + governed_audit

    audit, exit_code = obtain_audit(args.audit_json, audit_requirements)
    if audit is None:
        return exit_code

    # Pass the *full* HA-governed name set (not only the manifest entries HA
    # pins): a package HA governs which surfaces only transitively must not be
    # treated as an actionable transitive finding, since the integration cannot
    # bump HA's runtime pin any more than it can for a direct governed entry.
    findings = classify_audit(audit, owned_names, governed, exact_governed)

    # Re-audit governed packages that reach the manifest only transitively at
    # Home Assistant's own pin: pass 1 resolved them to the newest compatible
    # pick, not HA's pin, so a CVE living in HA's pinned version would otherwise
    # drop out of the report. The result folds into the governed bucket.
    span_exit = apply_audit_span_passes(
        findings,
        audit,
        constraint_texts,
        primary_pins,
        owned,
        ha_governed,
        owned_names=owned_names,
        governed=governed,
        exact_governed=exact_governed,
        audit_json=args.audit_json,
        governed_audit_json=args.governed_audit_json,
    )
    if span_exit is not None:
        return span_exit

    # The offline preview (--audit-json) runs neither the as-declared owned pass
    # nor the additional-HA-version governed passes (both need the network), so
    # flag the reduced coverage whenever either would have applied.
    offline_preview_gap = args.audit_json is not None and (
        bool(owned) or len(constraint_texts) > 1
    )
    report = render_report(
        owned,
        ha_governed,
        unbounded,
        findings,
        offline_preview_gap=offline_preview_gap,
    )

    blocking = blocking_records(findings)
    if args.verbose or blocking:
        print(report)
    emit_github_annotations(findings)

    if blocking:
        print(
            f"FAIL: {len(blocking)} fixable vulnerability/vulnerabilities in "
            "shipped integration-owned or transitive dependencies."
        )
    else:
        print(
            "OK: no fixable vulnerability in shipped integration-owned or "
            "transitive dependencies."
        )
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
