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
    "v1.7.14",  # a v-prefix is not a VERSION; release-stamp.yml strips it off
    #             the TAG first (see test_tag_to_version_v_strip_semantics)
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
    """Both release workflows' tag guards must mirror VERSION_RE byte-for-byte.

    The guards live in different languages (Python ``re`` vs POSIX ERE via
    ``grep -qE``) but are kept textually identical across ``release-stamp.yml``
    (the tag guard) and ``release.yml`` (the REAL_TIP line-tip gate) so a change
    to one without the others is caught here instead of at release time.
    """
    for wf_name in (
        ".github/workflows/release-stamp.yml",
        ".github/workflows/release.yml",
    ):
        wf = (_REPO_ROOT / wf_name).read_text(encoding="utf-8")
        # Other steps also grep to classify errors, but only with ``-qiE``/``-qxF``;
        # pick the sole ``grep -qE`` carrying a ``^[0-9]`` version-shaped pattern.
        patterns = re.findall(r"grep -qE '([^']+)'", wf)
        tag_patterns = [p for p in patterns if p.startswith(r"^[0-9]+\.")]
        assert len(tag_patterns) == 1, (
            f"expected exactly one tag guard in {wf_name}, found {tag_patterns}"
        )
        assert tag_patterns[0] == VERSION_RE.pattern, (
            f"guard drift in {wf_name}: {tag_patterns[0]!r} != "
            f"VERSION_RE {VERSION_RE.pattern!r}"
        )


