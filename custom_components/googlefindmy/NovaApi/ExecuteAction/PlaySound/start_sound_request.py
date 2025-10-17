# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/start_sound_request.py
#
#  GoogleFindMyTools - Tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger.
#
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
from custom_components.googlefindmy.NovaApi.util import generate_random_uuid
from custom_components.googlefindmy.example_data_provider import get_example_data

# For optional entry-scoped TTL/cache wrapping when a namespace is provided.
from custom_components.googlefindmy.Auth.token_cache import (
    async_get_cached_value as _cache_get_default,
    async_set_cached_value as _cache_set_default,
)


def start_sound_request(canonic_device_id: str, gcm_registration_id: str) -> str:
    """Build the hex payload for a 'Play Sound' action (pure builder).

    This function performs no network I/O. It exists for backwards
    compatibility with code paths that submit the payload themselves.

    Args:
        canonic_device_id: The canonical ID of the target device.
        gcm_registration_id: The FCM registration token for push notifications.

    Returns:
        Hex-encoded protobuf payload for Nova transport.
    """
    request_uuid = generate_random_uuid()
    return create_sound_request(True, canonic_device_id, gcm_registration_id, request_uuid)


async def async_submit_start_sound_request(
    canonic_device_id: str,
    gcm_registration_id: str,
    *,
    session: Optional[ClientSession] = None,
    namespace: Optional[str] = None,
    # Optional flow-local overrides (kept here for parity with nova_request API)
    cache_get: Optional[Callable[[str], Awaitable[Any]]] = None,
    cache_set: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    refresh_override: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
) -> Optional[str]:
    """Submit a 'Play Sound' action using the shared async Nova client.

    This function handles the network request and robustly catches common API
    and network errors, returning None in those cases to prevent crashes.

    Entry-scope:
        If `namespace` is provided (e.g., the config entry_id), TTL metadata and
        related token-cache keys for this request will be read/written via
        namespaced wrappers to avoid collisions in multi-entry setups.

    Args:
        canonic_device_id: The canonical ID of the target device.
        gcm_registration_id: The FCM registration token for push notifications.
        session: Optional aiohttp ClientSession to reuse.
        namespace: Optional cache namespace (e.g., entry_id) for entry-scoped TTL/cache.
        cache_get/cache_set: Optional async cache I/O overrides (flow-local).
        refresh_override: Optional async function to obtain a fresh ADM token.

    Returns:
        A hex string of the response payload on success (can be empty),
        or None on any handled error (e.g., auth, rate-limit, server error,
        or network issues).
    """
    hex_payload = start_sound_request(canonic_device_id, gcm_registration_id)

    # If a namespace is supplied and no explicit cache overrides are given,
    # wrap the default token cache with a simple key prefix to achieve
    # entry-scoped separation for TTL metadata during this call.
    ns_get = cache_get
    ns_set = cache_set
    if namespace and (cache_get is None or cache_set is None):
        prefix = f"{namespace}:"

        async def _ns_get(key: str) -> Any:
            return await _cache_get_default(prefix + key)

        async def _ns_set(key: str, value: Any) -> None:
            await _cache_set_default(prefix + key, value)

        # Only override the ones not explicitly provided by the caller.
        ns_get = ns_get or _ns_get
        ns_set = ns_set or _ns_set

    try:
        # Pass through optional session and the (possibly namespaced) cache I/O.
        return await async_nova_request(
            NOVA_ACTION_API_SCOPE,
            hex_payload,
            session=session,
            cache_get=ns_get,
            cache_set=ns_set,
            refresh_override=refresh_override,
        )
    except asyncio.CancelledError:
        raise
    except NovaRateLimitError:
        # Transient; caller should treat as soft-fail
        return None
    except NovaHTTPError:
        # Transient server-side; caller should treat as soft-fail
        return None
    except NovaAuthError:
        # Auth required; caller may trigger re-auth UX
        return None
    except aiohttp.ClientError:
        # Local/network problem
        return None


if __name__ == "__main__":
    # CLI helper (non-HA): obtain a token synchronously and submit via asyncio once.
    async def _main():
        """Run a test execution of the start sound request for development."""
        from custom_components.googlefindmy.Auth.fcm_receiver import (  # sync-only CLI variant
            FcmReceiver,
        )

        sample_canonic_device_id = get_example_data("sample_canonic_device_id")
        fcm_token = FcmReceiver().register_for_location_updates(lambda x: None)

        await async_submit_start_sound_request(sample_canonic_device_id, fcm_token)

    asyncio.run(_main())
