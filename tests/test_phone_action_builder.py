# tests/test_phone_action_builder.py
"""Builder for phone actions 1, 4 and 5 (whitelist, wire form, number and byte checks).

Layer 3 decodes the serialized bytes on the wire, not ``ListFields()``, which would
not show a field the schema does not know. Every Android ID is synthetic.
"""

from __future__ import annotations

import secrets
from enum import IntEnum
from typing import Any

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_action_request import (
    ALLOWED_PHONE_ACTIONS,
    PhoneAction,
    build_phone_action_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_location_crypto import (
    new_locate_key,
)

_TOKEN = "fcm-token-for-tests"


def _android_id() -> int:
    return 10**18 + secrets.randbelow(4 * 10**18)


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    shift = value = 0
    while True:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7


def _wire(data: bytes) -> dict[int, list[Any]]:
    """Decode one message level of raw protobuf wire data (varint and bytes only)."""
    out: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(data):
        tag, pos = _varint(data, pos)
        field, wire_type = tag >> 3, tag & 7
        if wire_type == 0:
            value, pos = _varint(data, pos)
        elif wire_type == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos : pos + size], pos + size
        else:
            raise AssertionError(f"unexpected wire type {wire_type}")
        out.setdefault(field, []).append(value)
    return out


def _decode(hex_request: str) -> dict[str, Any]:
    top = _wire(bytes.fromhex(hex_request))
    assert sorted(top) == [1, 2, 3]
    scope = _wire(top[1][0])
    action = _wire(top[2][0])
    meta = _wire(top[3][0])
    return {"scope": scope, "phone": _wire(scope[1][0]), "action": action, "meta": meta}


def test_locate_request_on_the_wire() -> None:
    android_id = _android_id()
    key = new_locate_key()
    decoded = _decode(
        build_phone_action_request(
            android_id, PhoneAction.LOCATE, _TOKEN, "req-1", locate_key=key
        )
    )
    assert sorted(decoded["scope"]) == [1, 2]
    assert decoded["phone"] == {1: [android_id]}
    assert decoded["scope"][2] == [1]  # ANDROID_DEVICE
    assert sorted(decoded["action"]) == [4]
    assert _wire(decoded["action"][4][0]) == {1: [key]}
    meta = decoded["meta"]
    assert sorted(meta) == [1, 2, 3, 4, 6]
    assert meta[1] == [1]
    assert meta[2] == [b"req-1"]
    assert _wire(meta[4][0]) == {1: [_TOKEN.encode()]}
    assert meta[6] == [1]


def test_two_locate_keys_differ() -> None:
    first, second = new_locate_key(), new_locate_key()
    assert len(first) == len(second) == 16
    assert first != second


@pytest.mark.parametrize(
    ("action", "field"), [(PhoneAction.RING, 1), (PhoneAction.STOP_RING, 5)]
)
def test_ring_and_stop_are_empty_and_keyless(action: PhoneAction, field: int) -> None:
    decoded = _decode(build_phone_action_request(_android_id(), action, _TOKEN, "r"))
    assert decoded["action"] == {field: [b""]}


@pytest.mark.parametrize("number", [n for n in range(64) if n not in {1, 4, 5}])
def test_av_every_other_number_raises(number: int) -> None:
    with pytest.raises(ValueError):
        build_phone_action_request(
            _android_id(), number, _TOKEN, "r", locate_key=new_locate_key()
        )
    with pytest.raises(ValueError):
        build_phone_action_request(_android_id(), number, _TOKEN, "r")


def test_av_a_manipulated_enum_raises() -> None:
    class Forged(IntEnum):
        RING = 2
        LOCATE = 6
        STOP_RING = 13

    class Disguised:
        def __int__(self) -> int:
            return 7

    for member in Forged:
        with pytest.raises(ValueError):
            build_phone_action_request(_android_id(), member, _TOKEN, "r")
    with pytest.raises(ValueError):
        build_phone_action_request(_android_id(), Disguised(), _TOKEN, "r")  # type: ignore[arg-type]
    assert frozenset(int(a) for a in PhoneAction) == ALLOWED_PHONE_ACTIONS


@pytest.mark.parametrize("key", [None, b"", b"x" * 15, b"x" * 17, "x" * 16])
def test_locate_needs_exactly_sixteen_bytes(key: Any) -> None:
    with pytest.raises(ValueError):
        build_phone_action_request(
            _android_id(), PhoneAction.LOCATE, _TOKEN, "r", locate_key=key
        )


@pytest.mark.parametrize("action", [PhoneAction.RING, PhoneAction.STOP_RING])
def test_ring_and_stop_refuse_a_key(action: PhoneAction) -> None:
    with pytest.raises(ValueError):
        build_phone_action_request(
            _android_id(), action, _TOKEN, "r", locate_key=new_locate_key()
        )


@pytest.mark.parametrize(
    ("android_id", "token", "uuid"),
    [
        (0, _TOKEN, "r"),
        (-5, _TOKEN, "r"),
        (True, _TOKEN, "r"),
        (7, " ", "r"),
        (7, _TOKEN, ""),
    ],
)
def test_blank_or_invalid_arguments_raise(
    android_id: Any, token: str, uuid: str
) -> None:
    with pytest.raises(ValueError):
        build_phone_action_request(android_id, PhoneAction.RING, token, uuid)


