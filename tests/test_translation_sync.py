# tests/test_translation_sync.py
"""Tests to ensure all translation files are synchronized and complete."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

TRANSLATIONS_DIR = Path("custom_components/googlefindmy/translations")
REFERENCE_LANG = "en"  # English is the reference translation


def _flatten_keys(data: dict[str, Any], prefix: str = "") -> set[str]:
    """Recursively flatten a nested dict into a set of dot-separated key paths."""
    keys: set[str] = set()
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys.update(_flatten_keys(value, full_key))
        else:
            keys.add(full_key)
    return keys


def _get_translation_files() -> list[Path]:
    """Return all translation JSON files in the translations directory."""
    return sorted(TRANSLATIONS_DIR.glob("*.json"))


def _load_translation(path: Path) -> dict[str, Any]:
    """Load and parse a translation JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_translations_have_same_structure_as_reference() -> None:
    """Ensure all translation files have the same key structure as the reference (en.json).

    This test:
    1. Loads the English translation as the reference
    2. Compares all other translations against the reference
    3. Reports missing keys and extra keys for each translation
    """
    reference_path = TRANSLATIONS_DIR / f"{REFERENCE_LANG}.json"
    assert reference_path.exists(), f"Reference translation {reference_path} not found"

    reference_data = _load_translation(reference_path)
    reference_keys = _flatten_keys(reference_data)

    translation_files = _get_translation_files()
    assert len(translation_files) >= 2, "Expected at least 2 translation files"

    errors: list[str] = []

    for translation_path in translation_files:
        lang = translation_path.stem
        if lang == REFERENCE_LANG:
            continue  # Skip reference language

        translation_data = _load_translation(translation_path)
        translation_keys = _flatten_keys(translation_data)

        missing_keys = reference_keys - translation_keys
        extra_keys = translation_keys - reference_keys

        if missing_keys:
            # Limit output for readability
            missing_sample = sorted(missing_keys)[:10]
            suffix = (
                f" (and {len(missing_keys) - 10} more)"
                if len(missing_keys) > 10
                else ""
            )
            errors.append(
                f"{lang}.json is MISSING {len(missing_keys)} keys: {missing_sample}{suffix}"
            )

        if extra_keys:
            extra_sample = sorted(extra_keys)[:10]
            suffix = (
                f" (and {len(extra_keys) - 10} more)" if len(extra_keys) > 10 else ""
            )
            errors.append(
                f"{lang}.json has {len(extra_keys)} EXTRA keys: {extra_sample}{suffix}"
            )

    assert not errors, "Translation synchronization errors:\n" + "\n".join(errors)


def test_reference_translation_exists() -> None:
    """Ensure the reference English translation file exists."""
    reference_path = TRANSLATIONS_DIR / f"{REFERENCE_LANG}.json"
    assert reference_path.exists(), f"Reference translation {reference_path} not found"


def test_all_expected_languages_present() -> None:
    """Ensure all expected language translations are present."""
    expected_languages = {"en", "de", "fr", "es", "it", "pl", "pt", "pt-BR"}
    translation_files = _get_translation_files()
    actual_languages = {f.stem for f in translation_files}

    missing_languages = expected_languages - actual_languages
    assert not missing_languages, (
        f"Missing translation files: {sorted(missing_languages)}"
    )


def test_translation_files_are_valid_json() -> None:
    """Ensure all translation files are valid JSON."""
    for translation_path in _get_translation_files():
        try:
            _load_translation(translation_path)
        except json.JSONDecodeError as e:
            pytest.fail(f"{translation_path.name} is invalid JSON: {e}")


def test_translation_title_key_not_at_root() -> None:
    """Ensure the deprecated root-level 'title' is NOT present in translation files.

    Home Assistant now auto-generates the title from manifest.json, so having
    a root-level 'title' key would be redundant and triggers hassfest warnings.
    """
    for translation_path in _get_translation_files():
        data = _load_translation(translation_path)
        assert "title" not in data, (
            f"{translation_path.name} has deprecated root-level 'title' key"
        )


@pytest.mark.parametrize("translation_file", _get_translation_files())
def test_no_empty_string_values(translation_file: Path) -> None:
    """Ensure no translation values are empty strings."""

    def find_empty_values(data: dict[str, Any], path: str = "") -> list[str]:
        """Find all keys with empty string values."""
        empty_paths: list[str] = []
        for key, value in data.items():
            full_path = f"{path}.{key}" if path else key
            if isinstance(value, dict):
                empty_paths.extend(find_empty_values(value, full_path))
            elif value == "":
                empty_paths.append(full_path)
        return empty_paths

    data = _load_translation(translation_file)
    empty_values = find_empty_values(data)

    # Allow certain keys to be intentionally empty (like abort/error sections)
    # Filter out known empty sections
    critical_empty = [
        p
        for p in empty_values
        if not any(
            p.endswith(suffix) for suffix in (".abort", ".error", "abort", "error")
        )
    ]

    assert not critical_empty, (
        f"{translation_file.name} has empty string values at: {critical_empty[:10]}"
    )
