# tests/test_guard_phone_action_numbers.py
"""Guard: no code outside the generated protobuf modules names a forbidden action.

Phone actions 2, 6, 7, 13 and 14 have no observed meaning and may lock or wipe the
phone (static layer of the action whitelist). The phone package also never names the tracker actions 30, 31
and 32 (``locateTracker``, ``startSound``, ``stopSound``). The guard reads the
syntax tree of every module of the integration except ``*_pb2*``. A name counts
wherever the syntax tree can carry it: an attribute, a keyword argument, or a word
inside a string constant (``getattr`` with a literal, a ``text_format`` or JSON
text, a dict key). An f-string that builds a ``phoneActionField`` name from a
formatted value counts as every phone action, because the number cannot be read.
Docstrings are skipped: they describe the rule, they do not build a request.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"
PHONE_PACKAGE = PACKAGE / "NovaApi" / "ExecuteAction" / "LocatePhone"

FORBIDDEN_PHONE_ACTIONS = frozenset(f"phoneActionField{n}" for n in (2, 6, 7, 13, 14))
TRACKER_ACTIONS = frozenset({"locateTracker", "startSound", "stopSound"})


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DYNAMIC_PHONE_ACTION = "phoneActionField{}"
ALL_PHONE_ACTION_NAMES = frozenset(f"phoneActionField{n}" for n in range(1, 64))


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _names(source: str) -> set[str]:
    tree = ast.parse(source)
    skip = _docstrings(tree)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            names.add(node.arg)
        elif isinstance(node, ast.JoinedStr):
            parts = node.values
            for left, right in zip(parts, parts[1:], strict=False):
                if (
                    isinstance(left, ast.Constant)
                    and isinstance(left.value, str)
                    and left.value.endswith("phoneActionField")
                    and isinstance(right, ast.FormattedValue)
                ):
                    names.add(_DYNAMIC_PHONE_ACTION)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ):
            names.update(_WORD.findall(node.value))
    if _DYNAMIC_PHONE_ACTION in names:
        names |= ALL_PHONE_ACTION_NAMES
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


def test_the_guard_sees_every_form() -> None:
    """Self-test: each form that can carry a forbidden name is found."""
    forms = {
        "req.action.phoneActionField2.SetInParent()": "phoneActionField2",
        "getattr(a, 'phoneActionField6')": "phoneActionField6",
        "pb2.ExecuteActionType(phoneActionField7=x)": "phoneActionField7",
        "text_format.Parse('phoneActionField13 {}', m)": "phoneActionField13",
        "json_format.ParseDict({'phoneActionField14': {}}, m)": "phoneActionField14",
        "getattr(a, f'phoneActionField{n}')": "phoneActionField2",
        "Action(locateTracker=x)": "locateTracker",
    }
    for source, name in forms.items():
        assert name in _names(source), source


def test_a_docstring_does_not_count() -> None:
    assert "phoneActionField2" not in _names('"""Never send phoneActionField2."""')


def test_the_guard_sees_both_forms() -> None:
    """Self-test: an attribute and a getattr literal are both found."""
    planted = (
        "req.action.phoneActionField2.SetInParent()\ngetattr(a, 'phoneActionField13')"
    )
    assert _names(planted) & FORBIDDEN_PHONE_ACTIONS == {
        "phoneActionField2",
        "phoneActionField13",
    }
