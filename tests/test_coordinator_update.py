"""Tests for coordinator_update.py - Phase 10 of coordinator refactoring.

These tests validate the update helper functions extracted from coordinator.py
into coordinator_update.py.

Test categories:
1. is_fatal_fcm_auth_error - Detect fatal FCM authentication errors
2. normalize_device_list_payload - Normalize API payloads to device list
3. filter_and_dedupe_devices - Filter valid devices and remove duplicates
4. should_defer_empty_list - Empty list quorum logic
5. calculate_presence_ttl - Calculate presence time-to-live
6. is_poll_cycle_due - Determine if poll cycle should trigger

REQUIREMENT: 100% test coverage for all extracted functions.

KEY RISKS COVERED:
- FCM error detection for auth failure vs transient errors
- Malformed API payloads (None, Mapping, Sequence, invalid types)
- Device deduplication and ID validation
- Empty list protection with quorum logic
- Poll timing edge cases (cold start, predictive blocking)
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.coordinator.helpers.update import (
    calculate_presence_ttl,
    filter_and_dedupe_devices,
    is_fatal_fcm_auth_error,
    is_poll_cycle_due,
    normalize_device_list_payload,
    should_defer_empty_list,
)

# ---------------------------------------------------------------------------
# is_fatal_fcm_auth_error Tests
# ---------------------------------------------------------------------------


class TestIsFatalFcmAuthError:
    """Tests for is_fatal_fcm_auth_error function.

    Detects fatal FCM authentication errors that should trigger re-auth
    immediately without retry.
    """

    def test_401_error_is_fatal(self) -> None:
        """401 errors should be considered fatal."""
        assert is_fatal_fcm_auth_error("HTTP 401 Unauthorized") is True
        assert is_fatal_fcm_auth_error("Error code: 401") is True

    def test_404_with_credential_is_fatal(self) -> None:
        """404 errors mentioning credentials should be fatal."""
        assert is_fatal_fcm_auth_error("404 credential not found") is True
        assert is_fatal_fcm_auth_error("404 - Credential issue") is True

    def test_404_without_credential_not_fatal(self) -> None:
        """404 errors without credential mention are not fatal."""
        assert is_fatal_fcm_auth_error("404 Not Found") is False
        assert is_fatal_fcm_auth_error("HTTP 404 endpoint missing") is False

    def test_credentials_invalid_is_fatal(self) -> None:
        """Explicit credentials invalid message is fatal."""
        assert is_fatal_fcm_auth_error("Credentials invalid for user") is True
        assert is_fatal_fcm_auth_error("CREDENTIALS INVALID") is True

    def test_invalid_auth_is_fatal(self) -> None:
        """Invalid auth message is fatal."""
        assert is_fatal_fcm_auth_error("Invalid auth token") is True
        assert is_fatal_fcm_auth_error("INVALID AUTH") is True

    def test_transient_errors_not_fatal(self) -> None:
        """Transient errors should not be considered fatal."""
        assert is_fatal_fcm_auth_error("Connection timeout") is False
        assert is_fatal_fcm_auth_error("HTTP 500 Server Error") is False
        assert is_fatal_fcm_auth_error("Network unreachable") is False

    def test_empty_string_not_fatal(self) -> None:
        """Empty string should not be fatal."""
        assert is_fatal_fcm_auth_error("") is False

    def test_none_not_fatal(self) -> None:
        """None should not be fatal."""
        assert is_fatal_fcm_auth_error(None) is False

    def test_non_string_not_fatal(self) -> None:
        """Non-string types should not be fatal."""
        assert is_fatal_fcm_auth_error(401) is False
        assert is_fatal_fcm_auth_error(["401"]) is False


# ---------------------------------------------------------------------------
# normalize_device_list_payload Tests
# ---------------------------------------------------------------------------


class TestNormalizeDeviceListPayload:
    """Tests for normalize_device_list_payload function.

    Normalizes API payloads that may arrive as mappings or sequences
    into a list of device candidates.
    """

    def test_mapping_with_devices_key(self) -> None:
        """Should extract devices from mapping with 'devices' key."""
        payload = {"devices": [{"id": "dev1"}, {"id": "dev2"}]}
        result = normalize_device_list_payload(payload)
        assert result == [{"id": "dev1"}, {"id": "dev2"}]

    def test_mapping_devices_as_single_dict(self) -> None:
        """Should wrap single device mapping in list."""
        payload = {"devices": {"id": "single-dev"}}
        result = normalize_device_list_payload(payload)
        assert result == [{"id": "single-dev"}]

    def test_direct_sequence(self) -> None:
        """Should handle direct sequence (not wrapped in mapping)."""
        payload = [{"id": "dev1"}, {"id": "dev2"}]
        result = normalize_device_list_payload(payload)
        assert result == [{"id": "dev1"}, {"id": "dev2"}]

    def test_none_payload(self) -> None:
        """Should return empty list for None payload."""
        result = normalize_device_list_payload(None)
        assert result == []

    def test_none_devices_key(self) -> None:
        """Should return empty list when devices key is None."""
        payload = {"devices": None}
        result = normalize_device_list_payload(payload)
        assert result == []

    def test_string_payload_returns_empty(self) -> None:
        """String payload should return empty list."""
        result = normalize_device_list_payload("not a valid payload")
        assert result == []

    def test_bytes_payload_returns_empty(self) -> None:
        """Bytes payload should return empty list."""
        result = normalize_device_list_payload(b"bytes data")
        assert result == []

    def test_mapping_without_devices_key(self) -> None:
        """Mapping without 'devices' key should return empty list."""
        payload = {"other_key": [{"id": "dev1"}]}
        result = normalize_device_list_payload(payload)
        assert result == []

    def test_empty_mapping(self) -> None:
        """Empty mapping should return empty list."""
        result = normalize_device_list_payload({})
        assert result == []

    def test_empty_list(self) -> None:
        """Empty list should return empty list."""
        result = normalize_device_list_payload([])
        assert result == []

    def test_tuple_sequence(self) -> None:
        """Should handle tuple sequences."""
        payload = ({"id": "dev1"}, {"id": "dev2"})
        result = normalize_device_list_payload(payload)
        assert result == [{"id": "dev1"}, {"id": "dev2"}]

    def test_integer_payload_returns_empty(self) -> None:
        """Integer payload should return empty list."""
        result = normalize_device_list_payload(123)
        assert result == []


# ---------------------------------------------------------------------------
# filter_and_dedupe_devices Tests
# ---------------------------------------------------------------------------


class TestFilterAndDedupeDevices:
    """Tests for filter_and_dedupe_devices function.

    Filters valid devices and removes duplicates (first-wins policy).
    """

    def test_filters_valid_devices(self) -> None:
        """Should keep devices with valid string IDs."""
        candidates = [
            {"id": "dev1", "name": "Device 1"},
            {"id": "dev2", "name": "Device 2"},
        ]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert len(devices) == 2
        assert seen == {"dev1", "dev2"}

    def test_removes_duplicates_first_wins(self) -> None:
        """Should remove duplicates, keeping first occurrence."""
        candidates = [
            {"id": "dev1", "name": "First"},
            {"id": "dev1", "name": "Duplicate"},
            {"id": "dev2", "name": "Other"},
        ]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert len(devices) == 2
        assert devices[0]["name"] == "First"
        assert seen == {"dev1", "dev2"}

    def test_skips_non_mapping_items(self) -> None:
        """Should skip non-mapping items."""
        candidates: list[Any] = [{"id": "dev1"}, "not a dict", 123, None]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert len(devices) == 1
        assert seen == {"dev1"}

    def test_skips_missing_id(self) -> None:
        """Should skip devices without 'id' key."""
        candidates = [{"id": "dev1"}, {"name": "No ID"}]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert len(devices) == 1
        assert seen == {"dev1"}

    def test_skips_non_string_id(self) -> None:
        """Should skip devices with non-string IDs."""
        candidates: list[Any] = [{"id": "valid"}, {"id": 123}, {"id": None}]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert len(devices) == 1
        assert seen == {"valid"}

    def test_skips_empty_string_id(self) -> None:
        """Should skip devices with empty string IDs."""
        candidates = [{"id": "valid"}, {"id": ""}, {"id": "   "}]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert len(devices) == 1
        assert seen == {"valid"}

    def test_strips_whitespace_from_id(self) -> None:
        """Should strip whitespace from IDs."""
        candidates = [{"id": "  dev1  ", "name": "Test"}]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert devices[0]["id"] == "dev1"
        assert seen == {"dev1"}

    def test_empty_candidates(self) -> None:
        """Should return empty results for empty candidates."""
        devices, seen = filter_and_dedupe_devices([])
        assert devices == []
        assert seen == set()

    def test_normalizes_device_to_dict(self) -> None:
        """Should convert devices to regular dicts."""
        # Use a custom mapping type to ensure conversion
        from collections import OrderedDict

        candidates = [OrderedDict([("id", "dev1"), ("name", "Test")])]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert isinstance(devices[0], dict)
        assert devices[0]["id"] == "dev1"

    def test_preserves_all_device_fields(self) -> None:
        """Should preserve all fields from original device."""
        candidates = [
            {
                "id": "dev1",
                "name": "Test",
                "latitude": 1.0,
                "longitude": 2.0,
                "extra": "value",
            }
        ]
        devices, seen = filter_and_dedupe_devices(candidates)
        assert devices[0]["name"] == "Test"
        assert devices[0]["latitude"] == 1.0
        assert devices[0]["extra"] == "value"


# ---------------------------------------------------------------------------
# should_defer_empty_list Tests
# ---------------------------------------------------------------------------


class TestShouldDeferEmptyList:
    """Tests for should_defer_empty_list function.

    Determines if an empty device list should be deferred based on
    streak count and quorum settings.
    """

    def test_defer_when_streak_below_quorum_with_previous(self) -> None:
        """Should defer when streak is below quorum and has previous list."""
        assert (
            should_defer_empty_list(streak=1, quorum=3, has_previous_list=True) is True
        )
        assert (
            should_defer_empty_list(streak=2, quorum=3, has_previous_list=True) is True
        )

    def test_not_defer_when_streak_reaches_quorum(self) -> None:
        """Should not defer when streak reaches quorum."""
        assert (
            should_defer_empty_list(streak=3, quorum=3, has_previous_list=True) is False
        )
        assert (
            should_defer_empty_list(streak=4, quorum=3, has_previous_list=True) is False
        )

    def test_not_defer_when_no_previous_list(self) -> None:
        """Should not defer when there's no previous list."""
        assert (
            should_defer_empty_list(streak=1, quorum=3, has_previous_list=False)
            is False
        )

    def test_not_defer_at_zero_streak(self) -> None:
        """Zero streak should not trigger deferral."""
        assert (
            should_defer_empty_list(streak=0, quorum=3, has_previous_list=True) is False
        )

    def test_quorum_of_one(self) -> None:
        """Quorum of 1 should never defer."""
        assert (
            should_defer_empty_list(streak=0, quorum=1, has_previous_list=True) is False
        )

    def test_high_quorum(self) -> None:
        """Should respect high quorum values."""
        assert (
            should_defer_empty_list(streak=9, quorum=10, has_previous_list=True) is True
        )
        assert (
            should_defer_empty_list(streak=10, quorum=10, has_previous_list=True)
            is False
        )


