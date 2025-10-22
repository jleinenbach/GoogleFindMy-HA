# tests/test_translation_placeholders.py
"""Translation placeholder consistency tests."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

BASE_LANGUAGE = "en"
OUTDATED_PLACEHOLDERS: dict[tuple[str, ...], set[str]] = {}

_PLACEHOLDER_PATTERN = re.compile(r"{([a-zA-Z0-9_]+)}")


def _collect_placeholders(
    value: Any, path: tuple[str, ...], target: dict[tuple[str, ...], set[str]]
) -> None:
    """Recursively gather placeholders from nested translation structures."""
    if isinstance(value, str):
        target.setdefault(path, set()).update(_PLACEHOLDER_PATTERN.findall(value))
        return

    target.setdefault(path, set())

    if isinstance(value, Mapping):
        for key, child in value.items():
            _collect_placeholders(child, path + (str(key),), target)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _collect_placeholders(child, path + (str(index),), target)


def _extract_placeholders(data: Any) -> dict[tuple[str, ...], set[str]]:
    placeholders: dict[tuple[str, ...], set[str]] = {}
    _collect_placeholders(data, tuple(), placeholders)
    return placeholders


def test_translation_placeholders() -> None:
    translations_dir = (
        Path(__file__).resolve().parent.parent
        / "custom_components"
        / "googlefindmy"
        / "translations"
    )
    assert translations_dir.is_dir(), (
        f"Translations directory not found: {translations_dir}"
    )

    language_placeholders: dict[str, dict[tuple[str, ...], set[str]]] = {}

    language_data: dict[str, Any] = {}

    for json_file in sorted(translations_dir.glob("*.json")):
        with json_file.open("r", encoding="utf-8") as file:
            data = json.load(file)
        language_placeholders[json_file.stem] = _extract_placeholders(data)
        language_data[json_file.stem] = data

    assert BASE_LANGUAGE in language_placeholders, (
        f"Base language '{BASE_LANGUAGE}' translation is missing."
    )

    all_paths: set[tuple[str, ...]] = set()
    for placeholders in language_placeholders.values():
        all_paths.update(placeholders.keys())

    base_placeholders = language_placeholders[BASE_LANGUAGE]

    base_data = language_data[BASE_LANGUAGE]

    def _collect_string_paths(
        value: Any, path: tuple[str, ...], out: set[tuple[str, ...]]
    ) -> None:
        if isinstance(value, str):
            out.add(path)
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                _collect_string_paths(child, path + (str(key),), out)
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            for index, child in enumerate(value):
                _collect_string_paths(child, path + (str(index),), out)

    base_string_paths: set[tuple[str, ...]] = set()
    _collect_string_paths(base_data, tuple(), base_string_paths)

    def _resolve_path(container: Any, path: tuple[str, ...]) -> Any:
        current = container
        for element in path:
            if isinstance(current, Mapping):
                current = current[element]
            elif isinstance(current, Sequence) and not isinstance(
                current, (str, bytes, bytearray)
            ):
                current = current[int(element)]
            else:
                raise KeyError(path)
        return current

    for language, data in sorted(language_data.items()):
        missing_paths: list[str] = []
        for path in sorted(base_string_paths):
            try:
                value = _resolve_path(data, path)
            except (KeyError, IndexError, ValueError):
                missing_paths.append("/".join(path))
                continue
            assert isinstance(value, str), (
                f"{language} translation for {'/'.join(path)} should be a string, got {type(value)!r}"
            )
        assert not missing_paths, (
            f"{language} translation is missing strings for: {', '.join(missing_paths)}"
        )

    for path in sorted(all_paths):
        union_placeholders: set[str] = set()
        for placeholders in language_placeholders.values():
            union_placeholders.update(placeholders.get(path, set()))

        path_display = "/".join(path) if path else "<root>"
        outdated_for_path = OUTDATED_PLACEHOLDERS.get(path, set())

        for language, placeholders in sorted(language_placeholders.items()):
            placeholders_for_lang = placeholders.get(path, set())

            missing = union_placeholders - placeholders_for_lang - outdated_for_path
            assert not missing, (
                f"{language} translation for {path_display} is missing placeholders: {sorted(missing)}"
            )

            placeholders_for_base = base_placeholders.get(path, set())
            extra = placeholders_for_lang - placeholders_for_base - outdated_for_path
            if language == BASE_LANGUAGE:
                assert not extra, (
                    f"Base language {BASE_LANGUAGE} has placeholders not in outdated list at {path_display}: {sorted(extra)}"
                )
            else:
                assert not extra, (
                    f"{language} translation for {path_display} has unexpected placeholders: {sorted(extra)}"
                )
