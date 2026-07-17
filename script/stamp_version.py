#!/usr/bin/env python3
# script/stamp_version.py
"""Deterministically stamp a released version into the three version literals.

This is the code-writing half of the "manual release -> code stamp" handshake
(see .github/workflows/release-stamp.yml). It takes the *exact* version the
maintainer published (``--version``, e.g. ``1.7.14`` or a beta ``1.7.14b1``)
and writes it verbatim into the three places that must move together:

* ``pyproject.toml``            -> ``[tool.poetry] version``
* ``custom_components/googlefindmy/const.py`` -> ``INTEGRATION_VERSION`` (no
  type annotation on purpose; ``version_variables`` and the guard tests both
  require the annotationless ``NAME = "x"`` form)
* ``custom_components/googlefindmy/manifest.json`` -> top-level ``"version"``

It never *computes* a version (that is semantic-release's job in the proposal
step) -- it only mirrors the tag the maintainer chose, so a hand-picked or beta
tag is honoured exactly. Each substitution must hit exactly one literal; zero or
multiple hits abort with a non-zero exit so a mis-anchored write can never land.

Usage (preview the effect on a clean worktree, then inspect ``git diff``)::

    python script/stamp_version.py --version 1.7.14
    python script/stamp_version.py --version 1.7.14b1 --repo-root /path/to/repo

The tag is validated against the ``tag_format = "{version}"`` convention
(no ``v`` prefix): ``X.Y.Z`` with an optional ``bN``/``aN`` prerelease and an
optional ``.N`` fourth segment (covers 1.7.14, 1.7.14b1, 1.7.9b1, 1.7.13.1).
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Canonical tag/version shape (SSOT). tag_format = "{version}" means the git tag
# IS the version, so a leading `v` is rejected. Mirrors the real tag set on 1.7,
# which is PEP 440 (`bN`/`aN` betas, four-segment `.N`) -- deliberately broader
# than SemVer so hand-picked maintenance tags stamp cleanly. Consequence:
# semantic-release (SemVer-only) cannot compute a trustworthy *proposal* for such
# lines, so release.yml (Workflow A) opens an empty hand-tag draft there instead
# of proposing a stale number; the stamp path here still honours the exact chosen
# tag verbatim. The shell guard in release-stamp.yml MUST stay byte-equal to this
# pattern (locked by tests/test_stamp_version.py).
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+([ab][0-9]+)?(\.[0-9]+)?$")

CONST_REL = "custom_components/googlefindmy/const.py"
MANIFEST_REL = "custom_components/googlefindmy/manifest.json"
PYPROJECT_REL = "pyproject.toml"


class StampError(RuntimeError):
    """Raised when a literal cannot be stamped exactly once."""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def stamp_pyproject(text: str, version: str) -> str:
    """Replace ``version = "..."`` inside the ``[tool.poetry]`` table only.

    Table-scoped so a dependency spec or another table's ``version`` can never
    be hit. Exactly one match is required.
    """
    lines = text.splitlines(keepends=True)
    in_poetry = False
    hits = 0
    version_line = re.compile(r'^(version\s*=\s*)"[^"]*"(.*)$')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_poetry = stripped == "[tool.poetry]"
            continue
        if in_poetry:
            m = version_line.match(line)
            if m:
                lines[i] = f'{m.group(1)}"{version}"{m.group(2)}\n'
                hits += 1
    if hits != 1:
        raise StampError(
            f"{PYPROJECT_REL}: expected exactly 1 [tool.poetry] version line, "
            f"found {hits}"
        )
    return "".join(lines)


def stamp_const(text: str, version: str) -> str:
    """Replace the annotationless ``INTEGRATION_VERSION`` literal, canonicalising.

    The match is deliberately as permissive as the loosest guard test
    (test_hypothesis_properties.py: flexible whitespace, single or double
    quotes), so a valid-per-test const.py never makes the stamp fail. The
    written form is always the canonical ``INTEGRATION_VERSION = "x"`` (single
    spaces, double quotes) that test_hacs_validation.py requires -- so stamping
    also normalises the literal and never re-introduces a type annotation.

    The trailing match is ``[ \\t]*$`` (horizontal whitespace only), not
    ``\\s*$``: a greedy ``\\s*$`` would also swallow the newline(s) after the
    literal and delete the blank line that follows it in const.py on every
    stamp. Restricting to spaces/tabs keeps the surrounding layout intact.
    """
    pattern = re.compile(
        r"""^INTEGRATION_VERSION\s*=\s*["'][^"']*["'][ \t]*$""", re.MULTILINE
    )
    new_text, hits = pattern.subn(f'INTEGRATION_VERSION = "{version}"', text)
    if hits != 1:
        raise StampError(
            f"{CONST_REL}: expected exactly 1 INTEGRATION_VERSION literal "
            f'(form INTEGRATION_VERSION = "x"), found {hits}'
        )
    return new_text


def stamp_manifest(text: str, version: str) -> str:
    """Replace the top-level ``"version"`` value with a minimal one-line diff.

    JSON is parsed to confirm a single top-level ``version`` key exists, then a
    targeted regex rewrites only that value (avoids reordering/reformatting the
    manifest, which hassfest is sensitive to). Exactly one match is required.
    """
    data = json.loads(text)
    if not isinstance(data, dict) or "version" not in data:
        raise StampError(f"{MANIFEST_REL}: no top-level 'version' key")
    pattern = re.compile(r'("version"\s*:\s*)"[^"]*"')
    new_text, hits = pattern.subn(rf'\g<1>"{version}"', text)
    if hits != 1:
        raise StampError(
            f'{MANIFEST_REL}: expected exactly 1 "version" field, found {hits}'
        )
    # Cross-check: the rewritten value round-trips and equals the target.
    if json.loads(new_text).get("version") != version:
        raise StampError(f"{MANIFEST_REL}: post-stamp version mismatch")
    return new_text


def stamp_all(repo_root: Path, version: str) -> list[Path]:
    """Stamp all three files. Returns the list of changed paths (sorted)."""
    targets = {
        repo_root / PYPROJECT_REL: stamp_pyproject,
        repo_root / CONST_REL: stamp_const,
        repo_root / MANIFEST_REL: stamp_manifest,
    }
    changed: list[Path] = []
    for path, stamper in targets.items():
        if not path.is_file():
            raise StampError(f"{path}: file not found")
        original = _read(path)
        stamped = stamper(original, version)
        if stamped != original:
            _write(path, stamped)
            changed.append(path)
    return sorted(changed)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stamp the exact released version into pyproject.toml, const.py and "
            "manifest.json. Does not compute a version; mirrors --version verbatim."
        )
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Exact version to stamp, e.g. 1.7.14 or 1.7.14b1 (no 'v' prefix).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root (default: current working directory).",
    )
    args = parser.parse_args()

    version = args.version.strip()
    if not VERSION_RE.match(version):
        parser.error(
            f"version {version!r} violates tag_format='{{version}}'; expected "
            "X.Y.Z with optional bN/aN and optional .N (no 'v' prefix)"
        )

    try:
        changed = stamp_all(args.repo_root, version)
    except StampError as exc:
        print(f"stamp_version: ERROR: {exc}")
        return 1

    for path in changed:
        print(f"stamped {path} -> {version}")
    print(f"stamp_version: OK, {len(changed)} file(s) set to {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
