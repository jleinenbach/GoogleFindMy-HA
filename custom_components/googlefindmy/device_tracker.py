"""Device tracker platform for Google Find My Device."""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.const import PERCENTAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from math import radians, cos, sin, asin, sqrt

from .const import DOMAIN
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device tracker entities."""
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    entities = []
    if coordinator.data:
        for device in coordinator.data:
            entities.append(GoogleFindMyDeviceTracker(coordinator, device))

    async_add_entities(entities, True)


class GoogleFindMyDeviceTracker(CoordinatorEntity, TrackerEntity):
    """Representation of a Google Find My Device tracker."""

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the tracker."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{DOMAIN}_{device['id']}"
        self._attr_name = device["name"]
        self._attr_source_type = SourceType.GPS
        self._attr_has_entity_name = True
        # Set battery attributes for proper display
        self._attr_battery_level = None
        self._attr_battery_unit = PERCENTAGE
        
        # Location stability tracking
        self._last_valid_location = {"latitude": None, "longitude": None, "accuracy": None}
        self._location_history = []  # Store last 5 locations for smoothing
        self._max_history = 5
        self._cached_smoothed_location = None  # Cache the smoothed result
        self._last_raw_location = None  # Track raw location to detect changes
        self._coordinate_cache = {}  # Cache coordinates for this update cycle
        
        # Get thresholds from config or use defaults
        config_data = coordinator.hass.data[DOMAIN].get("config_data", {})
        self._min_accuracy_threshold = config_data.get("min_accuracy_threshold", 100)  # meters
        self._movement_threshold = config_data.get("movement_threshold", 15)  # Very small threshold for micro-movements
        self._home_stability_count = 0  # Count stable home readings
        self._stable_location = None  # The "locked" stable location
        self._stable_since = None  # When we locked onto this stable location

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device["name"],
            "manufacturer": "Google",
            "model": "Find My Device",
        }

    @property
    def _current_device_data(self) -> dict[str, Any] | None:
        """Get current device data from coordinator."""
        if self.coordinator.data:
            for device in self.coordinator.data:
                if device["id"] == self._device["id"]:
                    _LOGGER.debug(f"Device data for {self._device['name']}: lat={device.get('latitude')}, lon={device.get('longitude')}, acc={device.get('accuracy')}")
                    return device
        _LOGGER.debug(f"No device data found for {self._device['name']} (id={self._device['id']})")
        return None

    def _calculate_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two coordinates in meters using Haversine formula."""
        # Radius of earth in meters
        R = 6371000
        
        dLat = radians(lat2 - lat1)
        dLon = radians(lon2 - lon1)
        a = sin(dLat/2) * sin(dLat/2) + cos(radians(lat1)) * cos(radians(lat2)) * sin(dLon/2) * sin(dLon/2)
        c = 2 * asin(sqrt(a))
        
        return R * c
    
    def _validate_and_smooth_location(self, lat: float, lon: float, accuracy: int) -> tuple[float, float, int] | None:
        """Validate and smooth location data to reduce bouncing."""
        if lat is None or lon is None:
            return None
        
        # Check if this is the same raw location as last time
        current_raw = (lat, lon, accuracy)
        if self._last_raw_location == current_raw and self._cached_smoothed_location:
            # Same raw data, return cached smoothed result
            return self._cached_smoothed_location
        
        self._last_raw_location = current_raw
            
        # Filter out poor accuracy readings
        if accuracy and accuracy > self._min_accuracy_threshold:
            _LOGGER.debug(f"Ignoring location with poor accuracy: {accuracy}m")
            # Return cached smoothed location if we have one
            if self._cached_smoothed_location:
                return self._cached_smoothed_location
            return None
        
        # Check for unrealistic jumps if we have a previous location
        if self._last_valid_location["latitude"] is not None:
            distance = self._calculate_distance(
                self._last_valid_location["latitude"],
                self._last_valid_location["longitude"],
                lat, lon
            )
            
            # If distance is less than threshold, treat as stationary
            if distance < self._movement_threshold:
                _LOGGER.debug(f"Small movement detected ({distance:.1f}m), treating as stationary")
                
                self._home_stability_count += 1
                
                # Once we're stable, lock onto a fixed location
                if self._home_stability_count >= 2:
                    if self._stable_location is None:
                        # Lock onto current stable location
                        self._stable_location = {
                            "latitude": self._last_valid_location["latitude"],
                            "longitude": self._last_valid_location["longitude"]
                        }
                        self._stable_since = time.time()
                        _LOGGER.debug(f"Locked onto stable location: {self._stable_location['latitude']:.6f}, {self._stable_location['longitude']:.6f}")
                    
                    # Return the locked stable location - no more bouncing
                    result = (self._stable_location["latitude"], self._stable_location["longitude"], accuracy)
                    self._cached_smoothed_location = result
                    return result
                else:
                    # Still building stability - use current location but don't bounce
                    result = (self._last_valid_location["latitude"], self._last_valid_location["longitude"], accuracy)
                    self._cached_smoothed_location = result
                    return result
                
            else:
                # Significant movement - accept new location and reset stability
                _LOGGER.debug(f"Significant movement detected ({distance:.1f}m), accepting new location")
                self._location_history = [{"lat": lat, "lon": lon, "acc": accuracy}]
                self._home_stability_count = 0
                self._stable_location = None  # Clear stable lock
                self._stable_since = None
        else:
            # First location - accept it
            self._location_history = [{"lat": lat, "lon": lon, "acc": accuracy}]
            self._home_stability_count = 0
            self._stable_location = None
            self._stable_since = None
        
        # Update last valid location and cache
        self._last_valid_location = {
            "latitude": lat,
            "longitude": lon,
            "accuracy": accuracy
        }
        
        result = (lat, lon, accuracy)
        self._cached_smoothed_location = result
        return result
    
    def _get_smoothed_coordinates(self) -> tuple[float, float, int] | None:
        """Get smoothed coordinates, using cache to avoid duplicate processing."""
        device_data = self._current_device_data
        if not device_data:
            return None
            
        lat = device_data.get("latitude")
        lon = device_data.get("longitude")
        acc = device_data.get("accuracy", 50)
        
        # Create cache key from raw coordinates
        cache_key = (lat, lon, acc)
        
        # Check if we already processed these coordinates in this update cycle
        if cache_key in self._coordinate_cache:
            return self._coordinate_cache[cache_key]
        
        # Process and cache the result
        result = self._validate_and_smooth_location(lat, lon, acc)
        self._coordinate_cache[cache_key] = result
        
        # Clear old cache entries (keep only the latest)
        if len(self._coordinate_cache) > 1:
            self._coordinate_cache = {cache_key: result}
        
        return result

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        result = self._get_smoothed_coordinates()
        return result[0] if result else None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        result = self._get_smoothed_coordinates()
        return result[1] if result else None

    @property
    def location_accuracy(self) -> int | None:
        """Return accuracy of location."""
        device_data = self._current_device_data
        if not device_data:
            return None
            
        lat = device_data.get("latitude")
        lon = device_data.get("longitude")
        acc = device_data.get("accuracy", 50)
        
        result = self._validate_and_smooth_location(lat, lon, acc)
        return result[2] if result else None

    @property
    def battery_level(self) -> int | None:
        """Return battery level of the device."""
        device_data = self._current_device_data
        battery = device_data.get("battery_level") if device_data else None
        # Update the attr for consistency
        self._attr_battery_level = battery
        return battery
    
    @property
    def location_name(self) -> str | None:
        """Return the location name (zone or semantic location)."""
        device_data = self._current_device_data
        if not device_data:
            return None
        
        # If we have a semantic location, use it
        semantic_name = device_data.get("semantic_name")
        if semantic_name:
            return semantic_name
        
        # Otherwise return None to let HA determine zone/home/away
        return None
    
    # Let Home Assistant handle state logic - it will determine home/away/zone based on coordinates
    # We can override this later for semantic locations if needed
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attributes = {}
        device_data = self._current_device_data
        
        if device_data:
            # Add all available location attributes
            if "last_seen" in device_data and device_data["last_seen"] is not None:
                import datetime
                attributes["last_seen"] = datetime.datetime.fromtimestamp(device_data["last_seen"]).isoformat()
            
            # Don't duplicate battery_level since it's a primary attribute
            # It will be shown in the UI automatically
            
            if "altitude" in device_data and device_data["altitude"] is not None:
                attributes["altitude"] = device_data["altitude"]
            
            if "status" in device_data and device_data["status"] is not None:
                attributes["device_status"] = device_data["status"]
            
            if "is_own_report" in device_data and device_data["is_own_report"] is not None:
                attributes["is_own_report"] = device_data["is_own_report"]
            
            if "semantic_name" in device_data and device_data["semantic_name"] is not None:
                attributes["semantic_location"] = device_data["semantic_name"]
            
            # Add polling status info
            attributes["polling_status"] = device_data.get("status", "Unknown")
            
            # Add stability metrics
            attributes["location_stability"] = len(self._location_history)
            attributes["movement_threshold"] = self._movement_threshold
            attributes["accuracy_threshold"] = self._min_accuracy_threshold
        
        return attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()