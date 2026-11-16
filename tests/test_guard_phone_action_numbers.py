# tests/test_guard_phone_action_numbers.py
"""Guard: no code outside the generated protobuf modules names a forbidden action.

Phone actions 2, 6, 7, 13 and 14 have no observed meaning and may lock or wipe the
phone. The phone package also never names the tracker actions 30, 31 and 32
(``locateTracker``, ``startSound``, ``stopSound``). The guard reads the syntax tree
of every module of the integration except ``*_pb2*``. A name counts wherever the
syntax tree can carry it: an attribute, a keyword argument, a word inside a string
constant (``getattr`` with a literal, a ``text_format`` or JSON text, a dict key),
and the generated constant ``PHONEACTIONFIELD<n>_FIELD_NUMBER``. A string that holds
the bare word ``phoneActionField`` without its number (an f-string,
``"phoneActionField%d" % n``, ``"phoneActionField" + str(n)``, ``str.format``)
counts as every phone action, because the number is built at run time and cannot
be read. Docstrings are skipped: they describe the rule, they do not build a
request.

This guard is a tripwire on names in code, not the lock. It is blind to every form
that carries the action as a number or as raw bytes: a field looked up by number
(``DESCRIPTOR.fields_by_number``), a message filled from bytes
(``MergeFromString``, ``ParseFromString``), a name split over several strings
(``"".join(...)``), a name read from data or configuration. The authoritative layer
is the wire lock in ``async_nova_request``
(``LocatePhone/phone_payload_guard.py``): it reads the bytes of every action
payload that addresses a phone and lets through only actions 1, 4 and 5 in their
fixed form. ``test_the_declared_blind_spots_stay_blind`` pins the blind forms, and
``test_the_wire_lock_refuses_what_this_guard_cannot_read`` shows the lock refusing
them.
"""

from __future__ import annotations

import ast
import binascii
import re
from pathlib import Path

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_payload_guard import (
    PhoneActionNotAllowedError,
    check_phone_action_payload,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2

PACKAGE = Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"
PHONE_PACKAGE = PACKAGE / "NovaApi" / "ExecuteAction" / "LocatePhone"

FORBIDDEN_PHONE_ACTIONS = frozenset(f"phoneActionField{n}" for n in (2, 6, 7, 13, 14))
TRACKER_ACTIONS = frozenset({"locateTracker", "startSound", "stopSound"})


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BARE_PHONE_ACTION = "phoneActionField"
_FIELD_NUMBER_SUFFIX = "_FIELD_NUMBER"
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
    """Names the syntax tree carries, with the generated constants mapped back.

    ``PHONEACTIONFIELD2_FIELD_NUMBER`` is returned as ``phoneActionField2``; a
    string with the bare word ``phoneActionField`` adds every phone action name.
    """
    tree = ast.parse(source)
    skip = _docstrings(tree)
    raw: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            raw.add(node.attr)
        elif isinstance(node, ast.Name):
            raw.add(node.id)
        elif isinstance(node, ast.keyword) and node.arg:
            raw.add(node.arg)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ):
            raw.update(_WORD.findall(node.value))
    known = {name.lower(): name for name in ALL_PHONE_ACTION_NAMES | TRACKER_ACTIONS}
    names = set(raw)
    for name in raw:
        if name.endswith(_FIELD_NUMBER_SUFFIX):
            stem = name.removesuffix(_FIELD_NUMBER_SUFFIX).lower()
            if stem in known:
                names.add(known[stem])
    if _BARE_PHONE_ACTION in names:
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
        "getattr(a, 'phoneActionField%d' % n)": "phoneActionField6",
        "getattr(a, 'phoneActionField' + str(n))": "phoneActionField7",
        "getattr(a, 'phoneActionField{}'.format(n))": "phoneActionField13",
        "pb2.ExecuteActionType.PHONEACTIONFIELD14_FIELD_NUMBER": "phoneActionField14",
        "pb2.ExecuteActionType.STOPSOUND_FIELD_NUMBER": "stopSound",
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


# Forms that carry the action as a number, as bytes or as a name split over
# strings. The guard cannot read them; listed so that the blind spot is stated
# by a test and not discovered by the next review.
_BLIND_FORMS = (
    "t = pb2.ExecuteActionType.DESCRIPTOR.fields_by_number[2]",
    "req.action.MergeFromString(bytes([0x12, 0x00]))",
    "req.action.ParseFromString(raw)",
    "getattr(a, ''.join(['phoneAction', 'Field2']))",
    "getattr(a, config['action'])",
)


@pytest.mark.parametrize("source", _BLIND_FORMS)
def test_the_declared_blind_spots_stay_blind(source: str) -> None:
    """The guard does not see these forms; the module docstring says so.

    When this test fails, the guard learned a form: move it to
    ``test_the_guard_sees_every_form`` and update the docstring.
    """
    assert not _names(source) & FORBIDDEN_PHONE_ACTIONS, source


def _phone_request() -> DeviceUpdate_pb2.ExecuteActionRequest:
    request = DeviceUpdate_pb2.ExecuteActionRequest()
    request.scope.phone.phoneInformationField1 = 1234567890123456789
    return request


def _by_number(number: int) -> str:
    request = _phone_request()
    field = DeviceUpdate_pb2.ExecuteActionType.DESCRIPTOR.fields_by_number[number]
    getattr(request.action, field.name).SetInParent()
    return binascii.hexlify(request.SerializeToString()).decode("ascii")


def _from_bytes(number: int) -> str:
    request = _phone_request()
    request.action.MergeFromString(bytes([number << 3 | 2, 0]))
    return binascii.hexlify(request.SerializeToString()).decode("ascii")


@pytest.mark.parametrize("number", sorted({2, 6, 7, 13, 14}))
@pytest.mark.parametrize("build", [_by_number, _from_bytes])
def test_the_wire_lock_refuses_what_this_guard_cannot_read(build, number: int) -> None:
    """Counter-proof for the blind forms: the bytes they produce do not pass the lock."""
    with pytest.raises(PhoneActionNotAllowedError):
        check_phone_action_payload(build(number))


def test_the_wire_lock_passes_the_allowed_ring_built_by_number() -> None:
    """Control: the same construction with action 1 passes, so the refusals above
    come from the number and not from the construction."""
    assert check_phone_action_payload(_by_number(1)) is True
