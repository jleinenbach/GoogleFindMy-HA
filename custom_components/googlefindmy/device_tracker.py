# custom_components/googlefindmy/device_tracker.py
"""Device tracker platform for Google Find My Device.

Notes on design and consistency with the coordinator:
- Entities are created from the coordinator's full snapshot and added dynamically later.
- Location significance gating and stale-timestamp guard are enforced by the coordinator,
  not here. This entity simply reflects the coordinator's sanitized cache.
- Extra attributes come from `_as_ha_attributes(...)` and intentionally use stable keys
  like `accuracy_m` for recorder friendliness, while the entity's built-in accuracy
  property exposes an integer `gps_accuracy` to HA Core.
- End devices rely on tracker subentry identifiers to associate with their
  registry entry; avoid manual `via_device` linkage.

Entry-scope guarantees (C2):
- Unique IDs are entry-scoped using the subentry-aware schema:
  "<entry_id>:<subentry_identifier>:<device_id>" (or "<subentry_identifier>:<device_id>"
  during bootstrap before the entry ID attaches).
- Device Registry identifiers are also entry-scoped to avoid cross-account merges.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from homeassistant.components.device_tracker import SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_GPS_ACCURACY,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EntityRecoveryManager, _extract_email_from_entry
from .const import (
    CONF_OAUTH_TOKEN,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    OPT_MIN_ACCURACY_THRESHOLD,
    TRACKER_SUBENTRY_KEY,
)
from .coordinator import GoogleFindMyCoordinator, _as_ha_attributes
from .discovery import (
    CLOUD_DISCOVERY_NAMESPACE,
    _cloud_discovery_stable_key,
    _redact_account_for_log,
    _trigger_cloud_discovery,
)
from .entity import (
    GoogleFindMyDeviceEntity,
    _entry_option,
    ensure_config_subentry_id,
    ensure_dispatcher_dependencies,
    known_config_subentry_ids,
    resolve_coordinator,
    schedule_add_entities,
)
from .ha_typing import RestoreEntity, TrackerEntity, callback

_LOGGER = logging.getLogger(__name__)


@dataclass
class _TrackerScope:
    """Tracker subentry details for entity creation."""

    subentry_key: str
    config_subentry_id: str | None
    identifier: str | None


def _subentry_type(subentry: Any | None) -> str | None:
    """Return the declared subentry type, if present."""

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    *,
    config_subentry_id: str | None = None,
) -> None:
    """Set up Google Find My Device tracker entities.

    Behavior:
    - On setup, create entities for all devices in the coordinator snapshot (if any).
    - Listen for coordinator updates and add entities for newly discovered devices.
    """
    _LOGGER.debug(
        "Device tracker setup invoked for entry=%s config_subentry_id=%s",
        getattr(config_entry, "entry_id", None),
        config_subentry_id,
    )
    coordinator = resolve_coordinator(config_entry)
    ensure_dispatcher_dependencies(hass)
    if getattr(coordinator, "config_entry", None) is None:
        coordinator.config_entry = config_entry

    def _known_subentry_ids() -> set[str]:
        return known_config_subentry_ids(config_entry)

    def _collect_tracker_scopes(
        hint_subentry_id: str | None = None,
        forwarded_config_id: str | None = None,
    ) -> list[_TrackerScope]:
        scopes: dict[str, _TrackerScope] = {}

        subentry_metas = getattr(coordinator, "_subentry_metadata", None)
        if isinstance(subentry_metas, Mapping):
            for key, meta in subentry_metas.items():
                meta_features = getattr(meta, "features", ())
                if "device_tracker" not in meta_features:
                    continue

                stable_identifier = getattr(meta, "stable_identifier", None)
                identifier = (
                    stable_identifier() if callable(stable_identifier) else None
                    or getattr(meta, "config_subentry_id", None)
                    or coordinator.stable_subentry_identifier(key=key)
                )
                scopes[identifier] = _TrackerScope(
                    key,
                    getattr(meta, "config_subentry_id", None),
                    identifier,
                )

        subentries = getattr(config_entry, "subentries", None)
        if isinstance(subentries, Mapping):
            for subentry in subentries.values():
                data = getattr(subentry, "data", {})
                group_key = TRACKER_SUBENTRY_KEY
                subentry_features: Iterable[Any] = ()
                if isinstance(data, Mapping):
                    group_key = data.get("group_key", group_key)
                    subentry_features = data.get("features", ())

                if "device_tracker" not in subentry_features:
                    continue

                config_id = (
                    getattr(subentry, "subentry_id", None)
                    or getattr(subentry, "entry_id", None)
                )
                identifier = (
                    config_id
                    or coordinator.stable_subentry_identifier(key=group_key)
                    or TRACKER_SUBENTRY_KEY
                )
                scopes.setdefault(
                    identifier,
                    _TrackerScope(group_key or TRACKER_SUBENTRY_KEY, config_id, identifier),
                )

        if hint_subentry_id:
            scopes.setdefault(
                hint_subentry_id,
                _TrackerScope(
                    TRACKER_SUBENTRY_KEY,
                    forwarded_config_id or hint_subentry_id,
                    hint_subentry_id,
                ),
            )

        if scopes:
            return list(scopes.values())

        fallback_identifier = coordinator.stable_subentry_identifier(key=TRACKER_SUBENTRY_KEY)
        return [
            _TrackerScope(
                TRACKER_SUBENTRY_KEY,
                forwarded_config_id or fallback_identifier,
                fallback_identifier or TRACKER_SUBENTRY_KEY,
            )
        ]

    added_unique_ids: set[str] = set()
    scope_states: dict[str, dict[str, Any]] = {}

    def _add_scope(scope: _TrackerScope, forwarded_config_id: str | None) -> None:
        scope_identifier = scope.identifier or scope.config_subentry_id or scope.subentry_key
        if scope_identifier in scope_states:
            scope_states[scope_identifier]["scan"]()
            return

        tracker_subentry_key = scope.subentry_key or TRACKER_SUBENTRY_KEY

        candidate_subentry_id = scope.config_subentry_id or forwarded_config_id
        tracker_config_subentry_id = (
            ensure_config_subentry_id(
                config_entry,
                "device_tracker",
                candidate_subentry_id,
                known_ids=_known_subentry_ids(),
            )
            if candidate_subentry_id is not None
            else None
        )

        if (
            candidate_subentry_id is not None
            and tracker_config_subentry_id is None
            and _known_subentry_ids()
        ):
            _LOGGER.debug(
                "Device tracker setup: skipping subentry '%s' because the config_subentry_id is unknown",
                candidate_subentry_id,
            )
            return

        tracker_identifier = (
            tracker_config_subentry_id
            or scope.identifier
            or coordinator.stable_subentry_identifier(key=tracker_subentry_key)
            or tracker_subentry_key
        )
        tracker_config_subentry_for_entities = (
            tracker_config_subentry_id or tracker_identifier
        )

        expected_config_subentry_id = scope.config_subentry_id or tracker_config_subentry_id

        _LOGGER.debug(
            "Device tracker setup: subentry_key=%s, config_subentry_id=%s",
            tracker_subentry_key,
            tracker_config_subentry_id,
        )

        if (
            forwarded_config_id
            and expected_config_subentry_id
            and forwarded_config_id != expected_config_subentry_id
        ):
            current_known_ids = _known_subentry_ids()
            if current_known_ids and forwarded_config_id not in current_known_ids:
                _LOGGER.debug(
                    "Device tracker setup skipped for unknown forwarded subentry '%s' (known: %s)",
                    forwarded_config_id,
                    ", ".join(sorted(current_known_ids)),
                )
                return
            _LOGGER.debug(
                "Device tracker setup ignored for unrelated subentry '%s' (expected '%s')",
                forwarded_config_id,
                expected_config_subentry_id,
            )
            return

        if tracker_config_subentry_for_entities is None:
            _LOGGER.debug(
                "Device tracker setup: awaiting config_subentry_id for key '%s'; skipping",
                tracker_subentry_key,
            )
            return

        tracker_config_subentry_for_entities_str = tracker_config_subentry_for_entities
        tracker_subentry_identifier_str = (
            tracker_identifier
            or tracker_config_subentry_for_entities_str
            or tracker_subentry_key
        )

        tracker_meta = coordinator.get_subentry_metadata(key=tracker_subentry_key)
        known_ids: set[str] = set()

        coordinator.get_subentry_snapshot(tracker_subentry_key)

        tracker_entities_added = False

        def _schedule_tracker_entities(
            new_entities: Iterable[GoogleFindMyDeviceTracker],
            update_before_add: bool = True,
        ) -> None:
            nonlocal tracker_entities_added

            entity_list = list(new_entities)
            tracker_entities_added |= bool(entity_list)

            schedule_add_entities(
                coordinator.hass,
                async_add_entities,
                entities=entity_list,
                update_before_add=update_before_add,
                config_subentry_id=tracker_config_subentry_for_entities_str,
                log_owner="Device tracker setup",
                logger=_LOGGER,
            )

        def _build_entities(
            snapshot: Sequence[Mapping[str, Any]]
        ) -> list[GoogleFindMyDeviceTracker]:
            to_add: list[GoogleFindMyDeviceTracker] = []
            for device in snapshot:
                dev_id = device.get("id")
                name = device.get("name")
                if not dev_id or not name:
                    continue
                if dev_id in known_ids:
                    continue
                entity = GoogleFindMyDeviceTracker(
                    coordinator,
                    dict(device),
                    subentry_key=tracker_subentry_key,
                    subentry_identifier=tracker_subentry_identifier_str,
                )
                unique_id = getattr(entity, "unique_id", None)
                if isinstance(unique_id, str):
                    if unique_id in added_unique_ids:
                        continue
                    added_unique_ids.add(unique_id)
                known_ids.add(dev_id)
                to_add.append(entity)
            return to_add

        @callback
        def _scan_available_trackers_from_coordinator() -> None:
            snapshot = coordinator.get_subentry_snapshot(tracker_subentry_key)
            if not snapshot:
                _LOGGER.debug(
                    "Device tracker setup: no coordinator snapshot for subentry %s", tracker_subentry_key
                )
                _schedule_tracker_entities([], True)
                return

            to_add = _build_entities(snapshot)
            if to_add:
                _LOGGER.info("Adding %d newly discovered Find My tracker(s)", len(to_add))
                _schedule_tracker_entities(to_add, True)

                registry_lookup = getattr(coordinator, "find_tracker_entity_entry", None)
                if callable(registry_lookup):
                    all_registered = True

                    for entity in to_add:
                        dev_id = getattr(entity, "device_id", None)

                        try:
                            if not dev_id or registry_lookup(dev_id) is None:
                                all_registered = False
                                break
                        except Exception:  # pragma: no cover - best effort registry probe
                            _LOGGER.debug(
                                "Registry lookup failed for tracker %s", dev_id
                            )
                            all_registered = False
                            break

                    if all_registered:
                        _LOGGER.debug(
                            "Device tracker setup: all %d tracker(s) already registered; skipping discovery",
                            len(to_add),
                        )
                        return
                else:
                    _LOGGER.debug(
                        "Device tracker setup: registry helper unavailable; treating trackers as new"
                    )

                email = _extract_email_from_entry(config_entry) or None
                token = config_entry.data.get(CONF_OAUTH_TOKEN)
                token_value = token if isinstance(token, str) and token else None
                secrets_raw = config_entry.data.get(DATA_SECRET_BUNDLE)
                secrets_bundle: Mapping[str, Any] | None
                if isinstance(secrets_raw, Mapping):
                    secrets_bundle = secrets_raw
                else:
                    secrets_bundle = None

                discovery_ns = f"{CLOUD_DISCOVERY_NAMESPACE}.{config_entry.entry_id}" if config_entry.entry_id else CLOUD_DISCOVERY_NAMESPACE
                stable_key = _cloud_discovery_stable_key(
                    email,
                    token_value,
                    secrets_bundle,
                )

                async def _async_trigger_cloud_scan(new_count: int) -> None:
                    triggered = await _trigger_cloud_discovery(
                        hass,
                        email=email,
                        token=token_value,
                        secrets_bundle=secrets_bundle,
                        discovery_ns=discovery_ns,
                        discovery_stable_key=stable_key,
                        source="cloud_scanner",
                    )
                    account_ref = _redact_account_for_log(email, stable_key)
                    if triggered:
                        _LOGGER.info(
                            "Cloud tracker scanner queued discovery for %s after %d newly available tracker(s)",
                            account_ref,
                            new_count,
                        )
                    else:
                        _LOGGER.debug(
                            "Cloud tracker scanner deduplicated discovery for %s", account_ref
                        )

                hass_async_create_task = getattr(hass, "async_create_task", None)
                if callable(hass_async_create_task):
                    pending = hass_async_create_task(
                        _async_trigger_cloud_scan(len(to_add))
                    )
                    if asyncio.iscoroutine(pending):
                        asyncio.create_task(pending)
                else:
                    _LOGGER.debug(
                        "Device tracker setup: hass missing async_create_task; skipping cloud discovery trigger"
                    )

        unsub = coordinator.async_add_listener(
            _scan_available_trackers_from_coordinator
        )
        config_entry.async_on_unload(unsub)

        _scan_available_trackers_from_coordinator()

        if not tracker_entities_added:
            _schedule_tracker_entities((), True)

        runtime_data = getattr(config_entry, "runtime_data", None)
        recovery_manager = getattr(runtime_data, "entity_recovery_manager", None)

        if isinstance(recovery_manager, EntityRecoveryManager):
            entry_id = getattr(config_entry, "entry_id", None)

            def _is_visible(device_id: str) -> bool:
                try:
                    return bool(
                        device_id
                        and tracker_meta
                        and device_id
                        in getattr(tracker_meta, "visible_device_ids", [])
                    )
                except TypeError:  # pragma: no cover - fallback for misconfigured metadata
                    return False

            def _is_enabled(device_id: str) -> bool:
                try:
                    return bool(
                        device_id
                        and tracker_meta
                        and device_id
                        in getattr(tracker_meta, "enabled_device_ids", [])
                    )
                except TypeError:  # pragma: no cover - fallback for misconfigured metadata
                    return False

            def _expected_unique_ids() -> set[str]:
                if not isinstance(entry_id, str) or not entry_id:
                    return set()
                expected: set[str] = set()
                for device in coordinator.get_subentry_snapshot(tracker_subentry_key):
                    if not isinstance(device, Mapping):
                        continue
                    dev_id = device.get("id")
                    if not isinstance(dev_id, str) or not dev_id:
                        continue
                    if not _is_visible(dev_id) or not _is_enabled(dev_id):
                        continue
                    expected.add(
                        GoogleFindMyDeviceEntity.join_parts(
                            entry_id,
                            tracker_subentry_identifier_str,
                            dev_id,
                        )
                    )
                return expected

            def _build_recovery_entities(
                missing: set[str],
            ) -> list[GoogleFindMyDeviceTracker]:
                if not missing:
                    return []
                built: list[GoogleFindMyDeviceTracker] = []
                if not isinstance(entry_id, str) or not entry_id:
                    return built
                for device in coordinator.get_subentry_snapshot(
                    tracker_subentry_key
                ):
                    if not isinstance(device, Mapping):
                        continue
                    dev_id = device.get("id")
                    name = device.get("name")
                    if (
                        not isinstance(dev_id, str)
                        or not isinstance(name, str)
                        or not dev_id
                        or not name
                    ):
                        continue
                    if not _is_visible(dev_id) or not _is_enabled(dev_id):
                        continue
                    unique_id = GoogleFindMyDeviceEntity.join_parts(
                        entry_id,
                        tracker_subentry_identifier_str,
                        dev_id,
                    )
                    if unique_id not in missing:
                        continue
                    built.append(
                        GoogleFindMyDeviceTracker(
                            coordinator,
                            dict(device),
                            subentry_key=tracker_subentry_key,
                            subentry_identifier=tracker_subentry_identifier_str,
                        )
                    )
                return built

            recovery_manager.register_device_tracker_platform(
                expected_unique_ids=_expected_unique_ids,
                entity_factory=_build_recovery_entities,
                add_entities=_schedule_tracker_entities,
            )

        scope_states[scope_identifier] = {"scan": _scan_available_trackers_from_coordinator}

    seen_subentries: set[str | None] = set()
    seen_subentry_keys: set[str] = set()
    placeholder_subentries: set[str | None] = set()
    placeholder_subentry_keys: set[str] = set()

    @callback
    def async_add_subentry(subentry: Any | None = None) -> None:
        subentry_key = TRACKER_SUBENTRY_KEY
        subentry_identifier = None
        if isinstance(subentry, str):
            subentry_identifier = subentry
        else:
            subentry_identifier = getattr(subentry, "subentry_id", None) or getattr(
                subentry, "entry_id", None
            )
            data = getattr(subentry, "data", None)
            if isinstance(data, Mapping):
                data_group_key = data.get("group_key")
                if isinstance(data_group_key, str) and data_group_key.strip():
                    subentry_key = data_group_key.strip()

        if not subentry_identifier:
            try:
                stable_identifier = coordinator.stable_subentry_identifier(
                    key=subentry_key
                )
            except Exception:  # pragma: no cover - best effort fallback
                stable_identifier = None

            subentry_identifier = (
                stable_identifier
                if isinstance(stable_identifier, str) and stable_identifier
                else subentry_key
            )

        subentry_type = _subentry_type(subentry)
        _LOGGER.debug(
            "Device tracker setup: processing subentry '%s' (type=%s)",
            subentry_identifier,
            subentry_type,
        )
        if subentry_type is not None and subentry_type != "tracker":
            _LOGGER.debug(
                "Device tracker setup skipped for unrelated subentry '%s' (type '%s')",
                subentry_identifier,
                subentry_type,
            )
            return

        has_full_context = hasattr(subentry, "subentry_id") or hasattr(
            subentry, "data"
        )
        if subentry_identifier in seen_subentries or subentry_key in seen_subentry_keys:
            if has_full_context and (
                subentry_identifier in placeholder_subentries
                or subentry_key in placeholder_subentry_keys
            ):
                placeholder_subentries.discard(subentry_identifier)
                placeholder_subentry_keys.discard(subentry_key)
                seen_subentries.discard(subentry_identifier)
                seen_subentry_keys.discard(subentry_key)
            else:
                return

        if has_full_context:
            seen_subentries.add(subentry_identifier)
            seen_subentry_keys.add(subentry_key)
        else:
            placeholder_subentries.add(subentry_identifier)
            placeholder_subentry_keys.add(subentry_key)

        for scope in _collect_tracker_scopes(
            subentry_identifier, forwarded_config_id=subentry_identifier
        ):
            _add_scope(scope, subentry_identifier)

    @callback
    def _handle_subentry_setup(subentry: Any | None = None) -> None:
        """Schedule subentry setup from dispatcher callbacks."""

        hass_async_create_task = getattr(hass, "async_create_task", None)
        if callable(hass_async_create_task):
            result = async_add_subentry(subentry)
            if inspect.isawaitable(result):
                hass_async_create_task(result)
            return

        hass_add_job = getattr(hass, "add_job", None)
        if callable(hass_add_job):
            result = async_add_subentry(subentry)
            if inspect.isawaitable(result):
                hass_add_job(result)
            return

        async_add_subentry(subentry)

    runtime_data = getattr(config_entry, "runtime_data", None)
    subentry_manager = getattr(runtime_data, "subentry_manager", None)
    managed_subentries = getattr(subentry_manager, "managed_subentries", None)
    entry_subentries = (
        config_entry.subentries
        if isinstance(getattr(config_entry, "subentries", None), Mapping)
        else None
    )

    if isinstance(managed_subentries, Mapping) and managed_subentries:
        for managed_subentry in managed_subentries.values():
            async_add_subentry(managed_subentry)
    elif isinstance(managed_subentries, Mapping):
        async_add_subentry(config_subentry_id)
    elif isinstance(entry_subentries, Mapping) and entry_subentries:
        for managed_subentry in entry_subentries.values():
            async_add_subentry(managed_subentry)
    else:
        async_add_subentry(config_subentry_id)

    config_entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            f"{DOMAIN}_subentry_setup_{config_entry.entry_id}",
            _handle_subentry_setup,
        )
    )



# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class GoogleFindMyDeviceTracker(GoogleFindMyDeviceEntity, TrackerEntity, RestoreEntity):
    """Representation of a Google Find My Device tracker."""

    # Convention: trackers represent the device itself; the entity name
    # should not have a suffix and will track the device name.
    _attr_has_entity_name = False
    _attr_source_type = SourceType.GPS
    _attr_entity_category: EntityCategory | None = None  # ensure tracker is not diagnostic
    # Default to enabled in the registry for per-device trackers
    _attr_entity_registry_enabled_default = True
    _attr_translation_key = "device"

    # ---- Display-name policy (strip legacy prefixes, no new prefixes) ----
    @staticmethod
    def _display_name(raw: str | None) -> str:
        """Return the UI display name without legacy prefixes."""
        name = (raw or "").strip()
        if name.lower().startswith("find my - "):
            name = name[10:].strip()
        return name or "Google Find My Device"

    def device_label(self) -> str:
        """Return the sanitized device label used for DeviceInfo."""

        return self._display_name(super().device_label())

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
        *,
        subentry_key: str,
        subentry_identifier: str,
    ) -> None:
        """Initialize the tracker entity."""
        super().__init__(
            coordinator,
            device,
            subentry_key=subentry_key,
            subentry_identifier=subentry_identifier,
            fallback_label=device.get("name"),
        )

        entry_id = self.entry_id
        dev_id = self.device_id

        self._attr_unique_id = self.build_unique_id(
            entry_id,
            subentry_identifier,
            dev_id,
        )

        # With has_entity_name=False we must set the entity's name ourselves.
        # If name is missing during cold boot, HA will show the entity_id; that's fine.
        self._attr_name = self._display_name(device.get("name"))

        # Persist a "last good" fix to keep map position usable when current accuracy is filtered
        self._last_good_accuracy_data: dict[str, Any] | None = None

    async def async_added_to_hass(self) -> None:
        """Restore last known location and seed the coordinator cache.

        On cold boots where the coordinator hasn't polled yet, we restore the last
        coordinates from the state machine to provide a better initial UX. We also
        prime the coordinator's cache via its public priming API (no private access).
        """
        await super().async_added_to_hass()

        try:
            last_state = await self.async_get_last_state()
        except (RuntimeError, AttributeError) as err:
            _LOGGER.debug("Failed to get last state for %s: %s", self.entity_id, err)
            return

        if not last_state:
            return

        # Standard device_tracker attributes (with safe fallbacks for legacy keys)
        lat = last_state.attributes.get(
            ATTR_LATITUDE, last_state.attributes.get("latitude")
        )
        lon = last_state.attributes.get(
            ATTR_LONGITUDE, last_state.attributes.get("longitude")
        )
        acc = last_state.attributes.get(
            ATTR_GPS_ACCURACY, last_state.attributes.get("gps_accuracy")
        )

        restored: dict[str, Any] = {}
        try:
            if lat is not None and lon is not None:
                restored["latitude"] = float(lat)
                restored["longitude"] = float(lon)
            if acc is not None:
                # HA core accuracy attribute is an int (meters).
                restored["accuracy"] = int(float(acc))
        except (TypeError, ValueError) as ex:
            _LOGGER.debug("Invalid restored coordinates for %s: %s", self.entity_id, ex)
            restored = {}

        if restored:
            self._last_good_accuracy_data = {**restored}
            # Prime coordinator cache using its public API (no private access).
            dev_id = self.device_id
            try:
                self.coordinator.prime_device_location_cache(dev_id, restored)
            except (AttributeError, TypeError) as err:
                _LOGGER.debug(
                    "Failed to seed coordinator cache for %s: %s", self.entity_id, err
                )

            self.async_write_ha_state()

    # ---------------- Device Info + Map Link ----------------
    @property
    def device_info(self) -> DeviceInfo:
        """Expose DeviceInfo using the shared entity helper."""

        return super().device_info

    def _current_row(self) -> dict[str, Any] | None:
        """Get current device data from the coordinator's public cache API."""

        dev_id = self.device_id
        try:
            data = self.coordinator.get_device_location_data_for_subentry(
                self.subentry_key, dev_id
            )
        except (AttributeError, TypeError):
            return None
        if isinstance(data, dict):
            return data
        return None

    @property
    def available(self) -> bool:
        """Return True if the device is currently present according to the coordinator.

        Presence has priority over restored coordinates: if the device is no
        longer present in the Google list (TTL-smoothed by the coordinator),
        the entity becomes unavailable and the user may delete it via HA UI.
        """
        if not super().available:
            return False
        if not self.coordinator_has_device():
            return False
        # Prefer coordinator presence; fall back to previous behavior if API is missing.
        try:
            if hasattr(self.coordinator, "is_device_present"):
                if not self.coordinator.is_device_present(self.device_id):
                    return False
        except Exception:
            # Be tolerant in case of older coordinator builds
            pass

        device_data = self._current_row()
        if device_data:
            if (
                device_data.get("latitude") is not None
                and device_data.get("longitude") is not None
            ) or device_data.get("semantic_name") is not None:
                return True
        return self._last_good_accuracy_data is not None

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device (float, if known)."""
        data = self._current_row() or self._last_good_accuracy_data
        if not data:
            return None
        return data.get("latitude")

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device (float, if known)."""
        data = self._current_row() or self._last_good_accuracy_data
        if not data:
            return None
        return data.get("longitude")

    @property
    def location_accuracy(self) -> int | None:
        """Return accuracy of location in meters as an integer.

        Coordinator stores accuracy as a float; HA's device_tracker expects
        an integer for the `gps_accuracy` attribute, so we coerce here.
        """
        data = self._current_row() or self._last_good_accuracy_data
        if not data:
            return None
        acc = data.get("accuracy")
        if acc is None:
            return None
        try:
            return int(round(float(acc)))
        except (TypeError, ValueError):
            return None

    @property
    def location_name(self) -> str | None:
        """Return a human place label only when it should override zone logic.

        Rules:
        - If we have valid coordinates, let HA compute the zone name.
        - If we don't have coordinates, fall back to Google's semantic label.
        - Never override zones with generic 'home' labels from Google.
        """
        data = self._current_row()
        if not data:
            return None

        lat = data.get("latitude")
        lon = data.get("longitude")
        sem = data.get("semantic_name")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            # Coordinates present -> let HA zone engine decide.
            return None

        if isinstance(sem, str) and sem.strip().casefold() in {"home", "zuhause"}:
            return None

        return sem

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes for diagnostics/UX (sanitized).

        Delegates to the coordinator helper `_as_ha_attributes`, which:
        - Adds a normalized UTC timestamp mirror (`last_seen_utc`).
        - Uses `accuracy_m` (float meters) rather than `gps_accuracy` for stability.
        - Includes source labeling (`source_label`/`source_rank`) for transparency.
        """
        row = self._current_row()
        attributes = _as_ha_attributes(row)
        return attributes if attributes is not None else {}

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator updates.

        - Keep the device's human-readable name in sync with the coordinator snapshot.
        - Maintain 'last good' accuracy data when current fixes are worse than the threshold.
        """
        if not self.coordinator_has_device():
            self._last_good_accuracy_data = None
            self.async_write_ha_state()
            return

        self.refresh_device_label_from_coordinator(log_prefix="DeviceTracker")
        desired_display = self._display_name(self._device.get("name"))
        if self._attr_name != desired_display:
            _LOGGER.debug(
                "Updating entity name for %s: '%s' -> '%s'",
                self.entity_id,
                self._attr_name,
                desired_display,
            )
            self._attr_name = desired_display

        config_entry = getattr(self.coordinator, "config_entry", None)
        min_accuracy_raw = _entry_option(
            config_entry,
            OPT_MIN_ACCURACY_THRESHOLD,
            0,
        )
        try:
            min_accuracy_threshold = float(min_accuracy_raw)
        except (TypeError, ValueError):
            min_accuracy_threshold = 0.0

        device_data = self._current_row()
        if not device_data:
            self.async_write_ha_state()
            return

        accuracy = device_data.get("accuracy")
        lat = device_data.get("latitude")
        lon = device_data.get("longitude")

        # Keep best-known fix when accuracy filtering rejects the current one.
        is_good = min_accuracy_threshold <= 0 or (
            accuracy is not None
            and lat is not None
            and lon is not None
            and accuracy <= min_accuracy_threshold
        )

        if is_good:
            self._last_good_accuracy_data = device_data.copy()
            if min_accuracy_threshold > 0 and accuracy is not None:
                _LOGGER.debug(
                    "Updated last good accuracy data for %s: accuracy=%sm (threshold=%sm)",
                    self.entity_id,
                    accuracy,
                    min_accuracy_threshold,
                )
        elif accuracy is not None:
            _LOGGER.debug(
                "Keeping previous good data for %s: current accuracy=%sm > threshold=%sm",
                self.entity_id,
                accuracy,
                min_accuracy_threshold,
            )

        self.async_write_ha_state()
