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
  side-channel, unfixable transitive findings, and *all* Home Assistant-pinned
  findings are reported but never block. HA-pinned findings never block because
  the integration cannot lower Home Assistant's runtime ``==`` pin; the only
  remediation for a fixable one is to raise the ``hacs.json`` Home Assistant
  floor to a release whose pin ships the fix, which is an integration-level
  decision surfaced honestly rather than a silent "already resolved" claim.

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

from packaging.requirements import InvalidRequirement, Requirement
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


def _requirement_floor(requirement: Requirement) -> str | None:
    """Return the lowest version the requirement's specifiers allow, or None.

    Considers inclusive lower-bound operators (``>=``, ``==``, ``~=``) and
    returns the most restrictive (highest) such bound as a version string. A
    requirement with no inclusive lower bound (a bare name, an upper-bound-only
    specifier, or an exclusive ``>X``) has no determinable floor and returns
    None; the caller then audits it at the resolver's newest pick and flags it,
    rather than fabricating a wrong ``==X`` pin. Every real manifest entry uses
    ``>=``.
    """
    best: Version | None = None
    best_text: str | None = None
    for spec in requirement.specifier:
        if spec.operator not in _LOWER_BOUND_OPERATORS:
            continue
        try:
            candidate = Version(spec.version)
        except InvalidVersion:
            continue
        if best is None or candidate > best:
            best = candidate
            best_text = spec.version
    return best_text


