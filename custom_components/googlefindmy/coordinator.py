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
        self._last_location_poll_time = 0  # When we last did a location poll - start at 0 to force immediate poll
        self._device_names = {}  # Map device IDs to names for easier lookup
        
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
            should_poll_location = (time_since_last_poll >= self.location_poll_interval)
            
            _LOGGER.debug(f"Polling check: time_since_last={time_since_last_poll:.1f}s, interval={self.location_poll_interval}s, should_poll={should_poll_location}, devices={len(devices)}")
            
            if should_poll_location and devices:
                _LOGGER.info(f"Polling locations for {len(devices)} devices")
                
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
                            
                            # Simple validation - just check coordinates exist
                            if lat is not None and lon is not None:
                                _LOGGER.info(f"Got location for {device_name}: lat={lat}, lon={lon}")
                                self._device_location_data[device_id] = location_data
                                self._device_location_data[device_id]["last_updated"] = current_time
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
                
                # Apply cached location data if available
                if device["id"] in self._device_location_data:
                    location_data = self._device_location_data[device["id"]]
                    device_info.update(location_data)
                    _LOGGER.debug(f"Applied cached location for {device_info['name']}: lat={device_info.get('latitude')}, lon={device_info.get('longitude')}, acc={device_info.get('accuracy')}")
                    
                    # Add status based on data age
                    last_updated = location_data.get("last_updated", 0)
                    data_age = current_time - last_updated
                    if data_age < self.location_poll_interval:
                        device_info["status"] = "Location data current"
                    elif data_age < self.location_poll_interval * 2:
                        device_info["status"] = "Location data aging"
                    else:
                        device_info["status"] = "Location data stale"
                else:
                    _LOGGER.debug(f"No cached location data for {device_info['name']} (id={device['id']})")
                
                device_data.append(device_info)
                
            _LOGGER.debug(f"Processed {len(devices)} devices, next poll in {max(0, self.location_poll_interval - time_since_last_poll):.0f}s")
            
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