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
    """The quality scale document must pin the platinum tier with completed rules."""

    assert "tier: platinum" in quality_scale_text
    statuses = re.findall(r"status:\s+(\w+)", quality_scale_text)
    assert statuses, "quality_scale.yaml should declare rule statuses"
    assert set(statuses) == {"done"}
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
            evidence_entries = rule.get("evidence", []) if isinstance(rule, dict) else []
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
