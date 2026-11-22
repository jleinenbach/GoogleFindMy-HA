# tests/test_owner_key_version_catch_sites.py
"""Every decision catch site handles the owner-key version gap.

``OwnerKeyVersionUnavailableError`` is a ``DecryptionError`` subclass. A catch
site that decides between "per tracker" and "account-wide" must catch the gap in
its own clause BEFORE that decision; otherwise the gap falls through to the
per-tracker or the account-wide branch and drives a reauth path (issue #246).

How a clause type is read: the expression of ``ast.ExceptHandler.type`` is
resolved to the watched classes it names. Resolved are a bare name, a name bound
by ``from … import X as Y`` or at module level by ``Y = X`` / ``Y = (X, Z)``,
``A = B = X``, ``Y: Final = X`` or ``A, B = X, Z``, an attribute (``module.X``,
resolved by its last part), a tuple or list of these (starred elements
included), a PEP 604 union ``X | Z`` and a walrus ``(Y := X)``.

Rules, per ``try``:

1. A clause that catches ``StaleOwnerKeyError`` without ``DecryptionError``, or
   whose body tests ``isinstance(…, StaleOwnerKeyError)`` or
   ``type(…) is`` / ``==`` / ``in`` ``StaleOwnerKeyError``, is a decision site and
   needs a preceding clause that catches ``OwnerKeyVersionUnavailableError``.
2. A clause that catches ``DecryptionError`` (alone or in a tuple) without a
   preceding gap clause hands the class on unchanged. Such pass-through clauses
   are pinned per file, so a new one, for example a decision site that only
   catches ``DecryptionError``, turns the pin red until it is classified.

Declared blind spots (not resolved, therefore neither flagged nor counted): a
clause type built by a call or a subscript (``except errors()``,
``except ERRORS[0]``), a name bound inside a function or a class
(``except self.ERRORS``), a name imported with ``import module`` and re-bound
later, a ``match`` statement on the exception, and an ``isinstance`` reached
through an alias of ``isinstance`` or a helper function.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"
_STALE = "StaleOwnerKeyError"
_GAP = "OwnerKeyVersionUnavailableError"
_DECRYPTION = "DecryptionError"
_WATCHED = frozenset({_STALE, _GAP, _DECRYPTION})
# Clauses that catch ``DecryptionError`` without a gap clause before them; each
# re-raises or stores the error for its caller unchanged (measured 2026-11-22).
_EXPECTED_PASSTHROUGH = {
    "NovaApi/ExecuteAction/LocateTracker/location_request.py": 2,
    "api.py": 1,
}


def _bindings(tree: ast.Module) -> dict[str, frozenset[str]]:
    """Map module-level names to the watched classes they stand for."""
    names: dict[str, frozenset[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in _WATCHED:
                    names[alias.asname or alias.name] = frozenset({alias.name})
        elif isinstance(node, ast.Assign):
            for target in node.targets:  # ``A = B = X`` binds both
                _bind(target, node.value, names)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            _bind(node.target, node.value, names)  # ``Y: Final = X``
    return names


def _bind(target: ast.expr, value: ast.expr, names: dict[str, frozenset[str]]) -> None:
    if isinstance(target, ast.Name):
        resolved = _resolve(value, names)
        if resolved:
            names[target.id] = resolved
    elif (
        isinstance(target, ast.Tuple | ast.List)
        and isinstance(value, ast.Tuple | ast.List)
        and len(target.elts) == len(value.elts)
    ):
        for inner_target, inner_value in zip(target.elts, value.elts, strict=True):
            _bind(inner_target, inner_value, names)


def _resolve(expr: ast.expr | None, names: dict[str, frozenset[str]]) -> frozenset[str]:
    """Return the watched classes an exception-type expression names."""
    if isinstance(expr, ast.Name):
        return names.get(expr.id, frozenset({expr.id}) & _WATCHED)
    if isinstance(expr, ast.Attribute):
        return frozenset({expr.attr}) & _WATCHED
    if isinstance(expr, ast.Tuple | ast.List):
        out: frozenset[str] = frozenset()
        for elt in expr.elts:
            inner = elt.value if isinstance(elt, ast.Starred) else elt
            out |= _resolve(inner, names)
        return out
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.BitOr):
        return _resolve(expr.left, names) | _resolve(expr.right, names)
    if isinstance(expr, ast.NamedExpr):
        return _resolve(expr.value, names)
    return frozenset()


def _body_tests_stale(
    handler: ast.ExceptHandler, names: dict[str, frozenset[str]]
) -> bool:
    return any(
        _is_isinstance_stale(node, names) or _is_type_compare_stale(node, names)
        for node in ast.walk(handler)
    )


def _is_isinstance_stale(node: ast.AST, names: dict[str, frozenset[str]]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "isinstance"
        and len(node.args) == 2
        and _STALE in _resolve(node.args[1], names)
    )


def _is_type_compare_stale(node: ast.AST, names: dict[str, frozenset[str]]) -> bool:
    """``type(err) is S``, ``type(err) == S`` or ``type(err) in (S, …)``."""
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == "type"
        and any(isinstance(op, ast.Is | ast.Eq | ast.In) for op in node.ops)
        and any(_STALE in _resolve(c, names) for c in node.comparators)
    )


@dataclass
class _Scan:
    violations: list[str] = field(default_factory=list)
    stale_clauses: int = 0
    passthrough: int = 0


def _scan(source: str, label: str) -> _Scan:
    tree = ast.parse(source)
    names = _bindings(tree)
    result = _Scan()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try | ast.TryStar):
            continue
        seen_gap = False
        for handler in node.handlers:
            caught = _resolve(handler.type, names)
            if _GAP in caught:
                seen_gap = True
                continue
            stale_clause = _STALE in caught and _DECRYPTION not in caught
            result.stale_clauses += stale_clause
            if stale_clause or _body_tests_stale(handler, names):
                if not seen_gap:
                    result.violations.append(f"{label}:{handler.lineno}")
            elif _DECRYPTION in caught and not seen_gap:
                result.passthrough += 1
    return result


def _violations(source: str, label: str) -> tuple[list[str], int]:
    """Return (violations, number of decision clauses naming the stale class)."""
    found = _scan(source, label)
    return found.violations, found.stale_clauses


def _package_sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(_ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(_ROOT.rglob("*.py"))
    ]


def test_every_stale_catch_is_preceded_by_gap_catch() -> None:
    """Each decision clause has a gap clause before it."""
    violations: list[str] = []
    stale_total = 0
    for label, source in _package_sources():
        found, stale = _violations(source, label)
        violations.extend(found)
        stale_total += stale

    assert violations == []
    # Not vacuous: the three decision sites (poll, manual locate, push) exist.
    assert stale_total == 3


def test_guard_positive_control_flags_missing_gap_clause() -> None:
    """Positive control: a decision site without the gap clause is reported."""
    source = (
        "try:\n"
        "    work()\n"
        "except StaleOwnerKeyError as err:\n"
        "    handle(err)\n"
        "except DecryptionError as err:\n"
        "    escalate(err)\n"
    )
    violations, stale = _violations(source, "sample.py")
    assert violations == ["sample.py:3"]
    assert stale == 1

    # A gap clause AFTER the stale clause does not count either.
    late = source.replace(
        "except DecryptionError", "except OwnerKeyVersionUnavailableError"
    )
    assert _violations(late, "late.py")[0] == ["late.py:3"]


_GAP_FIRST = "except OwnerKeyVersionUnavailableError as gap:\n    note(gap)\n"

# Each form names the stale class in a way the bare-name rule missed; every one
# must be reported without the gap clause and pass with it.
_DECISION_FORMS = {
    "attribute": ("", "except dl.StaleOwnerKeyError as err:\n    handle(err)\n"),
    "import_alias": (
        "from x import StaleOwnerKeyError as S\n",
        "except S as err:\n    handle(err)\n",
    ),
    "module_constant": (
        "from x import StaleOwnerKeyError\nPER_TRACKER = (StaleOwnerKeyError, KeyError)\n",
        "except PER_TRACKER as err:\n    handle(err)\n",
    ),
    "tuple_without_decryption": (
        "",
        "except (StaleOwnerKeyError, ValueError) as err:\n    handle(err)\n",
    ),
    "starred_tuple": (
        "",
        "except (*OTHERS, StaleOwnerKeyError) as err:\n    handle(err)\n",
    ),
    "isinstance_in_decryption_clause": (
        "",
        "except DecryptionError as err:\n"
        "    if isinstance(err, StaleOwnerKeyError):\n"
        "        per_tracker(err)\n"
        "    else:\n"
        "        escalate(err)\n",
    ),
    "final_constant": (
        "from x import StaleOwnerKeyError\nPER: Final = (StaleOwnerKeyError, KeyError)\n",
        "except PER as err:\n    handle(err)\n",
    ),
    "chained_constant": (
        "from x import StaleOwnerKeyError\nA = B = (StaleOwnerKeyError, KeyError)\n",
        "except B as err:\n    handle(err)\n",
    ),
    "unpacked_constant": (
        "from x import StaleOwnerKeyError\nA, B = StaleOwnerKeyError, KeyError\n",
        "except A as err:\n    handle(err)\n",
    ),
    "pep604_union_in_isinstance": (
        "",
        "except Exception as err:\n"
        "    if isinstance(err, KeyError | StaleOwnerKeyError):\n"
        "        per_tracker(err)\n",
    ),
    "type_is_in_body": (
        "",
        "except Exception as err:\n"
        "    if type(err) is StaleOwnerKeyError:\n"
        "        per_tracker(err)\n",
    ),
    "walrus_clause_type": (
        "",
        "except (E := StaleOwnerKeyError) as err:\n    handle(err)\n",
    ),
    "isinstance_with_tuple_and_alias": (
        "from x import StaleOwnerKeyError as S\n",
        "except Exception as err:\n"
        "    if isinstance(err, (KeyError, S)):\n"
        "        per_tracker(err)\n",
    ),
}


@pytest.mark.parametrize("form", sorted(_DECISION_FORMS))
def test_guard_flags_every_decision_form(form: str) -> None:
    """Every form is a decision site: red without the gap clause, green with it."""
    head, clause = _DECISION_FORMS[form]
    bare = f"{head}try:\n    work()\n{clause}"
    assert _violations(bare, "f.py")[0] == [f"f.py:{head.count(chr(10)) + 3}"]

    guarded = f"{head}try:\n    work()\n{_GAP_FIRST}{clause}"
    assert _violations(guarded, "f.py")[0] == []


def test_guard_gap_clause_by_alias_or_attribute_counts() -> None:
    """The preceding gap clause is resolved the same way as the stale clause."""
    source = (
        "from x import OwnerKeyVersionUnavailableError as Gap\n"
        "try:\n"
        "    work()\n"
        "except Gap:\n"
        "    note()\n"
        "except dl.StaleOwnerKeyError:\n"
        "    handle()\n"
    )
    assert _violations(source, "a.py") == ([], 1)
    attr = source.replace("except Gap:", "except dl.OwnerKeyVersionUnavailableError:")
    assert _violations(attr, "b.py") == ([], 1)


def test_guard_negative_control_tuple_form_is_not_a_decision_site() -> None:
    """Negative control: the pass-through tuple and a correct site are clean."""
    tuple_form = (
        "try:\n"
        "    work()\n"
        "except (StaleOwnerKeyError, DecryptionError) as err:\n"
        "    log(err)\n"
        "    raise\n"
    )
    found = _scan(tuple_form, "tuple.py")
    assert (found.violations, found.stale_clauses, found.passthrough) == ([], 0, 1)

    correct = (
        "try:\n"
        "    work()\n"
        "except OwnerKeyVersionUnavailableError as gap:\n"
        "    note(gap)\n"
        "except StaleOwnerKeyError as err:\n"
        "    handle(err)\n"
        "except DecryptionError as err:\n"
        "    escalate(err)\n"
    )
    found = _scan(correct, "correct.py")
    assert (found.violations, found.stale_clauses, found.passthrough) == ([], 1, 0)


def test_decryption_only_site_is_counted_as_pass_through() -> None:
    """A site that only catches ``DecryptionError`` lands in the pinned count."""
    source = "try:\n    work()\nexcept DecryptionError as err:\n    escalate(err)\n"
    found = _scan(source, "d.py")
    assert (found.violations, found.passthrough) == ([], 1)


def test_passthrough_clauses_are_pinned() -> None:
    """Clauses catching ``DecryptionError`` without a gap clause are pinned per file."""
    counts = {
        label: found.passthrough
        for label, source in _package_sources()
        if (found := _scan(source, label)).passthrough
    }
    assert counts == _EXPECTED_PASSTHROUGH
