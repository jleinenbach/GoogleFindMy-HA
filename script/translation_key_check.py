"""List missing translation keys relative to the base strings.json file.

Run this helper before the full test suite to catch localization drift early.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> Any:
    """Load JSON content from ``path`` using UTF-8 encoding."""

    return json.loads(path.read_text(encoding="utf-8"))


def _flatten_keys(value: Any, prefix: Sequence[str] = ()) -> set[tuple[str, ...]]:
    """Return all dictionary key paths as tuples.

    Only dictionary keys participate in the output. Lists are traversed to
    discover nested dictionaries but do not contribute their indices to the
    recorded paths so translated arrays remain key-agnostic.
    """

    keys: set[tuple[str, ...]] = set()
    if isinstance(value, Mapping):
        for key, nested in value.items():
            path = (*prefix, str(key))
            keys.add(path)
            keys.update(_flatten_keys(nested, path))
    elif isinstance(value, list):
        for item in value:
            keys.update(_flatten_keys(item, prefix))
    return keys


def _format_path(path: Iterable[str]) -> str:
    """Render a dotted path from a key tuple."""

    return ".".join(path)


def _discover_translation_files(base_dir: Path) -> list[Path]:
    """Return the base strings file and every locale translation file."""

    translations_dir = base_dir / "translations"
    return [base_dir / "strings.json", *sorted(translations_dir.glob("*.json"))]


def main() -> int:
    """Entry point for translation key coverage checks."""

    parser = argparse.ArgumentParser(
        description=(
            "List translation keys missing from locale files relative to"
            " custom_components/googlefindmy/strings.json."
        )
    )
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=Path("custom_components/googlefindmy"),
        help=(
            "Path containing strings.json and a translations/ directory;"
            " defaults to custom_components/googlefindmy."
        ),
    )
    args = parser.parse_args()

    base_dir = args.base_dir
    base_strings = base_dir / "strings.json"
    if not base_strings.exists():
        print(f"Base strings file not found: {base_strings}")
        return 1

    translation_files = _discover_translation_files(base_dir)
    if not translation_files:
        print(f"No translation files found under {base_dir}")
        return 1

    base_keys = _flatten_keys(_load_json(base_strings))
    has_missing = False
    print("Translation key coverage relative to", base_strings)

    for path in translation_files[1:]:
        locale_keys = _flatten_keys(_load_json(path))
        missing = sorted(base_keys - locale_keys)
        if not missing:
            print(f"- {path}: OK")
            continue

        has_missing = True
        print(f"- {path}: missing {len(missing)} key(s)")
        for key_path in missing:
            print(f"    • {_format_path(key_path)}")

    if not has_missing:
        return 0

    print("\nMissing translation keys detected. Add the paths above to the locale files.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
