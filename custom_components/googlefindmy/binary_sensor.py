# custom_components/googlefindmy/binary_sensor.py
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
from homeassistant.helpers import device_registry as dr  # Needed for DeviceEntryType

from .const import DOMAIN, INTEGRATION_VERSION
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)

POLLING_DESC = BinarySensorEntityDescription(
    key="polling",
    translation_key="polling",
    icon="mdi:refresh",  # Default icon
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
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = POLLING_DESC

    def __init__(self, coordinator: GoogleFindMyCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_polling"
        # _attr_name is intentionally not set; it's derived from translation_key.

    @property
    def is_on(self) -> bool:
        """Return True if a polling cycle is currently running.

        Prefer the public read-only property 'is_polling' (new Coordinator API).
        Fall back to the legacy private attribute '_is_polling' for backward
        compatibility.
        """
        # Public API (preferred)
        public_val = getattr(self.coordinator, "is_polling", None)
        if isinstance(public_val, bool):
            return public_val

        # Legacy fallback (for compatibility during transition)
        legacy_val = bool(getattr(self.coordinator, "_is_polling", False))
        return legacy_val

    @property
    def icon(self) -> str:
        """Return a dynamic icon reflecting the state."""
        return "mdi:sync" if self.is_on else "mdi:sync-off"

    @property
    def device_info(self) -> DeviceInfo:
        """Return DeviceInfo for the integration's diagnostic device."""
        return DeviceInfo(
            identifiers={(DOMAIN, "integration")},
            name="Google Find My Integration",
            manufacturer="BSkando",
            model="Find My Device Integration",
            sw_version=INTEGRATION_VERSION,  # Display integration version
            configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
            # Mark as a service device to hide the "Delete device" action in HA UI.
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write state on coordinator updates (polling status can change)."""
        self.async_write_ha_state()
