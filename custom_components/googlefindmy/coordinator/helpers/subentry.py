"""Subentry utilities for the coordinator.

This module contains pure functions for subentry operations extracted from
coordinator.py for improved testability and maintainability (Phases 5 & 6).

Contents (Phase 5):
- sanitize_subentry_identifier(): Normalize subentry identifiers
- normalize_epoch_seconds(): Epoch normalization with ms/s tolerance
- format_epoch_utc(): Format epoch to ISO 8601 UTC string
- parse_last_seen_timestamp(): Parse timestamp from various formats
- group_devices_by_subentry(): Group devices by subentry key

Contents (Phase 6 - _refresh_subentry_index helpers):
- filter_provisional_identifier(): Filter provisional subentry IDs
- extract_subentry_group_key(): Extract group key from subentry data
- detect_missing_core_subentry_keys(): Detect missing core keys
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

__all__ = [
    # Phase 5
    "format_epoch_utc",
    "group_devices_by_subentry",
    "normalize_epoch_seconds",
    "parse_last_seen_timestamp",
    "sanitize_subentry_identifier",
    # Phase 6
    "detect_missing_core_subentry_keys",
    "extract_subentry_group_key",
    "filter_provisional_identifier",
]


# ---------------------------------------------------------------------------
# Subentry Identifier Sanitization
# ---------------------------------------------------------------------------


def sanitize_subentry_identifier(candidate: Any) -> str | None:
    """Return a normalized subentry identifier or None when invalid.

    Strips whitespace and returns None for non-string or empty inputs.

    Args:
        candidate: Potential subentry identifier (any type).

    Returns:
        Normalized string identifier, or None if invalid.
    """
    if not isinstance(candidate, str):
        return None

    normalized = candidate.strip()
    if not normalized:
        return None

    return normalized


# ---------------------------------------------------------------------------
# Epoch Normalization
# ---------------------------------------------------------------------------


def normalize_epoch_seconds(value: Any) -> int | None:
    """Return epoch seconds as an int with millisecond tolerance.

    Automatically detects and converts millisecond timestamps (values >= 1e11
    in absolute value) to seconds.

    Args:
        value: Timestamp as int, float, string, or other type.

    Returns:
        Epoch seconds as int, or None if conversion fails.
    """
    # Reject byte-like inputs explicitly. ``float(b"123")`` would otherwise
    # silently succeed for ASCII-numeric bytes while raising on arbitrary
    # payloads, producing an inconsistent contract for callers that pass raw
    # network/cache data without decoding (parse-don't-validate; defends the
    # invariant the docstring already implies).
    if isinstance(value, (bytes, bytearray, memoryview)):
        return None

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None

    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None

    if not math.isfinite(ts):
        return None

    # Convert milliseconds to seconds
    if abs(ts) >= 1e11:
        ts /= 1000.0

    try:
        return int(ts)
    except (OverflowError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Epoch Formatting
# ---------------------------------------------------------------------------


def format_epoch_utc(value: Any) -> str | None:
    """Return an ISO 8601 UTC timestamp for epoch values (seconds or ms).

    Normalizes the input and formats as ISO 8601 with 'Z' suffix.

    Args:
        value: Epoch timestamp (seconds or milliseconds).

    Returns:
        ISO 8601 formatted string with 'Z' suffix, or None if invalid.
    """
    ts = normalize_epoch_seconds(value)
    if ts is None:
        return None

    try:
        dt = datetime.fromtimestamp(ts, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None

    return dt.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Timestamp Parsing
# ---------------------------------------------------------------------------


def parse_last_seen_timestamp(value: Any) -> float | None:
    """Parse a last_seen candidate into epoch seconds.

    Supports:
    - Numeric timestamps (int/float)
    - String timestamps (numeric or ISO 8601)
    - Millisecond timestamps (auto-converted)

    Args:
        value: Timestamp in various formats.

    Returns:
        Epoch seconds as float, or None if parsing fails.
    """
    # Try numeric parsing first
    ts = normalize_epoch_seconds(value)
    if ts is not None:
        return float(ts)

    # Try ISO 8601 parsing for strings
    if isinstance(value, str):
        try:
            # Replace Z with +00:00 for fromisoformat compatibility
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    return None


# ---------------------------------------------------------------------------
# Device Grouping
# ---------------------------------------------------------------------------


def group_devices_by_subentry(
    devices: Sequence[Any],
    device_to_subentry: Mapping[str, str],
    fallback_key: str,
    subentry_keys: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Group device entries by their assigned subentry key.

    Pure function that groups a sequence of device dicts by their
    subentry assignment, with fallback for unassigned devices.

    Args:
        devices: Sequence of device dicts with 'device_id' or 'id' field.
        device_to_subentry: Mapping of device_id to subentry key.
        fallback_key: Default subentry key for unassigned devices.
        subentry_keys: Set of all valid subentry keys to include in result.

    Returns:
        Dict mapping subentry keys to lists of device dicts (copies).
    """
    # Initialize all groups
    grouped: dict[str, list[dict[str, Any]]] = {key: [] for key in subentry_keys}
    grouped.setdefault(fallback_key, [])

    for row in devices:
        if not isinstance(row, Mapping):
            continue

        # Get device ID from either field
        dev_id_raw = row.get("device_id") or row.get("id")
        if not isinstance(dev_id_raw, str):
            continue

        # Determine target subentry
        target_key = device_to_subentry.get(dev_id_raw, fallback_key)
        grouped.setdefault(target_key, []).append(dict(row))

    return grouped


