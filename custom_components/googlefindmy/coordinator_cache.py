"""Cache utilities for the coordinator.

This module contains pure functions for cache operations extracted from
coordinator.py for improved testability and maintainability (Phase 3).

Contents:
- build_base_snapshot_entry(): Create base snapshot from device dict
- determine_location_status(): Determine status string from age
- epoch_to_datetime_utc(): Convert epoch timestamp to UTC datetime
- is_presence_expired(): Check if presence TTL has expired
- should_allow_location_update(): Determine if location update is allowed
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

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
