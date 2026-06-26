# tests/test_guard_config_entry_stub.py
"""Guard requiring the canonical ``make_config_entry`` factory in new tests.

``tests/AGENTS.md`` (Canonical config-entry stub) mandates that new tests build
config entries via ``tests.helpers.config_entries_stub.make_config_entry(...)``
instead of ad-hoc ``SimpleNamespace(entry_id=..., data=..., options=...)``
constructions. The factory guarantees ``data``/``options`` are always plain
dicts (never ``None``), which the production helpers (``_opt()``,
``entry.data.get(...)``) rely on; ad-hoc stubs that omit or null either field
are the structural cause of the ``AttributeError`` failures fixed in PR #172.

The rule is plain prose with no linter behind it, so omissions pass
``ruff``/``mypy`` silently and only surface in review (the Codex finding on
PR #1142 is the latest instance). This guard fails loudly instead, mirroring
the sibling ``test_guard_async_test_marker`` and ``test_guard_path_header``
guards.

Two design choices keep the guard honest and non-disruptive:

* **Per-file count ratchet, not a whole-file skip.** ``LEGACY_ALLOWLIST`` maps
  each pre-existing offender file to the *number* of ad-hoc stubs it currently
  carries, and that count may only shrink. A whole-file skip would freeze the
  baseline at file granularity and let a *new* ad-hoc stub be appended to an
  already-listed config-flow/coordinator module unseen -- the single most
  likely edit path. Pinning the count instead means any new stub raises the
  file above its frozen baseline and fails the guard; migrating stubs away
  lowers the count and the stale self-test demands the frozen number follow.

* **Sub-entry stubs are excluded from the predicate.** A config *sub-entry*
  legitimately shares the ``entry_id`` + ``data``/``options`` shape but carries
  sub-entry-only markers (``subentry_type``, ``parent_entry_id``,
  ``config_subentry_id``, ``subentry_id``). ``make_config_entry`` builds a
  ConfigEntry, the wrong object type for a sub-entry, so flagging these would
  force contributors to misuse the factory or pad the allowlist. They are not
  what ``tests/AGENTS.md`` addresses and are deliberately not counted.

Out of scope (no current occurrence in the test tree): aliased imports
(``from types import SimpleNamespace as SN``) and keyword-splat constructions
(``SimpleNamespace(**base)``), which the ``ast`` keyword inspection cannot see.

Scope note: this guard covers ``tests/test_*.py`` only, matching the rule's
"New tests" addressee and excluding the factory itself
(``tests/helpers/config_entries_stub.py``), which builds the very shape it
provides.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"

# Keyword markers that identify a config *sub-entry* stub. Any one of these on a
# ``SimpleNamespace(entry_id=..., data=...)`` call means the construct models a
# ConfigSubentry, for which ``make_config_entry`` is the wrong factory; such
# calls are excluded from the predicate below.
_SUBENTRY_MARKERS = frozenset(
    {"subentry_type", "parent_entry_id", "config_subentry_id", "subentry_id"}
)

# Pre-existing ad-hoc ``SimpleNamespace`` config-entry stubs, frozen per file as
# a count ratchet: ``repo-relative path -> number of offending stubs``. The
# count may only decrease. Adding a stub to one of these files raises it above
# the frozen number and fails the main guard; removing/migrating stubs lowers
# the real count and the stale self-test requires the frozen number to follow
# (drop the entry once a file reaches zero). Do not raise any number and do not
# add new files; use ``make_config_entry`` in new tests instead. The baseline is
# regenerated programmatically (subentry-excluded predicate), never hand-edited.
LEGACY_ALLOWLIST: dict[str, int] = {
    "tests/test_config_flow.py": 1,
    "tests/test_config_flow_basic.py": 2,
    "tests/test_config_flow_basics.py": 6,
    "tests/test_config_flow_initial_auth.py": 3,
    "tests/test_coordinator_device_registry.py": 7,
    "tests/test_coordinator_subentry_repair.py": 1,
    "tests/test_coordinator_subentry_visibility.py": 2,
    "tests/test_coordinator_timeout.py": 3,
    "tests/test_coordinator_visibility_availability.py": 3,
    "tests/test_device_identifier_normalization.py": 2,
    "tests/test_device_tracker.py": 2,
    "tests/test_eid_data_flow.py": 1,
    "tests/test_init_basics.py": 1,
    "tests/test_metadata_helpers.py": 1,
    "tests/test_poll_timeout_hardening.py": 1,
    "tests/test_services_refresh_device_urls.py": 4,
    "tests/test_spot_grpc_client.py": 1,
    "tests/test_transient_owner_key_propagation.py": 1,
}


def _is_config_entry_stub(call: ast.Call) -> bool:
    """Whether a call is an ad-hoc ``SimpleNamespace`` config-entry stub.

    Strict shape: the callee resolves to ``SimpleNamespace`` (both the bare
    ``SimpleNamespace(...)`` and the attribute form ``types.SimpleNamespace(...)``)
    and the keyword set carries ``entry_id`` together with at least one of
    ``data``/``options`` -- the exact construction tests/AGENTS.md forbids.

    Two shapes are deliberately not flagged: a bare ``entry_id`` without
    ``data``/``options``, and a config *sub-entry* stub (identified by any
    marker in :data:`_SUBENTRY_MARKERS`), for which ``make_config_entry`` would
    build the wrong object type.
    """
    func = call.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return False
    if name != "SimpleNamespace":
        return False
    keywords = {kw.arg for kw in call.keywords if kw.arg is not None}
    if "entry_id" not in keywords or not ("data" in keywords or "options" in keywords):
        return False
    if keywords & _SUBENTRY_MARKERS:
        return False
    return True


def _offending_stubs(path: Path) -> list[int]:
    """Return the line number of every ad-hoc config-entry stub in ``path``.

    ``ast.parse`` deliberately propagates ``SyntaxError`` rather than swallowing
    it, so a broken test file fails loudly instead of silently passing the guard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _is_config_entry_stub(node)
    ]


