#!/usr/bin/env python3
# script/clean_pycache.py
"""Remove Python bytecode cache artifacts from the repository."""

from __future__ import annotations

import argparse
import shutil
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def clean_pycache(*, dry_run: bool = False, verbose: bool = False) -> tuple[int, int]:
    """Delete ``__pycache__`` directories and ``.pyc`` files.

    Args:
        dry_run: When ``True`` the files are not deleted.
        verbose: When ``True`` each path scheduled for removal is printed.

    Returns:
        A tuple containing the number of ``__pycache__`` directories and ``.pyc``
        files that were removed or would be removed.
    """

    directory_paths = list(PROJECT_ROOT.rglob("__pycache__"))
    file_paths = list(PROJECT_ROOT.rglob("*.pyc"))

    if verbose:
        action = "Would remove" if dry_run else "Removing"
        for directory in directory_paths:
            print(f"{action} directory: {directory.relative_to(PROJECT_ROOT)}")
        for file_path in file_paths:
            print(f"{action} file: {file_path.relative_to(PROJECT_ROOT)}")

    if not dry_run:
        for directory in directory_paths:
            shutil.rmtree(directory, ignore_errors=True)
        for file_path in file_paths:
            try:
                file_path.unlink()
            except FileNotFoundError:
                continue

    return len(directory_paths), len(file_paths)


def format_summary(
    directory_count: int, file_count: int, *, dry_run: bool = False
) -> str:
    """Return a human-friendly summary string for the cleanup."""

    action = "Would remove" if dry_run else "Removed"
    directory_label = "directory" if directory_count == 1 else "directories"
    file_label = "file" if file_count == 1 else "files"
    return (
        f"{action} {directory_count} __pycache__ {directory_label} and "
        f"{file_count} .pyc {file_label}."
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for CLI usage."""

    parser = argparse.ArgumentParser(
        description="Remove Python bytecode cache artifacts from the repository.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List artifacts without deleting them.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Log each path that will be removed.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all output except errors.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.verbose and args.quiet:
        parser.error("--verbose and --quiet are mutually exclusive")

    if not args.quiet:
        intro = "Would remove" if args.dry_run else "Removing"
        print(f"{intro} Python cache artifacts (use --verbose for details)...")

    directory_count, file_count = clean_pycache(
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    if not args.quiet:
        print(format_summary(directory_count, file_count, dry_run=args.dry_run))
        if args.dry_run:
            print("Dry run complete; no files were deleted.")
        else:
            print("Cache artifacts removed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
