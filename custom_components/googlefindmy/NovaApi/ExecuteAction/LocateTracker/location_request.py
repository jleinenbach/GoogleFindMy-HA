#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Tuple, Union

# Import FcmReceiver lazily to avoid protobuf conflicts
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    decrypt_location_response_locations,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import (
    create_action_request,
    serialize_action_request,
)
from custom_components.googlefindmy.NovaApi.nova_request import async_nova_request
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE
from custom_components.googlefindmy.NovaApi.util import generate_random_uuid
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf
from custom_components.googlefindmy.example_data_provider import get_example_data

_LOGGER = logging.getLogger(__name__)

Json = Dict[str, Any]
LocList = List[Json]


def create_location_request(canonic_device_id: str, fcm_registration_id: str, request_uuid: str) -> str:
    """Build and serialize a LocateTracker action request (hex string)."""
    action_request = create_action_request(
        canonic_device_id,
        fcm_registration_id,
        request_uuid=request_uuid,
    )

    # Random values, can be arbitrary
    action_request.action.locateTracker.lastHighTrafficEnablingTime.seconds = 1732120060
    action_request.action.locateTracker.contributorType = (
        DeviceUpdate_pb2.SpotContributorType.FMDN_ALL_LOCATIONS
    )

    # Convert to hex string
    hex_payload = serialize_action_request(action_request)
    return hex_payload


def _to_loc_list(data: Any) -> LocList:
    """
    Normalize decrypted location data into a list of dicts.

    Accepts:
      - list[dict] → as-is
      - dict with 'locations' → that list
      - dict (single record) → [dict]
      - JSON string → parsed recursively
      - anything else → []
    """
    if data is None:
        return []

    if isinstance(data, list):
        return [rec for rec in data if isinstance(rec, dict)]

    if isinstance(data, dict):
        if "locations" in data and isinstance(data["locations"], list):
            return [rec for rec in data["locations"] if isinstance(rec, dict)]
        return [data]

    if isinstance(data, (bytes, bytearray)):
        try:
            data = data.decode("utf-8", "ignore")
        except Exception:
            return []

    if isinstance(data, str):
        s = data.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except Exception:
            return []
        return _to_loc_list(parsed)

    return []


