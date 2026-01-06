"""Bermuda BLE Integration - FMDN Location Upload Listener.

Listens to Bermuda device_tracker state changes to detect area changes
and trigger FMDN location uploads to Google's Find My Device network.

Architecture
------------
Bermuda (jleinenbach/bermuda fork) integrates with GoogleFindMy via the
EID Resolver API (see docs/google_find_my_support.md):

1. Bermuda detects FMDN BLE advertisements containing EIDs
2. EID Resolver maps EIDs to GoogleFindMy devices via precomputed lookup
3. Bermuda creates "metadevices" with fmdn_device_id pointing to HA Device Registry
4. For FMDN devices, Bermuda uses the SAME identifiers as GoogleFindMy (congealment)
   - This means Bermuda entities appear under the same HA device as GoogleFindMy
   - Identifier matching works because both share (DOMAIN, device_id) tuples

Entity Pattern: device_tracker.*_bermuda_tracker*
Attributes: area (semantic location), scanner (BLE scanner name)

Flow:
- Bermuda area change detected → find GoogleFindMy device via shared identifiers
- Get current EID from GoogleFindMy coordinator's identity keys
- Upload semantic location to Google FMDN backend

References:
- Bermuda Fork: https://github.com/jleinenbach/bermuda
- EID Resolver API: docs/google_find_my_support.md
- Bermuda entity congealment: custom_components/bermuda/entity.py
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from homeassistant.helpers.event import EventStateChangedData

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Bermuda entity pattern
BERMUDA_TRACKER_SUFFIX = "_bermuda_tracker"

# Bermuda state attributes
ATTR_AREA = "area"
ATTR_SCANNER = "scanner"
ATTR_SOURCE = "source"

# Data storage keys
DATA_BERMUDA_UNSUBSCRIBE = "fmdn_finder_bermuda_unsub"
DATA_LAST_AREA_CACHE = "fmdn_finder_last_area"

# Throttling
MIN_UPLOAD_INTERVAL_SECONDS = 60  # Minimum time between uploads for same device

# Log formatting
EID_LOG_PREFIX_LENGTH = 8  # Number of hex chars to show in logs


async def async_setup_bermuda_listener(hass: HomeAssistant) -> None:
    """Setup Bermuda integration event listener for FMDN location uploads.

    Registers a state change listener that monitors Bermuda device_tracker updates
    and triggers FMDN location uploads when area changes.

    Args:
        hass: Home Assistant instance
    """
    _LOGGER.info("Registering Bermuda FMDN beacon listener")

    # Initialize area cache for change detection
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_LAST_AREA_CACHE, {})

    @callback  # type: ignore[misc]
    def _bermuda_state_changed(event: Event[EventStateChangedData]) -> None:
        """Handle Bermuda device_tracker state changes.

        Filters for Bermuda tracker entities and triggers FMDN uploads on area change.
        """
        entity_id: str | None = event.data.get("entity_id")
        new_state: State | None = event.data.get("new_state")
        old_state: State | None = event.data.get("old_state")

        if not entity_id or not new_state:
            return

        # Filter for Bermuda tracker entities
        # Pattern: device_tracker.*_bermuda_tracker* (e.g., device_tracker.moto_tag_koffer_grun_bermuda_tracker_2)
        if not entity_id.startswith("device_tracker.") or BERMUDA_TRACKER_SUFFIX not in entity_id:
            return

        # Extract area from attributes
        new_area = new_state.attributes.get(ATTR_AREA)
        old_area = old_state.attributes.get(ATTR_AREA) if old_state else None

        # Skip if no area or area unchanged
        if not new_area:
            _LOGGER.debug("Bermuda tracker %s has no area attribute", entity_id)
            return

        if new_area == old_area:
            return

        _LOGGER.info(
            "Bermuda area change detected: %s -> %s (entity: %s)",
            old_area,
            new_area,
            entity_id,
        )

        # Trigger async upload task
        hass.async_create_task(
            _async_handle_area_change(hass, entity_id, new_area, new_state.attributes),
            name=f"fmdn_upload_{entity_id}",
        )

    # Register state change listener
    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, _bermuda_state_changed)

    # Store unsubscribe callback for cleanup
    hass.data[DOMAIN][DATA_BERMUDA_UNSUBSCRIBE] = unsubscribe

    _LOGGER.info("Bermuda FMDN beacon listener registered successfully")


async def _async_handle_area_change(
    hass: HomeAssistant,
    entity_id: str,
    area: str,
    attributes: dict[str, Any],
) -> None:
    """Handle area change and trigger FMDN upload.

    Args:
        hass: Home Assistant instance
        entity_id: Bermuda tracker entity ID
        area: New area name (semantic location)
        attributes: Entity attributes (scanner, source, etc.)
    """
    # Get entity registry to find the HA device
    ent_reg = er.async_get(hass)
    entity_entry = ent_reg.async_get(entity_id)

    if not entity_entry or not entity_entry.device_id:
        _LOGGER.warning("Cannot find device for Bermuda entity %s", entity_id)
        return

    ha_device_id = entity_entry.device_id

    # Find the GoogleFindMy device data for this HA device
    # For FMDN devices, Bermuda uses the same identifiers as GoogleFindMy
    device_info = await _async_find_googlefindmy_device(hass, ha_device_id)

    if not device_info:
        # This is normal for non-FMDN BLE devices tracked by Bermuda
        # (e.g., regular Bluetooth devices without GoogleFindMy integration)
        _LOGGER.debug(
            "No GoogleFindMy device found for HA device %s (entity: %s) - not an FMDN device",
            ha_device_id,
            entity_id,
        )
        return

    google_device_id = device_info.get("device_id")
    config_entry_id = device_info.get("config_entry_id")
    coordinator = device_info.get("coordinator")

    if not google_device_id or not coordinator or not config_entry_id:
        _LOGGER.warning("Incomplete GoogleFindMy device info for %s", ha_device_id)
        return

    # Type narrowing for mypy
    assert isinstance(config_entry_id, str)

    # Get current EID for the device
    eid = await _async_get_device_eid(hass, coordinator, google_device_id)

    if not eid:
        _LOGGER.warning(
            "Cannot get EID for GoogleFindMy device %s, skipping upload",
            google_device_id,
        )
        return

    _LOGGER.info(
        "Triggering FMDN upload: device=%s, area=%s, EID=%s...",
        google_device_id,
        area,
        eid[:EID_LOG_PREFIX_LENGTH].hex() if len(eid) >= EID_LOG_PREFIX_LENGTH else eid.hex(),
    )

    # Trigger the location upload
    await _async_upload_semantic_location(
        hass=hass,
        eid=eid,
        area=area,
        config_entry_id=config_entry_id,
        scanner=attributes.get(ATTR_SCANNER),
        google_device_id=google_device_id,
        coordinator=coordinator,
    )


async def _async_find_googlefindmy_device(
    hass: HomeAssistant,
    ha_device_id: str,
) -> dict[str, Any] | None:
    """Find GoogleFindMy device data for a Home Assistant device.

    For FMDN devices (GoogleFindMy trackers), Bermuda uses the same device
    identifiers as GoogleFindMy via the "congealment" mechanism. This means
    the Bermuda entity's HA device will have GoogleFindMy identifiers, making
    matching straightforward.

    See: jleinenbach/bermuda - custom_components/bermuda/entity.py
         docs/google_find_my_support.md

    Args:
        hass: Home Assistant instance
        ha_device_id: Home Assistant device registry ID

    Returns:
        Dict with device_id, config_entry_id, coordinator, or None if not found
    """
    domain_data = hass.data.get(DOMAIN)
    if not domain_data:
        return None

    # Get device registry to check identifiers
    dev_reg = dr.async_get(hass)
    device_entry = dev_reg.async_get(ha_device_id)

    if not device_entry:
        return None

    # Check each config entry's coordinator for matching devices
    for entry_id, entry_data in domain_data.items():
        if not isinstance(entry_data, dict):
            continue

        coordinator = entry_data.get("coordinator")
        if not coordinator:
            continue

        # Check if this coordinator has a device matching our HA device
        device_identities = getattr(coordinator, "_device_identities", {})

        for google_device_id, identity in device_identities.items():
            # For FMDN devices, Bermuda copies GoogleFindMy's identifiers
            # so we check if any identifier contains the google_device_id.
            # Identifier formats:
            #   - (DOMAIN, "device_id")
            #   - (DOMAIN, "entry_id:device_id")
            #   - (DOMAIN, "entry_id:subentry_id:device_id")
            for identifier in device_entry.identifiers:
                if identifier[0] == DOMAIN:
                    id_str = str(identifier[1])
                    # Check exact match or suffix match (for namespaced IDs)
                    if id_str == google_device_id or id_str.endswith(f":{google_device_id}"):
                        _LOGGER.debug(
                            "FMDN device matched: ha_device=%s, google_device=%s",
                            ha_device_id,
                            google_device_id,
                        )
                        return {
                            "device_id": google_device_id,
                            "config_entry_id": entry_id,
                            "coordinator": coordinator,
                            "identity": identity,
                        }

    # No match found - this is normal for non-FMDN BLE devices tracked by Bermuda
    return None


async def _async_get_device_eid(
    hass: HomeAssistant,
    coordinator: Any,
    device_id: str,
) -> bytes | None:
    """Get the current EID for a GoogleFindMy device.

    The EID is computed from the device's identity key and current time.

    Args:
        hass: Home Assistant instance
        coordinator: GoogleFindMyCoordinator instance
        device_id: Google device ID

    Returns:
        Current EID bytes (20 or 32 bytes), or None if unavailable
    """
    # Try to get EID from the EID resolver
    eid_resolver = hass.data.get(DOMAIN, {}).get("eid_resolver")

    if eid_resolver:
        # Get device identity from coordinator
        device_identities = getattr(coordinator, "_device_identities", {})
        identity = device_identities.get(device_id)

        if identity:
            # Generate current EID using the identity key
            identity_key = getattr(identity, "identity_key", None)
            if identity_key:
                try:
                    import time  # noqa: PLC0415

                    from ..FMDNCrypto.eid_generator import (  # noqa: PLC0415
                        EidVariant,
                        generate_eid_variant,
                    )

                    # Calculate beacon time counter
                    rotation_period = 1024  # Default FMDN rotation period
                    pair_date = getattr(identity, "pair_date", 0) or 0
                    current_time = int(time.time())
                    beacon_time_counter = (current_time - pair_date) // rotation_period

                    # Generate EID
                    eid = generate_eid_variant(
                        eik=identity_key,
                        time_counter_u32=beacon_time_counter,
                        variant=EidVariant.MODERN_P256_X20_TRUNC_BE,
                    )
                    return eid
                except Exception as err:
                    _LOGGER.debug("Failed to generate EID: %s", err)

    # Fallback: try to get from coordinator's cached data
    device_cache = getattr(coordinator, "_device_location_data", {})
    cached = device_cache.get(device_id, {})

    # Check for pre-computed EID in cache (if available)
    eid_hex = cached.get("current_eid")
    if eid_hex:
        try:
            return bytes.fromhex(eid_hex)
        except (ValueError, TypeError):
            pass

    return None


async def _async_upload_semantic_location(  # noqa: PLR0913
    hass: HomeAssistant,
    eid: bytes,
    area: str,
    config_entry_id: str,
    scanner: str | None = None,
    google_device_id: str | None = None,
    coordinator: Any | None = None,
) -> None:
    """Upload semantic location to Google FMDN backend.

    Args:
        hass: Home Assistant instance
        eid: Device EID (20 or 32 bytes)
        area: Semantic location name (e.g., "Windfang", "Wohnzimmer")
        config_entry_id: Config entry ID for authentication
        scanner: Optional scanner name for logging
        google_device_id: Google device ID for semantic_name update
        coordinator: GoogleFindMy coordinator for semantic_name update
    """
    from .location_uploader import async_process_fmdn_beacon_detection  # noqa: PLC0415

    _LOGGER.debug(
        "Uploading semantic location: EID=%s..., area=%s, scanner=%s",
        eid[:EID_LOG_PREFIX_LENGTH].hex() if len(eid) >= EID_LOG_PREFIX_LENGTH else eid.hex(),
        area,
        scanner,
    )

    # Use the location uploader with the semantic area
    await async_process_fmdn_beacon_detection(
        hass=hass,
        eid=eid,
        area=area,
        rssi=None,  # Not available from Bermuda tracker
        scanner_address=scanner,
        scanner_device_id=None,
        fmdn_device_id=None,
        entity_id=f"bermuda_semantic_{area}",
        google_device_id=google_device_id,
        coordinator=coordinator,
    )


async def async_unload_bermuda_listener(hass: HomeAssistant) -> None:
    """Unload Bermuda event listener.

    Args:
        hass: Home Assistant instance
    """
    domain_data = hass.data.get(DOMAIN, {})
    unsubscribe: Callable[[], None] | None = domain_data.get(DATA_BERMUDA_UNSUBSCRIBE)

    if unsubscribe:
        unsubscribe()
        domain_data.pop(DATA_BERMUDA_UNSUBSCRIBE, None)
        _LOGGER.info("Bermuda FMDN beacon listener unloaded")
    else:
        _LOGGER.debug("No Bermuda listener to unload")
