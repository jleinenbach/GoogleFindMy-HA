# tests/test_workflow_action_pins.py
"""Keep every third-party GitHub Action pinned to a full commit SHA.

A tag such as ``actions/checkout@v7`` can be moved to other code at any time; a
40-character commit SHA cannot. Each pinned line carries a comment naming the
release tag, which is what Dependabot's ``github-actions`` ecosystem reads to bump
the SHA and the comment together. The contract is documented in the root
``AGENTS.md`` (bullet "Action pinning").

Two actions track their default branch on purpose (see ``BRANCH_TRACKING_ACTIONS``).
Their newest release tags lag far behind the branch, and a pinned SHA would freeze
the validation rules they apply until someone refreshes it by hand.

Blind spot: the lines are read as text, not as parsed YAML, so that the version
comment survives. A ``uses:`` value spread over a YAML block scalar or built from an
expression would not be seen; none exists today, and the vacuity check below fails
if the reader stops finding lines at all.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

# Actions that deliberately follow a branch. Extend only together with the
# "Action pinning" bullet in AGENTS.md.
BRANCH_TRACKING_ACTIONS: dict[str, frozenset[str]] = {
    "home-assistant/actions/hassfest": frozenset({"master"}),
    "hacs/action": frozenset({"main"}),
}
BRANCH_TRACKING_MARKER = "see AGENTS.md, Action pinning"

_USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<value>[^\s#]+)\s*(?P<comment>#.*)?$")
_PINNED_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_COMMENT_RE = re.compile(r"^#\s*v?\d+(?:\.\d+)*$")


@dataclass(frozen=True)
class UsesLine:
    """One ``uses:`` line of a workflow file."""

    path: str
    lineno: int
    action: str
    ref: str
    comment: str
    previous_line: str


def parse_uses_line(line: str) -> tuple[str, str, str] | None:
    """Return ``(action, ref, comment)`` for a third-party ``uses:`` line.

    Local actions (``./``) and container actions (``docker://``) are not pinned
    through a Git ref and return ``None``, as does any line that is not ``uses:``.
    """

    match = _USES_RE.match(line)
    if match is None:
        return None
    value = match.group("value")
    if value.startswith(("./", "docker://")) or "@" not in value:
        return None
    action, ref = value.rsplit("@", 1)
    return action, ref, (match.group("comment") or "").strip()


def is_sha_pinned(ref: str, comment: str) -> bool:
    """Return whether a ref is a full commit SHA with a release-tag comment."""

    return bool(_PINNED_RE.fullmatch(ref)) and bool(_TAG_COMMENT_RE.fullmatch(comment))


def _collect_uses_lines() -> list[UsesLine]:
    lines: list[UsesLine] = []
    for path in sorted(WORKFLOW_DIR.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(text):
            parsed = parse_uses_line(line)
            if parsed is None:
                continue
            action, ref, comment = parsed
            lines.append(
                UsesLine(
                    path=str(path.relative_to(REPO_ROOT)),
                    lineno=index + 1,
                    action=action,
                    ref=ref,
                    comment=comment,
                    previous_line=text[index - 1] if index else "",
                )
            )
    return lines


USES_LINES = _collect_uses_lines()


def _repository(action: str) -> str:
    """Reduce ``owner/repo/sub/path`` to ``owner/repo``."""

    return "/".join(action.split("/")[:2])


def test_reader_finds_uses_lines() -> None:
    """Fail if the reader stops seeing workflow steps, which would empty every check."""

    assert USES_LINES, f"no `uses:` lines found under {WORKFLOW_DIR}"


def test_every_third_party_action_is_sha_pinned() -> None:
    """Every action outside the branch-tracking list is pinned to a SHA plus tag comment."""

    offenders = [
        f"{line.path}:{line.lineno} {line.action}@{line.ref} {line.comment}".rstrip()
        for line in USES_LINES
        if line.action not in BRANCH_TRACKING_ACTIONS
        and not is_sha_pinned(line.ref, line.comment)
    ]
    assert not offenders, (
        "pin these actions to a full commit SHA with a `# <tag>` comment "
        "(AGENTS.md, Action pinning):\n" + "\n".join(offenders)
    )


def test_branch_tracking_exceptions_are_current_and_explained() -> None:
    """Each exception is still used, only on its allowed branch, and carries its comment."""

    seen: dict[str, int] = defaultdict(int)
    problems: list[str] = []
    for line in USES_LINES:
        allowed = BRANCH_TRACKING_ACTIONS.get(line.action)
        if allowed is None:
            continue
        seen[line.action] += 1
        if line.ref not in allowed:
            problems.append(
                f"{line.path}:{line.lineno} {line.action}@{line.ref} is not one of {sorted(allowed)}"
            )
        if BRANCH_TRACKING_MARKER not in line.previous_line:
            problems.append(
                f"{line.path}:{line.lineno} lacks the comment line '{BRANCH_TRACKING_MARKER}' above it"
            )
    stale = sorted(set(BRANCH_TRACKING_ACTIONS) - set(seen))
    assert not stale, f"remove stale branch-tracking entries: {stale}"
    assert not problems, "\n".join(problems)


def test_one_sha_and_tag_per_action() -> None:
    """One repository resolves to one SHA and one tag comment across all workflows."""

    pins: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for line in USES_LINES:
        if line.action in BRANCH_TRACKING_ACTIONS:
            continue
        pins[_repository(line.action)].add((line.ref, line.comment))
    split = {repo: sorted(refs) for repo, refs in pins.items() if len(refs) > 1}
    assert not split, f"actions pinned to more than one SHA or tag: {split}"


_SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (f"      - uses: actions/checkout@{_SHA} # v7.0.1", True),
        (f"        uses: github/codeql-action/init@{_SHA} # v4.38.2", True),
        ("      - uses: actions/checkout@v7", False),
        ("        uses: hacs/action@main", False),
        (f"      - uses: actions/checkout@{_SHA}", False),
        (f"      - uses: actions/checkout@{_SHA[:-1]} # v7.0.1", False),
        (f"      - uses: actions/checkout@{_SHA.upper()} # v7.0.1", False),
        (f"      - uses: actions/checkout@{_SHA} # pinned", False),
    ],
)
def test_pin_predicate_separates_pinned_from_mutable(line: str, expected: bool) -> None:
    """Positive and negative controls for the predicate the real checks rely on."""

    parsed = parse_uses_line(line)
    assert parsed is not None
    _action, ref, comment = parsed
    assert is_sha_pinned(ref, comment) is expected


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: ./.github/actions/local",
        "      - uses: docker://alpine:3",
        "      - run: echo uses: x@v1",
    ],
)
def test_local_container_and_non_uses_lines_are_ignored(line: str) -> None:
    """Local and container actions are not Git refs; other keys are not steps."""

    assert parse_uses_line(line) is None