def floor_pin_requirements(requirements: list[str]) -> tuple[list[str], list[str]]:
    """Pin each manifest requirement to its declared floor for auditing.

    Returns ``(audit_specifiers, unbounded_names)``:
      - ``audit_specifiers``: one entry per requirement. A requirement with a
        determinable floor becomes ``name==floor`` so pip-audit evaluates the
        minimum a user may install rather than the resolver's newest pick. A
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
        audit_specifiers.append(f"{requirement.name}=={floor}")
    return audit_specifiers, unbounded


def ha_pin_requirements(
    requirements: list[str], governed_pins: dict[str, str]
) -> list[str]:
    """Pin each HA-governed requirement to Home Assistant's own ``==`` version.

    ``requirements`` are the manifest entries Home Assistant pins itself;
    ``governed_pins`` is the ``name -> version`` map from
    :func:`parse_ha_governed_pins`. Each entry becomes ``name==pin`` so the
    audit reflects the version the declared-minimum Home Assistant installs, not
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
        specifiers.append(f"{requirement.name}=={pin}")
    return specifiers


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
    worst-case version can be derived from them. On a duplicate the last wins,
    mirroring how a constraints file is applied top to bottom.
    """
    pins: dict[str, str] = {}
    for raw_line in constraints_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            # Non-PEP 508 line: salvage a bare ``name==version`` so a
            # non-standard exact constraint still governs. A range floor cannot
            # be recovered without a parseable specifier, so such a line is
            # dropped rather than guessed.
            name, separator, rhs = line.partition("==")
            name = name.strip()
            version = rhs.split(";", 1)[0].split(",", 1)[0].strip()
            if not separator or not name or not version:
                continue
            pins[canonicalize_name(name)] = version
            continue
        floor = _requirement_floor(requirement)
        if floor is None:
            continue
        pins[canonicalize_name(requirement.name)] = floor
    return pins


def parse_ha_governed_names(constraints_text: str) -> set[str]:
    """Return the canonical names Home Assistant pins in its constraints file.

    Thin wrapper over :func:`parse_ha_governed_pins`; the name set is derived
    from the pin mapping so the two never diverge.
    """
    return set(parse_ha_governed_pins(constraints_text))


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
) -> dict[str, list[Record]]:
    """Classify a pip-audit JSON report against the manifest name sets.

    Returns a mapping with five deterministic, sorted lists of findings, each a
    ``{"name","version","id","fix_versions"}`` record:
      - ``blocking``: integration-owned package with a fixable vulnerability
        (remediation: bump the manifest floor).
      - ``unfixable``: integration-owned package with no available fix.
      - ``governed``: a package Home Assistant pins itself, whether a direct
        manifest entry (audited at HA's own pin) or a transitive dependency that
        HA still governs; reported for transparency, never blocking. The
        integration cannot lower HA's runtime pin, so blocking would be a
        permanent red the contributor cannot clear; a fixable one is actionable
        only by raising the ``hacs.json`` HA floor.
      - ``transitive_blocking``: a *fixable* vulnerability in a package that is
        neither a direct manifest entry nor HA-governed (a truly transitive
        dependency the integration can reach). Unlike the governed case this IS
        actionable, by bumping the direct parent dependency or adding a
        constraint, so it blocks.
      - ``transitive``: an *unfixable*, non-governed transitive-dependency
        vulnerability; reported, never blocking.

    ``owned_names`` and ``governed_names`` decide the bucket. ``governed_names``
    is the *full* set of names Home Assistant pins (not only the HA-governed
    manifest entries), so a transitive package HA governs is never mistaken for
    an actionable transitive finding. Whether a non-owned, non-governed
    (truly transitive) finding blocks then depends on whether it carries a fix,
    mirroring the owned fixable/unfixable split.
    """
    governed = governed_names or set()
    blocking: list[Record] = []
    unfixable: list[Record] = []
    governed_findings: list[Record] = []
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
            elif name in governed:
                governed_findings.append(record)
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
) -> str:
    """Return a human-facing summary of the audit result."""
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
        required=True,
        help=(
            "Path to Home Assistant's package_constraints.txt for the "
            "declared-minimum HA version; packages pinned there are excluded "
            "from the blocking decision."
        ),
    )
    parser.add_argument(
        "--audit-json",
        type=Path,
        default=None,
        help=(
            "Use a pre-generated pip-audit JSON report instead of invoking "
            "pip-audit (no network; used by the test suite)."
        ),
    )
    parser.add_argument(
        "--governed-audit-json",
        type=Path,
        default=None,
        help=(
            "Use a pre-generated pip-audit JSON report for the governed "
            "transitive re-audit instead of invoking pip-audit (no network; "
            "used by the test suite)."
        ),
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the full report even when there are no blocking findings.",
    )
    return parser.parse_args(argv)


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
        return json.loads(source.read_text(encoding="utf-8")), 0
    except (json.JSONDecodeError, OSError) as exc:
        print(f"error: malformed {label}: {exc}", file=sys.stderr)
        return None, 2
    finally:
        if audit_json is None:
            source.unlink(missing_ok=True)


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


def apply_governed_transitive_reaudit(
    findings: dict[str, list[Record]],
    reachable: dict[str, str],
    *,
    governed_audit_json: Path | None,
    governed: set[str],
    owned_names: set[str],
) -> int | None:
    """Fold a governed-transitive re-audit into ``findings['governed']`` in place.

    ``reachable`` is the ``name -> pin`` map from
    :func:`reachable_governed_transitive_pins`: governed packages that reach the
    manifest only transitively and whose pass-1 version differs from Home
    Assistant's pin. Pass 1 audited them at the resolver's newest pick, so a CVE
    living in HA's pinned version but fixed in the newer release would drop out
    of the report. This runs a second, deterministic ``--no-deps`` pip-audit pass
    over ``name==pin`` specifiers for exactly those packages and merges the
    result authoritatively into the governed bucket (re-audited names replace
    their pass-1 entries).

    Returns ``None`` on success (``findings`` possibly updated in place), or a
    tooling exit code when the second pass could not run, mirroring
    :func:`obtain_audit` so a re-audit failure never becomes a spurious block.
    An empty ``reachable`` is a no-op: the second pass is skipped entirely.
    """
    if not reachable:
        return None
    reaudit_reqs = sorted(f"{name}=={pin}" for name, pin in reachable.items())
    reaudit, reaudit_exit = obtain_audit(
        governed_audit_json, reaudit_reqs, no_deps=True
    )
    if reaudit is None:
        return reaudit_exit
    # Every re-audited name is HA-governed, so classify_audit routes each finding
    # into the governed bucket at HA's own pin.
    reaudit_findings = classify_audit(reaudit, owned_names, governed)
    reaudited = set(reachable)
    findings["governed"] = _dedup_sorted_records(
        [record for record in findings["governed"] if record["name"] not in reaudited]
        + reaudit_findings["governed"]
    )
    return None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not args.manifest.is_file():
        print(f"error: manifest not found: {args.manifest}", file=sys.stderr)
        return 2
    if not args.ha_constraints.is_file():
        print(
            f"error: HA constraints not found: {args.ha_constraints}",
            file=sys.stderr,
        )
        return 2

    try:
        requirements = load_manifest_requirements(args.manifest)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        print(f"error: cannot read manifest {args.manifest}: {exc}", file=sys.stderr)
        return 2

    governed_pins = parse_ha_governed_pins(
        args.ha_constraints.read_text(encoding="utf-8")
    )
    governed = set(governed_pins)
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
    governed_audit = ha_pin_requirements(ha_governed, governed_pins)
    audit_requirements = owned_audit + governed_audit

    audit, exit_code = obtain_audit(args.audit_json, audit_requirements)
    if audit is None:
        return exit_code

    # Pass the *full* HA-governed name set (not only the manifest entries HA
    # pins): a package HA governs which surfaces only transitively must not be
    # treated as an actionable transitive finding, since the integration cannot
    # bump HA's runtime pin any more than it can for a direct governed entry.
    findings = classify_audit(audit, owned_names, governed)

    # Re-audit governed packages that reach the manifest only transitively at
    # Home Assistant's own pin: pass 1 resolved them to the newest compatible
    # pick, not HA's pin, so a CVE living in HA's pinned version would otherwise
    # drop out of the report. The result folds into the governed bucket.
    reachable = reachable_governed_transitive_pins(
        audit, governed_pins, _canonical_names(ha_governed), owned_names
    )
    reaudit_exit = apply_governed_transitive_reaudit(
        findings,
        reachable,
        governed_audit_json=args.governed_audit_json,
        governed=governed,
        owned_names=owned_names,
    )
    if reaudit_exit is not None:
        return reaudit_exit

    report = render_report(owned, ha_governed, unbounded, findings)

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
