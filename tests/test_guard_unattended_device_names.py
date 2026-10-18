# tests/test_guard_unattended_device_names.py
"""Guard: three unattended paths name no device above DEBUG.

`AGENTS.md` section 5 (b): a record that can repeat unattended keeps the
user-chosen device name out of the record at INFO and above and carries it
only at DEBUG. This guard pins that for three functions that run on every
poll cycle and used to log the display name at WARNING or ERROR:

* `api.py` `GoogleFindMyAPI.async_get_device_location` (the location
  lookup shared by the poll loop and the manual locate);
* `ProtoDecoders/decoder.py` `get_devices_with_location` (the device list
  decoder, called twice per poll);
* `coordinator/registry.py` `_heal_tracker_device_subentry`, nested in
  `RegistryOperations._ensure_registry_for_devices` (the registry sync).

Inside each function, every log call whose level is INFO or above is
checked: all positional arguments, including the format argument with an
f-string or the right side of a `%` format, and all keyword values. An
offender is any leaf below them that is a name, an attribute (other than
`__name__`) or a subscript with a string key whose spelling contains `name`
or `label`, in any case. The three field-name records of the missing-key
dump in the decoder log protobuf field names (`f.name`), not device names;
they are listed in `_FIELD_NAME_ROWS` with path, leaf and format prefix, and
each entry must match exactly one call so that a copied line does not
inherit the exception.

Written as a pin on three functions rather than a package-wide rule,
because the package still has records bound to one invocation (buttons,
services, the manual locate) that may name the device by section 5 (b), and
a package-wide name predicate would need an exception list longer than the
rule.

Blind spots, declared: a value copied into a variable whose name has neither
`name` nor `label` (`fields = ...` in the decoder is such a case and is
correct), a device object rendered through `str()`, a logger method bound to
a local name (the receiver rule needs `LOG` in the receiver), an exception
whose own text carries the name, and records in helpers these functions
call. The caplog tests in `tests/test_api_location_name_at_debug.py` and
`tests/test_decoder_registry_name_at_debug.py` pin the record contents;
this guard pins the shape of the call.

Retire this guard when a package-wide device-name guard covers these three
functions.
"""

from __future__ import annotations

import ast
from pathlib import Path

from custom_components import googlefindmy

_PACKAGE_ROOT = Path(googlefindmy.__file__).resolve().parent

_LEVELS_ABOVE_DEBUG = frozenset(
    {"info", "warning", "warn", "error", "exception", "critical", "log"}
)

# (path relative to the package, qualified name, minimum number of log calls
# above DEBUG measured on 2026-10-18). The minimum is the vacuum guard: a
# function that shrinks below it was renamed, moved or rewritten, and the
# guard would no longer see what it was written for.
_UNATTENDED_FUNCTIONS: tuple[tuple[str, str, int], ...] = (
    ("api.py", "GoogleFindMyAPI.async_get_device_location", 11),
    ("ProtoDecoders/decoder.py", "get_devices_with_location", 7),
    (
        "coordinator/registry.py",
        "RegistryOperations._ensure_registry_for_devices._heal_tracker_device_subentry",
        1,
    ),
)

# Records that log protobuf field names, reviewed on 2026-10-18
# (PLAN_GFMY_LOG_NAME_REST AP1, class C).
_FIELD_NAME_ROWS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("ProtoDecoders/decoder.py", "f.name", " -> Info fields"),
        ("ProtoDecoders/decoder.py", "f.name", " -> Registration fields"),
        ("ProtoDecoders/decoder.py", "f.name", " -> Secrets fields"),
    }
)

Offender = tuple[str, int, str, str]


