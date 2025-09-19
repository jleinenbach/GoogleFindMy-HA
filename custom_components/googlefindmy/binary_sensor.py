"""Binary sensor entities for Google Find My Device integration."""
import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device binary sensor entities."""
    coordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    # Add polling status binary sensor
    entities.append(GoogleFindMyPollingSensor(coordinator))

    async_add_entities(entities)


class GoogleFindMyPollingSensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor showing if polling is active."""

    def __init__(self, coordinator):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_name = "Google Find My Polling"
        self._attr_unique_id = f"{DOMAIN}_polling"

    @property
    def is_on(self):
        """Return true if polling is active."""
        polling_state = self.coordinator._is_polling
        _LOGGER.debug(f"Polling sensor returning _is_polling = {polling_state}")
        return polling_state

    @property
    def icon(self):
        """Return the icon for the sensor."""
        return "mdi:refresh" if self.is_on else "mdi:refresh-circle"

    @property
    def device_info(self) -> dict[str, Any]:
        """Return device info for integration device."""
        return {
            "identifiers": {(DOMAIN, "integration")},
            "name": "Google Find My Integration",
            "manufacturer": "BSkando",
            "model": "Find My Device Integration",
            "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
        }