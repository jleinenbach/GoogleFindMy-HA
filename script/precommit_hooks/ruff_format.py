#!/usr/bin/env python3
# script/precommit_hooks/ruff_format.py
"""Run Ruff format and report reformatted files."""

from __future__ import annotations

import subprocess
import sys


def _run_ruff(args: list[str]) -> int:
    """Run ``python -m ruff format`` with the provided arguments."""
    command = [sys.executable, "-m", "ruff", "format", *args]
    return subprocess.run(command, check=False).returncode


def _collect_changed_files(filenames: list[str]) -> list[str]:
    if not filenames:
        return []

    diff_command = ["git", "diff", "--name-only", "--", *filenames]
    result = subprocess.run(diff_command, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(result.returncode)

    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    if not argv:
        return 0

    ruff_exit = _run_ruff(argv)
    if ruff_exit not in (0, 1):
        return ruff_exit

    filenames = [arg for arg in argv if not arg.startswith("-")]
    changed_files = _collect_changed_files(filenames)
    if changed_files:
        print("Ruff formatted the following files:\n")
        for path in changed_files:
            print(f"  - {path}")
        print("\nPlease review the changes, stage the files, and re-run pre-commit.")
        return 1

    return 0 if ruff_exit == 0 else ruff_exit


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
