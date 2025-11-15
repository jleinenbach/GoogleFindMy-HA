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
* Tracker entities rely on per-device identifiers managed by the coordinator;
  do not link them to the service device via manual `via_device` tuples.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from homeassistant.components.button import ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    SERVICE_LOCATE_DEVICE,
    TRACKER_SUBENTRY_KEY,
)
from . import EntityRecoveryManager
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
    config_subentry_id: str | None = None,
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
                None,
                "async_trigger_coordinator_refresh",
            )

    tracker_meta = coordinator.get_subentry_metadata(feature="button")
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
    tracker_config_subentry_id = config_subentry_id or tracker_meta_config_id

    _LOGGER.debug(
        "Button setup: subentry_key=%s, config_subentry_id=%s",
        tracker_subentry_key,
        tracker_config_subentry_id,
    )

    if (
        config_subentry_id
        and tracker_meta_config_id
        and config_subentry_id != tracker_meta_config_id
    ):
        _LOGGER.debug(
            "Button setup ignored for unrelated subentry '%s' (expected '%s')",
            config_subentry_id,
            tracker_meta_config_id,
        )
        return

    known_ids: set[str] = set()
    entities: list[ButtonEntity] = []

    def _schedule_button_entities(
        new_entities: Iterable[ButtonEntity],
        update_before_add: bool = True,
    ) -> None:
        entity_list = list(new_entities)
        if not entity_list:
            return
        try:
            async_add_entities(
                entity_list,
                update_before_add=update_before_add,
                config_subentry_id=tracker_config_subentry_id,
            )
        except TypeError as err:
            if "config_subentry_id" not in str(err):
                raise
            _LOGGER.debug(
                "Button setup: AddEntitiesCallback rejected config_subentry_id; retrying without (error=%s)",
                err,
            )
            async_add_entities(entity_list, update_before_add=update_before_add)

    # Initial population from coordinator.data (if already available)
    for device in coordinator.get_subentry_snapshot(tracker_subentry_key):
        dev_id = device.get("id")
        if not dev_id or dev_id in known_ids:
            continue

        label = _derive_device_label(device)
        entities.append(
            GoogleFindMyPlaySoundButton(
                coordinator,
                device,
                label,
                subentry_key=tracker_subentry_key,
                subentry_identifier=tracker_subentry_identifier,
            )
        )
        entities.append(
            GoogleFindMyStopSoundButton(
                coordinator,
                device,
                label,
                subentry_key=tracker_subentry_key,
                subentry_identifier=tracker_subentry_identifier,
            )
        )
        entities.append(
            GoogleFindMyLocateButton(
                coordinator,
                device,
                label,
                subentry_key=tracker_subentry_key,
                subentry_identifier=tracker_subentry_identifier,
            )
        )
        known_ids.add(dev_id)

    if entities:
        _LOGGER.debug("Adding %d initial button entity(ies)", len(entities))
        _schedule_button_entities(entities, True)

    # Dynamically add buttons when new devices appear later
    @callback
    def _add_new_devices() -> None:
        new_entities: list[ButtonEntity] = []
        for device in coordinator.get_subentry_snapshot(tracker_subentry_key):
            dev_id = device.get("id") if isinstance(device, dict) else None
            if not dev_id:
                continue

            if dev_id in known_ids:
                _LOGGER.debug("Button setup: Skipping known dev_id '%s'", dev_id)
                continue

            label = _derive_device_label(device)
            _LOGGER.debug(
                "Button setup: Adding new buttons for dev_id '%s' (label=%s)",
                dev_id,
                label,
            )
            new_entities.append(
                GoogleFindMyPlaySoundButton(
                    coordinator,
                    device,
                    label,
                    subentry_key=tracker_subentry_key,
                    subentry_identifier=tracker_subentry_identifier,
                )
            )
            new_entities.append(
                GoogleFindMyStopSoundButton(
                    coordinator,
                    device,
                    label,
                    subentry_key=tracker_subentry_key,
                    subentry_identifier=tracker_subentry_identifier,
                )
            )
            new_entities.append(
                GoogleFindMyLocateButton(
                    coordinator,
                    device,
                    label,
                    subentry_key=tracker_subentry_key,
                    subentry_identifier=tracker_subentry_identifier,
                )
            )
            known_ids.add(dev_id)

        if new_entities:
            _LOGGER.debug("Dynamically adding %d button entity(ies)", len(new_entities))
            _schedule_button_entities(new_entities, True)

    unsub = coordinator.async_add_listener(_add_new_devices)
    config_entry.async_on_unload(unsub)

    runtime_data = getattr(config_entry, "runtime_data", None)
    recovery_manager = getattr(runtime_data, "entity_recovery_manager", None)

    if isinstance(recovery_manager, EntityRecoveryManager):
        entry_id = getattr(config_entry, "entry_id", None)

        def _is_visible(device_id: str) -> bool:
            try:
                return bool(
                    coordinator.is_device_visible_in_subentry(
                        tracker_subentry_key, device_id
                    )
                )
            except Exception:  # pragma: no cover - defensive best effort
                return True

        actions: dict[str, type[GoogleFindMyButtonEntity]] = {
            "play_sound": GoogleFindMyPlaySoundButton,
            "stop_sound": GoogleFindMyStopSoundButton,
            "locate_device": GoogleFindMyLocateButton,
        }

        def _expected_unique_ids() -> set[str]:
            if not isinstance(entry_id, str) or not entry_id:
                return set()
            if not isinstance(tracker_subentry_identifier, str) or not tracker_subentry_identifier:
                return set()
            expected: set[str] = set()
            for device in coordinator.get_subentry_snapshot(tracker_subentry_key):
                dev_id = device.get("id")
                if not isinstance(dev_id, str) or not dev_id:
                    continue
                if not _is_visible(dev_id):
                    continue
                for action in actions:
                    expected.add(
                        f"{DOMAIN}_{entry_id}_{tracker_subentry_identifier}_{dev_id}_{action}"
                    )
            return expected

        def _build_entities(missing: set[str]) -> list[ButtonEntity]:
            if not missing:
                return []
            built: list[ButtonEntity] = []
            if not isinstance(entry_id, str) or not entry_id:
                return built
            if not isinstance(tracker_subentry_identifier, str) or not tracker_subentry_identifier:
                return built
            snapshot = coordinator.get_subentry_snapshot(tracker_subentry_key)
            for device in snapshot:
                dev_id = device.get("id")
                if not isinstance(dev_id, str) or not dev_id:
                    continue
                if not _is_visible(dev_id):
                    continue
                label = _derive_device_label(device)
                for action, entity_cls in actions.items():
                    unique_id = (
                        f"{DOMAIN}_{entry_id}_{tracker_subentry_identifier}_{dev_id}_{action}"
                    )
                    if unique_id not in missing:
                        continue
                    built.append(
                        entity_cls(
                            coordinator,
                            device,
                            label,
                            subentry_key=tracker_subentry_key,
                            subentry_identifier=tracker_subentry_identifier,
                        )
                    )
            return built

        recovery_manager.register_button_platform(
            expected_unique_ids=_expected_unique_ids,
            entity_factory=_build_entities,
            add_entities=_schedule_button_entities,
        )


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
