# custom_components/googlefindmy/sensor.py
"""Sensor entities for Google Find My Device integration.

Exposes:
- Per-device `last_seen` timestamp sensors (restore-friendly).
- Optional integration diagnostic counters (stats), toggled via options.

Best practices:
- Device names are synced from the coordinator once known; user-assigned names are never overwritten.
- No placeholder names are written to the device registry on cold boot.
- End devices link to the per-entry *service device* via `via_device=(DOMAIN, f"integration_{entry_id}")`.
- Sensors default to **enabled** so a fresh installation is immediately functional.
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
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_ENABLE_STATS_ENTITIES,
    DEFAULT_ENABLE_STATS_ENTITIES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    SERVICE_DEVICE_NAME,
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_MANUFACTURER,
    service_device_identifier,
)
from .coordinator import GoogleFindMyCoordinator, _as_ha_attributes

_LOGGER = logging.getLogger(__name__)

# ----------------------------- Entity Descriptions -----------------------------

LAST_SEEN_DESCRIPTION = SensorEntityDescription(
    key="last_seen",
    translation_key="last_seen",
    icon="mdi:clock-outline",
    device_class=SensorDeviceClass.TIMESTAMP,
)

# NOTE:
# - Translation keys are aligned with en.json (entity.sensor.*), keeping the set in sync.
# - `skipped_duplicates` is intentionally absent (removed upstream).
STATS_DESCRIPTIONS: dict[str, SensorEntityDescription] = {
    "background_updates": SensorEntityDescription(
        key="background_updates",
        translation_key="stat_background_updates",
        icon="mdi:cloud-download",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "polled_updates": SensorEntityDescription(
        key="polled_updates",
        translation_key="stat_polled_updates",
        icon="mdi:download-network",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "crowd_sourced_updates": SensorEntityDescription(
        key="crowd_sourced_updates",
        translation_key="stat_crowd_sourced_updates",
        icon="mdi:account-group",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "history_fallback_used": SensorEntityDescription(
        key="history_fallback_used",
        translation_key="stat_history_fallback_used",
        icon="mdi:history",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "timeouts": SensorEntityDescription(
        key="timeouts",
        translation_key="stat_timeouts",
        icon="mdi:timer-off",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "invalid_coords": SensorEntityDescription(
        key="invalid_coords",
        translation_key="stat_invalid_coords",
        icon="mdi:map-marker-alert",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "low_quality_dropped": SensorEntityDescription(
        key="low_quality_dropped",
        translation_key="stat_low_quality_dropped",
        icon="mdi:target-off",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "non_significant_dropped": SensorEntityDescription(
        key="non_significant_dropped",
        translation_key="stat_non_significant_dropped",
        icon="mdi:filter-variant-remove",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
}


def _maybe_update_device_registry_name(
    hass: HomeAssistant, entity_id: str, new_name: str
) -> None:
    """Write the real Google device label into the device registry once known.

    Never touch if the user renamed the device (name_by_user is set). Defensive behavior:
    - Only update if a valid device is attached to the entity and name is non-empty.
    - Avoid churn by skipping when the current registry name already matches.
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
    """Set up Google Find My Device sensor entities.

    Behavior:
    - Create per-device last_seen sensors for devices in the current snapshot.
    - Optionally create diagnostic stat sensors when enabled via options.
    - Dynamically add per-device sensors when new devices appear.
    """
    runtime = getattr(entry, "runtime_data", None)
    coordinator: GoogleFindMyCoordinator | None = None
    if isinstance(runtime, GoogleFindMyCoordinator):
        coordinator = runtime
    else:
        runtime_bucket = hass.data.get(DOMAIN, {}).get("entries", {})
        runtime_entry = runtime_bucket.get(entry.entry_id)
        coordinator = getattr(runtime_entry, "coordinator", None)

    if not isinstance(coordinator, GoogleFindMyCoordinator):
        raise HomeAssistantError("googlefindmy coordinator not ready")

    entities: list[SensorEntity] = []
    known_ids: set[str] = set()

    # Options-first toggle for diagnostic counters (single source of truth)
    try:
        from . import _opt  # type: ignore

        enable_stats = _opt(
            entry, OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES
        )
    except Exception:
        enable_stats = entry.options.get(
            OPT_ENABLE_STATS_ENTITIES,
            entry.data.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES),
        )

    if enable_stats:
        created_stats: list[str] = []
        for stat_key, desc in STATS_DESCRIPTIONS.items():
            # Only create sensors for counters that actually exist in the coordinator
            if hasattr(coordinator, "stats") and stat_key in coordinator.stats:
                entities.append(GoogleFindMyStatsSensor(coordinator, stat_key, desc))
                created_stats.append(stat_key)
        if created_stats:
            _LOGGER.debug("Stats sensors created: %s", ", ".join(created_stats))
        else:
            _LOGGER.debug(
                "Stats option enabled but no known counters were present in coordinator.stats"
            )

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
            for device in getattr(coordinator, "data", None) or []:
                dev_id = device.get("id")
                dev_name = device.get("name")
                if not dev_id or not dev_name or dev_id in known_ids:
                    continue
                new_entities.append(GoogleFindMyLastSeenSensor(coordinator, device))
                known_ids.add(dev_id)

            if new_entities:
                _LOGGER.info(
                    "Discovered %d new devices; adding last_seen sensors",
                    len(new_entities),
                )
                async_add_entities(new_entities, True)
        except (AttributeError, TypeError) as err:
            _LOGGER.debug("Dynamic sensor add failed: %s", err)

    unsub = coordinator.async_add_listener(_add_new_sensors_on_update)
    entry.async_on_unload(unsub)


