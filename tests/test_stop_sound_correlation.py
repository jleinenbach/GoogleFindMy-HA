# tests/test_stop_sound_correlation.py
"""Pin that a Stop Sound request never fabricates a correlation key.

Background (BSkando#195): a stop request used to fall back to a freshly
generated random UUID when no cancel key was known. Such a UUID cannot, by
construction, reference a running ring, so the server can only reject or ignore
it while the integration still reported success.

The Find Hub Network accessory specification confirms the asymmetry from the
other side: on the local Beacon Actions channel a ring command carries no
request UUID, request ID or session identifier at all -- request and response
are correlated implicitly through the nonce that enters the HMAC of both
directions. A random UUID is therefore not a weak correlation handle, it is
none.
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
    create_action_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.sound_request import (
    create_sound_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.stop_sound_request import (
    stop_sound_request,
)
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2

_CANONICAL_ID = "canon-1"
_FCM_TOKEN = "fcm-token"


def _parse(payload_hex: str) -> Any:
    """Decode a hex-encoded ExecuteActionRequest payload."""

    request = DeviceUpdate_pb2.ExecuteActionRequest()
    request.ParseFromString(bytes.fromhex(payload_hex))
    return request


def test_stop_request_without_uuid_omits_request_uuid() -> None:
    """A stop without a known cancel key leaves ``requestUuid`` empty."""

    request = _parse(stop_sound_request(_CANONICAL_ID, _FCM_TOKEN))

    assert request.requestMetadata.requestUuid == ""
    # The stop action itself must still be present and well-formed.
    assert request.action.HasField("stopSound")
    assert request.action.stopSound.component == 0


def test_stop_request_with_uuid_keeps_the_cancel_key() -> None:
    """An explicitly supplied cancel key survives verbatim."""

    request = _parse(
        stop_sound_request(_CANONICAL_ID, _FCM_TOKEN, request_uuid="cancel-key-1")
    )

    assert request.requestMetadata.requestUuid == "cancel-key-1"


def test_start_request_without_uuid_still_generates_one() -> None:
    """Play semantics are unchanged: a start request needs a fresh key."""

    request = _parse(create_sound_request(True, _CANONICAL_ID, _FCM_TOKEN))

    assert len(request.requestMetadata.requestUuid) > 0
    assert request.action.HasField("startSound")


def test_create_action_request_preserves_empty_uuid() -> None:
    """An empty string is a deliberate marker, not a missing value."""

    request = create_action_request(_CANONICAL_ID, _FCM_TOKEN, request_uuid="")

    assert request.requestMetadata.requestUuid == ""


def test_create_action_request_generates_uuid_for_none() -> None:
    """``None`` still means "caller has no opinion, generate one"."""

    request = create_action_request(_CANONICAL_ID, _FCM_TOKEN, request_uuid=None)

    assert len(request.requestMetadata.requestUuid) > 0