async def get_location_data_for_device(canonic_device_id: str, name: str) -> LocList:
    """Get location data for device - HA-compatible async version."""

    logger = _LOGGER
    logger.info(f"GoogleFindMyTools: Requesting location data for {name}...")

    fcm_receiver = None
    try:
        # Generate request UUID
        request_uuid = generate_random_uuid()

        # Set up FCM receiver with callback (following original pattern)
        received_location_data: Dict[str, Any] = {"data": None, "received": False}

        def location_callback(response_canonic_id: str, hex_response: str) -> None:
            """
            FCM callback registered per device.

            Robust against varying return types from decrypt_location_response_locations:
            we normalize to List[Dict] and only then annotate with canonic_id.
            """
            try:
                logger.info(f"FCM callback triggered for {name}, processing response...")
                if isinstance(hex_response, (str, bytes, bytearray)):
                    logger.debug(f"FCM response length: {len(hex_response)} chars")

                # Import functions inside callback for thread safety (keep original pattern)
                try:
                    from custom_components.googlefindmy.ProtoDecoders.decoder import (
                        parse_device_update_protobuf as _parse,
                    )
                    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
                        decrypt_location_response_locations as _decrypt,
                    )
                except ImportError as import_error:
                    logger.error(f"Failed to import decoder functions in callback for {name}: {import_error}")
                    return

                # Parse the hex response → protobuf
                device_update = _parse(hex_response)

                # Validate canonic_id matches what we requested
                if response_canonic_id != canonic_device_id:
                    logger.warning(
                        "FCM callback received data for device %s, but we requested %s. Ignoring.",
                        response_canonic_id,
                        canonic_device_id,
                    )
                    return

                # Decrypt the location data (may return list/dict/str depending on upstream)
                raw_location_data = _decrypt(device_update)
                locs: LocList = _to_loc_list(raw_location_data)

                if locs:
                    # Annotate all records with canonic_id (no string-index error)
                    for rec in locs:
                        rec["canonic_id"] = response_canonic_id
                    received_location_data["data"] = locs
                    received_location_data["received"] = True
                    logger.info(f"Successfully decrypted {len(locs)} location record(s) for {name}")
                else:
                    # Provide a tiny preview for diagnostics without spamming logs
                    preview = None
                    try:
                        preview = str(raw_location_data)
                        if preview and len(preview) > 160:
                            preview = preview[:160] + "…"
                    except Exception:
                        preview = "<unprintable>"
                    logger.warning(f"No usable location data after decryption for {name}; preview={preview}")

            except Exception as callback_error:
                logger.error(f"Error processing FCM callback for {name}: {callback_error}")
                import traceback

                logger.debug(f"FCM callback traceback: {traceback.format_exc()}")

        # Get HA-compatible FCM receiver and register for updates
        try:
            from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA

            fcm_receiver = FcmReceiverHA()

            logger.debug(f"Initializing FCM receiver for {name}...")
            # Initialize async
            if not await fcm_receiver.async_initialize():
                logger.error(f"Failed to initialize FCM receiver for {name}")
                return []

            logger.debug(f"FCM receiver initialized, registering for location updates for {name}...")
            # Register for location updates with device-specific callback
            fcm_token = await fcm_receiver.async_register_for_location_updates(canonic_device_id, location_callback)
            if not fcm_token:
                logger.error(f"Failed to get FCM token for {name}")
                return []

            logger.debug(f"FCM token obtained for {name}: {fcm_token[:20]}...")

        except Exception as fcm_error:
            logger.error(f"FCM setup failed for {name}: {fcm_error}")
            import traceback

            logger.debug(f"FCM setup traceback: {traceback.format_exc()}")
            return []

        # Create location request payload
        hex_payload = create_location_request(canonic_device_id, fcm_token, request_uuid)

        # Send location request to Google API
        logger.info(f"Sending location request to Google API for {name}...")
        nova_result = await async_nova_request(NOVA_ACTION_API_SCOPE, hex_payload)

        # Google may return an empty/None response when request is accepted and data comes via FCM
        if nova_result is None:
            logger.debug(f"Location request accepted by Google for {name} (empty response body).")
        else:
            logger.info(f"Location request accepted by Google for {name} (response length: {len(nova_result)} chars)")

        # Wait for FCM response (extended timeout for device GPS acquisition)
        logger.info(f"Waiting for location response for {name}...")
        timeout = 60  # seconds
        try:
            for i in range(timeout * 2):  # Check every 0.5 seconds
                if received_location_data["received"]:
                    data = received_location_data["data"]
                    # Validate response belongs to the correct device
                    if isinstance(data, list) and data and isinstance(data[0], dict):
                        if data[0].get("canonic_id") == canonic_device_id:
                            logger.debug(f"Location response received for {name} after {i * 0.5:.1f}s")
                            break
                        else:
                            logger.debug("Location response belongs to a different device. Waiting for the correct one.")
                            received_location_data = {"data": None, "received": False}
                if i % 40 == 0 and i > 0:  # Log every 20 seconds instead of every 5 seconds
                    logger.debug(f"Still waiting for location response for {name} ({i * 0.5:.1f}s elapsed)")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            logger.info(f"Location request cancelled for {name}")
            # Clean up below in finally
            raise

        # Clean up - unregister callback first, then stop receiver if no more callbacks
        try:
            if fcm_receiver is not None:
                await fcm_receiver.async_unregister_for_location_updates(canonic_device_id)
                # Only stop the receiver if no other callbacks are registered
                if len(getattr(fcm_receiver, "location_update_callbacks", {})) == 0:
                    await fcm_receiver.async_stop()
                    logger.debug(f"Stopped FCM receiver after unregistering last callback for {name}")
                else:
                    logger.debug(
                        "FCM receiver kept running - %d callbacks still registered",
                        len(fcm_receiver.location_update_callbacks),
                    )
        except Exception as cleanup_error:
            logger.warning(f"Error during FCM cleanup for {name}: {cleanup_error}")

        data = received_location_data.get("data")
        if received_location_data.get("received") and isinstance(data, list) and data:
            logger.info(f"Successfully received location data for {name}")
            return data
        else:
            logger.warning(f"No location response received for {name} (timeout: {timeout}s)")
            return []

    except Exception as e:
        logger.error(f"Error requesting location for {name}: {e}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")
        return []
    finally:
        # Ensure the receiver is stopped in edge cases
        try:
            if fcm_receiver is not None and hasattr(fcm_receiver, "location_update_callbacks"):
                if len(fcm_receiver.location_update_callbacks) == 0:
                    await fcm_receiver.async_stop()
        except Exception:
            pass


if __name__ == "__main__":
    get_location_data_for_device(get_example_data("sample_canonic_device_id"), "Test")
