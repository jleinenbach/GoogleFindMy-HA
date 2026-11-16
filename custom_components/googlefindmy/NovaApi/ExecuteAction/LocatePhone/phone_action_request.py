# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/phone_action_request.py
"""Build the action request for a phone that Google lists without a canonical ID.

The request addresses the phone by its Android ID in scope field 1
(``ExecuteActionScope.phone.phoneInformationField1``) with type ``ANDROID_DEVICE``;
the request metadata is the one ``create_action_request`` writes for trackers, with
type ``ANDROID_DEVICE``. Observed form (n=1 phone, 2026-11-14 and 2026-11-15): scope
``{1: {1: android_id}, 2: 1}``; action 4 carries a fresh 16-byte key in field 1, the
phone answers with its location encrypted under that key; actions 1 and 5 are empty
and start and stop ringing.

Only the actions in ``ALLOWED_PHONE_ACTIONS`` are ever built. The check runs twice:
on the requested number, and on the fields actually set in the message before it is
serialized. Nothing here logs; the Android ID and the key are personal data and key
material.
"""

from __future__ import annotations

import binascii
from enum import IntEnum
from typing import Any

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_location_crypto import (
    LOCATE_KEY_BYTES,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
    _get_client_uuid,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2


class PhoneAction(IntEnum):
    """The phone actions this integration sends (ExecuteActionType field numbers)."""

    RING = 1
    LOCATE = 4
    STOP_RING = 5


ALLOWED_PHONE_ACTIONS: frozenset[int] = frozenset({1, 4, 5})
_MAX_ANDROID_ID = 2**64 - 1  # uint64 on the wire


def _expected_action_bytes(number: int, locate_key: bytes | None) -> bytes:
    """Return the exact wire form of the action message (no other field, known or not)."""
    if number == PhoneAction.LOCATE and locate_key is not None:
        inner = b"\x0a" + bytes([len(locate_key)]) + locate_key
        return b"\x22" + bytes([len(inner)]) + inner
    return bytes([(number << 3) | 2, 0])


def _proto() -> Any:
    """Return DeviceUpdate_pb2; a seam the builder tests replace."""
    return DeviceUpdate_pb2


def build_phone_action_request(  # noqa: PLR0913 - one argument per request field
    android_id: int,
    action: int,
    fcm_token: str,
    request_uuid: str,
    *,
    locate_key: bytes | None = None,
    fmd_client_uuid: str | None = None,
) -> str:
    """Return the hex-encoded ``ExecuteActionRequest`` for one phone action.

    Args:
        android_id: The phone's Android ID (``PhoneInformation`` field 1).
        action: ``PhoneAction.RING``, ``PhoneAction.LOCATE`` or
            ``PhoneAction.STOP_RING``; any other number raises.
        fcm_token: FCM registration token of the account (push correlation).
        request_uuid: Correlates the push answering this request; non-blank.
        locate_key: Exactly 16 bytes for ``LOCATE``; must be ``None`` otherwise.
        fmd_client_uuid: Optional client UUID; the session-stable one by default.

    Raises:
        ValueError: On a non-integer action or a number outside
            ``ALLOWED_PHONE_ACTIONS``, a missing or wrong key, blank arguments, an
            Android ID outside uint64, or a message whose action differs from the
            requested one in any field, known or unknown.
    """
    if isinstance(action, bool) or not isinstance(action, int):
        raise ValueError("phone action must be an integer")
    number = int(action)
    if number not in ALLOWED_PHONE_ACTIONS:
        raise ValueError(f"phone action {number} is not allowed")
    if (
        isinstance(android_id, bool)
        or not isinstance(android_id, int)
        or not 0 < android_id <= _MAX_ANDROID_ID
    ):
        raise ValueError("android_id must be a positive integer")
    if not isinstance(fcm_token, str) or not fcm_token.strip():
        raise ValueError("fcm_token must be a non-empty string")
    if not isinstance(request_uuid, str) or not request_uuid.strip():
        raise ValueError("request_uuid must be a non-empty string")
    if number == PhoneAction.LOCATE:
        if not isinstance(locate_key, bytes) or len(locate_key) != LOCATE_KEY_BYTES:
            raise ValueError("a locate request needs a 16-byte key")
    elif locate_key is not None:
        raise ValueError("only a locate request carries a key")

    pb2 = _proto()
    request = pb2.ExecuteActionRequest()
    request.scope.type = pb2.DeviceType.ANDROID_DEVICE
    request.scope.phone.phoneInformationField1 = android_id

    if number == PhoneAction.RING:
        request.action.phoneActionField1.SetInParent()
    elif number == PhoneAction.LOCATE:
        request.action.phoneActionField4.phoneLocateField1 = locate_key
    else:
        request.action.phoneActionField5.SetInParent()

    metadata = request.requestMetadata
    metadata.type = pb2.DeviceType.ANDROID_DEVICE
    metadata.requestUuid = request_uuid
    metadata.fmdClientUuid = fmd_client_uuid or _get_client_uuid()
    metadata.gcmRegistrationId.id = fcm_token
    metadata.unknown = True  # noqa: FBT003 (protocol quirk, as for trackers)

    # Two checks on the finished message: the known fields, and the exact bytes,
    # which also refuse a field the schema does not know.
    set_actions = {field.number for field, _ in request.action.ListFields()}
    if (
        set_actions != {number}
        or not set_actions <= ALLOWED_PHONE_ACTIONS
        or request.action.SerializeToString()
        != _expected_action_bytes(number, locate_key)
    ):
        raise ValueError("phone action message does not match the allowed action")

    return binascii.hexlify(request.SerializeToString()).decode("ascii")
