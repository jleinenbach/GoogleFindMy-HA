# custom_components/googlefindmy/device_tracker.py
"""Device tracker platform for Google Find My Device.

Notes on design and consistency with the coordinator:
- Entities are created from the coordinator's full snapshot and added dynamically later.
- Location significance gating and stale-timestamp guard are enforced by the coordinator,
  not here. This entity simply reflects the coordinator's sanitized cache.
- Extra attributes come from `_as_ha_attributes(...)` and intentionally use stable keys
  like `accuracy_m` for recorder friendliness, while the entity's built-in accuracy
  property exposes an integer `gps_accuracy` to HA Core.
- End devices link to the per-entry SERVICE device via `via_device=(DOMAIN, f"integration_{entry_id}")`.

Entry-scope guarantees (C2):
- Unique IDs are entry-scoped using the subentry-aware schema:
  "<entry_id>:<subentry_identifier>:<device_id>" (or "<subentry_identifier>:<device_id>"
  during bootstrap before the entry ID attaches).
- Device Registry identifiers are also entry-scoped to avoid cross-account merges.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.device_tracker import SourceType
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_GPS_ACCURACY,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import OPT_MIN_ACCURACY_THRESHOLD
from .coordinator import GoogleFindMyCoordinator, _as_ha_attributes
from .entity import GoogleFindMyDeviceEntity, resolve_coordinator, _entry_option
from .ha_typing import RestoreEntity, TrackerEntity, callback

_LOGGER = logging.getLogger(__name__)

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
) -> None:
    """Set up Google Find My Device tracker entities.

    Behavior:
    - On setup, create entities for all devices in the coordinator snapshot (if any).
    - Listen for coordinator updates and add entities for newly discovered devices.
    """
    coordinator = resolve_coordinator(config_entry)

    subentry_key = coordinator.get_subentry_key_for_feature("device_tracker")
    subentry_identifier = coordinator.stable_subentry_identifier(key=subentry_key)
    entities: list[GoogleFindMyDeviceTracker] = []
    known_ids: set[str] = set()

    # Startup population from coordinator snapshot (if already present)
    initial_snapshot = coordinator.get_subentry_snapshot(subentry_key)
    for device in initial_snapshot:
        dev_id = device.get("id")
        name = device.get("name")
        if not dev_id or not name:
            _LOGGER.debug("Skipping device without id/name: %s", device)
            continue
        if dev_id in known_ids:
            _LOGGER.debug("Ignoring duplicate device id %s in startup snapshot", dev_id)
            continue
        known_ids.add(dev_id)
        entities.append(
            GoogleFindMyDeviceTracker(
                coordinator,
                device,
                subentry_key=subentry_key,
                subentry_identifier=subentry_identifier,
            )
        )

    if entities:
        async_add_entities(entities, True)

    # Dynamically add new trackers when the coordinator learns about more devices
    @callback
    def _sync_entities_from_coordinator() -> None:
        snapshot = coordinator.get_subentry_snapshot(subentry_key)
        if not snapshot:
            return

        to_add: list[GoogleFindMyDeviceTracker] = []
        for device in snapshot:
            dev_id = device.get("id")
            name = device.get("name")
            if not dev_id or not name:
                continue
            if dev_id in known_ids:
                continue
            known_ids.add(dev_id)
            to_add.append(
                GoogleFindMyDeviceTracker(
                    coordinator,
                    device,
                    subentry_key=subentry_key,
                    subentry_identifier=subentry_identifier,
                )
            )

        if to_add:
            _LOGGER.info("Adding %d newly discovered Find My tracker(s)", len(to_add))
            async_add_entities(to_add, True)

    unsub = coordinator.async_add_listener(_sync_entities_from_coordinator)
    config_entry.async_on_unload(unsub)
    _sync_entities_from_coordinator()  # run once after registration to catch races


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class GoogleFindMyDeviceTracker(GoogleFindMyDeviceEntity, TrackerEntity, RestoreEntity):
    """Representation of a Google Find My Device tracker."""

    # Convention: trackers represent the device itself; the entity name
    # should not have a suffix and will track the device name.
    _attr_has_entity_name = False
    _attr_source_type = SourceType.GPS
    _attr_entity_category = None  # ensure tracker is not diagnostic
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
