#!/usr/bin/env python3
# script/precommit_hooks/ruff_format.py
"""Run Ruff format and report reformatted files."""

from __future__ import annotations

import subprocess
import sys
from typing import Sequence


def _split_args(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return Ruff options and filenames in the original argument order."""

    options: list[str] = []
    filenames: list[str] = []
    after_double_dash = False

    for arg in argv:
        if after_double_dash:
            filenames.append(arg)
            continue

        if arg == "--":
            after_double_dash = True
            continue

        if arg.startswith("-"):
            options.append(arg)
            continue

        filenames.append(arg)

    return options, filenames


def _maybe_force_exclude(options: list[str]) -> list[str]:
    """Ensure ``--force-exclude`` is passed unless explicitly disabled."""

    if "--no-force-exclude" in options:
        return options

    if "--force-exclude" in options:
        return options

    return [*options, "--force-exclude"]


def _run_ruff(options: Sequence[str], filenames: Sequence[str]) -> int:
    """Run ``python -m ruff format`` with the provided arguments."""

    command = [sys.executable, "-m", "ruff", "format", *options, *filenames]
    return subprocess.run(command, check=False).returncode


def _collect_changed_files(filenames: Sequence[str]) -> list[str]:
    if not filenames:
        return []

    diff_command = ["git", "diff", "--name-only", "--", *filenames]
    result = subprocess.run(diff_command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(result.returncode)

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def main(argv: Sequence[str]) -> int:
    if not argv:
        return 0

    options, filenames = _split_args(argv)
    options = _maybe_force_exclude(list(options))

    ruff_exit = _run_ruff(options, filenames)
    if ruff_exit not in (0, 1):
        return ruff_exit

    changed_files = _collect_changed_files(filenames)
    if changed_files:
        count = len(changed_files)
        noun = "file" if count == 1 else "files"
        print(f"{count} {noun} reformatted by Ruff:\n")
        for path in changed_files:
            print(f"  - {path}")
        print("\nPlease review the changes, stage the files, and re-run pre-commit.")
        return 1

    return 0 if ruff_exit == 0 else ruff_exit


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