# ---------------------------------------------------------------------------
# calculate_presence_ttl Tests
# ---------------------------------------------------------------------------


class TestCalculatePresenceTtl:
    """Tests for calculate_presence_ttl function.

    Calculates presence TTL based on effective poll interval.
    """

    def test_double_interval_when_above_minimum(self) -> None:
        """Should return 2x interval when above minimum."""
        result = calculate_presence_ttl(effective_interval=120.0, min_ttl=120.0)
        assert result == 240.0

    def test_returns_minimum_when_interval_too_low(self) -> None:
        """Should return minimum TTL when 2x interval is below minimum."""
        result = calculate_presence_ttl(effective_interval=30.0, min_ttl=120.0)
        assert result == 120.0

    def test_edge_case_half_minimum(self) -> None:
        """Edge case: interval exactly half of minimum."""
        result = calculate_presence_ttl(effective_interval=60.0, min_ttl=120.0)
        assert result == 120.0

    def test_zero_interval(self) -> None:
        """Zero interval should return minimum TTL."""
        result = calculate_presence_ttl(effective_interval=0.0, min_ttl=120.0)
        assert result == 120.0

    def test_custom_minimum(self) -> None:
        """Should respect custom minimum TTL."""
        result = calculate_presence_ttl(effective_interval=30.0, min_ttl=60.0)
        assert result == 60.0

    def test_large_interval(self) -> None:
        """Large intervals should return double."""
        result = calculate_presence_ttl(effective_interval=3600.0, min_ttl=120.0)
        assert result == 7200.0


