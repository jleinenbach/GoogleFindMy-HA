# tests/test_owner_key_version_catch_sites.py
"""Every decision catch site handles the owner-key version gap.

``OwnerKeyVersionUnavailableError`` is a ``DecryptionError`` subclass. A catch
site that decides between "per tracker" and "account-wide" (it names the bare
``StaleOwnerKeyError``) must catch the gap in its own clause BEFORE that one;
otherwise the gap falls through to ``except DecryptionError`` and drives the
account-wide reauth path (issue #246).

Rule: each ``except`` clause whose type is the bare name ``StaleOwnerKeyError``
(``ast.Name``) has a preceding clause for ``OwnerKeyVersionUnavailableError`` in
the same ``try``. Tuple clauses that list ``DecryptionError`` are pass-throughs
(they re-raise or log and hand the class on unchanged); they are not decision
sites and are pinned as a list instead.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"
_STALE = "StaleOwnerKeyError"
_GAP = "OwnerKeyVersionUnavailableError"
_DECRYPTION = "DecryptionError"
_EXPECTED_PASSTHROUGH = {"NovaApi/ExecuteAction/LocateTracker/location_request.py"}


def _handler_name(handler: ast.ExceptHandler) -> str | None:
    return handler.type.id if isinstance(handler.type, ast.Name) else None


def _tries(tree: ast.AST) -> list[ast.Try | ast.TryStar]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Try | ast.TryStar)]


def _violations(source: str, label: str) -> tuple[list[str], int]:
    """Return (violations, number of bare StaleOwnerKeyError clauses)."""
    violations: list[str] = []
    stale_clauses = 0
    for node in _tries(ast.parse(source)):
        seen_gap = False
        for handler in node.handlers:
            name = _handler_name(handler)
            if name == _GAP:
                seen_gap = True
            elif name == _STALE:
                stale_clauses += 1
                if not seen_gap:
                    violations.append(f"{label}:{handler.lineno}")
    return violations, stale_clauses


def _passthrough_tuples(source: str) -> int:
    count = 0
    for node in _tries(ast.parse(source)):
        for handler in node.handlers:
            if isinstance(handler.type, ast.Tuple) and any(
                isinstance(elt, ast.Name) and elt.id == _DECRYPTION
                for elt in handler.type.elts
            ):
                count += 1
    return count


def _package_sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(_ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(_ROOT.rglob("*.py"))
    ]


def test_every_stale_catch_is_preceded_by_gap_catch() -> None:
    """Each bare ``except StaleOwnerKeyError`` has a gap clause before it."""
    violations: list[str] = []
    stale_total = 0
    for label, source in _package_sources():
        found, stale = _violations(source, label)
        violations.extend(found)
        stale_total += stale

    assert violations == []
    # Not vacuous: the three decision sites (poll, manual locate, push) exist.
    assert stale_total >= 3


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


def test_guard_negative_control_tuple_form_is_not_a_decision_site() -> None:
    """Negative control: the tuple form and a correct site are both clean."""
    tuple_form = (
        "try:\n"
        "    work()\n"
        "except (StaleOwnerKeyError, DecryptionError) as err:\n"
        "    log(err)\n"
        "    raise\n"
    )
    assert _violations(tuple_form, "tuple.py") == ([], 0)
    assert _passthrough_tuples(tuple_form) == 1

    correct = (
        "try:\n"
        "    work()\n"
        "except OwnerKeyVersionUnavailableError as gap:\n"
        "    note(gap)\n"
        "except StaleOwnerKeyError as err:\n"
        "    handle(err)\n"
    )
    assert _violations(correct, "correct.py") == ([], 1)


def test_passthrough_tuples_are_pinned() -> None:
    """Pass-through tuples listing ``DecryptionError`` live only in the pinned file."""
    files = {
        label for label, source in _package_sources() if _passthrough_tuples(source)
    }
    assert files == _EXPECTED_PASSTHROUGH
