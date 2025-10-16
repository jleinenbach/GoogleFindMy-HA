# custom_components/googlefindmy/binary_sensor.py
"""Binary sensor entities for the Google Find My Device integration.

This module provides *diagnostic* binary sensors that live under the
per-entry **service device** (see `const.service_device_identifier`).
Sensors are intentionally light-weight and derive their state from the
integration's central `GoogleFindMyCoordinator` and, where appropriate,
from Home Assistant's system facilities (e.g., the Repairs issue registry).

Design goals:
- Keep all network I/O **out** of entity code; entities are consumers of
  coordinator state and HA registries only.
- Use stable, namespaced `unique_id`s to support multi-account setups.
- Route all sensors to the **service device** so users find diagnostics in
  one place.
- Prefer translation keys over hardcoded names/icons where supported.

Provided sensors:
- `polling` (diagnostic): `on` while a sequential polling cycle runs.
- `auth_status` (diagnostic): `on` when an authentication problem exists
  (active Repairs issue or recent auth-error event for this entry).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr  # DeviceEntryType enum
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    INTEGRATION_VERSION,
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_NAME,
    SERVICE_DEVICE_MANUFACTURER,
    TRANSLATION_KEY_AUTH_STATUS,
    EVENT_AUTH_ERROR,
    EVENT_AUTH_OK,
    issue_id_for,
    service_device_identifier,
)
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------
# Entity descriptions
# --------------------------------------------------------------------------------------
POLLING_DESC = BinarySensorEntityDescription(
    key="polling",
    translation_key="polling",
    icon="mdi:refresh",
    entity_category=EntityCategory.DIAGNOSTIC,
)

AUTH_STATUS_DESC = BinarySensorEntityDescription(
    key="auth_status",
    translation_key=TRANSLATION_KEY_AUTH_STATUS,
    device_class=BinarySensorDeviceClass.PROBLEM,  # True => problem present
    icon="mdi:key-alert",
    entity_category=EntityCategory.DIAGNOSTIC,
)


# --------------------------------------------------------------------------------------
# Platform setup
# --------------------------------------------------------------------------------------
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Google Find My Device binary sensor entities (per config entry).

    This registers both diagnostic sensors:
    - A *polling* sensor reflecting whether a sequential poll is currently running.
    - An *auth_status* sensor reflecting whether authentication needs user action.

    Both entities are attached to the per-entry service device and use
    translation-based names (see `translations/*.json`).
    """
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        GoogleFindMyPollingSensor(coordinator, entry),
        GoogleFindMyAuthStatusSensor(coordinator, entry),
    ]
    async_add_entities(entities, True)


# --------------------------------------------------------------------------------------
# Polling sensor
# --------------------------------------------------------------------------------------
class GoogleFindMyPollingSensor(
    CoordinatorEntity[GoogleFindMyCoordinator], BinarySensorEntity
):
    """Binary sensor indicating whether a background sequential polling cycle is active.

    Semantics:
        - `on`  → a sequential device polling cycle is currently in progress.
        - `off` → no sequential poll is running at the moment.

    Implementation details:
        - Uses the coordinator's public `is_polling` property (with a defensive
          fallback for older versions).
        - No network I/O; state changes propagate via the coordinator.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = POLLING_DESC

    def __init__(
        self, coordinator: GoogleFindMyCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the polling sensor."""
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
        """Return a dynamic icon reflecting the state (visual feedback in UI)."""
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


