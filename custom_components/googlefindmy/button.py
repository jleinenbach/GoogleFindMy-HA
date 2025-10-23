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
import time
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    entity_platform,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.update_coordinator import CoordinatorEntity
import voluptuous as vol

from .const import (
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DOMAIN,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,  # <-- use the constant (C2)
    SERVICE_LOCATE_DEVICE,
    map_token_hex_digest,
    map_token_secret_seed,
    service_device_identifier,
)
from .coordinator import GoogleFindMyCoordinator
from .util_services import register_entity_service

_LOGGER = logging.getLogger(__name__)

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


def _maybe_update_device_registry_name(
    hass: HomeAssistant, entity_id: str, new_name: str
) -> None:
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
    runtime = getattr(config_entry, "runtime_data", None)
    coordinator: GoogleFindMyCoordinator | None = None
    if isinstance(runtime, GoogleFindMyCoordinator):
        coordinator = runtime
    elif runtime is not None:
        coordinator = getattr(runtime, "coordinator", None)

    if not isinstance(coordinator, GoogleFindMyCoordinator):
        raise HomeAssistantError("googlefindmy coordinator not ready")

    platform_getter = getattr(entity_platform, "async_get_current_platform", None)
    if callable(platform_getter):
        platform = platform_getter()
        register_entity_service(
            platform,
            "trigger_device_refresh",
            vol.Schema({}),
            "async_trigger_coordinator_refresh",
        )

    known_ids: set[str] = set()
    entities: list[ButtonEntity] = []

    # Initial population from coordinator.data (if already available)
    for device in coordinator.data or []:
        dev_id = device.get("id")
        name = device.get("name")
        if dev_id and name and dev_id not in known_ids:
            entities.append(GoogleFindMyPlaySoundButton(coordinator, device))
            entities.append(GoogleFindMyStopSoundButton(coordinator, device))
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
                new_entities.append(GoogleFindMyStopSoundButton(coordinator, device))
                new_entities.append(GoogleFindMyLocateButton(coordinator, device))
                known_ids.add(dev_id)

        if new_entities:
            _LOGGER.debug("Dynamically adding %d button entity(ies)", len(new_entities))
            async_add_entities(new_entities, True)

    unsub = coordinator.async_add_listener(_add_new_devices)
    config_entry.async_on_unload(unsub)


