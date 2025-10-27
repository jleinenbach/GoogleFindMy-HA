#!/usr/bin/env python3
# script/list_typing_helpers.py
"""Enumerate typing helper modules grouped by package.

The script searches for helper modules whose filenames match the
``_typing.py`` pattern and prints them grouped by the containing Python
package. Use the ``--root`` flag to override the search root and
``--pattern`` to look for alternative filename patterns (for example,
``typing_helpers.py``).
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from typing import Iterable

DEFAULT_PATTERN = "_typing.py"


def _iter_typing_helpers(root: Path, pattern: str) -> Iterable[Path]:
    """Yield typing helper modules under ``root`` matching ``pattern``."""

    yield from root.rglob(pattern)


def _package_name(path: Path, repo_root: Path) -> str:
    """Return the dotted package name for ``path`` relative to ``repo_root``."""

    package_parts: list[str] = []
    current = path.parent
    while True:
        if not current.exists():
            break
        if not current.is_relative_to(repo_root):
            break
        init_file = current / "__init__.py"
        if not init_file.is_file():
            break
        package_parts.append(current.name)
        if current == repo_root:
            break
        current = current.parent
    if not package_parts:
        return "(non-package)"
    return ".".join(reversed(package_parts))


def _collect_helpers(root: Path, pattern: str) -> dict[str, list[Path]]:
    """Group typing helper modules by their package name."""

    grouped: dict[str, list[Path]] = defaultdict(list)
    for helper in _iter_typing_helpers(root, pattern):
        if not helper.is_file():
            continue
        if not helper.is_relative_to(root):
            continue
        package = _package_name(helper, root)
        grouped[package].append(helper)
    return grouped


def _print_helpers(grouped: dict[str, list[Path]], root: Path) -> None:
    """Print grouped typing helpers using repository-relative paths."""

    for package in sorted(grouped):
        helpers = sorted(grouped[package])
        if not helpers:
            continue
        print(package)
        for helper in helpers:
            relative = helper.relative_to(root)
            print(f"  - {relative}")


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the script."""

    parser = argparse.ArgumentParser(
        description=(
            "Enumerate typing helper modules (default: files named _typing.py)\n"
            "grouped by their containing Python package."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository root to search (default: current working directory).",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help="Filename pattern to match (default: _typing.py).",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    """Entry point for enumerating typing helper modules."""

    args = _parse_args(argv)
    root = args.root.resolve()
    pattern = args.pattern
    grouped = _collect_helpers(root, pattern)
    if not grouped:
        print("No typing helper modules found.")
        return 0
    _print_helpers(grouped, root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
