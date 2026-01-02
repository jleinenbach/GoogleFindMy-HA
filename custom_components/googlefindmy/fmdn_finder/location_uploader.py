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
from typing import TYPE_CHECKING

from homeassistant.components.zone import async_active_zone
from homeassistant.const import STATE_HOME

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

_LOGGER = logging.getLogger(__name__)

# Upload throttling constants (per FMDN spec Section 4)
MIN_UPLOAD_DISTANCE_METERS = 50  # Minimum distance change to trigger upload
MIN_UPLOAD_INTERVAL_SECONDS = 300  # 5 minutes minimum between uploads
MIN_UPLOAD_INTERVAL_SCREEN_OFF = 300  # Additional delay when screen off
MIN_UPLOAD_INTERVAL_BATTERY_SAVER = 900  # 15 minutes in battery saver mode

# Accuracy thresholds
MAX_ZONE_ACCURACY_METERS = 100  # Zone-based location accuracy
DEFAULT_HOME_ZONE_ACCURACY = 50  # Default home zone accuracy

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


async def async_process_fmdn_beacon_detection(
    hass: HomeAssistant,
    *,
    eid: bytes,
    area: str | None,
    rssi: int | None,
    scanner_address: str | None,
    scanner_device_id: str | None,
    fmdn_device_id: str | None,
    entity_id: str,
) -> None:
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
    """
    eid_hex = eid.hex()
    _LOGGER.debug(
        "Processing FMDN beacon: EID=%s..., area=%s, RSSI=%s",
        eid_hex[:8],
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
        return

    _LOGGER.debug(
        "Resolved location: lat=%.6f, lon=%.6f, accuracy=%dm, zone=%s",
        location.latitude,
        location.longitude,
        location.accuracy,
        location.zone_name,
    )

    # 2. Check upload throttling
    should_upload, reason = await _should_upload_location(hass, eid_hex, location)

    if not should_upload:
        _LOGGER.debug("Upload throttled for EID %s...: %s", eid_hex[:8], reason)
        return

    _LOGGER.info(
        "Uploading FMDN location report: EID=%s..., zone=%s, accuracy=%dm",
        eid_hex[:8],
        location.zone_name,
        location.accuracy,
    )

    # 3. Calculate accuracy from RSSI (if available)
    if rssi is not None:
        rssi_accuracy = _calculate_accuracy_from_rssi(rssi, location.accuracy)
        location.accuracy = max(location.accuracy, rssi_accuracy)

    # 4. Encrypt and upload
    try:
        await _encrypt_and_upload_location(hass, eid, location)

        # Update cache on successful upload
        _update_upload_cache(hass, eid_hex, location)

        _LOGGER.info("FMDN location report uploaded successfully for EID %s...", eid_hex[:8])

    except Exception as err:  # noqa: BLE001
        _LOGGER.error(
            "Failed to upload FMDN location report for EID %s...: %s",
            eid_hex[:8],
            err,
            exc_info=True,
        )


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


async def _should_upload_location(
    hass: HomeAssistant,
    eid_hex: str,
    new_location: LocationData,
) -> tuple[bool, str]:
    """Check if location should be uploaded (throttling per FMDN spec).

    FMDN Specification (Section 4, Appendix A3):
    - Upload if distance_grew OR accuracy_improved OR sufficient_time_elapsed
    - Additional delays when screen_off or battery_saver mode

    Args:
        hass: Home Assistant instance
        eid_hex: EID as hex string (cache key)
        new_location: New location to upload

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

    if elapsed < MIN_UPLOAD_INTERVAL_SECONDS:
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

    if rssi > -60:
        estimated_distance = 2  # Very close
    elif rssi > -70:
        estimated_distance = 5  # Close
    elif rssi > -80:
        estimated_distance = 10  # Medium
    elif rssi > -90:
        estimated_distance = 20  # Far
    else:
        estimated_distance = 30  # Very far

    # Return maximum of RSSI-based and zone-based accuracy
    return max(estimated_distance, zone_accuracy)


async def _encrypt_and_upload_location(
    hass: HomeAssistant,
    eid: bytes,
    location: LocationData,
) -> None:
    """Encrypt location and upload to Google FMDN backend.

    Uses existing crypto primitives from foreign_tracker_cryptor.py
    and protobuf definitions from LocationReportsUpload_pb2.

    Args:
        hass: Home Assistant instance
        eid: Ephemeral Identity Key (20 or 32 bytes)
        location: Location to upload

    Raises:
        ImportError: If crypto or protobuf modules not available
        ValueError: If encryption fails
    """
    # Lazy imports to avoid loading heavy crypto libraries at startup
    from ..FMDNCrypto.foreign_tracker_cryptor import encrypt
    from ..ProtoDecoders.LocationReportsUpload_pb2 import (
        LocationReportsUpload,
        LocationReport,
        Report,
    )

    # 1. Create LocationReport protobuf
    location_proto = LocationReport()
    location_proto.latitudeE7 = int(location.latitude * 1e7)
    location_proto.longitudeE7 = int(location.longitude * 1e7)
    location_proto.accuracyMeters = location.accuracy
    location_proto.timestampMs = int(time.time() * 1000)

    location_bytes = location_proto.SerializeToString()

    _LOGGER.debug(
        "Location protobuf: %d bytes, lat=%d, lon=%d, acc=%dm",
        len(location_bytes),
        location_proto.latitudeE7,
        location_proto.longitudeE7,
        location_proto.accuracyMeters,
    )

    # 2. Encrypt with EID public key (ECDH + AES-EAX)
    random_bytes = secrets.token_bytes(32)

    try:
        encrypted_and_tag, Sx = encrypt(
            message=location_bytes,
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

    # 3. Create LocationReportsUpload protobuf
    upload = LocationReportsUpload()
    upload.random1 = secrets.randbits(64)
    upload.random2 = secrets.randbits(64)

    report = upload.reports.add()
    report.advertisement.identifier.truncatedEid = eid[:10]
    report.advertisement.unwantedTrackingModeEnabled = 0
    report.time.seconds = int(time.time())

    # Note: The protobuf structure may need adjustment based on actual FMDN API
    # Current structure based on LocationReportsUpload.proto
    # Encrypted data and ephemeral key need to be embedded correctly
    # This is a placeholder - actual field mapping TBD from Google API analysis

    upload_bytes = upload.SerializeToString()

    _LOGGER.debug("Upload protobuf: %d bytes", len(upload_bytes))

    # 4. Upload to Google FMDN backend
    from .google_uploader import async_upload_to_google_fmdn

    await async_upload_to_google_fmdn(hass, upload_bytes, eid[:10].hex())


def _update_upload_cache(hass: HomeAssistant, eid_hex: str, location: LocationData) -> None:
    """Update upload cache with latest upload timestamp and location.

    Args:
        hass: Home Assistant instance
        eid_hex: EID as hex string
        location: Uploaded location
    """
    upload_cache: dict[str, UploadCacheEntry] = hass.data.setdefault(
        DATA_FMDN_UPLOAD_CACHE, {}
    )

    upload_cache[eid_hex] = UploadCacheEntry(
        eid_hex=eid_hex,
        location=location,
        timestamp=time.time(),
    )

    # Cleanup old entries (keep last 100)
    if len(upload_cache) > 100:
        oldest_keys = sorted(upload_cache.keys(), key=lambda k: upload_cache[k].timestamp)[:50]
        for key in oldest_keys:
            upload_cache.pop(key, None)
