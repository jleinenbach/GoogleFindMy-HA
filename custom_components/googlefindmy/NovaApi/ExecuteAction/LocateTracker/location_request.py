#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

import asyncio
import time
import logging
import traceback
from typing import Optional, Callable, Protocol, runtime_checkable

import aiohttp

# Keep heavy/protobuf-related imports lazy (done inside functions/callbacks)
from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
    create_action_request,
    serialize_action_request,
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

_LOGGER = logging.getLogger(__name__)


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


_FCM_ReceiverGetter: Optional[Callable[[], FcmReceiverProtocol]] = None


def register_fcm_receiver_provider(getter: Callable[[], FcmReceiverProtocol]) -> None:
    """Register a callable returning the long-lived FCM receiver instance.

    The getter must return an initialized receiver exposing:
      - async_register_for_location_updates(device_id, callback) -> str | None
      - async_unregister_for_location_updates(device_id) -> None

    Args:
        getter: A callable that returns the singleton FCM receiver instance.
    """
    global _FCM_ReceiverGetter
    _FCM_ReceiverGetter = getter


def unregister_fcm_receiver_provider() -> None:
    """Unregister the FCM receiver provider (called on integration unload)."""
    global _FCM_ReceiverGetter
    _FCM_ReceiverGetter = None


def create_location_request(canonic_device_id: str, fcm_registration_id: str, request_uuid: str) -> str:
    """Build and serialize a LocateTracker action request.

    DeviceUpdate_pb2 is imported lazily here to avoid protobuf side effects
    at module import time (important inside Home Assistant).

    Args:
        canonic_device_id: The canonical ID of the target device.
        fcm_registration_id: The FCM token for push notifications.
        request_uuid: A unique identifier for this request.

    Returns:
        A hex-encoded string representing the serialized protobuf message.
    """
    from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2  # lazy import

    action_request = create_action_request(
        canonic_device_id, fcm_registration_id, request_uuid=request_uuid
    )

    # Use a current timestamp; server treats this as an arbitrary marker.
    action_request.action.locateTracker.lastHighTrafficEnablingTime.seconds = int(time.time())
    action_request.action.locateTracker.contributorType = (
        DeviceUpdate_pb2.SpotContributorType.FMDN_ALL_LOCATIONS
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

    def __init__(self) -> None:
        """Initialize the callback context."""
        self.event: asyncio.Event = asyncio.Event()
        self.data: list | None = None


def _make_location_callback(
    *,
    name: str,
    canonic_device_id: str,
    ctx: _CallbackContext,
    loop: asyncio.AbstractEventLoop,
    cache_provider: any = None,
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
        cache_provider: The cache provider for this account (captured from context).

    Returns:
        A callback function suitable for the FCM receiver.
    """

    def location_callback(response_canonic_id: str, hex_response: str) -> None:
        """Processes the location update received via FCM."""
        try:
            _LOGGER.info("FCM callback triggered for %s, processing response...", name)
            _LOGGER.debug("FCM response length: %d chars", len(hex_response))

            # Lazy imports inside callback (avoid protobuf import side effects during HA startup)
            try:
                from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf
                from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
                    async_decrypt_location_response_locations,
                    DecryptionError,
                    StaleOwnerKeyError,
                )
                from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
                    SpotApiEmptyResponseError,
                )
            except ImportError as import_error:
                _LOGGER.error("Failed to import decoder/decrypt functions in callback for %s: %s", name, import_error)
                ctx.data = []
                ctx.event.set()
                return

            # Parse the hex response in this worker thread
            try:
                device_update = parse_device_update_protobuf(hex_response)
            except Exception as parse_exc:
                _LOGGER.error("Failed to parse device update for %s: %s", name, parse_exc)
                ctx.data = []
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

            async def _decrypt_and_store():
                """Asynchronous part of the callback to decrypt and store data."""
                # Restore cache provider context for multi-account support
                if cache_provider:
                    from custom_components.googlefindmy.NovaApi import nova_request
                    nova_request.register_cache_provider(cache_provider)

                try:
                    location_data = await async_decrypt_location_response_locations(device_update)
                except (StaleOwnerKeyError, DecryptionError, SpotApiEmptyResponseError) as err:
                    _LOGGER.error("Failed to process location data for %s: %s", name, err)
                    ctx.data = []
                    ctx.event.set()
                    return
                except Exception as err:
                    _LOGGER.error("Unexpected error during async decryption for %s: %s", name, err)
                    _LOGGER.debug("Traceback: %s", traceback.format_exc())
                    ctx.data = []
                    ctx.event.set()
                    return

                if location_data:
                    _LOGGER.info("Successfully decrypted %d location record(s) for %s", len(location_data), name)
                    # Attach canonic_id for validation after wait
                    location_data[0]["canonic_id"] = response_canonic_id
                    ctx.data = location_data
                else:
                    _LOGGER.debug("No location data found after decryption for %s", name)
                    ctx.data = []
                ctx.event.set()

            # Hand off to the HA event loop; do not block this worker thread.
            asyncio.run_coroutine_threadsafe(_decrypt_and_store(), loop)

        except Exception as callback_error:
            _LOGGER.error("Error processing FCM callback for %s: %s", name, callback_error)
            _LOGGER.debug("FCM callback traceback: %s", traceback.format_exc())
            ctx.data = []
            ctx.event.set()

    return location_callback


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------
async def get_location_data_for_device(
    canonic_device_id: str,
    name: str,
    session: Optional[aiohttp.ClientSession] = None,
    *,
    username: Optional[str] = None,
) -> list:
    """Get location data for a device (async, HA-compatible).

    This function orchestrates the entire process of requesting a device's location.
    It registers a temporary callback with the FCM receiver, sends the location
    request, and waits for the asynchronous response to arrive via the callback.

    Notes
    -----
    - The long-lived FCM receiver is provided by integration setup via a provider.
      This function only registers/unregisters callbacks; it does not start/stop the receiver.
    - If a Home Assistant aiohttp.ClientSession is provided, it will be reused for the Nova call.

    Args:
        canonic_device_id: The canonical ID of the device to locate.
        name: The human-readable name of the device for logging purposes.
        session: (Deprecated) An optional aiohttp.ClientSession. No longer used directly.
        username: The username for the request.

    Returns:
        A list of dictionaries containing location data, or an empty list on failure.
    """
    _LOGGER.info("Requesting location data for %s...", name)

    # Fail hard on missing/misconfigured provider: this is a programming/config error.
    if _FCM_ReceiverGetter is None:
        raise RuntimeError("FCM receiver provider has not been registered.")
    fcm_receiver = _FCM_ReceiverGetter()
    if fcm_receiver is None:
        raise RuntimeError("FCM receiver provider returned None.")

    registered = False
    ctx = _CallbackContext()
    loop = asyncio.get_running_loop()

    try:
        # Generate request UUID
        request_uuid = generate_random_uuid()

        # Capture the current cache provider context for multi-account support
        cache_provider = None
        try:
            from custom_components.googlefindmy.NovaApi import nova_request
            # Capture the actual cache provider callable (not just the function)
            cache = nova_request._get_cache_provider()
            if cache:
                cache_provider = lambda: cache
        except Exception:
            pass

        # Register the callback with the shared receiver
        try:
            _LOGGER.debug("Registering FCM location updates for %s...", name)
            callback = _make_location_callback(
                name=name, canonic_device_id=canonic_device_id, ctx=ctx, loop=loop,
                cache_provider=cache_provider
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
        hex_payload = create_location_request(canonic_device_id, fcm_token, request_uuid)

        # Send location request to Google API (async; HA session preferred if provided)
        _LOGGER.info("Sending location request to Google API for %s...", name)
        try:
            _ = await async_nova_request(
                NOVA_ACTION_API_SCOPE, hex_payload, username=username
            )
        except asyncio.CancelledError:
            raise
        except NovaRateLimitError as e:
            _LOGGER.warning("Rate limited while requesting location for %s: %s", name, e)
            return []
        except NovaHTTPError as e:
            _LOGGER.warning("Server error (%s) while requesting location for %s: %s", e.status, name, e)
            return []
        except NovaAuthError as e:
            _LOGGER.error("Authentication error while requesting location for %s: %s", name, e)
            return []
        except aiohttp.ClientError as e:
            _LOGGER.warning("Network/client error while requesting location for %s: %s", name, e)
            return []
        except Exception as e:
            _LOGGER.error("Nova API request failed for %s: %s", name, e)
            return []

        # For this RPC the server often returns HTTP 200 with empty body (FCM delivers the data).
        _LOGGER.info("Location request accepted for %s; awaiting FCM data...", name)

        # Wait efficiently for FCM callback to signal completion
        timeout = 60  # seconds
        _LOGGER.info("Waiting for location response for %s...", name)
        try:
            await asyncio.wait_for(ctx.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            _LOGGER.warning("No location response received for %s (timeout: %ss)", name, timeout)
            return []

        data = ctx.data or []
        if data and data[0].get("canonic_id") == canonic_device_id:
            _LOGGER.info("Successfully received location data for %s", name)
            return data
        if not data:
            _LOGGER.debug("No location data found for %s after decryption", name)
        else:
            _LOGGER.warning("Received location data for unexpected device in %s flow; ignoring.", name)
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
                await fcm_receiver.async_unregister_for_location_updates(canonic_device_id)
        except Exception as cleanup_error:
            _LOGGER.warning("Error during FCM unregister for %s: %s", name, cleanup_error)


if __name__ == '__main__':
    # CLI invocation will fail unless an external provider is registered; kept for parity.
    asyncio.run(
        get_location_data_for_device(get_example_data("sample_canonic_device_id"), "Test")
    )
