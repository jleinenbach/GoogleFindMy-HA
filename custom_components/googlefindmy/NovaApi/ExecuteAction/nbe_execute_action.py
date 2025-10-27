# custom_components/googlefindmy/NovaApi/ExecuteAction/nbe_execute_action.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import binascii
from typing import TYPE_CHECKING

from custom_components.googlefindmy.NovaApi.util import generate_random_uuid

# Typing-only imports to avoid expensive protobuf imports during runtime startup.
if TYPE_CHECKING:
    from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2

# Session-stable client UUID (created lazily to avoid import-time side effects)
_CLIENT_UUID: str | None = None


def _get_client_uuid() -> str:
    """Return a session-stable client UUID, generating it on first use."""
    global _CLIENT_UUID
    if not _CLIENT_UUID:
        _CLIENT_UUID = generate_random_uuid()
    return _CLIENT_UUID


def create_action_request(
    canonic_device_id: str,
    gcm_registration_id: str,
    *,
    request_uuid: str | None = None,
    fmd_client_uuid: str | None = None,
) -> "DeviceUpdate_pb2.ExecuteActionRequest":
    """Build an ExecuteActionRequest protobuf for Nova (pure builder, no I/O).

    Args:
        canonic_device_id: Canonical device id from the device list.
        gcm_registration_id: FCM registration token (used for push correlation).
        request_uuid: Optional request UUID. If omitted, a random UUID is generated.
        fmd_client_uuid: Optional session/client UUID. If omitted, a lazy, session-stable
            UUID is used.

    Returns:
        A `DeviceUpdate_pb2.ExecuteActionRequest` instance.

    Raises:
        ValueError: If required arguments are missing/empty.
    """
    if not isinstance(canonic_device_id, str) or not canonic_device_id.strip():
        raise ValueError("canonic_device_id must be a non-empty string")
    if not isinstance(gcm_registration_id, str) or not gcm_registration_id.strip():
        raise ValueError("gcm_registration_id must be a non-empty string")

    # Lazy import to avoid heavy protobuf import work at HA startup time.
    from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2

    req_uuid = request_uuid or generate_random_uuid()
    client_uuid = fmd_client_uuid or _get_client_uuid()

    action_request = DeviceUpdate_pb2.ExecuteActionRequest()

    # Scope: SPOT device by canonical id
    action_request.scope.type = DeviceUpdate_pb2.DeviceType.SPOT_DEVICE
    action_request.scope.device.canonicId.id = canonic_device_id

    # Request metadata (types mirror scope)
    action_request.requestMetadata.type = DeviceUpdate_pb2.DeviceType.SPOT_DEVICE
    action_request.requestMetadata.requestUuid = req_uuid
    action_request.requestMetadata.fmdClientUuid = client_uuid
    action_request.requestMetadata.gcmRegistrationId.id = gcm_registration_id

    # Historical flag observed in upstream traffic; kept for parity/back-compat.
    action_request.requestMetadata.unknown = True  # noqa: FBT003 (explicit protocol quirk)

    return action_request


def serialize_action_request(
    action_request: "DeviceUpdate_pb2.ExecuteActionRequest",
) -> str:
    """Serialize an ExecuteActionRequest to hex for Nova transport."""
    # Serialize to bytes
    binary_payload = action_request.SerializeToString()
    # Encode as hex for Nova HTTP transport
    return binascii.hexlify(binary_payload).decode("utf-8")
