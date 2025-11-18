# custom_components/googlefindmy/NovaApi/ExecuteAction/LocateTracker/location_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections.abc import Awaitable, Callable
from importlib import import_module
from types import ModuleType
from typing import Any, Protocol, cast, runtime_checkable

import aiohttp

# Keep heavy/protobuf-related imports lazy (done inside functions/callbacks)
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.const import (
    CACHE_KEY_CONTRIBUTOR_MODE,
    CACHE_KEY_LAST_MODE_SWITCH,
    CONTRIBUTOR_MODE_HIGH_TRAFFIC,
    CONTRIBUTOR_MODE_IN_ALL_AREAS,
    DEFAULT_CONTRIBUTOR_MODE,
    LOCATION_REQUEST_TIMEOUT_S,
)
from custom_components.googlefindmy.example_data_provider import get_example_data
from custom_components.googlefindmy.exceptions import (
    MissingNamespaceError,
    MissingTokenCacheError,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
    create_action_request,
    serialize_action_request,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaAuthError,
    NovaHTTPError,
    NovaRateLimitError,
    async_nova_request,
)
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE
from custom_components.googlefindmy.NovaApi.util import generate_random_uuid

_LOGGER = logging.getLogger(__name__)


def _normalize_contributor_mode(mode: str | None) -> str:
    """Return a sanitized contributor mode string."""

    if isinstance(mode, str):
        normalized = mode.strip().lower()
        if normalized in (
            CONTRIBUTOR_MODE_HIGH_TRAFFIC,
            CONTRIBUTOR_MODE_IN_ALL_AREAS,
        ):
            return normalized
    return DEFAULT_CONTRIBUTOR_MODE


# -----------------------------------------------------------------------------
# FCM receiver provider (registered by integration setup; unloaded on teardown)
# -----------------------------------------------------------------------------
@runtime_checkable
class FcmReceiverProtocol(Protocol):
    """Defines the interface for the FCM receiver."""

    async def async_register_for_location_updates(
        self, device_id: str, callback: Callable[[str, str], None]
    ) -> str | None: ...
    async def async_unregister_for_location_updates(self, device_id: str) -> None: ...


def _import_deviceupdate_pb2() -> ModuleType:
    return cast(
        ModuleType,
        import_module("custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2"),
    )


def _import_decoder_module() -> ModuleType:
    return cast(
        ModuleType,
        import_module("custom_components.googlefindmy.ProtoDecoders.decoder"),
    )


def _import_decrypt_locations_module() -> ModuleType:
    return cast(
        ModuleType,
        import_module(
            "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations"
        ),
    )


def _import_eid_info_module() -> ModuleType:
    return cast(
        ModuleType,
        import_module(
            "custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request"
        ),
    )


_fcm_receiver_state: dict[str, Callable[[], FcmReceiverProtocol] | None] = {
    "getter": None
}


def register_fcm_receiver_provider(getter: Callable[[], FcmReceiverProtocol]) -> None:
    """Register a callable returning the long-lived FCM receiver instance.

    The getter must return an initialized receiver exposing:
      - async_register_for_location_updates(device_id, callback) -> str | None
      - async_unregister_for_location_updates(device_id) -> None

    Args:
        getter: A callable that returns the singleton FCM receiver instance.
    """
    _fcm_receiver_state["getter"] = getter


def unregister_fcm_receiver_provider() -> None:
    """Unregister the FCM receiver provider (called on integration unload)."""
    _fcm_receiver_state["getter"] = None


