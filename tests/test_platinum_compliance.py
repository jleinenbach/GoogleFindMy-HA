# tests/test_platinum_compliance.py
"""Automated compliance checks for the Platinum quality level."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from custom_components.googlefindmy.const import INTEGRATION_VERSION


def test_manifest_declares_expected_keys(manifest: dict[str, object]) -> None:
    """Manifest must advertise the required metadata for a Platinum integration."""

    required_keys = {
        "domain",
        "name",
        "version",
        "after_dependencies",
        "codeowners",
        "config_flow",
        "dependencies",
        "documentation",
        "integration_type",
        "iot_class",
        "quality_scale",
        "issue_tracker",
        "loggers",
        "requirements",
    }
    assert required_keys.issubset(manifest.keys())
    assert manifest["domain"] == "googlefindmy"
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "hub"
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["version"] == INTEGRATION_VERSION
    assert manifest["quality_scale"] == "platinum"
    assert "recorder" in manifest.get("after_dependencies", [])
    assert "http" in manifest.get("dependencies", [])
    assert "custom_components.googlefindmy" in manifest.get("loggers", [])
    assert "discovery" not in manifest
    assert "discovery_update_info" not in manifest


@pytest.fixture(name="quality_scale_text")
def fixture_quality_scale_text(integration_root: Path) -> str:
    """Return the raw text of the quality_scale.yaml file."""

    return (integration_root / "quality_scale.yaml").read_text(encoding="utf-8")


def test_quality_scale_declares_platinum_status(quality_scale_text: str) -> None:
    """The quality scale document must pin the platinum tier with completed rules.

    Exception: ``test-coverage`` is held at ``todo`` while scoped integration
    coverage climbs toward the 95 % Platinum floor (sprint baseline 60 %,
    measured 64 %). See ``test_coverage_threshold_enforced`` for the wired
    gate. Every other rule must remain ``done``.
    """

    assert "tier: platinum" in quality_scale_text
    statuses = re.findall(r"status:\s+(\w+)", quality_scale_text)
    assert statuses, "quality_scale.yaml should declare rule statuses"
    assert set(statuses) <= {"done", "todo"}, (
        "Only 'done' and 'todo' are allowed as rule statuses"
    )
    todo_count = statuses.count("todo")
    assert todo_count == 1, (
        "Exactly one rule ('test-coverage') is expected at 'todo' during the "
        f"coverage sprint; found {todo_count}"
    )
    # Anchor: the single 'todo' must be 'test-coverage'.
    test_coverage_block = re.search(
        r"- id: test-coverage\b[\s\S]*?status:\s+(\w+)", quality_scale_text
    )
    assert test_coverage_block is not None, (
        "quality_scale.yaml must declare a 'test-coverage' rule"
    )
    assert test_coverage_block.group(1) == "todo", (
        "test-coverage must be at 'todo' until scoped coverage reaches 95 %"
    )
    assert "id: runtime-data" in quality_scale_text
    assert "id: repair-issues" in quality_scale_text


def test_runtime_data_and_coordinator_usage(integration_root: Path) -> None:
    """__init__.py must wire RuntimeData and GoogleFindMyCoordinator correctly."""

    init_text = (integration_root / "__init__.py").read_text(encoding="utf-8")
    assert "class RuntimeData" in init_text
    assert "from .coordinator import GoogleFindMyCoordinator" in init_text
    assert "entry.runtime_data" in init_text
    assert "RuntimeData(" in init_text


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a shell command and capture its output for assertions."""

    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_mypy_strict_compliance(integration_root: Path) -> None:
    """The integration must pass strict static type checking."""

    repo_root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "-m", "mypy", "custom_components/googlefindmy"]

    result = run_command(cmd, cwd=repo_root)

    error_msg = (
        "Mypy strict type checking failed.\n"
        f"Exit Code: {result.returncode}\n"
        f"Output:\n{result.stdout}\n"
        f"Errors:\n{result.stderr}"
    )

    assert result.returncode == 0, error_msg


def test_ruff_linting_compliance() -> None:
    """The integration must pass ruff linting checks."""

    repo_root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "-m", "ruff", "check", "."]

    result = run_command(cmd, cwd=repo_root)

    error_msg = (
        "Ruff linting failed.\n"
        "Run 'ruff check . --fix' to automatically fix some of these issues.\n"
        f"Output:\n{result.stdout}\n"
        f"Errors:\n{result.stderr}"
    )

    assert result.returncode == 0, error_msg


