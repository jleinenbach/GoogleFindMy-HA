"""Button platform for Google Find My Device."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device button entities.

    Design goals:
    - Create Play Sound buttons for devices available at setup time.
    - Dynamically add buttons for devices that appear later (post-initial refresh),
      guarded by a known_ids set to avoid duplicates.
    - Do NOT create skeleton buttons for unknown devices (buttons do not benefit
      from Restore like sensors/trackers do).
    """
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    known_ids: set[str] = set()
    entities: list[GoogleFindMyPlaySoundButton] = []

    # Initial population from coordinator.data (if already available)
    for device in (coordinator.data or []):
        dev_id = device.get("id")
        name = device.get("name")
        if dev_id and name and dev_id not in known_ids:
            entities.append(GoogleFindMyPlaySoundButton(coordinator, device))
            known_ids.add(dev_id)

    # Add initial entities and write state immediately
    if entities:
        _LOGGER.debug("Adding %d initial Play Sound button(s)", len(entities))
        async_add_entities(entities, True)

    # Dynamically add buttons when new devices appear later
    @callback
    def _add_new_devices() -> None:
        new_entities: list[GoogleFindMyPlaySoundButton] = []
        for device in (coordinator.data or []):
            dev_id = device.get("id")
            name = device.get("name")
            if dev_id and name and dev_id not in known_ids:
                new_entities.append(GoogleFindMyPlaySoundButton(coordinator, device))
                known_ids.add(dev_id)

        if new_entities:
            _LOGGER.debug("Dynamically adding %d Play Sound button(s)", len(new_entities))
            async_add_entities(new_entities, True)

    # Listen for coordinator updates and try to add any new devices
    unsub = coordinator.async_add_listener(_add_new_devices)
    config_entry.async_on_unload(unsub)


class GoogleFindMyPlaySoundButton(CoordinatorEntity, ButtonEntity):
    """Button to trigger 'Play Sound' on a Google Find My Device."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:volume-high"

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: dict[str, Any],
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._device = device
        dev_id = device["id"]
        name = device["name"]
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_play_sound"
        self._attr_name = f"{name} Play Sound"

    # ---------------- Availability ----------------
    @property
    def available(self) -> bool:
        """Button is available only if the coordinator allows Play Sound.

        Primary source of truth:
        - coordinator.can_play_sound(device_id) -> bool | None
          (None = unknown yet)

        Backward compatibility:
        - If the coordinator doesn't have this method yet (older versions),
          default to True (optimistic), so the button is not incorrectly greyed out.
        """
        dev_id = self._device["id"]
        can_play = getattr(self.coordinator, "can_play_sound", None)

        if callable(can_play):
            try:
                verdict = can_play(dev_id)  # may be True/False/None
                _LOGGER.debug(
                    "PlaySound availability for %s (%s): can_play_sound -> %r",
                    self._device.get("name", dev_id),
                    dev_id,
                    verdict,
                )
                if verdict is None:
                    return True  # optimistic while capability is unknown
                return bool(verdict)
            except Exception as err:
                _LOGGER.debug(
                    "PlaySound availability check for %s (%s) raised %s; defaulting to True",
                    self._device.get("name", dev_id),
                    dev_id,
                    err,
                )
                return True

        _LOGGER.debug(
            "PlaySound availability for %s (%s): legacy coordinator (no can_play_sound) -> default True",
            self._device.get("name", dev_id),
            dev_id,
        )
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """React to coordinator updates (availability may change)."""
        _LOGGER.debug("Coordinator update received for %s", self._attr_unique_id)
        self.async_write_ha_state()

    # ---------------- Device Info + Map Link ----------------
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info with a configuration_url to the map view.

        The base URL is resolved via get_url(...) with prefer_external=True so
        the link also works when the UI is opened through Nabu Casa Cloud or
        an external URL. We then append the relative path built by
        _build_map_path(...).

        Runs outside an HTTP request; we intentionally do NOT require the
        current request context. Home Assistant will choose a stable base URL
        according to the configured internal/external/cloud URLs.
        """
        from homeassistant.helpers.network import get_url

        try:
            base_url = get_url(
                self.hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except Exception:
            base_url = "http://homeassistant.local:8123"

        auth_token = self._get_map_token()
        path = self._build_map_path(self._device["id"], auth_token, redirect=False)

        return {
            "identifiers": {(DOMAIN, self._device["id"])},
            "name": self._device["name"],
            "manufacturer": "Google",
            "model": "Find My Device",
            "configuration_url": f"{base_url}{path}",
            "hw_version": self._device["id"],
        }

    def _build_map_path(self, device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the map URL *path* (no scheme/host).

        Keep the path construction here so we can switch to redirect endpoints
        without touching base URL resolution elsewhere.
        """
        if redirect:
            return f"/api/googlefindmy/redirect_map/{device_id}?token={token}"
        return f"/api/googlefindmy/map/{device_id}?token={token}"

    def _get_map_token(self) -> str:
        """Generate a simple map token (options-first; weekly/static).

        - Prefer config_entry.options over entry.data to reflect recent option changes.
        - Weekly-rotating token when enabled; otherwise static token.
        """
        import hashlib
        import time
        from .const import DEFAULT_MAP_VIEW_TOKEN_EXPIRATION

        config_entry = getattr(self.coordinator, "config_entry", None)
        if config_entry:
            token_expiration_enabled = config_entry.options.get(
                "map_view_token_expiration",
                config_entry.data.get("map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION),
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
        """Handle the button press.

        We perform a pre-check using availability to avoid hitting the API
        when Push/FCM is not ready or the device isn't ring-capable.
        """
        device_id = self._device["id"]
        device_name = self._device["name"]

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
                _LOGGER.info("Successfully played sound on %s", device_name)
            else:
                _LOGGER.warning("Failed to play sound on %s", device_name)
        except Exception as err:
            _LOGGER.error("Error playing sound on %s: %s", device_name, err)
