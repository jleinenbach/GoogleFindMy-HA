# tests/test_guard_import_time_module_stubs.py
"""Guard against replacing integration modules in ``sys.modules`` at import time.

A test module that writes a stub for ``custom_components.googlefindmy.<x>`` into
``sys.modules`` while it is being collected changes every test collected after
it, for the whole session. Whether the real module was already imported depends
only on collection order, so the damage shows up in a subset run and stays
hidden in the full run. Use ``monkeypatch.setitem(sys.modules, ...)`` inside a
test or fixture instead; it is undone when the test ends.

Seen, in every statement that runs on import (module level, ``if``/``with``/
``try``/``for``/``while``/``match`` bodies, class bodies), also in value
position (``x = sys.modules.setdefault(...)``, walrus):

* subscript assignment, ``sys.modules |= {...}``, ``sys.modules = {...}``,
  ``setdefault``, ``update``, ``__setitem__``;
* the aliases ``import sys as <name>``, ``from sys import modules [as <name>]``
  and a plain ``<name> = sys.modules`` at import time;
* a key that is not a string literal (variable, f-string, call) counts as a
  violation, because nothing proves it is not an integration module; the same
  holds for ``update`` with anything but a dict literal, including
  ``update(**stubs)``.

Not seen: writes inside a function that is called at import time, decorators
and default arguments of a ``def``, other alias forms (``s = sys``, walrus,
annotated or tuple assignment), and indirect access (``getattr(sys,
"modules")``, ``vars(sys)``, ``operator.setitem``, ``dict.__setitem__``,
``importlib``). Today no test file under ``tests/`` uses any of these forms.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

_PACKAGE = "custom_components.googlefindmy"
_KEYED_WRITES = frozenset({"setdefault", "__setitem__"})


def _import_time_statements(stmts: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements that run on import; function bodies do not."""

    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        yield stmt
        for field in ("body", "orelse", "handlers", "finalbody"):
            nested = getattr(stmt, field, None)
            if isinstance(nested, list):
                yield from _import_time_statements(nested)
        if isinstance(stmt, ast.Match):
            for case in stmt.cases:
                yield from _import_time_statements(case.body)


def _own_nodes(stmt: ast.stmt) -> Iterator[ast.AST]:
    """Yield the nodes of ``stmt`` without nested statements or lambdas."""

    pending: list[ast.AST] = [stmt]
    while pending:
        node = pending.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.stmt, ast.Lambda)):
                continue
            pending.append(child)


class _Aliases:
    """Names that refer to ``sys`` or to ``sys.modules`` in one file."""

    def __init__(self, statements: list[ast.stmt]) -> None:
        self.sys_names = {"sys"}
        self.modules_names: set[str] = set()
        for stmt in statements:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.name == "sys":
                        self.sys_names.add(alias.asname or "sys")
            elif isinstance(stmt, ast.ImportFrom) and stmt.module == "sys":
                for alias in stmt.names:
                    if alias.name == "modules":
                        self.modules_names.add(alias.asname or "modules")
        for stmt in statements:
            if isinstance(stmt, ast.Assign) and self.is_modules(stmt.value):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        self.modules_names.add(target.id)

    def is_modules(self, node: ast.expr) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.modules_names
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "modules"
            and isinstance(node.value, ast.Name)
            and node.value.id in self.sys_names
        )


def _key_is_suspect(key: ast.expr | None) -> bool:
    """A literal key names the package; any other key cannot be cleared."""

    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return _PACKAGE in key.value
    return True


def _mapping_is_suspect(value: ast.expr) -> bool:
    if isinstance(value, ast.Dict):
        return any(_key_is_suspect(key) for key in value.keys)
    return True


def _call_is_offense(node: ast.Call, aliases: _Aliases) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or not aliases.is_modules(func.value):
        return False
    if func.attr in _KEYED_WRITES:
        return _key_is_suspect(node.args[0] if node.args else None)
    if func.attr == "update":
        return any(_mapping_is_suspect(arg) for arg in node.args) or any(
            keyword.arg is None for keyword in node.keywords
        )
    return False


def _rebinds_sys_modules(node: ast.Assign | ast.AnnAssign, aliases: _Aliases) -> bool:
    """``sys.modules = {...}``; rebinding an alias name leaves the table alone."""

    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if not any(
        isinstance(target, ast.Attribute) and aliases.is_modules(target)
        for target in targets
    ):
        return False
    return node.value is None or _mapping_is_suspect(node.value)


