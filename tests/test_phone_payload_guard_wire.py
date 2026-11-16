# tests/test_phone_payload_guard_wire.py
"""Raw-wire reader of the phone payload lock: every malformed shape fails closed.

``check_phone_action_payload`` reads the request field by field instead of through
the generated classes. These cases feed it hand-built bytes: each malformed shape
must raise ``PhoneActionNotAllowedError``, and the two wire types the builder never
writes (fixed64, fixed32) must be skipped correctly when they are well formed. The
Android ID is invented.
"""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_payload_guard import (
    PhoneActionNotAllowedError,
    check_phone_action_payload,
)

_ANDROID_ID = 1_234_567_890_123_456_789


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _tag(number: int, wire_type: int) -> bytes:
    return _varint((number << 3) | wire_type)


def _ld(number: int, content: bytes) -> bytes:
    return _tag(number, 2) + _varint(len(content)) + content


_PHONE_SCOPE = _ld(1, _ld(1, _tag(1, 0) + _varint(_ANDROID_ID)))
_RING = _ld(2, _ld(1, b""))


def _check(raw: bytes) -> bool:
    return check_phone_action_payload(raw.hex())


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"\x80", "truncated varint"),
        (b"\xff" * 11, "varint too long"),
        (b"\x00\x00", "field number 0"),
        (_PHONE_SCOPE + _RING + _tag(9, 1) + b"\x00" * 3, "truncated fixed64"),
        (_PHONE_SCOPE + _RING + _tag(9, 5) + b"\x00" * 2, "truncated fixed32"),
        (_tag(1, 2) + _varint(5) + b"ab", "truncated length-delimited field"),
        (_tag(9, 3) + _PHONE_SCOPE + _RING, "unsupported wire type 3"),
        (_tag(1, 0) + _varint(1), "scope is not a message"),
        (_PHONE_SCOPE + _tag(2, 0) + _varint(1), "action is not a message"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_malformed_request_is_refused(raw: bytes, reason: str) -> None:
    with pytest.raises(PhoneActionNotAllowedError, match="unreadable payload") as err:
        _check(raw)
    assert reason in str(err.value)


def test_unreadable_locate_content_is_refused() -> None:
    raw = _PHONE_SCOPE + _ld(2, _ld(4, b"\x80"))
    with pytest.raises(PhoneActionNotAllowedError, match="unreadable phone action"):
        _check(raw)


@pytest.mark.parametrize(
    "extra",
    [_tag(9, 1) + b"\x01" * 8, _tag(9, 5) + b"\x01" * 4],
    ids=["fixed64", "fixed32"],
)
def test_well_formed_fixed_width_fields_are_skipped(extra: bytes) -> None:
    assert _check(extra + _PHONE_SCOPE + _RING) is True


def test_fixed_width_fields_do_not_hide_a_forbidden_action() -> None:
    raw = _tag(9, 1) + b"\x01" * 8 + _PHONE_SCOPE + _ld(2, _ld(2, b""))
    with pytest.raises(PhoneActionNotAllowedError, match="phone action 2"):
        _check(raw)
