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

* **Per-file fingerprint ratchet, not a per-file count.** ``LEGACY_ALLOWLIST``
  maps each pre-existing offender file to the multiset of *content
  fingerprints* of the ad-hoc stubs it currently carries (one fingerprint per
  stub; identical stubs repeat). The current multiset may only be a
  sub-multiset of the frozen one. A bare per-file *count* would be defeated by
  a one-for-one swap inside a single file -- migrate one legacy stub to
  ``make_config_entry`` and add a different ad-hoc stub elsewhere in the same
  file, and the count stays equal while a fresh violation slips in. Keying by
  fingerprint catches that: the new stub's fingerprint is absent from the
  frozen multiset (main guard fails) and the migrated stub's fingerprint has
  vanished (stale self-test fails), so the frozen list must track every
  individual construction, never just their number.

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
One residual blind spot follows from the fingerprint being content-based: if a
stub is migrated away and a byte-identical copy of it is re-added in the same
file, the multiset is unchanged and the swap is not flagged -- an acceptable
corner, since the re-added construction is the very one already tolerated.

Scope note: this guard covers ``tests/test_*.py`` only, matching the rule's
"New tests" addressee and excluding the factory itself
(``tests/helpers/config_entries_stub.py``), which builds the very shape it
provides.

Regenerating the baseline (after an intentional, reviewed migration): collect
``_fingerprint(node)`` for every offending stub per file with the predicate
below; never hand-edit individual fingerprints.
"""

from __future__ import annotations

import ast
import hashlib
from collections import Counter
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
# a multiset of content fingerprints: ``repo-relative path -> [fingerprint, ...]``
# (one entry per stub; identical stubs repeat). The current multiset may only be
# a sub-multiset of the frozen one. Adding a stub introduces a fingerprint absent
# from the frozen list and fails the main guard; migrating a stub removes its
# fingerprint and the stale self-test requires the frozen list to follow (drop
# the file once it reaches zero). Do not add fingerprints or files; use
# ``make_config_entry`` in new tests instead. The baseline is regenerated
# programmatically (subentry-excluded predicate), never hand-edited.
LEGACY_ALLOWLIST: dict[str, list[str]] = {
    "tests/test_config_flow.py": ["b393a469b2ac84dd"],
    "tests/test_config_flow_basic.py": ["109d00756f09ca13", "bdd7ebb3b879b2f2"],
    "tests/test_config_flow_basics.py": [
        "21d86f4176312ada",
        "2c6fcd65ecc5bab3",
        "6222527e4597b2eb",
        "8626c937b96b42c5",
        "8819bb7ce5cd90b0",
        "f3aa1597f46bc215",
    ],
    "tests/test_config_flow_initial_auth.py": [
        "6e8e460fa3b657c4",
        "d56997d75a716553",
        "d56997d75a716553",
    ],
    "tests/test_coordinator_device_registry.py": [
        "28b6c6b177b0aa47",
        "3d372c3756d326a4",
        "6083199e05528d7f",
        "ab34f8b764396ec8",
        "bb5e79636e2492c0",
        "c9850eadba79f857",
        "c9850eadba79f857",
    ],
    "tests/test_coordinator_subentry_repair.py": ["8141af41d52e782f"],
    "tests/test_coordinator_subentry_visibility.py": [
        "16f460785453411e",
        "f2dbf2c324fa68bf",
    ],
    "tests/test_coordinator_timeout.py": [
        "06d43e206795eb3d",
        "06d43e206795eb3d",
        "17c69b7598ac3b5d",
    ],
    "tests/test_coordinator_visibility_availability.py": [
        "3a256b9c1d0eecb6",
        "85c8582cb1e77080",
        "e0ebcc5ff3fe757d",
    ],
    "tests/test_device_identifier_normalization.py": [
        "2f0cdd8e390a910a",
        "abc95eedd76db511",
    ],
    "tests/test_device_tracker.py": ["7fef2a47acaceb73", "bbd287278e1d123d"],
    "tests/test_eid_data_flow.py": ["933ec79bfd9f3907"],
    "tests/test_init_basics.py": ["e888f3e6bb63d425"],
    "tests/test_metadata_helpers.py": ["b123536351d48ee4"],
    "tests/test_poll_timeout_hardening.py": ["06d43e206795eb3d"],
    "tests/test_services_refresh_device_urls.py": [
        "57bba74fc930d271",
        "be0b655793dddf26",
        "be0b655793dddf26",
        "be0b655793dddf26",
    ],
    "tests/test_spot_grpc_client.py": ["17c69b7598ac3b5d"],
    "tests/test_transient_owner_key_propagation.py": ["06d43e206795eb3d"],
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


def _fingerprint(call: ast.Call) -> str:
    """Stable content fingerprint of one offending stub construction.

    Computed over the normalised source of the call node: ``ast.unparse``
    collapses whitespace and formatting, so reformatting an untouched stub
    leaves the fingerprint unchanged, while any change to the keyword names or
    literal values produces a different one. SHA-256 (not SHA-1/MD5) keeps
    ``bandit`` quiet; the digest is an identity key, never a security primitive.
    A 64-bit prefix makes accidental collisions between distinct stubs in one
    file negligible for the tens of constructions tracked here.
    """
    return hashlib.sha256(ast.unparse(call).encode("utf-8")).hexdigest()[:16]


def _offending_stubs(path: Path) -> list[tuple[int, str]]:
    """Return ``(line number, fingerprint)`` for every ad-hoc stub in ``path``.

    ``ast.parse`` deliberately propagates ``SyntaxError`` rather than swallowing
    it, so a broken test file fails loudly instead of silently passing the guard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        (node.lineno, _fingerprint(node))
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
    """Fail if any test file carries a config-entry stub absent from its baseline."""
    violations: list[str] = []
    for path, rel in _iter_test_files():
        offenders = _offending_stubs(path)
        current = Counter(fp for _, fp in offenders)
        frozen = Counter(LEGACY_ALLOWLIST.get(rel, []))
        excess = current - frozen  # fingerprints present more often than frozen
        if not excess:
            continue
        new_lines = sorted(lineno for lineno, fp in offenders if excess.get(fp))
        lines = ", ".join(str(lineno) for lineno in new_lines)
        violations.append(
            f"File: {rel}\n"
            f"Frozen baseline: {sum(frozen.values())} ad-hoc config-entry stub(s)\n"
            f"New or grown ad-hoc stub(s) on line(s): {lines}\n"
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
            "AttributeError failures fixed in PR #172. The frozen baseline is a\n"
            "per-stub fingerprint multiset: it may shrink, never grow, and a\n"
            "one-for-one swap inside a file is caught because the new stub's\n"
            "fingerprint is not in the frozen list.\n\n"
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
    """Frozen fingerprints that vanished or shrank must be removed/lowered."""
    current_by_file = {
        rel: Counter(fp for _, fp in _offending_stubs(path))
        for path, rel in _iter_test_files()
    }
    stale: list[str] = []
    for rel, fingerprints in sorted(LEGACY_ALLOWLIST.items()):
        if not fingerprints:
            stale.append(
                f"{rel}: empty frozen list; a fully migrated file must be removed "
                "from the allowlist, not kept empty"
            )
            continue
        if rel not in current_by_file:
            stale.append(f"{rel}: file no longer exists")
            continue
        missing = Counter(fingerprints) - current_by_file[rel]
        if missing:
            gone = ", ".join(sorted(missing.elements()))
            stale.append(
                f"{rel}: frozen fingerprint(s) {gone} no longer present "
                "(migrated or changed); update the frozen list to match"
            )
    assert not stale, (
        "Stale LEGACY_ALLOWLIST entries (files migrated toward make_config_entry "
        f"or removed); update the frozen fingerprints to keep the backlog honest: {stale}"
    )
