"""Device tracker platform for Google Find My Device."""
from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
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
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_MAP_VIEW_TOKEN_EXPIRATION, DOMAIN
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)


def _maybe_update_device_registry_name(hass, entity_id: str, new_name: str) -> None:
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
                entity_id, dev.name, new_name
            )
    except Exception as e:
        _LOGGER.debug("Device registry name update failed for %s: %s", entity_id, e)

async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device tracker entities.

    Design goals:
    - Follow HA convention: tracker entity represents the device itself.
    - Create entities from current coordinator snapshot when available.
    - On cold start, create "skeleton" entities from tracked IDs to enable RestoreEntity.
    - Dynamically add entities for devices discovered later.
    """
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][config_entry.entry_id]

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
    else:
        # No live data yet: create skeletons for configured tracked IDs (restore-friendly).
        tracked_ids: list[str] = getattr(coordinator, "tracked_devices", []) or []
        for dev_id in tracked_ids:
            # Neutral default name for early boot; will be replaced on first update.
            name = "Google Find My Device"
            known_ids.add(dev_id)
            entities.append(GoogleFindMyDeviceTracker(coordinator, {"id": dev_id, "name": name}))
        if tracked_ids:
            _LOGGER.debug(
                "Created %d skeleton device_tracker entities for restore (no live data yet)",
                len(tracked_ids),
            )

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


class GoogleFindMyDeviceTracker(CoordinatorEntity, TrackerEntity, RestoreEntity):
    """Representation of a Google Find My Device tracker."""

    # Convention: trackers represent the device itself, so the entity name
    # should not have a suffix and will follow the device name.
    _attr_has_entity_name = False
    _attr_source_type = SourceType.GPS
    _attr_entity_category = None  # Ensure tracker is not diagnostic

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the tracker entity."""
        super().__init__(coordinator)
        self._device = device
        self._attr_unique_id = f"{DOMAIN}_{device['id']}"
        # NEW: Set an explicit entity display name from the current device name.
        # Without this, HA leaves the Name field empty when has_entity_name=False.
        self._attr_name = device.get("name") or "Google Find My Device"
        self._last_good_accuracy_data: dict[str, Any] | None = None  # Persisted coordinates for writes

    async def async_added_to_hass(self) -> None:
        """Restore last known location and seed the coordinator cache.

        Why:
        - Keeps the entity immediately useful across restarts, even before the first poll.
        - Uses public coordinator API; avoids writing private attrs.
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
        lat = last_state.attributes.get(ATTR_LATITUDE, last_state.attributes.get("latitude"))
        lon = last_state.attributes.get(ATTR_LONGITUDE, last_state.attributes.get("longitude"))
        acc = last_state.attributes.get(
            ATTR_GPS_ACCURACY, last_state.attributes.get("gps_accuracy")
        )

        restored: dict[str, Any] = {}
        try:
            if lat is not None and lon is not None:
                restored["latitude"] = float(lat)
                restored["longitude"] = float(lon)
            if acc is not None:
                restored["accuracy"] = int(acc)
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

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info, including a configuration_url to the map view."""
        # Build token + path first
        auth_token = self._get_map_token()
        path = self._build_map_path(self._device["id"], auth_token, redirect=False)

        # Return an absolute URL; the redirect view keeps it robust across origins.
        try:
            base_url = get_url(
                self.hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except HomeAssistantError as e:
            _LOGGER.debug("Could not determine Home Assistant URL, using fallback: %s", e)
            base_url = "http://homeassistant.local:8123"

        return DeviceInfo(
            identifiers={(DOMAIN, self._device["id"])},
            name=self._device.get("name"),
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}" if base_url else None,
            # Expose the technical ID in the semantically correct field.
            serial_number=self._device["id"],
        )

    def _build_map_path(self, device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host)."""
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    @property
    def _current_device_data(self) -> dict[str, Any] | None:
        """Get current device data from the coordinator's public cache API."""
        dev_id = self._device["id"]
        try:
            return self.coordinator.get_device_location_data(dev_id)
        except (AttributeError, TypeError) as err:
            _LOGGER.debug(
                "Coordinator.get_device_location_data failed for %s: %s", dev_id, err
            )
            return None

    @property
    def _data_to_persist(self) -> dict[str, Any] | None:
        """Return data used for persistent state (lat/lon/accuracy)."""
        return self._last_good_accuracy_data or self._current_device_data

    @property
    def available(self) -> bool:
        """Return True if the entity has valid location data (or restored data).

        UX rationale:
        - If the coordinator does not yet have live data, keep the entity available
        when we restored a valid location to avoid 'unavailable' after reboot.
        """
        device_data = self._current_device_data
        if device_data:
            if (
                device_data.get("latitude") is not None
                and device_data.get("longitude") is not None
            ) or device_data.get("semantic_name") is not None:
                return True
        return self._last_good_accuracy_data is not None

    @property
    def latitude(self) -> float | None:
        """Return latitude value of the device."""
        if data := self._data_to_persist:
            return data.get("latitude")
        return None

    @property
    def longitude(self) -> float | None:
        """Return longitude value of the device."""
        if data := self._data_to_persist:
            return data.get("longitude")
        return None

    @property
    def location_accuracy(self) -> int | None:
        """Return accuracy of location."""
        if data := self._data_to_persist:
            return data.get("accuracy")
        return None

    @property
    def location_name(self) -> str | None:
        """Return a semantic location (if provided by the API)."""
        if device_data := self._current_device_data:
            return device_data.get("semantic_name")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes for diagnostics/UX."""
        attributes: dict[str, Any] = {}
        if device_data := self._current_device_data:
            if last_seen_ts := device_data.get("last_seen"):
                # Use timezone-aware UTC for consistency across the integration
                attributes["last_seen"] = datetime.fromtimestamp(
                    float(last_seen_ts), tz=timezone.utc
                ).isoformat()
            if altitude := device_data.get("altitude"):
                attributes["altitude"] = altitude
            if status := device_data.get("status"):
                attributes["device_status"] = status
            if (is_own := device_data.get("is_own_report")) is not None:
                attributes["is_own_report"] = is_own
            if semantic_name := device_data.get("semantic_name"):
                attributes["semantic_location"] = semantic_name
        return attributes

    def _get_map_token(self) -> str:
        """Generate a simple token for map authentication (options-first)."""
        config_entry = getattr(self.coordinator, "config_entry", None)
        if config_entry:
            # Helper defined in __init__.py for options-first reading
            from . import _opt

            token_expiration_enabled = _opt(
                config_entry, "map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            )
        else:
            token_expiration_enabled = DEFAULT_MAP_VIEW_TOKEN_EXPIRATION

        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))

        if token_expiration_enabled:
            # Weekly-rolling token (7-day bucket)
            week = str(int(time.time() // 604800))
            secret = f"{ha_uuid}:{week}"
        else:
            # Static token (no rotation)
            secret = f"{ha_uuid}:static"

        return hashlib.md5(secret.encode()).hexdigest()[:16]

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator updates.

        - Keep the device's human-readable name in sync with the coordinator snapshot.
        - Maintain 'last good' accuracy data when new fixes are worse than the threshold.
        """
        # Sync the device name from the coordinator's public data.
        try:
            data = getattr(self.coordinator, "data", None) or []
            my_id = self._device["id"]
            for dev in data:
                if dev.get("id") == my_id:
                    new_name = dev.get("name")
                    # Ignore bootstrap placeholder names
                    if not new_name or new_name == "Google Find My Device":
                        break 
                    if new_name and new_name != self._device.get("name"):
                        old = self._device.get("name")
                        self._device["name"] = new_name
                        # Keep device registry in sync (no-op if user renamed device)
                        _maybe_update_device_registry_name(self.hass, self.entity_id, new_name)
                        # NEW: Also update the entity display name to keep UI in sync.
                        # This matters because has_entity_name=False does not auto-copy.
                        if self._attr_name != new_name:
                            _LOGGER.debug(
                                "Updating entity name for %s (%s): '%s' -> '%s'",
                                self.entity_id,
                                my_id,
                                old,
                                new_name,
                            )
                            self._attr_name = new_name
                    break
        except (AttributeError, TypeError):
            # This is a non-critical update, so we can ignore failures.
            pass

        config_entry = getattr(self.coordinator, "config_entry", None)
        if config_entry:
            # Helper defined in __init__.py for options-first reading
            from . import _opt

            min_accuracy_threshold = _opt(config_entry, "min_accuracy_threshold", 0)
        else:
            min_accuracy_threshold = 0  # Fallback if entry is not available

        device_data = self._current_device_data
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
