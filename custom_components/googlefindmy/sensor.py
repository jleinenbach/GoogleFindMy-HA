"""Sensor entities for Google Find My Device integration."""
from __future__ import annotations

import hashlib
import logging
import time
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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device sensor entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    known_ids: set[str] = set()

    # Global statistics sensors (diagnostic)
    if entry.data.get("enable_stats_entities", True):
        entities.extend(
            [
                GoogleFindMyStatsSensor(coordinator, "skipped_duplicates", "Skipped Duplicates"),
                GoogleFindMyStatsSensor(coordinator, "background_updates", "Background Updates"),
                GoogleFindMyStatsSensor(coordinator, "crowd_sourced_updates", "Crowd-sourced Updates"),
            ]
        )

    # Per-device last_seen sensors
    if coordinator.data:
        for device in coordinator.data:
            dev_id = device.get("id")
            dev_name = device.get("name")
            if dev_id and dev_name:
                entities.append(GoogleFindMyLastSeenSensor(coordinator, device))
                known_ids.add(dev_id)
            else:
                _LOGGER.warning("Skipping device due to missing 'id' or 'name': %s", device)
    else:
        # Startup restore path: create skeletons from tracked_devices so Restore works immediately
        tracked_ids: list[str] = getattr(coordinator, "tracked_devices", []) or []
        for dev_id in tracked_ids:
            # Neutral default; do NOT leak technical device_id into the visible name.
            name = "Google Find My Device"
            entities.append(GoogleFindMyLastSeenSensor(coordinator, {"id": dev_id, "name": name}))
            known_ids.add(dev_id)
        if tracked_ids:
            _LOGGER.debug(
                "Created %d skeleton last_seen sensors for restore (no live data yet)",
                len(tracked_ids),
            )

    # Immediate state push so restored/native values are written right away
    if entities:
        async_add_entities(entities, True)

    # Dynamic entity creation: add sensors when new devices appear later
    @callback
    def _add_new_sensors_on_update() -> None:
        try:
            new_entities: list[SensorEntity] = []
            for device in getattr(coordinator, "data", []) or []:
                dev_id = device.get("id")
                dev_name = device.get("name")
                if not dev_id or not dev_name:
                    continue
                if dev_id in known_ids:
                    continue
                new_entities.append(GoogleFindMyLastSeenSensor(coordinator, device))
                known_ids.add(dev_id)

            if new_entities:
                _LOGGER.info("Discovered %d new devices; adding last_seen sensors", len(new_entities))
                async_add_entities(new_entities, True)
        except (AttributeError, TypeError) as err:
            _LOGGER.debug("Dynamic sensor add failed: %s", err)

    coordinator.async_add_listener(_add_new_sensors_on_update)


class GoogleFindMyStatsSensor(CoordinatorEntity, SensorEntity):
    """Sensor for Google Find My Device statistics."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, stat_key: str, stat_name: str) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._stat_key = stat_key
        self._attr_name = f"Google Find My {stat_name}"
        self._attr_unique_id = f"{DOMAIN}_{stat_key}"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_native_unit_of_measurement = "updates"

        # Variante A: icon einmalig setzen (stabil, performant)
        icon_map = {
            "skipped_duplicates": "mdi:cancel",
            "background_updates": "mdi:cloud-download",
            "crowd_sourced_updates": "mdi:account-group",
        }
        self._attr_icon = icon_map.get(stat_key, "mdi:counter")

    @property
    def state(self) -> int | None:
        """Return the state of the sensor."""
        stats = getattr(self.coordinator, "stats", None)
        if stats is None:
            return None
        value = stats.get(self._stat_key, 0)
        _LOGGER.debug("Sensor %s returning value %s", self._attr_name, value)
        return value

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info for the integration device."""
        return DeviceInfo(
            identifiers={(DOMAIN, "integration")},
            name="Google Find My Integration",
            manufacturer="BSkando",
            model="Find My Device Integration",
            configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
        )


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
        self._attr_device_class = SensorDeviceClass.TIMESTAMP  # native timestamp semantics
        self._attr_has_entity_name = True
        self._attr_native_value: datetime | None = None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update native timestamp and handle device name drift."""
        # Update display name from coordinator snapshot (no private maps).
        try:
            my_id = self._device_id or ""
            for dev in (getattr(self.coordinator, "data", None) or []):
                if dev.get("id") == my_id:
                    new_name = dev.get("name")
                    if new_name and new_name != self._device.get("name"):
                        self._device["name"] = new_name
                    break
        except (AttributeError, TypeError) as e:
            _LOGGER.debug("Name refresh failed for %s: %s", self._device_id, e)

        # Source last_seen strictly via public API; robust type handling.
        try:
            value = self.coordinator.get_device_last_seen(self._device_id) if self._device_id else None
            if isinstance(value, datetime):
                self._attr_native_value = value
            elif isinstance(value, (int, float)):
                self._attr_native_value = datetime.fromtimestamp(float(value), tz=timezone.utc)
            elif isinstance(value, str):
                v = value.strip()
                if v.endswith("Z"):
                    v = v.replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(v)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    self._attr_native_value = dt
                except ValueError:
                    self._attr_native_value = None
            else:
                self._attr_native_value = None
        except (AttributeError, TypeError, ValueError) as e:
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
        except (RuntimeError, AttributeError) as e:
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
                except ValueError:
                    ts = float(v)  # try numeric string
            elif isinstance(value, datetime):
                ts = value.timestamp()
        except (ValueError, TypeError) as ex:
            _LOGGER.debug("Could not parse restored value '%s' for %s: %s", value, self.entity_id, ex)
            ts = None

        if ts is None or not self._device_id:
            return

        # Seed coordinator cache using its public API (no private access).
        try:
            self.coordinator.seed_device_last_seen(self._device_id, ts)
        except (AttributeError, TypeError) as e:
            _LOGGER.debug("Failed to seed coordinator cache for %s: %s", self._device_name, e)
            return

        # Set our native value now (no need to wait for next coordinator tick)
        self._attr_native_value = datetime.fromtimestamp(ts, tz=timezone.utc)
        self.async_write_ha_state()

    @property
    def icon(self) -> str:
        """Return the icon for the sensor."""
        return "mdi:clock-outline"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        path = self._build_map_path(self._device["id"], self._get_map_token(), redirect=False)

        try:
            # Absolute base URL so the "Visit" link in device registry works from anywhere.
            base_url = get_url(
                self.hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except (HomeAssistantError, RuntimeError) as e:
            _LOGGER.debug("Could not determine Home Assistant URL, using fallback: %s", e)
            base_url = "http://homeassistant.local:8123"

        return DeviceInfo(
            identifiers={(DOMAIN, self._device["id"])},
            name=self._device["name"],
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}",
            serial_number=self._device["id"],  # semantically correct tech identifier
        )

    def _build_map_path(self, device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host)."""
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a simple token for map authentication (options-first)."""
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
            week = str(int(time.time() // 604800))  # weekly-rolling bucket
            token_src = f"{ha_uuid}:{week}"
        else:
            token_src = f"{ha_uuid}:static"

        return hashlib.md5(token_src.encode()).hexdigest()[:16]
