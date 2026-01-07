"""tests/test_pip_audit_security.py: pip-audit dependency vulnerability scan as pytest.

This test runs pip-audit to check for known vulnerabilities in project dependencies
and provides detailed, actionable feedback for AI agents to fix security issues.

Usage:
    pytest tests/test_pip_audit_security.py -v --no-header -p no:conftest

    Or standalone:
    python tests/test_pip_audit_security.py

The test will:
1. Run pip-audit on requirements.txt
2. FAIL on any known vulnerabilities with fix instructions
3. Provide AI-friendly remediation guidance for each finding
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Requirements files to audit (relative to repo root)
REQUIREMENTS_FILES = [
    "custom_components/googlefindmy/requirements.txt",
]

# Known vulnerabilities to ignore (with justification)
# Format: {"CVE-ID": "Justification why this is acceptable"}
IGNORED_VULNERABILITIES: dict[str, str] = {
    # ecdsa is only used for curve definitions (CurveFp, Point, SECP160r1).
    # The vulnerable sign_digest() function is NOT used in this project.
    # ECDH operations use the 'cryptography' library instead.
    "CVE-2024-23342": "ecdsa timing attack - sign_digest() not used; only curve definitions imported",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Vulnerability:
    """Represents a single vulnerability finding."""

    vuln_id: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    fix_versions: list[str] = field(default_factory=list)

    @property
    def has_fix(self) -> bool:
        """Return True if a fix version is available."""
        return bool(self.fix_versions)

    @property
    def fix_version(self) -> str:
        """Return the recommended fix version."""
        return self.fix_versions[0] if self.fix_versions else "No fix available"


@dataclass
class DependencyIssue:
    """Represents a dependency with vulnerabilities."""

    name: str
    version: str
    vulns: list[Vulnerability] = field(default_factory=list)

    @property
    def vuln_ids(self) -> list[str]:
        """Return all vulnerability IDs."""
        return [v.vuln_id for v in self.vulns]


# ---------------------------------------------------------------------------
# pip-audit runner
# ---------------------------------------------------------------------------


def run_pip_audit(requirements_file: str) -> dict[str, Any]:
    """Run pip-audit and return JSON results."""
    cmd = [
        sys.executable,
        "-m",
        "pip_audit",
        "-r",
        requirements_file,
        "-f",
        "json",
        "--progress-spinner",
        "off",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603

    if "No module named pip_audit" in result.stderr:
        pytest.skip("pip-audit not installed. Run: pip install pip-audit")

    # pip-audit returns exit code 1 when vulnerabilities are found
    # Parse stdout for JSON regardless of exit code
    stdout = result.stdout.strip()
    if not stdout:
        # Check stderr for error messages
        if result.stderr and "error" in result.stderr.lower():
            raise RuntimeError(f"pip-audit failed: {result.stderr}")
        return {"dependencies": [], "fixes": []}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        # Try to find JSON in output
        if "{" in stdout:
            json_start = stdout.index("{")
            return json.loads(stdout[json_start:])
        raise RuntimeError(f"Failed to parse pip-audit output: {e}\nOutput: {stdout[:500]}")


def parse_issues(audit_output: dict[str, Any]) -> list[DependencyIssue]:
    """Parse pip-audit JSON output into DependencyIssue objects."""
    issues = []
    for dep in audit_output.get("dependencies", []):
        vulns = dep.get("vulns", [])
        if vulns:
            parsed_vulns = [
                Vulnerability(
                    vuln_id=v.get("id", "Unknown"),
                    aliases=v.get("aliases", []),
                    description=v.get("description", ""),
                    fix_versions=v.get("fix_versions", []),
                )
                for v in vulns
            ]
            issues.append(
                DependencyIssue(
                    name=dep.get("name", "Unknown"),
                    version=dep.get("version", "Unknown"),
                    vulns=parsed_vulns,
                )
            )
    return issues


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_ai_report(issues: list[DependencyIssue], ignored: dict[str, str]) -> str:
    """Generate a detailed AI-friendly report with remediation instructions."""
    # Filter out ignored vulnerabilities
    active_issues = []
    ignored_issues = []

    for issue in issues:
        active_vulns = []
        ignored_vulns = []
        for vuln in issue.vulns:
            if vuln.vuln_id in ignored:
                ignored_vulns.append((vuln, ignored[vuln.vuln_id]))
            else:
                active_vulns.append(vuln)

        if active_vulns:
            active_issues.append(
                DependencyIssue(name=issue.name, version=issue.version, vulns=active_vulns)
            )
        if ignored_vulns:
            ignored_issues.append((issue.name, issue.version, ignored_vulns))

    lines = []
    lines.append("=" * 80)
    lines.append("🔐 PIP-AUDIT DEPENDENCY VULNERABILITY REPORT")
    lines.append("=" * 80)
    lines.append("")

    total_vulns = sum(len(i.vulns) for i in active_issues)
    fixable = sum(1 for i in active_issues for v in i.vulns if v.has_fix)
    unfixable = total_vulns - fixable

    lines.append(f"📊 Summary: {total_vulns} vulnerabilities in {len(active_issues)} packages")
    lines.append(f"   ✅ Fixable: {fixable} | ⚠️ No fix available: {unfixable}")
    lines.append("")

    if not active_issues:
        lines.append("✅ No active vulnerabilities found!")
        if ignored_issues:
            lines.append("")
            lines.append(f"ℹ️  {len(ignored_issues)} vulnerabilities ignored (see IGNORED_VULNERABILITIES)")
        return "\n".join(lines)

    lines.append("⚠️  CRITICAL: The following vulnerabilities were found!")
    lines.append("")

    # Group by fixable vs unfixable
    fixable_issues = [(i, v) for i in active_issues for v in i.vulns if v.has_fix]
    unfixable_issues = [(i, v) for i in active_issues for v in i.vulns if not v.has_fix]

    if fixable_issues:
        lines.append("-" * 80)
        lines.append(f"🔧 FIXABLE VULNERABILITIES ({len(fixable_issues)} found)")
        lines.append("-" * 80)
        lines.append("")

        for issue, vuln in fixable_issues:
            lines.append(f"### [{vuln.vuln_id}] {issue.name} {issue.version}")
            lines.append(f"🔗 Aliases: {', '.join(vuln.aliases) if vuln.aliases else 'None'}")
            lines.append(f"✅ Fix available: Upgrade to {vuln.fix_version}")
            lines.append("")
            lines.append(f"📝 {vuln.description[:500]}...")
            lines.append("")
            lines.append("**AI ACTION REQUIRED:**")
            lines.append("```bash")
            lines.append(f"# Update requirements.txt:")
            lines.append(f"# Change: {issue.name}>={issue.version}")
            lines.append(f"# To:     {issue.name}>={vuln.fix_version}")
            lines.append("```")
            lines.append("")

    if unfixable_issues:
        lines.append("-" * 80)
        lines.append(f"⚠️  UNFIXABLE VULNERABILITIES ({len(unfixable_issues)} found)")
        lines.append("-" * 80)
        lines.append("")
        lines.append("These vulnerabilities have no fix available from the package maintainers.")
        lines.append("Consider the following options:")
        lines.append("")

        for issue, vuln in unfixable_issues:
            lines.append(f"### [{vuln.vuln_id}] {issue.name} {issue.version}")
            lines.append(f"🔗 Aliases: {', '.join(vuln.aliases) if vuln.aliases else 'None'}")
            lines.append(f"❌ No fix available")
            lines.append("")
            lines.append(f"📝 {vuln.description}")
            lines.append("")
            lines.append("**AI ACTION OPTIONS:**")
            lines.append("")
            lines.append("1. **Ignore if risk is acceptable** - Add to IGNORED_VULNERABILITIES:")
            lines.append("   ```python")
            lines.append(f'   IGNORED_VULNERABILITIES = {{')
            lines.append(f'       "{vuln.vuln_id}": "Justification: [explain why this is acceptable]",')
            lines.append(f'   }}')
            lines.append("   ```")
            lines.append("")
            lines.append("2. **Find alternative package** - Search for a replacement:")
            lines.append(f"   - Search PyPI for alternatives to `{issue.name}`")
            lines.append(f"   - Evaluate if the vulnerable functionality is actually used")
            lines.append("")
            lines.append("3. **Mitigate in code** - If the vulnerable function is used:")
            lines.append(f"   - Review code that imports `{issue.name}`")
            lines.append(f"   - Avoid using the affected functionality if possible")
            lines.append("")

    if ignored_issues:
        lines.append("-" * 80)
        lines.append(f"ℹ️  IGNORED VULNERABILITIES ({sum(len(v) for _, _, v in ignored_issues)} found)")
        lines.append("-" * 80)
        lines.append("")
        for name, version, vulns in ignored_issues:
            for vuln, justification in vulns:
                lines.append(f"- [{vuln.vuln_id}] {name} {version}")
                lines.append(f"  Justification: {justification}")
        lines.append("")

    lines.append("=" * 80)
    lines.append("🤖 AI AGENT INSTRUCTIONS")
    lines.append("=" * 80)
    lines.append("")
    lines.append("1. For FIXABLE vulnerabilities:")
    lines.append("   a. Update the version in requirements.txt")
    lines.append("   b. Update the version in manifest.json (if present)")
    lines.append("   c. Run pip-audit again to verify the fix")
    lines.append("")
    lines.append("2. For UNFIXABLE vulnerabilities:")
    lines.append("   a. Assess if the vulnerable functionality is used in this project")
    lines.append("   b. If not used or risk is acceptable, add to IGNORED_VULNERABILITIES")
    lines.append("   c. If used, consider alternative packages or code mitigations")
    lines.append("")
    lines.append("3. Commit with message: 'security: fix pip-audit findings [package-name]'")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pytest tests
# ---------------------------------------------------------------------------


class TestPipAuditSecurity:
    """Dependency vulnerability tests using pip-audit."""

    def test_no_known_vulnerabilities(self) -> None:
        """Ensure no known vulnerabilities exist in dependencies.

        This test runs pip-audit on requirements.txt and fails if any
        vulnerabilities are found (unless explicitly ignored).
        It provides detailed, AI-friendly remediation instructions.
        """
        repo_root = Path(__file__).parent.parent

        all_issues: list[DependencyIssue] = []

        for req_file in REQUIREMENTS_FILES:
            req_path = repo_root / req_file
            if not req_path.exists():
                print(f"⚠️  Skipping {req_file} (not found)")
                continue

            print(f"\n📦 Auditing {req_file}...")
            audit_output = run_pip_audit(str(req_path))
            issues = parse_issues(audit_output)
            all_issues.extend(issues)

        # Generate report
        report = generate_ai_report(all_issues, IGNORED_VULNERABILITIES)

        # Filter active (non-ignored) issues
        active_issues = []
        for issue in all_issues:
            active_vulns = [v for v in issue.vulns if v.vuln_id not in IGNORED_VULNERABILITIES]
            if active_vulns:
                active_issues.append(issue)

        # Always print the report
        print("\n" + report)

        # Fail if active vulnerabilities found
        if active_issues:
            total_vulns = sum(len(i.vulns) for i in active_issues)
            pytest.fail(
                f"\n\n{report}\n\n"
                f"❌ SECURITY CHECK FAILED: Found {total_vulns} vulnerabilities "
                f"in {len(active_issues)} packages.\n"
                f"See report above for AI-friendly remediation instructions."
            )

    def test_pip_audit_available(self) -> None:
        """Verify pip-audit is installed and accessible."""
        result = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--version"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "pip-audit is not installed. Install with: pip install pip-audit"
        )
        print(f"\n✅ pip-audit version: {result.stdout.strip()}")


# ---------------------------------------------------------------------------
# Standalone execution
# ---------------------------------------------------------------------------


def main() -> int:
    """Run pip-audit analysis standalone (without pytest).

    Returns:
        0 if no vulnerabilities, 1 if vulnerabilities found.
    """
    print("🔐 Running pip-audit Dependency Vulnerability Scan (standalone mode)...")
    print()

    repo_root = Path(__file__).parent.parent

    all_issues: list[DependencyIssue] = []

    for req_file in REQUIREMENTS_FILES:
        req_path = repo_root / req_file
        if not req_path.exists():
            print(f"⚠️  Skipping {req_file} (not found)")
            continue

        print(f"📦 Auditing {req_file}...")
        try:
            audit_output = run_pip_audit(str(req_path))
        except RuntimeError as e:
            print(f"❌ Error: {e}")
            return 1

        issues = parse_issues(audit_output)
        all_issues.extend(issues)

    report = generate_ai_report(all_issues, IGNORED_VULNERABILITIES)
    print(report)

    # Filter active (non-ignored) issues
    active_issues = []
    for issue in all_issues:
        active_vulns = [v for v in issue.vulns if v.vuln_id not in IGNORED_VULNERABILITIES]
        if active_vulns:
            active_issues.append(issue)

    if active_issues:
        total_vulns = sum(len(i.vulns) for i in active_issues)
        print()
        print(f"❌ SECURITY CHECK FAILED: Found {total_vulns} vulnerabilities.")
        print("   Fix these issues and run again.")
        return 1

    print()
    print("✅ SECURITY CHECK PASSED: No active vulnerabilities found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
