"""Device tracker platform for Google Find My Device."""
from __future__ import annotations

import logging
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
        self._attr_has_entity_name = False
        self._attr_entity_category = None  # Ensure device trackers are not diagnostic
        # Set battery attributes for proper display
        self._attr_battery_level = None
        self._attr_battery_unit = PERCENTAGE
        # Track last good accuracy location for database writes
        self._last_good_accuracy_data = None

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        # Get Home Assistant base URL using proper HA methods
        from homeassistant.helpers.network import get_url

        try:
            # Try to get the best available URL, preferring external access
            # This follows HA's built-in URL priority: cloud -> external -> internal
            base_url = get_url(self.hass, prefer_external=True, allow_cloud=True, allow_external=True, allow_internal=True)
            _LOGGER.info(f"Using URL for device {self._device['name']}: {base_url}")

        except Exception as e:
            _LOGGER.warning(f"Error getting URL for device {self._device['name']}: {e}")
            base_url = "http://homeassistant.local:8123"
            _LOGGER.warning(f"Using default URL for device {self._device['name']}: {base_url}")

        # Generate auth token for map access
        auth_token = self._get_map_token()

        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device["name"],
            "manufacturer": "Google",
            "model": "Find My Device",
            "configuration_url": f"{base_url}/api/googlefindmy/map/{self._device['id']}?token={auth_token}",
            "hw_version": self._device["id"],  # Show device ID as hardware version for easy copying
        }

    @property
    def _current_device_data(self) -> dict[str, Any] | None:
        """Get current device data from coordinator."""
        if self.coordinator.data:
            for device in self.coordinator.data:
                if device["id"] == self._device["id"]:
                    return device
        return None


    @property
    def available(self) -> bool:
        """Return True if entity has valid location data."""
        # Stay available as long as we have coordinates, even if they're old
        device_data = self._current_device_data
        if device_data:
            lat = device_data.get("latitude")
            lon = device_data.get("longitude")
            semantic_name = device_data.get("semantic_name")
            _LOGGER.debug(f"Device {self._device['name']} availability check: lat={lat}, lon={lon}, semantic_name={semantic_name}")
            # Available if we have both coordinates or a semantic location name
            is_available = (lat is not None and lon is not None) or (semantic_name is not None)
            _LOGGER.debug(f"Device {self._device['name']} available={is_available}")
            return is_available
        _LOGGER.debug(f"Device {self._device['name']} has no device_data - unavailable")
        return False

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        # Return filtered data for database writes
        data_to_use = self._last_good_accuracy_data if self._last_good_accuracy_data else self._current_device_data
        if not data_to_use:
            return None
        lat = data_to_use.get("latitude")
        return lat if lat is not None else None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        # Return filtered data for database writes
        data_to_use = self._last_good_accuracy_data if self._last_good_accuracy_data else self._current_device_data
        if not data_to_use:
            return None
        lon = data_to_use.get("longitude")
        return lon if lon is not None else None

    @property
    def location_accuracy(self) -> int | None:
        """Return accuracy of location."""
        # Return filtered data for database writes
        data_to_use = self._last_good_accuracy_data if self._last_good_accuracy_data else self._current_device_data
        if not data_to_use:
            return None
        acc = data_to_use.get("accuracy")
        return acc if acc is not None else None

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
        
        return attributes

    def _get_map_token(self) -> str:
        """Generate a simple token for map authentication."""
        import hashlib
        import time
        # Use HA's UUID and current day to create a simple token
        day = str(int(time.time() // 86400))  # Current day since epoch
        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))
        return hashlib.md5(f"{ha_uuid}:{day}".encode()).hexdigest()[:16]

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Get accuracy threshold from config
        config_data = self.hass.data[DOMAIN].get("config_data", {})
        min_accuracy_threshold = config_data.get("min_accuracy_threshold", 0)

        # Get current device data
        device_data = self._current_device_data

        if device_data and min_accuracy_threshold > 0:
            accuracy = device_data.get("accuracy")
            lat = device_data.get("latitude")
            lon = device_data.get("longitude")

            # Check if this is good accuracy data
            if accuracy is not None and lat is not None and lon is not None:
                if accuracy <= min_accuracy_threshold:
                    # Good accuracy - save as last good data
                    self._last_good_accuracy_data = {
                        "latitude": lat,
                        "longitude": lon,
                        "accuracy": accuracy,
                        "last_seen": device_data.get("last_seen"),
                        "altitude": device_data.get("altitude"),
                        "battery_level": device_data.get("battery_level"),
                        "status": device_data.get("status"),
                        "is_own_report": device_data.get("is_own_report"),
                        "semantic_name": device_data.get("semantic_name")
                    }
                    _LOGGER.debug(f"Updated last good accuracy data for {self._device['name']}: accuracy={accuracy}m")
                else:
                    _LOGGER.info(f"Keeping previous good data for {self._device['name']}: current accuracy={accuracy}m > threshold={min_accuracy_threshold}m")
        elif device_data:
            # No filtering or no accuracy data - use current data
            self._last_good_accuracy_data = device_data

        self.async_write_ha_state()