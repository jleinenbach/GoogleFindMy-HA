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

    def __init__(self, hass: HomeAssistant, secrets_data: dict, tracked_devices: list = None, location_poll_interval: int = 300, device_poll_delay: int = 5, min_poll_interval: int = 120, min_accuracy_threshold: int = 100) -> None:
        """Initialize."""
        self.api = GoogleFindMyAPI(secrets_data=secrets_data)

        self.tracked_devices = tracked_devices or []
        self.location_poll_interval = location_poll_interval  # Use configured interval
        self.device_poll_delay = device_poll_delay
        self.min_poll_interval = min_poll_interval  # Minimum 2 minutes between polls
        self._min_accuracy_threshold = min_accuracy_threshold  # Accuracy filtering threshold
        
        # Sequential polling state
        self._device_location_data = {}  # Store latest location data for each device
        self._current_device_index = 0   # Which device to poll next
        self._last_location_poll_time = 0  # When we last did a location poll
        self._device_names = {}  # Map device IDs to names for easier lookup

        # Statistics tracking - load from cache or start with zeros
        self.stats = {
            "skipped_duplicates": 0,
            "background_updates": 0,
            "crowd_sourced_updates": 0,
        }
        _LOGGER.debug(f"Initialized stats: {self.stats}")

        # Load persistent statistics from cache
        hass.async_create_task(self._async_load_stats())

        # Polling state tracking
        self._is_polling = False
        self._startup_complete = False

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

            # During startup, just set baseline without polling
            if not self._startup_complete:
                self._startup_complete = True
                self._last_location_poll_time = current_time  # Set baseline for future polls
                _LOGGER.debug("First startup - setting poll baseline, will poll on normal schedule")
                should_poll_location = False  # Skip first poll
                time_since_last_poll = 0  # Reset for logging
            else:
                should_poll_location = (time_since_last_poll >= self.location_poll_interval)

            _LOGGER.debug(f"Poll check: time_since_last_poll={time_since_last_poll:.1f}s, interval={self.location_poll_interval}s, should_poll={should_poll_location}, devices={len(devices) if devices else 0}")

            if should_poll_location and devices:
                # Set polling state
                self._is_polling = True
                _LOGGER.debug("Started polling cycle")
                # Notify listeners about state change
                self.async_set_updated_data(self.data)

                # Poll ALL devices in this cycle with timeouts
                _LOGGER.info(f"Starting sequential poll of {len(devices)} devices")

                for i, device_to_poll in enumerate(devices):
                    device_id = device_to_poll["id"]
                    device_name = device_to_poll["name"]

                    _LOGGER.info(f"Sequential poll: requesting location for device {device_name} ({i + 1}/{len(devices)})")

                    try:
                        # Call async location function with timeout to prevent hanging
                        location_data = await asyncio.wait_for(
                            self.api.async_get_device_location(device_id, device_name),
                            timeout=30.0
                        )

                        # Log data age for informational purposes but don't reject or retry
                        if location_data:
                            last_seen = location_data.get('last_seen', 0)
                            if last_seen > 0:
                                location_age_hours = (current_time - last_seen) / 3600
                                if location_age_hours > 24:
                                    _LOGGER.info(f"Using old location data for {device_name} (age: {location_age_hours:.1f}h)")
                                elif location_age_hours > 1:
                                    _LOGGER.debug(f"Using location data for {device_name} (age: {location_age_hours:.1f}h)")

                        if location_data:
                            # Validate location data before storing
                            lat = location_data.get('latitude')
                            lon = location_data.get('longitude')
                            acc = location_data.get('accuracy')
                            last_seen = location_data.get('last_seen', 0)

                            # Validate coordinates first
                            if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
                                # Check for duplicates first
                                existing_data = self._device_location_data.get(device_id, {})
                                existing_last_seen = existing_data.get('last_seen')

                                if last_seen != existing_last_seen:
                                    # New data
                                    if last_seen > 0:
                                        location_age_hours = (current_time - last_seen) / 3600
                                        _LOGGER.info(f"Got valid location for {device_name}: lat={lat}, lon={lon}, accuracy={acc}m, age={location_age_hours:.1f}h")
                                    else:
                                        _LOGGER.info(f"Got valid location for {device_name}: lat={lat}, lon={lon}, accuracy={acc}m")

                                    self._device_location_data[device_id] = location_data
                                    self._device_location_data[device_id]["last_updated"] = current_time
                                    # Increment polling stats
                                    self.increment_stat("background_updates")
                                else:
                                    # Duplicate detected
                                    _LOGGER.debug(f"Skipping duplicate location data for {device_name} (same last_seen: {last_seen})")
                                    self.increment_stat("skipped_duplicates")
                            else:
                                _LOGGER.warning(f"Invalid coordinates for {device_name}: lat={lat}, lon={lon}")
                                # Keep previous valid data if available
                        else:
                            _LOGGER.warning(f"No location data returned for {device_name}")

                    except asyncio.TimeoutError:
                        _LOGGER.warning(f"Location request timed out for {device_name} after 30 seconds")
                    except Exception as e:
                        _LOGGER.error(f"Failed to get location for {device_name}: {e}")
                        # Keep previous data on error

                    # Add delay before next device (except for the last device)
                    if i < len(devices) - 1:
                        _LOGGER.debug(f"Waiting {self.device_poll_delay}s before next device poll")
                        await asyncio.sleep(self.device_poll_delay)

                # Clear polling flag after polling ALL devices
                self._is_polling = False
                _LOGGER.debug(f"Completed polling cycle for {len(devices)} devices")
                # Notify listeners about state change
                self.async_set_updated_data(self.data)

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
                    # Fallback to Home Assistant historical state if no cached data
                    # Need to find the actual entity_id from the entity registry
                    # The unique_id is googlefindmy_{device_id} but the entity_id is different
                    from homeassistant.helpers import entity_registry as er
                    ent_reg = er.async_get(self.hass)
                    unique_id = f"{DOMAIN}_{device['id']}"
                    entity_entry = ent_reg.async_get_entity_id("device_tracker", DOMAIN, unique_id)

                    if entity_entry:
                        entity_id = entity_entry
                        _LOGGER.debug(f"Found entity_id '{entity_id}' for device '{device_info['name']}' from registry")
                    else:
                        # Fallback to guessing the entity_id format
                        entity_id = f"device_tracker.{device_info['name'].lower().replace(' ', '_').replace("'", '')}"
                        _LOGGER.debug(f"No registry entry found, trying '{entity_id}' for device '{device_info['name']}'")

                    try:
                        state = self.hass.states.get(entity_id)
                        _LOGGER.debug(f"State for {entity_id}: {state.state if state else 'None'}")

                        # If entity not found in current state, query database for historical data
                        if not state:
                            _LOGGER.debug(f"Entity {entity_id} not found in current state, querying database")
                            try:
                                # Query the database for the most recent state with coordinates
                                import sqlite3
                                db_path = self.hass.config.path("home-assistant_v2.db")

                                # Try different entity ID patterns
                                entity_patterns = [
                                    entity_id,  # Current format
                                    f"device_tracker.{device_info['name'].lower().replace(' ', '_').replace("'", '')}",  # Name-based
                                    f"device_tracker.{device_info['name'].lower().replace(' ', '_')}",  # With apostrophe
                                ]

                                conn = sqlite3.connect(db_path)
                                cursor = conn.cursor()

                                for pattern in entity_patterns:
                                    _LOGGER.debug(f"Trying pattern '{pattern}'")
                                    query = """
                                    SELECT s.state, sa.shared_attrs
                                    FROM states s
                                    JOIN state_attributes sa ON s.attributes_id = sa.attributes_id
                                    WHERE s.entity_id = ?
                                    AND sa.shared_attrs LIKE '%latitude%'
                                    AND sa.shared_attrs LIKE '%longitude%'
                                    ORDER BY s.last_updated_ts DESC
                                    LIMIT 1
                                    """
                                    cursor.execute(query, (pattern,))
                                    result = cursor.fetchone()

                                    if result:
                                        import json
                                        attrs = json.loads(result[1])
                                        lat = attrs.get('latitude')
                                        lon = attrs.get('longitude')
                                        acc = attrs.get('gps_accuracy')

                                        if lat is not None and lon is not None:
                                            _LOGGER.debug(f"Found DB data for {pattern}: lat={lat}, lon={lon}")
                                            # Create a fake state object
                                            class FakeState:
                                                def __init__(self, state_val, attrs):
                                                    self.state = state_val
                                                    self.attributes = attrs

                                            state = FakeState(result[0], attrs)
                                            entity_id = pattern
                                            break

                                conn.close()
                            except Exception as e:
                                _LOGGER.error(f"Database query failed for {device_info['name']}: {e}", exc_info=True)

                        if state:
                            # Get coordinates from state attributes (even if state is 'unavailable' or 'unknown')
                            lat = state.attributes.get('latitude')
                            lon = state.attributes.get('longitude')
                            acc = state.attributes.get('gps_accuracy')
                            _LOGGER.debug(f"Attributes for {entity_id}: lat={lat}, lon={lon}, acc={acc}")

                            if lat is not None and lon is not None:
                                device_info.update({
                                    "latitude": lat,
                                    "longitude": lon,
                                    "accuracy": acc,
                                    "status": "Using historical data"
                                })
                                _LOGGER.info(f"Using historical location for {device_info['name']}: lat={lat}, lon={lon}")
                            else:
                                _LOGGER.debug(f"No historical location data for {device_info['name']} (id={device['id']})")
                        else:
                            _LOGGER.debug(f"No state found for {device_info['name']} (id={device['id']})")
                    except Exception as e:
                        _LOGGER.debug(f"Error retrieving historical data for {device_info['name']}: {e}")
                        _LOGGER.debug(f"No cached location data for {device_info['name']} (id={device['id']})")
                
                device_data.append(device_info)
                
            _LOGGER.debug(f"Sequential polling: processed {len(devices)} devices, next poll in {max(0, self.location_poll_interval - time_since_last_poll):.0f}s")

            # Debug log what we're returning
            for device in device_data:
                _LOGGER.debug(f"Returning device {device['name']}: lat={device.get('latitude')}, lon={device.get('longitude')}, semantic={device.get('semantic_name')}")

            return device_data
            
        except Exception as exception:
            raise UpdateFailed(exception) from exception

    async def _async_load_stats(self):
        """Load statistics from cache."""
        try:
            from .Auth.token_cache import async_get_cached_value
            cached_stats = await async_get_cached_value("integration_stats")
            if cached_stats and isinstance(cached_stats, dict):
                for key in self.stats.keys():
                    if key in cached_stats:
                        self.stats[key] = cached_stats[key]
                _LOGGER.debug(f"Loaded statistics from cache: {self.stats}")
        except Exception as e:
            _LOGGER.debug(f"Failed to load statistics from cache: {e}")

    async def _async_save_stats(self):
        """Save statistics to cache."""
        try:
            from .Auth.token_cache import async_set_cached_value
            await async_set_cached_value("integration_stats", self.stats.copy())
        except Exception as e:
            _LOGGER.debug(f"Failed to save statistics to cache: {e}")

    def increment_stat(self, stat_name: str):
        """Increment a statistic counter and save to cache."""
        if stat_name in self.stats:
            old_value = self.stats[stat_name]
            self.stats[stat_name] += 1
            _LOGGER.debug(f"Incremented {stat_name} from {old_value} to {self.stats[stat_name]}")
            # Schedule async save to avoid blocking
            self.hass.async_create_task(self._async_save_stats())
        else:
            _LOGGER.warning(f"Tried to increment unknown stat {stat_name}, available: {list(self.stats.keys())}")

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