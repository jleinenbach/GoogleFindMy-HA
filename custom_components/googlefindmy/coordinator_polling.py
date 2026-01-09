"""Pure helper functions for coordinator polling logic.

Phase 11 of coordinator refactoring: Extracted from _async_start_poll_cycle.

These functions handle:
- Location age calculation and logging decisions
- Semantic location coordinate preservation

All functions are pure (no side effects) for easy testing and reuse.
"""

from __future__ import annotations

from typing import Any

# Age thresholds for log level determination
_AGE_THRESHOLD_INFO_HOURS = 24  # Log at INFO level above this
_AGE_THRESHOLD_DEBUG_HOURS = 1  # Log at DEBUG level above this

__all__ = [
    "calculate_location_age_hours",
    "get_age_log_level",
    "should_preserve_previous_coordinates",
]


def calculate_location_age_hours(
    last_seen: float | int | None,
    wall_now: float,
) -> float | None:
    """Calculate the age of location data in hours.

    Args:
        last_seen: The last_seen timestamp (epoch seconds).
        wall_now: The current wall clock time (epoch seconds).

    Returns:
        Age in hours, or None if last_seen is invalid/missing.
        Negative ages are clamped to 0.
    """
    if last_seen is None or last_seen <= 0:
        return None

    age_seconds = wall_now - float(last_seen)
    age_hours = max(0.0, age_seconds / 3600.0)
    return age_hours


def get_age_log_level(age_hours: float) -> tuple[str | None, bool]:
    """Determine the appropriate log level based on location age.

    Returns a tuple of (log_level, should_log):
    - "info" for age > 24 hours (stale data warning)
    - "debug" for 1 < age <= 24 hours (informational)
    - None for age <= 1 hour (no logging needed)

    Args:
        age_hours: The location age in hours.

    Returns:
        Tuple of (log_level, should_log).
    """
    if age_hours > _AGE_THRESHOLD_INFO_HOURS:
        return ("info", True)
    if age_hours > _AGE_THRESHOLD_DEBUG_HOURS:
        return ("debug", True)
    return (None, False)


def should_preserve_previous_coordinates(location: dict[str, Any]) -> bool:
    """Check if a semantic-only location should preserve previous coordinates.

    Returns True when:
    - latitude is None or missing AND
    - longitude is None or missing AND
    - semantic_name is a non-empty string

    This handles the case where the API returns only a semantic location
    (e.g., "Home") without actual coordinates.

    Args:
        location: The location dictionary.

    Returns:
        True if previous coordinates should be preserved.
    """
    lat = location.get("latitude")
    lon = location.get("longitude")

    # If we have valid coordinates, no need to preserve
    if lat is not None and lon is not None:
        return False

    # Check for valid semantic name
    semantic_name = location.get("semantic_name")
    if not isinstance(semantic_name, str) or not semantic_name.strip():
        return False

    return True
