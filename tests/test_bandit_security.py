"""tests/test_bandit_security.py: Bandit security analysis as a pytest.

This test runs Bandit security analysis on the codebase and provides
detailed, actionable feedback for AI agents to fix critical security issues.

Usage:
    pytest tests/test_bandit_security.py -v --no-header -p no:conftest

    Or standalone:
    python tests/test_bandit_security.py

The test will:
1. Run Bandit on custom_components/googlefindmy
2. FAIL on High/Medium severity issues with detailed fix instructions
3. WARN on Low severity issues (informational)
4. Provide AI-friendly remediation guidance for each finding
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCAN_PATHS = ["custom_components/googlefindmy"]
EXCLUDE_PATHS = [".venv", "tests", "__pycache__"]

# Severity levels that cause test failure
FAIL_ON_SEVERITY = {"HIGH", "MEDIUM"}

# CWE to remediation mapping with AI-friendly instructions
REMEDIATION_GUIDE: dict[str, dict[str, str]] = {
    "B311": {
        "title": "Insecure Random Number Generator",
        "risk": "Using random module for security-sensitive operations (IDs, tokens, keys)",
        "fix": """
## AI ACTION REQUIRED: Replace insecure random with secrets module

### Step 1: Update import
```python
# REMOVE: import random
# ADD: import secrets
```

### Step 2: Replace random calls
```python
# BEFORE (insecure):
value = random.randint(min_val, max_val)

# AFTER (secure):
value = secrets.randbelow(max_val - min_val + 1) + min_val

# For random bytes:
# BEFORE: random.getrandbits(64)
# AFTER: secrets.randbits(64)

# For random choice:
# BEFORE: random.choice(items)
# AFTER: secrets.choice(items)
```

### Exception: Non-security uses (e.g., backoff jitter)
If the random value is NOT used for security (e.g., timing jitter), add nosec:
```python
delay = backoff + random.uniform(0.1, 0.3) * backoff  # nosec B311
```
""",
    },
    "B101": {
        "title": "Assert Used in Production Code",
        "risk": "Assert statements are removed in optimized bytecode (-O flag)",
        "fix": """
## AI ACTION REQUIRED: Replace assert with proper error handling

### For type narrowing (acceptable with nosec):
```python
# If assert is used for type narrowing for mypy, add nosec:
assert isinstance(value, str)  # nosec B101 - type narrowing for mypy
```

### For validation (replace with exception):
```python
# BEFORE:
assert user_input is not None

# AFTER:
if user_input is None:
    raise ValueError("user_input cannot be None")
```

### For internal invariants in retry loops:
```python
# If asserting after a loop that guarantees non-None, add nosec:
assert last_exc is not None  # nosec B101 - loop guarantees assignment
raise last_exc
```
""",
    },
    "B110": {
        "title": "Try-Except-Pass (Silent Exception Swallowing)",
        "risk": "Exceptions are silently ignored, potentially hiding bugs",
        "fix": """
## AI ACTION REQUIRED: Handle or document exception suppression

### For intentional best-effort operations:
```python
# Add nosec comment explaining WHY this is acceptable:
try:
    await cache.set(key, value)
except Exception:  # nosec B110 - best-effort cache write
    pass
```

### For operations that should log:
```python
# BEFORE:
try:
    do_something()
except Exception:
    pass

# AFTER:
try:
    do_something()
except Exception:
    _LOGGER.debug("Operation failed, continuing", exc_info=True)
```

### For cleanup operations:
```python
try:
    resource.close()
except Exception:  # nosec B110 - cleanup must not raise
    pass
```
""",
    },
    "B112": {
        "title": "Try-Except-Continue (Silent Loop Continuation)",
        "risk": "Loop continues silently on error, potentially processing bad data",
        "fix": """
## AI ACTION REQUIRED: Document or handle loop exception continuation

### For intentional skip-on-error patterns:
```python
for item in items:
    try:
        process(item)
    except Exception:  # nosec B112 - skip invalid items
        continue
```

### For operations that should log skipped items:
```python
for item in items:
    try:
        process(item)
    except Exception as err:
        _LOGGER.warning("Skipping item %s: %s", item, err)
        continue
```
""",
    },
    "B608": {
        "title": "SQL Injection Risk",
        "risk": "String formatting in SQL queries allows injection attacks",
        "fix": """
