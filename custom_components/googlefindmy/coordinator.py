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

    def __init__(self, hass: HomeAssistant, oauth_token: str = None, google_email: str = None, secrets_data: dict = None, tracked_devices: list = None, location_poll_interval: int = 300, device_poll_delay: int = 5, min_poll_interval: int = 120) -> None:
        """Initialize."""
        if secrets_data:
            self.api = GoogleFindMyAPI(secrets_data=secrets_data)
        else:
            self.api = GoogleFindMyAPI(oauth_token=oauth_token, google_email=google_email)
        
        self.tracked_devices = tracked_devices or []
        self.location_poll_interval = max(location_poll_interval, min_poll_interval)  # Enforce minimum interval
        self.device_poll_delay = device_poll_delay
        self.min_poll_interval = min_poll_interval  # Minimum 2 minutes between polls
        
        # Sequential polling state
        self._device_location_data = {}  # Store latest location data for each device
        self._current_device_index = 0   # Which device to poll next
        self._last_location_poll_time = 0  # When we last did a location poll
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
            
            if should_poll_location and devices:
                # Sequential polling: poll next device in rotation
                if self._current_device_index >= len(devices):
                    self._current_device_index = 0  # Wrap around
                    
                device_to_poll = devices[self._current_device_index]
                device_id = device_to_poll["id"]
                device_name = device_to_poll["name"]
                
                _LOGGER.info(f"Sequential poll: requesting location for device {device_name} ({self._current_device_index + 1}/{len(devices)})")
                
                try:
                    # Call async location function directly
                    location_data = await self.api.async_get_device_location(device_id, device_name)
                    
                    # Check if we got stale data and retry once
                    if location_data:
                        last_seen = location_data.get('last_seen', 0)
                        if last_seen > 0:
                            location_age_hours = (current_time - last_seen) / 3600
                            if location_age_hours > 0.5:  # If older than 30 minutes
                                _LOGGER.warning(f"Received stale location data for {device_name} (age: {location_age_hours:.1f}h), retrying once...")
                                await asyncio.sleep(2)  # Brief delay before retry
                                retry_location_data = await self.api.async_get_device_location(device_id, device_name)
                                if retry_location_data:
                                    retry_last_seen = retry_location_data.get('last_seen', 0)
                                    if retry_last_seen > 0:
                                        retry_age_hours = (current_time - retry_last_seen) / 3600
                                        if retry_age_hours < location_age_hours:  # If retry data is fresher
                                            _LOGGER.info(f"Retry successful for {device_name}, got fresher data (age: {retry_age_hours:.1f}h)")
                                            location_data = retry_location_data
                                        else:
                                            _LOGGER.warning(f"Retry didn't improve data freshness for {device_name}")
                    
                    if location_data:
                        # Validate location data before storing
                        lat = location_data.get('latitude')
                        lon = location_data.get('longitude')
                        acc = location_data.get('accuracy')
                        last_seen = location_data.get('last_seen', 0)
                        
                        # Check if location data is too old (older than 30 minutes)
                        max_age_hours = 0.5  # 30 minutes for fresher location data
                        is_stale = False
                        if last_seen > 0:
                            location_age_hours = (current_time - last_seen) / 3600
                            if location_age_hours > max_age_hours:
                                _LOGGER.warning(f"Rejecting stale location for {device_name}: {location_age_hours:.1f} hours old")
                                # Don't update with stale data, keep existing data
                                is_stale = True
                        
                        # Basic validation
                        if not is_stale and lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                            if last_seen > 0:
                                location_age_hours = (current_time - last_seen) / 3600
                                _LOGGER.info(f"Got valid location for {device_name}: lat={lat}, lon={lon}, accuracy={acc}m, age={location_age_hours:.1f}h")
                            else:
                                _LOGGER.info(f"Got valid location for {device_name}: lat={lat}, lon={lon}, accuracy={acc}m")
                            
                            self._device_location_data[device_id] = location_data
                            self._device_location_data[device_id]["last_updated"] = current_time
                        else:
                            _LOGGER.warning(f"Invalid coordinates for {device_name}: lat={lat}, lon={lon}")
                            # Keep previous valid data if available
                    else:
                        _LOGGER.warning(f"No location data returned for {device_name}")
                        
                except Exception as e:
                    _LOGGER.error(f"Failed to get location for {device_name}: {e}")
                    # Keep previous data on error
                
                # Add delay before next device poll (except for the last device)
                self._current_device_index += 1
                if self._current_device_index < len(devices):
                    _LOGGER.debug(f"Waiting {self.device_poll_delay}s before next device poll")
                    await asyncio.sleep(self.device_poll_delay)
                
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
                
                # Apply cached location data if available and not too old
                if device["id"] in self._device_location_data:
                    location_data = self._device_location_data[device["id"]]
                    last_seen = location_data.get("last_seen", 0)
                    
                    # Check if cached data is too old (older than 30 minutes from actual location timestamp)
                    max_age_hours = 0.5  # 30 minutes for fresher location data
                    location_too_old = False
                    if last_seen > 0:
                        location_age_hours = (current_time - last_seen) / 3600
                        if location_age_hours > max_age_hours:
                            _LOGGER.warning(f"Cached location for {device['name']} is {location_age_hours:.1f}h old, removing from cache")
                            # Remove the stale cached data
                            del self._device_location_data[device["id"]]
                            location_too_old = True
                    
                    if not location_too_old:
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
                
            _LOGGER.debug(f"Sequential polling: processed {len(devices)} devices, next poll in {max(0, self.location_poll_interval - time_since_last_poll):.0f}s")
            
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