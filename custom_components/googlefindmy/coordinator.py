"""Data coordinator for Google Find My Device."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from math import isfinite
from typing import Any, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import GoogleFindMyAPI
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


def _valid_coords(lat: Any, lon: Any) -> bool:
    """Strict validation for coordinates."""
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return False
    if not (isfinite(lat) and isfinite(lon)):
        return False
    return -90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0


class GoogleFindMyCoordinator(DataUpdateCoordinator[List[Dict[str, Any]]]):
    """Manage fetching and normalizing Google Find My Device data."""

    def __init__(
        self,
        hass: HomeAssistant,
        secrets_data: dict,
        tracked_devices: Optional[List[str]] = None,
        location_poll_interval: int = 300,
        device_poll_delay: int = 5,
        min_poll_interval: int = 120,
        min_accuracy_threshold: int = 100,
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

        self.api = GoogleFindMyAPI(secrets_data=secrets_data)

        # Polling / filtering configuration
        self.tracked_devices = tracked_devices or []
        self.location_poll_interval = max(int(location_poll_interval), 0)
        self.device_poll_delay = max(int(device_poll_delay), 0)
        self.min_poll_interval = max(int(min_poll_interval), 0)  # absolute floor (HA option min 120s)
        self._min_accuracy_threshold = max(int(min_accuracy_threshold), 0)  # meters

        # Sequential polling state
        self._device_location_data: Dict[str, Dict[str, Any]] = {}  # latest accepted location per device
        self._current_device_index = 0  # reserved for future round-robin
        self._last_location_poll_time: float = 0.0
        self._device_names: Dict[str, str] = {}

        # Log rate-limiting (per device)
        self._invalid_coord_log_ts: Dict[str, float] = {}

        # Statistics tracking - load from cache or start with zeros
        self.stats: Dict[str, int] = {
            "skipped_duplicates": 0,
            "background_updates": 0,
            "crowd_sourced_updates": 0,
        }
        _LOGGER.debug("Initialized stats: %s", self.stats)

        # Load persistent statistics from cache
        hass.async_create_task(self._async_load_stats())

        # Polling state tracking
        self._is_polling = False
        self._startup_complete = False

    async def _async_update_data(self) -> List[Dict[str, Any]]:
        """Update data via library."""
        try:
            # Get basic device list first
            all_devices = await self.hass.async_add_executor_job(self.api.get_basic_device_list)

            # Filter to only tracked devices
            if self.tracked_devices:
                devices = [dev for dev in all_devices if dev.get("id") in self.tracked_devices]
            else:
                devices = all_devices

            # Update device names mapping
            for device in devices:
                did = device.get("id")
                if did:
                    self._device_names[did] = device.get("name", did)

            current_time = time.time()

            # Effective poll interval honors a minimum, to be kind to upstream
            effective_poll_interval = max(self.location_poll_interval, self.min_poll_interval)
            time_since_last_poll = current_time - self._last_location_poll_time

            # During startup, set baseline without polling immediately
            if not self._startup_complete:
                self._startup_complete = True
                self._last_location_poll_time = current_time  # Set baseline for future polls
                _LOGGER.debug("First startup - setting poll baseline, will poll on normal schedule")
                should_poll_location = False  # Skip first poll
                time_since_last_poll = 0.0
            else:
                should_poll_location = time_since_last_poll >= effective_poll_interval

            _LOGGER.debug(
                "Poll check: elapsed=%.1fs, interval=%ss (effective=%ss), should_poll=%s, devices=%d",
                time_since_last_poll,
                self.location_poll_interval,
                effective_poll_interval,
                should_poll_location,
                len(devices) if devices else 0,
            )

            if should_poll_location and devices:
                # Set polling state
                self._is_polling = True
                _LOGGER.debug("Started polling cycle")
                # Notify listeners about state change (so UI can reflect 'polling')
                self.async_set_updated_data(getattr(self, "data", None))

                # Poll ALL devices in this cycle with timeouts
                _LOGGER.info("Starting sequential poll of %d devices", len(devices))

                for i, device_to_poll in enumerate(devices):
                    device_id = device_to_poll.get("id")
                    device_name = device_to_poll.get("name", device_id or "unknown")

                    if not device_id:
                        _LOGGER.debug("Skipping device without id: %r", device_to_poll)
                        continue

                    _LOGGER.info(
                        "Sequential poll: requesting location for device %s (%d/%d)",
                        device_name,
                        i + 1,
                        len(devices),
                    )

                    try:
                        # Call async location function with timeout to prevent hanging
                        location_data = await asyncio.wait_for(
                            self.api.async_get_device_location(device_id, device_name),
                            timeout=30.0,
                        )

                        # Log data age for informational purposes but don't reject or retry
                        if location_data:
                            last_seen = location_data.get("last_seen", 0)
                            if isinstance(last_seen, (int, float)) and last_seen > 0:
                                location_age_hours = (current_time - float(last_seen)) / 3600.0
                                if location_age_hours > 24:
                                    _LOGGER.info(
                                        "Using old location data for %s (age: %.1fh)", device_name, location_age_hours
                                    )
                                elif location_age_hours > 1:
                                    _LOGGER.debug(
                                        "Using location data for %s (age: %.1fh)", device_name, location_age_hours
                                    )

                        if location_data:
                            # Validate location data before storing
                            lat = location_data.get("latitude")
                            lon = location_data.get("longitude")
                            acc = location_data.get("accuracy")
                            last_seen = location_data.get("last_seen", 0)

                            coords_ok = _valid_coords(lat, lon)
                            acc_ok = acc is None or (
                                isinstance(acc, (int, float)) and isfinite(acc) and acc <= self._min_accuracy_threshold
                            )

                            if coords_ok and acc_ok:
                                # Check for duplicates via last_seen
                                existing_data = self._device_location_data.get(device_id, {})
                                existing_last_seen = existing_data.get("last_seen")

                                if last_seen != existing_last_seen:
                                    # New data accepted
                                    if isinstance(last_seen, (int, float)) and last_seen > 0:
                                        location_age_hours = (current_time - float(last_seen)) / 3600.0
                                        _LOGGER.info(
                                            "Got valid location for %s: lat=%s, lon=%s, accuracy=%sm, age=%.1fh",
                                            device_name,
                                            lat,
                                            lon,
                                            acc,
                                            location_age_hours,
                                        )
                                    else:
                                        _LOGGER.info(
                                            "Got valid location for %s: lat=%s, lon=%s, accuracy=%sm",
                                            device_name,
                                            lat,
                                            lon,
                                            acc,
                                        )

                                    self._device_location_data[device_id] = dict(location_data)
                                    self._device_location_data[device_id]["last_updated"] = current_time
                                    # Increment polling stats
                                    self.increment_stat("background_updates")
                                else:
                                    # Duplicate detected
                                    _LOGGER.debug(
                                        "Skipping duplicate location data for %s (same last_seen: %s)",
                                        device_name,
                                        last_seen,
                                    )
                                    self.increment_stat("skipped_duplicates")

                            else:
                                # Do not overwrite last valid data; log at DEBUG with rate limit to avoid spam
                                now_mono = time.monotonic()
                                last_log = self._invalid_coord_log_ts.get(device_id, 0.0)
                                if now_mono - last_log > 1800.0:  # 30 min per device
                                    if not coords_ok:
                                        _LOGGER.debug(
                                            "Ignoring invalid coordinates for %s: lat=%r, lon=%r",
                                            device_name,
                                            lat,
                                            lon,
                                        )
                                    elif not acc_ok:
                                        _LOGGER.debug(
                                            "Ignoring low-quality fix for %s: accuracy=%r (threshold=%sm)",
                                            device_name,
                                            acc,
                                            self._min_accuracy_threshold,
                                        )
                                    self._invalid_coord_log_ts[device_id] = now_mono
                                # Keep previous valid data if available
                        else:
                            _LOGGER.debug("No location data returned for %s", device_name)

                    except asyncio.TimeoutError:
                        _LOGGER.warning("Location request timed out for %s after 30 seconds", device_name)
                    except Exception as e:
                        _LOGGER.error("Failed to get location for %s: %s", device_name, e)
                        # Keep previous data on error

                    # Add delay before next device (except for the last device)
                    if i < len(devices) - 1 and self.device_poll_delay > 0:
                        _LOGGER.debug("Waiting %ds before next device poll", self.device_poll_delay)
                        await asyncio.sleep(self.device_poll_delay)

                # Clear polling flag after polling ALL devices
                self._is_polling = False
                _LOGGER.debug("Completed polling cycle for %d devices", len(devices))
                # Notify listeners about state change
                self.async_set_updated_data(getattr(self, "data", None))

                # Update polling state
                self._last_location_poll_time = current_time

            # Build device data with cached location information
            device_data: List[Dict[str, Any]] = []
            for device in devices:
                device_id = device.get("id")
                device_name = device.get("name", device_id or "unknown")

                device_info: Dict[str, Any] = {
                    "name": device_name,
                    "id": device_id,
                    "device_id": device_id,
                    "latitude": None,
                    "longitude": None,
                    "altitude": None,
                    "accuracy": None,
                    "last_seen": None,
                    "status": "Waiting for location poll",
                    "is_own_report": None,
                    "semantic_name": None,
                    "battery_level": None,
                }

                # Apply cached location data if available
                if device_id in self._device_location_data:
                    location_data = self._device_location_data[device_id]
                    device_info.update(location_data)
                    _LOGGER.debug(
                        "Applied cached location for %s: lat=%s, lon=%s, acc=%s",
                        device_info["name"],
                        device_info.get("latitude"),
                        device_info.get("longitude"),
                        device_info.get("accuracy"),
                    )

                    # Add status based on data age
                    last_updated = location_data.get("last_updated", 0.0)
                    data_age = current_time - float(last_updated)
                    if data_age < effective_poll_interval:
                        device_info["status"] = "Location data current"
                    elif data_age < effective_poll_interval * 2:
                        device_info["status"] = "Location data aging"
                    else:
                        device_info["status"] = "Location data stale"
                else:
                    # Fallback to Home Assistant historical state if no cached data
                    # Need to find the actual entity_id from the entity registry
                    # The unique_id is googlefindmy_{device_id} but the entity_id is different
                    try:
                        from homeassistant.helpers import entity_registry as er

                        ent_reg = er.async_get(self.hass)
                        unique_id = f"{DOMAIN}_{device_id}"
                        entity_id = ent_reg.async_get_entity_id("device_tracker", DOMAIN, unique_id)

                        if not entity_id:
                            # Fallback to guessing the entity_id format
                            # sanitize name → device_tracker.<name>
                            guessed = (
                                (device_name or "").lower().replace(" ", "_").replace("'", "")
                            )
                            entity_id = f"device_tracker.{guessed}"
                            _LOGGER.debug(
                                "No registry entry found, trying '%s' for device '%s'", entity_id, device_name
                            )

                        state = self.hass.states.get(entity_id)
                        _LOGGER.debug("State for %s: %s", entity_id, state.state if state else "None")

                        # If entity not found in current state, query database for historical data
                        if not state:
                            _LOGGER.debug("Entity %s not in current state; skipping DB lookup to avoid I/O in loop", entity_id)
                        else:
                            # Get coordinates from state attributes (even if state is 'unavailable' or 'unknown')
                            lat = state.attributes.get("latitude")
                            lon = state.attributes.get("longitude")
                            acc = state.attributes.get("gps_accuracy")
                            _LOGGER.debug("Attributes for %s: lat=%s, lon=%s, acc=%s", entity_id, lat, lon, acc)

                            if _valid_coords(lat, lon):
                                device_info.update(
                                    {
                                        "latitude": lat,
                                        "longitude": lon,
                                        "accuracy": acc,
                                        "status": "Using historical data",
                                    }
                                )
                                _LOGGER.info("Using historical location for %s: lat=%s, lon=%s", device_name, lat, lon)
                            else:
                                _LOGGER.debug("No historical coordinates for %s (id=%s)", device_name, device_id)
                    except Exception as e:
                        _LOGGER.debug("Error retrieving historical data for %s: %s", device_name, e)
                        _LOGGER.debug("No cached/historical location data for %s (id=%s)", device_name, device_id)

                device_data.append(device_info)

            _LOGGER.debug(
                "Sequential polling: processed %d devices, next poll in %ds",
                len(devices),
                max(0, int(effective_poll_interval - time_since_last_poll)),
            )

            # Debug log what we're returning
            for d in device_data:
                _LOGGER.debug(
                    "Returning %s: lat=%s, lon=%s, status=%s, semantic=%s",
                    d["name"],
                    d.get("latitude"),
                    d.get("longitude"),
                    d.get("status"),
                    d.get("semantic_name"),
                )

            return device_data

        except Exception as exception:
            raise UpdateFailed(exception) from exception

    async def _async_load_stats(self) -> None:
        """Load statistics from cache."""
        try:
            from .Auth.token_cache import async_get_cached_value

            cached_stats = await async_get_cached_value("integration_stats")
            if cached_stats and isinstance(cached_stats, dict):
                for key in self.stats.keys():
                    if key in cached_stats:
                        self.stats[key] = int(cached_stats[key])
                _LOGGER.debug("Loaded statistics from cache: %s", self.stats)
        except Exception as e:
            _LOGGER.debug("Failed to load statistics from cache: %s", e)

    async def _async_save_stats(self) -> None:
        """Save statistics to cache."""
        try:
            from .Auth.token_cache import async_set_cached_value

            await async_set_cached_value("integration_stats", self.stats.copy())
        except Exception as e:
            _LOGGER.debug("Failed to save statistics to cache: %s", e)

    def increment_stat(self, stat_name: str) -> None:
        """Increment a statistic counter and schedule persistence."""
        if stat_name in self.stats:
            old_value = self.stats[stat_name]
            self.stats[stat_name] = int(old_value) + 1
            _LOGGER.debug("Incremented %s from %s to %s", stat_name, old_value, self.stats[stat_name])
            # Schedule async save to avoid blocking
            self.hass.async_create_task(self._async_save_stats())
        else:
            _LOGGER.debug("Tried to increment unknown stat %s (available: %s)", stat_name, list(self.stats.keys()))

    async def async_locate_device(self, device_id: str) -> dict:
        """Locate a device."""
        return await self.hass.async_add_executor_job(self.api.locate_device, device_id)

    async def async_play_sound(self, device_id: str) -> bool:
        """Play sound on a device."""
        return await self.hass.async_add_executor_job(self.api.play_sound, device_id)
