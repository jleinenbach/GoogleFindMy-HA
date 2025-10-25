# tests/test_manifest_translation_schema.py
"""Regression tests for manifest and translation schema compatibility."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


MANIFEST_BANNED_KEYS = {"discovery", "discovery_update_info"}
TRANSLATION_BANNED_KEYS = {"discovery"}


def _find_banned_paths(
    data: object, banned_keys: set[str], *, path: tuple[str, ...] = ()
) -> list[tuple[str, ...]]:
    """Return every object path containing a banned key."""

    matches: list[tuple[str, ...]] = []

    if isinstance(data, dict):
        for key, value in data.items():
            next_path = (*path, key)
            if key in banned_keys:
                matches.append(next_path)
            matches.extend(_find_banned_paths(value, banned_keys, path=next_path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            matches.extend(
                _find_banned_paths(item, banned_keys, path=(*path, f"[{index}]"))
            )

    return matches


def _assert_no_banned_keys(
    data: object, banned_keys: set[str], *, filename: str
) -> None:
    """Assert that a JSON payload does not contain hassfest-banned keys anywhere."""

    matches = _find_banned_paths(data, banned_keys)
    assert not matches, f"{filename} contains hassfest-unsupported keys: " + ", ".join(
        " → ".join(path) for path in matches
    )


@pytest.mark.parametrize(
    "manifest_path",
    [
        Path("custom_components/googlefindmy/manifest.json"),
    ],
)
def test_manifest_has_no_custom_discovery_sections(manifest_path: Path) -> None:
    """Ensure hassfest-unsupported discovery keys are absent from the manifest."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _assert_no_banned_keys(manifest, MANIFEST_BANNED_KEYS, filename=str(manifest_path))


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
    _assert_no_banned_keys(
        payload, TRANSLATION_BANNED_KEYS, filename=str(translation_path)
    )
