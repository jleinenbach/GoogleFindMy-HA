# custom_components/googlefindmy/NovaApi/ListDevices/nbe_list_devices.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Handles fetching the list of Find My devices from the Nova API."""

from __future__ import annotations

import argparse
import asyncio
import binascii
import logging
import os
from typing import Any
from collections.abc import Callable, Awaitable

from aiohttp import ClientSession

from custom_components.googlefindmy.NovaApi.nova_request import async_nova_request
from custom_components.googlefindmy.NovaApi.scopes import NOVA_LIST_DEVICES_API_SCOPE
from custom_components.googlefindmy.NovaApi.util import generate_random_uuid
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    parse_device_list_protobuf,
    get_canonic_ids,
)
from custom_components.googlefindmy.Auth.token_cache import (
    TokenCache,  # entry-scoped cache
    get_cache_for_entry,
    get_registered_entry_ids,
)
from custom_components.googlefindmy.exceptions import (
    MissingNamespaceError,
    MissingTokenCacheError,
)

_LOGGER = logging.getLogger(__name__)


def create_device_list_request() -> str:
    """Build the protobuf request and return it as a hex string (transport payload).

    This function creates the serialized message needed to request a list of all
    Spot-enabled devices from the Nova API. It does not perform any network I/O.

    Returns:
        A hex-encoded string representing the serialized protobuf message.
    """
    wrapper = DeviceUpdate_pb2.DevicesListRequest()

    # Query for Spot devices only (keeps payload lean).
    wrapper.deviceListRequestPayload.type = DeviceUpdate_pb2.DeviceType.SPOT_DEVICE

    # Assign a random UUID as request id to help server-side correlation.
    wrapper.deviceListRequestPayload.id = generate_random_uuid()

    # Serialize to bytes and hex-encode for Nova transport.
    binary_payload = wrapper.SerializeToString()
    hex_payload = binascii.hexlify(binary_payload).decode("utf-8")
    return hex_payload


async def async_request_device_list(
    username: str | None = None,
    *,
    session: ClientSession | None = None,
    # Entry-scoped TokenCache (recommended in HA coordinators)
    cache: TokenCache | None = None,
    # Flow-local / entry-scoped overrides (all optional):
    token: str | None = None,
    cache_get: Callable[[str], Awaitable[Any]] | None = None,
    cache_set: Callable[[str, Any], Awaitable[None]] | None = None,
    refresh_override: Callable[[], Awaitable[str | None]] | None = None,
    namespace: str | None = None,
) -> str:
    """Asynchronously request the device list via Nova.

    This is the primary function for fetching the device list within Home Assistant,
    as it is non-blocking.

    Priority of HTTP session (HA best practice):
    1) Explicit `session` argument (tests/special cases),
    2) Registered provider from nova_request (uses HA's async_get_clientsession),
    3) Short-lived fallback session managed by nova_request.

    Entry-scope & cache isolation:
    - Preferred: pass the entry's TokenCache via `cache` plus a `namespace` (e.g., entry_id).
      In this mode, all username/ADM/TTL reads & writes remain strictly entry-local.
    - Alternatively (e.g. config flows), you can inject `cache_get`/`cache_set` (and optionally
      a `namespace`). When provided, these override the TokenCache for TTL metadata.

    Args:
        username: Google account username (email). If None, nova_request will
                  resolve it via its async cache helpers (entry-scoped when `cache` is set).
        session: aiohttp ClientSession to reuse (recommended in HA).
        cache: Entry-scoped TokenCache to enforce multi-account isolation.
        token: Optional direct ADM token for config-flow isolation.
        cache_get: Optional async getter for TTL/aux metadata (flow-local).
        cache_set: Optional async setter for TTL/aux metadata (flow-local).
        refresh_override: Optional async function to obtain a new token, isolated
                          from global/entry caches (e.g., AAS→ADM refresh during flows).
        namespace: Optional entry-scoped namespace used to prefix cache keys.

    Returns:
        Hex-encoded Nova response payload.

    Raises:
        MissingTokenCacheError: If no TokenCache is provided.
        MissingNamespaceError: If no namespace/entry ID can be determined.
        RuntimeError / aiohttp.ClientError on transport failures.
        Nova* errors bubble via nova_request (handled by callers).
    """
    if cache is None:
        raise MissingTokenCacheError()

    if namespace is None:
        inferred_namespace = getattr(cache, "entry_id", None) or getattr(
            cache, "namespace", None
        )
        if isinstance(inferred_namespace, str) and inferred_namespace.strip():
            namespace = inferred_namespace.strip()
        elif isinstance(cache, TokenCache):
            raise MissingNamespaceError()
        else:
            namespace = None

    hex_payload = create_device_list_request()

    # Optionally wrap flow-local cache I/O with a namespace (only if overrides are supplied).
    ns_get = cache_get
    ns_set = cache_set

    if namespace:
        ns_prefix = f"{namespace}:"
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

    # Delegate HTTP to Nova client (handles session provider & timeouts).
    # Pass through entry-scoped TokenCache (preferred) and the namespace.
    return await async_nova_request(
        NOVA_LIST_DEVICES_API_SCOPE,
        hex_payload,
        username=username,
        session=session,
        token=token,
        cache=cache,  # ← ensure entry-local reads/writes where available
        cache_get=ns_get,  # ← only used if provided; otherwise TokenCache is used
        cache_set=ns_set,  # ← only used if provided; otherwise TokenCache is used
        refresh_override=refresh_override,
        namespace=namespace,
    )


