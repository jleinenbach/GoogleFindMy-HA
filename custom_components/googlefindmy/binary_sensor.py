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
- Use stable, entry-scoped `unique_id`s to support multi-account setups:
  "<entry_id>:<sensor_key>" (e.g., "abcd1234:polling").
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
from collections.abc import Callable

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import issue_registry as ir

from .const import (
    DOMAIN,
    TRANSLATION_KEY_AUTH_STATUS,
    EVENT_AUTH_ERROR,
    EVENT_AUTH_OK,
    issue_id_for,
)
from .coordinator import GoogleFindMyCoordinator, format_epoch_utc
from .entity import GoogleFindMyEntity, resolve_coordinator

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
    icon="mdi:account-alert",
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

    Registers both diagnostic sensors under the per-entry service device.
    """
    coordinator = resolve_coordinator(entry)
    subentry_identifier = coordinator.stable_subentry_identifier(
        feature="binary_sensor"
    )
    entities: list[BinarySensorEntity] = [
        GoogleFindMyPollingSensor(
            coordinator, entry, subentry_identifier=subentry_identifier
        ),
        GoogleFindMyAuthStatusSensor(
            coordinator, entry, subentry_identifier=subentry_identifier
        ),
    ]
    async_add_entities(entities, True)


# --------------------------------------------------------------------------------------
# Polling sensor
# --------------------------------------------------------------------------------------
class GoogleFindMyPollingSensor(GoogleFindMyEntity, BinarySensorEntity):
    """Binary sensor indicating whether a background sequential polling cycle is active.

    Semantics:
        - `on`  → a sequential device polling cycle is currently in progress.
        - `off` → no sequential poll is running at the moment.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = POLLING_DESC

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        entry: ConfigEntry,
        *,
        subentry_identifier: str,
    ) -> None:
        """Initialize the polling sensor."""
        super().__init__(coordinator, subentry_identifier=subentry_identifier)
        self._entry_id = entry.entry_id
        entry_id = self.entry_id
        # Entry-scoped unique_id: "<entry_id>:<subentry_identifier>:polling"
        self._attr_unique_id = self.build_unique_id(
            entry_id,
            subentry_identifier,
            "polling",
        )

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
    def available(self) -> bool:
        """Polling diagnostic sensor stays online to expose status information."""

        return True

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the sensor to the per-entry service device."""
        # Single service device per config entry:
        # identifiers -> (DOMAIN, f"integration_<entry_id>")
        return self.service_device_info(include_subentry_identifier=True)


# --------------------------------------------------------------------------------------
# Authentication status sensor
# --------------------------------------------------------------------------------------
class GoogleFindMyAuthStatusSensor(GoogleFindMyEntity, BinarySensorEntity):
    """Binary sensor indicating whether user action is required to re-authenticate.

    Semantics (device_class=problem):
        - `on`  → Authentication problem detected for this config entry
                  (e.g., invalid/expired token). User action is required.
        - `off` → No active authentication problem known.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description = AUTH_STATUS_DESC

    # Internal event-driven state (None -> unknown, True -> problem, False -> ok)
    _event_state: bool | None
    _unsub_err: Callable[[], None] | None
    _unsub_ok: Callable[[], None] | None

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        entry: ConfigEntry,
        *,
        subentry_identifier: str,
    ) -> None:
        """Initialize the authentication status sensor."""
        super().__init__(coordinator, subentry_identifier=subentry_identifier)
        self._entry_id = entry.entry_id
        entry_id = self.entry_id
        # Entry-scoped unique_id: "<entry_id>:<subentry_identifier>:auth_status"
        self._attr_unique_id = self.build_unique_id(
            entry_id,
            subentry_identifier,
            "auth_status",
        )
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
        return "mdi:account-alert" if self.is_on else "mdi:account-check"

    @property
    def available(self) -> bool:
        """Auth status diagnostics remain available even if polling fails."""

        return True

    @property
    def extra_state_attributes(self) -> dict[str, str | None] | None:
        """Expose Nova API and push transport health snapshots."""

        attributes: dict[str, str | None] = {}

        status = getattr(self.coordinator, "api_status", None)
        state = getattr(status, "state", None)
        if isinstance(state, str):
            attributes["nova_api_status"] = state
        reason = getattr(status, "reason", None)
        if isinstance(reason, str) and reason:
            attributes["nova_api_status_reason"] = reason
        changed_at = getattr(status, "changed_at", None)
        changed_at_iso = format_epoch_utc(changed_at)
        if changed_at_iso is not None:
            attributes["nova_api_status_changed_at"] = changed_at_iso

        fcm_status = getattr(self.coordinator, "fcm_status", None)
        fcm_state = getattr(fcm_status, "state", None)
        if isinstance(fcm_state, str):
            attributes["nova_fcm_status"] = fcm_state
        fcm_reason = getattr(fcm_status, "reason", None)
        if isinstance(fcm_reason, str) and fcm_reason:
            attributes["nova_fcm_status_reason"] = fcm_reason
        fcm_changed_at = getattr(fcm_status, "changed_at", None)
        fcm_changed_at_iso = format_epoch_utc(fcm_changed_at)
        if fcm_changed_at_iso is not None:
            attributes["nova_fcm_status_changed_at"] = fcm_changed_at_iso

        return attributes or None

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the sensor to the per-entry service device."""
        return self.service_device_info(include_subentry_identifier=True)