def _find_function(
    tree: ast.Module, qualname: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the definition reached by following ``qualname`` from the module."""
    scope: ast.AST = tree
    found: ast.AST | None = None
    for part in qualname.split("."):
        found = None
        for node in ast.walk(scope):
            if node is scope:
                continue
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == part
            ):
                found = node
                break
        if found is None:
            return None
        scope = found
    if isinstance(found, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return found
    return None


def _is_log_call_above_debug(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LEVELS_ABOVE_DEBUG:
        return False
    return "LOG" in ast.unparse(func.value).upper()


def _spells_a_name(text: str) -> bool:
    lowered = text.lower()
    return "name" in lowered or "label" in lowered


def _name_leaves(node: ast.AST) -> set[str]:
    """Leaves under ``node`` whose spelling contains ``name`` or ``label``."""
    found: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and _spells_a_name(child.id):
            found.add(child.id)
        elif (
            isinstance(child, ast.Attribute)
            and child.attr != "__name__"
            and _spells_a_name(child.attr)
        ):
            found.add(ast.unparse(child))
        elif (
            isinstance(child, ast.Subscript)
            and isinstance(child.slice, ast.Constant)
            and isinstance(child.slice.value, str)
            and _spells_a_name(child.slice.value)
        ):
            found.add(ast.unparse(child))
    return found


def _format_prefix(node: ast.Call) -> str:
    # `.log(level, msg, ...)` carries the format string at index 1.
    index = 1 if isinstance(node.func, ast.Attribute) and node.func.attr == "log" else 0
    first = node.args[index] if len(node.args) > index else None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return ""


def _is_excepted(
    rows: frozenset[tuple[str, str, str]], relative: str, leaf: str, fmt: str
) -> bool:
    # An empty prefix would excuse every call of the function.
    assert all(prefix for _path, _leaf, prefix in rows), "empty prefix in exceptions"
    return any(
        relative == path and leaf == name and fmt.startswith(prefix)
        for path, name, prefix in rows
    )


def scan(
    rows: frozenset[tuple[str, str, str]] = _FIELD_NAME_ROWS,
) -> tuple[list[Offender], dict[str, int]]:
    """Return ((path, line, leaf, format) offenders, log calls per function)."""
    offenders: list[Offender] = []
    counts: dict[str, int] = {}
    for relative, qualname, _minimum in _UNATTENDED_FUNCTIONS:
        path = _PACKAGE_ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        function = _find_function(tree, qualname)
        assert function is not None, f"{relative}: {qualname} not found"
        counts[qualname] = 0
        for node in ast.walk(function):
            if not isinstance(node, ast.Call) or not _is_log_call_above_debug(node):
                continue
            counts[qualname] += 1
            fmt = _format_prefix(node)
            leaves: set[str] = set()
            for argument in [*node.args, *(kw.value for kw in node.keywords)]:
                leaves |= _name_leaves(argument)
            for leaf in sorted(leaves):
                if _is_excepted(rows, relative, leaf, fmt):
                    continue
                offenders.append((relative, node.lineno, leaf, fmt))
    return offenders, counts


def test_functions_are_found() -> None:
    _offenders, counts = scan()
    for _relative, qualname, minimum in _UNATTENDED_FUNCTIONS:
        assert counts[qualname] >= minimum, (
            f"{qualname}: only {counts[qualname]} log calls above DEBUG, expected "
            f"at least {minimum}; the guard would no longer see the records it "
            "was written for"
        )


def test_no_device_name_above_debug() -> None:
    offenders, _counts = scan()
    assert offenders == [], (
        "Log calls above DEBUG on an unattended path name a device (AGENTS.md "
        "section 5 b): name the device by a class (a)/(b) identifier and log the "
        "display name in a DEBUG companion record:\n"
        + "\n".join(
            f"OFFENDER {path}:{line} ({leaf})" for path, line, leaf, _fmt in offenders
        )
    )


def test_exception_rows_match_exactly_one_call() -> None:
    """Each field-name row excuses one vetted call, no more, no less."""
    offenders, _counts = scan(rows=frozenset())
    matches = {
        row: [
            (path, line)
            for path, line, leaf, fmt in offenders
            if _is_excepted(frozenset({row}), path, leaf, fmt)
        ]
        for row in _FIELD_NAME_ROWS
    }
    assert all(len(found) == 1 for found in matches.values()), matches
