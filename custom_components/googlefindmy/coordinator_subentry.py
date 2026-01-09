"""Subentry utilities for the coordinator.

This module contains pure functions for subentry operations extracted from
coordinator.py for improved testability and maintainability (Phase 5).

Contents:
- sanitize_subentry_identifier(): Normalize subentry identifiers
- normalize_epoch_seconds(): Epoch normalization with ms/s tolerance
- format_epoch_utc(): Format epoch to ISO 8601 UTC string
- parse_last_seen_timestamp(): Parse timestamp from various formats
- group_devices_by_subentry(): Group devices by subentry key
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "format_epoch_utc",
    "group_devices_by_subentry",
    "normalize_epoch_seconds",
    "parse_last_seen_timestamp",
    "sanitize_subentry_identifier",
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