def request_device_list() -> str:
    """Synchronous convenience wrapper for CLI/legacy callers.

    NOTE:
    - This wrapper spins a private event loop via `asyncio.run(...)`.
    - Do NOT call from inside an active event loop (will raise RuntimeError).
    - In Home Assistant, prefer `await async_request_device_list(...)` and await it.
    - Requires `GOOGLEFINDMY_ENTRY_ID` to be set to the target config entry ID.

    Returns:
        The hex-encoded response from the Nova API.

    Raises:
        RuntimeError: If called from within a running asyncio event loop.
    """
    entry_hint = os.environ.get("GOOGLEFINDMY_ENTRY_ID")
    cache, namespace = _resolve_cli_cache(entry_hint)

    try:
        return asyncio.run(async_request_device_list(cache=cache, namespace=namespace))
    except RuntimeError as err:
        # This indicates incorrect usage (called from within a running loop).
        _LOGGER.error(
            "request_device_list() must not be called inside an active event loop. "
            "Use async_request_device_list(...) instead. Error: %s",
            err,
        )
        raise


# ------------------------------ CLI helper ---------------------------------
def _resolve_cli_cache(entry_id_hint: str | None) -> tuple[TokenCache, str]:
    """Return the entry-scoped cache/namespace for CLI usage or raise informative error."""

    entry_ids = get_registered_entry_ids()
    if not entry_ids:
        raise MissingTokenCacheError()

    if entry_id_hint is None or not entry_id_hint.strip():
        if len(entry_ids) > 1:
            available = ", ".join(sorted(entry_ids)) or "<none>"
            raise RuntimeError(
                "Multiple token caches registered. Provide the ConfigEntry ID via the "
                "CLI argument or set GOOGLEFINDMY_ENTRY_ID. Mehrere Token-Caches "
                "registriert. Bitte die ConfigEntry-ID per CLI angeben oder "
                "GOOGLEFINDMY_ENTRY_ID setzen. Available IDs: {avail}.".format(
                    avail=available
                )
            )
        raise MissingTokenCacheError()

    normalized = entry_id_hint.strip()

    try:
        cache = get_cache_for_entry(normalized)
    except KeyError as err:
        available = ", ".join(sorted(entry_ids)) or "<none>"
        raise RuntimeError(
            "Unknown entry_id '{entry}'. Available entry IDs: {avail}. "
            "Unbekannte Entry-ID '{entry}'. Verfügbare IDs: {avail}.".format(
                entry=normalized, avail=available
            )
        ) from err

    return cache, normalized


async def _async_cli_main(entry_id: str | None = None) -> None:
    """Asynchronous main function for the CLI experience (single event loop).

    This function provides an interactive command-line interface for fetching
    device locations or registering new microcontroller-based trackers.
    It is intended for development and testing purposes.
    """
    cache, namespace = _resolve_cli_cache(
        entry_id or os.environ.get("GOOGLEFINDMY_ENTRY_ID")
    )

    print("Loading...")
    result_hex = await async_request_device_list(cache=cache, namespace=namespace)

    device_list = parse_device_list_protobuf(result_hex)

    # Maintain side-effect helpers for Spot custom trackers.
    # NOTE: These imports are CLI-only to avoid heavy HA startup imports.
    from custom_components.googlefindmy.SpotApi.UploadPrecomputedPublicKeyIds.upload_precomputed_public_key_ids import (  # noqa: E501
        refresh_custom_trackers,
    )

    refresh_custom_trackers(device_list)
    canonic_ids = get_canonic_ids(device_list)

    print("")
    print("-" * 50)
    print("Welcome to GoogleFindMyTools!")
    print("-" * 50)
    print("")
    print("The following trackers are available:")

    for idx, (device_name, canonic_id) in enumerate(canonic_ids, start=1):
        print(f"{idx}. {device_name}: {canonic_id}")

    selected_value = input(
        "\nIf you want to see locations of a tracker, type the number of the tracker and press 'Enter'.\n"
        "If you want to register a new ESP32- or Zephyr-based tracker, type 'r' and press 'Enter': "
    )

    if selected_value == "r":
        print("Loading...")

        def _register_esp32_cli() -> None:
            """Synchronous helper to register a new ESP32 device."""
            # Lazy import to avoid touching spot token logic at HA startup
            from custom_components.googlefindmy.SpotApi.CreateBleDevice.create_ble_device import (
                register_esp32,
            )

            register_esp32()

        # Run potential blocking/IO work in a worker thread to avoid blocking the loop.
        await asyncio.to_thread(_register_esp32_cli)
    else:
        selected_idx = int(selected_value) - 1
        selected_device_name = canonic_ids[selected_idx][0]
        selected_canonic_id = canonic_ids[selected_idx][1]

        print("Fetching location...")

        # Lazy import: only needed for the CLI branch
        from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (  # noqa: E501
            get_location_data_for_device,
        )

        await get_location_data_for_device(
            selected_canonic_id,
            selected_device_name,
            cache=cache,
            namespace=namespace,
        )


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments for entry selection."""

    parser = argparse.ArgumentParser(description="Google Find My Device CLI helper")
    parser.add_argument(
        "--entry",
        help=("Config entry ID to target. Alternatively set GOOGLEFINDMY_ENTRY_ID."),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    # This block allows the script to be run directly from the command line
    # for testing or manual device registration.
    try:
        args = _parse_cli_args()
        entry_hint = args.entry or os.environ.get("GOOGLEFINDMY_ENTRY_ID")
        asyncio.run(_async_cli_main(entry_hint))
    except KeyboardInterrupt:
        print("\nExiting.")
