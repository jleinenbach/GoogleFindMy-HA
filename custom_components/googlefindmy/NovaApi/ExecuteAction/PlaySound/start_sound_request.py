# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/start_sound_request.py
#
#  GoogleFindMyTools - Tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger.
#
from __future__ import annotations

import asyncio
from typing import Optional, Callable, Awaitable, Any, cast

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

from custom_components.googlefindmy.exceptions import MissingTokenCacheError

from custom_components.googlefindmy.Auth.token_cache import TokenCache


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
    # NEW: entry-scoped content/metadata cache for multi-account setups
    cache: Optional[TokenCache] = None,
    # Optional parity with nova_request (flow-local & overrides)
    username: Optional[str] = None,
    token: Optional[str] = None,
    cache_get: Optional[Callable[[str], Awaitable[Any]]] = None,
    cache_set: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    refresh_override: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
) -> Optional[str]:
    """Submit a 'Play Sound' action using the shared async Nova client.

    This function handles the network request and robustly catches common API
    and network errors, returning None in those cases to prevent crashes.

    Entry-scope:
        - If `namespace` is provided and no explicit `cache_get`/`cache_set` are given,
          TTL metadata keys are prefixed with `"{namespace}:"`.
        - If `cache` is provided, `async_nova_request` will read/write **username**,
          **ADM token content**, and (if no overrides were provided) **TTL metadata**
          via that entry-local TokenCache.

    Args:
        canonic_device_id: Target device canonical ID.
        gcm_registration_id: FCM registration token for push correlation.
        session: Optional aiohttp session to reuse.
        namespace: Optional cache namespace (e.g., entry_id) to avoid cross-entry collisions.
        cache: Optional entry-scoped TokenCache (multi-account safe).
        username: Optional Google account e-mail (else resolved by nova_request).
        token: Optional direct ADM token to bypass cache lookups for this call.
        cache_get/cache_set: Optional async cache I/O overrides (flow-local).
        refresh_override: Optional async function to obtain a fresh ADM token.

    Returns:
        Hex response payload on success (may be empty) or None on handled errors.
    """
    # Build payload (pure)
    hex_payload = start_sound_request(canonic_device_id, gcm_registration_id)

    # Prepare optional namespaced TTL cache wrappers if requested and not overridden
    if cache is None:
        raise MissingTokenCacheError()

    cache_ref = cast(TokenCache, cache)

    resolved_namespace = namespace or getattr(cache_ref, "entry_id", None)

    ns_get = cache_get
    ns_set = cache_set

    if resolved_namespace and (ns_get is None or ns_set is None):
        ns_prefix = f"{resolved_namespace}:"

        if ns_get is None:

            async def _ns_get(key: str) -> Any:
                return await cache_ref.async_get_cached_value(f"{ns_prefix}{key}")

            ns_get = _ns_get

        if ns_set is None:

            async def _ns_set(key: str, value: Any) -> None:
                await cache_ref.async_set_cached_value(f"{ns_prefix}{key}", value)

            ns_set = _ns_set
    else:
        if ns_get is None:
            ns_get = cache_ref.async_get_cached_value
        if ns_set is None:
            ns_set = cache_ref.async_set_cached_value

    try:
        # Submit via Nova (now entry-scoped when `cache`/`namespace` provided)
        return await async_nova_request(
            NOVA_ACTION_API_SCOPE,
            hex_payload,
            username=username,
            session=session,
            token=token,
            cache_get=ns_get,
            cache_set=ns_set,
            refresh_override=refresh_override,
            namespace=resolved_namespace,
            cache=cache_ref,
        )
    except asyncio.CancelledError:
        raise
    except NovaRateLimitError:
        return None
    except NovaHTTPError:
        return None
    except NovaAuthError:
        return None
    except aiohttp.ClientError:
        return None


if __name__ == "__main__":
    # CLI helper (non-HA): obtain an FCM token synchronously and submit via asyncio once.
    async def _main():
        """Run a test execution of the start sound request for development."""
        from custom_components.googlefindmy.Auth.fcm_receiver import (  # sync-only CLI variant
            FcmReceiver,
        )

        sample_canonic_device_id = get_example_data("sample_canonic_device_id")
        fcm_token = FcmReceiver().register_for_location_updates(lambda x: None)

        class _CliTokenCache:
            """Minimal in-memory TokenCache shim for CLI experiments."""

            def __init__(self) -> None:
                self.entry_id = "cli"
                self._values: dict[str, Any] = {}

            async def async_get_cached_value(self, key: str) -> Any:
                return self._values.get(key)

            async def async_set_cached_value(self, key: str, value: Any) -> None:
                self._values[key] = value

        await async_submit_start_sound_request(
            sample_canonic_device_id,
            fcm_token,
            cache=_CliTokenCache(),
        )

    asyncio.run(_main())