# ------------------------------- Stats Sensor ---------------------------------


class GoogleFindMyStatsSensor(CoordinatorEntity, SensorEntity):
    """Diagnostic counters for the integration (entry-scoped).

    Naming policy (HA Quality Scale – Platinum):
    - Do **not** set a hard-coded `_attr_name`. We rely on `entity_description.translation_key`
      and `_attr_has_entity_name = True` so HA composes the visible name as
      "<device name> <translated entity name>". This ensures locale-correct UI strings.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True  # allow translated entity name to be used

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        stat_key: str,
        description: SensorEntityDescription,
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
        entry_id = getattr(
            getattr(coordinator, "config_entry", None), "entry_id", "default"
        )
        # Entry-scoped unique_id avoids collisions in multi-account setups.
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{stat_key}"
        # Plain unit label; TOTAL_INCREASING counters represent event counts.
        self._attr_native_unit_of_measurement = "updates"

    @property
    def native_value(self) -> int | None:
        """Return the current counter value."""
        stats = getattr(self.coordinator, "stats", None)
        if stats is None:
            return None
        return stats.get(self._stat_key, 0)

    @property
    def available(self) -> bool:
        """Stats sensors stay available even when polling fails."""

        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Propagate coordinator updates to Home Assistant state."""

        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Expose a single integration service device for diagnostic sensors.

        All counters live on the per-entry SERVICE device to keep the UI tidy.
        """
        entry_id = getattr(
            getattr(self.coordinator, "config_entry", None), "entry_id", "default"
        )
        ident = service_device_identifier(entry_id)
        return DeviceInfo(
            identifiers={ident},
            name=SERVICE_DEVICE_NAME,
            manufacturer=SERVICE_DEVICE_MANUFACTURER,
            model=SERVICE_DEVICE_MODEL,
            configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
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
    # Entities should be enabled by default on fresh installs
    _attr_entity_registry_enabled_default = True
    entity_description = LAST_SEEN_DESCRIPTION

    def __init__(
        self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._device = device
        self._device_id: str | None = device.get("id")
        safe_id = self._device_id if self._device_id is not None else "unknown"
        entry_id = getattr(
            getattr(coordinator, "config_entry", None), "entry_id", "default"
        )
        # Namespace unique_id by entry for safety (keeps multi-entry setups clean).
        self._attr_unique_id = f"{DOMAIN}_{entry_id}_{safe_id}_last_seen"
        self._attr_native_value: datetime | None = None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra state attributes for diagnostics/UX (sanitized & stable).

        Delegates to the coordinator helper `_as_ha_attributes`, which:
        - Adds a normalized UTC timestamp mirror (`last_seen_utc`).
        - Uses `accuracy_m` (float meters) rather than `gps_accuracy` for stability.
        - Includes source labeling (`source_label`/`source_rank`) for transparency.
        """
        dev_id = self._device_id
        if not dev_id:
            return None
        row = self.coordinator.get_device_location_data(dev_id)
        return _as_ha_attributes(row) if row else None

    @property
    def available(self) -> bool:
        """Mirror device presence in availability (TTL-smoothed by coordinator).

        Fallback: If coordinator presence is unavailable, consider the sensor available
        when we have at least a restored/known last_seen value.
        """
        dev_id = self._device_id
        if not dev_id:
            return False
        is_fcm_connected = getattr(self.coordinator, "is_fcm_connected", None)
        if is_fcm_connected is False:
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
        """Update native timestamp and keep the device label in sync.

        - Synchronize the raw Google label into the Device Registry unless user-renamed.
        - Keep the existing last_seen when no fresh value is available to avoid churn.
        """
        # 1) Keep the raw device name synchronized with the coordinator snapshot.
        try:
            my_id = self._device_id or ""
            for dev in getattr(self.coordinator, "data", None) or []:
                if dev.get("id") == my_id:
                    new_name = dev.get("name")
                    if new_name and new_name != self._device.get("name"):
                        self._device["name"] = new_name
                        _maybe_update_device_registry_name(
                            self.hass, self.entity_id, new_name
                        )
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
                "Invalid last_seen for %s: %s",
                self._device.get("name", self._device_id),
                e,
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
        """Restore last_seen from HA's persistent store and seed coordinator cache.

        We only seed `last_seen` (epoch seconds) — coordinates are handled by the tracker.
        """
        await super().async_added_to_hass()

        # Use RestoreSensor API to get the last native value (may be datetime/str/number)
        try:
            data = await self.async_get_last_sensor_data()
            value = getattr(data, "native_value", None) if data else None
        except (RuntimeError, AttributeError) as e:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to restore sensor state for %s: %s", self.entity_id, e
            )
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
                "Could not parse restored value '%s' for %s: %s",
                value,
                self.entity_id,
                ex,
            )
            ts = None

        if ts is None or not self._device_id:
            return

        # Seed coordinator cache using its public API (no private access).
        try:
            self.coordinator.seed_device_last_seen(self._device_id, ts)
        except (AttributeError, TypeError) as e:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to seed coordinator cache for %s: %s", self.entity_id, e
            )
            return

        # Set our native value now (no need to wait for next coordinator tick)
        self._attr_native_value = datetime.fromtimestamp(ts, tz=timezone.utc)
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return per-device info linked via the per-entry service device.

        Notes:
            - Only provide `name` when we have a real Google label; otherwise omit it.
            - Include a stable configuration_url pointing to the per-device map.
            - Link to the service device using entry-scoped `service_device_identifier`.
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
            _LOGGER.debug(
                "Could not determine Home Assistant URL, using fallback: %s", e
            )
            base_url = "http://homeassistant.local:8123"

        # Only provide a name if we have a real device label (no bootstrap placeholder)
        raw_name = (self._device.get("name") or "").strip()
        use_name = (
            raw_name if raw_name and raw_name != "Google Find My Device" else None
        )

        # Link this end device to the single per-entry service device
        entry_id = getattr(
            getattr(self.coordinator, "config_entry", None), "entry_id", None
        )
        via = service_device_identifier(entry_id) if entry_id else None
        dev_id = self._device["id"]
        entry_scoped_identifier = f"{entry_id}:{dev_id}" if entry_id else dev_id

        return DeviceInfo(
            identifiers={(DOMAIN, entry_scoped_identifier)},
            name=use_name,  # may be None; that's OK when identifiers are provided
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}",
            serial_number=self._device["id"],
            via_device=via,
        )

    @staticmethod
    def _build_map_path(device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host)."""
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a hardened token for map authentication (entry-scoped + weekly/static).

        Token formula (kept consistent with buttons, tracker and map_view):
            md5( f"{ha_uuid}:{entry_id}:{week|static}" )[:16]
        """
        config_entry = getattr(self.coordinator, "config_entry", None)

        # Prefer the central _opt helper; fall back to direct options/data reads for safety.
        try:
            from . import _opt  # type: ignore

            token_expiration_enabled = _opt(
                config_entry,
                OPT_MAP_VIEW_TOKEN_EXPIRATION,
                DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
            )
        except Exception:
            if config_entry:
                token_expiration_enabled = config_entry.options.get(
                    OPT_MAP_VIEW_TOKEN_EXPIRATION,
                    config_entry.data.get(
                        OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
                    ),
                )
            else:
                token_expiration_enabled = DEFAULT_MAP_VIEW_TOKEN_EXPIRATION

        entry_id = getattr(config_entry, "entry_id", "") if config_entry else ""
        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))

        if token_expiration_enabled:
            week = str(int(time.time() // 604800))  # weekly-rolling
            secret = f"{ha_uuid}:{entry_id}:{week}"
        else:
            secret = f"{ha_uuid}:{entry_id}:static"

        return hashlib.md5(secret.encode()).hexdigest()[:16]
