#!/usr/bin/env python3
# script/local_verify.py
"""Run the canonical local verification commands for Google Find My."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

DEFAULT_RUFF_EXCLUDES: tuple[str, ...] = (
    "custom_components/googlefindmy/ProtoDecoders/",
    "custom_components/googlefindmy/Auth/firebase_messaging/proto/",
)
RUFF_FORMAT_SCRIPT = (
    Path(__file__).resolve().parent / "precommit_hooks" / "ruff_format.py"
)


def _echo_command(command: Sequence[str]) -> None:
    """Log the command in a shell-friendly format."""

    printable = shlex.join(command)
    print(f"+ {printable}")


def _run_command(command: Sequence[str]) -> int:
    """Execute a subprocess command and return its exit status."""

    _echo_command(command)
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        print(f"Command exited with status {result.returncode}")
    return result.returncode


def _build_ruff_command() -> list[str]:
    """Return the Ruff format --check command with repository defaults."""

    command = [sys.executable, str(RUFF_FORMAT_SCRIPT), "--check"]
    command.extend(f"--exclude={path}" for path in DEFAULT_RUFF_EXCLUDES)
    return command


def _build_pytest_command(pytest_args: Sequence[str] | None) -> list[str]:
    """Return the pytest command respecting optional overrides."""

    command = ["pytest", "-q"]
    if pytest_args:
        command.extend(pytest_args)
    return command


def main(argv: Sequence[str] | None = None) -> int:
    """Run Ruff format --check and pytest -q, returning the combined status."""

    parser = argparse.ArgumentParser(
        description=(
            "Run Ruff format --check followed by pytest -q using the repository\n"
            "defaults so contributors can quickly mirror the required local"
            " checks."
        )
    )
    parser.add_argument(
        "--skip-ruff",
        action="store_true",
        help="Skip running Ruff format --check (not recommended).",
    )
    parser.add_argument(
        "--skip-pytest",
        action="store_true",
        help="Skip running pytest -q.",
    )
    parser.add_argument(
        "--pytest-args",
        nargs=argparse.REMAINDER,
        help=(
            "Additional arguments to pass to pytest after the default '-q'. "
            "Place this flag last and prefix pytest options with '--'."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    exit_code = 0

    if not args.skip_ruff:
        ruff_exit = _run_command(_build_ruff_command())
        exit_code = max(exit_code, ruff_exit)

    if not args.skip_pytest:
        pytest_exit = _run_command(_build_pytest_command(args.pytest_args or []))
        exit_code = max(exit_code, pytest_exit)

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