# --------------------------------------------------------------------------------------
# Authentication status sensor
# --------------------------------------------------------------------------------------
class GoogleFindMyAuthStatusSensor(
    CoordinatorEntity[GoogleFindMyCoordinator], BinarySensorEntity
):
    """Binary sensor indicating whether user action is required to re-authenticate.

    Semantics (device_class=problem):
        - `on`  → Authentication problem detected for this config entry
                  (e.g., invalid/expired token). User action is required.
        - `off` → No active authentication problem known.

    Signal sources:
        - **Repairs issue registry**: Existence of the entry-scoped issue
          `issue_id_for(entry_id)` (domain=`googlefindmy`) indicates a known auth
          problem (preferred, idempotent).
        - **Coordinator bus events**: We listen for `googlefindmy.authentication_error`
          and `googlefindmy.authentication_ok` to update state quickly between
          coordinator refreshes. Events must include the `entry_id` to target
          the correct sensor in multi-account setups.

    Notes:
        - No network I/O occurs in this entity.
        - The sensor lives under the *service device* alongside other diagnostics.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = AUTH_STATUS_DESC

    # Internal event-driven state (None -> unknown, True -> problem, False -> ok)
    _event_state: Optional[bool]
    _unsub_err: Optional[Callable[[], None]]
    _unsub_ok: Optional[Callable[[], None]]

    def __init__(
        self, coordinator: GoogleFindMyCoordinator, entry: ConfigEntry
    ) -> None:
        """Initialize the authentication status sensor."""
        super().__init__(coordinator)
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{DOMAIN}_{self._entry_id}_auth_status"
        self._event_state = None
        self._unsub_err = None
        self._unsub_ok = None

    # ----------------------- HA lifecycle hooks -----------------------
    async def async_added_to_hass(self) -> None:
        """Subscribe to auth events when the entity is added to Home Assistant."""
        await super().async_added_to_hass()

        @callback
        def _on_auth_error(event: Event) -> None:
            # Only process events for *this* config entry
            if event.data.get("entry_id") == self._entry_id:
                self._event_state = True
                _LOGGER.debug(
                    "Auth error event received for entry %s; setting problem=True",
                    self._entry_id,
                )
                self.async_write_ha_state()

        @callback
        def _on_auth_ok(event: Event) -> None:
            if event.data.get("entry_id") == self._entry_id:
                self._event_state = False
                _LOGGER.debug(
                    "Auth ok event received for entry %s; setting problem=False",
                    self._entry_id,
                )
                self.async_write_ha_state()

        # Register listeners and keep unsubscribe callables
        self._unsub_err = self.hass.bus.async_listen(EVENT_AUTH_ERROR, _on_auth_error)
        self._unsub_ok = self.hass.bus.async_listen(EVENT_AUTH_OK, _on_auth_ok)

    async def async_will_remove_from_hass(self) -> None:
        """Unsubscribe from auth events when the entity is removed."""
        await super().async_will_remove_from_hass()
        for unsub in (self._unsub_err, self._unsub_ok):
            if unsub:
                try:
                    unsub()
                except Exception:  # defensive
                    pass
        self._unsub_err = None
        self._unsub_ok = None

    # ----------------------- State calculation ------------------------
    @property
    def is_on(self) -> bool:
        """Return True if an authentication problem exists.

        Resolution order:
        1) If we have an event-driven state (_event_state is not None), prefer it.
        2) Otherwise, query the Repairs issue registry for the per-entry issue.
           Existence of the issue is interpreted as "problem = True".

        This dual-source approach ensures the sensor is responsive to events
        while remaining accurate across restarts (Repairs issues persist).
        """
        # 1) Event-driven fast path
        if self._event_state is not None:
            return bool(self._event_state)

        # 2) Repairs issue registry fallback (persistent source of truth)
        reg = ir.async_get(self.hass)
        issue = reg.async_get_issue(DOMAIN, issue_id_for(self._entry_id))
        return issue is not None

    @property
    def icon(self) -> str:
        """Return a dynamic icon to communicate the current auth state."""
        # Keep explicit icons for clarity, even with device_class=problem.
        return "mdi:key-alert" if self.is_on else "mdi:key-check"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the sensor to the per-entry service device."""
        return DeviceInfo(
            identifiers={service_device_identifier(self._entry_id)},
            name=SERVICE_DEVICE_NAME,
            manufacturer=SERVICE_DEVICE_MANUFACTURER,
            model=SERVICE_DEVICE_MODEL,
            sw_version=INTEGRATION_VERSION,
            configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
