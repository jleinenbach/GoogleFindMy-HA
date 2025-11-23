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
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, NamedTuple

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EntityRecoveryManager
from .const import (
    DEFAULT_ENABLE_STATS_ENTITIES,
    DOMAIN,
    OPT_ENABLE_STATS_ENTITIES,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from .coordinator import GoogleFindMyCoordinator, _as_ha_attributes
from .entity import (
    GoogleFindMyDeviceEntity,
    GoogleFindMyEntity,
    ensure_config_subentry_id,
    ensure_dispatcher_dependencies,
    resolve_coordinator,
    schedule_add_entities,
)
from .ha_typing import RestoreSensor, SensorEntity, callback

_LOGGER = logging.getLogger(__name__)


class _Scope(NamedTuple):
    """Resolved subentry scope for entity creation."""

    subentry_key: str
    config_subentry_id: str | None
    identifier: str


def _subentry_type(subentry: Any | None) -> str | None:
    """Return the declared subentry type for dispatcher filtering."""

    if subentry is None or isinstance(subentry, str):
        return None

    declared_type = getattr(subentry, "subentry_type", None)
    if isinstance(declared_type, str):
        return declared_type

    data = getattr(subentry, "data", None)
    if isinstance(data, Mapping):
        fallback_type = data.get("subentry_type") or data.get("type")
        if isinstance(fallback_type, str):
            return fallback_type
    return None

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
    *,
    config_subentry_id: str | None = None,
) -> None:
    """Set up Google Find My Device sensor entities.

    Behavior:
    - Create per-device last_seen sensors for devices in the current snapshot.
    - Optionally create diagnostic stat sensors when enabled via options.
    - Dynamically add per-device sensors when new devices appear.
    """
    coordinator = resolve_coordinator(entry)
    ensure_dispatcher_dependencies(hass)
    if getattr(coordinator, "config_entry", None) is None:
        coordinator.config_entry = entry

    def _collect_scopes(
        *,
        feature: str,
        default_key: str,
        hint_subentry_id: str | None = None,
        forwarded_config_id: str | None = None,
    ) -> list[_Scope]:
        scopes: dict[str, _Scope] = {}

        subentry_metas = getattr(coordinator, "_subentry_metadata", None)
        if isinstance(subentry_metas, Mapping):
            for key, meta in subentry_metas.items():
                meta_features = getattr(meta, "features", ())
                if feature not in meta_features:
                    continue

                stable_identifier = getattr(meta, "stable_identifier", None)
                identifier = (
                    stable_identifier() if callable(stable_identifier) else None
                    or getattr(meta, "config_subentry_id", None)
                    or coordinator.stable_subentry_identifier(key=key)
                )
                scopes[identifier] = _Scope(
                    key,
                    getattr(meta, "config_subentry_id", None),
                    identifier,
                )

        subentries = getattr(entry, "subentries", None)
        if isinstance(subentries, Mapping):
            for subentry in subentries.values():
                data = getattr(subentry, "data", {})
                group_key = default_key
                subentry_features: Iterable[Any] = ()
                if isinstance(data, Mapping):
                    group_key = data.get("group_key", group_key)
                    subentry_features = data.get("features", ())

                if isinstance(group_key, str) and group_key:
                    if default_key == TRACKER_SUBENTRY_KEY and group_key in (
                        SERVICE_SUBENTRY_KEY,
                        "service",
                    ):
                        continue
                    if default_key == SERVICE_SUBENTRY_KEY and group_key not in (
                        SERVICE_SUBENTRY_KEY,
                        "service",
                    ):
                        continue

                if feature not in subentry_features:
                    continue

                config_id = (
                    getattr(subentry, "subentry_id", None)
                    or getattr(subentry, "entry_id", None)
                )
                identifier = (
                    config_id
                    or coordinator.stable_subentry_identifier(key=group_key)
                    or default_key
                )
                scopes.setdefault(
                    identifier,
                    _Scope(group_key or default_key, config_id, identifier),
                )

        if scopes:
            return list(scopes.values())

        fallback_identifier = coordinator.stable_subentry_identifier(
            key=default_key, feature=feature
        )
        fallback_config_id = forwarded_config_id or hint_subentry_id

        return [
            _Scope(
                default_key,
                fallback_config_id,
                fallback_identifier or fallback_config_id or default_key,
            )
        ]

    enable_stats_raw = entry.options.get(
        OPT_ENABLE_STATS_ENTITIES,
        entry.data.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES),
    )
    enable_stats = bool(enable_stats_raw)

    added_unique_ids: set[str] = set()
    created_stats: dict[str, list[str]] = {}
    service_scopes: dict[str, _Scope] = {}
    tracker_scopes: list[_Scope] = []
    processed_tracker_identifiers: set[str] = set()
    primary_tracker_scope: _Scope | None = None
    tracker_scheduler: Callable[[Iterable[SensorEntity], bool], None] | None = None

    def _scope_matches_forwarded(scope: _Scope, forwarded_config_id: str | None) -> bool:
        if forwarded_config_id is None:
            return True
        return forwarded_config_id in (
            scope.config_subentry_id,
            scope.identifier,
            scope.subentry_key,
        )

    def _add_service_scope(scope: _Scope, forwarded_config_id: str | None) -> None:
        sanitized_config_id = ensure_config_subentry_id(
            entry,
            "sensor_service",
            scope.config_subentry_id
            or forwarded_config_id
            or scope.identifier,
        )
        if sanitized_config_id is None:
            sanitized_config_id = (
                scope.config_subentry_id
                or forwarded_config_id
                or scope.identifier
                or SERVICE_SUBENTRY_KEY
            )

        identifier = scope.identifier or sanitized_config_id or scope.subentry_key
        service_scopes[identifier] = _Scope(scope.subentry_key, sanitized_config_id, identifier)

        def _schedule_service_entities(
            new_entities: Iterable[SensorEntity],
            update_before_add: bool = True,
        ) -> None:
            schedule_add_entities(
                coordinator.hass,
                async_add_entities,
                entities=new_entities,
                update_before_add=update_before_add,
                config_subentry_id=sanitized_config_id,
                log_owner="Sensor setup (service)",
                logger=_LOGGER,
            )

        if not enable_stats:
            _schedule_service_entities([], True)
            return

        stats_entities: list[SensorEntity] = []
        created_for_scope: list[str] = []
        if hasattr(coordinator, "stats"):
            for stat_key, desc in STATS_DESCRIPTIONS.items():
                if stat_key not in coordinator.stats:
                    continue
                entity = GoogleFindMyStatsSensor(
                    coordinator,
                    stat_key,
                    desc,
                    subentry_key=scope.subentry_key,
                    subentry_identifier=identifier,
                )
                unique_id = getattr(entity, "unique_id", None)
                if isinstance(unique_id, str) and unique_id in added_unique_ids:
                    continue
                if isinstance(unique_id, str):
                    added_unique_ids.add(unique_id)
                stats_entities.append(entity)
                created_for_scope.append(stat_key)

        if stats_entities:
            _LOGGER.debug(
                "Sensor setup: service_key=%s, config_subentry_id=%s (stats=%d)",
                scope.subentry_key,
                sanitized_config_id,
                len(stats_entities),
            )
            _schedule_service_entities(stats_entities, True)
            created_stats[identifier] = created_for_scope

    def _add_tracker_scope(scope: _Scope, forwarded_config_id: str | None) -> None:
        nonlocal primary_tracker_scope, tracker_scheduler

        candidate_subentry_id = scope.config_subentry_id
        if candidate_subentry_id is None and forwarded_config_id in (
            scope.identifier,
            scope.subentry_key,
        ):
            candidate_subentry_id = forwarded_config_id
        candidate_subentry_id = candidate_subentry_id or scope.identifier

        sanitized_config_id = ensure_config_subentry_id(
            entry,
            "sensor_tracker",
            candidate_subentry_id,
        )
        if sanitized_config_id is None:
            _LOGGER.debug(
                "Sensor setup: awaiting config_subentry_id for tracker key '%s'", scope.subentry_key
            )
            return

        tracker_identifier = scope.identifier or sanitized_config_id or scope.subentry_key
        if tracker_identifier in processed_tracker_identifiers:
            return
        processed_tracker_identifiers.add(tracker_identifier)
        tracker_scope = _Scope(scope.subentry_key, sanitized_config_id, tracker_identifier)
        tracker_scopes.append(tracker_scope)

        if primary_tracker_scope is None:
            primary_tracker_scope = tracker_scope

        known_ids: set[str] = set()
        entities_added = False

        def _schedule_tracker_entities(
            new_entities: Iterable[SensorEntity],
            update_before_add: bool = True,
        ) -> None:
            nonlocal entities_added
            entity_list = list(new_entities)
            entities_added |= bool(entity_list)

            schedule_add_entities(
                coordinator.hass,
                async_add_entities,
                entities=entity_list,
                update_before_add=update_before_add,
                config_subentry_id=sanitized_config_id,
                log_owner="Sensor setup (tracker)",
                logger=_LOGGER,
            )

        if tracker_scheduler is None:
            tracker_scheduler = _schedule_tracker_entities

        def _build_entities() -> list[SensorEntity]:
            entities: list[SensorEntity] = []
            for device in coordinator.get_subentry_snapshot(tracker_scope.subentry_key):
                dev_id = device.get("id") if isinstance(device, Mapping) else None
                dev_name = device.get("name") if isinstance(device, Mapping) else None
                if not dev_id or not dev_name:
                    _LOGGER.debug("Skipping device without id/name: %s", device)
                    continue
                if dev_id in known_ids:
                    continue

                visible = True
                is_visible = getattr(coordinator, "is_device_visible_in_subentry", None)
                if callable(is_visible):
                    try:
                        visible = bool(
                            is_visible(tracker_scope.subentry_key, dev_id)
                        )
                    except Exception:  # pragma: no cover - defensive fallback for stubs
                        visible = True

                if not visible:
                    _LOGGER.debug(
                        "Sensor setup: skipping hidden device id %s for subentry %s",
                        dev_id,
                        tracker_scope.subentry_key,
                    )
                    continue

                entity = GoogleFindMyLastSeenSensor(
                    coordinator,
                    device,
                    subentry_key=tracker_scope.subentry_key,
                    subentry_identifier=tracker_identifier,
                )
                unique_id = getattr(entity, "unique_id", None)
                if isinstance(unique_id, str):
                    if unique_id in added_unique_ids:
                        continue
                    added_unique_ids.add(unique_id)
                known_ids.add(dev_id)
                entities.append(entity)

            return entities

        initial_entities = _build_entities()
        if initial_entities:
            _LOGGER.debug(
                "Sensor setup: tracker_key=%s, config_subentry_id=%s (initial=%d)",
                tracker_scope.subentry_key,
                sanitized_config_id,
                len(initial_entities),
            )
            _schedule_tracker_entities(initial_entities, True)
        else:
            _schedule_tracker_entities([], True)

        @callback
        def _add_new_devices() -> None:
            new_entities = _build_entities()
            if new_entities:
                _LOGGER.debug(
                    "Sensor setup: dynamically adding %d entity(ies) for tracker subentry %s",
                    len(new_entities),
                    tracker_scope.subentry_key,
                )
                _schedule_tracker_entities(new_entities, True)
            elif not entities_added:
                _schedule_tracker_entities([], True)

        unsub = coordinator.async_add_listener(_add_new_devices)
        entry.async_on_unload(unsub)

    seen_subentries: set[str | None] = set()

    @callback
    def async_add_subentry(subentry: Any | None = None) -> None:
        subentry_identifier = None
        if isinstance(subentry, str):
            subentry_identifier = subentry
        else:
            subentry_identifier = getattr(subentry, "subentry_id", None) or getattr(
                subentry, "entry_id", None
            )

        subentry_type = _subentry_type(subentry)
        if subentry_type not in (None, "service", "tracker"):
            _LOGGER.debug(
                "Sensor setup skipped for unrelated subentry '%s' (type '%s')",
                subentry_identifier,
                subentry_type,
            )
            return

        if subentry_identifier in seen_subentries:
            return
        seen_subentries.add(subentry_identifier)

        service_config_id = next(
            (
                getattr(candidate, "subentry_id", None)
                for candidate in getattr(entry, "subentries", {}).values()
                if getattr(candidate, "subentry_type", None) == "service"
                or (
                    isinstance(getattr(candidate, "data", None), Mapping)
                    and candidate.data.get("group_key") in (SERVICE_SUBENTRY_KEY, "service")
                )
            ),
            None,
        )
        service_subentries_exist = service_config_id is not None

        processed_ids: set[str | None] = set()
        if subentry_type == "tracker" and not service_subentries_exist:
            _add_service_scope(
                _Scope(
                    SERVICE_SUBENTRY_KEY,
                    service_config_id or SERVICE_SUBENTRY_KEY,
                    SERVICE_SUBENTRY_KEY,
                ),
                service_config_id or SERVICE_SUBENTRY_KEY,
            )
        service_forward_id = (
            service_config_id
            if (subentry_type == "tracker" and service_subentries_exist)
            else SERVICE_SUBENTRY_KEY
            if subentry_type == "tracker"
            else subentry_identifier
        )

        if not (subentry_type == "tracker" and service_subentries_exist):
            for scope in _collect_scopes(
                feature="sensor",
                default_key=SERVICE_SUBENTRY_KEY,
                hint_subentry_id=service_forward_id,
                forwarded_config_id=service_forward_id,
            ):
                scope_identifier = (
                    scope.config_subentry_id or scope.identifier or scope.subentry_key
                )
                if scope_identifier in processed_ids and subentry_type != "tracker":
                    continue
                if subentry_type != "tracker":
                    processed_ids.add(scope_identifier)
                if _scope_matches_forwarded(scope, subentry_identifier):
                    _add_service_scope(scope, subentry_identifier)

        if subentry_type in (None, "tracker"):
            for scope in _collect_scopes(
                feature="sensor",
                default_key=TRACKER_SUBENTRY_KEY,
                hint_subentry_id=subentry_identifier,
                forwarded_config_id=subentry_identifier,
            ):
                scope_identifier = (
                    scope.config_subentry_id or scope.identifier or scope.subentry_key
                )
                if scope_identifier in processed_ids:
                    continue
                processed_ids.add(scope_identifier)
                if _scope_matches_forwarded(scope, subentry_identifier):
                    _add_tracker_scope(scope, subentry_identifier)

    runtime_data = getattr(entry, "runtime_data", None)
    subentry_manager = getattr(runtime_data, "subentry_manager", None)
    managed_subentries = getattr(subentry_manager, "managed_subentries", None)
    if isinstance(managed_subentries, Mapping):
        if config_subentry_id is not None:
            for managed_subentry in managed_subentries.values():
                async_add_subentry(managed_subentry)
    elif isinstance(getattr(entry, "subentries", None), Mapping):
        for managed_subentry in entry.subentries.values():
            async_add_subentry(managed_subentry)
    else:
        async_add_subentry(config_subentry_id)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_subentry_setup_{entry.entry_id}", async_add_subentry
        )
    )

    recovery_manager = getattr(runtime_data, "entity_recovery_manager", None)

    if isinstance(recovery_manager, EntityRecoveryManager):
        entry_id = getattr(entry, "entry_id", None)
        service_identifier = next(iter(created_stats.keys()), None)
        service_scope = service_scopes.get(service_identifier) if service_identifier else None
        tracker_scope = primary_tracker_scope

        def _recovery_add_entities(
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

            if service_batch and service_scope is not None:
                schedule_add_entities(
                    coordinator.hass,
                    async_add_entities,
                    entities=service_batch,
                    update_before_add=update_before_add,
                    config_subentry_id=service_scope.config_subentry_id,
                    log_owner="Sensor setup (service)",
                    logger=_LOGGER,
                )
            if tracker_batch and tracker_scheduler is not None:
                tracker_scheduler(tracker_batch, update_before_add)

        def _is_visible_device(device_id: str) -> bool:
            if tracker_scope is None:
                return True
            is_visible = getattr(coordinator, "is_device_visible_in_subentry", None)
            if callable(is_visible):
                try:
                    return bool(is_visible(tracker_scope.subentry_key, device_id))
                except Exception:  # pragma: no cover - defensive fallback for stubs
                    return True
            return True

        def _expected_unique_ids() -> set[str]:
            if not isinstance(entry_id, str) or not entry_id:
                return set()
            expected: set[str] = set()
            if (
                service_identifier
                and isinstance(service_identifier, str)
                and service_identifier
            ):
                for stat_key in created_stats.get(service_identifier, []):
                    expected.add(
                        f"{DOMAIN}_{entry_id}_{service_identifier}_{stat_key}"
                    )
            if tracker_scope and isinstance(tracker_scope.identifier, str):
                for device in coordinator.get_subentry_snapshot(tracker_scope.subentry_key):
                    dev_id = device.get("id")
                    dev_name = device.get("name")
                    if not isinstance(dev_id, str) or not dev_id or not isinstance(
                        dev_name, str
                    ) or not dev_name:
                        continue
                    if not _is_visible_device(dev_id):
                        continue
                    expected.add(
                        f"{DOMAIN}_{entry_id}_{tracker_scope.identifier}_{dev_id}_last_seen"
                    )
            return expected

        def _build_entities(missing: set[str]) -> list[SensorEntity]:
            if not missing:
                return []
            built: list[SensorEntity] = []
            if not isinstance(entry_id, str) or not entry_id:
                return built
            if (
                service_scope
                and isinstance(service_identifier, str)
                and service_identifier
            ):
                for stat_key in created_stats.get(service_identifier, []):
                    unique_id = f"{DOMAIN}_{entry_id}_{service_identifier}_{stat_key}"
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
                            subentry_key=service_scope.subentry_key,
                            subentry_identifier=service_identifier,
                        )
                    )
            if tracker_scope and isinstance(tracker_scope.identifier, str):
                for device in coordinator.get_subentry_snapshot(tracker_scope.subentry_key):
                    dev_id = device.get("id")
                    dev_name = device.get("name")
                    if not isinstance(dev_id, str) or not dev_id or not isinstance(
                        dev_name, str
                    ) or not dev_name:
                        continue
                    if not _is_visible_device(dev_id):
                        continue
                    unique_id = (
                        f"{DOMAIN}_{entry_id}_{tracker_scope.identifier}_{dev_id}_last_seen"
                    )
                    if unique_id not in missing:
                        continue
                    built.append(
                        GoogleFindMyLastSeenSensor(
                            coordinator,
                            device,
                            subentry_key=tracker_scope.subentry_key,
                            subentry_identifier=tracker_scope.identifier,
                        )
                    )
            return built

        recovery_manager.register_sensor_platform(
            expected_unique_ids=_expected_unique_ids,
            entity_factory=_build_entities,
            add_entities=_recovery_add_entities,
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
        return bool(super().available)

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
        if not super().available:
            return False
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
                new_dt = datetime.fromtimestamp(float(value), tz=UTC)
            elif isinstance(value, str):
                v = value.strip()
                if v.endswith("Z"):
                    v = v.replace("Z", "+00:00")
                try:
                    dt = datetime.fromisoformat(v)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=UTC)
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
                        dt = dt.replace(tzinfo=UTC)
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
        self._attr_native_value = datetime.fromtimestamp(ts, tz=UTC)
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Expose DeviceInfo using the shared entity helper."""

        return super().device_info
