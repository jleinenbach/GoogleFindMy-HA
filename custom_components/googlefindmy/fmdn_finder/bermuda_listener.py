"""Bermuda BLE Integration - FMDN Location Upload Listener.

Listens to Bermuda device_tracker state changes to detect area changes
and trigger FMDN location uploads to Google's Find My Device network.

CRITICAL: Device Matching via Congealment
=========================================
Bermuda uses "congealment" to attach its entities to EXISTING HA devices.
This means:

    ┌─────────────────────────────────────────────────────────────────┐
    │  SAME HA Device (e.g., "moto tag Jens' Schlüsselbund")          │
    │  HA Device ID: 11b2838b4bb2ba2eb5f4f4b2c742cbf9                 │
    │                                                                 │
    │  ┌─────────────────────────┐  ┌─────────────────────────────┐  │
    │  │ GoogleFindMy Entity     │  │ Bermuda Entity              │  │
    │  │ device_tracker.moto_... │  │ device_tracker.moto_..._2   │  │
    │  │ platform: googlefindmy  │  │ platform: bermuda           │  │
    │  └─────────────────────────┘  └─────────────────────────────┘  │
    └─────────────────────────────────────────────────────────────────┘

To find the GoogleFindMy device for a Bermuda tracker:
1. Get the HA device_id from the Bermuda entity (via entity registry)
2. Find ALL entities belonging to that HA device
3. Look for the entity with platform="googlefindmy" and domain="device_tracker"
4. Extract the Google device ID from that entity's unique_id

WARNING: DO NOT match by:
- Entity names (users can rename entities at any time!)
- Device identifiers (Bermuda doesn't add googlefindmy identifiers)
- MAC addresses (BLE MACs rotate for privacy)

Architecture
------------
Bermuda (jleinenbach/bermuda fork) integrates with GoogleFindMy via the
EID Resolver API (see docs/google_find_my_support.md):

1. Bermuda detects FMDN BLE advertisements containing EIDs
2. EID Resolver maps EIDs to GoogleFindMy devices via precomputed lookup
3. Bermuda attaches its entities to the SAME HA device as GoogleFindMy
4. Both integrations share one HA device, but have separate entities

Entity Pattern: device_tracker.*_bermuda_tracker*
Attributes: area (semantic location), scanner (BLE scanner name)

Flow:
- Bermuda area change detected → find GoogleFindMy entity on SAME HA device
- Get current EID from GoogleFindMy coordinator's identity keys
- Upload semantic location to Google FMDN backend

References:
- Bermuda Fork: https://github.com/jleinenbach/bermuda
- EID Resolver API: docs/google_find_my_support.md
- Bermuda entity congealment: custom_components/bermuda/entity.py
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback
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
DATA_AREA_DEBOUNCE = "fmdn_finder_area_debounce"

# Debouncing - area must be stable for this duration before triggering upload
AREA_STABILIZATION_SECONDS = (
    30  # Wait 30 seconds after area change to confirm it's stable
)
MIN_UPLOAD_INTERVAL_SECONDS = 60  # Minimum time between uploads for same device

# Log formatting
EID_LOG_PREFIX_LENGTH = 8  # Number of hex chars to show in logs


@dataclass
class AreaDebounceState:
    """Track area debouncing state for an entity."""

    entity_id: str
    area: str
    first_seen: float
    last_seen: float
    upload_scheduled: bool = False


async def async_setup_bermuda_listener(hass: HomeAssistant) -> None:
    """Setup Bermuda integration event listener for FMDN location uploads.

    Registers a state change listener that monitors Bermuda device_tracker updates
    and triggers FMDN location uploads when area changes.

    Args:
        hass: Home Assistant instance
    """
    _LOGGER.info("Registering Bermuda FMDN beacon listener")

    # Initialize caches
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_LAST_AREA_CACHE, {})
    hass.data[DOMAIN].setdefault(DATA_AREA_DEBOUNCE, {})

    @callback  # type: ignore[misc, untyped-decorator, unused-ignore]
    def _bermuda_state_changed(event: Event[EventStateChangedData]) -> None:
        """Handle Bermuda device_tracker state changes with debouncing.

        Filters for Bermuda tracker entities and triggers FMDN uploads
        only after area has been stable for AREA_STABILIZATION_SECONDS.
        """
        entity_id: str | None = event.data.get("entity_id")
        new_state: State | None = event.data.get("new_state")
        old_state: State | None = event.data.get("old_state")

        if not entity_id or not new_state:
            return

        # Filter for Bermuda tracker entities
        # Pattern: device_tracker.*_bermuda_tracker* (e.g., device_tracker.moto_tag_koffer_grun_bermuda_tracker_2)
        if (
            not entity_id.startswith("device_tracker.")
            or BERMUDA_TRACKER_SUFFIX not in entity_id
        ):
            return

        # Extract area from attributes
        new_area = new_state.attributes.get(ATTR_AREA)
        old_area = old_state.attributes.get(ATTR_AREA) if old_state else None

        # Skip if no area
        if not new_area:
            _LOGGER.debug("Bermuda tracker %s has no area attribute", entity_id)
            return

        # Skip if area unchanged (unless first detection)
        if new_area == old_area:
            _LOGGER.debug("Bermuda tracker %s area unchanged: %s", entity_id, new_area)
            return

        # Get debounce cache
        debounce_cache: dict[str, AreaDebounceState] = hass.data[DOMAIN].get(
            DATA_AREA_DEBOUNCE, {}
        )
        current_time = time.time()
        debounce_state = debounce_cache.get(entity_id)

        # Check if this is the same area as current debounce state
        if debounce_state and debounce_state.area == new_area:
            # Same area as debounce, update last_seen time
            debounce_state.last_seen = current_time
            _LOGGER.debug(
                "Bermuda %s still in area '%s' (stable for %.1fs)",
                entity_id,
                new_area,
                current_time - debounce_state.first_seen,
            )
            return

        # Area changed - log and start debounce
        _LOGGER.info(
            "Bermuda area change detected: %s -> %s (entity: %s), starting %ds stabilization",
            old_area,
            new_area,
            entity_id,
            AREA_STABILIZATION_SECONDS,
        )

        # Create/update debounce state
        debounce_cache[entity_id] = AreaDebounceState(
            entity_id=entity_id,
            area=new_area,
            first_seen=current_time,
            last_seen=current_time,
            upload_scheduled=False,
        )

        # Schedule delayed upload check
        hass.async_create_task(
            _async_debounced_area_upload(
                hass, entity_id, new_area, new_state.attributes, current_time
            ),
            name=f"fmdn_debounce_{entity_id}",
        )

    # Register state change listener
    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, _bermuda_state_changed)

    # Store unsubscribe callback for cleanup
    hass.data[DOMAIN][DATA_BERMUDA_UNSUBSCRIBE] = unsubscribe

    _LOGGER.info("Bermuda FMDN beacon listener registered successfully")


async def _async_debounced_area_upload(
    hass: HomeAssistant,
    entity_id: str,
    expected_area: str,
    attributes: dict[str, Any],
    debounce_start_time: float,
) -> None:
    """Wait for stabilization period then trigger upload if area is still the same.

    Args:
        hass: Home Assistant instance
        entity_id: Bermuda tracker entity ID
        expected_area: Area that triggered this debounce
        attributes: Entity attributes at the time of area change
        debounce_start_time: When this debounce was started
    """
    # Wait for stabilization period
    await asyncio.sleep(AREA_STABILIZATION_SECONDS)

    # Get current debounce state
    debounce_cache: dict[str, AreaDebounceState] = hass.data.get(DOMAIN, {}).get(
        DATA_AREA_DEBOUNCE, {}
    )
    debounce_state = debounce_cache.get(entity_id)

    if not debounce_state:
        _LOGGER.debug(
            "Debounce state cleared for %s during stabilization, skipping upload",
            entity_id,
        )
        return

    # Check if area changed during stabilization
    if debounce_state.area != expected_area:
        _LOGGER.info(
            "Area changed during stabilization for %s: %s -> %s, skipping upload",
            entity_id,
            expected_area,
            debounce_state.area,
        )
        return

    # Check if this debounce was superseded by a newer one
    if debounce_state.first_seen != debounce_start_time:
        _LOGGER.debug(
            "Superseded debounce for %s (started %.1f, current %.1f), skipping",
            entity_id,
            debounce_start_time,
            debounce_state.first_seen,
        )
        return

    # Check if already uploaded (prevent duplicate uploads)
    if debounce_state.upload_scheduled:
        _LOGGER.debug(
            "Upload already scheduled for %s in area '%s'", entity_id, expected_area
        )
        return

    # Mark as scheduled to prevent duplicates
    debounce_state.upload_scheduled = True

    _LOGGER.info(
        "Area '%s' stable for %ds for %s, triggering FMDN upload",
        expected_area,
        AREA_STABILIZATION_SECONDS,
        entity_id,
    )

    # Trigger the actual upload
    await _async_handle_area_change(hass, entity_id, expected_area, attributes)


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
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

    # Get entity registry to find the HA device
    ent_reg = er.async_get(hass)
    entity_entry = ent_reg.async_get(entity_id)

    if not entity_entry or not entity_entry.device_id:
        _LOGGER.warning("Cannot find device for Bermuda entity %s", entity_id)
        return

    ha_device_id = entity_entry.device_id

    # Debug: Log entity and device information
    dev_reg = dr.async_get(hass)
    bermuda_device = dev_reg.async_get(ha_device_id)
    _LOGGER.debug(
        "Bermuda entity %s: device_id=%s, device_name=%s, identifiers=%s, via_device=%s",
        entity_id,
        ha_device_id,
        bermuda_device.name if bermuda_device else None,
        bermuda_device.identifiers if bermuda_device else None,
        bermuda_device.via_device_id if bermuda_device else None,
    )

    # Find the GoogleFindMy device data for this Bermuda tracker
    device_info = await _async_find_googlefindmy_device(hass, ha_device_id, entity_id)

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
        eid[:EID_LOG_PREFIX_LENGTH].hex()
        if len(eid) >= EID_LOG_PREFIX_LENGTH
        else eid.hex(),
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


async def _async_find_googlefindmy_device(  # noqa: PLR0911
    hass: HomeAssistant,
    ha_device_id: str,
    entity_id: str | None = None,
) -> dict[str, Any] | None:
    """Find GoogleFindMy device data for a Bermuda tracker.

    CRITICAL: This uses Bermuda's "congealment" mechanism where Bermuda
    entities are attached to the SAME HA device as GoogleFindMy entities.

    Algorithm:
        1. Use entity registry to find ALL entities for the given HA device
        2. Filter for entities where platform="googlefindmy" AND domain="device_tracker"
        3. Extract Google device ID from the entity's unique_id

    Example:
        HA Device: "moto tag Jens' Schlüsselbund" (ID: 11b2838b...)
        Contains:
          - device_tracker.moto_tag_jens_schlusselbund (platform=googlefindmy)
          - device_tracker.moto_tag_jens_schlusselbund_bermuda_tracker_2 (platform=bermuda)

        When Bermuda triggers with ha_device_id="11b2838b...", we find the
        googlefindmy entity and extract its Google device ID.

    WARNING - DO NOT USE THESE MATCHING STRATEGIES:
        - Entity name matching: Users can rename entities!
        - Device identifier matching: Bermuda doesn't add googlefindmy identifiers
        - MAC address matching: BLE MACs rotate for privacy

    Args:
        hass: Home Assistant instance
        ha_device_id: Home Assistant device registry ID (shared by both integrations)
        entity_id: Bermuda entity ID (unused, kept for API compatibility)

    Returns:
        Dict with device_id, config_entry_id, coordinator, or None if not found
    """
    domain_data = hass.data.get(DOMAIN)
    if not domain_data:
        return None

    # Get entity registry to find all entities for this device
    ent_reg = er.async_get(hass)

    # Find all entities belonging to this HA device
    # IMPORTANT: Convert to list to avoid iterator exhaustion by debug logging
    device_entities = list(er.async_entries_for_device(ent_reg, ha_device_id))

    # Debug: Log all entities on this device
    _LOGGER.debug(
        "Entities on HA device %s: %s",
        ha_device_id,
        [(e.entity_id, e.platform, e.domain) for e in device_entities],
    )

    # Look for a GoogleFindMy device_tracker entity
    gfm_entity = None
    for entity in device_entities:
        if entity.domain == "device_tracker" and entity.platform == DOMAIN:
            gfm_entity = entity
            break

    if not gfm_entity:
        # Debug: Log all GoogleFindMy entities with their device info
        from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

        dev_reg = dr.async_get(hass)
        gfm_devices_info = []
        for e in ent_reg.entities.values():
            if e.platform == DOMAIN and e.domain == "device_tracker":
                device = dev_reg.async_get(e.device_id) if e.device_id else None
                gfm_devices_info.append(
                    {
                        "entity_id": e.entity_id,
                        "device_id": e.device_id,
                        "device_name": device.name if device else None,
                        "identifiers": list(device.identifiers) if device else None,
                    }
                )
        _LOGGER.debug(
            "No GoogleFindMy device_tracker found for HA device %s "
            "(found %d entities on device, none with platform=%s). "
            "All GoogleFindMy device_trackers: %s",
            ha_device_id,
            len(device_entities),
            DOMAIN,
            gfm_devices_info,
        )
        return None

    # Get coordinator from config entry
    config_entry_id = gfm_entity.config_entry_id
    if not config_entry_id:
        _LOGGER.debug(
            "GoogleFindMy entity %s has no config_entry_id",
            gfm_entity.entity_id,
        )
        return None

    # Runtime data is stored in hass.data[DOMAIN]["entries"][config_entry_id]
    entries_bucket = domain_data.get("entries", {})
    if not isinstance(entries_bucket, dict):
        _LOGGER.debug(
            "No entries bucket in domain_data (got: %s, type: %s)",
            entries_bucket,
            type(entries_bucket).__name__,
        )
        return None

    runtime_data = entries_bucket.get(config_entry_id)
    if runtime_data is None:
        _LOGGER.debug(
            "No runtime_data for config_entry_id %s (available entries: %s)",
            config_entry_id,
            list(entries_bucket.keys()),
        )
        return None

    # RuntimeData is a dataclass with .coordinator attribute
    coordinator = getattr(runtime_data, "coordinator", None)
    if not coordinator:
        _LOGGER.debug(
            "No coordinator in runtime_data for config_entry_id %s (runtime_data: %s)",
            config_entry_id,
            type(runtime_data).__name__,
        )
        return None

    # Extract Google device ID from entity unique_id
    # Format: "{config_entry_id}:{google_device_id}" or just "{google_device_id}"
    google_device_id = gfm_entity.unique_id
    if google_device_id and ":" in google_device_id:
        google_device_id = google_device_id.split(":")[-1]

    _LOGGER.info(
        "Found GoogleFindMy device %s for HA device %s (entity: %s)",
        google_device_id,
        ha_device_id,
        gfm_entity.entity_id,
    )

    return {
        "device_id": google_device_id,
        "config_entry_id": config_entry_id,
        "coordinator": coordinator,
        "ha_device_id": ha_device_id,
    }


async def _async_get_device_eid(  # noqa: PLR0911, PLR0912, PLR0915
    hass: HomeAssistant,
    coordinator: Any,
    device_id: str,
) -> bytes | None:
    """Get the current EID for a GoogleFindMy device.

    The EID is computed from the device's identity key and current time.

    Args:
        hass: Home Assistant instance
        coordinator: GoogleFindMyCoordinator instance
        device_id: Google device UUID (e.g., "68419b51-0000-2131-873b-fc411691d329")

    Returns:
        Current EID bytes (20 or 32 bytes), or None if unavailable
    """
    # Get device identities from coordinator
    get_identities = getattr(coordinator, "get_active_device_identities", None)
    if not callable(get_identities):
        _LOGGER.debug("Coordinator does not have get_active_device_identities method")
        return None

    try:
        identities = get_identities()
    except Exception as err:
        _LOGGER.debug("Failed to get device identities: %s", err)
        return None

    if not identities:
        _LOGGER.debug("No device identities available from coordinator")
        return None

    # Find identity matching our device_id
    # canonical_id format: "{entry_id}:{device_uuid}" or just "{device_uuid}"
    # We match if device_id equals canonical_id or is the suffix after ":"
    identity = None
    for ident in identities:
        canonical_id = getattr(ident, "canonical_id", None)
        if not canonical_id:
            continue

        # Check for exact match or suffix match
        if canonical_id == device_id or canonical_id.endswith(f":{device_id}"):
            identity = ident
            _LOGGER.debug(
                "Found matching identity for device %s (canonical_id: %s)",
                device_id,
                canonical_id,
            )
            break

    if not identity:
        _LOGGER.debug(
            "No identity found for device %s in %d identities. Available canonical_ids: %s",
            device_id,
            len(identities),
            [
                getattr(i, "canonical_id", "?") for i in identities[:10]
            ],  # Limit for logging
        )
        return None

    # Get identity key
    identity_key = getattr(identity, "identity_key", None)
    if not identity_key:
        _LOGGER.debug(
            "Identity for device %s has no identity_key (encrypted_identity_key present: %s)",
            device_id,
            getattr(identity, "encrypted_identity_key", None) is not None,
        )
        return None

    # Generate EID
    try:
        from ..FMDNCrypto.eid_generator import (  # noqa: PLC0415
            EidVariant,
            generate_eid_variant,
        )

        # Calculate beacon time counter
        rotation_period = 1024  # Default FMDN rotation period in seconds
        pair_date = getattr(identity, "pair_date", None) or 0
        current_time = int(time.time())
        beacon_time_counter = (current_time - pair_date) // rotation_period

        # Determine EID variant - check EID resolver's persisted lock first
        variant = EidVariant.LEGACY_SECP160R1_X20_BE  # Default for most FMDN trackers
        eid_resolver = hass.data.get(DOMAIN, {}).get("eid_resolver")
        if eid_resolver:
            # Get the HA device registry ID from the identity
            registry_id = getattr(identity, "registry_id", None)
            if registry_id:
                # Check for persisted lock with known variant
                locks = getattr(eid_resolver, "_persisted_locks", {})
                lock = locks.get(registry_id)
                if lock:
                    lock_variant_str = getattr(lock, "variant", None)
                    if lock_variant_str:
                        try:
                            locked_variant = EidVariant(lock_variant_str)
                            # Only use SECP160r1 variants for encryption (encrypt() only supports SECP160r1)
                            if locked_variant == EidVariant.LEGACY_SECP160R1_X20_BE:
                                variant = locked_variant
                                _LOGGER.debug(
                                    "Using locked EID variant for device %s: %s",
                                    device_id,
                                    variant.value,
                                )
                            else:
                                _LOGGER.warning(
                                    "Device %s uses %s variant which is not supported for upload "
                                    "(encrypt() only supports SECP160r1). Trying LEGACY_SECP160R1_X20_BE.",
                                    device_id,
                                    lock_variant_str,
                                )
                        except ValueError:
                            _LOGGER.debug(
                                "Unknown variant in lock: %s", lock_variant_str
                            )

        _LOGGER.debug(
            "Generating EID for device %s: pair_date=%s, current=%s, counter=%s, variant=%s",
            device_id,
            pair_date,
            current_time,
            beacon_time_counter,
            variant.value,
        )

        # Generate EID using the determined variant
        # Note: encrypt() in foreign_tracker_cryptor.py only supports SECP160r1 (20-byte EID)
        eid = generate_eid_variant(
            eik=identity_key,
            time_counter_u32=beacon_time_counter,
            variant=variant,
        )

        _LOGGER.debug(
            "Generated EID for device %s: %s...",
            device_id,
            eid[:EID_LOG_PREFIX_LENGTH].hex()
            if len(eid) >= EID_LOG_PREFIX_LENGTH
            else eid.hex(),
        )

        return eid

    except Exception as err:
        _LOGGER.warning("Failed to generate EID for device %s: %s", device_id, err)
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
        eid[:EID_LOG_PREFIX_LENGTH].hex()
        if len(eid) >= EID_LOG_PREFIX_LENGTH
        else eid.hex(),
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
