"""Cache utilities for the coordinator.

This module contains pure functions for cache operations extracted from
coordinator.py for improved testability and maintainability (Phase 3 + 13).

Contents:
- build_base_snapshot_entry(): Create base snapshot from device dict
- determine_location_status(): Determine status string from age
- epoch_to_datetime_utc(): Convert epoch timestamp to UTC datetime
- is_presence_expired(): Check if presence TTL has expired
- should_allow_location_update(): Determine if location update is allowed

Phase 13 additions:
- validate_location_data(): Validate location data has required fields
- merge_location_update(): Merge new location with existing cache
- detect_significant_change(): Detect significant location changes
- select_best_accuracy(): Select better accuracy value
- normalize_location_fields(): Normalize location field types
- preserve_metadata_fields(): Preserve metadata from previous cache
- should_clear_metadata_only_flag(): Determine if flag should be cleared
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "DEFAULT_SNAPSHOT_FIELDS",
    "STATUS_AGING",
    "STATUS_CURRENT",
    "STATUS_STALE",
    "STATUS_WAITING",
    "build_base_snapshot_entry",
    "detect_significant_change",
    "determine_location_status",
    "epoch_to_datetime_utc",
    "is_presence_expired",
    "merge_location_update",
    "normalize_location_fields",
    "preserve_metadata_fields",
    "select_best_accuracy",
    "should_allow_location_update",
    "should_clear_metadata_only_flag",
    "validate_location_data",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default fields for a device snapshot entry
DEFAULT_SNAPSHOT_FIELDS = (
    "name",
    "id",
    "device_id",
    "latitude",
    "longitude",
    "altitude",
    "accuracy",
    "last_seen",
    "status",
    "is_own_report",
    "semantic_name",
    "battery_level",
)

# Status strings for location data freshness
STATUS_CURRENT = "Location data current"
STATUS_AGING = "Location data aging"
STATUS_STALE = "Location data stale"
STATUS_WAITING = "Waiting for location poll"


# ---------------------------------------------------------------------------
# Snapshot Building
# ---------------------------------------------------------------------------


def build_base_snapshot_entry(device_dict: dict[str, Any]) -> dict[str, Any]:
    """Create the base snapshot entry for a device.

    This centralizes the common fields to keep snapshot builders DRY.
    No cache lookups or coordinator state access happens here.

    Args:
        device_dict: A dictionary containing basic device info (id, name).

    Returns:
        A dictionary with default fields for a device snapshot.
    """
    dev_id = device_dict["id"]
    dev_name = device_dict.get("name") or dev_id
    return {
        "name": dev_name,
        "id": dev_id,
        "device_id": dev_id,
        "latitude": None,
        "longitude": None,
        "altitude": None,
        "accuracy": None,
        "last_seen": None,
        "status": STATUS_WAITING,
        "is_own_report": None,
        "semantic_name": None,
        "battery_level": None,
    }


# ---------------------------------------------------------------------------
# Location Status
# ---------------------------------------------------------------------------


def determine_location_status(age: float, poll_interval: float) -> str:
    """Determine the location status string based on data age.

    Args:
        age: Age of the location data in seconds.
        poll_interval: The configured poll interval in seconds.

    Returns:
        One of: "Location data current", "Location data aging",
        or "Location data stale".
    """
    if age < poll_interval:
        return STATUS_CURRENT
    elif age < poll_interval * 2:
        return STATUS_AGING
    else:
        return STATUS_STALE


# ---------------------------------------------------------------------------
# DateTime Conversion
# ---------------------------------------------------------------------------


def epoch_to_datetime_utc(ts: float | int | str | None) -> datetime | None:
    """Convert an epoch timestamp to a timezone-aware UTC datetime.

    Args:
        ts: Epoch timestamp as float, int, string, or None.

    Returns:
        A timezone-aware datetime object in UTC, or None if conversion fails.
    """
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Presence Checking
# ---------------------------------------------------------------------------


def is_presence_expired(
    last_seen_mono: float | None,
    now_mono: float,
    ttl_seconds: float,
) -> bool:
    """Check if a device's presence has expired based on TTL.

    Args:
        last_seen_mono: Monotonic timestamp when device was last seen,
                        or None/0 if never seen.
        now_mono: Current monotonic time.
        ttl_seconds: Time-to-live in seconds before presence expires.

    Returns:
        True if presence has expired, False if still valid.
    """
    if not last_seen_mono:
        return True
    return (now_mono - float(last_seen_mono)) > ttl_seconds


# ---------------------------------------------------------------------------
# Location Update Decision
# ---------------------------------------------------------------------------


def should_allow_location_update(
    existing_seen: int | float | None,
    incoming_seen: int | float | None,
    existing_rank: int | None,
    incoming_rank: int | None,
) -> bool | None:
    """Determine if a location update should be allowed based on timestamps and ranks.

    This implements the core logic for deciding whether to accept an incoming
    location update over an existing cached location.

    Args:
        existing_seen: Epoch timestamp of existing cached location (or None).
        incoming_seen: Epoch timestamp of incoming location (or None).
        existing_rank: Source rank of existing location (higher = more authoritative).
        incoming_rank: Source rank of incoming location.

    Returns:
        True if update should be allowed.
        False if update should be blocked.
        None if the decision requires additional context (e.g., distance check).
    """
    # Case 1: Both have timestamps - compare them
    if existing_seen is not None and incoming_seen is not None:
        if incoming_seen > existing_seen:
            return True
        if incoming_seen < existing_seen:
            return False
        # Same timestamp - compare ranks
        if existing_rank is not None and (
            incoming_rank is None or existing_rank > incoming_rank
        ):
            return False
        return True

    # Case 2: No existing timestamp - allow update
    if existing_seen is None and incoming_seen is not None:
        return True

    # Case 3: Existing has timestamp, incoming doesn't - needs distance check
    if existing_seen is not None and incoming_seen is None:
        return None  # Caller must do distance-based decision

    # Case 4: Neither has timestamp - compare ranks
    if existing_rank is not None and (
        incoming_rank is None or existing_rank > incoming_rank
    ):
        return False

    return True


# ---------------------------------------------------------------------------
# Phase 13: Cache Update Helper Functions
# ---------------------------------------------------------------------------


def validate_location_data(
    data: dict[str, Any],
    required_fields: tuple[str, ...] = ("latitude", "longitude"),
) -> bool:
    """Validate that location data contains required fields with valid types.

    Args:
        data: Location data dictionary.
        required_fields: Tuple of field names that must be present and numeric.

    Returns:
        True if all required fields are present and are int/float.
    """
    for field in required_fields:
        value = data.get(field)
        if value is None:
            return False
        if not isinstance(value, (int, float)):
            return False
    return True


def merge_location_update(
    existing: dict[str, Any] | None,
    update: dict[str, Any],
    preserve_better_accuracy: bool = True,
) -> dict[str, Any]:
    """Merge a new location update with existing cache data.

    Args:
        existing: Existing cache entry (or None).
        update: New location data to merge.
        preserve_better_accuracy: If True, keep better (lower) accuracy.

    Returns:
        Merged location dictionary.
    """
    if existing is None:
        return dict(update)

    result = dict(existing)
    result.update(update)

    if preserve_better_accuracy:
        old_acc = existing.get("accuracy")
        new_acc = update.get("accuracy")
        if old_acc is not None and new_acc is not None:
            if old_acc < new_acc:  # Lower is better
                result["accuracy"] = old_acc

    return result


def _haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in meters using Haversine formula."""
    r = 6371000  # Earth's radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def detect_significant_change(
    existing: dict[str, Any] | None,
    new: dict[str, Any],
    min_distance_meters: float = 50.0,
    min_timestamp_diff_seconds: float = 60.0,
    min_accuracy_improvement: float = 0.0,
) -> bool:
    """Detect if a location change is significant enough to warrant an update.

    A change is significant if:
    - No existing data exists
    - New data has coordinates when existing doesn't
    - Distance change exceeds min_distance_meters
    - Timestamp change exceeds min_timestamp_diff_seconds
    - Accuracy improves by at least min_accuracy_improvement

    Args:
        existing: Existing cache entry (or None).
        new: New location data.
        min_distance_meters: Minimum distance change to be significant.
        min_timestamp_diff_seconds: Minimum timestamp difference to be significant.
        min_accuracy_improvement: Minimum accuracy improvement to be significant.

    Returns:
        True if the change is significant.
    """
    # No existing data - always significant
    if existing is None:
        return True

    # Check if new data has valid coordinates
    new_lat = new.get("latitude")
    new_lon = new.get("longitude")
    new_has_coords = (
        new_lat is not None
        and new_lon is not None
        and isinstance(new_lat, (int, float))
        and isinstance(new_lon, (int, float))
    )

    # New data lacks coordinates - not significant
    if not new_has_coords:
        return False

    # Check existing coordinates
    existing_lat = existing.get("latitude")
    existing_lon = existing.get("longitude")
    existing_has_coords = (
        existing_lat is not None
        and existing_lon is not None
        and isinstance(existing_lat, (int, float))
        and isinstance(existing_lon, (int, float))
    )

    # New has coordinates, existing doesn't - significant
    if not existing_has_coords:
        return True

    # Check distance
    distance = _haversine_distance(
        float(existing_lat), float(existing_lon), float(new_lat), float(new_lon)
    )
    if distance >= min_distance_meters:
        return True

    # Check timestamp difference
    new_ts = new.get("last_seen")
    existing_ts = existing.get("last_seen")
    if new_ts is not None and existing_ts is not None:
        try:
            ts_diff = abs(float(new_ts) - float(existing_ts))
            if ts_diff >= min_timestamp_diff_seconds:
                return True
        except (TypeError, ValueError):
            pass

    # Check accuracy improvement
    if min_accuracy_improvement > 0:
        new_acc = new.get("accuracy")
        existing_acc = existing.get("accuracy")
        if new_acc is not None and existing_acc is not None:
            try:
                improvement = float(existing_acc) - float(new_acc)
                if improvement >= min_accuracy_improvement:
                    return True
            except (TypeError, ValueError):
                pass

    return False


