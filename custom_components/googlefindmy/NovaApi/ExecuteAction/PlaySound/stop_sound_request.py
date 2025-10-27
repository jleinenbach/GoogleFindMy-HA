# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/stop_sound_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Handles sending a 'Stop Sound' command for a Google Find My Device."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any
from collections.abc import Awaitable, Callable

import aiohttp
from aiohttp import ClientSession

from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound.sound_request import (
    create_sound_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound._cli_helpers import (
    async_fetch_cli_fcm_token,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    async_nova_request,
    NovaAuthError,
    NovaRateLimitError,
    NovaHTTPError,
)
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE
from custom_components.googlefindmy.example_data_provider import get_example_data

from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.exceptions import MissingTokenCacheError


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
    session: ClientSession | None = None,
    # Entry-scope & flow-friendly optional parameters (all pass-through / optional):
    namespace: str | None = None,
    username: str | None = None,
    token: str | None = None,
    cache_get: Callable[[str], Awaitable[Any]] | None = None,
    cache_set: Callable[[str, Any], Awaitable[None]] | None = None,
    refresh_override: Callable[[], Awaitable[str | None]] | None = None,
    # NEW: entry-scoped TokenCache to keep credentials and TTL metadata local
    cache: TokenCache | None = None,
) -> str | None:
    """Submit a 'Stop Sound' action using the shared async Nova client.

    This function handles the network request and robustly catches common API
    and network errors, returning None in those cases to prevent crashes.

    Notes on parameters:
        - ``session``: Optional aiohttp session reuse. Supported by the Nova client.
        - ``namespace``: Entry-scope hint (e.g., config_entry.entry_id). Helps avoid
          cross-entry cache bleed for TTL metadata and related keys.
        - ``cache``: Entry-scoped TokenCache for **username**, **ADM token content**,
          and (when not overridden) **TTL metadata** I/O.
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
        cache: Optional entry-local TokenCache (multi-account safe).

    Returns:
        A hex string of the response payload on success (can be empty),
        or None on any handled error (e.g., auth, rate-limit, server error,
        or network issues).
    """
    hex_payload = stop_sound_request(canonic_device_id, gcm_registration_id)
    if cache is None:
        raise MissingTokenCacheError()

    resolved_namespace = namespace or getattr(cache, "entry_id", None)

    ns_get = cache_get
    ns_set = cache_set

    if resolved_namespace and (ns_get is None or ns_set is None):
        ns_prefix = f"{resolved_namespace}:"

        if ns_get is None:

            async def _ns_get(key: str) -> Any:
                return await cache.async_get_cached_value(f"{ns_prefix}{key}")

            ns_get = _ns_get

        if ns_set is None:

            async def _ns_set(key: str, value: Any) -> None:
                await cache.async_set_cached_value(f"{ns_prefix}{key}", value)

            ns_set = _ns_set
    else:
        if ns_get is None:
            ns_get = cache.async_get_cached_value
        if ns_set is None:
            ns_set = cache.async_set_cached_value

    try:
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
            cache=cache,
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


async def _async_cli_main(entry_id_hint: str | None = None) -> None:
    """Execute the CLI helper for Stop Sound, enforcing explicit entry selection."""

    from custom_components.googlefindmy.NovaApi.ListDevices import nbe_list_devices

    explicit_hint = entry_id_hint
    if explicit_hint is None:
        env_hint = os.environ.get("GOOGLEFINDMY_ENTRY_ID")
        if env_hint is not None:
            explicit_hint = env_hint.strip() or None

    cache, namespace = nbe_list_devices._resolve_cli_cache(explicit_hint)

    sample_canonic_device_id = get_example_data("sample_canonic_device_id")
    fcm_token = await async_fetch_cli_fcm_token(cache, namespace)
    if not isinstance(fcm_token, str) or not fcm_token:
        raise RuntimeError(
            "Unable to retrieve an FCM token for the selected entry. Ensure the "
            "account has valid credentials and try again."
        )

    await async_submit_stop_sound_request(
        sample_canonic_device_id,
        fcm_token,
        cache=cache,
        namespace=namespace,
    )


if __name__ == "__main__":
    # This block serves as a CLI helper for standalone testing and development.
    # It obtains an FCM token synchronously and then runs the async submission
    # function in a new event loop.
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
