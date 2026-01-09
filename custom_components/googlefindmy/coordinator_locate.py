"""Pure helper functions for coordinator locate operations.

Phase 14 of coordinator refactoring: Extracted from async_locate_device.

These functions handle:
- API response parsing for location data
- Location result validation
- Null island detection
- Cache entry building
- Error classification

All functions are pure (no side effects) for easy testing and reuse.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "build_locate_cache_entry",
    "classify_locate_error",
    "detect_null_island",
    "extract_location_from_response",
    "normalize_locate_coordinates",
    "parse_locate_response",
    "validate_locate_result",
]

# Default threshold for null island detection (in degrees)
_NULL_ISLAND_THRESHOLD = 0.0001


def parse_locate_response(response: dict[str, Any] | None) -> dict[str, Any] | None:
    """Parse location data from API response.

    Extracts location information from a nested API response structure.
    Validates that required coordinate fields are present.

    Args:
        response: API response dictionary.

    Returns:
        Parsed location dict with normalized coordinates, or None if invalid.
    """
    if response is None or not isinstance(response, Mapping):
        return None

    location = response.get("location")
    if location is None or not isinstance(location, Mapping):
        return None

    lat = location.get("latitude")
    lon = location.get("longitude")

    if lat is None or lon is None:
        return None

    # Convert to float if possible
    try:
        lat_float = float(lat)
        lon_float = float(lon)
    except (TypeError, ValueError):
        return None

    result: dict[str, Any] = {
        "latitude": lat_float,
        "longitude": lon_float,
    }

    # Copy optional fields
    optional_fields = (
        "accuracy",
        "altitude",
        "last_seen",
        "timestamp",
        "semantic_name",
        "source",
        "status",
    )
    for field in optional_fields:
        value = location.get(field)
        if value is not None:
            result[field] = value

    return result


def validate_locate_result(
    result: dict[str, Any] | None,
    require_coordinates: bool = True,
    reject_null_island: bool = True,
) -> bool:
    """Validate that a location result is usable.

    Args:
        result: Location result dictionary.
        require_coordinates: If True, lat/lon must be present.
        reject_null_island: If True, reject (0,0) coordinates.

    Returns:
        True if the result is valid and usable.
    """
    if result is None or not isinstance(result, Mapping):
        return False

    lat = result.get("latitude")
    lon = result.get("longitude")
    semantic_name = result.get("semantic_name")

    # Check if we have coordinates
    has_coords = (
        lat is not None
        and lon is not None
        and isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
    )

    # If coordinates required and missing, check for semantic name
    if require_coordinates and not has_coords:
        return False

    # If no coordinates and no semantic name, invalid
    if not has_coords and not semantic_name:
        return False

    # Check for null island if we have coordinates
    if has_coords and reject_null_island:
        if detect_null_island(float(lat), float(lon)):
            return False

    return True


def detect_null_island(
    latitude: float,
    longitude: float,
    threshold: float = _NULL_ISLAND_THRESHOLD,
) -> bool:
    """Detect invalid (0,0) or near-zero coordinates (null island).

    Null island is at coordinates (0, 0) in the Gulf of Guinea, and
    is commonly used as a default/error coordinate. Real locations
    at exactly (0,0) are extremely rare.

    Args:
        latitude: Latitude coordinate.
        longitude: Longitude coordinate.
        threshold: Maximum absolute value to consider as null island.

    Returns:
        True if coordinates are at or near null island.
    """
    return abs(latitude) < threshold and abs(longitude) < threshold


def extract_location_from_response(response: Any) -> dict[str, Any]:
    """Extract location data from various response formats.

    Handles both nested (location key) and flat response structures.
    Prefers nested structure when both are present.

    Args:
        response: API response (dict or other).

    Returns:
        Location dict, or empty dict if no location found.
    """
    if response is None or not isinstance(response, Mapping):
        return {}

    # Try nested location first
    nested = response.get("location")
    if nested is not None and isinstance(nested, Mapping):
        return dict(nested)

    # Try flat structure
    lat = response.get("latitude")
    lon = response.get("longitude")
    if lat is not None or lon is not None:
        result: dict[str, Any] = {}
        for key in (
            "latitude",
            "longitude",
            "accuracy",
            "altitude",
            "last_seen",
            "semantic_name",
        ):
            if key in response:
                result[key] = response[key]
        return result

    return {}


def normalize_locate_coordinates(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize coordinate fields to float values.

    Converts string representations to floats. Invalid values become None.

    Args:
        data: Location data dictionary.

    Returns:
        New dictionary with normalized coordinates.
    """
    result = dict(data)

    for field in ("latitude", "longitude", "accuracy", "altitude"):
        value = result.get(field)
        if value is None:
            continue

        if isinstance(value, (int, float)):
            result[field] = float(value)
        elif isinstance(value, str):
            try:
                result[field] = float(value)
            except ValueError:
                result[field] = None

    return result


def build_locate_cache_entry(
    location_data: dict[str, Any],
    device_id: str,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a cache entry from location data for manual locate.

    Args:
        location_data: Raw location data from API.
        device_id: The device ID.
        timestamp: Optional timestamp for last_updated.

    Returns:
        Cache entry dictionary.
    """
    result = dict(location_data)
    result["device_id"] = device_id
    result["source"] = "manual"

    if timestamp is not None:
        result["last_updated"] = timestamp

    return result


def classify_locate_error(exc: BaseException | None) -> str:  # noqa: PLR0911
    """Classify a locate exception for error handling.

    Categories:
    - "timeout": TimeoutError or asyncio.TimeoutError
    - "connection": ConnectionError, OSError
    - "auth": PermissionError or auth-related message
    - "data": ValueError, KeyError, TypeError
    - "unknown": All other exceptions

    Args:
        exc: The exception to classify.

    Returns:
        Error type string.
    """
    if exc is None:
        return "unknown"

    # Check exception type first
    if isinstance(exc, TimeoutError):
        return "timeout"

    if isinstance(exc, PermissionError):
        return "auth"

    if isinstance(exc, (ConnectionError, OSError)):
        return "connection"

    if isinstance(exc, (ValueError, KeyError, TypeError)):
        return "data"

    # Check message for auth indicators
    err_str = str(exc).lower()
    if any(
        keyword in err_str
        for keyword in ("auth", "expired", "unauthorized", "forbidden", "credential")
    ):
        return "auth"

    return "unknown"