def select_best_accuracy(
    accuracy1: float | int | None,
    accuracy2: float | int | None,
) -> float | int | None:
    """Select the better (lower) accuracy value from two options.

    Lower accuracy values represent better precision.

    Args:
        accuracy1: First accuracy value (or None).
        accuracy2: Second accuracy value (or None).

    Returns:
        The lower (better) accuracy, or None if both are None.
    """
    if accuracy1 is None and accuracy2 is None:
        return None
    if accuracy1 is None:
        return accuracy2
    if accuracy2 is None:
        return accuracy1
    return min(accuracy1, accuracy2)


def normalize_location_fields(
    data: dict[str, Any],
    fields: Sequence[str] = ("latitude", "longitude", "accuracy", "altitude"),
) -> dict[str, Any]:
    """Normalize location field types to float.

    Converts string representations of numbers to floats.
    Invalid values become None.

    Args:
        data: Location data dictionary.
        fields: Field names to normalize.

    Returns:
        New dictionary with normalized fields.
    """
    result = dict(data)

    for field in fields:
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


def preserve_metadata_fields(
    previous: dict[str, Any] | Mapping[str, Any] | None,
    current: dict[str, Any],
    metadata_keys: Sequence[str],
) -> dict[str, Any]:
    """Preserve metadata fields from previous cache when missing in current.

    Deep copies dict and list values to avoid shared references.

    Args:
        previous: Previous cache entry (or None).
        current: Current/incoming data.
        metadata_keys: Field names to preserve from previous.

    Returns:
        New dictionary with preserved metadata fields.
    """
    result = dict(current)

    if previous is None or not isinstance(previous, Mapping):
        return result

    for key in metadata_keys:
        # Only preserve if current doesn't have the value
        if key in result and result[key] is not None:
            continue

        cached_value = previous.get(key)
        if cached_value is None:
            continue

        # Deep copy mutable types
        if isinstance(cached_value, dict):
            result[key] = dict(cached_value)
        elif isinstance(cached_value, list):
            result[key] = list(cached_value)
        else:
            result[key] = cached_value

    return result


def should_clear_metadata_only_flag(
    data: dict[str, Any],
    incoming_metadata_only: bool | None,
) -> bool:
    """Determine if the metadata_only flag should be cleared from data.

    The flag should be cleared when:
    - incoming_metadata_only is explicitly False, OR
    - data has location payload AND incoming_metadata_only is not explicitly True

    Args:
        data: Location data dictionary.
        incoming_metadata_only: The incoming metadata_only value (True/False/None).

    Returns:
        True if the metadata_only flag should be cleared.
    """
    # Not set - nothing to clear
    if not data.get("metadata_only"):
        return False

    # Explicit False clears the flag
    if incoming_metadata_only is False:
        return True

    # Explicit True preserves the flag
    if incoming_metadata_only is True:
        return False

    # Check if there's location data
    has_location = data.get("latitude") is not None or data.get("longitude") is not None

    return has_location
