# tests/test_guard_async_test_marker.py
"""Guard requiring an explicit asyncio marker on every async test.

``tests/AGENTS.md`` (Async tests) mandates that coroutine tests carry
``@pytest.mark.asyncio`` (or a module-level ``pytestmark = pytest.mark.asyncio``).
The repository runs ``asyncio_mode = "auto"``, which still *collects* unmarked
``async def test_*`` functions, so the omission passes silently today and only
surfaces in review. This guard fails loudly instead, mirroring the sibling
``test_no_asyncio_run_in_tests`` guard: the legacy files below are frozen as a
documented backlog, and no new violations may be added outside the allowlist.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Pre-existing files whose async tests rely solely on ``asyncio_mode = "auto"``
# without an explicit marker. Do not add new entries; mark new async tests
# instead. Shrinking this list (by adding markers) is always welcome.
LEGACY_ALLOWLIST: set[str] = {
    "tests/test_aas_token_retrieval.py",
    "tests/test_api_location_selection.py",
    "tests/test_coordinator_locate_basics.py",
    "tests/test_coordinator_polling_basics.py",
    "tests/test_coordinator_polling_branches.py",
    "tests/test_coordinator_snapshot.py",
    "tests/test_device_tracker.py",
    "tests/test_fcmpushclient_close_log_level.py",
    "tests/test_get_owner_key.py",
    "tests/test_last_seen_sensor_no_map_attributes.py",
    "tests/test_map_view_unique_id_resolution.py",
    "tests/test_nova_request.py",
    "tests/test_restart_required_issue.py",
    "tests/test_sensor_coverage_supplement.py",
    "tests/test_shared_key_retrieval.py",
}


def _references_asyncio_marker(node: ast.AST) -> bool:
    """Whether an expression refers to ``pytest.mark.asyncio``.

    Matches the precise ``.mark.asyncio`` attribute chain (covering the bare
    decorator, its call form, and list-valued ``pytestmark``) rather than any
    attribute named ``asyncio``, so an unrelated ``.asyncio`` symbol inside,
    say, a ``parametrize`` argument cannot mask an unmarked test.
    """

    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "asyncio"
            and isinstance(sub.value, ast.Attribute)
            and sub.value.attr == "mark"
        ):
            return True
    return False


def _has_module_asyncio_pytestmark(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in stmt.targets
        ) and _references_asyncio_marker(stmt.value):
            return True
    return False


def _unmarked_async_tests(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if _has_module_asyncio_pytestmark(tree):
        return []
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        if any(_references_asyncio_marker(deco) for deco in node.decorator_list):
            continue
        offenders.append((node.lineno, node.name))
    return offenders


def test_async_tests_declare_asyncio_marker() -> None:
    """Fail loudly if an async test lacks an explicit asyncio marker."""

    offenses: list[tuple[str, int, str]] = []
    tests_root = Path(__file__).parent
    # Anchor relative paths at the known ``tests/`` root instead of the volatile
    # process cwd, so the allowlist matches regardless of where pytest runs.
    repo_root = tests_root.parent
    for path in tests_root.rglob("*.py"):
        if path.name == Path(__file__).name:
            continue
        rel_path = path.relative_to(repo_root).as_posix()
        if rel_path in LEGACY_ALLOWLIST:
            continue
        for lineno, name in _unmarked_async_tests(path):
            offenses.append((rel_path, lineno, name))

    if offenses:
        lines = [
            f"File: {file}\nLine: {lineno}\nTest: {name}\n"
            for file, lineno, name in offenses
        ]
        message = (
            "ASYNC TEST CONTRACT VIOLATION DETECTED!\n"
            "------------------------------------------------------------\n"
            + "\n".join(lines)
            + "WHY THIS IS WRONG:\n"
            "tests/AGENTS.md requires every coroutine test to carry an explicit\n"
            "asyncio marker. asyncio_mode='auto' still collects unmarked tests, so\n"
            "the omission passes silently and only surfaces in review.\n\n"
            "HOW TO FIX:\n"
            "1. Add '@pytest.mark.asyncio' above the async test, or\n"
            "2. Add 'pytestmark = pytest.mark.asyncio' at module scope.\n"
            "------------------------------------------------------------\n"
        )
        pytest.fail(message)
