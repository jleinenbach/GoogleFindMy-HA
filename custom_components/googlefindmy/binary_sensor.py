"""Binary sensor entities for Google Find My Device integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)

POLLING_DESC = BinarySensorEntityDescription(
    key="polling",
    translation_key="polling",
    icon="mdi:refresh",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device binary sensor entities.

    We expose a single diagnostic sensor that reflects whether a polling cycle
    is currently in progress. This is helpful for troubleshooting.
    """
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[GoogleFindMyPollingSensor] = [GoogleFindMyPollingSensor(coordinator)]

    # Write state immediately so the dashboard reflects the current status
    async_add_entities(entities, True)


class GoogleFindMyPollingSensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating whether background polling is active."""

    _attr_has_entity_name = True  # Compose "<Device Name> <Entity Name>"
    _attr_name = "Polling"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = POLLING_DESC

    def __init__(self, coordinator: GoogleFindMyCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_polling"

    @property
    def is_on(self) -> bool:
        """Return True if a polling cycle is currently running.

        Prefer the public read-only property 'is_polling' (new Coordinator API).
        Fall back to the legacy private attribute '_is_polling' for backward
        compatibility (will be removed once all users have updated the coordinator).
        """
        # Public API (preferred)
        public_val = getattr(self.coordinator, "is_polling", None)
        if isinstance(public_val, bool):
            _LOGGER.debug("Polling sensor using public is_polling = %s", public_val)
            return public_val

        # Legacy fallback (compat)
        legacy_val = bool(getattr(self.coordinator, "_is_polling", False))
        _LOGGER.debug("Polling sensor using legacy _is_polling = %s", legacy_val)
        return legacy_val

    @property
    def icon(self) -> str:
        """Return a dynamic icon reflecting the state."""
        return "mdi:refresh" if self.is_on else "mdi:refresh-circle"

    @property
    def device_info(self) -> DeviceInfo:
        """Return DeviceInfo for the integration's diagnostic device."""
        return DeviceInfo(
            identifiers={(DOMAIN, "integration")},
            name="Google Find My Integration",
            manufacturer="BSkando",
            model="Find My Device Integration",
            configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on coordinator updates (polling status can change)."""
        self.async_write_ha_state()
