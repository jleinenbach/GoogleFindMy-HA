# tests/test_manifest_translation_schema.py
"""Regression tests for manifest and translation schema compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "manifest_path",
    [
        Path("custom_components/googlefindmy/manifest.json"),
    ],
)
def test_manifest_has_no_custom_discovery_sections(manifest_path: Path) -> None:
    """Ensure hassfest-unsupported discovery keys are absent from the manifest."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "discovery" not in manifest
    assert "discovery_update_info" not in manifest


@pytest.mark.parametrize(
    "translation_path",
    [
        Path("custom_components/googlefindmy/strings.json"),
        *sorted(Path("custom_components/googlefindmy/translations").glob("*.json")),
    ],
)
def test_translations_have_no_discovery_section(translation_path: Path) -> None:
    """Ensure translation payloads omit hassfest-unsupported discovery sections."""

    payload = json.loads(translation_path.read_text(encoding="utf-8"))
    assert "discovery" not in payload
