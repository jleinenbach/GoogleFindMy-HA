# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/phone_payload_guard.py
"""Wire-level lock on the send path for actions addressed to a phone.

A phone that Google lists without a canonical ID is addressed through scope field
1 of ``ExecuteActionRequest``. Of the phone actions only 1 (ring), 4 (locate) and 5
(stop ringing) are ever sent; the others have no observed meaning and may lock or
wipe the phone. The builder refuses them before serialization and an AST guard keeps
their field names out of the package, but both are checks on code. This module is
the check on the bytes: ``async_nova_request`` calls :func:`check_phone_action_payload`
for every payload of the action scope before anything goes to the wire, and a
payload whose scope names a phone passes only when its action message carries
exactly one field, numbered 1, 4 or 5, with the content the builder writes for it
(16 key bytes in field 1 for action 4, nothing for 1 and 5).

The payload is read raw, field by field, not through the generated classes: those
drop a field the schema does not know, which is precisely the field this lock has
to see. A payload without the phone scope (every tracker request) is not judged
here. Nothing is logged; the payload carries the Android ID.
"""

from __future__ import annotations

import binascii
from collections.abc import Iterator

ALLOWED_PHONE_ACTION_NUMBERS: frozenset[int] = frozenset({1, 4, 5})
_LOCATE_ACTION = 4
_LOCATE_KEY_BYTES = 16
_MAX_VARINT_SHIFT = 63  # a varint holds at most 64 bits

_WIRE_VARINT = 0
_WIRE_FIXED64 = 1
_WIRE_LENGTH = 2
_WIRE_FIXED32 = 5

_REQUEST_SCOPE_FIELD = 1
_REQUEST_ACTION_FIELD = 2
_SCOPE_PHONE_FIELD = 1
_LOCATE_KEY_FIELD = 1


class PhoneActionNotAllowedError(ValueError):
    """The payload addresses a phone with an action that must never be sent."""


class _WireError(ValueError):
    """The bytes are not a well-formed protobuf message."""


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise _WireError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > _MAX_VARINT_SHIFT:
            raise _WireError("varint too long")


def _fields(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Yield ``(field_number, wire_type, raw_value)`` for each field in ``data``.

    ``raw_value`` is the content for a length-delimited field and the encoded
    bytes of the value otherwise. Groups (wire types 3 and 4) and unknown wire
    types are errors: this reader fails closed on anything it cannot judge.
    """
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        number, wire_type = tag >> 3, tag & 0x07
        if number == 0:
            raise _WireError("field number 0")
        if wire_type == _WIRE_VARINT:
            start = pos
            _value, pos = _read_varint(data, pos)
            yield number, wire_type, data[start:pos]
        elif wire_type == _WIRE_FIXED64:
            if pos + 8 > len(data):
                raise _WireError("truncated fixed64")
            yield number, wire_type, data[pos : pos + 8]
            pos += 8
        elif wire_type == _WIRE_LENGTH:
            length, pos = _read_varint(data, pos)
            if pos + length > len(data):
                raise _WireError("truncated length-delimited field")
            yield number, wire_type, data[pos : pos + length]
            pos += length
        elif wire_type == _WIRE_FIXED32:
            if pos + 4 > len(data):
                raise _WireError("truncated fixed32")
            yield number, wire_type, data[pos : pos + 4]
            pos += 4
        else:
            raise _WireError(f"unsupported wire type {wire_type}")


def _has_phone_scope(request: bytes) -> bool:
    for number, wire_type, value in _fields(request):
        if number != _REQUEST_SCOPE_FIELD:
            continue
        if wire_type != _WIRE_LENGTH:
            raise _WireError("scope is not a message")
        for scope_number, _scope_type, _scope_value in _fields(value):
            if scope_number == _SCOPE_PHONE_FIELD:
                return True
    return False


def _action_fields(request: bytes) -> list[tuple[int, int, bytes]]:
    """Return the fields of the action message, merged over repeated occurrences."""
    found: list[tuple[int, int, bytes]] = []
    for number, wire_type, value in _fields(request):
        if number != _REQUEST_ACTION_FIELD:
            continue
        if wire_type != _WIRE_LENGTH:
            raise _WireError("action is not a message")
        found.extend(_fields(value))
    return found


def _check_action_content(number: int, wire_type: int, content: bytes) -> None:
    if wire_type != _WIRE_LENGTH:
        raise PhoneActionNotAllowedError(
            f"phone action {number} is not a message on the wire"
        )
    if number == _LOCATE_ACTION:
        inner = list(_fields(content))
        if len(inner) != 1:
            raise PhoneActionNotAllowedError(
                "phone locate action must carry exactly one field"
            )
        key_number, key_type, key = inner[0]
        if (
            key_number != _LOCATE_KEY_FIELD
            or key_type != _WIRE_LENGTH
            or len(key) != _LOCATE_KEY_BYTES
        ):
            raise PhoneActionNotAllowedError(
                "phone locate action must carry a 16-byte key in field 1"
            )
        return
    if content:
        raise PhoneActionNotAllowedError(f"phone action {number} must be empty")


def check_phone_action_payload(hex_payload: str) -> bool:
    """Refuse a payload that addresses a phone with anything but action 1, 4 or 5.

    Returns ``True`` when the payload names a phone in its scope and passed,
    ``False`` when it does not address a phone (nothing is judged then). Raises
    :class:`PhoneActionNotAllowedError` for a phone payload whose action message
    is missing, carries more than one field, a field outside
    ``ALLOWED_PHONE_ACTION_NUMBERS``, or content other than the builder's, and for
    bytes that cannot be read as a protobuf message at all (fail closed: what
    cannot be judged is not sent).
    """
    try:
        request = binascii.unhexlify(hex_payload)
    except (binascii.Error, TypeError, ValueError) as err:
        raise PhoneActionNotAllowedError("payload is not hex") from err
    try:
        if not _has_phone_scope(request):
            return False
        actions = _action_fields(request)
    except _WireError as err:
        raise PhoneActionNotAllowedError(f"unreadable payload ({err})") from err
    if len(actions) != 1:
        raise PhoneActionNotAllowedError(
            f"phone action message must carry exactly one field, found {len(actions)}"
        )
    number, wire_type, content = actions[0]
    if number not in ALLOWED_PHONE_ACTION_NUMBERS:
        raise PhoneActionNotAllowedError(f"phone action {number} is not allowed")
    try:
        _check_action_content(number, wire_type, content)
    except _WireError as err:
        raise PhoneActionNotAllowedError(f"unreadable phone action ({err})") from err
    return True
