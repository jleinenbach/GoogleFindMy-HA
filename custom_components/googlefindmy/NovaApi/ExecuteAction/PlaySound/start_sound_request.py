# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/start_sound_request.py
#
#  GoogleFindMyTools - Tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger.
#
from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, cast
from collections.abc import Callable, Awaitable

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
    return create_sound_request(
        True, canonic_device_id, gcm_registration_id, request_uuid
    )


async def async_submit_start_sound_request(
    canonic_device_id: str,
    gcm_registration_id: str,
    *,
    session: ClientSession | None = None,
    namespace: str | None = None,
    # NEW: entry-scoped content/metadata cache for multi-account setups
    cache: TokenCache | None = None,
    # Optional parity with nova_request (flow-local & overrides)
    username: str | None = None,
    token: str | None = None,
    cache_get: Callable[[str], Awaitable[Any]] | None = None,
    cache_set: Callable[[str, Any], Awaitable[None]] | None = None,
    refresh_override: Callable[[], Awaitable[str | None]] | None = None,
) -> str | None:
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


async def _async_cli_main(entry_id_hint: str | None = None) -> None:
    """Execute the CLI helper, enforcing explicit ConfigEntry selection."""

    from custom_components.googlefindmy.Auth.fcm_receiver import FcmReceiver
    from custom_components.googlefindmy.NovaApi.ListDevices import nbe_list_devices

    explicit_hint = entry_id_hint
    if explicit_hint is None:
        env_hint = os.environ.get("GOOGLEFINDMY_ENTRY_ID")
        if env_hint is not None:
            explicit_hint = env_hint.strip() or None

    cache, namespace = nbe_list_devices._resolve_cli_cache(explicit_hint)

    receiver = FcmReceiver(entry_id=namespace, cache=cache)

    sample_canonic_device_id = get_example_data("sample_canonic_device_id")
    fcm_token = receiver.register_for_location_updates(lambda x: None)
    if not isinstance(fcm_token, str) or not fcm_token:
        raise RuntimeError(
            "Unable to retrieve an FCM token for the selected entry. Ensure the "
            "account has valid credentials and try again."
        )

    await async_submit_start_sound_request(
        sample_canonic_device_id,
        fcm_token,
        cache=cache,
        namespace=namespace,
    )


if __name__ == "__main__":
    # CLI helper (non-HA): obtain an FCM token synchronously and submit via asyncio once.
    cli_entry = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        asyncio.run(_async_cli_main(cli_entry))
    except MissingTokenCacheError:
        print(
            "No token cache is available for the CLI helper. Provide the ConfigEntry "
            "ID via the first CLI argument or set GOOGLEFINDMY_ENTRY_ID.",
            file=sys.stderr,
        )
        sys.exit(1)
    except RuntimeError as err:
        print(err, file=sys.stderr)
        sys.exit(1)
    except ValueError as err:
        print(err, file=sys.stderr)
        sys.exit(1)
