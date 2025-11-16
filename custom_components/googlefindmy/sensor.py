# custom_components/googlefindmy/sensor.py
"""Sensor entities for Google Find My Device integration.

Exposes:
- Per-device `last_seen` timestamp sensors (restore-friendly).
- Optional integration diagnostic counters (stats), toggled via options.

Best practices:
- Device names are synced from the coordinator once known; user-assigned names are never overwritten.
- No placeholder names are written to the device registry on cold boot.
- End devices rely on the coordinator's tracker subentry metadata; do not set
  manual `via_device` links.
- Sensors default to **enabled** so a fresh installation is immediately functional.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_ENABLE_STATS_ENTITIES,
    DOMAIN,
    OPT_ENABLE_STATS_ENTITIES,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from . import EntityRecoveryManager
from .coordinator import GoogleFindMyCoordinator, _as_ha_attributes
from .entity import (
    GoogleFindMyDeviceEntity,
    GoogleFindMyEntity,
    ensure_config_subentry_id,
    resolve_coordinator,
    schedule_add_entities,
)
from .ha_typing import RestoreSensor, SensorEntity, callback

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
        icon="mdi:target",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    "non_significant_dropped": SensorEntityDescription(
        key="non_significant_dropped",
        translation_key="stat_non_significant_dropped",
        icon="mdi:filter-variant-remove",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    config_subentry_id: str | None = None,
) -> None:
    """Set up Google Find My Device sensor entities.

    Behavior:
    - Create per-device last_seen sensors for devices in the current snapshot.
    - Optionally create diagnostic stat sensors when enabled via options.
    - Dynamically add per-device sensors when new devices appear.
    """
    coordinator = resolve_coordinator(entry)

    service_meta = coordinator.get_subentry_metadata(feature="binary_sensor")
    service_subentry_key = (
        service_meta.key if service_meta is not None else SERVICE_SUBENTRY_KEY
    )
    service_meta_config_id = (
        getattr(service_meta, "config_subentry_id", None)
        if service_meta is not None
        else None
    )
    service_subentry_identifier = coordinator.stable_subentry_identifier(
        key=service_subentry_key
    )
    tracker_meta = coordinator.get_subentry_metadata(feature="sensor")
    tracker_subentry_key = (
        tracker_meta.key if tracker_meta is not None else TRACKER_SUBENTRY_KEY
    )
    tracker_meta_config_id = (
        getattr(tracker_meta, "config_subentry_id", None)
        if tracker_meta is not None
        else None
    )
    tracker_subentry_identifier = coordinator.stable_subentry_identifier(
        key=tracker_subentry_key
    )
    service_config_subentry_id = service_meta_config_id
    tracker_config_subentry_id = tracker_meta_config_id

    _LOGGER.debug(
        "Sensor setup: service_key=%s (id=%s), tracker_key=%s (id=%s), config_subentry_id=%s",
        service_subentry_key,
        service_config_subentry_id,
        tracker_subentry_key,
        tracker_config_subentry_id,
        config_subentry_id,
    )

    def _matches(identifier: str | None, fallback: str) -> bool:
        if not isinstance(config_subentry_id, str) or not config_subentry_id:
            return False
        if identifier and config_subentry_id == identifier:
            return True
        return config_subentry_id == fallback

    matches_service = False
    matches_tracker = False
    should_init_service = True
    should_init_tracker = True
    if isinstance(config_subentry_id, str) and config_subentry_id:
        matches_service = _matches(
            service_config_subentry_id, service_subentry_identifier
        )
        matches_tracker = _matches(
            tracker_config_subentry_id, tracker_subentry_identifier
        )
        if matches_service and not matches_tracker:
            should_init_tracker = False
        elif matches_tracker and not matches_service:
            should_init_service = False
        elif not matches_service and not matches_tracker:
            _LOGGER.debug(
                "Sensor setup received unknown config_subentry_id '%s'; defaulting to all scopes",
                config_subentry_id,
            )

    if not service_config_subentry_id and matches_service:
        service_config_subentry_id = config_subentry_id
    if not tracker_config_subentry_id and matches_tracker:
        tracker_config_subentry_id = config_subentry_id

    service_config_subentry_id = ensure_config_subentry_id(
        entry,
        "sensor_service",
        service_config_subentry_id,
    )
    tracker_config_subentry_id = ensure_config_subentry_id(
        entry,
        "sensor_tracker",
        tracker_config_subentry_id,
    )

    if service_config_subentry_id is None:
        should_init_service = False
    if tracker_config_subentry_id is None:
        should_init_tracker = False

    if not should_init_service:
        _LOGGER.debug(
            "Sensor setup: service metrics paused because config_subentry_id is unavailable",
        )
    if not should_init_tracker:
        _LOGGER.debug(
            "Sensor setup: tracker metrics paused because config_subentry_id is unavailable",
        )

    service_entities: list[SensorEntity] = []
    tracker_entities: list[SensorEntity] = []
    known_ids: set[str] = set()

    def _schedule_service_entities(
        new_entities: Iterable[SensorEntity],
        update_before_add: bool = True,
    ) -> None:
        schedule_add_entities(
            coordinator.hass,
            async_add_entities,
            entities=new_entities,
            update_before_add=update_before_add,
            config_subentry_id=service_config_subentry_id,
            log_owner="Sensor setup (service)",
            logger=_LOGGER,
        )

    def _schedule_tracker_entities(
        new_entities: Iterable[SensorEntity],
        update_before_add: bool = True,
    ) -> None:
        schedule_add_entities(
            coordinator.hass,
            async_add_entities,
            entities=new_entities,
            update_before_add=update_before_add,
            config_subentry_id=tracker_config_subentry_id,
            log_owner="Sensor setup (tracker)",
            logger=_LOGGER,
        )

    def _schedule_recovered_entities(
        new_entities: Iterable[SensorEntity],
        update_before_add: bool = True,
    ) -> None:
        service_batch: list[SensorEntity] = []
        tracker_batch: list[SensorEntity] = []
        for entity in new_entities:
            if isinstance(entity, GoogleFindMyStatsSensor):
                service_batch.append(entity)
            else:
                tracker_batch.append(entity)

        if service_batch:
            _schedule_service_entities(service_batch, update_before_add)
        if tracker_batch:
            _schedule_tracker_entities(tracker_batch, update_before_add)

    enable_stats_raw = entry.options.get(
        OPT_ENABLE_STATS_ENTITIES,
        entry.data.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES),
    )
    enable_stats = bool(enable_stats_raw)

    created_stats: list[str] = []
    if should_init_service and enable_stats:
        for stat_key, desc in STATS_DESCRIPTIONS.items():
            if hasattr(coordinator, "stats") and stat_key in coordinator.stats:
                service_entities.append(
                    GoogleFindMyStatsSensor(
                        coordinator,
                        stat_key,
                        desc,
                        subentry_key=service_subentry_key,
                        subentry_identifier=service_subentry_identifier,
                    )
                )
                created_stats.append(stat_key)
        if created_stats:
            _LOGGER.debug("Stats sensors created: %s", ", ".join(created_stats))
        elif enable_stats:
            _LOGGER.debug(
                "Stats option enabled but no known counters were present in coordinator.stats"
            )

    if should_init_tracker:
        snapshot = coordinator.get_subentry_snapshot(tracker_subentry_key)
        for device in snapshot:
            dev_id = device.get("id")
            dev_name = device.get("name")
            if not dev_id or not dev_name:
                _LOGGER.debug("Skipping device without id/name: %s", device)
                continue
            if dev_id in known_ids:
                _LOGGER.debug("Ignoring duplicate device id %s in startup snapshot", dev_id)
                continue
            tracker_entities.append(
                GoogleFindMyLastSeenSensor(
                    coordinator,
                    device,
                    subentry_key=tracker_subentry_key,
                    subentry_identifier=tracker_subentry_identifier,
                )
            )
            known_ids.add(dev_id)

    if service_entities:
        _schedule_service_entities(service_entities, True)
    if tracker_entities:
        _schedule_tracker_entities(tracker_entities, True)

    @callback
    def _add_new_sensors_on_update() -> None:
        if not should_init_tracker:
            return
        try:
            new_entities: list[SensorEntity] = []
            for device in coordinator.get_subentry_snapshot(tracker_subentry_key):
                dev_id = device.get("id")
                dev_name = device.get("name")
                if not dev_id or not dev_name or dev_id in known_ids:
                    continue
                new_entities.append(
                    GoogleFindMyLastSeenSensor(
                        coordinator,
                        device,
                        subentry_key=tracker_subentry_key,
                        subentry_identifier=tracker_subentry_identifier,
                    )
                )
                known_ids.add(dev_id)

            if new_entities:
                _LOGGER.info(
                    "Discovered %d new devices; adding last_seen sensors",
                    len(new_entities),
                )
                _schedule_tracker_entities(new_entities, True)
        except (AttributeError, TypeError) as err:
            _LOGGER.debug("Dynamic sensor add failed: %s", err)

    if should_init_tracker:
        unsub = coordinator.async_add_listener(_add_new_sensors_on_update)
        entry.async_on_unload(unsub)

    runtime_data = getattr(entry, "runtime_data", None)
    recovery_manager = getattr(runtime_data, "entity_recovery_manager", None)

    if isinstance(recovery_manager, EntityRecoveryManager) and (
        should_init_tracker or (should_init_service and created_stats)
    ):
        entry_id = getattr(entry, "entry_id", None)

        def _is_visible(device_id: str) -> bool:
            try:
                return bool(
                    coordinator.is_device_visible_in_subentry(
                        tracker_subentry_key, device_id
                    )
                )
            except Exception:  # pragma: no cover - defensive best effort
                return True

        def _expected_unique_ids() -> set[str]:
            if not isinstance(entry_id, str) or not entry_id:
                return set()
            expected: set[str] = set()
            if (
                should_init_service
                and created_stats
                and isinstance(service_subentry_identifier, str)
                and service_subentry_identifier
            ):
                for stat_key in created_stats:
                    expected.add(
                        f"{DOMAIN}_{entry_id}_{service_subentry_identifier}_{stat_key}"
                    )
            if should_init_tracker and isinstance(
                tracker_subentry_identifier, str
            ) and tracker_subentry_identifier:
                for device in coordinator.get_subentry_snapshot(tracker_subentry_key):
                    dev_id = device.get("id")
                    dev_name = device.get("name")
                    if not isinstance(dev_id, str) or not dev_id or not isinstance(
                        dev_name, str
                    ) or not dev_name:
                        continue
                    if not _is_visible(dev_id):
                        continue
                    expected.add(
                        f"{DOMAIN}_{entry_id}_{tracker_subentry_identifier}_{dev_id}_last_seen"
                    )
            return expected

        def _build_entities(missing: set[str]) -> list[SensorEntity]:
            if not missing:
                return []
            built: list[SensorEntity] = []
            if not isinstance(entry_id, str) or not entry_id:
                return built
            if (
                should_init_service
                and created_stats
                and isinstance(service_subentry_identifier, str)
                and service_subentry_identifier
            ):
                for stat_key in created_stats:
                    unique_id = (
                        f"{DOMAIN}_{entry_id}_{service_subentry_identifier}_{stat_key}"
                    )
                    if unique_id not in missing:
                        continue
                    description = STATS_DESCRIPTIONS.get(stat_key)
                    if description is None:
                        continue
                    built.append(
                        GoogleFindMyStatsSensor(
                            coordinator,
                            stat_key,
                            description,
                            subentry_key=service_subentry_key,
                            subentry_identifier=service_subentry_identifier,
                        )
                    )
            if should_init_tracker and isinstance(
                tracker_subentry_identifier, str
            ) and tracker_subentry_identifier:
                for device in coordinator.get_subentry_snapshot(tracker_subentry_key):
                    dev_id = device.get("id")
                    dev_name = device.get("name")
                    if not isinstance(dev_id, str) or not dev_id or not isinstance(
                        dev_name, str
                    ) or not dev_name:
                        continue
                    if not _is_visible(dev_id):
                        continue
                    unique_id = (
                        f"{DOMAIN}_{entry_id}_{tracker_subentry_identifier}_{dev_id}_last_seen"
                    )
                    if unique_id not in missing:
                        continue
                    built.append(
                        GoogleFindMyLastSeenSensor(
                            coordinator,
                            device,
                            subentry_key=tracker_subentry_key,
                            subentry_identifier=tracker_subentry_identifier,
                        )
                    )
            return built

        recovery_manager.register_sensor_platform(
            expected_unique_ids=_expected_unique_ids,
            entity_factory=_build_entities,
            add_entities=_schedule_recovered_entities,
        )


# ------------------------------- Stats Sensor ---------------------------------


class GoogleFindMyStatsSensor(GoogleFindMyEntity, SensorEntity):
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
        *,
        subentry_key: str,
        subentry_identifier: str,
    ) -> None:
        """Initialize the stats sensor.

        Args:
            coordinator: The integration's data coordinator.
            stat_key: Name of the counter in coordinator.stats.
            description: Home Assistant entity description (icon, translation_key, etc.).
        """
        super().__init__(
            coordinator,
            subentry_key=subentry_key,
            subentry_identifier=subentry_identifier,
        )
        self._stat_key = stat_key
        self.entity_description = description
        entry_id = self.entry_id or "default"
        # Entry-scoped unique_id avoids collisions in multi-account setups.
        self._attr_unique_id = self.build_unique_id(
            DOMAIN,
            entry_id,
            subentry_identifier,
            stat_key,
            separator="_",
        )
        # Plain unit label; TOTAL_INCREASING counters represent event counts.
        self._attr_native_unit_of_measurement = "updates"

    @property
    def native_value(self) -> int | None:
        """Return the current counter value."""
        stats = getattr(self.coordinator, "stats", None)
        if stats is None:
            return None
        raw = stats.get(self._stat_key)
        if isinstance(raw, bool):
            return int(raw)
        if isinstance(raw, (int, float)):
            return int(raw)
        return None

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
        return self.service_device_info(include_subentry_identifier=True)


# ----------------------------- Per-Device Last Seen ---------------------------


class GoogleFindMyLastSeenSensor(GoogleFindMyDeviceEntity, RestoreSensor):
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
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
        *,
        subentry_key: str,
        subentry_identifier: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            device,
            subentry_key=subentry_key,
            subentry_identifier=subentry_identifier,
            fallback_label=device.get("name"),
        )
        self._device_id: str | None = device.get("id")
        safe_id = self._device_id if self._device_id is not None else "unknown"
        entry_id = self.entry_id or "default"
        # Namespace unique_id by entry for safety (keeps multi-entry setups clean).
        self._attr_unique_id = self.build_unique_id(
            DOMAIN,
            entry_id,
            subentry_identifier,
            f"{safe_id}_last_seen",
            separator="_",
        )
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
        row = self.coordinator.get_device_location_data_for_subentry(
            self.subentry_key, dev_id
        )
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
        if not self.coordinator_has_device():
            return False

        present: bool | None = None
        try:
            if hasattr(self.coordinator, "is_device_present"):
                raw = self.coordinator.is_device_present(dev_id)
                if isinstance(raw, bool):
                    present = raw
                else:
                    present = bool(raw)
        except Exception:
            # Be tolerant if a different coordinator build is used.
            present = None

        if present is True:
            return True
        if present is False:
            # Presence expired; fall back to restored values if available.
            return self._attr_native_value is not None

        # Unknown presence: consider available if we have any known last_seen value
        return self._attr_native_value is not None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Update native timestamp and keep the device label in sync.

        - Synchronize the raw Google label into the Device Registry unless user-renamed.
        - Keep the existing last_seen when no fresh value is available to avoid churn.
        """
        # 1) Keep the raw device name synchronized with the coordinator snapshot.
        if not self.coordinator_has_device():
            self._attr_native_value = None
            self.async_write_ha_state()
            return

        self.refresh_device_label_from_coordinator(log_prefix="LastSeen")
        current_name = self._device.get("name")
        if isinstance(current_name, str):
            self.maybe_update_device_registry_name(current_name)

        # 2) Update last_seen when a valid value is available; otherwise keep the previous value.
        previous = self._attr_native_value
        new_dt: datetime | None = None
        try:
            value = (
                self.coordinator.get_device_last_seen_for_subentry(
                    self._subentry_key, self._device_id
                )
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
        """Expose DeviceInfo using the shared entity helper."""

        return super().device_info
