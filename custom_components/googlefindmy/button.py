"""Button platform for Google Find My Device."""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Optional

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEFAULT_MAP_VIEW_TOKEN_EXPIRATION, DOMAIN
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)

# A single, reusable entity description is efficient and allows for translation.
PLAY_SOUND_DESCRIPTION = ButtonEntityDescription(
    key="play_sound",
    translation_key="play_sound",  # Links to strings.json for the entity name
    icon="mdi:volume-high",
)

# Placeholder used during early boot; never prefix this into a final display name.
_PLACEHOLDER_NAME = "Google Find My Device"


def _display_name(raw: Optional[str]) -> Optional[str]:
    """Return the final entity display name or None if we should defer.

    Important rules:
    - Only build a composite name when a *real* device label is available.
    - Never use the placeholder text to avoid duplicate names across entities.
    """
    base = (raw or "").strip()
    if not base or base == _PLACEHOLDER_NAME:
        return None
    return f"Find My – {base} • Play Sound"


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
    - Do NOT create skeleton buttons for unknown devices, as buttons do not benefit
      from RestoreEntity like sensors or trackers do.
    """
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][config_entry.entry_id]

    known_ids: set[str] = set()
    entities: list[GoogleFindMyPlaySoundButton] = []

    # Initial population from coordinator.data (if already available)
    for device in coordinator.data or []:
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
        """Add entities for newly discovered devices."""
        new_entities: list[GoogleFindMyPlaySoundButton] = []
        for device in coordinator.data or []:
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

    # We provide a full display name ourselves; keep this False to avoid HA composing names.
    _attr_has_entity_name = False
    _attr_should_poll = False  # explicit: buttons are event-driven
    entity_description = PLAY_SOUND_DESCRIPTION

    def __init__(self, coordinator: GoogleFindMyCoordinator, device: dict[str, Any]) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._device = device
        dev_id = device["id"]
        self._attr_unique_id = f"{DOMAIN}_{dev_id}_play_sound"

        # Set an initial display name only if we have a real device label.
        dn = _display_name(device.get("name"))
        if dn:
            self._attr_name = dn
        # else: leave name unset for now; UI will use the entity_id temporarily.
        # We will resync the final name once the coordinator delivers the real label.

    # ---------------- Availability ----------------
    @property
    def available(self) -> bool:
        """Derive availability from coordinator.can_play_sound().

        Optimistic UX: if capability is unknown internally, the coordinator returns True,
        so the button remains usable while the API enforces reality.
        """
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
        # Sync the device label and, if it became real, update the entity display name.
        try:
            data = getattr(self.coordinator, "data", None) or []
            my_id = self._device["id"]
            for dev in data:
                if dev.get("id") == my_id:
                    new_label = dev.get("name")
                    old_label = self._device.get("name")
                    if new_label and new_label != old_label:
                        self._device["name"] = new_label
                        # Recompute display name with guard against the placeholder.
                        dn = _display_name(new_label)
                        if dn and self._attr_name != dn:
                            _LOGGER.debug(
                                "Updating button name for %s: '%s' -> '%s'",
                                my_id,
                                self._attr_name,
                                dn,
                            )
                            self._attr_name = dn
                    break
        except (AttributeError, TypeError):
            # Non-critical; keep going.
            pass

        self.async_write_ha_state()

    # ---------------- Device Info + Map Link ----------------
    @property
    def device_info(self) -> DeviceInfo:
        """Return DeviceInfo with a stable configuration_url and proper metadata."""
        try:
            base_url = get_url(
                self.hass,
                prefer_external=True,  # also works from remote/cloud
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except HomeAssistantError:
            base_url = "http://homeassistant.local:8123"

        auth_token = self._get_map_token()
        path = self._build_map_path(self._device["id"], auth_token, redirect=False)

        return DeviceInfo(
            identifiers={(DOMAIN, self._device["id"])},
            # Device label stays the raw device name (no "Find My –" prefix) for registry clarity.
            name=self._device.get("name"),
            manufacturer="Google",
            model="Find My Device",
            configuration_url=f"{base_url}{path}",
            # Expose the technical ID in the semantically correct field.
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
        """Handle the button press.

        Pre-check via availability avoids hitting the API when push/FCM is not ready
        or the device isn't ring-capable.
        """
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
                _LOGGER.warning("Failed to play sound on %s (request may have been rejected)", device_name)
        except Exception as err:
            _LOGGER.error("Error playing sound on %s: %s", device_name, err)
