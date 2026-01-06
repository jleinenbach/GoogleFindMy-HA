"""FMDN Location Report Encryptor & Uploader.

Encrypts location data using the FMDN cryptographic primitives and creates
protobuf messages for upload to Google's FMDN backend.

Encryption Flow:
1. Resolve scanner location (Bermuda area → HA Zone → GPS)
2. Check upload throttling (FMDN spec: distance/time gating)
3. Serialize location to protobuf
4. Encrypt with EID public key (ECDH + AES-EAX)
5. Upload to Google FMDN backend

References:
- FMDN.md Section 4: Finder Device Behavior
- FMDN.md Appendix A2: Finder-side E2EE location upload
- foreign_tracker_cryptor.py: encrypt() function
"""

from __future__ import annotations

import logging
import math
import secrets
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

_LOGGER = logging.getLogger(__name__)

# Upload throttling constants (per FMDN spec Section 4)
MIN_UPLOAD_DISTANCE_METERS = 50  # Minimum distance change to trigger upload
MIN_UPLOAD_INTERVAL_SECONDS = 300  # 5 minutes minimum between uploads for GPS-only
MIN_UPLOAD_INTERVAL_SEMANTIC = 60  # 1 minute minimum for semantic location changes
MIN_UPLOAD_INTERVAL_SCREEN_OFF = 300  # Additional delay when screen off
MIN_UPLOAD_INTERVAL_BATTERY_SAVER = 900  # 15 minutes in battery saver mode

# Accuracy thresholds
MAX_ZONE_ACCURACY_METERS = 100  # Zone-based location accuracy
DEFAULT_HOME_ZONE_ACCURACY = 50  # Default home zone accuracy

# RSSI-based distance estimation thresholds (dBm)
RSSI_THRESHOLD_VERY_CLOSE = -60  # < -60 dBm = very close (2m estimated)
RSSI_THRESHOLD_CLOSE = -70  # < -70 dBm = close (5m estimated)
RSSI_THRESHOLD_MEDIUM = -80  # < -80 dBm = medium (10m estimated)
RSSI_THRESHOLD_FAR = -90  # < -90 dBm = far (20m estimated)

# Cache management
UPLOAD_CACHE_MAX_ENTRIES = 100  # Maximum cached upload entries
UPLOAD_CACHE_CLEANUP_COUNT = 50  # Number of old entries to remove

# Log formatting
EID_LOG_PREFIX_LENGTH = 8  # Number of hex chars to show in logs

# Data storage keys
DATA_FMDN_UPLOAD_CACHE = "fmdn_finder_upload_cache"


@dataclass
class LocationData:
    """Location data with coordinates and accuracy."""

    latitude: float
    longitude: float
    accuracy: int  # meters
    zone_name: str | None = None
    timestamp: float = 0.0


@dataclass
class UploadCacheEntry:
    """Cache entry for upload throttling."""

    eid_hex: str
    location: LocationData
    timestamp: float
    semantic_area: str | None = None  # Track semantic area for change detection