# ----------------------------- Base class -----------------------------------
class _BaseGoogleFindMyButton(CoordinatorEntity, ButtonEntity):
    """Common helpers for all per-device buttons.

    Centralizes:
    - unique_id namespacing with config_entry.entry_id (multi-account safe),
    - DeviceInfo construction with `via_device` linking to the per-entry service device,
    - map token / url building,
    - label refresh on coordinator updates.
    """

    # Entities are enabled by default; device-level disabling is handled elsewhere
    _attr_entity_registry_enabled_default = True
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]
    ) -> None:
        super().__init__(coordinator)
        self._device = device

    async def async_trigger_coordinator_refresh(self) -> None:
        """Request a coordinator refresh via the entity service placeholder."""

        await self.coordinator.async_request_refresh()

    # --------------- Common name refresh on coordinator update ---------------
    @callback
    def _refresh_label_from_coordinator(self, log_prefix: str) -> None:
        """Sync raw device label from coordinator snapshot and update DR if needed."""
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
                        _maybe_update_device_registry_name(
                            self.hass, self.entity_id, new_name
                        )
                        _LOGGER.debug(
                            "%s device label refreshed for %s: '%s' -> '%s'",
                            log_prefix,
                            my_id,
                            old,
                            new_name,
                        )
                    break
        except (AttributeError, TypeError):
            pass

    # ---------------- Device Info + Map Link (shared) ----------------
    @property
    def _entry_id(self) -> str | None:
        """Return entry_id if the entity is bound to a config entry."""
        entry = getattr(self.coordinator, "config_entry", None)
        return getattr(entry, "entry_id", None)

    def _service_device_identifier(self) -> tuple[str, str] | None:
        """Return the `(DOMAIN, f"integration_{entry_id}")` identifier for via_device."""
        eid = self._entry_id
        if not eid:
            return None
        return service_device_identifier(eid)

    @property
    def device_info(self) -> DeviceInfo:
        """Return DeviceInfo for the **end device**, linked via the service device.

        Notes:
        - `identifiers` uses the canonical per-device id, so entities group under that device.
        - `via_device` links the end device to the single per-entry service device to prevent
          duplicate "integration devices" and to keep topology clean.
        - We avoid writing placeholder names on cold boot; only pass `name` when known.
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

        raw_name = (self._device.get("name") or "").strip()
        use_name = (
            raw_name if raw_name and raw_name != "Google Find My Device" else None
        )

        info_kwargs: dict[str, Any] = {}
        if use_name:
            info_kwargs["name"] = use_name

        via = self._service_device_identifier()
        entry_id = self._entry_id
        dev_id = self._device["id"]
        entry_scoped_identifier = f"{entry_id}:{dev_id}" if entry_id else dev_id

        return DeviceInfo(
            identifiers={(DOMAIN, entry_scoped_identifier)},
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}",
            serial_number=self._device["id"],
            via_device=via,  # <-- link to the per-entry service device
            **info_kwargs,
        )

    @staticmethod
    def _build_map_path(device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host)."""
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a hardened map token (entry-scoped + weekly/static).

        Token formula (kept consistent with other platforms & map_view):
            secret = map_token_secret_seed(...)
            token  = map_token_hex_digest(secret)
        """
        config_entry = getattr(self.coordinator, "config_entry", None)

        # Prefer central options helper if available; fall back to direct reads.
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
        ha_uuid = str(self.hass.data.get("core.uuid", "ha"))
        if token_expiration_enabled:
            seed = map_token_secret_seed(ha_uuid, entry_id, True, now=int(time.time()))
        else:
            seed = map_token_secret_seed(ha_uuid, entry_id, False)

        return map_token_hex_digest(seed)


# ----------------------------- Play Sound -----------------------------------
class GoogleFindMyPlaySoundButton(_BaseGoogleFindMyButton):
    """Button to trigger 'Play Sound' on a Google Find My Device."""

    entity_description = PLAY_SOUND_DESCRIPTION

    def __init__(
        self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, device)
        dev_id = device["id"]
        eid = self._entry_id
        # Unique ID is namespaced by entry_id for multi-account safety
        if eid:
            self._attr_unique_id = f"{DOMAIN}_{eid}_{dev_id}_play_sound"
        else:
            self._attr_unique_id = f"{DOMAIN}_{dev_id}_play_sound"

    @property
    def available(self) -> bool:
        """Return True only if the device is present AND can likely play a sound.

        Presence has priority: if the device is absent from the latest Google list,
        the button is unavailable regardless of capability/push readiness.
        """
        dev_id = self._device["id"]
        try:
            # Presence gate
            if hasattr(
                self.coordinator, "is_device_present"
            ) and not self.coordinator.is_device_present(dev_id):
                return False
            # Capability / push readiness gate
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
        self._refresh_label_from_coordinator("PlaySound")
        self.async_write_ha_state()

    async def async_press(self) -> None:
        """Handle the button press."""
        device_id = self._device["id"]
        device_name = self._device.get("name", device_id)

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
class GoogleFindMyStopSoundButton(_BaseGoogleFindMyButton):
    """Button to trigger 'Stop Sound' on a Google Find My Device."""

    entity_description = STOP_SOUND_DESCRIPTION

    def __init__(
        self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, device)
        dev_id = device["id"]
        eid = self._entry_id
        if eid:
            self._attr_unique_id = f"{DOMAIN}_{eid}_{dev_id}_stop_sound"
        else:
            self._attr_unique_id = f"{DOMAIN}_{dev_id}_stop_sound"

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
        dev_id = self._device["id"]
        try:
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
                self._device.get("name", dev_id),
                dev_id,
                err,
            )
            return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator updates (availability and device name may change)."""
        self._refresh_label_from_coordinator("StopSound")
        self.async_write_ha_state()

    async def async_press(self) -> None:
        """Handle the button press (stop sound)."""
        device_id = self._device["id"]
        device_name = self._device.get("name", device_id)

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
class GoogleFindMyLocateButton(_BaseGoogleFindMyButton):
    """Button to trigger an immediate 'Locate now' request (manual location update)."""

    entity_description = LOCATE_DEVICE_DESCRIPTION

    def __init__(
        self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]
    ) -> None:
        super().__init__(coordinator, device)
        dev_id = device["id"]
        eid = self._entry_id
        if eid:
            self._attr_unique_id = f"{DOMAIN}_{eid}_{dev_id}_locate_device"
        else:
            self._attr_unique_id = f"{DOMAIN}_{dev_id}_locate_device"

    @property
    def available(self) -> bool:
        """Return True only if the device is present AND a manual locate is currently allowed.

        Presence has priority: if absent from Google's list, the button is unavailable.
        """
        dev_id = self._device["id"]
        try:
            # Presence gate
            if hasattr(
                self.coordinator, "is_device_present"
            ) and not self.coordinator.is_device_present(dev_id):
                return False
            # Locate gating
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
        self._refresh_label_from_coordinator("Locate")
        self.async_write_ha_state()

    async def async_press(self) -> None:
        """Invoke the `googlefindmy.locate_device` service for this device.

        The service path keeps UI and logic decoupled and ensures that all
        manual triggers (buttons, automations, scripts) share the same code path.
        """
        device_id = self._device["id"]
        device_name = self._device.get("name", device_id)

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