## AI ACTION REQUIRED: Use parameterized queries

### CRITICAL: Never use string formatting for SQL

```python
# BEFORE (VULNERABLE):
cursor.execute(f"SELECT * FROM users WHERE id = {user_id}")
cursor.execute("SELECT * FROM users WHERE name = '%s'" % name)

# AFTER (SAFE):
cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
cursor.execute("SELECT * FROM users WHERE name = %s", (name,))
```
""",
    },
    "B301": {
        "title": "Pickle Deserialization Risk",
        "risk": "Pickle can execute arbitrary code during deserialization",
        "fix": """
## AI ACTION REQUIRED: Replace pickle with safe alternatives

```python
# BEFORE (DANGEROUS):
data = pickle.loads(untrusted_data)

# AFTER (SAFE - use JSON for simple data):
data = json.loads(untrusted_data)

# If complex objects needed, use restricted unpickler or sign data
```
""",
    },
    "B324": {
        "title": "Insecure Hash Function",
        "risk": "MD5/SHA1 are cryptographically broken for security uses",
        "fix": """
## AI ACTION REQUIRED: Use SHA-256 or stronger

```python
# BEFORE:
hash_val = hashlib.md5(data).hexdigest()
hash_val = hashlib.sha1(data).hexdigest()

# AFTER:
hash_val = hashlib.sha256(data).hexdigest()

# For non-security uses (checksums), add nosec:
checksum = hashlib.md5(data).hexdigest()  # nosec B324 - non-crypto checksum
```
""",
    },
    "B602": {
        "title": "Subprocess Shell Injection",
        "risk": "shell=True with user input allows command injection",
        "fix": """
## AI ACTION REQUIRED: Avoid shell=True or sanitize input

```python
# BEFORE (VULNERABLE):
subprocess.run(f"echo {user_input}", shell=True)

# AFTER (SAFE - use list form):
subprocess.run(["echo", user_input], shell=False)

# If shell features needed, use shlex.quote:
import shlex
subprocess.run(f"echo {shlex.quote(user_input)}", shell=True)
```
""",
    },
    "B603": {
        "title": "Subprocess Without Shell Check",
        "risk": "Subprocess call may execute untrusted input",
        "fix": """
## AI ACTION REQUIRED: Validate subprocess arguments

```python
# If arguments are trusted/hardcoded, add nosec:
subprocess.Popen(["git", "status"])  # nosec B603 - hardcoded command

# If arguments come from config, validate them:
ALLOWED_COMMANDS = {"git", "npm", "pip"}
if cmd[0] not in ALLOWED_COMMANDS:
    raise ValueError(f"Command not allowed: {cmd[0]}")
subprocess.Popen(cmd)
```
""",
    },
    "B404": {
        "title": "Subprocess Module Import",
        "risk": "Subprocess module can be misused for command injection",
        "fix": """
## AI ACTION: Review subprocess usage

This is an informational warning. If subprocess is used safely:
```python
import subprocess  # nosec B404 - used with hardcoded commands only
```
""",
    },
    "B613": {
        "title": "Trojan Source (Bidirectional Characters)",
        "risk": "Unicode bidirectional characters can hide malicious code",
        "fix": """
## AI ACTION REQUIRED: Remove bidirectional control characters

### Step 1: Find the characters
The file contains hidden Unicode characters (U+202E, U+2066, etc.)

### Step 2: Remove them
Open the file in a hex editor or use:
```bash
sed -i 's/[\\u202a-\\u202e\\u2066-\\u2069]//g' filename.py
```

