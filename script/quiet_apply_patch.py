#!/usr/bin/env python3
"""Apply patches with truncated output to avoid overwhelming the shell.

The helper wraps the built-in ``apply_patch`` command, capturing its stdout/stderr
and collapsing excessively long output so interactive sessions avoid massive
diff echoes that can trigger terminal disconnects. Provide patches via stdin
(default) or with ``--patch-file``. Adjust truncation with ``--max-lines`` to keep
only the most relevant lines from the command output.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Iterable


def _read_patch(patch_file: Path | None) -> str:
    if patch_file is None:
        return sys.stdin.read()
    return patch_file.read_text(encoding="utf-8")


def _print_block(label: str, content: str, max_lines: int) -> None:
    if not content:
        return

    lines = content.rstrip().splitlines()
    if len(lines) <= max_lines:
        print(f"{label}:")
        print(content, end="" if content.endswith("\n") else "\n")
        return

    midpoint = max_lines // 2
    head: Iterable[str] = lines[:midpoint]
    tail: Iterable[str] = lines[-(max_lines - midpoint) :]

    print(f"{label} (truncated to {max_lines} of {len(lines)} lines):")
    for line in head:
        print(line)
    print(f"... {len(lines) - max_lines} lines truncated ...")
    for line in tail:
        print(line)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Wrap apply_patch and suppress overly long output to keep diffs readable."
        )
    )
    parser.add_argument(
        "--patch-file",
        type=Path,
        help=(
            "Optional path to a patch file. If omitted, the helper reads the patch "
            "from stdin."
        ),
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=120,
        help=(
            "Maximum number of stdout/stderr lines to display before truncating "
            "apply_patch output."
        ),
    )

    args = parser.parse_args()
    patch_text = _read_patch(args.patch_file)
    if not patch_text.strip():
        print("No patch content provided; nothing to apply.")
        return 1

    result = subprocess.run(
        ["apply_patch"], input=patch_text, capture_output=True, text=True, check=False
    )

    _print_block("apply_patch stdout", result.stdout, args.max_lines)
    _print_block("apply_patch stderr", result.stderr, args.max_lines)

    if result.returncode != 0:
        print(f"apply_patch exited with status {result.returncode}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
