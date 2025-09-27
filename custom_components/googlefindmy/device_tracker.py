"""Device tracker platform for Google Find My Device."""
from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.const import PERCENTAGE
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.restore_state import RestoreEntity

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
        self._attr_battery_level = None
        self._attr_battery_unit = PERCENTAGE
        # Track last good accuracy location for database writes
        self._last_good_accuracy_data: Optional[dict[str, Any]] = None

    async def async_added_to_hass(self) -> None:
        """Restore previous state and seed last-known cache on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state:
            return

        lat = last_state.attributes.get("latitude")
        lon = last_state.attributes.get("longitude")
        acc = last_state.attributes.get("gps_accuracy")
        if lat is not None and lon is not None:
            data = {
                "latitude": lat,
                "longitude": lon,
                "accuracy": acc,
                "last_seen": None,
                "last_updated": None,
            }
            try:
                # Seed coordinator's last-known cache so we can render immediately
                self.coordinator.set_last_known(self._device["id"], data, source="restore")
                self.async_write_ha_state()
            except Exception:  # defensive: never break startup on restore
                pass

    def _live_device_data(self) -> Optional[dict[str, Any]]:
        """Get current live device data from coordinator (current session)."""
        return self.coordinator._device_location_data.get(self._device["id"])

    def _last_known_data(self) -> Optional[dict[str, Any]]:
        """Get last-known (persisted) data from coordinator (across restarts)."""
        return self.coordinator.get_last_known(self._device["id"])

    def _data_for_read(self) -> Optional[dict[str, Any]]:
        """Choose best-available data for readout/order: last-good > live > last-known."""
        return self._last_good_accuracy_data or self._live_device_data() or self._last_known_data()

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
            if hasattr(self.hass, 'http') and hasattr(self.hass.http, 'server_port'):
                port = self.hass.http.server_port or 8123
                use_ssl = hasattr(self.hass.http, 'ssl_context') and self.hass.http.ssl_context is not None

            protocol = "https" if use_ssl else "http"
            base_url = f"{protocol}://{local_ip}:{port}"

        except Exception as e:
            _LOGGER.debug(f"Local IP detection failed: {e}, using fallback URL")
            # Fallback to HA's network detection
            try:
                base_url = get_url(self.hass, prefer_external=False, allow_cloud=False, allow_external=False, allow_internal=True)
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
    def available(self) -> bool:
        """Return True if entity has valid location data."""
        data = self._data_for_read()
        if data:
            lat = data.get("latitude")
            lon = data.get("longitude")
            semantic_name = data.get("semantic_name")
            _LOGGER.debug(
                "Device %s availability check: lat=%s, lon=%s, semantic_name=%s",
                self._device["name"], lat, lon, semantic_name
            )
            return (lat is not None and lon is not None) or (semantic_name is not None)
        _LOGGER.debug("Device %s has no device_data - unavailable", self._device["name"])
        return False

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        data = self._data_for_read()
        return data.get("latitude") if data else None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        data = self._data_for_read()
        return data.get("longitude") if data else None

    @property
    def location_accuracy(self) -> int | None:
        """Return accuracy of location."""
        data = self._data_for_read()
        return data.get("accuracy") if data else None

    @property
    def battery_level(self) -> int | None:
        """Return battery level of the device."""
        data = self._data_for_read() or self._live_device_data()
        battery = data.get("battery_level") if data else None
        # Update the attr for consistency
        self._attr_battery_level = battery
        return battery
    
    @property
    def location_name(self) -> str | None:
        """Return the location name (zone or semantic location)."""
        data = self._data_for_read()
        if not data:
            return None
        semantic_name = data.get("semantic_name")
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
        data = self._data_for_read()

        if data:
            # last_seen (epoch seconds) → ISO8601
            if "last_seen" in data and data["last_seen"] is not None:
                import datetime
                try:
                    attributes["last_seen"] = datetime.datetime.fromtimestamp(float(data["last_seen"])).isoformat()
                except Exception:
                    attributes["last_seen"] = data["last_seen"]

            if "altitude" in data and data["altitude"] is not None:
                attributes["altitude"] = data["altitude"]

            if "status" in data and data["status"] is not None:
                attributes["device_status"] = data["status"]

            if "is_own_report" in data and data["is_own_report"] is not None:
                attributes["is_own_report"] = data["is_own_report"]

            if "semantic_name" in data and data["semantic_name"] is not None:
                attributes["semantic_location"] = data["semantic_name"]

            # Persisted source marker (poll | restore | store)
            if "source" in data:
                attributes["gfm_source"] = data["source"]

            # Polling status info (fallback for UI)
            attributes["polling_status"] = data.get("status", "Unknown")

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
            token_expiration_enabled = config_entries[0].data.get("map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION)

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

        # Prefer live data for evaluating "last good" accuracy
        device_data = self._live_device_data() or self._last_known_data()

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
                        "source": device_data.get("source"),
                    }
                    _LOGGER.debug(
                        "Updated last good accuracy data for %s: accuracy=%sm",
                        self._device["name"], accuracy
                    )
                else:
                    _LOGGER.info(
                        "Keeping previous good data for %s: current accuracy=%sm > threshold=%sm",
                        self._device["name"], accuracy, min_accuracy_threshold
                    )
        elif device_data:
            # No filtering or no accuracy data - use current data
            self._last_good_accuracy_data = device_data

        self.async_write_ha_state()
