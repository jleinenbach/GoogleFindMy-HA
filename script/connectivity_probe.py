"""Capture the standard connectivity probe output for reproducible citations.

This helper runs the project-standard connectivity check
(`python -m pip install --dry-run --no-deps pip`), writes the full stdout/stderr
transcript to a log file, and prints the log location for quick citation reuse.
Use the optional ``--output`` flag to redirect the log elsewhere.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

DEFAULT_OUTPUT = Path("artifacts/connectivity_probe.log")


def run_probe(output_path: Path) -> int:
    """Execute the connectivity probe and persist its output.

    Returns the subprocess exit status so callers can propagate failures.
    """

    cmd = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--dry-run",
        "--no-deps",
        "pip",
    ]
    print(f"Running connectivity probe: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.stdout + result.stderr, encoding="utf-8")
    print(f"Wrote probe output to {output_path}")

    if result.returncode != 0:
        print(
            "Probe reported a non-zero exit status; check the log for details.",
            file=sys.stderr,
        )

    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the standard connectivity probe and save the output for later citations."
        )
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=(
            "Path to the log file capturing probe output. "
            f"Defaults to {DEFAULT_OUTPUT}."
        ),
    )
    args = parser.parse_args()
    return run_probe(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
