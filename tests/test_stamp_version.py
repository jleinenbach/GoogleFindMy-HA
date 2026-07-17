# tests/test_stamp_version.py
"""Regression tests for ``script/stamp_version.py`` (the release-stamp writer).

Covers three things the code-architekt review flagged as unguarded (Z1/Z2):

* ``VERSION_RE`` against the *real* 1.7 tag matrix -- plain SemVer releases plus
  the PEP 440 forms the maintainer actually uses (beta ``bN``, four-segment
  ``.N``) -- and the invalid forms that must be rejected (``v`` prefix, the
  Workflow-A hand-tag placeholder, ``rc`` prereleases, etc.).
* the three per-file stampers: exact-one-hit accounting, table/key scoping so a
  dependency spec or sibling key is never touched, and const canonicalisation.
* a byte-parity guard between ``VERSION_RE`` and the shell regex mirrored in
  ``.github/workflows/release-stamp.yml`` -- so the two guards can never drift
  apart silently.

The suite is CI-authoritative: ``tests/conftest.py`` hard-requires the real
``homeassistant`` package at collection time, so it is not locally collectable
without it. The stamp logic itself is pure stdlib and imported directly.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from script.stamp_version import (  # noqa: E402  (path bootstrap must precede import)
    VERSION_RE,
    StampError,
    stamp_const,
    stamp_manifest,
    stamp_pyproject,
)

# --- VERSION_RE: the real tag matrix on the 1.7 line -----------------------

VALID_TAGS = [
    "1.7.14",  # plain SemVer patch release
    "1.7.9",  # plain SemVer
    "1.8.0",  # plain SemVer minor
    "1.7.14b1",  # PEP 440 beta (maintainer's hand-tag scheme)
    "1.7.9b1",  # PEP 440 beta
    "1.7.13b2",  # PEP 440 beta, real tag on 1.7
    "1.7.14a1",  # PEP 440 alpha
    "1.7.13.1",  # four-segment maintenance tag, real tag on 1.7
    "1.7.13.0",  # four-segment maintenance tag
    "1.7.14.1b1",  # PEP 440 order: four-segment release THEN prerelease
]

INVALID_TAGS = [
    "v1.7.14",  # leading v prefix violates tag_format = "{version}"
    "1.7",  # too few segments
    "1.7.14-rc1",  # SemVer-style prerelease, not this scheme
    "1.7.14rc1",  # rc is not in the [ab] prerelease set
    "1.7.14.1.2",  # five segments
    "1.7.14b1.1",  # prerelease before the fourth segment (not PEP 440 order)
    "1.7.14 ",  # trailing whitespace
    " 1.7.14",  # leading whitespace
    "",  # empty
    "set-release-tag",  # Workflow A hand-mode placeholder MUST be rejected
]


@pytest.mark.parametrize("tag", VALID_TAGS)
def test_version_re_accepts_real_tags(tag: str) -> None:
    assert VERSION_RE.match(tag), f"{tag!r} should be accepted by VERSION_RE"


@pytest.mark.parametrize("tag", INVALID_TAGS)
def test_version_re_rejects_bad_tags(tag: str) -> None:
    assert not VERSION_RE.match(tag), f"{tag!r} should be rejected by VERSION_RE"


# --- stamp_pyproject: [tool.poetry] table scoping --------------------------


def test_stamp_pyproject_hits_only_tool_poetry() -> None:
    text = (
        "[tool.poetry]\n"
        'version = "1.7.13"\n'
        'name = "x"\n'
        "\n"
        "[tool.poetry.dependencies]\n"
        'somelib = { version = "2.0.0" }\n'
        "\n"
        "[tool.other]\n"
        'version = "9.9.9"\n'
    )
    out = stamp_pyproject(text, "1.7.14b1")
    assert 'version = "1.7.14b1"' in out
    # Dependency spec and the unrelated table are untouched.
    assert 'somelib = { version = "2.0.0" }' in out
    assert 'version = "9.9.9"' in out
    # Exactly one literal changed.
    assert out.count('"1.7.14b1"') == 1


def test_stamp_pyproject_zero_hits_raises() -> None:
    with pytest.raises(StampError):
        stamp_pyproject('[tool.other]\nversion = "1.0.0"\n', "1.7.14")


# --- stamp_const: annotationless literal, canonicalisation -----------------


def test_stamp_const_canonicalises_loose_form() -> None:
    # Loosest guard-valid form: single quotes and extra whitespace.
    text = "INTEGRATION_VERSION  =  '1.7.13'\n"
    out = stamp_const(text, "1.7.14b1")
    # Written form is always the canonical double-quote single-space shape.
    assert out == 'INTEGRATION_VERSION = "1.7.14b1"\n'


def test_stamp_const_double_quote_form() -> None:
    text = 'INTEGRATION_VERSION = "1.7.13"\n'
    assert stamp_const(text, "1.7.14") == 'INTEGRATION_VERSION = "1.7.14"\n'


def test_stamp_const_preserves_following_blank_line() -> None:
    # Mirrors the real const.py layout: the literal is followed by a blank line.
    # A greedy `\s*$` match would swallow that newline and delete the blank line
    # on every stamp; the horizontal-whitespace match must leave it intact.
    text = 'INTEGRATION_VERSION = "1.7.13"\n\n# next section\n'
    out = stamp_const(text, "1.7.14")
    assert out == 'INTEGRATION_VERSION = "1.7.14"\n\n# next section\n'


def test_stamp_const_zero_hits_raises() -> None:
    with pytest.raises(StampError):
        stamp_const("OTHER = 1\n", "1.7.14")


# --- stamp_manifest: single top-level "version" ----------------------------


def test_stamp_manifest_replaces_only_version() -> None:
    text = '{\n  "domain": "googlefindmy",\n  "version": "1.7.13",\n  "name": "x"\n}\n'
    out = stamp_manifest(text, "1.7.14b1")
    parsed = json.loads(out)
    assert parsed["version"] == "1.7.14b1"
    assert parsed["domain"] == "googlefindmy"  # untouched


def test_stamp_manifest_no_version_key_raises() -> None:
    with pytest.raises(StampError):
        stamp_manifest('{"domain": "x"}', "1.7.14")


# --- Z2: byte-parity between VERSION_RE and the shell guard -----------------


def test_workflow_guard_regex_matches_version_re() -> None:
    """release-stamp.yml's tag guard must mirror VERSION_RE byte-for-byte.

    The two live in different languages (Python ``re`` vs POSIX ERE via
    ``grep -qE``) but are kept textually identical so a change to one without
    the other is caught here instead of at release time.
    """
    wf = (_REPO_ROOT / ".github/workflows/release-stamp.yml").read_text(
        encoding="utf-8"
    )
    # The workflow also carries a 40-hex SHA guard; pick the version-shaped one.
    patterns = re.findall(r"grep -qE '([^']+)'", wf)
    tag_patterns = [p for p in patterns if p.startswith(r"^[0-9]+\.")]
    assert len(tag_patterns) == 1, (
        f"expected exactly one tag guard, found {tag_patterns}"
    )
    assert tag_patterns[0] == VERSION_RE.pattern, (
        f"guard drift: workflow {tag_patterns[0]!r} != VERSION_RE {VERSION_RE.pattern!r}"
    )
