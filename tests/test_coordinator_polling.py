"""Tests for coordinator_polling.py - Phase 11 of coordinator refactoring.

These tests validate the polling helper functions extracted from
_async_start_poll_cycle into coordinator_polling.py.

Test categories:
1. calculate_location_age_hours - Calculate age of location data
2. get_age_log_level - Determine logging level based on age
3. should_preserve_previous_coordinates - Check if previous coords should be used

REQUIREMENT: 100% test coverage for all extracted functions.

KEY RISKS COVERED:
- Location age calculation with missing/invalid timestamps
- Log level determination edge cases
- Semantic-only location handling
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.googlefindmy.coordinator.helpers.polling import (
    calculate_location_age_hours,
    get_age_log_level,
    should_preserve_previous_coordinates,
)

# ---------------------------------------------------------------------------
# calculate_location_age_hours Tests
# ---------------------------------------------------------------------------


class TestCalculateLocationAgeHours:
    """Tests for calculate_location_age_hours function.

    Calculates the age of location data in hours based on last_seen timestamp.
    """

    def test_calculates_age_correctly(self) -> None:
        """Should calculate age in hours correctly."""
        wall_now = 1704067200.0  # 2024-01-01 00:00:00 UTC
        last_seen = 1704063600.0  # 1 hour earlier
        result = calculate_location_age_hours(last_seen, wall_now)
        assert result == pytest.approx(1.0, rel=0.01)

    def test_calculates_large_age(self) -> None:
        """Should handle large age values (days)."""
        wall_now = 1704067200.0
        last_seen = wall_now - (48 * 3600)  # 48 hours earlier
        result = calculate_location_age_hours(last_seen, wall_now)
        assert result == pytest.approx(48.0, rel=0.01)

    def test_returns_zero_for_same_time(self) -> None:
        """Should return 0 when timestamps are equal."""
        wall_now = 1704067200.0
        result = calculate_location_age_hours(wall_now, wall_now)
        assert result == pytest.approx(0.0)

    def test_returns_none_for_none_last_seen(self) -> None:
        """Should return None when last_seen is None."""
        result = calculate_location_age_hours(None, 1704067200.0)
        assert result is None

    def test_returns_none_for_zero_last_seen(self) -> None:
        """Should return None when last_seen is 0."""
        result = calculate_location_age_hours(0, 1704067200.0)
        assert result is None

    def test_returns_none_for_negative_last_seen(self) -> None:
        """Should return None for negative last_seen."""
        result = calculate_location_age_hours(-100, 1704067200.0)
        assert result is None

    def test_clamps_negative_age_to_zero(self) -> None:
        """Should clamp negative age to zero (future timestamp)."""
        wall_now = 1704067200.0
        last_seen = wall_now + 3600  # 1 hour in the future
        result = calculate_location_age_hours(last_seen, wall_now)
        assert result == pytest.approx(0.0)

    def test_handles_float_last_seen(self) -> None:
        """Should handle float last_seen values."""
        wall_now = 1704067200.0
        last_seen = 1704063600.5  # With milliseconds
        result = calculate_location_age_hours(last_seen, wall_now)
        assert result is not None
        assert result > 0

    def test_handles_int_last_seen(self) -> None:
        """Should handle int last_seen values."""
        wall_now = 1704067200.0
        last_seen = 1704063600  # Integer
        result = calculate_location_age_hours(last_seen, wall_now)
        assert result == pytest.approx(1.0, rel=0.01)


# ---------------------------------------------------------------------------
# get_age_log_level Tests
# ---------------------------------------------------------------------------


class TestGetAgeLogLevel:
    """Tests for get_age_log_level function.

    Determines the appropriate log level based on location age.
    """

    def test_returns_info_for_age_over_24h(self) -> None:
        """Should return 'info' for age > 24 hours."""
        level, should_log = get_age_log_level(25.0)
        assert level == "info"
        assert should_log is True

    def test_returns_debug_for_age_between_1_and_24h(self) -> None:
        """Should return 'debug' for 1 < age <= 24 hours."""
        level, should_log = get_age_log_level(12.0)
        assert level == "debug"
        assert should_log is True

    def test_returns_none_for_age_under_1h(self) -> None:
        """Should not log for age <= 1 hour."""
        level, should_log = get_age_log_level(0.5)
        assert level is None
        assert should_log is False

    def test_boundary_at_24h(self) -> None:
        """Should return 'debug' at exactly 24 hours."""
        level, should_log = get_age_log_level(24.0)
        assert level == "debug"
        assert should_log is True

    def test_boundary_at_1h(self) -> None:
        """Should return 'debug' at exactly 1 hour."""
        level, should_log = get_age_log_level(1.0)
        assert level is None
        assert should_log is False

    def test_just_over_1h(self) -> None:
        """Should return 'debug' just over 1 hour."""
        level, should_log = get_age_log_level(1.01)
        assert level == "debug"
        assert should_log is True

    def test_just_over_24h(self) -> None:
        """Should return 'info' just over 24 hours."""
        level, should_log = get_age_log_level(24.01)
        assert level == "info"
        assert should_log is True

    def test_handles_zero_age(self) -> None:
        """Should not log for zero age."""
        level, should_log = get_age_log_level(0.0)
        assert level is None
        assert should_log is False

    def test_handles_very_large_age(self) -> None:
        """Should return 'info' for very large age."""
        level, should_log = get_age_log_level(1000.0)
        assert level == "info"
        assert should_log is True


# ---------------------------------------------------------------------------
# should_preserve_previous_coordinates Tests
# ---------------------------------------------------------------------------


class TestShouldPreservePreviousCoordinates:
    """Tests for should_preserve_previous_coordinates function.

    Determines if semantic-only location should preserve previous coordinates.
    """

    def test_returns_true_when_no_coords_but_has_semantic(self) -> None:
        """Should return True when no coords but has semantic_name."""
        location: dict[str, Any] = {"semantic_name": "Home"}
        assert should_preserve_previous_coordinates(location) is True

    def test_returns_true_when_lat_none_but_has_semantic(self) -> None:
        """Should return True when latitude is None but has semantic_name."""
        location: dict[str, Any] = {
            "latitude": None,
            "longitude": 1.0,
            "semantic_name": "Work",
        }
        assert should_preserve_previous_coordinates(location) is True

    def test_returns_true_when_lon_none_but_has_semantic(self) -> None:
        """Should return True when longitude is None but has semantic_name."""
        location: dict[str, Any] = {
            "latitude": 1.0,
            "longitude": None,
            "semantic_name": "Work",
        }
        assert should_preserve_previous_coordinates(location) is True

    def test_returns_false_when_has_coords(self) -> None:
        """Should return False when both coords are present."""
        location: dict[str, Any] = {
            "latitude": 1.0,
            "longitude": 2.0,
            "semantic_name": "Home",
        }
        assert should_preserve_previous_coordinates(location) is False

    def test_returns_false_when_no_semantic_name(self) -> None:
        """Should return False when no semantic_name."""
        location: dict[str, Any] = {"latitude": None, "longitude": None}
        assert should_preserve_previous_coordinates(location) is False

    def test_returns_false_when_empty_semantic_name(self) -> None:
        """Should return False when semantic_name is empty string."""
        location: dict[str, Any] = {"latitude": None, "semantic_name": ""}
        assert should_preserve_previous_coordinates(location) is False

    def test_returns_false_for_empty_location(self) -> None:
        """Should return False for empty location dict."""
        assert should_preserve_previous_coordinates({}) is False

    def test_handles_non_string_semantic_name(self) -> None:
        """Should return False for non-string semantic_name."""
        location: dict[str, Any] = {"latitude": None, "semantic_name": 123}
        assert should_preserve_previous_coordinates(location) is False

    def test_handles_zero_coordinates(self) -> None:
        """Should return False when coords are zero (valid)."""
        location: dict[str, Any] = {
            "latitude": 0.0,
            "longitude": 0.0,
            "semantic_name": "Home",
        }
        assert should_preserve_previous_coordinates(location) is False
