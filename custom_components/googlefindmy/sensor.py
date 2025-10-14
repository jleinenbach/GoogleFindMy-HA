# custom_components/googlefindmy/sensor.py
"""Sensor entities for Google Find My Device integration.

Exposes:
- Per-device `last_seen` timestamp sensors (restore-friendly).
- Optional integration diagnostic counters (stats), toggled via options.

Best practices:
- Device names are synced from the coordinator once known; user-assigned names are never overwritten.
- No placeholder names are written to the device registry on cold boot.
- DeviceInfo uses Primary fields only; never sets default_* keys to avoid misclassification.
"""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,  # stores native_value
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    DOMAIN,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_ENABLE_STATS_ENTITIES,
    DEFAULT_ENABLE_STATS_ENTITIES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
)
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)

# ----------------------------- Entity Descriptions -----------------------------

LAST_SEEN_DESCRIPTION = SensorEntityDescription(
    key="last_seen",
    translation_key="last_seen",
    icon="mdi:clock-outline",
    device_class=SensorDeviceClass.TIMESTAMP,
)

# NOTE: HA Quality Scale (Platinum): entity descriptions define translation_key,
# icon and state_class; keys must match coordinator.stats counters exactly.
# `skipped_duplicates` was removed from the coordinator and is intentionally absent here.
STATS_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    "background_updates": SensorEntityDescription(
        key="background_updates",
        translation_key="stat_background_updates",
        icon="mdi:cloud-download",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "crowd_sourced_updates": SensorEntityDescription(
        key="crowd_sourced_updates",
        translation_key="stat_crowd_sourced_updates",
        icon="mdi:account-group",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    # Unified counter for all updates dropped by the significance filter.
    "non_significant_dropped": SensorEntityDescription(
        key="non_significant_dropped",
        translation_key="stat_non_significant_dropped",
        icon="mdi:filter-variant-remove",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
}


def _maybe_update_device_registry_name(hass: HomeAssistant, entity_id: str, new_name: str) -> None:
    """Write the real Google device label into the device registry once known.

    Never touch if the user renamed the device (name_by_user is set).
    """
    try:
        ent_reg = er.async_get(hass)
        ent = ent_reg.async_get(entity_id)
        if not ent or not ent.device_id:
            return
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get(ent.device_id)
        # Respect user overrides
        if not dev or dev.name_by_user:
            return
        if new_name and dev.name != new_name:
            dev_reg.async_update_device(device_id=ent.device_id, name=new_name)
            _LOGGER.debug(
                "Device registry name updated for %s: '%s' -> '%s'",
                entity_id,
                dev.name,
                new_name,
            )
    except Exception as e:  # noqa: BLE001
        _LOGGER.debug("Device registry name update failed for %s: %s", entity_id, e)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device sensor entities."""
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []
    known_ids: set[str] = set()

    # Options-first toggle for diagnostic counters (single source of truth)
    enable_stats = entry.options.get(
        OPT_ENABLE_STATS_ENTITIES,
        entry.data.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES),
    )
    if enable_stats:
        created_stats = []
        for stat_key, desc in STATS_DESCRIPTIONS.items():
            entities.append(GoogleFindMyStatsSensor(coordinator, stat_key, desc))
            created_stats.append(stat_key)
        # Helpful debug to verify which stats sensors are created at setup time.
        _LOGGER.debug("Stats sensors created: %s", ", ".join(created_stats))

    # Per-device last_seen sensors from current snapshot
    if coordinator.data:
        for device in coordinator.data:
            dev_id = device.get("id")
            dev_name = device.get("name")
            if dev_id and dev_name:
                entities.append(GoogleFindMyLastSeenSensor(coordinator, device))
                known_ids.add(dev_id)
            else:
                _LOGGER.debug("Skipping device without id/name: %s", device)

    if entities:
        async_add_entities(entities, True)

    # Dynamically add sensors when new devices appear later
    @callback
    def _add_new_sensors_on_update() -> None:
        try:
            new_entities: list[SensorEntity] = []
            for device in (getattr(coordinator, "data", None) or []):
                dev_id = device.get("id")
                dev_name = device.get("name")
                if not dev_id or not dev_name or dev_id in known_ids:
                    continue
                new_entities.append(GoogleFindMyLastSeenSensor(coordinator, device))
                known_ids.add(dev_id)

            if new_entities:
                _LOGGER.info(
                    "Discovered %d new devices; adding last_seen sensors", len(new_entities)
                )
                async_add_entities(new_entities, True)
        except (AttributeError, TypeError) as err:
            _LOGGER.debug("Dynamic sensor add failed: %s", err)

    unsub = coordinator.async_add_listener(_add_new_sensors_on_update)
    entry.async_on_unload(unsub)


# ------------------------------- Stats Sensor ---------------------------------


class GoogleFindMyStatsSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic counters for the integration.

    Naming policy (HA Quality Scale – Platinum):
    - Do **not** set a hard-coded `_attr_name`. We rely on `entity_description.translation_key`
      and `_attr_has_entity_name = True` so HA composes the visible name as
      "<device name> <translated entity name>". This ensures locale-correct UI strings.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True  # allow translated entity name to be used

    def __init__(
        self, coordinator: GoogleFindMyCoordinator, stat_key: str, description: SensorEntityDescription
    ) -> None:
        """Initialize the stats sensor.

        Args:
            coordinator: The integration's data coordinator.
            stat_key: Name of the counter in coordinator.stats.
            description: Home Assistant entity description (icon, translation_key, etc.).
        """
        super().__init__(coordinator)
        self._stat_key = stat_key
        self.entity_description = description
        self._attr_unique_id = f"{DOMAIN}_{stat_key}"
        # Intentionally no `_attr_name` here; translations drive the visible name.
        self._attr_native_unit_of_measurement = "updates"

    @property
    def native_value(self) -> int | None:
        """Return the current counter value."""
        stats = getattr(self.coordinator, "stats", None)
        if stats is None:
            return None
        return stats.get(self._stat_key, 0)

    @property
    def device_info(self) -> DeviceInfo:
        """Expose a single integration device for diagnostic sensors."""
        return DeviceInfo(
            identifiers={(DOMAIN, "integration")},
            name="Google Find My Integration",
            manufacturer="BSkando",
            model="Find My Device Integration",
            configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
            # Mark as a service device to hide the "Delete device" action in HA UI.
            # (Still allows showing diagnostic entities on this device.)
            entry_type=dr.DeviceEntryType.SERVICE,
        )


# ----------------------------- Per-Device Last Seen ---------------------------


class GoogleFindMyLastSeenSensor(CoordinatorEntity, RestoreSensor):
    """Per-device sensor exposing the last_seen timestamp.

    Behavior:
    - Restores the last native value on startup and seeds the coordinator cache.
    - Updates on coordinator ticks and keeps the registry name aligned with Google's label.
    - Never writes a placeholder name to the device registry.
    """

    # Best practice: let HA compose "<Device Name> <translated entity name>"
    _attr_has_entity_name = True
    # Default to disabled in the registry for per-device sensors
    _attr_entity_registry_enabled_default = False
    entity_description = LAST_SEEN_DESCRIPTION

    def __init__(self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device = device
        self._device_id: str | None = device.get("id")
        safe_id = self._device_id if self._device_id is not None else "unknown"
        self._attr_unique_id = f"{DOMAIN}_{safe_id}_last_seen"
        self._attr_native_value: datetime | None = None

    @property
    def available(self) -> bool:
        """Mirror device presence in availability."""
        dev_id = self._device_id
        if not dev_id:
            return False
        try:
            if hasattr(self.coordinator, "is_device_present"):
                return self.coordinator.is_device_present(dev_id)
        except Exception:
            # Be tolerant if a different coordinator build is used.
            pass
        # Fallback: consider available if we have any known last_seen value
        return self._attr_native_value is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update native timestamp and keep the device label in sync."""
        # 1) Keep the raw device name synchronized with the coordinator snapshot.
        try:
            my_id = self._device_id or ""
            for dev in (getattr(self.coordinator, "data", None) or []):
                if dev.get("id") == my_id:
                    new_name = dev.get("name")
                    if new_name and new_name != self._device.get("name"):
                        self._device["name"] = new_name
                        _maybe_update_device_registry_name(self.hass, self.entity_id, new_name)
                    break
        except (AttributeError, TypeError) as e:  # noqa: BLE001
            _LOGGER.debug("Name refresh failed for %s: %s", self._device_id, e)

        # 2) Update last_seen when a valid value is available; otherwise keep the previous value.
        previous = self._attr_native_value
        new_dt: datetime | None = None
        try:
            value = (
                self.coordinator.get_device_last_seen(self._device_id)
                if self._device_id
                else None
            )
            if isinstance(value, datetime):
                new_dt = value
            elif isinstance(value, (int, float)):
                new_dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
            elif isinstance(value, str):
                v = value.strip()
                if v.endswith("Z"):
                    v = v.replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(v)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    new_dt = dt
                except ValueError:
                    new_dt = None
        except (AttributeError, TypeError, ValueError) as e:  # noqa: BLE001
            _LOGGER.debug(
                "Invalid last_seen for %s: %s", self._device.get("name", self._device_id), e
            )
            new_dt = None

        if new_dt is not None:
            self._attr_native_value = new_dt
        elif previous is not None:
            _LOGGER.debug(
                "Keeping previous last_seen for %s (no update available)",
                self._device.get("name", self._device_id),
            )

        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore last_seen from HA's persistent store and seed coordinator cache."""
        await super().async_added_to_hass()

        # Use RestoreSensor API to get the last native value (may be datetime/str/number)
        try:
            data = await self.async_get_last_sensor_data()
            value = getattr(data, "native_value", None) if data else None
        except (RuntimeError, AttributeError) as e:  # noqa: BLE001
            _LOGGER.debug("Failed to restore sensor state for %s: %s", self.entity_id, e)
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
                    ts = float(v)  # numeric string fallback
            elif isinstance(value, datetime):
                ts = value.timestamp()
        except (ValueError, TypeError) as ex:  # noqa: BLE001
            _LOGGER.debug(
                "Could not parse restored value '%s' for %s: %s", value, self.entity_id, ex
            )
            ts = None

        if ts is None or not self._device_id:
            return

        # Seed coordinator cache using its public API (no private access).
        try:
            self.coordinator.seed_device_last_seen(self._device_id, ts)
        except (AttributeError, TypeError) as e:  # noqa: BLE001
            _LOGGER.debug("Failed to seed coordinator cache for %s: %s", self.entity_id, e)
            return

        # Set our native value now (no need to wait for next coordinator tick)
        self._attr_native_value = datetime.fromtimestamp(ts, tz=timezone.utc)
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return per-device info without writing placeholders to the registry.

        Notes:
            - Only provide `name` when we have a real Google label; otherwise omit it.
            - Include a stable configuration_url pointing to the per-device map.
        """
        auth_token = self._get_map_token()
        path = self._build_map_path(self._device["id"], auth_token, redirect=False)

        try:
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

        # Only provide a name if we have a real device label (no bootstrap placeholder)
        raw_name = (self._device.get("name") or "").strip()
        use_name = raw_name if raw_name and raw_name != "Google Find My Device" else None

        return DeviceInfo(
            identifiers={(DOMAIN, self._device["id"])},
            name=use_name,  # may be None; that's OK when identifiers are provided
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}",
            serial_number=self._device["id"],
        )

    @staticmethod
    def _build_map_path(device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host)."""
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a simple token for map authentication (options-first)."""
        config_entry = getattr(self.coordinator, "config_entry", None)
        if config_entry:
            token_expiration_enabled = config_entry.options.get(
                OPT_MAP_VIEW_TOKEN_EXPIRATION,
                config_entry.data.get(OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION),
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