async def async_process_fmdn_beacon_detection(  # noqa: PLR0913
    hass: HomeAssistant,
    *,
    eid: bytes,
    area: str | None,
    rssi: int | None,
    scanner_address: str | None,
    scanner_device_id: str | None,
    fmdn_device_id: str | None,
    entity_id: str,
    google_device_id: str | None = None,
    coordinator: Any | None = None,
) -> bool:
    """Process FMDN beacon detection and upload location report.

    Main entry point from bermuda_listener. Coordinates location resolution,
    throttling, encryption, and upload.

    Args:
        hass: Home Assistant instance
        eid: Ephemeral Identity Key (20 or 32 bytes) from FMDN beacon
        area: Bermuda area/room name
        rssi: Signal strength (dBm)
        scanner_address: BLE scanner MAC address
        scanner_device_id: HA device registry ID of scanner
        fmdn_device_id: HA device registry ID of FMDN device
        entity_id: Bermuda entity ID for logging
        google_device_id: Google device ID for semantic_name update
        coordinator: GoogleFindMy coordinator for semantic_name update

    Returns:
        True if upload succeeded, False otherwise
    """
    eid_hex = eid.hex()
    _LOGGER.debug(
        "Processing FMDN beacon: EID=%s..., area=%s, RSSI=%s",
        eid_hex[:EID_LOG_PREFIX_LENGTH],
        area,
        rssi,
    )

    # 1. Resolve scanner location
    location = await _resolve_scanner_location(hass, area, scanner_address, scanner_device_id)

    if not location:
        _LOGGER.debug(
            "No location available for scanner (area=%s, scanner=%s), skipping upload",
            area,
            scanner_address,
        )
        return False

    _LOGGER.debug(
        "Resolved location: lat=%.6f, lon=%.6f, accuracy=%dm, zone=%s",
        location.latitude,
        location.longitude,
        location.accuracy,
        location.zone_name,
    )

    # 2. Check upload throttling (pass area for semantic upload handling)
    should_upload, reason = await _should_upload_location(hass, eid_hex, location, area)

    if not should_upload:
        _LOGGER.debug("Upload throttled for EID %s...: %s", eid_hex[:EID_LOG_PREFIX_LENGTH], reason)
        return False

    _LOGGER.info("Upload allowed for EID %s...: %s", eid_hex[:EID_LOG_PREFIX_LENGTH], reason)

    _LOGGER.info(
        "Uploading FMDN location report: EID=%s..., zone=%s, accuracy=%dm",
        eid_hex[:EID_LOG_PREFIX_LENGTH],
        location.zone_name,
        location.accuracy,
    )

    # 3. Calculate accuracy from RSSI (if available)
    if rssi is not None:
        rssi_accuracy = _calculate_accuracy_from_rssi(rssi, location.accuracy)
        location.accuracy = max(location.accuracy, rssi_accuracy)

    # 4. Encrypt and upload
    try:
        success = await _encrypt_and_upload_location(hass, eid, location, area)

        if success:
            # Update cache on successful upload (including semantic area for throttling)
            _update_upload_cache(hass, eid_hex, location, area)

            # Update semantic_name in coordinator for device_tracker display
            if google_device_id and coordinator and area:
                _update_semantic_name(coordinator, google_device_id, area)
                _LOGGER.debug(
                    "Updated semantic_name='%s' for device %s",
                    area,
                    google_device_id,
                )

            _LOGGER.info(
                "FMDN location report uploaded successfully for EID %s... (semantic: %s)",
                eid_hex[:EID_LOG_PREFIX_LENGTH],
                area,
            )

        return success

    except Exception as err:  # noqa: BLE001
        _LOGGER.error(
            "Failed to upload FMDN location report for EID %s...: %s",
            eid_hex[:EID_LOG_PREFIX_LENGTH],
            err,
            exc_info=True,
        )
        return False


async def _resolve_scanner_location(
    hass: HomeAssistant,
    area: str | None,
    scanner_address: str | None,
    scanner_device_id: str | None,
) -> LocationData | None:
    """Resolve GPS coordinates from Bermuda area or scanner location.

    Resolution priority:
    1. HA Zone matching Bermuda area name
    2. Scanner device location (from device attributes)
    3. Home zone as fallback

    Args:
        hass: Home Assistant instance
        area: Bermuda area/room name
        scanner_address: BLE scanner MAC address
        scanner_device_id: HA device registry ID of scanner

    Returns:
        LocationData if location found, None otherwise
    """
    # Option 1: Resolve area to HA zone
    if area:
        zone_state = hass.states.get(f"zone.{area.lower().replace(' ', '_')}")
        if zone_state and _is_valid_zone_state(zone_state):
            return _location_from_zone_state(zone_state, area)

    # Option 2: Check if scanner has fixed location in device attributes
    # (Future enhancement: could read from device_tracker or area)
    if scanner_device_id:
        scanner_location = await _get_scanner_device_location(hass, scanner_device_id)
        if scanner_location:
            return scanner_location

    # Option 3: Fallback to home zone
    home_zone = hass.states.get("zone.home")
    if home_zone and _is_valid_zone_state(home_zone):
        return _location_from_zone_state(home_zone, "home")

    # Option 4: Person location (if scanner is a mobile device)
    # Could check if scanner_address matches a known mobile_app device
    # and use its GPS location (future enhancement)

    return None


def _is_valid_zone_state(state: State) -> bool:
    """Check if zone state has valid GPS coordinates."""
    lat = state.attributes.get("latitude")
    lon = state.attributes.get("longitude")
    return isinstance(lat, (int, float)) and isinstance(lon, (int, float))


