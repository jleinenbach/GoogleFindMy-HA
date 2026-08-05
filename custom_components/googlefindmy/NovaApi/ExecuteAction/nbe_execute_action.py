# custom_components/googlefindmy/NovaApi/ExecuteAction/nbe_execute_action.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import binascii
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from custom_components.googlefindmy.NovaApi.util import generate_random_uuid

# Typing-only imports to avoid expensive protobuf imports during runtime startup.
if TYPE_CHECKING:
    from custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2 import (
        ExecuteActionRequest,
    )

_CLIENT_STATE: dict[str, str | None] = {"uuid": None}
_PROTO_STATE: dict[str, Any] = {"module": None}


def _get_client_uuid() -> str:
    """Return a session-stable client UUID, generating it on first use."""

    if not _CLIENT_STATE["uuid"]:
        _CLIENT_STATE["uuid"] = generate_random_uuid()
    return cast(str, _CLIENT_STATE["uuid"])


def _get_proto_module() -> Any:
    """Load the protobuf module lazily to avoid startup overhead."""

    if _PROTO_STATE["module"] is None:
        _PROTO_STATE["module"] = import_module(
            "custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2"
        )
    return _PROTO_STATE["module"]


def create_action_request(
    canonic_device_id: str,
    gcm_registration_id: str,
    *,
    request_uuid: str | None = None,
    fmd_client_uuid: str | None = None,
) -> ExecuteActionRequest:
    """Build an ExecuteActionRequest protobuf for Nova (pure builder, no I/O).

    Args:
        canonic_device_id: Canonical device id from the device list.
        gcm_registration_id: FCM registration token (used for push correlation).
        request_uuid: Optional request UUID, written verbatim. If omitted
            (``None``), a random UUID is generated. A supplied value survives
            unchanged, including an empty string; whether an empty value is a
            deliberate marker or a defect depends on the action, which this
            generic envelope builder does not know. The action-aware builders
            (`create_sound_request`, `create_location_request`) own that rule.
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

    proto_module = _get_proto_module()

    # `None` means "caller has no opinion, generate one". Any supplied value is
    # written as given: `or` would silently replace an empty one with a random
    # UUID, and for a stop request that is exactly the fabricated correlation
    # this guard exists to prevent (BSkando#195). This builder does not judge
    # the value -- it cannot, it does not know the action -- it only refrains
    # from overwriting it.
    req_uuid = generate_random_uuid() if request_uuid is None else request_uuid
    client_uuid = fmd_client_uuid or _get_client_uuid()

    action_request: ExecuteActionRequest = proto_module.ExecuteActionRequest()

    # Scope: SPOT device by canonical id
    action_request.scope.type = proto_module.DeviceType.SPOT_DEVICE
    action_request.scope.device.canonicId.id = canonic_device_id

    # Request metadata (types mirror scope)
    action_request.requestMetadata.type = proto_module.DeviceType.SPOT_DEVICE
    action_request.requestMetadata.requestUuid = req_uuid
    action_request.requestMetadata.fmdClientUuid = client_uuid
    action_request.requestMetadata.gcmRegistrationId.id = gcm_registration_id

    # Historical flag observed in upstream traffic; kept for parity/back-compat.
    action_request.requestMetadata.unknown = True  # noqa: FBT003 (explicit protocol quirk)

    return action_request


def serialize_action_request(
    action_request: ExecuteActionRequest,
) -> str:
    """Serialize an ExecuteActionRequest to hex for Nova transport."""
    # Serialize to bytes
    binary_payload = action_request.SerializeToString()
    # Encode as hex for Nova HTTP transport
    return binascii.hexlify(binary_payload).decode("utf-8")