### Step 3: Review the visible code
After removal, verify the code logic matches what was visually displayed.
""",
    },
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class BanditIssue:
    """Represents a single Bandit finding."""

    test_id: str
    test_name: str
    severity: str
    confidence: str
    filename: str
    line_number: int
    line_range: list[int]
    code: str
    issue_text: str
    cwe: dict[str, Any]

    @property
    def location(self) -> str:
        """Return file:line format."""
        return f"{self.filename}:{self.line_number}"

    @property
    def cwe_id(self) -> str:
        """Return CWE ID if available."""
        return self.cwe.get("id", "Unknown") if self.cwe else "Unknown"


# ---------------------------------------------------------------------------
# Bandit runner
# ---------------------------------------------------------------------------


def run_bandit(paths: list[str], exclude: list[str]) -> dict[str, Any]:
    """Run Bandit and return JSON results."""
    cmd = [
        sys.executable,
        "-m",
        "bandit",
        "-r",
        *paths,
        "-f",
        "json",
        "-q",  # Quiet mode - suppress progress bar and info messages
        "--exclude",
        ",".join(exclude),
    ]

    result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603

    if result.returncode not in (0, 1):
        # Exit code 1 means issues found, which is expected
        # Other codes indicate actual errors
        if "No module named bandit" in result.stderr:
            pytest.skip("Bandit not installed. Run: pip install bandit")
        raise RuntimeError(f"Bandit failed: {result.stderr}")

    stdout = result.stdout.strip()
    if not stdout:
        return {"results": [], "metrics": {}}

    # Parse JSON from stdout (bandit outputs JSON to stdout, info to stderr)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        # If quiet mode didn't suppress all output, try to find JSON in output
        if "{" in stdout:
            json_start = stdout.index("{")
            return json.loads(stdout[json_start:])
        raise RuntimeError(
            f"Failed to parse Bandit output: {e}\nOutput: {stdout[:500]}"
        )


def parse_issues(bandit_output: dict[str, Any]) -> list[BanditIssue]:
    """Parse Bandit JSON output into BanditIssue objects."""
    issues = []
    for item in bandit_output.get("results", []):
        issues.append(
            BanditIssue(
                test_id=item.get("test_id", ""),
                test_name=item.get("test_name", ""),
                severity=item.get("issue_severity", "UNKNOWN"),
                confidence=item.get("issue_confidence", "UNKNOWN"),
                filename=item.get("filename", ""),
                line_number=item.get("line_number", 0),
                line_range=item.get("line_range", []),
                code=item.get("code", ""),
                issue_text=item.get("issue_text", ""),
                cwe=item.get("issue_cwe", {}),
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def generate_ai_report(issues: list[BanditIssue]) -> str:
    """Generate a detailed AI-friendly report with remediation instructions."""
    if not issues:
        return "✅ No security issues found."

    # Group by severity
    high = [i for i in issues if i.severity == "HIGH"]
    medium = [i for i in issues if i.severity == "MEDIUM"]
    low = [i for i in issues if i.severity == "LOW"]

    lines = []
    lines.append("=" * 80)
    lines.append("🔒 BANDIT SECURITY ANALYSIS REPORT")
    lines.append("=" * 80)
    lines.append("")
    lines.append(
        f"📊 Summary: {len(high)} HIGH | {len(medium)} MEDIUM | {len(low)} LOW"
    )
    lines.append("")

    if high or medium:
        lines.append("⚠️  CRITICAL: The following issues MUST be fixed before merge!")
        lines.append("")

    # Report critical issues first
    for severity_name, severity_issues in [("HIGH", high), ("MEDIUM", medium)]:
        if not severity_issues:
            continue

        lines.append("-" * 80)
        lines.append(
            f"🚨 {severity_name} SEVERITY ISSUES ({len(severity_issues)} found)"
        )
        lines.append("-" * 80)

        for issue in severity_issues:
            lines.append("")
            lines.append(f"### [{issue.test_id}] {issue.test_name}")
            lines.append(f"📍 Location: {issue.location}")
            lines.append(f"🔗 CWE: {issue.cwe_id}")
            lines.append(f"📝 Issue: {issue.issue_text}")
            lines.append("")
            lines.append("```python")
            lines.append(issue.code.rstrip())
            lines.append("```")

            # Add remediation guide
            if issue.test_id in REMEDIATION_GUIDE:
                guide = REMEDIATION_GUIDE[issue.test_id]
                lines.append("")
                lines.append(f"🔧 **{guide['title']}**")
                lines.append(f"⚡ Risk: {guide['risk']}")
                lines.append("")
                lines.append(guide["fix"])
            lines.append("")

    # Report low severity as informational
    if low:
        lines.append("-" * 80)
        lines.append(f"ℹ️  LOW SEVERITY ISSUES ({len(low)} found) - Informational")
        lines.append("-" * 80)
        lines.append("")
        lines.append(
            "These issues are informational and may not require immediate action."
        )
        lines.append(
            "Consider adding `# nosec BXXX` comments if the code is intentional."
        )
        lines.append("")

        # Group low issues by test_id for cleaner output
        low_by_type: dict[str, list[BanditIssue]] = {}
        for issue in low:
            low_by_type.setdefault(issue.test_id, []).append(issue)

        for test_id, type_issues in sorted(low_by_type.items()):
            lines.append(
                f"**[{test_id}] {type_issues[0].test_name}** ({len(type_issues)} occurrences)"
            )
            for issue in type_issues[:5]:  # Show first 5
                lines.append(f"  - {issue.location}")
            if len(type_issues) > 5:
                lines.append(f"  - ... and {len(type_issues) - 5} more")

            if test_id in REMEDIATION_GUIDE:
                lines.append(
                    f"  💡 Fix: See {test_id} remediation guide above or add `# nosec {test_id}`"
                )
            lines.append("")

    lines.append("=" * 80)
    lines.append("🤖 AI AGENT INSTRUCTIONS")
    lines.append("=" * 80)
    lines.append("")
    lines.append("1. Fix all HIGH and MEDIUM severity issues before proceeding")
    lines.append("2. For each issue:")
    lines.append("   a. Read the file at the specified location")
    lines.append("   b. Apply the remediation from the guide above")
    lines.append("   c. Run this test again to verify the fix")
    lines.append("3. For LOW severity issues that are intentional:")
    lines.append("   - Add `# nosec BXXX` comment with brief justification")
    lines.append("4. Commit with message: 'security: fix bandit findings [BXXX, BYYY]'")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pytest test
# ---------------------------------------------------------------------------


class TestBanditSecurity:
    """Security analysis tests using Bandit."""

    def test_no_critical_security_issues(self) -> None:
        """Ensure no HIGH or MEDIUM severity security issues exist.

        This test runs Bandit security analysis and fails if any
        HIGH or MEDIUM severity issues are found. It provides
        detailed, AI-friendly remediation instructions for each finding.
        """
        # Resolve paths relative to repo root
        repo_root = Path(__file__).parent.parent
        scan_paths = [str(repo_root / p) for p in SCAN_PATHS]
        exclude_paths = [str(repo_root / p) for p in EXCLUDE_PATHS]

        # Run Bandit
        bandit_output = run_bandit(scan_paths, exclude_paths)
        issues = parse_issues(bandit_output)

        # Generate report
        report = generate_ai_report(issues)

        # Filter critical issues
        critical_issues = [i for i in issues if i.severity in FAIL_ON_SEVERITY]

        # Always print the report for visibility
        print("\n" + report)

        # Fail if critical issues found
        if critical_issues:
            pytest.fail(
                f"\n\n{report}\n\n"
                f"❌ SECURITY CHECK FAILED: Found {len(critical_issues)} "
                f"HIGH/MEDIUM severity issues.\n"
                f"See report above for AI-friendly remediation instructions."
            )

    def test_bandit_available(self) -> None:
        """Verify Bandit is installed and accessible."""
        result = subprocess.run(
            [sys.executable, "-m", "bandit", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            "Bandit is not installed. Install with: pip install bandit"
        )
        print(f"\n✅ Bandit version: {result.stdout.strip()}")


# ---------------------------------------------------------------------------
# Standalone execution
# ---------------------------------------------------------------------------


def main() -> int:
    """Run Bandit analysis standalone (without pytest).

    Returns:
        0 if no critical issues, 1 if critical issues found.
    """
    print("🔒 Running Bandit Security Analysis (standalone mode)...")
    print()

    # Resolve paths relative to repo root
    repo_root = Path(__file__).parent.parent
    scan_paths = [str(repo_root / p) for p in SCAN_PATHS]
    exclude_paths = [str(repo_root / p) for p in EXCLUDE_PATHS]

    try:
        bandit_output = run_bandit(scan_paths, exclude_paths)
    except RuntimeError as e:
        print(f"❌ Error: {e}")
        return 1

    issues = parse_issues(bandit_output)
    report = generate_ai_report(issues)
    print(report)

    critical_issues = [i for i in issues if i.severity in FAIL_ON_SEVERITY]

    if critical_issues:
        print()
        print(
            f"❌ SECURITY CHECK FAILED: Found {len(critical_issues)} HIGH/MEDIUM severity issues."
        )
        print("   Fix these issues and run again.")
        return 1

    print()
    print("✅ SECURITY CHECK PASSED: No HIGH/MEDIUM severity issues found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
