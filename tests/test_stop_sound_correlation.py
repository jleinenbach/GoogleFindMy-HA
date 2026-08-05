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
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.start_sound_request import (
    start_sound_request,
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


def test_stop_request_treats_a_blank_key_as_no_key() -> None:
    """Blank strings are not cancel keys, on either side of the wire.

    proto3 drops an implicit-presence scalar that holds its default value, so
    an empty ``requestUuid`` never reaches the server as a field. A blank but
    non-empty value ("   ") on the other hand *would* travel as a literal,
    fabricated cancel key -- exactly what this module exists to prevent.
    """

    for blank in ("", "   ", "\t"):
        request = _parse(
            stop_sound_request(_CANONICAL_ID, _FCM_TOKEN, request_uuid=blank)
        )
        assert request.requestMetadata.requestUuid == "", blank


def test_start_builder_rejects_a_blank_cancel_key() -> None:
    """A start request must never go out without a usable cancel key.

    ``None`` keeps meaning "generate one" (unchanged), but a blank string is a
    caller defect: it would submit a ``startSound`` whose ring nothing can ever
    reference, and the builder returns only the payload, so it cannot repair
    the mistake by generating a key the caller would never learn about. The
    stop branch is the mirror image and must stay permissive.
    """

    for blank in ("", "   ", "\t"):
        try:
            create_sound_request(True, _CANONICAL_ID, _FCM_TOKEN, request_uuid=blank)
        except ValueError:
            pass
        else:  # pragma: no cover - only reached on a regression
            raise AssertionError(f"start request accepted blank key {blank!r}")

        # Same value, stop branch: allowed, and it leaves the field off the wire.
        stop = _parse(
            create_sound_request(False, _CANONICAL_ID, _FCM_TOKEN, request_uuid=blank)
        )
        assert stop.requestMetadata.requestUuid == ""


def test_start_sound_request_returns_the_key_it_puts_on_the_wire() -> None:
    """The returned cancel key and the transmitted one are the same string.

    This is the anti-fabrication invariant of the play path. If a lower layer
    silently generated a substitute for a blank input, the caller would cache a
    key that references nothing -- the same defect as BSkando#195, only
    inverted (fabricated on the play side instead of the stop side).
    """

    for supplied in (None, "", "   "):
        payload_hex, used = start_sound_request(_CANONICAL_ID, _FCM_TOKEN, supplied)
        assert used, f"no cancel key returned for {supplied!r}"
        assert _parse(payload_hex).requestMetadata.requestUuid == used


def test_stop_request_with_omitted_uuid_does_not_invent_one() -> None:
    """The shared builder, not just its caller, refuses to fabricate a key.

    Regression for the layering defect: the original guard sat one level down,
    in `create_action_request`, so `create_sound_request(False, ...)` with an
    omitted UUID still generated a random one. `stop_sound_request` merely
    covered that up for the in-repo path by passing "" itself. Any other caller
    of this public builder reintroduced BSkando#195 verbatim.
    """

    request = _parse(create_sound_request(False, _CANONICAL_ID, _FCM_TOKEN))

    assert request.requestMetadata.requestUuid == ""
    assert request.action.HasField("stopSound")
