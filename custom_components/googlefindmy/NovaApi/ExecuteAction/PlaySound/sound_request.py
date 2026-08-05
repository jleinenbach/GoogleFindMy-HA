# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/sound_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
    create_action_request,
    serialize_action_request,
)

if TYPE_CHECKING:
    from custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2 import (
        ExecuteActionRequest,
    )


def _load_proto_module() -> Any:
    """Return the protobuf module without import-time side effects."""

    return import_module(
        "custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2"
    )


def _load_uuid_factory() -> Callable[[], str]:
    """Return the UUID generator helper lazily."""

    return cast(
        Callable[[], str],
        getattr(
            import_module("custom_components.googlefindmy.NovaApi.util"),
            "generate_random_uuid",
        ),
    )


def create_sound_request(
    should_start: bool,
    canonic_device_id: str,
    gcm_registration_id: str,
    request_uuid: str | None = None,
) -> str:
    """Build the hex-encoded Nova payload for a Play/Stop Sound action (pure builder).

    This function performs **no network I/O**. It only constructs and serializes the
    protobuf action request used by Nova. Transport submission is handled elsewhere.

    Args:
        should_start: True to start playing a sound, False to stop.
        canonic_device_id: Canonical device id (as returned by the device list).
        gcm_registration_id: FCM registration/token string used for push correlation.
        request_uuid: Optional request UUID, interpreted per action:

            * start, ``None``  -> a fresh UUID is generated;
            * start, blank     -> ``ValueError`` (see below);
            * stop, ``None``   -> no cancel key is sent, and none is invented;
            * stop, blank      -> same as ``None``;
            * either, a value  -> written verbatim.

    Returns:
        Hex-encoded protobuf payload suitable for Nova transport.

    Raises:
        ValueError: If required arguments are empty or malformed, or if a start
            request is given a blank ``request_uuid``.
    """
    # Defensive argument validation (keep server-side errors out of transport path)
    if not isinstance(canonic_device_id, str) or not canonic_device_id.strip():
        raise ValueError("canonic_device_id must be a non-empty string")
    if not isinstance(gcm_registration_id, str) or not gcm_registration_id.strip():
        raise ValueError("gcm_registration_id must be a non-empty string")

    # The two branches carry OPPOSITE invariants, and `should_start` is the only
    # field that can tell them apart, so the rule lives here rather than in a
    # sentinel value:
    #   start -> the payload must always carry a usable cancel key;
    #   stop  -> the payload must never carry a fabricated one.
    # A blank string is not a key. On the wire it cannot even be one: proto3
    # omits an implicit-presence scalar that holds its default value, so ""
    # reaches the server as *no* requestUuid at all.
    if should_start:
        if request_uuid is not None and not request_uuid.strip():
            # Not repairable here: this builder returns only the payload, so a
            # substitute key would never reach the caller, and a cancel key the
            # caller does not know is worth exactly as much as none.
            raise ValueError(
                "request_uuid must be a non-empty string for start requests"
            )
    else:
        # Blank in, absent out. Without this, "   " would travel as a literal,
        # fabricated cancel key -- the very defect this module removes.
        request_uuid = (request_uuid or "").strip()

    proto_module = _load_proto_module()
    generate_random_uuid = _load_uuid_factory()

    if request_uuid is None:
        request_uuid = generate_random_uuid()

    # Create a base action request envelope
    action_request: ExecuteActionRequest = create_action_request(
        canonic_device_id,
        gcm_registration_id,
        request_uuid=request_uuid,
    )

    # Select action branch; Google’s API currently accepts an unspecified component
    # for whole-device sound requests.
    if should_start:
        action_request.action.startSound.component = (
            proto_module.DeviceComponent.DEVICE_COMPONENT_UNSPECIFIED
        )
    else:
        action_request.action.stopSound.component = (
            proto_module.DeviceComponent.DEVICE_COMPONENT_UNSPECIFIED
        )

    # Serialize to hex for Nova transport
    return serialize_action_request(action_request)
