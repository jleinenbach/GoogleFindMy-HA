#!/usr/bin/env python3
"""Block PRs that ship an integration-owned dependency with a fixable CVE.

Home Assistant installs the packages listed in ``manifest.json`` ``requirements``
into its own environment; that list, not ``poetry.lock``, is what reaches an end
user. The repository's ``pip-audit`` workflow audits the Poetry dev/test lock and
is deliberately report-only, so a vulnerability in a shipped runtime dependency
never blocks a merge. This script closes that gap with a narrow, *actionable*
gate.

Scope and policy:
- It audits only the ``manifest.json`` ``requirements`` (the end-user surface),
  resolved exactly as Home Assistant would resolve the declared specifiers.
- It excludes packages that Home Assistant itself pins in its
  ``package_constraints.txt`` for the declared-minimum HA version (e.g. aiohttp,
  cryptography). The integration cannot raise its floor above Home Assistant's
  ``==`` pin without making installation impossible, so a finding there is not
  actionable and must not block. These are reported for transparency only.
- Among the remaining integration-owned packages it blocks (exit 1) only when a
  package has at least one ``fix_versions`` entry, i.e. the CVE is fixable by
  bumping the manifest floor. Unfixable (won't-fix) findings such as ``ecdsa``
  are reported as warnings and never block.

The gate is intentionally green today: the only integration-owned vulnerability
is ``ecdsa`` (unfixable), and every other shipped package resolves clean. It
turns red the first time a shipped, integration-owned package gains a fixable
CVE, which is exactly the case the report-only job waves through.

Usage:
    python script/audit_manifest.py --ha-constraints ha_constraints.txt
    python script/audit_manifest.py --ha-constraints ha_constraints.txt --verbose

    # Preview classification against a pre-generated pip-audit report (no
    # network, used by the test suite):
    python script/audit_manifest.py --ha-constraints ha_constraints.txt \
        --audit-json audit.json

Exit status:
    0  no blocking findings (may still print warnings)
    1  at least one integration-owned, fixable vulnerability -> block the PR
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

DEFAULT_MANIFEST = "custom_components/googlefindmy/manifest.json"

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


def parse_ha_governed_names(constraints_text: str) -> set[str]:
    """Return the canonical names Home Assistant pins in its constraints file.

    Only ``name==version`` pins are treated as governed; comments, blank lines
    and range/marker entries are ignored. Home Assistant's generated
    ``package_constraints.txt`` uses exact ``==`` pins throughout.
    """
    governed: set[str] = set()
    for raw_line in constraints_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            continue
        name = line.split("==", 1)[0].strip()
        if not name:
            continue
        try:
            governed.add(canonicalize_name(Requirement(line).name))
        except InvalidRequirement:
            # Fall back to the bare left-hand side for non-PEP 508 lines.
            governed.add(canonicalize_name(name))
    return governed


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
    audit: dict[str, Any], owned_names: set[str]
) -> dict[str, list[Record]]:
    """Classify a pip-audit JSON report against the integration-owned names.

    Returns a mapping with three deterministic, sorted lists of findings, each a
    ``{"name","version","id","fix_versions"}`` record:
      - ``blocking``: integration-owned package with a fixable vulnerability.
      - ``unfixable``: integration-owned package with no available fix.
      - ``transitive``: a vulnerability in a package that is not a direct
        manifest entry (a transitive dependency); reported, never blocking.
    """
    blocking: list[Record] = []
    unfixable: list[Record] = []
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
            if name not in owned_names:
                transitive.append(record)
            elif record["fix_versions"]:
                blocking.append(record)
            else:
                unfixable.append(record)

    def sort_key(record: Record) -> tuple[str, str]:
        return (record["name"], record["id"])

    return {
        "blocking": sorted(blocking, key=sort_key),
        "unfixable": sorted(unfixable, key=sort_key),
        "transitive": sorted(transitive, key=sort_key),
    }


def run_pip_audit(requirements: list[str], output_path: Path) -> int:
    """Run pip-audit on the given requirement specifiers, writing JSON output.

    Returns the pip-audit exit code: 0 (clean), 1 (vulnerabilities found) or
    >1 (tooling error). Callers decide what to block on from the JSON, not from
    the exit code, because a vulnerable-but-unfixable finding still yields 1.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    ) as handle:
        handle.write("\n".join(requirements) + "\n")
        requirements_path = handle.name
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "-r",
                requirements_path,
                "-f",
                "json",
                "-o",
                str(output_path),
            ],
            check=False,
        )
    finally:
        Path(requirements_path).unlink(missing_ok=True)
    return completed.returncode


