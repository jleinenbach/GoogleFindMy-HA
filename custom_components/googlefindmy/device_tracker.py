"""Device tracker platform for Google Find My Device."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.components.device_tracker.const import ATTR_GPS_ACCURACY
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.const import ATTR_LATITUDE, ATTR_LONGITUDE, PERCENTAGE
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

    entities: list[GoogleFindMyDeviceTracker] = []
    if coordinator.data:
        for device in coordinator.data:
            entities.append(GoogleFindMyDeviceTracker(coordinator, device))

    async_add_entities(entities, True)


class GoogleFindMyDeviceTracker(CoordinatorEntity, TrackerEntity, RestoreEntity):
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
        self._attr_battery_level: int | None = None
        self._attr_battery_unit = PERCENTAGE
        # Track last good accuracy location for database writes
        self._last_good_accuracy_data: dict[str, Any] | None = None

    async def async_added_to_hass(self) -> None:
        """Restore last known coordinates/battery after restart via RestoreEntity."""
        await super().async_added_to_hass()

        try:
            last_state = await self.async_get_last_state()
        except Exception:  # noqa: BLE001
            last_state = None

        if not last_state:
            return

        # Read standard device_tracker attributes (with fallbacks)
        lat = last_state.attributes.get(ATTR_LATITUDE, last_state.attributes.get("latitude"))
        lon = last_state.attributes.get(ATTR_LONGITUDE, last_state.attributes.get("longitude"))
        acc = last_state.attributes.get(ATTR_GPS_ACCURACY, last_state.attributes.get("gps_accuracy"))
        batt = last_state.attributes.get("battery_level")

        restored: dict[str, Any] = {}
        try:
            if lat is not None and lon is not None:
                restored["latitude"] = float(lat)
                restored["longitude"] = float(lon)
            if acc is not None:
                restored["accuracy"] = int(acc)
            if batt is not None:
                restored["battery_level"] = int(batt)
        except (TypeError, ValueError):
            # Ignore malformed persisted attributes
            restored = {}

        # Seed our own cache and the coordinator cache so properties return data immediately
        if restored:
            self._last_good_accuracy_data = {**restored}
            self._attr_battery_level = restored.get("battery_level")

            try:  # Prime coordinator cache used by other entities (e.g., map/last_seen)
                dev_id = self._device["id"]
                mapping = self.coordinator._device_location_data.get(dev_id, {})  # noqa: SLF001
                mapping.update(restored)
                self.coordinator._device_location_data[dev_id] = mapping  # noqa: SLF001
            except Exception:
                pass

            self.async_write_ha_state()

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info."""
        # Generate auth token for map access
        auth_token = self._get_map_token()

        # Get a base URL for the redirect endpoint - use local IP detection
        # The redirect endpoint will handle proper routing based on request origin
        try:
            import socket
            from homeassistant.helpers.network import get_url

            # Use socket connection method to get the actual local network IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()

            # Get HA port and SSL settings from config
            port = 8123
            use_ssl = False

            # Try to get actual port from HA configuration
            if hasattr(self.hass, "http") and hasattr(self.hass.http, "server_port"):
                port = self.hass.http.server_port or 8123
                use_ssl = hasattr(self.hass.http, "ssl_context") and self.hass.http.ssl_context is not None

            protocol = "https" if use_ssl else "http"
            base_url = f"{protocol}://{local_ip}:{port}"

        except Exception as e:
            _LOGGER.debug(f"Local IP detection failed: {e}, using fallback URL")
            # Fallback to HA's network detection
            try:
                base_url = get_url(
                    self.hass,
                    prefer_external=False,
                    allow_cloud=False,
                    allow_external=False,
                    allow_internal=True,
                )
            except Exception as fallback_e:
                _LOGGER.warning(f"All URL detection methods failed: {fallback_e}")
                base_url = "http://homeassistant.local:8123"

        # Use the redirect endpoint that will automatically detect the request origin
        # and redirect to the appropriate URL (local IP or cloud URL)
        redirect_url = f"{base_url}/api/googlefindmy/redirect_map/{self._device['id']}?token={auth_token}"

        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device["name"],
            "manufacturer": "Google",
            "model": "Find My Device",
            "configuration_url": redirect_url,
            "hw_version": self._device["id"],  # Show device ID as hardware version for easy copying
        }

    @property
    def _current_device_data(self) -> dict[str, Any] | None:
        """Get current device data from coordinator's location cache."""
        # Use cached location data which persists even when polling fails
        return self.coordinator._device_location_data.get(self._device["id"])  # noqa: SLF001

    @property
    def available(self) -> bool:
        """Return True if entity has valid location data."""
        # Stay available as long as we have coordinates, even if they're old
        device_data = self._current_device_data
        if device_data:
            lat = device_data.get("latitude")
            lon = device_data.get("longitude")
            semantic_name = device_data.get("semantic_name")
            _LOGGER.debug(
                "Device %s availability check: lat=%s, lon=%s, semantic_name=%s",
                self._device["name"],
                lat,
                lon,
                semantic_name,
            )
            # Available if we have both coordinates or a semantic location name
            is_available = (lat is not None and lon is not None) or (semantic_name is not None)
            _LOGGER.debug("Device %s available=%s", self._device["name"], is_available)
            return is_available
        _LOGGER.debug("Device %s has no device_data - unavailable", self._device["name"])
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
        attributes: dict[str, Any] = {}
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
        from .const import DEFAULT_MAP_VIEW_TOKEN_EXPIRATION

        # Check if token expiration is enabled in config
        config_entries = self.hass.config_entries.async_entries(DOMAIN)
        token_expiration_enabled = DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
        if config_entries:
            token_expiration_enabled = config_entries[0].data.get(
                "map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            )

        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))

        if token_expiration_enabled:
            # Use weekly expiration when enabled
            week = str(int(time.time() // 604800))  # Current week since epoch (7 days)
            return hashlib.md5(f"{ha_uuid}:{week}".encode()).hexdigest()[:16]
        else:
            # No expiration - use static token based on HA UUID only
            return hashlib.md5(f"{ha_uuid}:static".encode()).hexdigest()[:16]

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
                        "semantic_name": device_data.get("semantic_name"),
                    }
                    _LOGGER.debug(
                        "Updated last good accuracy data for %s: accuracy=%sm",
                        self._device["name"],
                        accuracy,
                    )
                else:
                    _LOGGER.info(
                        "Keeping previous good data for %s: current accuracy=%sm > threshold=%sm",
                        self._device["name"],
                        accuracy,
                        min_accuracy_threshold,
                    )
        elif device_data:
            # No filtering or no accuracy data - use current data
            self._last_good_accuracy_data = device_data

        self.async_write_ha_state()
