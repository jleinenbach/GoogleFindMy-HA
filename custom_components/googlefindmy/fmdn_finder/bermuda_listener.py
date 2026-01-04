"""Bermuda BLE Integration - FMDN Beacon Event Listener.

Listens to Bermuda integration state changes to detect FMDN beacons and
extract relevant data (EID, RSSI, scanner location) for location reporting.

Bermuda Integration: https://github.com/jleinenbach/bermuda
FMDN Specification: FMDN.md Section 3 (BLE Advertising & EID)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback

if TYPE_CHECKING:
    from homeassistant.helpers.event import EventStateChangedData

_LOGGER = logging.getLogger(__name__)

# Bermuda constants
BERMUDA_DOMAIN = "bermuda"
ATTR_FMDN_EID = "fmdn_eid"
ATTR_AREA = "area"
ATTR_RSSI = "rssi"
ATTR_SOURCE = "source"
ATTR_SCANNER_DEVICE_ID = "scanner_device_id"
ATTR_FMDN_DEVICE_ID = "fmdn_device_id"

# Data storage key
DATA_BERMUDA_LISTENER = "fmdn_finder_bermuda_listener"
DATA_BERMUDA_UNSUBSCRIBE = "fmdn_finder_bermuda_unsub"

# Log formatting constants
EID_LOG_PREFIX_LENGTH = 8  # Number of hex chars to show in logs


async def async_setup_bermuda_listener(hass: HomeAssistant) -> None:
    """Setup Bermuda integration event listener for FMDN beacons.

    Registers a state change listener that monitors Bermuda sensor updates
    for FMDN device detections.

    Args:
        hass: Home Assistant instance

    Raises:
        RuntimeError: If location_uploader module cannot be imported
    """
    # Lazy import to avoid circular dependencies
    try:
        from .location_uploader import (  # noqa: PLC0415
            async_process_fmdn_beacon_detection,
        )
    except ImportError as err:
        _LOGGER.error("Failed to import location_uploader: %s", err)
        raise RuntimeError("FMDN Finder location_uploader not available") from err

    _LOGGER.info("Registering Bermuda FMDN beacon listener")

    @callback  # type: ignore[misc,untyped-decorator,unused-ignore]
    def _bermuda_state_changed(event: Event[EventStateChangedData]) -> None:  # noqa: PLR0911
        """Handle Bermuda entity state changes.

        Filters for FMDN-related entities and extracts beacon data.

        Args:
            event: State change event from Home Assistant
        """
        entity_id: str | None = event.data.get("entity_id")
        new_state: State | None = event.data.get("new_state")

        # Combined early validation checks
        if not entity_id or not new_state:
            return

        # Filter for Bermuda FMDN entities
        # Bermuda creates sensors like: sensor.bermuda_fmdn_device_xxxxx
        if not entity_id.startswith(f"sensor.{BERMUDA_DOMAIN}_") or "fmdn" not in entity_id.lower():
            return

        # Extract FMDN attributes from Bermuda state
        attrs = new_state.attributes
        fmdn_eid_hex = attrs.get(ATTR_FMDN_EID)

        if not fmdn_eid_hex:
            # Not an FMDN beacon or EID not yet resolved
            _LOGGER.debug(
                "Entity %s has no FMDN EID attribute (may be unresolved beacon)",
                entity_id,
            )
            return

        # Validate EID format (hex string)
        if not isinstance(fmdn_eid_hex, str):
            _LOGGER.warning(
                "Invalid FMDN EID type for %s: %s (expected str)",
                entity_id,
                type(fmdn_eid_hex),
            )
            return

        # Extract additional metadata
        area = attrs.get(ATTR_AREA)
        rssi = attrs.get(ATTR_RSSI)
        scanner_source = attrs.get(ATTR_SOURCE)
        scanner_device_id = attrs.get(ATTR_SCANNER_DEVICE_ID)
        fmdn_device_id = attrs.get(ATTR_FMDN_DEVICE_ID)

        _LOGGER.debug(
            "FMDN beacon detected: entity=%s, EID=%s..., area=%s, rssi=%s, scanner=%s",
            entity_id,
            fmdn_eid_hex[:EID_LOG_PREFIX_LENGTH] if len(fmdn_eid_hex) >= EID_LOG_PREFIX_LENGTH else fmdn_eid_hex,
            area,
            rssi,
            scanner_source,
        )

        # Convert hex EID to bytes
        try:
            eid_bytes = bytes.fromhex(fmdn_eid_hex)
        except (ValueError, AttributeError) as err:
            _LOGGER.warning("Failed to parse FMDN EID hex '%s': %s", fmdn_eid_hex, err)
            return

        # Validate EID length (20 or 32 bytes per FMDN spec)
        if len(eid_bytes) not in (20, 32):
            _LOGGER.warning(
                "Invalid FMDN EID length for %s: %d bytes (expected 20 or 32)",
                entity_id,
                len(eid_bytes),
            )
            return

        # Pass to location uploader (async task)
        hass.async_create_task(
            async_process_fmdn_beacon_detection(
                hass=hass,
                eid=eid_bytes,
                area=area,
                rssi=rssi,
                scanner_address=scanner_source,
                scanner_device_id=scanner_device_id,
                fmdn_device_id=fmdn_device_id,
                entity_id=entity_id,
            ),
            name=f"fmdn_finder_upload_{entity_id}",
        )

    # Register state change listener
    # Using async_track_state_change_event for efficient filtering
    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, _bermuda_state_changed)

    # Store unsubscribe callback for cleanup
    hass.data.setdefault(BERMUDA_DOMAIN, {})
    hass.data[BERMUDA_DOMAIN][DATA_BERMUDA_UNSUBSCRIBE] = unsubscribe

    _LOGGER.info("Bermuda FMDN beacon listener registered successfully")


async def async_unload_bermuda_listener(hass: HomeAssistant) -> None:
    """Unload Bermuda event listener.

    Args:
        hass: Home Assistant instance
    """
    bermuda_data = hass.data.get(BERMUDA_DOMAIN, {})
    unsubscribe: Callable[[], None] | None = bermuda_data.get(DATA_BERMUDA_UNSUBSCRIBE)

    if unsubscribe:
        unsubscribe()
        bermuda_data.pop(DATA_BERMUDA_UNSUBSCRIBE, None)
        _LOGGER.info("Bermuda FMDN beacon listener unloaded")
    else:
        _LOGGER.debug("No Bermuda listener to unload")