def _iter_test_files() -> list[tuple[Path, str]]:
    """Every ``tests/test_*.py`` file as ``(absolute path, repo-relative posix)``.

    Scoped to ``test_*.py`` so the factory (``tests/helpers/config_entries_stub.py``)
    and this guard file itself are excluded; ``__pycache__`` artifacts are skipped.
    """
    files: list[tuple[Path, str]] = []
    self_name = Path(__file__).name
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.name == self_name:
            continue
        rel = path.relative_to(_REPO_ROOT).as_posix()
        files.append((path, rel))
    return files


def test_no_new_or_grown_adhoc_config_entry_stubs() -> None:
    """Fail if any test file carries more ad-hoc config-entry stubs than frozen."""
    violations: list[str] = []
    for path, rel in _iter_test_files():
        offenders = _offending_stubs(path)
        frozen = LEGACY_ALLOWLIST.get(rel, 0)
        if len(offenders) > frozen:
            lines = ", ".join(str(lineno) for lineno in offenders)
            violations.append(
                f"File: {rel}\n"
                f"Frozen baseline: {frozen} ad-hoc config-entry stub(s)\n"
                f"Found now: {len(offenders)} on line(s) {lines}\n"
            )

    if violations:
        message = (
            "CONFIG-ENTRY STUB CONTRACT VIOLATION DETECTED!\n"
            "------------------------------------------------------------\n"
            + "\n".join(violations)
            + "WHY THIS IS WRONG:\n"
            "tests/AGENTS.md requires new tests to build config entries via\n"
            "tests.helpers.config_entries_stub.make_config_entry(...). Ad-hoc\n"
            "SimpleNamespace stubs that omit or null data/options bypass the\n"
            "factory's dict guarantee and are the structural cause of the\n"
            "AttributeError failures fixed in PR #172. The per-file baseline is\n"
            "a ratchet: it may shrink, never grow.\n\n"
            "HOW TO FIX:\n"
            "Replace the new SimpleNamespace stub with:\n"
            "    from tests.helpers.config_entries_stub import make_config_entry\n"
            "    entry = make_config_entry(entry_id=..., data={...}, options={...})\n"
            "(A config sub-entry stub -- carrying subentry_type/parent_entry_id/\n"
            "config_subentry_id -- is not flagged and needs no change.)\n"
            "------------------------------------------------------------\n"
        )
        pytest.fail(message)


def test_allowlist_has_no_stale_entries() -> None:
    """Frozen counts that vanished or now exceed the real count must be lowered."""
    offenders_by_file = {rel: _offending_stubs(path) for path, rel in _iter_test_files()}
    stale: list[str] = []
    for rel, frozen in sorted(LEGACY_ALLOWLIST.items()):
        if frozen < 1:
            stale.append(
                f"{rel}: frozen baseline {frozen} (< 1); a fully migrated file must "
                "be removed from the allowlist, not kept at zero"
            )
        elif rel not in offenders_by_file:
            stale.append(f"{rel}: file no longer exists")
        elif len(offenders_by_file[rel]) < frozen:
            stale.append(
                f"{rel}: frozen {frozen} but only {len(offenders_by_file[rel])} remain; "
                "lower the count (drop the entry once it reaches zero)"
            )
    assert not stale, (
        "Stale LEGACY_ALLOWLIST entries (files migrated toward make_config_entry "
        f"or removed); update the frozen counts to keep the backlog honest: {stale}"
    )
