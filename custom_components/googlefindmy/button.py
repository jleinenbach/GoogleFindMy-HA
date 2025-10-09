# custom_components/googlefindmy/button.py
"""Button platform for Google Find My Device."""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import DEFAULT_MAP_VIEW_TOKEN_EXPIRATION, DOMAIN, SERVICE_LOCATE_DEVICE
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)

# Reusable entity description with translations in strings.json
PLAY_SOUND_DESCRIPTION = ButtonEntityDescription(
    key="play_sound",
    translation_key="play_sound",
    icon="mdi:volume-high",
)

# New entity description for the manual "Locate now" action
LOCATE_DEVICE_DESCRIPTION = ButtonEntityDescription(
    key="locate_device",
    translation_key="locate_device",
    icon="mdi:radar",
)


def _maybe_update_device_registry_name(hass: HomeAssistant, entity_id: str, new_name: str) -> None:
    """Update the device's name in the registry if the user hasn't overridden it.

    Best practice:
    - Do not touch user-defined names (name_by_user).
    - Keep device registry name aligned with the upstream device label so that
    entity names composed via has_entity_name=True stay current.
    """
    try:
        ent_reg = er.async_get(hass)
        ent = ent_reg.async_get(entity_id)
        if not ent or not ent.device_id:
            return
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get(ent.device_id)
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
    except Exception as e:  # Avoid noisy errors during boot/races
        _LOGGER.debug("Device registry name update failed for %s: %s", entity_id, e)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device button entities."""
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    known_ids: set[str] = set()
    entities: list[ButtonEntity] = []

    # Initial population from coordinator.data (if already available)
    for device in coordinator.data or []:
        dev_id = device.get("id")
        name = device.get("name")
        if dev_id and name and dev_id not in known_ids:
            entities.append(GoogleFindMyPlaySoundButton(coordinator, device))
            entities.append(GoogleFindMyLocateButton(coordinator, device))
            known_ids.add(dev_id)

    if entities:
        _LOGGER.debug("Adding %d initial button entity(ies)", len(entities))
        async_add_entities(entities, True)

    # Dynamically add buttons when new devices appear later
    @callback
    def _add_new_devices() -> None:
        new_entities: list[ButtonEntity] = []
        for device in coordinator.data or []:
            dev_id = device.get("id")
            name = device.get("name")
            if dev_id and name and dev_id not in known_ids:
                new_entities.append(GoogleFindMyPlaySoundButton(coordinator, device))
                new_entities.append(GoogleFindMyLocateButton(coordinator, device))
                known_ids.add(dev_id)

        if new_entities:
            _LOGGER.debug("Dynamically adding %d button entity(ies)", len(new_entities))
            async_add_entities(new_entities, True)

    unsub = coordinator.async_add_listener(_add_new_devices)
    config_entry.async_on_unload(unsub)


class GoogleFindMyPlaySoundButton(CoordinatorEntity, ButtonEntity):
    """Button to trigger 'Play Sound' on a Google Find My Device."""

    # Best practice: let HA compose "<Device Name> <translated entity name>"
    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description = PLAY_SOUND_DESCRIPTION

    def __init__(self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._device = device
        dev_id = device["id"]
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_play_sound"
        # Do not set _attr_name: with has_entity_name=True the UI composes the name automatically.

    # ---------------- Availability ----------------
    @property
    def available(self) -> bool:
        """Derive availability from coordinator.can_play_sound()."""
        dev_id = self._device["id"]
        try:
            return self.coordinator.can_play_sound(dev_id)
        except (AttributeError, TypeError) as err:
            _LOGGER.debug(
                "PlaySound availability check for %s (%s) raised %s; defaulting to True",
                self._device.get("name", dev_id),
                dev_id,
                err,
            )
            return True  # Optimistic fallback

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator updates (availability and device name may change)."""
        # Keep the raw device name in sync and update device registry if needed.
        try:
            data = getattr(self.coordinator, "data", None) or []
            my_id = self._device["id"]
            for dev in data:
                if dev.get("id") == my_id:
                    new_name = dev.get("name")
                    # Never write bootstrap placeholders into the registry
                    if (
                        new_name
                        and new_name != "Google Find My Device"
                        and new_name != self._device.get("name")
                    ):
                        old = self._device.get("name")
                        self._device["name"] = new_name
                        _maybe_update_device_registry_name(self.hass, self.entity_id, new_name)
                        _LOGGER.debug(
                            "Button device label refreshed for %s: '%s' -> '%s'",
                            my_id,
                            old,
                            new_name,
                        )
                    break
        except (AttributeError, TypeError):
            pass

        self.async_write_ha_state()

    # ---------------- Device Info + Map Link ----------------
    @property
    def device_info(self) -> DeviceInfo:
        """Return DeviceInfo with a stable configuration_url and safe naming.

        IMPORTANT: Do not mix `default_*` keys with `identifiers`. Always provide
        a concrete `name` so the info cleanly matches the 'Primary' device-info category.
        """
        try:
            base_url = get_url(
                self.hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except HomeAssistantError:
            base_url = "http://homeassistant.local:8123"

        auth_token = self._get_map_token()
        path = self._build_map_path(self._device["id"], auth_token, redirect=False)

        # Always provide a concrete `name` (no default_name) to satisfy device-info validation.
        raw_name = (self._device.get("name") or "").strip()
        safe_name = raw_name if raw_name else "Google Find My Device"

        return DeviceInfo(
            identifiers={(DOMAIN, self._device["id"])},
            name=safe_name,
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}",
            serial_number=self._device["id"],  # technical id in the proper field
        )

    @staticmethod
    def _build_map_path(device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host)."""
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a simple map token (options-first; weekly/static)."""
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
            week = str(int(time.time() // 604800))  # 7-day bucket
            token_src = f"{ha_uuid}:{week}"
        else:
            token_src = f"{ha_uuid}:static"

        return hashlib.md5(token_src.encode()).hexdigest()[:16]

    # ---------------- Action ----------------
    async def async_press(self) -> None:
        """Handle the button press."""
        device_id = self._device["id"]
        device_name = self._device.get("name", device_id)

        if not self.available:
            _LOGGER.warning(
                "Play Sound not available for %s (%s) — push not ready or device not capable",
                device_name,
                device_id,
            )
            return

        _LOGGER.debug("Play Sound: attempting on %s (%s)", device_name, device_id)
        try:
            result = await self.coordinator.async_play_sound(device_id)
            if result:
                _LOGGER.info("Successfully submitted Play Sound request for %s", device_name)
            else:
                _LOGGER.warning(
                    "Failed to play sound on %s (request may have been rejected)",
                    device_name,
                )
        except Exception as err:  # Avoid crashing the update loop
            _LOGGER.error("Error playing sound on %s: %s", device_name, err)


class GoogleFindMyLocateButton(CoordinatorEntity, ButtonEntity):
    """Button to trigger an immediate 'Locate now' request (manual location update)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description = LOCATE_DEVICE_DESCRIPTION

    def __init__(self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]) -> None:
        """Initialize the locate button entity."""
        super().__init__(coordinator)
        self._device = device
        dev_id = device["id"]
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_locate_device"

    # ---------------- Availability ----------------
    @property
    def available(self) -> bool:
        """Derive availability from coordinator.can_request_location()."""
        dev_id = self._device["id"]
        try:
            return self.coordinator.can_request_location(dev_id)
        except (AttributeError, TypeError) as err:
            _LOGGER.debug(
                "Locate availability check for %s (%s) raised %s; defaulting to True",
                self._device.get("name", dev_id),
                dev_id,
                err,
            )
            return True  # Optimistic fallback

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator updates (availability and device name may change)."""
        try:
            data = getattr(self.coordinator, "data", None) or []
            my_id = self._device["id"]
            for dev in data:
                if dev.get("id") == my_id:
                    new_name = dev.get("name")
                    if (
                        new_name
                        and new_name != "Google Find My Device"
                        and new_name != self._device.get("name")
                    ):
                        old = self._device.get("name")
                        self._device["name"] = new_name
                        _maybe_update_device_registry_name(self.hass, self.entity_id, new_name)
                        _LOGGER.debug(
                            "Locate button device label refreshed for %s: '%s' -> '%s'",
                            my_id,
                            old,
                            new_name,
                        )
                    break
        except (AttributeError, TypeError):
            pass

        self.async_write_ha_state()

    # ---------------- Device Info + Map Link ----------------
    @property
    def device_info(self) -> DeviceInfo:
        """Return DeviceInfo identical to Play Sound for consistent grouping."""
        try:
            base_url = get_url(
                self.hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except HomeAssistantError:
            base_url = "http://homeassistant.local:8123"

        auth_token = self._get_map_token()
        path = self._build_map_path(self._device["id"], auth_token, redirect=False)

        raw_name = (self._device.get("name") or "").strip()
        safe_name = raw_name if raw_name else "Google Find My Device"

        return DeviceInfo(
            identifiers={(DOMAIN, self._device["id"])},
            name=safe_name,
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}",
            serial_number=self._device["id"],
        )

    @staticmethod
    def _build_map_path(device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host)."""
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a simple map token (options-first; weekly/static)."""
        config_entry = getattr(self.coordinator, "config_entry", None)
        if config_entry:
            from . import _opt  # lazy import to avoid HA startup overhead

            token_expiration_enabled = _opt(
                config_entry, "map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            )
        else:
            token_expiration_enabled = DEFAULT_MAP_VIEW_TOKEN_EXPIRATION

        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))
        if token_expiration_enabled:
            week = str(int(time.time() // 604800))  # 7-day bucket
            token_src = f"{ha_uuid}:{week}"
        else:
            token_src = f"{ha_uuid}:static"

        return hashlib.md5(token_src.encode()).hexdigest()[:16]

    # ---------------- Action ----------------
    async def async_press(self) -> None:
        """Invoke the `googlefindmy.locate_device` service for this device.

        The service path keeps UI and logic decoupled and ensures that all
        manual triggers (buttons, automations, scripts) share the same code path.
        """
        device_id = self._device["id"]
        device_name = self._device.get("name", device_id)

        if not self.available:
            _LOGGER.warning(
                "Locate now not available for %s (%s) — push not ready, in-flight or cooldown",
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
                blocking=False,  # <-- non-blocking: avoid UI stall
            )
            _LOGGER.info("Successfully submitted manual locate for %s", device_name)
        except Exception as err:  # Avoid crashing the update loop
            _LOGGER.error("Error submitting manual locate for %s: %s", device_name, err)
