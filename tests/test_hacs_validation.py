# tests/test_hacs_validation.py
# tests/test_hacs_validation.py
"""Validate HACS metadata alignment and guard against unsupported characters."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from custom_components.googlefindmy.const import INTEGRATION_VERSION


@pytest.fixture(name="hacs_metadata")
def fixture_hacs_metadata() -> dict[str, object]:
    """Load the hacs.json metadata file."""

    hacs_path = Path("hacs.json")
    metadata = json.loads(hacs_path.read_text(encoding="utf-8"))
    assert isinstance(metadata, dict)
    return metadata


def test_hacs_metadata_matches_manifest(
    hacs_metadata: dict[str, object],
    manifest: dict[str, object],
    integration_root: Path,
) -> None:
    """Ensure HACS metadata mirrors manifest declarations and const values."""

    allowed_keys = {
        "name",
        "content_in_root",
        "render_readme",
        "filename",
        "zip_release",
        "hide_default_branch",
    }
    assert set(hacs_metadata).issubset(allowed_keys)
    assert hacs_metadata["name"] == manifest["name"]

    const_text = (integration_root / "const.py").read_text(encoding="utf-8")
    match = re.search(r'INTEGRATION_VERSION: str = "([^"]+)"', const_text)
    assert match, "INTEGRATION_VERSION constant missing"
    assert manifest["version"] == INTEGRATION_VERSION == match.group(1)
    assert "homeassistant" not in manifest
    assert "homeassistant" not in hacs_metadata


def test_no_micro_sign_in_integration_files(
    integration_python_files: list[Path], integration_root: Path
) -> None:
    """Integration Python files must not contain the micro sign character."""

    offenders: list[str] = []
    for path in integration_python_files:
        text = path.read_text(encoding="utf-8")
        if "\u00b5" in text:
            offenders.append(str(path.relative_to(integration_root)))
    assert not offenders, f"micro sign detected in: {offenders}"
