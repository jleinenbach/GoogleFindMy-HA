"""Subentry operations for GoogleFindMyCoordinator.

This module contains subentry-related methods extracted from main.py
during Phase 3 of the refactoring.
"""

from __future__ import annotations

import asyncio
import logging
import time
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from homeassistant.helpers import device_registry as dr

from ..const import (
    DOMAIN,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SERVICE_SUBENTRY_TRANSLATION_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    TRACKER_SUBENTRY_TRANSLATION_KEY,
)
from ._mixin_typing import _MixinBase
from .helpers.subentry import (
    detect_missing_core_subentry_keys as _detect_missing_core_keys_impl,
)
from .helpers.subentry import (
    extract_subentry_group_key as _extract_group_key_impl,
)
from .helpers.subentry import (
    filter_provisional_identifier as _filter_provisional_impl,
)
from .helpers.subentry import (
    group_devices_by_subentry as _group_devices_impl,
)
from .helpers.subentry import (
    sanitize_subentry_identifier as _sanitize_subentry_id_impl,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .. import ConfigEntrySubentryDefinition, ConfigEntrySubEntryManager

_LOGGER = logging.getLogger(__name__)


# --- Subentry feature sets ---------------------------------------------------
_DEFAULT_SUBENTRY_FEATURES: tuple[str, ...] = (
    "binary_sensor",
    "button",
    "device_tracker",
    "sensor",
)

_SERVICE_SUBENTRY_FEATURES: tuple[str, ...] = tuple(
    sorted(dict.fromkeys(SERVICE_FEATURE_PLATFORMS))
)
_TRACKER_SUBENTRY_FEATURES: tuple[str, ...] = tuple(
    sorted(dict.fromkeys(TRACKER_FEATURE_PLATFORMS))
)


# --- SubentryMetadata dataclass ----------------------------------------------
@dataclass(slots=True, frozen=True)
class SubentryMetadata:
    """Lightweight view of a config-entry subentry relevant to platforms."""

    key: str
    config_subentry_id: str | None
    features: tuple[str, ...]
    title: str | None
    poll_intervals: Mapping[str, int]
    filters: Mapping[str, Any]
    feature_flags: Mapping[str, Any]
    visible_device_ids: tuple[str, ...]
    enabled_device_ids: tuple[str, ...]

    def stable_identifier(self) -> str:
        """Return the identifier to use when namespacing entities."""

        return self.config_subentry_id or self.key

    @property
    def subentry_id(self) -> str | None:
        """Backwards-compatible alias for the config subentry identifier."""

        return self.config_subentry_id


# --- Helper functions --------------------------------------------------------
def _sanitize_subentry_identifier(candidate: Any) -> str | None:
    """Return a normalized subentry identifier or ``None`` when fabricated."""
    return _sanitize_subentry_id_impl(candidate)


# --- SubentryOperations mixin ------------------------------------------------


class SubentryOperations(_MixinBase):
    """Subentry operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage config entry subentries,
    including creation, updates, and synchronization of subentry indices
    with tracked devices.
    """

    # Attribute declarations for mypy (actual values set in GoogleFindMyCoordinator.__init__)
    _subentry_manager: ConfigEntrySubEntryManager | None
    _pending_subentry_repair: asyncio.Task[None] | None
    _present_last_seen: dict[str, float]
    _present_device_ids: set[str]

    def attach_subentry_manager(
        self,
        manager: ConfigEntrySubEntryManager,
        *,
        is_reload: bool = False,
    ) -> None:
        """Attach the config entry subentry manager to the coordinator."""

        self._subentry_manager = manager
        self._skip_repair_during_reload_refresh = bool(is_reload)
        self._reload_repair_skip_pending_release = False
        if manager is None:
            return

        try:
            self._refresh_subentry_index(
                skip_manager_update=True, skip_repair=is_reload
            )
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "Initial subentry refresh failed during setup: %s",
                err,
            )
            return

        service_meta = self._subentry_metadata.get(SERVICE_SUBENTRY_KEY)
        if service_meta is not None and service_meta.config_subentry_id:
            try:
                self._ensure_service_device_exists()
            except Exception as err:  # noqa: BLE001 - defensive guard
                _LOGGER.debug(
                    "Service device ensure skipped during setup: %s",
                    err,
                )

    def _default_subentry_key(self) -> str:
        """Return the default subentry key used when no explicit mapping exists."""

        return self._default_subentry_key_value or "core_tracking"

    async def async_wait_subentry_visibility_updates(
        self,
    ) -> None:
        """Await pending visibility updates scheduled by the subentry manager."""

        manager = self._subentry_manager
        wait_visible = getattr(manager, "async_wait_visible_device_updates", None)
        if not callable(wait_visible):
            return

        try:
            await wait_visible()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "[%s] Visibility wait helper skipped due to: %s",
                self._entry_id() or "unknown",
                err,
            )

    def _build_core_subentry_definitions(
        self,
    ) -> list[ConfigEntrySubentryDefinition]:
        """Return definitions for the core tracker/service subentries."""

        entry = self.config_entry or getattr(self, "entry", None)
        entry_id = getattr(entry, "entry_id", None) if entry is not None else None
        if entry is None or not isinstance(entry_id, str) or not entry_id:
            _LOGGER.debug(
                "Skipping core subentry repair: config entry unavailable (entry=%s)",
                entry,
            )
            return []

        try:
            from .. import ConfigEntrySubentryDefinition  # local import to avoid cycles
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "Skipping core subentry repair: definition factory import failed (%s)",
                err,
            )
            return []

        runtime_data = getattr(entry, "runtime_data", None)
        fcm_receiver = getattr(runtime_data, "fcm_receiver", None)
        google_home_filter = getattr(runtime_data, "google_home_filter", None)

        fcm_push_enabled = fcm_receiver is not None
        has_google_home_filter = google_home_filter is not None
        entry_title = getattr(entry, "title", None) or "Google Find My"

        tracker_features = list(_TRACKER_SUBENTRY_FEATURES or TRACKER_FEATURE_PLATFORMS)
        service_features = list(_SERVICE_SUBENTRY_FEATURES or SERVICE_FEATURE_PLATFORMS)

        tracker_definition = ConfigEntrySubentryDefinition(
            key=TRACKER_SUBENTRY_KEY,
            title="Google Find My devices",
            data={
                "features": tracker_features,
                "fcm_push_enabled": fcm_push_enabled,
                "has_google_home_filter": has_google_home_filter,
                "entry_title": entry_title,
            },
            subentry_type=SUBENTRY_TYPE_TRACKER,
            unique_id=f"{entry_id}-{TRACKER_SUBENTRY_KEY}",
            translation_key=TRACKER_SUBENTRY_TRANSLATION_KEY,
        )
        service_definition = ConfigEntrySubentryDefinition(
            key=SERVICE_SUBENTRY_KEY,
            title="Google Find Hub Service",
            data={
                "features": service_features,
                "fcm_push_enabled": fcm_push_enabled,
                "has_google_home_filter": has_google_home_filter,
                "entry_title": entry_title,
            },
            subentry_type=SUBENTRY_TYPE_SERVICE,
            unique_id=f"{entry_id}-{SERVICE_SUBENTRY_KEY}",
            translation_key=SERVICE_SUBENTRY_TRANSLATION_KEY,
        )

        return [tracker_definition, service_definition]

    def _schedule_core_subentry_repair(
        self, missing_keys: set[str]
    ) -> None:
        """Schedule a repair task to recreate missing core subentries."""

        if not missing_keys:
            return

        manager = self._subentry_manager
        hass = getattr(self, "hass", None)
        if manager is None or hass is None:
            return

        pending = self._pending_subentry_repair
        if pending is not None and not pending.done():
            _LOGGER.debug(
                "Core subentry repair already running; deferring additional request (%s)",
                sorted(missing_keys),
            )
            return

        entry_id = self._entry_id() or "unknown"

        async def _repair() -> None:
            try:
                if not self._config_entry_exists(entry_id):
                    _LOGGER.debug(
                        "Skipping core subentry repair for %s: entry removed", entry_id
                    )
                    return

                definitions = self._build_core_subentry_definitions()
                if not definitions:
                    _LOGGER.debug(
                        "Core subentry repair skipped for %s: definitions unavailable",
                        entry_id,
                    )
                    return

                _LOGGER.debug(
                    "Repairing missing subentries %s for entry %s",
                    sorted(missing_keys),
                    entry_id,
                )
                await manager.async_sync(definitions)
            except asyncio.CancelledError:  # pragma: no cover - task cancelled
                raise
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.warning(
                    "Core subentry repair failed for entry %s: %s",
                    entry_id,
                    err,
                )
                return
            finally:
                self._pending_subentry_repair = None

            if not self._config_entry_exists(entry_id):
                _LOGGER.debug(
                    "Skipping core subentry post-processing for %s: entry removed",
                    entry_id,
                )
                return

            self._ensure_service_device_exists()
            self._refresh_subentry_index()
            _LOGGER.debug("Core subentry repair completed for entry %s", entry_id)

        task_name = f"{DOMAIN}.repair_core_subentries"
        create_task = getattr(hass, "async_create_task", None)
        if callable(create_task):
            task = create_task(_repair(), name=task_name)
        else:  # pragma: no cover - fallback for legacy stubs
            task = asyncio.create_task(_repair(), name=task_name)
        self._pending_subentry_repair = task

    def _cancel_pending_subentry_repair(self) -> None:
        """Cancel any pending core subentry repair task."""

        pending = self._pending_subentry_repair
        if pending is None:
            return

        if not pending.done():
            pending.cancel()

        self._pending_subentry_repair = None

    def _refresh_subentry_index(
        self,
        visible_devices: Sequence[Mapping[str, Any]] | None = None,
        *,
        skip_manager_update: bool = False,
        skip_repair: bool = False,
    ) -> None:
        """Refresh internal subentry metadata caches."""

        if not hasattr(self, "_present_last_seen"):
            self._present_last_seen = {}
        if not hasattr(self, "_present_device_ids"):
            self._present_device_ids = set()

        reload_skip_active = bool(
            getattr(self, "_skip_repair_during_reload_refresh", False)
        )
        reload_skip_consumed = False
        if reload_skip_active and not skip_repair:
            skip_repair = True
            reload_skip_consumed = True
            self._reload_repair_skip_pending_release = True

        entry = self.config_entry

        entry_id = getattr(entry, "entry_id", None)
        entry_service_subentry_id = (
            _sanitize_subentry_identifier(getattr(entry, "service_subentry_id", None))
            if entry is not None
            else None
        )
        entry_tracker_subentry_id = (
            _sanitize_subentry_identifier(getattr(entry, "tracker_subentry_id", None))
            if entry is not None
            else None
        )

        service_provisional_seen = False
        tracker_provisional_seen = False

        raw_entries: list[tuple[str, str | None, dict[str, Any], str | None]] = []
        core_group_keys_present: set[str] = set()
        if entry and getattr(entry, "subentries", None):
            for subentry in entry.subentries.values():
                data = dict(getattr(subentry, "data", {}) or {})
                subentry_id_raw = getattr(subentry, "subentry_id", None)
                group_key = _extract_group_key_impl(data, subentry_id_raw)
                if group_key in (SERVICE_SUBENTRY_KEY, TRACKER_SUBENTRY_KEY):
                    core_group_keys_present.add(group_key)
                identifier = _sanitize_subentry_identifier(subentry_id_raw)

                # Use filter_provisional_identifier for service subentries
                if group_key == SERVICE_SUBENTRY_KEY:
                    identifier, was_filtered = _filter_provisional_impl(
                        identifier,
                        group_key,
                        entry_service_subentry_id,
                        SERVICE_SUBENTRY_KEY,
                        TRACKER_SUBENTRY_KEY,
                    )
                    if was_filtered:
                        service_provisional_seen = True
                # Use filter_provisional_identifier for tracker subentries
                elif group_key == TRACKER_SUBENTRY_KEY:
                    identifier, was_filtered = _filter_provisional_impl(
                        identifier,
                        group_key,
                        entry_tracker_subentry_id,
                        SERVICE_SUBENTRY_KEY,
                        TRACKER_SUBENTRY_KEY,
                    )
                    if was_filtered:
                        tracker_provisional_seen = True

                raw_entries.append(
                    (
                        group_key,
                        identifier,
                        data,
                        getattr(subentry, "title", None),
                    )
                )

        if entry is not None:
            missing_core_keys = _detect_missing_core_keys_impl(
                core_group_keys_present,
                SERVICE_SUBENTRY_KEY,
                TRACKER_SUBENTRY_KEY,
            )
        else:
            missing_core_keys = set()

        if not raw_entries:
            raw_entries.append(
                (
                    "core_tracking",
                    None,
                    {
                        "features": _DEFAULT_SUBENTRY_FEATURES,
                        "feature_flags": {},
                    },
                    getattr(entry, "title", None),
                )
            )

        ignored = self._get_ignored_set()
        device_index: dict[str, dict[str, Any]] = {}
        now_mono = time.monotonic()

        device_registry: dr.DeviceRegistry | None = None
        registry_lookup: Callable[[str], dr.DeviceEntry | None] | None = None
        hass_obj = getattr(self, "hass", None)
        if hass_obj is not None:
            try:
                device_registry = dr.async_get(hass_obj)
            except Exception:  # defensive: registry helpers may not be patched in tests
                device_registry = None
            else:
                candidate_lookup = getattr(device_registry, "async_get", None)
                if callable(candidate_lookup):
                    registry_lookup = candidate_lookup

        canonical_to_registry_id: dict[str, str] = {}
        registry_to_canonical: dict[str, str] = {}
        if device_registry is not None:
            candidate_entries: list[Any] = []
            raw_devices = getattr(device_registry, "devices", None)
            if isinstance(raw_devices, Mapping):
                candidate_entries.extend(raw_devices.values())
            else:
                registry_entries = getattr(device_registry, "_entries", None)
                if isinstance(registry_entries, Mapping):
                    candidate_entries.extend(registry_entries.values())

            if not candidate_entries:
                entry_id = self._entry_id()
                fetch_entries = getattr(dr, "async_entries_for_config_entry", None)
                if callable(fetch_entries) and entry_id:
                    try:
                        candidate_entries.extend(
                            fetch_entries(device_registry, entry_id)
                        )
                    except Exception:  # defensive: stub mismatches / legacy HA versions
                        candidate_entries = []

            for device_entry in candidate_entries:
                try:
                    canonical = self._extract_our_identifier(device_entry)
                except Exception:  # defensive: tolerate stub deviations
                    canonical = None
                if not canonical:
                    continue
                device_id_attr = getattr(device_entry, "id", None)
                if isinstance(device_id_attr, str) and device_id_attr:
                    canonical_to_registry_id.setdefault(canonical, device_id_attr)
                    registry_to_canonical.setdefault(device_id_attr, canonical)

        def _register_device(candidate: Mapping[str, Any]) -> None:
            dev_id = candidate.get("id")
            if not isinstance(dev_id, str) or not dev_id:
                fallback_id = candidate.get("device_id")
                if isinstance(fallback_id, str) and fallback_id:
                    dev_id = fallback_id
                else:
                    return
            if dev_id in ignored:
                return
            name = (
                candidate.get("name")
                if isinstance(candidate.get("name"), str)
                else None
            )
            device_index.setdefault(dev_id, {"id": dev_id, "name": name})

        if visible_devices is not None:
            for dev in visible_devices:
                if isinstance(dev, Mapping):
                    _register_device(dev)
        else:
            for dev in self.data or []:
                if isinstance(dev, Mapping):
                    _register_device(dev)

        if device_index:
            self._present_device_ids = set(device_index)
            for dev_id in device_index:
                self._present_last_seen.setdefault(dev_id, now_mono)

        previous_visible: dict[str, tuple[str, ...]] = {
            key: meta.visible_device_ids
            for key, meta in self._subentry_metadata.items()
        }

        metadata: dict[str, SubentryMetadata] = {}
        feature_map: dict[str, str] = {}
        default_key: str | None = None
        manager_visible: dict[str, tuple[str, ...]] = {}

        def _current_poll_intervals() -> Mapping[str, int]:
            return MappingProxyType(
                {
                    "location": int(self.location_poll_interval),
                    "minimum": int(self.min_poll_interval),
                    "device": int(self.device_poll_delay),
                }
            )

        def _current_filters() -> Mapping[str, Any]:
            return MappingProxyType(
                {
                    "ignored_device_ids": tuple(sorted(ignored)),
                    "allow_history_fallback": bool(self.allow_history_fallback),
                }
            )

        for group_key, subentry_id, data, title in raw_entries:
            raw_features = data.get("features")
            if isinstance(raw_features, (list, tuple, set)):
                normalized_features = tuple(
                    sorted(
                        {
                            str(feature)
                            for feature in raw_features
                            if isinstance(feature, str)
                        }
                    )
                )
            else:
                normalized_features = _DEFAULT_SUBENTRY_FEATURES

            if group_key == SERVICE_SUBENTRY_KEY:
                features = _SERVICE_SUBENTRY_FEATURES or normalized_features
            elif group_key == TRACKER_SUBENTRY_KEY:
                features = _TRACKER_SUBENTRY_FEATURES or normalized_features
            else:
                features = normalized_features or _DEFAULT_SUBENTRY_FEATURES

            raw_flags = data.get("feature_flags")
            feature_flags: dict[str, Any]
            if isinstance(raw_flags, Mapping):
                feature_flags = {str(key): raw_flags[key] for key in raw_flags}
            else:
                feature_flags = dict[str, Any]()

            raw_allowed = data.get("visible_device_ids")
            normalized_allowed: set[str] | None = None
            if isinstance(raw_allowed, (list, tuple, set)):
                collected: set[str] = set()
                for item in raw_allowed:
                    if not isinstance(item, str) or not item:
                        continue
                    cleaned = item.rsplit(":", 1)[-1] if ":" in item else item
                    if cleaned:
                        collected.add(cleaned)
                if collected:
                    normalized_allowed = set(collected)
                    if registry_lookup is not None:
                        resolved: set[str] = set()
                        for candidate in collected:
                            try:
                                device_entry = registry_lookup(candidate)
                            except Exception:  # defensive against stub mismatches
                                device_entry = None
                            if device_entry is None:
                                continue
                            canonical = self._extract_our_identifier(device_entry)
                            if canonical:
                                resolved.add(canonical)
                        if resolved:
                            normalized_allowed.update(resolved)
                else:
                    normalized_allowed = None

            allow_filter = normalized_allowed

            if device_index:
                base_ids = [
                    dev_id
                    for dev_id in device_index
                    if allow_filter is None or dev_id in allow_filter
                ]
            else:
                base_ids = [
                    dev_id
                    for dev_id in previous_visible.get(group_key, ())
                    if allow_filter is None or dev_id in allow_filter
                ]

            visibility_candidates: list[str] = list(base_ids)
            if normalized_allowed:
                visibility_candidates.extend(normalized_allowed)

            visible_ids = tuple(sorted(dict.fromkeys(visibility_candidates)))
            if group_key != SERVICE_SUBENTRY_KEY and registry_to_canonical:
                canonicalized_ids: list[str] = []
                for dev_id in visible_ids:
                    canonicalized_ids.append(dev_id)
                    canonical_id = registry_to_canonical.get(dev_id)
                    if canonical_id and canonical_id != dev_id:
                        canonicalized_ids.append(canonical_id)
                visible_ids = tuple(sorted(dict.fromkeys(canonicalized_ids)))

            if group_key == SERVICE_SUBENTRY_KEY:
                visible_ids = cast(tuple[str, ...], ())
                enabled_ids = cast(tuple[str, ...], ())
                manager_visible_ids = cast(tuple[str, ...], ())
            else:
                enabled_ids = tuple(
                    sorted(
                        dev_id
                        for dev_id in visible_ids
                        if dev_id in self._enabled_poll_device_ids
                    )
                )
                manager_visible_ids = tuple(
                    dict.fromkeys(
                        canonical_to_registry_id.get(dev_id, dev_id)
                        for dev_id in visible_ids
                    )
                )

            metadata[group_key] = SubentryMetadata(
                key=group_key,
                config_subentry_id=subentry_id,
                features=features,
                title=title,
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType(dict(feature_flags)),
                visible_device_ids=visible_ids,
                enabled_device_ids=enabled_ids,
            )

            if group_key != SERVICE_SUBENTRY_KEY:
                manager_visible[group_key] = manager_visible_ids

            for feature in features:
                feature_map.setdefault(feature, group_key)

            if default_key is None:
                default_key = group_key

        if SERVICE_SUBENTRY_KEY not in metadata:
            service_features = _SERVICE_SUBENTRY_FEATURES or _DEFAULT_SUBENTRY_FEATURES
            stable_service_id: str | None
            if isinstance(entry_id, str) and entry_id and not service_provisional_seen:
                stable_service_id = f"{entry_id}-{SERVICE_SUBENTRY_KEY}-subentry"
            else:
                stable_service_id = None
            metadata[SERVICE_SUBENTRY_KEY] = SubentryMetadata(
                key=SERVICE_SUBENTRY_KEY,
                config_subentry_id=stable_service_id,
                features=service_features,
                title=getattr(entry, "title", None),
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType({}),
                visible_device_ids=(),
                enabled_device_ids=(),
            )
            for feature in service_features:
                feature_map.setdefault(feature, SERVICE_SUBENTRY_KEY)

        if TRACKER_SUBENTRY_KEY not in metadata:
            tracker_features = _TRACKER_SUBENTRY_FEATURES or _DEFAULT_SUBENTRY_FEATURES
            previous_tracker_visible = previous_visible.get(TRACKER_SUBENTRY_KEY, ())
            stable_tracker_id: str | None
            if isinstance(entry_id, str) and entry_id and not tracker_provisional_seen:
                stable_tracker_id = f"{entry_id}-{TRACKER_SUBENTRY_KEY}-subentry"
            else:
                stable_tracker_id = None

            if device_index:
                tracker_visible_ids = tuple(sorted(device_index.keys()))
            else:
                tracker_visible_ids = previous_tracker_visible
            metadata[TRACKER_SUBENTRY_KEY] = SubentryMetadata(
                key=TRACKER_SUBENTRY_KEY,
                config_subentry_id=stable_tracker_id,
                features=tracker_features,
                title=getattr(entry, "title", None),
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType({}),
                visible_device_ids=tracker_visible_ids,
                enabled_device_ids=tuple(
                    dev_id
                    for dev_id in tracker_visible_ids
                    if dev_id in self._enabled_poll_device_ids
                ),
            )
            manager_visible[TRACKER_SUBENTRY_KEY] = tuple(
                dict.fromkeys(
                    canonical_to_registry_id.get(dev_id, dev_id)
                    for dev_id in tracker_visible_ids
                )
            )
            for feature in tracker_features:
                feature_map.setdefault(feature, TRACKER_SUBENTRY_KEY)

        if isinstance(entry_id, str) and entry_id:
            stable_ids = {
                SERVICE_SUBENTRY_KEY: f"{entry_id}-{SERVICE_SUBENTRY_KEY}-subentry",
                TRACKER_SUBENTRY_KEY: f"{entry_id}-{TRACKER_SUBENTRY_KEY}-subentry",
            }

            for key, default_id in stable_ids.items():
                if (key == SERVICE_SUBENTRY_KEY and service_provisional_seen) or (
                    key == TRACKER_SUBENTRY_KEY and tracker_provisional_seen
                ):
                    continue
                meta = metadata.get(key)
                if meta is None or meta.config_subentry_id is not None:
                    continue

                metadata[key] = replace(meta, config_subentry_id=default_id)

        self._subentry_metadata = metadata
        self._feature_to_subentry = feature_map
        if TRACKER_SUBENTRY_KEY in metadata:
            default_key = TRACKER_SUBENTRY_KEY
        elif default_key is None and metadata:
            default_key = next(iter(metadata))
        if default_key:
            self._default_subentry_key_value = default_key

        manager = self._subentry_manager
        if reload_skip_consumed:
            if visible_devices is not None or missing_core_keys:
                self._skip_repair_during_reload_refresh = False
                self._reload_repair_skip_pending_release = False
        elif (
            reload_skip_active
            and self._reload_repair_skip_pending_release
            and (visible_devices is not None or missing_core_keys)
        ):
            self._skip_repair_during_reload_refresh = False
            self._reload_repair_skip_pending_release = False

        if not skip_repair and missing_core_keys:
            self._schedule_core_subentry_repair(missing_core_keys)

        if manager and manager_visible and not skip_manager_update:
            for group_key, visible_ids in manager_visible.items():
                if group_key == SERVICE_SUBENTRY_KEY:
                    continue
                manager.update_visible_device_ids(group_key, visible_ids)
        # Ensure snapshot container has entries for all known keys
        for key in list(self._subentry_snapshots):
            if key not in metadata:
                self._subentry_snapshots.pop(key, None)
        for key in metadata:
            self._subentry_snapshots.setdefault(key, ())

    def _group_snapshot_by_subentry(
        self, snapshot: Sequence[Mapping[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Return snapshot entries grouped by subentry key."""
        # Build device-to-subentry mapping from metadata
        device_to_key: dict[str, str] = {}
        for key, meta in self._subentry_metadata.items():
            for dev_id in meta.visible_device_ids:
                device_to_key.setdefault(dev_id, key)

        return _group_devices_impl(
            snapshot,
            device_to_key,
            self._default_subentry_key(),
            set(self._subentry_metadata.keys()),
        )

    def _store_subentry_snapshots(
        self, snapshot: Sequence[Mapping[str, Any]]
    ) -> None:
        """Persist grouped snapshots for subentry-aware consumers."""

        grouped = self._group_snapshot_by_subentry(snapshot)
        self._subentry_snapshots = {
            key: tuple(entries) for key, entries in grouped.items()
        }

    def _resolve_subentry_key_for_feature(
        self, feature: str
    ) -> str:
        """Return the subentry key for a platform feature without warnings."""

        return self._feature_to_subentry.get(feature, self._default_subentry_key())

    def get_subentry_key_for_feature(
        self, feature: str
    ) -> str:
        """Return the subentry key responsible for a platform feature."""

        warnings.warn(
            "get_subentry_key_for_feature() is deprecated; pass the subentry key "
            "explicitly when constructing entities.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._resolve_subentry_key_for_feature(feature)

    def get_subentry_metadata(
        self,
        *,
        key: str | None = None,
        feature: str | None = None,
    ) -> SubentryMetadata | None:
        """Return metadata for a given subentry key or feature."""

        lookup_key = key
        if lookup_key is None and feature is not None:
            lookup_key = self._resolve_subentry_key_for_feature(feature)
        if lookup_key is None:
            return None
        return self._subentry_metadata.get(lookup_key)

    def stable_subentry_identifier(
        self,
        *,
        key: str | None = None,
        feature: str | None = None,
    ) -> str:
        """Return the stable identifier string for a subentry."""

        meta = self.get_subentry_metadata(key=key, feature=feature)
        if meta is not None:
            return meta.stable_identifier()
        if key:
            return key
        if feature:
            return feature
        return self._default_subentry_key()

    def get_subentry_snapshot(
        self,
        key: str | None = None,
        *,
        feature: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return a copy of the current snapshot for a subentry."""

        lookup_key = key
        if lookup_key is None and feature is not None:
            lookup_key = self._resolve_subentry_key_for_feature(feature)
        if lookup_key is None:
            lookup_key = self._default_subentry_key()
        entries = self._subentry_snapshots.get(lookup_key)
        if not entries:
            return []
        return [dict(row) for row in entries]

    def is_device_visible_in_subentry(
        self, subentry_key: str, device_id: str
    ) -> bool:
        """Return True if a device is visible within the subentry scope.

        Handles both raw device IDs and namespaced identifiers (ENTRY_ID:DEVICE_ID)
        to ensure robust visibility checks in multi-account setups.
        """

        meta = self._subentry_metadata.get(subentry_key)
        if meta is None:
            return False

        # Fast path: Check for exact match (raw ID)
        if device_id in meta.visible_device_ids:
            return True

        # Robust path: Check for namespaced IDs (e.g., "01KBB...:DEVICE_ID")
        # The registry index may contain the fully qualified identifier.
        suffix = f":{device_id}"
        for visible_id in meta.visible_device_ids:
            if visible_id.endswith(suffix):
                return True

        return False

    def get_device_location_data_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> dict[str, Any] | None:
        """Return location data for a device if it belongs to the subentry."""

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_device_location_data(device_id)

    def get_device_last_seen_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> datetime | None:
        """Return last_seen for a device within the given subentry."""

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_device_last_seen(device_id)