def test_quality_scale_evidence_existence(integration_root: Path) -> None:
    """Verify that evidence paths in quality_scale.yaml point to real files."""

    repo_root = Path(__file__).resolve().parent.parent
    quality_yaml_path = integration_root / "quality_scale.yaml"

    if not quality_yaml_path.exists():
        pytest.skip("quality_scale.yaml not found")

    quality_data = yaml.safe_load(quality_yaml_path.read_text(encoding="utf-8"))

    rules = quality_data.get("rules", {}) if isinstance(quality_data, dict) else {}
    missing_files: list[str] = []

    for tier_rules in rules.values():
        if not isinstance(tier_rules, list):
            continue
        for rule in tier_rules:
            evidence_entries = (
                rule.get("evidence", []) if isinstance(rule, dict) else []
            )
            for evidence in evidence_entries:
                if not isinstance(evidence, str):
                    continue
                file_reference = evidence.split(":", 1)[0].split("#", 1)[0]
                full_path = repo_root / file_reference
                if not full_path.exists():
                    missing_files.append(file_reference)

    assert not missing_files, (
        "The following evidence files listed in quality_scale.yaml do not exist:\n"
        + "\n".join(sorted(set(missing_files)))
    )


# Sprint baseline for the test-coverage quality-scale rule.
# Real coverage on scoped integration source was 64 % (CI run on b9ca59cd).
# The Platinum target remains >= 95 %; this floor only prevents regression
# while incremental test expansion lands. Raise as the sprint phase-gates
# advance (60 -> 65 -> 75 -> 85 -> 90 -> 95).
COVERAGE_SPRINT_FLOOR = 60
COVERAGE_PLATINUM_TARGET = 95


def test_coverage_threshold_enforced() -> None:
    """Sprint floor for quality-scale rule ``test-coverage`` must be wired.

    Two orthogonal gates must declare a numeric coverage floor of at least
    ``COVERAGE_SPRINT_FLOOR``:

    * ``pyproject.toml`` ``[tool.coverage.report] fail_under`` (engine side)
    * ``.github/workflows/ci.yml`` ``--cov-fail-under`` (CI side)

    Both must NOT exceed ``COVERAGE_PLATINUM_TARGET`` either, so a future
    drift cannot silently re-introduce a 95 % gate above the actual baseline
    and break CI on an unrelated PR. Empirical enforcement happens at CI
    runtime via the pytest flag; this test only verifies the wiring.
    """

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11 path
        import tomli as tomllib  # type: ignore[no-redef]

    repo_root = Path(__file__).resolve().parent.parent
    pyproject_data = tomllib.loads(
        (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    ci_text = (repo_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    coverage_report = (
        pyproject_data.get("tool", {}).get("coverage", {}).get("report", {})
    )
    fail_under = coverage_report.get("fail_under")
    assert fail_under is not None, (
        "pyproject.toml [tool.coverage.report] is missing fail_under "
        "(required by quality-scale rule test-coverage)"
    )
    assert COVERAGE_SPRINT_FLOOR <= int(fail_under) <= COVERAGE_PLATINUM_TARGET, (
        f"pyproject.toml fail_under must be within "
        f"[{COVERAGE_SPRINT_FLOOR}, {COVERAGE_PLATINUM_TARGET}] "
        f"(current sprint floor; raise as test expansion lands)"
    )

    ci_match = re.search(r"--cov-fail-under=(\d+)", ci_text)
    assert ci_match, (
        ".github/workflows/ci.yml is missing --cov-fail-under flag in the "
        "pytest invocation (required as a CI-side gate)"
    )
    ci_floor = int(ci_match.group(1))
    assert COVERAGE_SPRINT_FLOOR <= ci_floor <= COVERAGE_PLATINUM_TARGET, (
        f"CI --cov-fail-under must be within "
        f"[{COVERAGE_SPRINT_FLOOR}, {COVERAGE_PLATINUM_TARGET}]"
    )
    assert ci_floor == int(fail_under), (
        "pyproject.toml fail_under and CI --cov-fail-under must agree "
        "(both gates must enforce the same sprint floor)"
    )
