"""Data coordinator for Google Find My Device."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, UPDATE_INTERVAL
from .api import GoogleFindMyAPI
from .location_recorder import LocationRecorder

_LOGGER = logging.getLogger(__name__)


class GoogleFindMyCoordinator(DataUpdateCoordinator):
    """Class to manage fetching Google Find My Device data."""

    def __init__(self, hass: HomeAssistant, oauth_token: str = None, google_email: str = None, secrets_data: dict = None, tracked_devices: list = None, location_poll_interval: int = 300, device_poll_delay: int = 5, min_poll_interval: int = 60) -> None:
        """Initialize."""
        if secrets_data:
            self.api = GoogleFindMyAPI(secrets_data=secrets_data)
        else:
            self.api = GoogleFindMyAPI(oauth_token=oauth_token, google_email=google_email)
        
        self.tracked_devices = tracked_devices or []
        self.location_poll_interval = max(location_poll_interval, min_poll_interval)  # Enforce minimum interval
        self.device_poll_delay = device_poll_delay
        self.min_poll_interval = min_poll_interval  # Minimum 1 minute between polls
        
        # Location data cache
        self._device_location_data = {}  # Store latest location data for each device
        self._last_location_poll_time = time.time()  # Start at current time to avoid immediate poll on first startup
        self._device_names = {}  # Map device IDs to names for easier lookup
        self._startup_complete = False  # Flag to track if initial setup is done
        
        # Initialize recorder-based location history
        self.location_recorder = LocationRecorder(hass)
        
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def _async_update_data(self):
        """Update data via library."""
        try:
            # Get basic device list first
            all_devices = await self.hass.async_add_executor_job(self.api.get_basic_device_list)
            
            # Filter to only tracked devices
            if self.tracked_devices:
                devices = [dev for dev in all_devices if dev["id"] in self.tracked_devices]
            else:
                devices = all_devices
            
            # Update device names mapping
            for device in devices:
                self._device_names[device["id"]] = device["name"]
            
            current_time = time.time()
            
            # Check if it's time for a location poll
            time_since_last_poll = current_time - self._last_location_poll_time
            should_poll_location = (time_since_last_poll >= self.location_poll_interval) and self._startup_complete
            
            if should_poll_location and devices:
                _LOGGER.debug(f"Polling locations for {len(devices)} devices")
                
                # Poll all devices with delays to avoid rate limiting
                for i, device in enumerate(devices):
                    device_id = device["id"]
                    device_name = device["name"]
                    
                    # Add delay between requests to avoid overwhelming the API
                    if i > 0:
                        await asyncio.sleep(self.device_poll_delay)
                    
                    try:
                        _LOGGER.debug(f"Requesting location for {device_name}")
                        location_data = await self.api.async_get_device_location(device_id, device_name)
                        
                        if location_data:
                            lat = location_data.get('latitude')
                            lon = location_data.get('longitude')
                            accuracy = location_data.get('accuracy')
                            
                            # Get accuracy threshold from config
                            config_data = self.hass.data[DOMAIN].get("config_data", {})
                            min_accuracy_threshold = config_data.get("min_accuracy_threshold", 100)
                            
                            # Validate coordinates and accuracy threshold
                            if lat is not None and lon is not None:
                                if accuracy is not None and accuracy > min_accuracy_threshold:
                                    _LOGGER.debug(f"Filtering out location for {device_name}: accuracy {accuracy}m exceeds threshold {min_accuracy_threshold}m")
                                else:
                                    # Location received successfully
                                    
                                    # Store current location and get best from recorder history
                                    self._device_location_data[device_id] = location_data.copy()
                                    self._device_location_data[device_id]["last_updated"] = current_time
                                    
                                    # Get recorder history and combine with current data for better location selection
                                    try:
                                        # Try both possible entity ID formats
                                        entity_id_by_unique = f"device_tracker.{DOMAIN}_{device_id}"
                                        entity_id_by_name = f"device_tracker.{device_name.lower().replace(' ', '_')}"
                                        
                                        # Try unique ID format first
                                        historical_locations = await self.location_recorder.get_location_history(entity_id_by_unique, hours=24)
                                        
                                        # If no history found, try name-based format
                                        if not historical_locations:
                                            _LOGGER.debug(f"No history for {entity_id_by_unique}, trying {entity_id_by_name}")
                                            historical_locations = await self.location_recorder.get_location_history(entity_id_by_name, hours=24)
                                        
                                        # Add current Google API location to historical data
                                        current_location_entry = {
                                            'timestamp': location_data.get('last_seen', current_time),
                                            'latitude': location_data.get('latitude'),
                                            'longitude': location_data.get('longitude'),
                                            'accuracy': location_data.get('accuracy'),
                                            'is_own_report': location_data.get('is_own_report', False),
                                            'altitude': location_data.get('altitude')
                                        }
                                        historical_locations.insert(0, current_location_entry)
                                        
                                        # Select best location from all data (current + 24hrs of history)
                                        best_location = self.location_recorder.get_best_location(historical_locations)
                                        
                                        if best_location:
                                            # Use the best location from combined dataset
                                            self._device_location_data[device_id].update({
                                                'latitude': best_location.get('latitude'),
                                                'longitude': best_location.get('longitude'),
                                                'accuracy': best_location.get('accuracy'),
                                                'altitude': best_location.get('altitude'),
                                                'is_own_report': best_location.get('is_own_report')
                                            })
                                    
                                    except Exception as e:
                                        _LOGGER.debug(f"Recorder history lookup failed for {device_name}, using current data: {e}")
                            else:
                                _LOGGER.warning(f"Invalid coordinates for {device_name}: lat={lat}, lon={lon}")
                        else:
                            _LOGGER.warning(f"No location data returned for {device_name}")
                            
                    except Exception as e:
                        _LOGGER.error(f"Failed to get location for {device_name}: {e}")
                
                # Update polling state
                self._last_location_poll_time = current_time
            
            # Build device data with cached location information
            device_data = []
            for device in devices:
                # Start with last known good position if available
                if device["id"] in self._device_location_data:
                    # Use cached location as base (preserves last known good position)
                    device_info = self._device_location_data[device["id"]].copy()
                    device_info.update({
                        "name": device["name"],
                        "id": device["id"], 
                        "device_id": device["id"],
                        "status": "Using last known position"
                    })
                else:
                    # No cached data, create new entry
                    device_info = {
                        "name": device["name"],
                        "id": device["id"], 
                        "device_id": device["id"],
                        "latitude": None,
                        "longitude": None,
                        "altitude": None,
                        "accuracy": None,
                        "last_seen": None,
                        "status": "Waiting for location poll",
                        "is_own_report": None,
                        "semantic_name": None,
                        "battery_level": None
                    }
                
                # Apply fresh cached location data if available (COPY to avoid contamination)
                if device["id"] in self._device_location_data and "last_updated" in self._device_location_data[device["id"]]:
                    cached_location = self._device_location_data[device["id"]].copy()
                    device_info.update(cached_location)
                    # Applied cached location data
                    
                    # Add status based on data age
                    last_updated = location_data.get("last_updated", 0)
                    data_age = current_time - last_updated
                    if data_age < self.location_poll_interval:
                        device_info["status"] = "Location data current"
                    elif data_age < self.location_poll_interval * 2:
                        device_info["status"] = "Location data aging"
                    else:
                        device_info["status"] = "Location data stale"
                # Remove excessive "no cached data" logging
                
                device_data.append(device_info)
                
            # Remove excessive processing debug log
            
            # Mark startup as complete after first successful refresh
            if not self._startup_complete:
                self._startup_complete = True
                _LOGGER.debug(f"GoogleFindMy startup complete - location polling will begin after {self.location_poll_interval}s")
            
            # Cleanup disabled - was part of location history system
            
            return device_data
            
        except Exception as exception:
            raise UpdateFailed(exception) from exception

    async def async_locate_device(self, device_id: str) -> dict:
        """Locate a device."""
        return await self.hass.async_add_executor_job(
            self.api.locate_device, device_id
        )

    async def async_play_sound(self, device_id: str) -> bool:
        """Play sound on a device."""
        return await self.hass.async_add_executor_job(
            self.api.play_sound, device_id
        )