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
- Unique IDs are entry-scoped using the new schema: "<entry_id>:<device_id>".
- Device Registry identifiers are also entry-scoped to avoid cross-account merges.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_GPS_ACCURACY,
    ATTR_LATITUDE,
    ATTR_LONGITUDE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DOMAIN,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    map_token_hex_digest,
    map_token_secret_seed,
    service_device_identifier,
)
from .coordinator import GoogleFindMyCoordinator, _as_ha_attributes

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_update_device_registry_name(
    hass: HomeAssistant, entity_id: str, new_name: str
) -> None:
    """Write the real Google device label into the Device Registry once known.

    We never touch the registry if the user renamed the device (name_by_user set).
    Defensive behavior:
    - Only update if we have a device for the entity and a non-empty name.
    - Avoid churn: skip if the current registry name already matches.

    This function is best-effort and will silently no-op on errors.
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
                "Device Registry name updated for %s: '%s' -> '%s'",
                entity_id,
                dev.name,
                new_name,
            )
    except Exception as e:  # noqa: BLE001 - best-effort only
        _LOGGER.debug("Device Registry name update failed for %s: %s", entity_id, e)


def _entry_id_of(coordinator: GoogleFindMyCoordinator) -> str:
    """Return the entry_id for namespacing (empty string if unavailable)."""
    return getattr(getattr(coordinator, "config_entry", None), "entry_id", "") or ""


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
    runtime = getattr(config_entry, "runtime_data", None)
    coordinator: GoogleFindMyCoordinator | None = None
    if isinstance(runtime, GoogleFindMyCoordinator):
        coordinator = runtime
    elif runtime is not None:
        coordinator = getattr(runtime, "coordinator", None)

    if not isinstance(coordinator, GoogleFindMyCoordinator):
        raise HomeAssistantError("googlefindmy coordinator not ready")

    entities: list[GoogleFindMyDeviceTracker] = []
    known_ids: set[str] = set()

    # Startup population from coordinator snapshot (if already present)
    if coordinator.data:
        for device in coordinator.data:
            dev_id = device.get("id")
            name = device.get("name")
            if dev_id and name:
                known_ids.add(dev_id)
                entities.append(GoogleFindMyDeviceTracker(coordinator, device))
            else:
                _LOGGER.debug("Skipping device without id/name: %s", device)

    if entities:
        async_add_entities(entities, True)

    # Dynamically add new trackers when the coordinator learns about more devices
    @callback
    def _sync_entities_from_coordinator() -> None:
        if not coordinator.data:
            return

        to_add: list[GoogleFindMyDeviceTracker] = []
        for device in coordinator.data:
            dev_id = device.get("id")
            name = device.get("name")
            if not dev_id or not name:
                continue
            if dev_id in known_ids:
                continue
            known_ids.add(dev_id)
            to_add.append(GoogleFindMyDeviceTracker(coordinator, device))

        if to_add:
            _LOGGER.info("Adding %d newly discovered Find My tracker(s)", len(to_add))
            async_add_entities(to_add, True)

    unsub = coordinator.async_add_listener(_sync_entities_from_coordinator)
    config_entry.async_on_unload(unsub)
    _sync_entities_from_coordinator()  # run once after registration to catch races


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class GoogleFindMyDeviceTracker(CoordinatorEntity, TrackerEntity, RestoreEntity):
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

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the tracker entity."""
        super().__init__(coordinator)
        self._device = device

        entry_id = _entry_id_of(coordinator)
        dev_id = device["id"]

        # New unique_id schema: "<entry_id>:<device_id>" (entry-scoped).
        self._attr_unique_id = f"{entry_id}:{dev_id}"

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
            dev_id = self._device["id"]
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
        """Return DeviceInfo with a stable configuration_url and safe naming.

        Important:
        - Identifiers are entry-scoped to keep devices distinct across accounts.
        - Link this end device to the per-entry SERVICE device using `via_device`.
        """
        try:
            base_url = get_url(
                self.hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except HomeAssistantError as e:
            _LOGGER.debug(
                "Could not determine Home Assistant URL, using fallback: %s", e
            )
            base_url = "http://homeassistant.local:8123"

        entry_id = _entry_id_of(self.coordinator)
        dev_id = self._device["id"]

        auth_token = self._get_map_token()
        path = self._build_map_path(dev_id, auth_token, redirect=False)

        # Avoid overwriting stored device names during cold boot
        raw_name = self._device.get("name")
        display_name = self._display_name(raw_name) if raw_name else None

        name_kwargs: dict[str, Any] = {}
        if display_name and display_name != "Google Find My Device":
            # Only pass a real name; never pass default_name here.
            name_kwargs["name"] = display_name

        # Link against the per-entry service device
        via = service_device_identifier(entry_id) if entry_id else None

        # Entry-scoped device identifier to avoid merges across accounts.
        entry_scoped_identifier = f"{entry_id}:{dev_id}" if entry_id else dev_id

        return DeviceInfo(
            identifiers={(DOMAIN, entry_scoped_identifier)},
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}" if base_url else None,
            serial_number=dev_id,  # technical id in the proper field
            via_device=via,
            **name_kwargs,
        )

    @staticmethod
    def _build_map_path(device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host)."""
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a hardened token for map authentication (entry-scoped + weekly/static).

        Token formula:
            secret = map_token_secret_seed(...)
            token  = map_token_hex_digest(secret)
        """
        config_entry = getattr(self.coordinator, "config_entry", None)

        # Prefer the central _opt helper; fall back to direct options/data reads.
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
        # Best-effort UUID; on very early startup it might be missing (safe fallback).
        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))

        if token_expiration_enabled:
            # Weekly-rolling token (7-day bucket).
            seed = map_token_secret_seed(ha_uuid, entry_id, True, now=int(time.time()))
        else:
            # Static token (no rotation).
            seed = map_token_secret_seed(ha_uuid, entry_id, False)

        # Short SHA-256 digest slice is fine here; this token does not gate sensitive operations.
        return map_token_hex_digest(seed)

    def _current_row(self) -> dict[str, Any] | None:
        """Get current device data from the coordinator's public cache API."""
        dev_id = self._device["id"]
        try:
            return self.coordinator.get_device_location_data(dev_id)
        except (AttributeError, TypeError):
            return None

    @property
    def available(self) -> bool:
        """Return True if the device is currently present according to the coordinator.

        Presence has priority over restored coordinates: if the device is no
        longer present in the Google list (TTL-smoothed by the coordinator),
        the entity becomes unavailable and the user may delete it via HA UI.
        """
        dev_id = self._device["id"]
        # Prefer coordinator presence; fall back to previous behavior if API is missing.
        try:
            if hasattr(self.coordinator, "is_device_present"):
                if not self.coordinator.is_device_present(dev_id):
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
        return _as_ha_attributes(row) or {}

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator updates.

        - Keep the device's human-readable name in sync with the coordinator snapshot.
        - Maintain 'last good' accuracy data when current fixes are worse than the threshold.
        """
        # Sync the raw device name from the coordinator and keep the entity display name in sync (no prefixes).
        try:
            data = getattr(self.coordinator, "data", None) or []
            my_id = self._device["id"]
            for dev in data:
                if dev.get("id") == my_id:
                    new_name = dev.get("name")
                    # Ignore bootstrap placeholder names
                    if not new_name or new_name == "Google Find My Device":
                        break
                    if new_name != self._device.get("name"):
                        old = self._device.get("name")
                        _LOGGER.debug(
                            "Coordinator provided Google name for %s: '%s' -> '%s'",
                            my_id,
                            old,
                            new_name,
                        )
                        self._device["name"] = new_name
                        # Sync Device Registry (no-op if user renamed)
                        _maybe_update_device_registry_name(
                            self.hass, self.entity_id, new_name
                        )
                        # Update entity display name (has_entity_name=False).
                        desired_display = self._display_name(new_name)
                        if self._attr_name != desired_display:
                            _LOGGER.debug(
                                "Updating entity name for %s (%s): '%s' -> '%s'",
                                self.entity_id,
                                my_id,
                                self._attr_name,
                                desired_display,
                            )
                            self._attr_name = desired_display
                    break
        except (AttributeError, TypeError):
            # Non-critical update; ignore failures.
            pass

        config_entry = getattr(self.coordinator, "config_entry", None)
        if config_entry:
            # Helper defined in __init__.py for options-first reading.
            from . import _opt

            min_accuracy_threshold = _opt(config_entry, "min_accuracy_threshold", 0)
        else:
            min_accuracy_threshold = 0  # fallback if entry is not available

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
