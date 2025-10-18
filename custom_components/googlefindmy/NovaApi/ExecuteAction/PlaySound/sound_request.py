# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/sound_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

from typing import Optional

from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
    create_action_request,
    serialize_action_request,
)


def create_sound_request(
    should_start: bool,
    canonic_device_id: str,
    gcm_registration_id: str,
    request_uuid: Optional[str] = None,
) -> str:
    """Build the hex-encoded Nova payload for a Play/Stop Sound action (pure builder).

    This function performs **no network I/O**. It only constructs and serializes the
    protobuf action request used by Nova. Transport submission is handled elsewhere.

    Args:
        should_start: True to start playing a sound, False to stop.
        canonic_device_id: Canonical device id (as returned by the device list).
        gcm_registration_id: FCM registration/token string used for push correlation.
        request_uuid: Optional request UUID; a random one will be generated when omitted.

    Returns:
        Hex-encoded protobuf payload suitable for Nova transport.

    Raises:
        ValueError: If required arguments are empty or malformed.
    """
    # Defensive argument validation (keep server-side errors out of transport path)
    if not isinstance(canonic_device_id, str) or not canonic_device_id.strip():
        raise ValueError("canonic_device_id must be a non-empty string")
    if not isinstance(gcm_registration_id, str) or not gcm_registration_id.strip():
        raise ValueError("gcm_registration_id must be a non-empty string")

    # Lazy import of protobuf module to avoid heavy import work at integration startup.
    from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
    from custom_components.googlefindmy.NovaApi.util import generate_random_uuid

    if request_uuid is None:
        request_uuid = generate_random_uuid()

    # Create a base action request envelope
    action_request = create_action_request(
        canonic_device_id,
        gcm_registration_id,
        request_uuid=request_uuid,
    )

    # Select action branch; Google’s API currently accepts an unspecified component
    # for whole-device sound requests.
    if should_start:
        action_request.action.startSound.component = (
            DeviceUpdate_pb2.DeviceComponent.DEVICE_COMPONENT_UNSPECIFIED
        )
    else:
        action_request.action.stopSound.component = (
            DeviceUpdate_pb2.DeviceComponent.DEVICE_COMPONENT_UNSPECIFIED
        )

    # Serialize to hex for Nova transport
    return serialize_action_request(action_request)
