# tests/test_guard_phone_action_numbers.py
"""Guard: no code outside the generated protobuf modules names a forbidden action.

Phone actions 2, 6, 7, 13 and 14 have no observed meaning and may lock or wipe the
phone (static layer of the action whitelist). The phone package also never names the tracker actions 30, 31
and 32 (``locateTracker``, ``startSound``, ``stopSound``). The guard reads the
syntax tree of every module of the integration except ``*_pb2*``: attribute names
and string constants (``getattr`` with a literal) both count.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"
PHONE_PACKAGE = PACKAGE / "NovaApi" / "ExecuteAction" / "LocatePhone"

FORBIDDEN_PHONE_ACTIONS = frozenset(f"phoneActionField{n}" for n in (2, 6, 7, 13, 14))
TRACKER_ACTIONS = frozenset({"locateTracker", "startSound", "stopSound"})


def _names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            names.add(node.value)
    return names


def _modules(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "_pb2" not in p.name)


def _offenses(root: Path, forbidden: frozenset[str]) -> list[str]:
    return [
        f"{path.relative_to(PACKAGE)}: {name}"
        for path in _modules(root)
        for name in sorted(_names(path.read_text(encoding="utf-8")) & forbidden)
    ]


def test_no_module_names_a_forbidden_phone_action() -> None:
    assert _offenses(PACKAGE, FORBIDDEN_PHONE_ACTIONS) == []


def test_the_phone_package_never_names_a_tracker_action() -> None:
    assert _modules(PHONE_PACKAGE)
    assert _offenses(PHONE_PACKAGE, TRACKER_ACTIONS) == []


def test_the_guard_sees_both_forms() -> None:
    """Self-test: an attribute and a getattr literal are both found."""
    planted = (
        "req.action.phoneActionField2.SetInParent()\ngetattr(a, 'phoneActionField13')"
    )
    assert _names(planted) & FORBIDDEN_PHONE_ACTIONS == {
        "phoneActionField2",
        "phoneActionField13",
    }