def render_report(
    owned: list[str],
    ha_governed: list[str],
    findings: dict[str, list[Record]],
) -> str:
    """Return a human-facing summary of the audit result."""
    lines: list[str] = []
    lines.append("Manifest-only pip-audit (integration-owned requirements)")
    lines.append("")
    lines.append(f"Integration-owned packages audited: {len(owned)}")
    for requirement in sorted(owned):
        lines.append(f"  - {requirement}")
    lines.append("")
    lines.append(f"Home Assistant-governed packages excluded: {len(ha_governed)}")
    for requirement in sorted(ha_governed):
        lines.append(f"  - {requirement}  (pinned by Home Assistant)")
    lines.append("")

    blocking = findings["blocking"]
    unfixable = findings["unfixable"]
    transitive = findings["transitive"]

    if blocking:
        lines.append(f"BLOCKING ({len(blocking)}) - fixable, integration-owned:")
        for record in blocking:
            fixes = ", ".join(record["fix_versions"])
            lines.append(
                f"  - {record['name']} {record['version']}: "
                f"{record['id']} (fix: {fixes})"
            )
    else:
        lines.append("BLOCKING (0) - no fixable integration-owned vulnerability.")
    lines.append("")

    if unfixable:
        lines.append(f"WARN ({len(unfixable)}) - integration-owned, no fix available:")
        for record in unfixable:
            lines.append(f"  - {record['name']} {record['version']}: {record['id']}")
        lines.append("")

    if transitive:
        lines.append(
            f"INFO ({len(transitive)}) - transitive dependency vulnerabilities "
            "(not a direct manifest entry):"
        )
        for record in transitive:
            state = "fixable" if record["fix_versions"] else "no fix"
            lines.append(
                f"  - {record['name']} {record['version']}: {record['id']} ({state})"
            )
        lines.append("")

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
    for record in findings["unfixable"]:
        print(
            f"::warning title=Unfixable vulnerability in shipped dependency::"
            f"{record['name']} {record['version']} is affected by "
            f"{record['id']} with no available fix."
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
        "--verbose",
        action="store_true",
        help="Print the full report even when there are no blocking findings.",
    )
    return parser.parse_args(argv)


def obtain_audit(
    audit_json: Path | None, owned: list[str]
) -> tuple[dict[str, Any] | None, int]:
    """Return ``(audit, 0)`` or ``(None, exit_code)`` on a tooling error.

    Reads a pre-generated report when ``audit_json`` is given; otherwise runs
    pip-audit over the integration-owned requirements. A missing/malformed
    report or a pip-audit exit code > 1 yields ``(None, 2)`` (tooling error),
    never a spurious block.
    """
    if audit_json is not None:
        if not audit_json.is_file():
            print(f"error: audit JSON not found: {audit_json}", file=sys.stderr)
            return None, 2
        source = audit_json
        label = f"audit JSON {audit_json}"
    elif not owned:
        return {"dependencies": []}, 0
    else:
        with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as handle:
            audit_path = Path(handle.name)
        exit_code = run_pip_audit(owned, audit_path)
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

    governed = parse_ha_governed_names(args.ha_constraints.read_text(encoding="utf-8"))
    owned, ha_governed = partition_requirements(requirements, governed)

    owned_names: set[str] = set()
    for requirement in owned:
        try:
            owned_names.add(requirement_project_name(requirement))
        except InvalidRequirement:
            # An unparseable manifest entry has no canonical name, so it can
            # never match a pip-audit dependency name and is simply not
            # classified as owned. partition_requirements still audits it.
            continue

    audit, exit_code = obtain_audit(args.audit_json, owned)
    if audit is None:
        return exit_code

    findings = classify_audit(audit, owned_names)
    report = render_report(owned, ha_governed, findings)

    if args.verbose or findings["blocking"]:
        print(report)
    emit_github_annotations(findings)

    if findings["blocking"]:
        print(
            f"FAIL: {len(findings['blocking'])} fixable vulnerability/"
            "vulnerabilities in shipped integration-owned dependencies."
        )
        return 1
    print("OK: no fixable vulnerability in shipped integration-owned dependencies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
