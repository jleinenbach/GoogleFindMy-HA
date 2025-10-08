#
#  GoogleFindMyTools - Tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger.
#
from __future__ import annotations

from typing import Optional

from aiohttp import ClientSession

from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.sound_request import (
    create_sound_request,
)
from custom_components.googlefindmy.NovaApi.nova_request import async_nova_request
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE
from custom_components.googlefindmy.NovaApi.util import generate_random_uuid
from custom_components.googlefindmy.example_data_provider import get_example_data


def start_sound_request(canonic_device_id: str, gcm_registration_id: str) -> str:
    """Build the hex payload for a 'Play Sound' action (pure builder).

    This function returns the hex-encoded protobuf payload without performing network I/O.
    It is kept for backwards compatibility with code that wants to submit the payload itself.
    """
    request_uuid = generate_random_uuid()
    return create_sound_request(True, canonic_device_id, gcm_registration_id, request_uuid)


async def async_submit_start_sound_request(
    canonic_device_id: str,
    gcm_registration_id: str,
    *,
    session: Optional[ClientSession] = None,
) -> Optional[str]:
    """Submit a 'Play Sound' action using the shared async Nova client.

    Priority of session usage:
        explicit `session` argument > registered provider (inside Nova client) > ephemeral fallback.
    Returns the hex response (may be empty) or None on fatal error.
    """
    hex_payload = start_sound_request(canonic_device_id, gcm_registration_id)
    return await async_nova_request(NOVA_ACTION_API_SCOPE, hex_payload, session=session)


if __name__ == "__main__":
    # CLI helper (non-HA): obtain a token synchronously and submit via asyncio once.
    import asyncio
    from custom_components.googlefindmy.Auth.fcm_receiver import FcmReceiver  # sync-only CLI variant

    sample_canonic_device_id = get_example_data("sample_canonic_device_id")
    fcm_token = FcmReceiver().register_for_location_updates(lambda x: None)

    async def _main():
        await async_submit_start_sound_request(sample_canonic_device_id, fcm_token)

    asyncio.run(_main())
