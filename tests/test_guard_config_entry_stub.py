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
guards: the legacy files below are frozen as a documented backlog, and no new
violations may be added outside the allowlist.

Detection is strict on purpose: an offender is a ``SimpleNamespace`` call that
carries ``entry_id`` *and* at least one of ``data``/``options`` -- the exact
shape the rule names. Sub-entry stubs that carry ``entry_id`` without
``data``/``options`` are intentionally not flagged.

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

# Pre-existing test files that build config entries via ad-hoc ``SimpleNamespace``
# stubs and predate this guard. Do not add new entries; use ``make_config_entry``
# in new tests instead. Shrinking this list (by migrating call sites to the
# factory, as the wave-based migration in tests/AGENTS.md invites) is welcome.
LEGACY_ALLOWLIST: set[str] = {
    "tests/test_config_flow.py",
    "tests/test_config_flow_basic.py",
    "tests/test_config_flow_basics.py",
    "tests/test_config_flow_initial_auth.py",
    "tests/test_coordinator_device_registry.py",
    "tests/test_coordinator_subentry_repair.py",
    "tests/test_coordinator_subentry_visibility.py",
    "tests/test_coordinator_timeout.py",
    "tests/test_coordinator_visibility_availability.py",
    "tests/test_device_identifier_normalization.py",
    "tests/test_device_tracker.py",
    "tests/test_eid_data_flow.py",
    "tests/test_init_basics.py",
    "tests/test_metadata_helpers.py",
    "tests/test_poll_timeout_hardening.py",
    "tests/test_services_refresh_device_urls.py",
    "tests/test_spot_grpc_client.py",
    "tests/test_subentry_manager_registry_resolution.py",
    "tests/test_subentry_setup_trigger.py",
    "tests/test_transient_owner_key_propagation.py",
    "tests/test_unload_subentry_cleanup.py",
}


def _is_config_entry_stub(call: ast.Call) -> bool:
    """Whether a call is an ad-hoc ``SimpleNamespace`` config-entry stub.

    Strict shape: the callee resolves to ``SimpleNamespace`` (both the bare
    ``SimpleNamespace(...)`` and the attribute form ``types.SimpleNamespace(...)``)
    and the keyword set carries ``entry_id`` together with at least one of
    ``data``/``options`` -- the exact construction tests/AGENTS.md forbids. A
    bare ``entry_id`` without ``data``/``options`` (e.g. a sub-entry stub) is
    deliberately not flagged.
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
    return "entry_id" in keywords and ("data" in keywords or "options" in keywords)


def _offending_stubs(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, construct)`` for every ad-hoc config-entry stub in ``path``.

    ``ast.parse`` deliberately propagates ``SyntaxError`` rather than swallowing
    it, so a broken test file fails loudly instead of silently passing the guard.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_config_entry_stub(node):
            offenders.append((node.lineno, "SimpleNamespace(entry_id=, data=/options=)"))
    return offenders


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


def test_new_tests_use_make_config_entry_factory() -> None:
    """Fail loudly if a non-allowlisted test builds an ad-hoc config-entry stub."""
    offenses: list[tuple[str, int, str]] = []
    for path, rel in _iter_test_files():
        if rel in LEGACY_ALLOWLIST:
            continue
        for lineno, construct in _offending_stubs(path):
            offenses.append((rel, lineno, construct))

    if offenses:
        lines = [
            f"File: {file}\nLine: {lineno}\nConstruct: {construct}\n"
            for file, lineno, construct in offenses
        ]
        message = (
            "CONFIG-ENTRY STUB CONTRACT VIOLATION DETECTED!\n"
            "------------------------------------------------------------\n"
            + "\n".join(lines)
            + "WHY THIS IS WRONG:\n"
            "tests/AGENTS.md requires new tests to build config entries via\n"
            "tests.helpers.config_entries_stub.make_config_entry(...). Ad-hoc\n"
            "SimpleNamespace stubs that omit or null data/options bypass the\n"
            "factory's dict guarantee and are the structural cause of the\n"
            "AttributeError failures fixed in PR #172.\n\n"
            "HOW TO FIX:\n"
            "Replace the SimpleNamespace stub with:\n"
            "    from tests.helpers.config_entries_stub import make_config_entry\n"
            "    entry = make_config_entry(entry_id=..., data={...}, options={...})\n"
            "------------------------------------------------------------\n"
        )
        pytest.fail(message)


def test_allowlist_has_no_stale_entries() -> None:
    """Allowlisted files that vanished or lost the offender shape must leave the list."""
    known = {rel for _, rel in _iter_test_files()}
    stale = sorted(
        rel
        for rel in LEGACY_ALLOWLIST
        if rel not in known or not _offending_stubs(_REPO_ROOT / rel)
    )
    assert not stale, (
        "Stale LEGACY_ALLOWLIST entries (file migrated to make_config_entry or no "
        f"longer exists); remove them to keep the backlog honest: {stale}"
    )