def create_location_request(
    canonic_device_id: str,
    fcm_registration_id: str,
    request_uuid: str,
    *,
    contributor_mode: str = DEFAULT_CONTRIBUTOR_MODE,
    last_mode_switch: int | None = None,
) -> str:
    """Build and serialize a LocateTracker action request.

    DeviceUpdate_pb2 is imported lazily here to avoid protobuf side effects
    at module import time (important inside Home Assistant).

    Args:
        canonic_device_id: The canonical ID of the target device.
        fcm_registration_id: The FCM token for push notifications.
        request_uuid: A unique identifier for this request.
        contributor_mode: Contributor preference ("high_traffic" or "in_all_areas").
        last_mode_switch: Epoch timestamp when the contributor mode last changed.

    Returns:
        A hex-encoded string representing the serialized protobuf message.
    """
    device_update_pb2 = _import_deviceupdate_pb2()

    normalized_mode = _normalize_contributor_mode(contributor_mode)
    if last_mode_switch is None or last_mode_switch <= 0:
        last_mode_switch = int(time.time())

    action_request = create_action_request(
        canonic_device_id, fcm_registration_id, request_uuid=request_uuid
    )

    action_request.action.locateTracker.lastHighTrafficEnablingTime.seconds = int(
        last_mode_switch
    )

    proto_mode_map = {
        CONTRIBUTOR_MODE_HIGH_TRAFFIC: device_update_pb2.SpotContributorType.FMDN_HIGH_TRAFFIC,
        CONTRIBUTOR_MODE_IN_ALL_AREAS: device_update_pb2.SpotContributorType.FMDN_ALL_LOCATIONS,
    }
    action_request.action.locateTracker.contributorType = proto_mode_map.get(
        normalized_mode,
        device_update_pb2.SpotContributorType.FMDN_ALL_LOCATIONS,
    )

    # Convert to hex string
    hex_payload = serialize_action_request(action_request)

    return hex_payload


# -----------------------------------------------------------------------------
# Internal callback context and factory
# -----------------------------------------------------------------------------
class _CallbackContext:
    """Explicit context shared between the FCM callback and awaiting task.

    This class holds the state needed to pass data from the asynchronous FCM
    callback (which may run in a different thread) to the awaiting coroutine.

    Attributes:
        event: An asyncio.Event to signal that data has been received.
        data: The data payload received from the callback.
    """

    __slots__ = ("event", "data")
    event: asyncio.Event
    data: list[dict[str, Any]] | None

    def __init__(self) -> None:
        """Initialize the callback context."""
        self.event: asyncio.Event = asyncio.Event()
        self.data: list[dict[str, Any]] | None = None