def test_errors_never_carry_the_android_id() -> None:
    android_id = _android_id()
    for kwargs in (
        {"action": 2},
        {"action": 4},
        {"action": 1, "locate_key": b"k" * 16},
    ):
        with pytest.raises(ValueError) as excinfo:
            build_phone_action_request(
                android_id, fcm_token=_TOKEN, request_uuid="r", **kwargs
            )
        assert str(android_id) not in str(excinfo.value)


def test_layer4_tracker_builders_refuse_a_phone_row_id() -> None:
    from custom_components.googlefindmy.const import PHONE_ID_PREFIX
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (
        create_location_request,
    )
    from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
        create_action_request,
    )

    phone_id = PHONE_ID_PREFIX + "0" * 32
    with pytest.raises(ValueError):
        create_action_request(phone_id, _TOKEN)
    with pytest.raises(ValueError):
        create_location_request(phone_id, _TOKEN, "req")
    assert (
        create_action_request("tracker-id", _TOKEN).scope.device.canonicId.id
        == "tracker-id"
    )
    assert create_location_request("tracker-id", _TOKEN, "req")


def test_layer1_number_check_runs_before_any_message_is_built(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The number is refused even if building the message would succeed."""
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone import (
        phone_action_request,
    )

    def _no_proto() -> Any:
        raise AssertionError("message built for a forbidden number")

    monkeypatch.setattr(phone_action_request, "_proto", _no_proto)
    for number in (0, 2, 6, 7, 13, 14, 30, 31, 32):
        with pytest.raises(ValueError, match="not allowed"):
            build_phone_action_request(_android_id(), number, _TOKEN, "r")


@pytest.mark.parametrize("extra", [2, 6, 7, 13, 14])
def test_layer1_field_check_refuses_an_extra_action_field(
    monkeypatch: pytest.MonkeyPatch, extra: int
) -> None:
    """A message that carries any other action field is never serialized."""
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone import (
        phone_action_request,
    )
    from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2

    class _Tainted:
        DeviceType = DeviceUpdate_pb2.DeviceType

        @staticmethod
        def ExecuteActionRequest() -> Any:  # noqa: N802 - mirrors the pb2 name
            request = DeviceUpdate_pb2.ExecuteActionRequest()
            getattr(request.action, f"phoneActionField{extra}").SetInParent()
            return request

    monkeypatch.setattr(phone_action_request, "_proto", lambda: _Tainted)
    with pytest.raises(ValueError, match="does not match"):
        build_phone_action_request(_android_id(), PhoneAction.RING, _TOKEN, "r")


def test_layer1_byte_check_refuses_an_unknown_action_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field the schema does not know is invisible to ListFields(), not to bytes."""
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone import (
        phone_action_request,
    )
    from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2

    class _Tainted:
        DeviceType = DeviceUpdate_pb2.DeviceType

        @staticmethod
        def ExecuteActionRequest() -> Any:  # noqa: N802 - mirrors the pb2 name
            request = DeviceUpdate_pb2.ExecuteActionRequest()
            request.action.MergeFromString(b"\x9a\x06\x00")  # field 99, empty
            return request

    monkeypatch.setattr(phone_action_request, "_proto", lambda: _Tainted)
    with pytest.raises(ValueError, match="does not match"):
        build_phone_action_request(_android_id(), PhoneAction.RING, _TOKEN, "r")


@pytest.mark.parametrize("action", [None, 4.0, "4", True])
def test_non_integer_actions_raise_value_error(action: Any) -> None:
    with pytest.raises(ValueError):
        build_phone_action_request(
            _android_id(), action, _TOKEN, "r", locate_key=new_locate_key()
        )


def test_android_id_beyond_uint64_never_reaches_the_error_text() -> None:
    too_big = 2**64 + secrets.randbelow(10**6)
    with pytest.raises(ValueError) as excinfo:
        build_phone_action_request(too_big, PhoneAction.RING, _TOKEN, "r")
    assert str(too_big) not in str(excinfo.value)
    assert build_phone_action_request(2**64 - 1, PhoneAction.RING, _TOKEN, "r")


@pytest.mark.parametrize("action", [PhoneAction.RING, PhoneAction.STOP_RING])
def test_scope_and_metadata_match_the_locate_form(action: PhoneAction) -> None:
    android_id = _android_id()
    decoded = _decode(build_phone_action_request(android_id, action, _TOKEN, "r"))
    assert decoded["phone"] == {1: [android_id]}
    assert decoded["scope"][2] == [1]
    assert sorted(decoded["meta"]) == [1, 2, 3, 4, 6]


def test_layer4_also_refuses_a_padded_phone_row_id() -> None:
    from custom_components.googlefindmy.const import PHONE_ID_PREFIX
    from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
        create_action_request,
    )

    with pytest.raises(ValueError):
        create_action_request("  " + PHONE_ID_PREFIX + "0" * 32, _TOKEN)
