#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import asyncio
import time

# Import FcmReceiver lazily to avoid protobuf conflicts
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import decrypt_location_response_locations
from custom_components.googlefindmy.NovaApi.ExecuteAction.nbe_execute_action import create_action_request, serialize_action_request
from custom_components.googlefindmy.NovaApi.nova_request import async_nova_request
from custom_components.googlefindmy.NovaApi.scopes import NOVA_ACTION_API_SCOPE
from custom_components.googlefindmy.NovaApi.util import generate_random_uuid
from custom_components.googlefindmy.ProtoDecoders import DeviceUpdate_pb2
from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf
from custom_components.googlefindmy.example_data_provider import get_example_data

def create_location_request(canonic_device_id, fcm_registration_id, request_uuid):

    action_request = create_action_request(canonic_device_id, fcm_registration_id, request_uuid=request_uuid)

    # Random values, can be arbitrary
    action_request.action.locateTracker.lastHighTrafficEnablingTime.seconds = 1732120060
    action_request.action.locateTracker.contributorType = DeviceUpdate_pb2.SpotContributorType.FMDN_ALL_LOCATIONS

    # Convert to hex string
    hex_payload = serialize_action_request(action_request)

    return hex_payload


async def get_location_data_for_device(canonic_device_id, name):
    """Get location data for device - HA-compatible async version."""
    
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"GoogleFindMyTools: Requesting location data for {name}...")

    try:
        # Generate request UUID
        request_uuid = generate_random_uuid()

        # Set up FCM receiver with callback (following original pattern)
        received_location_data = {"data": None, "received": False}

        def location_callback(response_canonic_id, hex_response):
            try:
                logger.info(f"FCM callback triggered for {name}, processing response...")
                logger.debug(f"FCM response length: {len(hex_response)} chars")
                
                # Import functions inside callback for thread safety
                try:
                    from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf
                    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import decrypt_location_response_locations
                except ImportError as import_error:
                    logger.error(f"Failed to import decoder functions in callback for {name}: {import_error}")
                    return
                
                # Parse the hex response
                device_update = parse_device_update_protobuf(hex_response)
                
                # Validate canonic_id matches what we requested
                if response_canonic_id != canonic_device_id:
                    logger.warning(f"FCM callback received data for device {response_canonic_id}, but we requested {canonic_device_id}. Ignoring.")
                    return

                # Decrypt the location data
                location_data = decrypt_location_response_locations(device_update)

                if location_data:
                    logger.info(f"Successfully decrypted {len(location_data)} location records for {name}")
                    # Add canonic_id for later reference and validation
                    location_data[0]["canonic_id"] = response_canonic_id
                    received_location_data["data"] = location_data
                    received_location_data["received"] = True
                    logger.info(f"Successfully processed location data for {name}")
                else:
                    logger.warning(f"No location data found after decryption for {name}")

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

        # Google returns empty response when location request is accepted but data comes via FCM
        if nova_result is None:
            logger.error(f"Failed to send location request for {name}")
            return []
        
        logger.info(f"Location request accepted by Google for {name} (response length: {len(nova_result)} chars)")
        
        # Wait for FCM response (extended timeout for device GPS acquisition)
        logger.info(f"Waiting for location response for {name}...")
        wait_time = 0
        timeout = 60  # 60 seconds timeout to allow device GPS acquisition

        try:
            # Wait for response with timeout
            for i in range(timeout * 2):  # Check every 0.5 seconds
                if received_location_data["received"]:
                    # Validate the response is for the correct device
                    if (received_location_data["data"] and 
                        len(received_location_data["data"]) > 0 and 
                        received_location_data["data"][0].get("canonic_id") == canonic_device_id):
                        logger.debug(f"Location response received for {name} after {i*0.5:.1f}s")
                        break
                    else:
                        logger.debug(f"Location response received for different device. Keep waiting.")
                        received_location_data = {"data": None, "received": False}  # reset to not trigger message again
                if i % 40 == 0 and i > 0:  # Log every 20 seconds instead of every 5 seconds
                    logger.debug(f"Still waiting for location response for {name} ({i*0.5:.1f}s elapsed)")
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            logger.info(f"Location request cancelled for {name}")
            try:
                await fcm_receiver.async_unregister_for_location_updates(canonic_device_id)
                if len(fcm_receiver.location_update_callbacks) == 0:
                    await fcm_receiver.async_stop()
            except Exception as e:
                logger.warning(f"Error during cancellation cleanup for {name}: {e}")
            raise

        # Clean up - unregister callback first, then stop receiver if no more callbacks
        try:
            await fcm_receiver.async_unregister_for_location_updates(canonic_device_id)
            
            # Only stop the receiver if no other callbacks are registered
            if len(fcm_receiver.location_update_callbacks) == 0:
                await fcm_receiver.async_stop()
                logger.debug(f"Stopped FCM receiver after unregistering last callback for {name}")
            else:
                logger.debug(f"FCM receiver kept running - {len(fcm_receiver.location_update_callbacks)} callbacks still registered")
                
        except Exception as cleanup_error:
            logger.warning(f"Error during FCM cleanup for {name}: {cleanup_error}")

        if received_location_data["received"] and received_location_data["data"]:
            logger.info(f"Successfully received location data for {name}")
            return received_location_data["data"]
        else:
            logger.warning(f"No location response received for {name} (timeout: {timeout}s)")
            return []

    except Exception as e:
        logger.error(f"Error requesting location for {name}: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return []

if __name__ == '__main__':
    get_location_data_for_device(get_example_data("sample_canonic_device_id"), "Test")