def _make_location_callback(  # noqa: PLR0915
    *,
    name: str,
    canonic_device_id: str,
    ctx: _CallbackContext,
    loop: asyncio.AbstractEventLoop,
    cache: TokenCache,
) -> Callable[[str, str], None]:
    """Factory that creates an FCM callback bound to a context object.

    This function generates a callback that will be invoked by the FCM receiver
    when a location update is received.

    Design:
      - The receiver triggers this callback in a worker thread.
      - We parse in this thread and then hand off CPU-heavy/async work
        (decryption & normalization) to the main HA loop with
        `asyncio.run_coroutine_threadsafe(...)`.

    Args:
        name: The human-readable name of the device for logging.
        canonic_device_id: The canonical ID of the device to validate the response.
        ctx: The shared context object for signaling and data transfer.
        loop: The asyncio event loop of the main thread.

    Returns:
        A callback function suitable for the FCM receiver.
    """

    def location_callback(  # noqa: PLR0915
        response_canonic_id: str, hex_response: str
    ) -> None:
        """Processes the location update received via FCM."""
        try:
            _LOGGER.info("FCM callback triggered for %s, processing response...", name)
            _LOGGER.debug("FCM response length: %d chars", len(hex_response))

            # Lazy imports inside callback (avoid protobuf import side effects during HA startup)
            try:
                decoder_module = _import_decoder_module()
                decrypt_module = _import_decrypt_locations_module()
                eid_info_module = _import_eid_info_module()
            except ImportError as import_error:
                _LOGGER.error(
                    "Failed to import decoder/decrypt functions in callback for %s: %s",
                    name,
                    import_error,
                )
                ctx.data = cast(list[dict[str, Any]], [])
                ctx.event.set()
                return

            try:
                parse_device_update_protobuf = cast(
                    Callable[[str], Any],
                    getattr(decoder_module, "parse_device_update_protobuf"),
                )
                async_decrypt_location_response_locations = cast(
                    Callable[[Any], Awaitable[list[dict[str, Any]]]],
                    getattr(
                        decrypt_module,
                        "async_decrypt_location_response_locations",
                    ),
                )
                DecryptionError = cast(
                    type[Exception], getattr(decrypt_module, "DecryptionError")
                )
                StaleOwnerKeyError = cast(
                    type[Exception], getattr(decrypt_module, "StaleOwnerKeyError")
                )
                SpotApiEmptyResponseError = cast(
                    type[Exception],
                    getattr(eid_info_module, "SpotApiEmptyResponseError"),
                )
            except AttributeError as import_error:
                _LOGGER.error(
                    "Failed to load decoder/decrypt attributes in callback for %s: %s",
                    name,
                    import_error,
                )
                ctx.data = cast(list[dict[str, Any]], [])
                ctx.event.set()
                return

            # Parse the hex response in this worker thread
            try:
                device_update = parse_device_update_protobuf(hex_response)
            except Exception as parse_exc:
                _LOGGER.error(
                    "Failed to parse device update for %s: %s", name, parse_exc
                )
                ctx.data = cast(list[dict[str, Any]], [])
                ctx.event.set()
                return

            # Validate canonic_id matches what we requested
            if response_canonic_id != canonic_device_id:
                _LOGGER.warning(
                    "FCM callback received data for %s, but we requested %s. Ignoring.",
                    response_canonic_id,
                    canonic_device_id,
                )
                return

            async def _decrypt_and_store() -> None:
                """Asynchronous part of the callback to decrypt and store data."""
                try:
                    location_data: list[dict[str, Any]] = await async_decrypt_location_response_locations(
                        device_update, cache=cache
                    )
                except (
                    StaleOwnerKeyError,
                    DecryptionError,
                    SpotApiEmptyResponseError,
                ) as err:
                    _LOGGER.error(
                        "Failed to process location data for %s: %s", name, err
                    )
                    ctx.data = cast(list[dict[str, Any]], [])
                    ctx.event.set()
                    return
                except Exception as err:
                    _LOGGER.error(
                        "Unexpected error during async decryption for %s: %s", name, err
                    )
                    _LOGGER.debug("Traceback: %s", traceback.format_exc())
                    ctx.data = cast(list[dict[str, Any]], [])
                    ctx.event.set()
                    return

                if location_data:
                    _LOGGER.info(
                        "Successfully decrypted %d location record(s) for %s",
                        len(location_data),
                        name,
                    )
                    # Attach canonic_id for validation after wait
                    location_data[0]["canonic_id"] = response_canonic_id
                    ctx.data = location_data
                else:
                    _LOGGER.warning(
                        "No location data found after decryption for %s", name
                    )
                    ctx.data = cast(list[dict[str, Any]], [])
                ctx.event.set()

            # Hand off to the HA event loop; do not block this worker thread.
            asyncio.run_coroutine_threadsafe(_decrypt_and_store(), loop)

        except Exception as callback_error:
            _LOGGER.error(
                "Error processing FCM callback for %s: %s", name, callback_error
            )
            _LOGGER.debug("FCM callback traceback: %s", traceback.format_exc())
            ctx.data = cast(list[dict[str, Any]], [])
            ctx.event.set()

    return location_callback


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
async def get_location_data_for_device(  # noqa: PLR0911, PLR0912, PLR0913, PLR0915
    canonic_device_id: str,
    name: str,
    session: aiohttp.ClientSession | None = None,
    *,
    username: str | None = None,
    # Entry-scope extensions (all optional / backward compatible):
    token: str | None = None,
    cache_get: Callable[[str], Awaitable[Any]] | None = None,
    cache_set: Callable[[str, Any], Awaitable[None]] | None = None,
    refresh_override: Callable[[], Awaitable[str | None]] | None = None,
    namespace: str | None = None,
    cache: TokenCache | None = None,
    contributor_mode: str | None = None,
    last_mode_switch: int | None = None,
) -> list[dict[str, Any]]:
    """Get location data for a device (async, HA-compatible).

    Orchestrates:
    - Register a temporary callback with the FCM receiver,
    - Send the locate request via Nova,
    - Await the FCM-delivered response and decrypt it.

    Multi-entry safety
    ------------------
    - When `namespace` is provided (e.g., entry_id), **TTL metadata** is namespaced.
    - When `cache` is provided, `async_nova_request` will prefer that entry-local
      TokenCache for **username**, **token content**, and (if no `cache_get/set`
      overrides are provided) **TTL metadata** as well.

    Args:
        canonic_device_id: The canonical ID of the device to locate.
        name: The human-readable name of the device for logging purposes.
        session: An optional aiohttp.ClientSession to reuse for the Nova call.
        username: The username for the request.
        token: Optional, direct ADM token for flow-local validation.
        cache_get: Optional async getter for TTL/aux metadata.
        cache_set: Optional async setter for TTL/aux metadata.
        refresh_override: Optional async function to refresh a token in isolation.
        namespace: Optional entry-scoped namespace (e.g., config_entry.entry_id).
        cache: TokenCache providing entry-scoped username/token/metadata storage.
        contributor_mode: Contributor preference ("high_traffic" or "in_all_areas").
        last_mode_switch: Epoch timestamp when the contributor mode last changed.

    Returns:
        A list of dictionaries containing location data, or an empty list on failure.
    """
    _LOGGER.info("Requesting location data for %s...", name)

    # Fail hard on missing/misconfigured provider: this is a programming/config error.
    fcm_getter = _fcm_receiver_state["getter"]

    if fcm_getter is None:
        raise RuntimeError("FCM receiver provider has not been registered.")
    fcm_receiver = fcm_getter()
    if fcm_receiver is None:
        raise RuntimeError("FCM receiver provider returned None.")

    registered = False
    ctx = _CallbackContext()
    loop = asyncio.get_running_loop()

    if cache is None:
        raise MissingTokenCacheError()

    cache_ref = cache

    resolved_namespace = namespace or getattr(cache_ref, "entry_id", None)
    if not resolved_namespace:
        raise MissingNamespaceError()

    async def _cache_get_raw(key: str) -> Any:
        getter = getattr(cache_ref, "async_get_cached_value", None)
        if callable(getter):
            return await getter(key)
        fallback = getattr(cache_ref, "get", None)
        if callable(fallback):
            result = fallback(key)
            if asyncio.iscoroutine(result):
                return await result
            return result
        return None

    async def _cache_set_raw(key: str, value: Any) -> None:
        setter = getattr(cache_ref, "async_set_cached_value", None)
        if callable(setter):
            await setter(key, value)
            return
        fallback = getattr(cache_ref, "set", None)
        if callable(fallback):
            result = fallback(key, value)
            if asyncio.iscoroutine(result):
                await result

    # Build cache accessors, preferring entry-local namespacing when provided.
    ns_get: Callable[[str], Awaitable[Any]] | None = cache_get
    ns_set: Callable[[str, Any], Awaitable[None]] | None = cache_set

    if resolved_namespace and (cache_get is None or cache_set is None):
        ns_prefix = f"{resolved_namespace}:"

        if ns_get is None:

            async def _ns_get(key: str) -> Any:
                return await _cache_get_raw(f"{ns_prefix}{key}")

            ns_get = _ns_get

        if ns_set is None:

            async def _ns_set(key: str, value: Any) -> None:
                await _cache_set_raw(f"{ns_prefix}{key}", value)

            ns_set = _ns_set
    else:
        if ns_get is None:
            ns_get = _cache_get_raw
        if ns_set is None:
            ns_set = _cache_set_raw

    if ns_get is None or ns_set is None:
        raise RuntimeError("Cache accessors could not be initialized.")

    mode = _normalize_contributor_mode(contributor_mode)
    resolved_last_mode_switch: int | None = (
        int(last_mode_switch)
        if isinstance(last_mode_switch, (int, float)) and last_mode_switch > 0
        else None
    )

    try:
        cached_mode = await ns_get(CACHE_KEY_CONTRIBUTOR_MODE)
    except Exception as err:  # pragma: no cover - defensive logging
        _LOGGER.debug("Failed to load contributor mode from cache: %s", err)
        cached_mode = None

    if contributor_mode is None and isinstance(cached_mode, str):
        mode = _normalize_contributor_mode(cached_mode)
    elif contributor_mode is not None:
        try:
            await ns_set(CACHE_KEY_CONTRIBUTOR_MODE, mode)
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug("Failed to persist contributor mode override: %s", err)

    if resolved_last_mode_switch is None:
        try:
            cached_switch = await ns_get(CACHE_KEY_LAST_MODE_SWITCH)
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug("Failed to load contributor mode timestamp: %s", err)
        else:
            if isinstance(cached_switch, (int, float)) and cached_switch > 0:
                resolved_last_mode_switch = int(cached_switch)

    if resolved_last_mode_switch is None:
        resolved_last_mode_switch = int(time.time())

    try:
        await ns_set(
            CACHE_KEY_LAST_MODE_SWITCH, resolved_last_mode_switch
        )
    except Exception as err:  # pragma: no cover - defensive logging
        _LOGGER.debug("Failed to persist contributor mode timestamp: %s", err)

    try:
        # Generate request UUID
        request_uuid = generate_random_uuid()

        # Register the callback with the shared receiver
        try:
            _LOGGER.debug("Registering FCM location updates for %s...", name)
            callback = _make_location_callback(
                name=name,
                canonic_device_id=canonic_device_id,
                ctx=ctx,
                loop=loop,
                cache=cache_ref,
            )
            fcm_token = await fcm_receiver.async_register_for_location_updates(
                canonic_device_id, callback
            )
            if not fcm_token:
                _LOGGER.error("Failed to get FCM token for %s", name)
                return []
            registered = True
            _LOGGER.debug("FCM token obtained for %s (len=%d)", name, len(fcm_token))
        except Exception as fcm_error:
            _LOGGER.error("FCM registration failed for %s: %s", name, fcm_error)
            _LOGGER.debug("FCM registration traceback: %s", traceback.format_exc())
            return []

        # Create location request payload
        hex_payload = create_location_request(
            canonic_device_id,
            fcm_token,
            request_uuid,
            contributor_mode=mode,
            last_mode_switch=resolved_last_mode_switch,
        )

        # Send location request to Google API (async; HA session preferred if provided)
        _LOGGER.info("Sending location request to Google API for %s...", name)
        try:
            _ = await async_nova_request(
                NOVA_ACTION_API_SCOPE,
                hex_payload,
                username=username,
                session=session,
                token=token,
                cache_get=ns_get,
                cache_set=ns_set,
                refresh_override=refresh_override,
                namespace=resolved_namespace,
                cache=cache_ref,  # pass entry-scoped TokenCache through
            )
        except asyncio.CancelledError:
            raise
        except NovaRateLimitError as e:
            _LOGGER.warning(
                "Rate limited while requesting location for %s: %s", name, e
            )
            return []
        except NovaHTTPError as e:
            _LOGGER.warning(
                "Server error (%s) while requesting location for %s: %s",
                e.status,
                name,
                e,
            )
            return []
        except NovaAuthError as e:
            _LOGGER.error(
                "Authentication error while requesting location for %s: %s", name, e
            )
            return []
        except aiohttp.ClientError as e:
            _LOGGER.warning(
                "Network/client error while requesting location for %s: %s", name, e
            )
            return []
        except Exception as e:
            _LOGGER.error("Nova API request failed for %s: %s", name, e)
            return []

        # For this RPC the server often returns HTTP 200 with empty body (FCM delivers the data).
        _LOGGER.info("Location request accepted for %s; awaiting FCM data...", name)

        # Wait efficiently for FCM callback to signal completion
        timeout = LOCATION_REQUEST_TIMEOUT_S
        _LOGGER.info("Waiting for location response for %s...", name)
        try:
            await asyncio.wait_for(ctx.event.wait(), timeout=timeout)
        except TimeoutError:
            _LOGGER.warning(
                "No location response received for %s (timeout: %ss)", name, timeout
            )
            return []

        data = ctx.data or []
        if data and data[0].get("canonic_id") == canonic_device_id:
            _LOGGER.info("Successfully received location data for %s", name)
            return data
        if not data:
            _LOGGER.warning("No location data found for %s after decryption", name)
        else:
            _LOGGER.warning(
                "Received location data for unexpected device in %s flow; ignoring.",
                name,
            )
        return []

    except asyncio.CancelledError:
        _LOGGER.info("Location request cancelled for %s", name)
        raise
    except Exception as e:
        _LOGGER.error("Error requesting location for %s: %s", name, e)
        _LOGGER.debug("Traceback: %s", traceback.format_exc())
        return []
    finally:
        # Clean up - unregister callback only (receiver lifecycle is owned by integration)
        try:
            if registered:
                await fcm_receiver.async_unregister_for_location_updates(
                    canonic_device_id
                )
        except Exception as cleanup_error:
            _LOGGER.warning(
                "Error during FCM unregister for %s: %s", name, cleanup_error
            )


if __name__ == "__main__":
    # CLI invocation will fail unless an external provider is registered; kept for parity.

    class _CliTokenCache:
        """Minimal in-memory TokenCache shim for CLI experiments."""

        def __init__(self) -> None:
            self.entry_id = "cli"
            self._values: dict[str, Any] = {}

        async def async_get_cached_value(self, key: str) -> Any:
            return self._values.get(key)

        async def async_set_cached_value(self, key: str, value: Any) -> None:
            self._values[key] = value

    asyncio.run(
        get_location_data_for_device(
            get_example_data("sample_canonic_device_id"),
            "Test",
            cache=cast(TokenCache, _CliTokenCache()),
        )
    )
