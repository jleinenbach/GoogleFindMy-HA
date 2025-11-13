#!/usr/bin/env python3
"""Wheelhouse cache inspection helper.

This CLI summarizes cached wheel metadata and optionally validates the contents
against a manifest of expected filename patterns. Reviewers without an on-disk
cache can pass ``--allow-missing`` to preview the formatter without needing to
generate wheels locally.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True, slots=True)
class WheelRecord:
    """Normalized view of a wheel cached on disk."""

    path: Path
    distribution: str
    version: str
    build_tag: str | None
    python_tag: str
    abi_tag: str
    platform_tag: str

    @property
    def identifier(self) -> str:
        """Return the canonical distribution identifier."""

        return f"{self.distribution}=={self.version}"

    @property
    def formatted_tag(self) -> str:
        """Return a compact interpreter / platform tag."""

        segments = [self.python_tag, self.abi_tag, self.platform_tag]
        return "/".join(segment for segment in segments if segment)


def iter_wheel_records(paths: Iterable[Path]) -> Iterable[WheelRecord]:
    """Yield parsed wheel metadata for the provided wheel paths."""

    for path in paths:
        if path.suffix != ".whl":
            continue
        stem_parts = path.stem.split("-")
        if len(stem_parts) < 5:
            continue
        distribution = stem_parts[0].replace("_", "-")
        version = stem_parts[1]
        python_tag, abi_tag, platform_tag = stem_parts[-3:]
        build_tokens: Sequence[str] = stem_parts[2:-3]
        build_tag = "-".join(build_tokens) if build_tokens else None
        yield WheelRecord(
            path=path,
            distribution=distribution,
            version=version,
            build_tag=build_tag,
            python_tag=python_tag,
            abi_tag=abi_tag,
            platform_tag=platform_tag,
        )


def load_manifest_patterns(manifest_path: Path | None) -> list[str]:
    """Load glob patterns from a manifest file if provided."""

    if manifest_path is None:
        return []
    patterns: list[str] = []
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


def summarize(records: Sequence[WheelRecord]) -> None:
    """Print a grouped summary of cached wheels."""

    if not records:
        print("(no wheels found)")
        return

    by_distribution: dict[str, dict[str, list[WheelRecord]]] = {}
    for record in records:
        versions = by_distribution.setdefault(record.distribution, {})
        versions.setdefault(record.version, []).append(record)

    for distribution in sorted(by_distribution):
        versions = by_distribution[distribution]
        print(f"{distribution}:")
        for version in sorted(versions):
            print(f"  - {version}")
            for record in sorted(
                versions[version],
                key=lambda item: (item.python_tag, item.abi_tag, item.platform_tag, item.build_tag or ""),
            ):
                size_kib = record.path.stat().st_size / 1024
                build_suffix = f" (build {record.build_tag})" if record.build_tag else ""
                print(
                    "    • "
                    f"{record.formatted_tag}{build_suffix}"
                    f" — {record.path.name} ({size_kib:.1f} KiB)"
                )
        print()


def report_manifest_coverage(patterns: Sequence[str], records: Sequence[WheelRecord]) -> int:
    """Report manifest coverage and return the number of missing patterns."""

    if not patterns:
        return 0

    print("Manifest coverage:")
    missing = 0
    filenames = [record.path.name for record in records]
  t for pattern in patterns:
        if any(fnmatch(name, pattern) for name in filenames):
            print(f"  ✓ {pattern}")
        else:
            print(f"  ✗ {pattern}")
            missing += 1
    print()
    return missing


def build_argument_parser() -> argparse.ArgumentParser:
    """Create the argument parser for the CLI."""

    parser = argparse.ArgumentParser(description="Summarize cached wheelhouse contents.")
    parser.add_argument(
        "wheelhouse",
        nargs="?",
        default=Path(".wheelhouse/ssot"),
        type=Path,
        help="Directory containing cached wheels (default: .wheelhouse/ssot)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Optional manifest of glob patterns to validate against the wheel cache.",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Continue even if the wheelhouse directory is absent (prints an empty summary).",
    )
    return parser


def main() -> int:
    """Entry point for the CLI."""

    parser = build_argument_parser()
    args = parser.parse_args()

    wheelhouse: Path = args.wheelhouse
    manifest: Path | None = args.manifest

    if not wheelhouse.exists():
        if not args.allow_missing:
            parser.error(f"wheelhouse directory '{wheelhouse}' does not exist")
        print(
            "Wheelhouse:",
            f"{wheelhouse} (missing)\n\n",
            "No wheel cache found; run without --allow-missing after caching wheels to validate contents.",
            sep="",
        )
        patterns = load_manifest_patterns(manifest)
        if patterns:
            print("Manifest coverage:\n  ✗ (skipped; wheelhouse missing)\n")
            return 1
        return 0

    wheel_paths = sorted(wheelhouse.iterdir())
    records = list(iter_wheel_records(wheel_paths))

    print(f"Wheelhouse: {wheelhouse} ({len(records)} wheel{'s' if len(records) != 1 else ''})\n")
    summarize(records)

    patterns = load_manifest_patterns(manifest)
    missing = report_manifest_coverage(patterns, records)

    if missing:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())