# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/stop_sound_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Handles sending a 'Stop Sound' command for a Google Find My Device."""
from __future__ import annotations

import asyncio
from typing import Optional, Callable, Awaitable, Any

import aiohttp
from aiohttp import ClientSession

from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.sound_request import (
    create_sound_request,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    async_nova_request,
    NovaAuthError,
    NovaRateLimitError,
    NovaHTTPError,
)
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE
from custom_components.googlefindmy.example_data_provider import get_example_data


def stop_sound_request(canonic_device_id: str, gcm_registration_id: str) -> str:
    """Build the hex payload for a 'Stop Sound' action (pure builder).

    This function performs no network I/O. It creates the serialized protobuf
    message required to stop a sound on a device.

    Args:
        canonic_device_id: The canonical ID of the target device.
        gcm_registration_id: The FCM registration token for push notifications.

    Returns:
        Hex-encoded protobuf payload for Nova transport.
    """
    return create_sound_request(False, canonic_device_id, gcm_registration_id)


async def async_submit_stop_sound_request(
    canonic_device_id: str,
    gcm_registration_id: str,
    *,
    session: Optional[ClientSession] = None,
    # Entry-scope & flow-friendly optional parameters (all pass-through / optional):
    namespace: Optional[str] = None,
    username: Optional[str] = None,
    token: Optional[str] = None,
    cache_get: Optional[Callable[[str], Awaitable[Any]]] = None,
    cache_set: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    refresh_override: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
) -> Optional[str]:
    """Submit a 'Stop Sound' action using the shared async Nova client.

    This function handles the network request and robustly catches common API
    and network errors, returning None in those cases to prevent crashes.

    Notes on parameters:
        - ``session``: Optional aiohttp session reuse. Supported by the Nova client.
        - ``namespace``: Entry-scope hint (e.g., config_entry.entry_id). Forwarded to
          newer Nova clients when supported; safely ignored on older ones.
        - ``username``, ``token``, ``cache_get``, ``cache_set``, ``refresh_override``:
          Optional overrides to support isolated flow validation or custom cache I/O.

    Args:
        canonic_device_id: The canonical ID of the target device.
        gcm_registration_id: The FCM registration token for push notifications.
        session: Optional aiohttp ClientSession to reuse (preferred in HA).
        namespace: Optional entry-scoped namespace to avoid cross-entry cache bleed.
        username: Optional Google account e-mail for the request context.
        token: Optional direct ADM token to bypass cache for this call.
        cache_get: Optional async getter for TTL/aux metadata (flow-local).
        cache_set: Optional async setter for TTL/aux metadata (flow-local).
        refresh_override: Optional async function producing a fresh ADM token.

    Returns:
        A hex string of the response payload on success (can be empty),
        or None on any handled error (e.g., auth, rate-limit, server error,
        or network issues).
    """
    hex_payload = stop_sound_request(canonic_device_id, gcm_registration_id)
    try:
        # Prefer the most recent Nova client signature that accepts `namespace=...`.
        try:
            return await async_nova_request(
                NOVA_ACTION_API_SCOPE,
                hex_payload,
                username=username,
                session=session,
                token=token,
                cache_get=cache_get,
                cache_set=cache_set,
                refresh_override=refresh_override,
                namespace=namespace,  # may raise TypeError on older clients
            )
        except TypeError:
            # Backward-compat path: older Nova clients without `namespace`.
            return await async_nova_request(
                NOVA_ACTION_API_SCOPE,
                hex_payload,
                username=username,
                session=session,
                token=token,
                cache_get=cache_get,
                cache_set=cache_set,
                refresh_override=refresh_override,
            )

    except asyncio.CancelledError:
        raise
    except NovaRateLimitError:
        # transient; caller should treat as soft-fail
        return None
    except NovaHTTPError:
        # transient server-side; caller should treat as soft-fail
        return None
    except NovaAuthError:
        # auth required; caller may trigger re-auth UX
        return None
    except aiohttp.ClientError:
        # local/network problem
        return None


if __name__ == "__main__":
    # This block serves as a CLI helper for standalone testing and development.
    # It obtains an FCM token synchronously and then runs the async submission
    # function in a new event loop.
    async def _main():
        """Run a test execution of the stop sound request for development."""
        from custom_components.googlefindmy.Auth.fcm_receiver import FcmReceiver  # sync-only CLI variant

        sample_canonic_device_id = get_example_data("sample_canonic_device_id")
        fcm_token = FcmReceiver().register_for_location_updates(lambda x: None)

        await async_submit_stop_sound_request(sample_canonic_device_id, fcm_token)

    asyncio.run(_main())