# ---------------------------------------------------------------------------
# Phase 6: _refresh_subentry_index Helpers
# ---------------------------------------------------------------------------


def filter_provisional_identifier(
    identifier: str | None,
    group_key: str,
    entry_subentry_id: str | None,
    core_service_key: str = "core_service",
    core_tracker_key: str = "core_tracking",
) -> tuple[str | None, bool]:
    """Filter provisional subentry identifiers.

    Provisional identifiers (ending with '-provisional') are filtered to None
    unless they match the entry's stable subentry ID.

    Args:
        identifier: The subentry identifier to filter.
        group_key: The group key for this subentry.
        entry_subentry_id: The entry's stable subentry ID for this group.
        core_service_key: The key for service subentries.
        core_tracker_key: The key for tracker subentries.

    Returns:
        Tuple of (filtered_identifier, was_provisional_filtered).
        was_provisional_filtered is True if a provisional ID was filtered.
    """
    if identifier is None:
        return None, False

    if not identifier.endswith("-provisional"):
        return identifier, False

    # Check if this provisional matches the entry's stable ID
    if group_key == core_service_key and identifier == entry_subentry_id:
        return identifier, False
    if group_key == core_tracker_key and identifier == entry_subentry_id:
        return identifier, False

    # Filter out non-matching provisional IDs
    return None, True


def extract_subentry_group_key(
    data: Mapping[str, Any] | None,
    subentry_id: str | None,
    fallback: str = "core_tracking",
) -> str:
    """Extract the group key from subentry data.

    Falls back to subentry_id, then to the fallback value.

    Args:
        data: Subentry data dict containing optional 'group_key'.
        subentry_id: Fallback subentry ID.
        fallback: Final fallback value if nothing else found.

    Returns:
        The extracted group key as a string.
    """
    if data is not None:
        group_key = data.get("group_key")
        if group_key is not None:
            return str(group_key)

    if subentry_id is not None:
        return str(subentry_id)

    return fallback


def detect_missing_core_subentry_keys(
    present_keys: set[str],
    service_key: str = "core_service",
    tracker_key: str = "core_tracking",
) -> set[str]:
    """Detect which core subentry keys are missing.

    Args:
        present_keys: Set of currently present subentry group keys.
        service_key: The service subentry key.
        tracker_key: The tracker subentry key.

    Returns:
        Set of missing core keys.
    """
    required = {service_key, tracker_key}
    return required - present_keys