# ---------------------------------------------------------------------------
# is_poll_cycle_due Tests
# ---------------------------------------------------------------------------


class TestIsPollCycleDue:
    """Tests for is_poll_cycle_due function.

    Determines if a poll cycle should be triggered based on various conditions.
    """

    def test_due_when_elapsed_exceeds_interval(self) -> None:
        """Should be due when elapsed time exceeds interval."""
        result = is_poll_cycle_due(
            elapsed=120.0,
            effective_interval=60.0,
            predictive_block=False,
            predictive_due=False,
            hard_limit_passed=True,
            is_cold_start=False,
        )
        assert result is True

    def test_not_due_when_elapsed_below_interval(self) -> None:
        """Should not be due when elapsed is below interval."""
        result = is_poll_cycle_due(
            elapsed=30.0,
            effective_interval=60.0,
            predictive_block=False,
            predictive_due=False,
            hard_limit_passed=False,
            is_cold_start=False,
        )
        assert result is False

    def test_blocked_by_predictive_block(self) -> None:
        """Should not be due when predictive_block is True."""
        result = is_poll_cycle_due(
            elapsed=120.0,
            effective_interval=60.0,
            predictive_block=True,
            predictive_due=False,
            hard_limit_passed=True,
            is_cold_start=False,
        )
        assert result is False

    def test_not_due_on_predictive_due_below_cadence(self) -> None:
        """Predictive due must not trigger below the chosen cadence.

        Stage-1 cadence fix: min_poll_interval (reflected here by
        hard_limit_passed=True) alone no longer pulls a poll below
        effective_interval. Predictive pre-fetch only fires once
        elapsed >= effective_interval.
        """
        result = is_poll_cycle_due(
            elapsed=30.0,
            effective_interval=60.0,
            predictive_block=False,
            predictive_due=True,
            hard_limit_passed=True,
            is_cold_start=False,
        )
        assert result is False

    def test_not_due_on_predictive_due_without_hard_limit(self) -> None:
        """Should not be due on predictive_due without hard limit."""
        result = is_poll_cycle_due(
            elapsed=30.0,
            effective_interval=60.0,
            predictive_block=False,
            predictive_due=True,
            hard_limit_passed=False,
            is_cold_start=False,
        )
        assert result is False

    def test_due_on_cold_start(self) -> None:
        """Should always be due on cold start."""
        result = is_poll_cycle_due(
            elapsed=0.0,
            effective_interval=60.0,
            predictive_block=False,
            predictive_due=False,
            hard_limit_passed=False,
            is_cold_start=True,
        )
        assert result is True

    def test_cold_start_overrides_predictive_block(self) -> None:
        """Cold start should override predictive block."""
        result = is_poll_cycle_due(
            elapsed=0.0,
            effective_interval=60.0,
            predictive_block=True,
            predictive_due=False,
            hard_limit_passed=False,
            is_cold_start=True,
        )
        assert result is True

    def test_exact_interval_boundary(self) -> None:
        """Should be due at exactly the interval boundary."""
        result = is_poll_cycle_due(
            elapsed=60.0,
            effective_interval=60.0,
            predictive_block=False,
            predictive_due=False,
            hard_limit_passed=True,
            is_cold_start=False,
        )
        assert result is True

    def test_just_below_interval(self) -> None:
        """Should not be due just below interval."""
        result = is_poll_cycle_due(
            elapsed=59.9,
            effective_interval=60.0,
            predictive_block=False,
            predictive_due=False,
            hard_limit_passed=False,
            is_cold_start=False,
        )
        assert result is False