def test_real_tip_gate_blanks_foreign_tag_keeps_pep440() -> None:
    """release.yml REAL_TIP gate: foreign tips blanked, PEP 440 tips kept.

    The gate first strips an optional leading ``v`` (BSkando tags ``vX.Y.Z``),
    then re-uses VERSION_RE (the SSOT) to decide whether the topological line tip
    is a valid version. A foreign, non-version tag (Workflow-A hand-tag
    placeholder, ``nightly``, ``v2`` -> ``2`` after the strip) must be blanked so
    it cannot spuriously route PSR to the hand-tag draft; a genuine PEP 440
    beta/four-segment tag -- and a genuine ``v``-prefixed release tag -- must be
    kept so a real stale-base mismatch still fires ``MODE=hand``. This mirrors the
    shell steps
    ``REAL_TIP="${REAL_TIP#v}"`` then
    ``[ -n "$REAL_TIP" ] && ! printf '%s' "$REAL_TIP" | grep -qE '<VERSION_RE>'``.

    Mutation cross-check (re-runnable): (1) replace ``gate``'s tail with a bare
    ``return stripped`` (no gating) and the three foreign-tag assertions turn red;
    (2) drop the ``#v`` strip line and ``gate("v1.7.14")`` turns red.
    """

    def gate(real_tip: str) -> str:
        # Faithful Python mirror of the release.yml shell gate, incl. the
        # ``REAL_TIP="${REAL_TIP#v}"`` strip that precedes the VERSION_RE check.
        stripped = real_tip[1:] if real_tip.startswith("v") else real_tip
        return stripped if VERSION_RE.match(stripped) else ""

    # foreign / non-version tips are blanked -> no spurious MODE=hand
    assert gate("set-release-tag") == ""
    assert gate("nightly") == ""
    assert gate("v2") == ""  # v-strip -> "2", still not a full version
    # genuine PEP 440 line tips are kept -> MODE=hand still fires on a mismatch
    assert gate("1.7.14b1") == "1.7.14b1"
    assert gate("1.7.14.0") == "1.7.14.0"
    assert gate("1.7.13") == "1.7.13"
    # a genuine v-prefixed release tag (BSkando) is kept as its v-less version
    assert gate("v1.7.14") == "1.7.14"

    # Lock the strip into release.yml itself: the mirror above only models the
    # intended behaviour, so without this the strip could be dropped from the
    # workflow with no red test.
    wf = (_REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert 'REAL_TIP="${REAL_TIP#v}"' in wf, (
        "release.yml must strip an optional leading v from REAL_TIP"
    )


# --- v-prefix tag -> v-less version mapping (both fork conventions) ---------


def test_release_stamp_strips_v_for_version_keeps_tag_for_release() -> None:
    """release-stamp.yml maps a (possibly v-prefixed) TAG to a v-less VERSION.

    The version stamped into the code literals is the v-less value
    (``VERSION="${TAG_NAME#v}"``), while git/release object addressing keeps the
    full published tag (checkout ref, ``rev-list refs/tags``, ``gh release
    upload``). Guards against a mutation that strips the wrong variable, which the
    byte-parity test above cannot catch (it only checks the regex pattern).
    """
    wf = (_REPO_ROOT / ".github/workflows/release-stamp.yml").read_text(
        encoding="utf-8"
    )
    # The version passed to the stamp script is the v-stripped value.
    assert 'VERSION="${TAG_NAME#v}"' in wf, "must derive a v-less VERSION"
    assert '--version "$VERSION"' in wf, "stamp must use the v-less VERSION"
    # The guard validates the stripped VERSION, not the raw TAG_NAME.
    assert 'printf \'%s\' "$VERSION" | grep -qE' in wf, (
        "guard must validate the stripped VERSION"
    )
    # Git/release object addressing keeps the full published tag (with any `v`).
    assert "ref: ${{ github.event.release.tag_name }}" in wf
    assert 'git rev-list -n1 "refs/tags/${TAG_NAME}"' in wf
    assert 'gh release upload "$TAG_NAME"' in wf, (
        "release upload must address the real tag, not the stripped version "
        "(locked by test_hacs_validation.py)"
    )


@pytest.mark.parametrize(
    ("tag", "version"),
    [
        ("v1.7.14", "1.7.14"),  # BSkando v-prefix -> v-less version
        ("1.7.14", "1.7.14"),  # jleinenbach no-prefix -> unchanged (no-op)
        ("v1.7.14b1", "1.7.14b1"),  # v-prefix beta
        ("1.7.13.1", "1.7.13.1"),  # four-segment, no prefix
        ("vv1", "v1"),  # POSIX ${VAR#v} strips exactly ONE leading v
    ],
)
def test_tag_to_version_v_strip_semantics(tag: str, version: str) -> None:
    """POSIX ``${TAG_NAME#v}`` strips at most one leading ``v``.

    ``vv1 -> v1`` (not ``1``) proves a single-strip, so the guard would still
    reject a malformed double-v tag. A real ``v``-prefixed tag maps to a version
    that VERSION_RE accepts; the raw ``v``-tag itself does not.
    """
    stripped = tag[1:] if tag.startswith("v") else tag
    assert stripped == version
    if version in {"1.7.14", "1.7.14b1", "1.7.13.1"}:
        assert VERSION_RE.match(stripped), f"{stripped!r} must be a valid version"
        if tag.startswith("v"):
            assert not VERSION_RE.match(tag), f"raw tag {tag!r} must not match"


# --- Tag-vs-branch: checkout must pin the published tag ---------------------


def test_workflow_checks_out_published_tag() -> None:
    """The stamp workflow must check out the published TAG, not target_commitish.

    GitHub documents ``release.target_commitish`` as unused once the git tag
    already exists -- it then defaults to the repo default branch, which only
    coincidentally equals the maintenance line on some forks. Checking that out
    would stamp and package the wrong branch. Pinning ``tag_name`` guarantees the
    stamped tree and the uploaded HACS ZIP always match the exact published
    commit. Regression guard for the tag-vs-branch confusion
    (CA-WORKFLOW-EVENT-PAYLOAD-CONTRACT-001).
    """
    wf = (_REPO_ROOT / ".github/workflows/release-stamp.yml").read_text(
        encoding="utf-8"
    )
    # The checkout ref must reference the published tag ...
    assert "ref: ${{ github.event.release.tag_name }}" in wf, (
        "checkout step must pin the published tag as ref"
    )
    # ... and never the stale target_commitish.
    assert "ref: ${{ github.event.release.target_commitish }}" not in wf, (
        "checkout must not use target_commitish (stale for pre-existing tags)"
    )
    # The stamp push must resolve the owning branch from the tag commit, so a
    # stale/wrong target_commitish cannot send the stamp to the wrong branch.
    assert "git branch -r --contains" in wf, (
        "stamp push must resolve the owning branch from the tag commit, "
        "not trust the stale target_commitish"
    )
    # The resolution must NOT fall back to the stale event hint as a tiebreaker:
    # when the tag sits on several branches (e.g. main + 1.7 on a non-diverged
    # maintenance line) the stale target_commitish could steer the stamp to the
    # wrong branch. Only an unambiguous single owner is stamped; zero or several
    # owners skip safely (CA-WORKFLOW-EVENT-PAYLOAD-CONTRACT-001).
    assert "TARGET_BRANCH" not in wf, (
        "branch resolution must not use the stale target_commitish hint at all; "
        "the tiebreaker could pick the wrong branch when the tag has several owners"
    )
