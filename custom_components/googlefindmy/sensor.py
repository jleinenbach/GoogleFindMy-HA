"""Sensor entities for Google Find My Device integration."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,  # built-in restore for sensors
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device sensor entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Explicit typing for readability and IDE support
    entities: list[SensorEntity] = []

    # Add global statistics sensors (for the integration itself) if enabled
    if entry.data.get("enable_stats_entities", True):
        entities.extend(
            [
                GoogleFindMyStatsSensor(coordinator, "skipped_duplicates", "Skipped Duplicates"),
                GoogleFindMyStatsSensor(coordinator, "background_updates", "Background Updates"),
                GoogleFindMyStatsSensor(coordinator, "crowd_sourced_updates", "Crowd-sourced Updates"),
            ]
        )

    # Add per-device last_seen sensors if we have device data
    if coordinator.data:
        for device in coordinator.data:
            # Guard against malformed device dicts
            dev_id = device.get("id")
            dev_name = device.get("name")
            if dev_id and dev_name:
                entities.append(GoogleFindMyLastSeenSensor(coordinator, device))
            else:
                _LOGGER.warning("Skipping device due to missing 'id' or 'name': %s", device)

    async_add_entities(entities)


class GoogleFindMyStatsSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Google Find My Device statistics."""

    def __init__(self, coordinator, stat_key: str, stat_name: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._stat_key = stat_key
        self._stat_name = stat_name
        self._attr_name = f"Google Find My {stat_name}"
        self._attr_unique_id = f"{DOMAIN}_{stat_key}"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = "updates"

    @property
    def state(self) -> int | None:
        """Return the state of the sensor."""
        stats = getattr(self.coordinator, "stats", None)
        if stats is None:
            return None
        value = stats.get(self._stat_key, 0)
        _LOGGER.debug("Sensor %s returning value %s", self._stat_name, value)
        return value

    @property
    def icon(self) -> str:
        """Return the icon for the sensor."""
        if "duplicate" in self._stat_key:
            return "mdi:cancel"
        elif "background" in self._stat_key:
            return "mdi:cloud-download"
        elif "crowd" in self._stat_key:
            return "mdi:account-group"
        return "mdi:counter"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info for integration device."""
        return {
            "identifiers": {(DOMAIN, "integration")},
            "name": "Google Find My Integration",
            "manufacturer": "BSkando",
            "model": "Find My Device Integration",
            "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
        }


class GoogleFindMyLastSeenSensor(CoordinatorEntity, RestoreSensor):
    """Sensor showing last_seen timestamp for each device."""

    def __init__(self, coordinator, device: dict[str, Any]) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device = device
        self._device_id: str | None = device.get("id")
        safe_id = self._device_id if self._device_id is not None else "unknown"
        self._device_name: str = device.get("name", f"Unknown Device {safe_id}")
        self._attr_name = "Last Seen"
        self._attr_unique_id = f"{DOMAIN}_{safe_id}_last_seen"
        # Use native timestamp semantics so HA can persist/restore properly
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_has_entity_name = True
        self._attr_native_value: datetime | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update native timestamp from coordinator.

        Prefer a public coordinator API if available; otherwise fall back
        to the legacy internal cache for backward compatibility.
        """
        try:
            if hasattr(self.coordinator, "get_device_last_seen") and self._device_id:
                # Expected signature: get_device_last_seen(device_id) -> datetime | None
                value = self.coordinator.get_device_last_seen(self._device_id)  # type: ignore[attr-defined]
                self._attr_native_value = value
            else:
                mapping = getattr(self.coordinator, "_device_location_data", {})
                ts = mapping.get(self._device_id, {}).get("last_seen") if self._device_id else None
                self._attr_native_value = (
                    datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts is not None else None
                )
        except (ValueError, TypeError) as e:
            _LOGGER.debug("Invalid last_seen for %s: %s", self._device_name, e)
            self._attr_native_value = None

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore last_seen from HA's persistent store and seed coordinator cache."""
        await super().async_added_to_hass()

        # Use RestoreSensor API to get the last native value (may be datetime/str/number)
        try:
            data = await self.async_get_last_sensor_data()
            value = getattr(data, "native_value", None) if data else None
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("Failed to restore sensor state for %s: %s", self.entity_id, e)
            value = None

        if value in (None, "unknown", "unavailable"):
            return

        # Parse restored value -> epoch seconds for coordinator cache
        ts: float | None = None
        try:
            if isinstance(value, (int, float)):
                ts = float(value)
            elif isinstance(value, str):
                v = value.strip()
                if v.endswith("Z"):
                    v = v.replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(v)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    ts = dt.timestamp()
                except ValueError as ex:
                    _LOGGER.debug("Could not parse restored ISO value '%s' for %s: %s", value, self.entity_id, ex)
                    ts = float(v)  # try numeric string
            elif isinstance(value, datetime):
                ts = value.timestamp()
        except (ValueError, TypeError) as ex:
            _LOGGER.debug("Could not parse restored value '%s' for %s: %s", value, self.entity_id, ex)
            ts = None

        if ts is None or not self._device_id:
            return

        # Seed coordinator cache so native_value is available immediately after restart.
        try:
            if hasattr(self.coordinator, "seed_device_last_seen"):
                # Expected signature: seed_device_last_seen(device_id, timestamp: float) -> None
                self.coordinator.seed_device_last_seen(self._device_id, ts)  # type: ignore[attr-defined]
            else:
                mapping = getattr(self.coordinator, "_device_location_data", {})
                slot = mapping.setdefault(self._device_id, {})
                # Guard: do not override fresh data if coordinator already has last_seen
                slot.setdefault("last_seen", ts)
                setattr(self.coordinator, "_device_location_data", mapping)
        except Exception as e:  # noqa: BLE001
            _LOGGER.debug("Failed to seed coordinator cache for %s: %s", self._device_name, e)

        # Set our native value now (no need to wait for next coordinator tick)
        self._attr_native_value = datetime.fromtimestamp(ts, tz=timezone.utc)
        self.async_write_ha_state()

    @property
    def icon(self) -> str:
        """Return the icon for the sensor."""
        return "mdi:clock-outline"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info.

        NOTE (prep for later path refactor):
        Build the *path* separately so we can switch to returning a relative
        configuration_url in a later step without touching other code.
        """
        path = self._build_map_path(self._device["id"], self._get_map_token(), redirect=False)

        # Today we still return an absolute URL to avoid changing behavior now.
        from homeassistant.helpers.network import get_url

        try:
            base_url = get_url(
                self.hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except Exception:
            base_url = "http://homeassistant.local:8123"

        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device["name"],
            "manufacturer": "Google",
            "model": "Find My Device",
            "configuration_url": f"{base_url}{path}",
            "hw_version": self._device["id"],
        }

    def _build_map_path(self, device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host).

        Using a dedicated builder avoids later code churn when switching to relative URLs.
        """
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a simple token for map authentication.

        Weekly-rotating token when enabled; otherwise a static token.
        """
        import hashlib
        import time
        from .const import DEFAULT_MAP_VIEW_TOKEN_EXPIRATION

        # Check if token expiration is enabled - prefer options over data (options-first)
        config_entry = getattr(self.coordinator, "config_entry", None)
        if config_entry:
            token_expiration_enabled = config_entry.options.get(
                "map_view_token_expiration",
                config_entry.data.get("map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION),
            )
        else:
            token_expiration_enabled = DEFAULT_MAP_VIEW_TOKEN_EXPIRATION

        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))

        if token_expiration_enabled:
            # Weekly-rolling token (7-day bucket)
            week = str(int(time.time() // 604800))
            token_src = f"{ha_uuid}:{week}"
        else:
            # Static token (no rotation)
            token_src = f"{ha_uuid}:static"

        return hashlib.md5(token_src.encode()).hexdigest()[:16]
