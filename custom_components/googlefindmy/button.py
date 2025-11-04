# custom_components/googlefindmy/button.py
"""Button platform for Google Find My Device.

This module exposes per-device buttons that trigger actions on Google Find My
devices via the integration coordinator:

- **Play Sound**: request the device to play a sound.
- **Stop Sound**: request the device to stop a playing sound.
- **Locate now**: trigger an immediate manual locate through the integration's
  service call.

Quality & design notes (HA Platinum guidelines)
-----------------------------------------------
* Async-first: no blocking calls on the event loop.
* Availability mirrors device presence and capability gates from the coordinator.
* Device registry naming respects user overrides; we never write placeholders.
* Entities default to **enabled** (see `_attr_entity_registry_enabled_default = True`).
* End devices are linked to the single per-entry *service device* via `via_device`
  using the identifier `(DOMAIN, f"integration_{entry_id}")` for clean grouping.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from homeassistant.components.button import ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import voluptuous as vol

from .const import (
    DOMAIN,
    SERVICE_LOCATE_DEVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
)
from .coordinator import GoogleFindMyCoordinator
from .entity import GoogleFindMyDeviceEntity, resolve_coordinator
from .ha_typing import ButtonEntity, callback
from .util_services import register_entity_service

_LOGGER = logging.getLogger(__name__)


def _derive_device_label(device: dict[str, Any]) -> str | None:
    """Return a stable device label without mutating the coordinator snapshot."""

    name = device.get("name")
    if isinstance(name, str):
        stripped = name.strip()
        if stripped:
            return stripped

    fallback = device.get("device_id") or device.get("id")
    if isinstance(fallback, str):
        stripped = fallback.strip()
        if stripped:
            return stripped

    return None


# Reusable entity description with translations in en.json
PLAY_SOUND_DESCRIPTION = ButtonEntityDescription(
    key="play_sound",
    translation_key="play_sound",
    icon="mdi:volume-high",
)

# Entity description for stopping a sound manually
STOP_SOUND_DESCRIPTION = ButtonEntityDescription(
    key="stop_sound",
    translation_key="stop_sound",
    icon="mdi:volume-off",
)

# Entity description for the manual "Locate now" action
LOCATE_DEVICE_DESCRIPTION = ButtonEntityDescription(
    key="locate_device",
    translation_key="locate_device",
    icon="mdi:radar",
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device button entities."""
    coordinator = resolve_coordinator(config_entry)

    platform_getter = getattr(entity_platform, "async_get_current_platform", None)
    if callable(platform_getter):
        platform = platform_getter()
        if platform is not None:
            register_entity_service(
                platform,
                "trigger_device_refresh",
                vol.Schema({}),
                "async_trigger_coordinator_refresh",
            )

    def _iter_tracker_subentries() -> list[Any]:
        """Return tracker subentries associated with this config entry."""

        subentries = getattr(config_entry, "subentries", None)
        if subentries is None:
            return []

        getter = getattr(subentries, "get_subentries", None)
        candidates: Iterable[Any]
        if callable(getter):
            try:
                candidates = getter(subentry_type=SUBENTRY_TYPE_TRACKER)
            except TypeError:
                try:
                    candidates = getter(SUBENTRY_TYPE_TRACKER)
                except Exception:  # noqa: BLE001 - defensive fallback
                    candidates = ()
        elif isinstance(subentries, Mapping):
            candidates = subentries.values()
        else:
            values = getattr(subentries, "values", None)
            if callable(values):
                candidates = values()
            else:
                candidates = ()

        tracker_entries: list[Any] = []
        for subentry in candidates:
            if getattr(subentry, "subentry_type", None) == SUBENTRY_TYPE_TRACKER:
                tracker_entries.append(subentry)
        return tracker_entries

    def _normalize_visible_ids(raw: Any) -> tuple[str, ...]:
        if not isinstance(raw, (list, tuple, set)):
            return ()
        ordered = []
        for candidate in raw:
            if isinstance(candidate, str):
                normalized = candidate.strip()
                if normalized and normalized not in ordered:
                    ordered.append(normalized)
        return tuple(ordered)

    def _coordinator_device_name(device_id: str) -> str | None:
        getter = getattr(coordinator, "get_device_display_name", None)
        if callable(getter):
            try:
                result = getter(device_id)
            except Exception:  # noqa: BLE001 - defensive best effort
                result = None
            if isinstance(result, str) and result:
                return result
        name_map_getter = getattr(coordinator, "get_device_name_map", None)
        if callable(name_map_getter):
            try:
                mapping = name_map_getter()
            except Exception:  # noqa: BLE001 - defensive best effort
                mapping = None
            if isinstance(mapping, Mapping):
                value = mapping.get(device_id)
                if isinstance(value, str) and value:
                    return value
        return None

    def _tracker_groups() -> list[tuple[str, str, tuple[str, ...]]]:
        groups: list[tuple[str, str, tuple[str, ...]]] = []
        for subentry in _iter_tracker_subentries():
            data = getattr(subentry, "data", {}) or {}
            group_key = data.get("group_key")
            if not isinstance(group_key, str) or not group_key:
                group_key = TRACKER_SUBENTRY_KEY
            metadata = coordinator.get_subentry_metadata(key=group_key)
            if metadata is not None and hasattr(metadata, "stable_identifier"):
                stable_identifier = metadata.stable_identifier()
            else:
                stable_identifier = coordinator.stable_subentry_identifier(
                    key=group_key
                )
            if metadata is not None and hasattr(metadata, "visible_device_ids"):
                visible = getattr(metadata, "visible_device_ids")
            else:
                visible = _normalize_visible_ids(data.get("visible_device_ids"))
            groups.append((group_key, stable_identifier, visible))

        if groups:
            return groups

        fallback_meta = coordinator.get_subentry_metadata(feature="button")
        fallback_key = (
            fallback_meta.key if fallback_meta is not None else TRACKER_SUBENTRY_KEY
        )
        if fallback_meta is not None and hasattr(fallback_meta, "stable_identifier"):
            fallback_identifier = fallback_meta.stable_identifier()
        else:
            fallback_identifier = coordinator.stable_subentry_identifier(
                key=fallback_key
            )
        if fallback_meta is not None and hasattr(fallback_meta, "visible_device_ids"):
            fallback_visible = getattr(fallback_meta, "visible_device_ids")
        else:
            fallback_visible = ()
        return [(fallback_key, fallback_identifier, fallback_visible)]

    def _iter_tracker_devices() -> Iterable[tuple[str, dict[str, Any], str, str]]:
        for group_key, subentry_identifier, visible_ids in _tracker_groups():
            snapshot = coordinator.get_subentry_snapshot(group_key)
            snapshot_by_id: dict[str, dict[str, Any]] = {}
            for row in snapshot:
                dev_id = row.get("id") if isinstance(row, dict) else None
                if isinstance(dev_id, str) and dev_id:
                    snapshot_by_id.setdefault(dev_id, dict(row))

            ordered_ids: list[str] = list(dict.fromkeys(visible_ids))
            for dev_id in snapshot_by_id:
                if dev_id not in ordered_ids:
                    ordered_ids.append(dev_id)

            for dev_id in ordered_ids:
                if not isinstance(dev_id, str) or not dev_id:
                    continue
                payload = snapshot_by_id.get(dev_id, {"id": dev_id})
                if "id" not in payload:
                    payload = {"id": dev_id, **payload}
                if not isinstance(payload.get("name"), str):
                    fallback_name = _coordinator_device_name(dev_id)
                    if fallback_name:
                        payload = dict(payload)
                        payload.setdefault("name", fallback_name)
                yield dev_id, dict(payload), group_key, subentry_identifier

    known_ids: set[str] = set()

    def _build_entities_for_new_devices() -> list[ButtonEntity]:
        new_entities: list[ButtonEntity] = []
        for dev_id, device, subentry_key, subentry_identifier in _iter_tracker_devices():
            if dev_id in known_ids:
                continue
            label = _derive_device_label(device)
            if not label:
                fallback = _coordinator_device_name(dev_id)
                if fallback:
                    device.setdefault("name", fallback)
                    label = fallback
            fallback_label = label or _coordinator_device_name(dev_id) or dev_id
            new_entities.extend(
                (
                    GoogleFindMyPlaySoundButton(
                        coordinator,
                        dict(device),
                        fallback_label,
                        subentry_key=subentry_key,
                        subentry_identifier=subentry_identifier,
                    ),
                    GoogleFindMyStopSoundButton(
                        coordinator,
                        dict(device),
                        fallback_label,
                        subentry_key=subentry_key,
                        subentry_identifier=subentry_identifier,
                    ),
                    GoogleFindMyLocateButton(
                        coordinator,
                        dict(device),
                        fallback_label,
                        subentry_key=subentry_key,
                        subentry_identifier=subentry_identifier,
                    ),
                )
            )
            known_ids.add(dev_id)
        return new_entities

    initial_entities = _build_entities_for_new_devices()
    if initial_entities:
        _LOGGER.debug("Adding %d initial button entity(ies)", len(initial_entities))
        async_add_entities(initial_entities, True)

    @callback
    def _handle_coordinator_update() -> None:
        new_entities = _build_entities_for_new_devices()
        if new_entities:
            _LOGGER.debug(
                "Dynamically adding %d button entity(ies)", len(new_entities)
            )
            async_add_entities(new_entities, True)

    unsub = coordinator.async_add_listener(_handle_coordinator_update)
    config_entry.async_on_unload(unsub)


