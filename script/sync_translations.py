#!/usr/bin/env python3
# script/sync_translations.py
"""Synchronize translation JSON files with the canonical ``strings.json`` tree.

If CLI entry points or helper wrappers are missing, follow the module
invocation fallbacks documented in ``AGENTS.md`` (for example, invoke this
script via ``python -m script.sync_translations``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
STRINGS_PATH = ROOT / "custom_components" / "googlefindmy" / "strings.json"
TRANSLATIONS_DIR = ROOT / "custom_components" / "googlefindmy" / "translations"
BASE_LANGUAGE = "en"


def _load_json(path: Path) -> Any:
    """Load JSON data from ``path`` and return the parsed structure."""
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def _dump_json(path: Path, data: Any) -> None:
    """Write ``data`` to ``path`` using UTF-8 JSON with deterministic layout."""
    serialized = json.dumps(data, indent=2, ensure_ascii=False)
    path.write_text(f"{serialized}\n", encoding="utf-8")


def _merge_structure(base: Any, current: Any) -> Any:
    """Merge ``current`` translation data onto the ``base`` structure."""
    if isinstance(base, Mapping):
        merged: dict[str, Any] = {}
        current_mapping = current if isinstance(current, Mapping) else {}
        for key, value in base.items():
            merged[key] = _merge_structure(value, current_mapping.get(key))
        return merged

    if isinstance(base, Sequence) and not isinstance(base, (str, bytes, bytearray)):
        current_sequence = current if isinstance(current, Sequence) else []
        result: list[Any] = []
        for index, value in enumerate(base):
            candidate = None
            if (
                isinstance(current_sequence, Sequence)
                and not isinstance(current_sequence, (str, bytes, bytearray))
                and index < len(current_sequence)
            ):
                candidate = current_sequence[index]
            result.append(_merge_structure(value, candidate))
        return result

    if isinstance(current, str) and current:
        return current

    return base


def _find_extra_keys(
    base: Any, candidate: Any, path: tuple[str, ...] = tuple()
) -> list[str]:
    """Return a list of JSON pointer-like paths present only in ``candidate``."""
    if isinstance(candidate, Mapping):
        if not isinstance(base, Mapping):
            return ["/".join(path) if path else "<root>"]
        extras: list[str] = []
        for key, value in candidate.items():
            next_path = path + (str(key),)
            if key not in base:
                extras.append("/".join(next_path))
                continue
            extras.extend(_find_extra_keys(base[key], value, next_path))
        return extras

    if isinstance(candidate, Sequence) and not isinstance(
        candidate, (str, bytes, bytearray)
    ):
        if not (
            isinstance(base, Sequence) and not isinstance(base, (str, bytes, bytearray))
        ):
            return ["/".join(path) if path else "<root>"]
        sequence_extras: list[str] = []
        limit = min(len(candidate), len(base))
        for index in range(limit):
            sequence_extras.extend(
                _find_extra_keys(
                    base[index],
                    candidate[index],
                    path + (str(index),),
                )
            )
        if len(candidate) > len(base):
            sequence_extras.extend(
                "/".join(path + (str(index),))
                for index in range(len(base), len(candidate))
            )
        return sequence_extras

    return []


def sync_translations(*, check: bool) -> int:
    """Synchronize translation files. Return ``0`` on success, ``1`` on drift."""
    if not STRINGS_PATH.is_file():
        print(f"Strings file missing: {STRINGS_PATH}", file=sys.stderr)
        return 1

    if not TRANSLATIONS_DIR.is_dir():
        print(f"Translations directory missing: {TRANSLATIONS_DIR}", file=sys.stderr)
        return 1

    strings_data = _load_json(STRINGS_PATH)

    translation_files = sorted(TRANSLATIONS_DIR.glob("*.json"))
    if not translation_files:
        print("No translation files found. Nothing to synchronize.")
        return 0

    drift_detected = False

    for translation_file in translation_files:
        language = translation_file.stem
        if language == BASE_LANGUAGE:
            if check:
                current_data = _load_json(translation_file)
                if current_data != strings_data:
                    drift_detected = True
                    print(
                        f"{translation_file}: differs from strings.json (base language)",
                        file=sys.stderr,
                    )
            else:
                _dump_json(translation_file, strings_data)
            continue

        current_data = _load_json(translation_file)
        merged = _merge_structure(strings_data, current_data)
        extras = _find_extra_keys(strings_data, current_data)
        if check:
            if current_data != merged or extras:
                drift_detected = True
                if extras:
                    print(
                        f"{translation_file}: extra keys -> {', '.join(extras)}",
                        file=sys.stderr,
                    )
                if current_data != merged:
                    print(
                        f"{translation_file}: missing keys or placeholders need sync",
                        file=sys.stderr,
                    )
            continue

        if extras:
            print(
                f"{translation_file}: dropping extra keys -> {', '.join(extras)}",
                file=sys.stderr,
            )
        _dump_json(translation_file, merged)

    return 1 if drift_detected else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize translation files with"
            " custom_components/googlefindmy/strings.json. If the CLI"
            " entry point is missing, use the module invocation fallbacks"
            " described in AGENTS.md (for example, python -m"
            " script.sync_translations)."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only check for drift without updating files.",
    )
    args = parser.parse_args(argv)
    return sync_translations(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