def _location_from_zone_state(state: State, zone_name: str) -> LocationData:
    """Create LocationData from zone state."""
    latitude = float(state.attributes["latitude"])
    longitude = float(state.attributes["longitude"])
    radius = state.attributes.get("radius", DEFAULT_HOME_ZONE_ACCURACY)

    # Zone-based location is inherently less accurate
    accuracy = max(int(radius), MAX_ZONE_ACCURACY_METERS)

    return LocationData(
        latitude=latitude,
        longitude=longitude,
        accuracy=accuracy,
        zone_name=zone_name,
        timestamp=time.time(),
    )


async def _get_scanner_device_location(
    hass: HomeAssistant,
    device_id: str,
) -> LocationData | None:
    """Get fixed location from scanner device attributes.

    This is a placeholder for future enhancement where ESPHome devices
    or Bluetooth proxies could have configured GPS coordinates.

    Args:
        hass: Home Assistant instance
        device_id: HA device registry ID

    Returns:
        LocationData if device has location, None otherwise
    """
    # Future: Check device registry for GPS attributes
    # For now, return None (not implemented)
    return None


async def _should_upload_location(  # noqa: PLR0911
    hass: HomeAssistant,
    eid_hex: str,
    new_location: LocationData,
    semantic_area: str | None = None,
) -> tuple[bool, str]:
    """Check if location should be uploaded (throttling per FMDN spec).

    FMDN Specification (Section 4, Appendix A3):
    - Upload if distance_grew OR accuracy_improved OR sufficient_time_elapsed
    - Additional delays when screen_off or battery_saver mode

    For semantic location uploads (from Bermuda areas):
    - Allow upload if semantic area changed (even without GPS distance change)
    - Use shorter throttle interval (MIN_UPLOAD_INTERVAL_SEMANTIC)

    Args:
        hass: Home Assistant instance
        eid_hex: EID as hex string (cache key)
        new_location: New location to upload
        semantic_area: Semantic location name (e.g., "Büro", "Wohnzimmer")

    Returns:
        Tuple of (should_upload: bool, reason: str)
    """
    # Get upload cache
    upload_cache: dict[str, UploadCacheEntry] = hass.data.setdefault(
        DATA_FMDN_UPLOAD_CACHE, {}
    )

    last_upload = upload_cache.get(eid_hex)

    # First upload for this EID
    if not last_upload:
        return True, "first_upload"

    # Check time elapsed
    elapsed = time.time() - last_upload.timestamp

    # For semantic area uploads, check if area changed
    if semantic_area:
        # If area changed, use shorter throttle interval
        if last_upload.semantic_area != semantic_area:
            if elapsed >= MIN_UPLOAD_INTERVAL_SEMANTIC:
                return True, f"semantic_area_changed ({last_upload.semantic_area} -> {semantic_area})"
            return False, f"semantic_area_changed_too_soon ({elapsed:.0f}s < {MIN_UPLOAD_INTERVAL_SEMANTIC}s)"

        # Same area - use longer interval and require significant change
        if elapsed < MIN_UPLOAD_INTERVAL_SECONDS:
            return False, f"same_area_too_soon ({elapsed:.0f}s < {MIN_UPLOAD_INTERVAL_SECONDS}s)"

    # GPS-only upload - standard throttling
    elif elapsed < MIN_UPLOAD_INTERVAL_SECONDS:
        return False, f"too_soon ({elapsed:.0f}s < {MIN_UPLOAD_INTERVAL_SECONDS}s)"

    # After minimum interval, upload if ANY of: distance grew, accuracy improved
    # (per FMDN spec: distance_grew OR accuracy_improved OR sufficient_time_elapsed)

    # Check distance (Haversine)
    distance = _haversine_distance(
        last_upload.location.latitude,
        last_upload.location.longitude,
        new_location.latitude,
        new_location.longitude,
    )

    if distance >= MIN_UPLOAD_DISTANCE_METERS:
        return True, f"distance_threshold_met ({distance:.0f}m >= {MIN_UPLOAD_DISTANCE_METERS}m)"

    # Check accuracy improvement
    accuracy_improved = new_location.accuracy < last_upload.location.accuracy * 0.8

    if accuracy_improved:
        return True, f"accuracy_improved ({new_location.accuracy}m < {last_upload.location.accuracy}m)"

    # For semantic uploads, allow periodic refresh even without GPS change
    if semantic_area and elapsed >= MIN_UPLOAD_INTERVAL_SECONDS:
        return True, f"semantic_periodic_refresh (elapsed={elapsed:.0f}s)"

    # No significant change
    return False, f"too_close ({distance:.0f}m < {MIN_UPLOAD_DISTANCE_METERS}m, accuracy not improved)"


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two GPS points using Haversine formula.

    Args:
        lat1: Latitude of point 1 (degrees)
        lon1: Longitude of point 1 (degrees)
        lat2: Latitude of point 2 (degrees)
        lon2: Longitude of point 2 (degrees)

    Returns:
        Distance in meters
    """
    # Earth radius in meters
    R = 6371000

    # Convert to radians
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lat = math.radians(lat2 - lat1)
    delta_lon = math.radians(lon2 - lon1)

    # Haversine formula
    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    return R * c


def _calculate_accuracy_from_rssi(rssi: int, zone_accuracy: int) -> int:
    """Estimate location accuracy from RSSI signal strength.

    Rough estimation based on typical BLE signal propagation.
    Combines RSSI-based distance with zone accuracy.

    Args:
        rssi: Signal strength in dBm
        zone_accuracy: Base accuracy from zone (meters)

    Returns:
        Estimated accuracy in meters
    """
    # RSSI to distance mapping (very rough estimation)
    # Formula: distance = 10 ^ ((TxPower - RSSI) / (10 * N))
    # Assuming TxPower = -59 dBm (typical), N = 2.5 (indoor)

    if rssi > RSSI_THRESHOLD_VERY_CLOSE:
        estimated_distance = 2  # Very close
    elif rssi > RSSI_THRESHOLD_CLOSE:
        estimated_distance = 5  # Close
    elif rssi > RSSI_THRESHOLD_MEDIUM:
        estimated_distance = 10  # Medium
    elif rssi > RSSI_THRESHOLD_FAR:
        estimated_distance = 20  # Far
    else:
        estimated_distance = 30  # Very far

    # Return maximum of RSSI-based and zone-based accuracy
    return max(estimated_distance, zone_accuracy)


def _update_semantic_name(coordinator: Any, google_device_id: str, area: str) -> None:
    """Update semantic_name in coordinator's device data.

    This updates the device_tracker's location_name attribute after
    a successful FMDN location upload.

    Args:
        coordinator: GoogleFindMy coordinator
        google_device_id: Google device ID
        area: Semantic location name (e.g., "Büro", "Wohnzimmer")
    """
    device_location_data = getattr(coordinator, "_device_location_data", None)
    if device_location_data is None:
        return

    device_data = device_location_data.get(google_device_id)
    if device_data is None:
        # Create minimal entry if not exists
        device_location_data[google_device_id] = {"semantic_name": area}
    else:
        device_data["semantic_name"] = area

    # Trigger entity update by requesting refresh
    # The coordinator will propagate this to the device_tracker entity
    if hasattr(coordinator, "async_request_refresh"):
        # Don't await - just schedule the refresh
        import asyncio  # noqa: PLC0415

        asyncio.create_task(coordinator.async_request_refresh())


async def _encrypt_and_upload_location(
    hass: HomeAssistant,
    eid: bytes,
    location: LocationData,
    semantic_area: str | None = None,
) -> bool:
    """Encrypt location and upload to Google FMDN backend.

    Uses existing crypto primitives from foreign_tracker_cryptor.py
    and protobuf definitions from LocationReportsUpload_pb2.

    For semantic locations (from Bermuda areas), we send:
    - status = SEMANTIC (0)
    - semanticLocation.locationName = area name

    For GPS locations, we additionally send:
    - geoLocation.encryptedReport with encrypted GPS data
    - geoLocation.accuracy

    Args:
        hass: Home Assistant instance
        eid: Ephemeral Identity Key (20 or 32 bytes)
        location: Location to upload
        semantic_area: Optional semantic location name (e.g., "Büro", "Wohnzimmer")

    Returns:
        True if upload succeeded, False otherwise

    Raises:
        ImportError: If crypto or protobuf modules not available
        ValueError: If encryption fails
    """
    # Lazy imports to avoid loading heavy crypto libraries at startup
    from ..FMDNCrypto.foreign_tracker_cryptor import encrypt  # noqa: PLC0415
    from ..ProtoDecoders.Common_pb2 import (  # noqa: PLC0415
        EncryptedReport,
        GeoLocation,
        LocationReport,
        SemanticLocation,
        Status,
    )
    from ..ProtoDecoders.LocationReportsUpload_pb2 import (  # noqa: PLC0415
        LocationReportsUpload,
    )

    # 1. Create raw GPS data for encryption (simple lat/lon format)
    # Note: This is the inner encrypted payload, not the outer protobuf
    gps_data = f"{location.latitude:.7f},{location.longitude:.7f}".encode()

    _LOGGER.debug(
        "GPS data for encryption: %s (accuracy=%dm, zone=%s)",
        gps_data.decode(),
        location.accuracy,
        location.zone_name,
    )

    # 2. Encrypt GPS data with EID public key (ECDH + AES-EAX)
    random_bytes = secrets.token_bytes(32)

    try:
        encrypted_and_tag, Sx = encrypt(
            message=gps_data,
            random=random_bytes,
            eid=eid,
        )
    except Exception as err:
        _LOGGER.error("Failed to encrypt location: %s", err, exc_info=True)
        raise ValueError(f"Encryption failed: {err}") from err

    _LOGGER.debug(
        "Encrypted location: %d bytes, ephemeral key Sx=%s...",
        len(encrypted_and_tag),
        Sx.hex()[:16],
    )

    # 3. Create LocationReport protobuf with proper structure
    location_report = LocationReport()

    # Set semantic location if available (primary for Bermuda area-based uploads)
    if semantic_area:
        location_report.semanticLocation.CopyFrom(
            SemanticLocation(locationName=semantic_area)
        )
        location_report.status = Status.SEMANTIC
        _LOGGER.debug("Setting semantic location: '%s'", semantic_area)
    else:
        location_report.status = Status.CROWDSOURCED

    # Add encrypted GPS data
    encrypted_report = EncryptedReport(
        publicKeyRandom=Sx,  # Ephemeral public key (x-coordinate)
        encryptedLocation=encrypted_and_tag,
        isOwnReport=True,  # This is from our own scanner
    )

    location_report.geoLocation.CopyFrom(
        GeoLocation(
            encryptedReport=encrypted_report,
            deviceTimeOffset=0,  # Offset from report.time
            accuracy=float(location.accuracy),
        )
    )

    _LOGGER.debug(
        "LocationReport: status=%s, semantic='%s', accuracy=%.0fm",
        Status.Name(location_report.status),
        semantic_area or "(none)",
        location.accuracy,
    )

    # 4. Create LocationReportsUpload protobuf
    upload = LocationReportsUpload()
    upload.random1 = secrets.randbits(64)
    upload.random2 = secrets.randbits(64)

    report = upload.reports.add()
    report.advertisement.identifier.truncatedEid = eid[:10]
    report.advertisement.unwantedTrackingModeEnabled = 0
    report.time.seconds = int(time.time())
    report.location.CopyFrom(location_report)

    upload_bytes = upload.SerializeToString()

    _LOGGER.info(
        "Upload protobuf: %d bytes, semantic='%s', EID=%s...",
        len(upload_bytes),
        semantic_area or "(GPS only)",
        eid[:8].hex(),
    )

    # 5. Upload to Google FMDN backend
    from .google_uploader import async_upload_to_google_fmdn  # noqa: PLC0415

    return await async_upload_to_google_fmdn(hass, upload_bytes, eid[:10].hex())


def _update_upload_cache(
    hass: HomeAssistant,
    eid_hex: str,
    location: LocationData,
    semantic_area: str | None = None,
) -> None:
    """Update upload cache with latest upload timestamp and location.

    Args:
        hass: Home Assistant instance
        eid_hex: EID as hex string
        location: Uploaded location
        semantic_area: Semantic location name for throttling comparison
    """
    upload_cache: dict[str, UploadCacheEntry] = hass.data.setdefault(
        DATA_FMDN_UPLOAD_CACHE, {}
    )

    upload_cache[eid_hex] = UploadCacheEntry(
        eid_hex=eid_hex,
        location=location,
        timestamp=time.time(),
        semantic_area=semantic_area,
    )

    # Cleanup old entries (keep last UPLOAD_CACHE_MAX_ENTRIES)
    if len(upload_cache) > UPLOAD_CACHE_MAX_ENTRIES:
        oldest_keys = sorted(upload_cache.keys(), key=lambda k: upload_cache[k].timestamp)[:UPLOAD_CACHE_CLEANUP_COUNT]
        for key in oldest_keys:
            upload_cache.pop(key, None)
