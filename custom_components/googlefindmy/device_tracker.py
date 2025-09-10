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
        
        # Get thresholds from config or use defaults
        config_data = coordinator.hass.data[DOMAIN].get("config_data", {})
        self._min_accuracy_threshold = config_data.get("min_accuracy_threshold", 100)  # meters
        self._staleness_threshold_hours = config_data.get("staleness_threshold_hours", 2.0)  # hours

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

    def _get_location_data(self) -> tuple[float, float, int] | None:
        """Get location data with basic validation."""
        device_data = self._current_device_data
        if not device_data:
            return None
            
        lat = device_data.get("latitude")
        lon = device_data.get("longitude")
        acc = device_data.get("accuracy", 50)
        
        # Basic validation - must have coordinates
        if lat is None or lon is None:
            return None
        
        # Optional accuracy filtering
        if acc and acc > self._min_accuracy_threshold:
            _LOGGER.debug(f"Ignoring location with poor accuracy: {acc}m for {self._attr_name}")
            return None
        
        return (lat, lon, acc)
    
    def _is_location_stale(self) -> bool:
        """Check if the current location data is stale based on last_seen timestamp."""
        device_data = self._current_device_data
        if not device_data:
            return True
            
        last_seen = device_data.get("last_seen")
        if last_seen is None:
            return True
            
        # Calculate time difference in hours
        current_time = time.time()
        time_diff_hours = (current_time - last_seen) / 3600
        
        return time_diff_hours > self._staleness_threshold_hours

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        result = self._get_location_data()
        return result[0] if result else None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        result = self._get_location_data()
        return result[1] if result else None

    @property
    def location_accuracy(self) -> int | None:
        """Return accuracy of location."""
        result = self._get_location_data()
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
            
            # Add configuration info
            attributes["accuracy_threshold"] = self._min_accuracy_threshold
            attributes["staleness_threshold_hours"] = self._staleness_threshold_hours
            
            # Add staleness information
            is_stale = self._is_location_stale()
            attributes["location_is_stale"] = is_stale
            
            if device_data.get("last_seen"):
                import datetime
                current_time = time.time()
                hours_since_update = (current_time - device_data["last_seen"]) / 3600
                attributes["hours_since_last_update"] = round(hours_since_update, 1)
        
        return attributes

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()