def _is_offense(node: ast.AST, aliases: _Aliases) -> bool:
    if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
        return aliases.is_modules(node.value) and _key_is_suspect(node.slice)
    if isinstance(node, ast.AugAssign) and aliases.is_modules(node.target):
        return _mapping_is_suspect(node.value)
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        return _rebinds_sys_modules(node, aliases)
    if isinstance(node, ast.Call):
        return _call_is_offense(node, aliases)
    return False


def find_import_time_stubs(source: str) -> list[int]:
    """Return line numbers that write an integration module into ``sys.modules``."""

    statements = list(_import_time_statements(ast.parse(source).body))
    aliases = _Aliases(statements)
    offenses = {
        stmt.lineno
        for stmt in statements
        for node in _own_nodes(stmt)
        if _is_offense(node, aliases)
    }
    return sorted(offenses)


def test_no_test_module_stubs_integration_modules_at_import() -> None:
    """No test module may replace an integration module while it is collected."""

    tests_root = Path(__file__).parent
    offenses = [
        f"{path.relative_to(tests_root.parent).as_posix()}:{lineno}"
        for path in sorted(tests_root.rglob("*.py"))
        for lineno in find_import_time_stubs(path.read_text(encoding="utf-8"))
    ]
    assert not offenses, (
        "Test modules replace integration modules in sys.modules at import time; "
        "use monkeypatch.setitem(sys.modules, ...) in a test or fixture instead:\n"
        + "\n".join(offenses)
    )


def test_detector_sees_every_write_form() -> None:
    """Positive control: each write form at import time is reported."""

    name = f'"{_PACKAGE}.map_view"'
    lines = [
        "import sys",  # 1
        f"sys.modules[{name}] = object()",  # 2
        f"if {name} not in sys.modules:",  # 3
        f"    sys.modules[{name}] = object()",  # 4
        f"sys.modules.setdefault({name}, object())",  # 5
        f"sys.modules.update({{{name}: object()}})",  # 6
        "class Holder:",  # 7
        f"    sys.modules[{name}] = object()",  # 8
        "import sys as _s",  # 9
        f"_s.modules[{name}] = object()",  # 10
        "from sys import modules as mods",  # 11
        f"mods[{name}] = object()",  # 12
        "table = sys.modules",  # 13
        f"table[{name}] = object()",  # 14
        f"stub = sys.modules.setdefault({name}, object())",  # 15
        f"sys.modules |= {{{name}: object()}}",  # 16
        "for key in ('a',):",  # 17
        "    sys.modules[key] = object()",  # 18
        'sys.modules[f"custom_components.{key}"] = object()',  # 19
        "sys.modules.update(extra)",  # 20
        "match 1:",  # 21
        "    case 1:",  # 22
        f"        sys.modules[{name}] = object()",  # 23
        f"if (m := sys.modules.setdefault({name}, object())):",  # 24
        "    pass",  # 25
        "with open('x') as fh:",  # 26
        f"    sys.modules[{name}] = object()",  # 27
        "try:",  # 28
        f"    sys.modules[{name}] = object()",  # 29
        "finally:",  # 30
        f"    sys.modules[{name}] = object()",  # 31
        f"sys.modules.__setitem__({name}, object())",  # 32
        "if False:",  # 33
        "    pass",  # 34
        "else:",  # 35
        f"    sys.modules[{name}] = object()",  # 36
        "try:",  # 37
        "    pass",  # 38
        "except Exception:",  # 39
        f"    sys.modules[{name}] = object()",  # 40
        "sys.modules.update(**stubs)",  # 41
        f"sys.modules = {{{name}: object()}}",  # 42
    ]
    expected = [2, 4, 5, 6, 8, 10, 12, 14, 15, 16, 18, 19, 20, 23, 24, 27, 29]
    expected += [31, 32, 36, 40, 41, 42]
    assert find_import_time_stubs("\n".join(lines)) == expected


def test_detector_ignores_writes_inside_functions_and_other_packages() -> None:
    """Negative control: test-scoped writes and foreign literal keys stay allowed."""

    name = f'"{_PACKAGE}.map_view"'
    source = "\n".join(
        [
            "import sys",
            'sys.modules["homeassistant.components"] = object()',
            'sys.modules.setdefault("undetected_chromedriver", object())',
            'sys.modules.update({"homeassistant.helpers": object()})',
            "def _install():",
            f"    sys.modules[{name}] = object()",
            "async def _install_async():",
            f"    sys.modules.setdefault({name}, object())",
            f"install = lambda: sys.modules.setdefault({name}, object())",
            f"value = sys.modules[{name}]",
            "sys.modules.update(homeassistant_helpers=object())",
        ]
    )
    assert find_import_time_stubs(source) == []
