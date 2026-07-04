"""Pure helper functions for coordinator update logic.

Phase 10 of coordinator refactoring: Extracted from _async_update_data.

These functions handle:
- FCM error classification
- API payload normalization
- Device filtering and deduplication
- Empty list protection
- Poll timing decisions

All functions are pure (no side effects) for easy testing and reuse.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# Word-boundary matchers for HTTP status codes. Using ``\b`` prevents a
# transient message like "retry after 4012 ms" (which contains the substring
# "401") from being misread as an HTTP 401, while still matching a genuine
# "401"/"404" that appears as a standalone token (e.g. "HTTP 401 Unauthorized",
# "Error code: 401", "404 credential not found").
_HTTP_401_TOKEN = re.compile(r"\b401\b")
_HTTP_404_TOKEN = re.compile(r"\b404\b")

__all__ = [
    "calculate_presence_ttl",
    "filter_and_dedupe_devices",
    "is_fatal_fcm_auth_error",
    "is_poll_cycle_due",
    "normalize_device_list_payload",
    "should_defer_empty_list",
]


def is_fatal_fcm_auth_error(error: Any) -> bool:
    """Check if an FCM error is a fatal authentication error.

    Fatal auth errors should trigger re-authentication immediately
    without retry. These include:
    - 401 Unauthorized errors
    - 404 errors mentioning credentials
    - Explicit "credentials invalid" messages
    - "invalid auth" messages

    Args:
        error: The error string to check.

    Returns:
        True if the error is a fatal auth error requiring re-auth.
    """
    if not isinstance(error, str) or not error:
        return False

    error_lower = error.lower()

    # 401 errors are always fatal. Match the status code on a word boundary so a
    # transient throttling message such as "retry after 4012 ms" (substring
    # "401") is not misclassified as an authentication failure.
    if _HTTP_401_TOKEN.search(error):
        return True

    # 404 with credential mention is fatal. Same word-boundary guard so e.g.
    # "retry after 40412 ms" (substring "404") cannot false-fire.
    if _HTTP_404_TOKEN.search(error) and "credential" in error_lower:
        return True

    # Explicit credential/auth failure messages
    if "credentials invalid" in error_lower:
        return True

    if "invalid auth" in error_lower:
        return True

    return False


def normalize_device_list_payload(payload: Any) -> list[Any]:
    """Normalize API payloads to a list of device candidates.

    Handles various payload formats:
    - Mapping with 'devices' key containing list
    - Mapping with 'devices' key containing single device
    - Direct sequence of devices
    - None or invalid payloads

    Args:
        payload: The API response payload.

    Returns:
        List of device candidates (may contain invalid items).
    """
    # Extract devices from mapping if present
    devices_source: Any
    if isinstance(payload, Mapping):
        devices_source = payload.get("devices")
    else:
        devices_source = payload

    # Handle None
    if devices_source is None:
        return []

    # Single device mapping -> wrap in list
    if isinstance(devices_source, Mapping):
        return [devices_source]

    # Sequence (but not string/bytes) -> convert to list
    if isinstance(devices_source, Sequence) and not isinstance(
        devices_source, (str, bytes, bytearray)
    ):
        return list(devices_source)

    # Invalid type
    return []


def filter_and_dedupe_devices(
    candidates: Iterable[Any],
) -> tuple[list[dict[str, Any]], set[str]]:
    """Filter valid devices and remove duplicates.

    Validates each device candidate:
    - Must be a Mapping
    - Must have 'id' key with non-empty string value
    - IDs are stripped of whitespace
    - First occurrence wins for duplicates

    Args:
        candidates: Iterable of potential device items.

    Returns:
        Tuple of (filtered_devices, seen_ids) where:
        - filtered_devices: List of valid device dicts
        - seen_ids: Set of device IDs that were processed
    """
    filtered: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for item in candidates:
        # Must be a mapping
        if not isinstance(item, Mapping):
            continue

        # Convert to regular dict
        normalized = dict(item)

        # Validate ID
        dev_id_value = normalized.get("id")
        if not isinstance(dev_id_value, str) or not dev_id_value.strip():
            continue

        # Normalize ID
        dev_id = dev_id_value.strip()
        normalized["id"] = dev_id

        # Check for duplicate
        if dev_id in seen_ids:
            continue

        seen_ids.add(dev_id)
        filtered.append(normalized)

    return filtered, seen_ids


def should_defer_empty_list(
    streak: int,
    quorum: int,
    has_previous_list: bool,
) -> bool:
    """Determine if an empty device list should be deferred.

    Implements quorum-based protection against transient empty lists.
    Deferral only happens when:
    - Streak is >= 1 (at least one empty list received)
    - Streak is below quorum threshold
    - A previous non-empty list exists to fall back to

    Args:
        streak: Number of consecutive empty lists received.
        quorum: Number of consecutive empties required to accept.
        has_previous_list: Whether a previous non-empty list exists.

    Returns:
        True if the empty list should be deferred (use previous).
    """
    if streak < 1:
        return False

    if streak >= quorum:
        return False

    return has_previous_list


def calculate_presence_ttl(
    effective_interval: float,
    min_ttl: float,
) -> float:
    """Calculate presence time-to-live based on poll interval.

    TTL is set to 2x the effective interval, but never below
    the minimum TTL to ensure reasonable availability windows.

    Args:
        effective_interval: The effective poll interval in seconds.
        min_ttl: Minimum TTL value in seconds.

    Returns:
        Calculated TTL in seconds.
    """
    return max(2 * effective_interval, min_ttl)


def is_poll_cycle_due(
    elapsed: float,
    effective_interval: float,
    predictive_block: bool,
    predictive_due: bool,
    hard_limit_passed: bool,
    is_cold_start: bool,
) -> bool:
    """Determine if a poll cycle should be triggered.

    Poll is due when:
    1. Cold start (first poll with no location data) - always due
    2. Elapsed time >= effective_interval AND not predictively blocked
    3. Predictive due AND hard limit passed AND elapsed >= effective_interval
       (min_poll_interval alone no longer triggers a poll below the chosen
       cadence; effective_interval governs the cadence, min_poll_interval
       stays a pure API safety floor)

    Args:
        elapsed: Time since last poll in seconds.
        effective_interval: Target poll interval in seconds.
        predictive_block: Whether predictive polling has blocked this cycle.
        predictive_due: Whether predictive polling indicates due.
        hard_limit_passed: Whether minimum poll interval has passed.
        is_cold_start: Whether this is a cold start (no location data).

    Returns:
        True if poll cycle should be triggered.
    """
    # Cold start always triggers poll
    if is_cold_start:
        return True

    # Normal interval check (unless predictively blocked)
    if elapsed >= effective_interval and not predictive_block:
        return True

    # Predictive due, but never below the chosen cadence. min_poll_interval
    # stays a pure API safety floor; effective_interval governs the cadence,
    # so predictive pre-fetch no longer pulls polling below effective_interval.
    # hard_limit_passed is redundant while callers keep the invariant
    # effective_interval >= min_poll_interval, but is retained so this pure
    # function stays correct for any caller (do not "simplify" it away).
    if predictive_due and hard_limit_passed and elapsed >= effective_interval:
        return True

    return False