# ----------------------------- Base class -----------------------------------
class GoogleFindMyButtonEntity(GoogleFindMyDeviceEntity, ButtonEntity):
    """Common helpers for all per-device buttons."""

    _attr_entity_registry_enabled_default = True
    _attr_has_entity_name = True
    _attr_should_poll = False
    log_prefix = "Button"

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
        fallback_label: str | None,
        *,
        subentry_key: str,
        subentry_identifier: str,
    ) -> None:
        super().__init__(
            coordinator,
            device,
            subentry_key=subentry_key,
            subentry_identifier=subentry_identifier,
            fallback_label=fallback_label,
        )

    async def async_trigger_coordinator_refresh(self) -> None:
        """Request a coordinator refresh via the entity service placeholder."""

        await self.coordinator.async_request_refresh()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh device metadata and propagate state updates."""

        self.refresh_device_label_from_coordinator(log_prefix=self.log_prefix)
        self.async_write_ha_state()


# ----------------------------- Play Sound -----------------------------------
class GoogleFindMyPlaySoundButton(GoogleFindMyButtonEntity):
    """Button to trigger 'Play Sound' on a Google Find My Device."""

    entity_description = PLAY_SOUND_DESCRIPTION
    log_prefix = "PlaySound"

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
        fallback_label: str | None,
        *,
        subentry_key: str,
        subentry_identifier: str,
    ) -> None:
        super().__init__(
            coordinator,
            device,
            fallback_label,
            subentry_key=subentry_key,
            subentry_identifier=subentry_identifier,
        )
        dev_id = self.device_id
        self._attr_unique_id = self.build_unique_id(
            DOMAIN,
            self.entry_id,
            subentry_identifier,
            dev_id,
            "play_sound",
            separator="_",
        )

    @property
    def available(self) -> bool:
        """Return True only if the device is present AND can likely play a sound.

        Presence has priority: if the device is absent from the latest Google list,
        the button is unavailable regardless of capability/push readiness.
        """
        dev_id = self.device_id
        device_label = self.device_label()
        try:
            # Presence gate
            if not self.coordinator_has_device():
                return False
            if hasattr(
                self.coordinator, "is_device_present"
            ) and not self.coordinator.is_device_present(dev_id):
                return False
            # Capability / push readiness gate
            return bool(self.coordinator.can_play_sound(dev_id))
        except (AttributeError, TypeError) as err:
            _LOGGER.debug(
                "PlaySound availability check for %s (%s) raised %s; defaulting to True",
                device_label,
                dev_id,
                err,
            )
            return True  # Optimistic fallback

    async def async_press(self) -> None:
        """Handle the button press."""
        device_id = self.device_id
        device_name = self.device_label()

        if not self.available:
            _LOGGER.warning(
                "Play Sound not available for %s (%s) — push not ready, device not capable, or absent",
                device_name,
                device_id,
            )
            return

        _LOGGER.debug("Play Sound: attempting on %s (%s)", device_name, device_id)
        try:
            result = await self.coordinator.async_play_sound(device_id)
            if result:
                _LOGGER.info(
                    "Successfully submitted Play Sound request for %s", device_name
                )
            else:
                _LOGGER.warning(
                    "Failed to play sound on %s (request may have been rejected)",
                    device_name,
                )
        except Exception as err:  # Avoid crashing the update loop
            _LOGGER.error("Error playing sound on %s: %s", device_name, err)


# ----------------------------- Stop Sound -----------------------------------
class GoogleFindMyStopSoundButton(GoogleFindMyButtonEntity):
    """Button to trigger 'Stop Sound' on a Google Find My Device."""

    entity_description = STOP_SOUND_DESCRIPTION
    log_prefix = "StopSound"

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
        fallback_label: str | None,
        *,
        subentry_key: str,
        subentry_identifier: str,
    ) -> None:
        super().__init__(
            coordinator,
            device,
            fallback_label,
            subentry_key=subentry_key,
            subentry_identifier=subentry_identifier,
        )
        dev_id = self.device_id
        self._attr_unique_id = self.build_unique_id(
            DOMAIN,
            self.entry_id,
            subentry_identifier,
            dev_id,
            "stop_sound",
            separator="_",
        )

    @property
    def available(self) -> bool:
        """Return True if the device is present; do not couple to Play gating.

        Rationale:
        - The Play button may be intentionally unavailable during in-flight/cooldown.
        - Stopping must remain possible in that phase.
        We therefore:
          1) require presence, and
          2) prefer a dedicated `can_stop_sound()` if provided by the coordinator,
             otherwise assume stopping is allowed when the device is present.
        """
        dev_id = self.device_id
        device_label = self.device_label()
        try:
            if not self.coordinator_has_device():
                return False
            if hasattr(
                self.coordinator, "is_device_present"
            ) and not self.coordinator.is_device_present(dev_id):
                return False
            can_stop = getattr(self.coordinator, "can_stop_sound", None)
            if callable(can_stop):
                return bool(can_stop(dev_id))
            # Do NOT fall back to can_play_sound(): Stop should stay available even if Play is gated.
            return True
        except (AttributeError, TypeError) as err:
            _LOGGER.debug(
                "StopSound availability check for %s (%s) raised %s; defaulting to True",
                device_label,
                dev_id,
                err,
            )
            return True

    async def async_press(self) -> None:
        """Handle the button press (stop sound)."""
        device_id = self.device_id
        device_name = self.device_label()

        if not self.available:
            _LOGGER.warning(
                "Stop Sound not available for %s (%s) — device absent or not eligible",
                device_name,
                device_id,
            )
            return

        _LOGGER.debug("Stop Sound: attempting on %s (%s)", device_name, device_id)
        try:
            result = await self.coordinator.async_stop_sound(device_id)
            if result:
                _LOGGER.info(
                    "Successfully submitted Stop Sound request for %s", device_name
                )
            else:
                _LOGGER.warning(
                    "Failed to stop sound on %s (request may have been rejected)",
                    device_name,
                )
        except Exception as err:
            _LOGGER.error("Error stopping sound on %s: %s", device_name, err)


# ----------------------------- Locate now -----------------------------------
class GoogleFindMyLocateButton(GoogleFindMyButtonEntity):
    """Button to trigger an immediate 'Locate now' request (manual location update)."""

    entity_description = LOCATE_DEVICE_DESCRIPTION
    log_prefix = "Locate"

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
        fallback_label: str | None,
        *,
        subentry_key: str,
        subentry_identifier: str,
    ) -> None:
        super().__init__(
            coordinator,
            device,
            fallback_label,
            subentry_key=subentry_key,
            subentry_identifier=subentry_identifier,
        )
        dev_id = self.device_id
        self._attr_unique_id = self.build_unique_id(
            DOMAIN,
            self.entry_id,
            subentry_identifier,
            dev_id,
            "locate_device",
            separator="_",
        )

    @property
    def available(self) -> bool:
        """Return True only if the device is present AND a manual locate is currently allowed.

        Presence has priority: if absent from Google's list, the button is unavailable.
        """
        dev_id = self.device_id
        device_label = self.device_label()
        try:
            # Presence gate
            if not self.coordinator_has_device():
                return False
            if hasattr(
                self.coordinator, "is_device_present"
            ) and not self.coordinator.is_device_present(dev_id):
                return False
            # Locate gating
            return bool(self.coordinator.can_request_location(dev_id))
        except (AttributeError, TypeError) as err:
            _LOGGER.debug(
                "Locate availability check for %s (%s) raised %s; defaulting to True",
                device_label,
                dev_id,
                err,
            )
            return True  # Optimistic fallback

    async def async_press(self) -> None:
        """Invoke the `googlefindmy.locate_device` service for this device.

        The service path keeps UI and logic decoupled and ensures that all
        manual triggers (buttons, automations, scripts) share the same code path.
        """
        device_id = self.device_id
        device_name = self.device_label()

        if not self.available:
            _LOGGER.warning(
                "Locate now not available for %s (%s) — push not ready, in-flight/cooldown, or absent",
                device_name,
                device_id,
            )
            return

        _LOGGER.debug("Locate now: attempting on %s (%s)", device_name, device_id)
        try:
            # Fire-and-forget for responsive UI; coordinator handles gating & updates
            await self.hass.services.async_call(
                DOMAIN,
                SERVICE_LOCATE_DEVICE,
                {"device_id": device_id},
                blocking=False,  # non-blocking: avoid UI stall
            )
            _LOGGER.info("Successfully submitted manual locate for %s", device_name)
        except Exception as err:  # Avoid crashing the update loop
            _LOGGER.error("Error submitting manual locate for %s: %s", device_name, err)
