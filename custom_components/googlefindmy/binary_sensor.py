# custom_components/googlefindmy/binary_sensor.py
"""Binary sensor entities for Google Find My Device integration."""
from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import device_registry as dr  # DeviceEntryType

from .const import (
    DOMAIN,
    INTEGRATION_VERSION,
    SERVICE_DEVICE_NAME,
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_MANUFACTURER,
    service_device_identifier,
)
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

    Exposes a single diagnostic sensor reflecting whether a polling cycle
    is currently in progress (useful for troubleshooting).
    """
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[GoogleFindMyPollingSensor] = [
        GoogleFindMyPollingSensor(coordinator, entry)
    ]
    async_add_entities(entities, True)


class GoogleFindMyPollingSensor(CoordinatorEntity[GoogleFindMyCoordinator], BinarySensorEntity):
    """Binary sensor indicating whether background polling is active."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = POLLING_DESC

    def __init__(self, coordinator: GoogleFindMyCoordinator, entry: ConfigEntry) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        # Namespaced unique_id for multi-account safety
        self._attr_unique_id = f"{DOMAIN}_{self._entry_id}_polling"
        # Name is derived from translation_key; no explicit _attr_name.

    @property
    def is_on(self) -> bool:
        """Return True if a polling cycle is currently running."""
        public_val = getattr(self.coordinator, "is_polling", None)
        if isinstance(public_val, bool):
            return public_val
        # Legacy fallback (older coordinator builds)
        return bool(getattr(self.coordinator, "_is_polling", False))

    @property
    def icon(self) -> str:
        """Return a dynamic icon reflecting the state."""
        return "mdi:sync" if self.is_on else "mdi:sync-off"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the sensor to the per-entry service device."""
        # Single service device per config entry:
        # identifiers -> (DOMAIN, f"integration_<entry_id>")
        return DeviceInfo(
            identifiers={service_device_identifier(self._entry_id)},
            name=SERVICE_DEVICE_NAME,
            manufacturer=SERVICE_DEVICE_MANUFACTURER,
            model=SERVICE_DEVICE_MODEL,
            sw_version=INTEGRATION_VERSION,
            configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
