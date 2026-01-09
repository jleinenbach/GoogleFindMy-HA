"""Cache utilities for the coordinator.

This module contains pure functions for cache operations extracted from
coordinator.py for improved testability and maintainability (Phase 3 + 15).

Contents:
- build_base_snapshot_entry(): Create base snapshot from device dict
- determine_location_status(): Determine status string from age
- epoch_to_datetime_utc(): Convert epoch timestamp to UTC datetime
- is_presence_expired(): Check if presence TTL has expired
- should_allow_location_update(): Determine if location update is allowed
- normalize_location_fields(): Normalize location field types
- preserve_metadata_fields(): Preserve metadata from previous cache
- should_clear_metadata_only_flag(): Determine if flag should be cleared

Phase 15 additions:
- compare_location_freshness(): Compare two timestamps for freshness
- select_best_location_source(): Select best source based on priority
- preserve_monotonic_timestamp(): Preserve monotonic timestamps
- fill_missing_coordinates(): Fill missing coordinate fields
- merge_cache_row(): Orchestrating function for cache row merging
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .geo import haversine_distance

__all__ = [
    "DEFAULT_SNAPSHOT_FIELDS",
    "LOCATION_FIELDS",
    "SOURCE_PRIORITY",
    "STATUS_AGING",
    "STATUS_CURRENT",
    "STATUS_STALE",
    "STATUS_WAITING",
    "build_base_snapshot_entry",
    "compare_location_freshness",
    "determine_location_status",
    "epoch_to_datetime_utc",
    "fill_missing_coordinates",
    "is_presence_expired",
    "merge_cache_row",
    "normalize_location_fields",
    "preserve_metadata_fields",
    "preserve_monotonic_timestamp",
    "select_best_location_source",
    "should_allow_location_update",
    "should_clear_metadata_only_flag",
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

# Location fields that should be blocked during location update rejection
LOCATION_FIELDS = frozenset(
    {
        "latitude",
        "longitude",
        "accuracy",
        "altitude",
        "status",
        "source_label",
        "source_rank",
        "is_own_report",
        "last_seen",
        "last_seen_utc",
    }
)

# Source priority for location data (higher = more authoritative)
SOURCE_PRIORITY: dict[str, int] = {
    "api": 10,
    "poll": 8,
    "fcm": 5,
    "cache": 2,
    "unknown": 0,
}

# Default significant change threshold in meters
_DEFAULT_SIGNIFICANT_CHANGE_M = 50.0

# Epsilon for timestamp comparison (floating point tolerance)
_TIMESTAMP_EPSILON = 0.001


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


# ---------------------------------------------------------------------------
# Phase 15: Cache Row Merge Helper Functions
# ---------------------------------------------------------------------------


def _normalize_epoch_seconds(value: Any) -> float | None:
    """Normalize a timestamp value to epoch seconds as float.

    Args:
        value: Timestamp value (float, int, str, or None).

    Returns:
        Normalized float timestamp or None.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    """Coerce a value to float.

    Args:
        value: Value to coerce.

    Returns:
        Float value or None if coercion fails.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compare_location_freshness(
    existing_ts: float | int | None,
    new_ts: float | int | None,
) -> int:
    """Compare two timestamps to determine relative freshness.

    Args:
        existing_ts: Existing location timestamp (epoch seconds).
        new_ts: New location timestamp (epoch seconds).

    Returns:
        1 if new is fresher (newer), -1 if existing is fresher, 0 if equal.
    """
    existing_norm = _normalize_epoch_seconds(existing_ts)
    new_norm = _normalize_epoch_seconds(new_ts)

    if existing_norm is None and new_norm is None:
        return 0
    if existing_norm is None:
        return 1  # New has timestamp, existing doesn't
    if new_norm is None:
        return -1  # Existing has timestamp, new doesn't

    # Compare with epsilon tolerance
    if abs(new_norm - existing_norm) < _TIMESTAMP_EPSILON:
        return 0
    if new_norm > existing_norm:
        return 1
    return -1


def select_best_location_source(
    source_a: str | None,
    source_b: str | None,
) -> str:
    """Select the best location source based on priority.

    Higher priority sources are more authoritative.

    Args:
        source_a: First source identifier.
        source_b: Second source identifier.

    Returns:
        The source with higher priority, or 'unknown' if both are None.
    """
    prio_a = SOURCE_PRIORITY.get(source_a or "unknown", 0)
    prio_b = SOURCE_PRIORITY.get(source_b or "unknown", 0)

    if prio_a >= prio_b:
        return source_a or "unknown"
    return source_b or "unknown"


def preserve_monotonic_timestamp(
    existing: dict[str, Any],
    merged: dict[str, Any],
) -> dict[str, Any]:
    """Ensure timestamps are monotonically preserved.

    When incoming data has an older or missing timestamp, preserve the
    existing timestamp to maintain monotonicity.

    Args:
        existing: Existing cache entry.
        merged: Merged data that may have older timestamps.

    Returns:
        New dictionary with monotonically preserved timestamps.
    """
    result = dict(merged)

    existing_seen = _normalize_epoch_seconds(existing.get("last_seen"))
    merged_seen = _normalize_epoch_seconds(merged.get("last_seen"))

    # Preserve existing timestamp if it's newer
    if existing_seen is not None and (
        merged_seen is None or merged_seen < existing_seen
    ):
        result["last_seen"] = existing.get("last_seen")
        if existing.get("last_seen_utc") is not None:
            result["last_seen_utc"] = existing.get("last_seen_utc")
    elif (
        merged_seen is not None
        and existing_seen is not None
        and abs(merged_seen - existing_seen) < _TIMESTAMP_EPSILON
        and result.get("last_seen_utc") is None
        and existing.get("last_seen_utc") is not None
    ):
        # Same timestamp but merged lacks UTC - preserve existing UTC
        result["last_seen_utc"] = existing.get("last_seen_utc")

    return result


def fill_missing_coordinates(
    existing: dict[str, Any],
    merged: dict[str, Any],
) -> dict[str, Any]:
    """Fill missing coordinate fields from existing data.

    Args:
        existing: Existing cache entry with coordinates.
        merged: Merged data that may have missing coordinates.

    Returns:
        New dictionary with missing coordinates filled.
    """
    result = dict(merged)

    for coord_field in ("latitude", "longitude", "accuracy", "altitude"):
        if result.get(coord_field) is None and existing.get(coord_field) is not None:
            result[coord_field] = existing.get(coord_field)

    return result


def merge_cache_row(
    existing: dict[str, Any] | None,
    incoming: dict[str, Any],
    significant_change_meters: float = _DEFAULT_SIGNIFICANT_CHANGE_M,
) -> dict[str, Any]:
    """Merge incoming location data with existing cache row.

    This is the main orchestrating function that combines all the merge logic:
    1. Determine if location update should be allowed based on timestamps/ranks
    2. If allowed, merge all fields; if not, only merge non-location fields
    3. Preserve monotonic timestamps
    4. Fill missing coordinates from existing

    Args:
        existing: Existing cache entry (or None).
        incoming: Incoming location data.
        significant_change_meters: Distance threshold for significant change.

    Returns:
        Merged cache row dictionary.
    """
    if not existing:
        return dict(incoming)

    # Get normalized timestamps and ranks
    existing_seen = _normalize_epoch_seconds(existing.get("last_seen"))
    incoming_seen = _normalize_epoch_seconds(incoming.get("last_seen"))
    existing_rank = existing.get("source_rank")
    incoming_rank = incoming.get("source_rank")

    # Determine if location update should be allowed
    allow_update = should_allow_location_update(
        existing_seen, incoming_seen, existing_rank, incoming_rank
    )

    # Handle case where distance check is needed (allow_update is None)
    if allow_update is None:
        # Need to check distance for significant change
        incoming_lat = _coerce_float(incoming.get("latitude"))
        incoming_lon = _coerce_float(incoming.get("longitude"))
        existing_lat = _coerce_float(existing.get("latitude"))
        existing_lon = _coerce_float(existing.get("longitude"))

        if (
            incoming_lat is not None
            and incoming_lon is not None
            and existing_lat is not None
            and existing_lon is not None
        ):
            try:
                dist = haversine_distance(
                    existing_lat, existing_lon, incoming_lat, incoming_lon
                )
                allow_update = dist > significant_change_meters
            except Exception:
                allow_update = False
        else:
            allow_update = False

    # Build merged result
    merged = dict(existing)

    if allow_update:
        # Update all fields from incoming
        merged.update(incoming)
    else:
        # Only update non-location fields
        for key, value in incoming.items():
            if key not in LOCATION_FIELDS:
                merged[key] = value

    # Preserve monotonic timestamps
    merged = preserve_monotonic_timestamp(existing, merged)

    # Fill missing coordinates
    merged = fill_missing_coordinates(existing, merged)

    return merged
