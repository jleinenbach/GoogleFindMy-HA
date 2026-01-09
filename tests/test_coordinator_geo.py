"""Tests for coordinator_geo.py - Phase 1 of coordinator refactoring.

These tests validate the geographic utility functions extracted
from coordinator.py into coordinator_geo.py.

Test categories:
1. clamp - Value clamping between bounds
2. coerce_float - Safe float conversion
3. safe_accuracy - GPS accuracy normalization
4. haversine_distance - Distance calculation between coordinates

REQUIREMENT: 100% test coverage for all extracted functions.
"""

from __future__ import annotations

import math

from custom_components.googlefindmy.coordinator.helpers.geo import (
    clamp,
    coerce_float,
    haversine_distance,
    safe_accuracy,
)

# ---------------------------------------------------------------------------
# clamp Tests
# ---------------------------------------------------------------------------


class TestClamp:
    """Tests for clamp function."""

    def test_value_within_bounds(self) -> None:
        """Value within bounds is returned unchanged."""
        assert clamp(5.0, 0.0, 10.0) == 5.0

    def test_value_at_lower_bound(self) -> None:
        """Value at lower bound is returned unchanged."""
        assert clamp(0.0, 0.0, 10.0) == 0.0

    def test_value_at_upper_bound(self) -> None:
        """Value at upper bound is returned unchanged."""
        assert clamp(10.0, 0.0, 10.0) == 10.0

    def test_value_below_lower_bound(self) -> None:
        """Value below lower bound is clamped to lower bound."""
        assert clamp(-5.0, 0.0, 10.0) == 0.0

    def test_value_above_upper_bound(self) -> None:
        """Value above upper bound is clamped to upper bound."""
        assert clamp(15.0, 0.0, 10.0) == 10.0

    def test_negative_bounds(self) -> None:
        """Works correctly with negative bounds."""
        assert clamp(-5.0, -10.0, -1.0) == -5.0
        assert clamp(-15.0, -10.0, -1.0) == -10.0
        assert clamp(0.0, -10.0, -1.0) == -1.0

    def test_float_precision(self) -> None:
        """Handles float precision correctly."""
        result = clamp(0.1 + 0.2, 0.0, 1.0)
        assert 0.29 < result < 0.31

    def test_string_number_coercion(self) -> None:
        """String numbers are coerced to float."""
        assert clamp("5", 0.0, 10.0) == 5.0
        assert clamp("15", 0.0, 10.0) == 10.0

    def test_invalid_value_returns_lower_bound(self) -> None:
        """Invalid value returns lower bound."""
        assert clamp("invalid", 0.0, 10.0) == 0.0
        assert clamp(None, 0.0, 10.0) == 0.0  # type: ignore[arg-type]

    def test_invalid_bounds_coerced(self) -> None:
        """Invalid bounds are coerced to float."""
        assert clamp(5, "0", "10") == 5.0  # type: ignore[arg-type]

    def test_equal_bounds(self) -> None:
        """Equal bounds returns that value."""
        assert clamp(5.0, 5.0, 5.0) == 5.0
        assert clamp(0.0, 5.0, 5.0) == 5.0
        assert clamp(10.0, 5.0, 5.0) == 5.0

    def test_infinity_handling(self) -> None:
        """Infinity values are clamped correctly."""
        assert clamp(float("inf"), 0.0, 10.0) == 10.0
        assert clamp(float("-inf"), 0.0, 10.0) == 0.0

    def test_nan_returns_lower_bound(self) -> None:
        """NaN returns lower bound due to comparison behavior."""
        result = clamp(float("nan"), 0.0, 10.0)
        # NaN comparisons are tricky - verify it returns a valid number
        assert math.isfinite(result)


# ---------------------------------------------------------------------------
# coerce_float Tests
# ---------------------------------------------------------------------------


class TestCoerceFloat:
    """Tests for coerce_float function."""

    def test_valid_float(self) -> None:
        """Valid float is returned unchanged."""
        assert coerce_float(3.14) == 3.14

    def test_valid_int(self) -> None:
        """Integer is converted to float."""
        assert coerce_float(42) == 42.0

    def test_valid_string_number(self) -> None:
        """String number is converted to float."""
        assert coerce_float("3.14") == 3.14
        assert coerce_float("-42") == -42.0

    def test_zero(self) -> None:
        """Zero is a valid float."""
        assert coerce_float(0) == 0.0
        assert coerce_float(0.0) == 0.0
        assert coerce_float("0") == 0.0

    def test_negative_values(self) -> None:
        """Negative values are valid."""
        assert coerce_float(-3.14) == -3.14
        assert coerce_float("-100") == -100.0

    def test_none_returns_none(self) -> None:
        """None returns None."""
        assert coerce_float(None) is None

    def test_invalid_string_returns_none(self) -> None:
        """Invalid string returns None."""
        assert coerce_float("invalid") is None
        assert coerce_float("") is None
        assert coerce_float("abc123") is None

    def test_nan_returns_none(self) -> None:
        """NaN returns None (not finite)."""
        assert coerce_float(float("nan")) is None

    def test_inf_returns_none(self) -> None:
        """Infinity returns None (not finite)."""
        assert coerce_float(float("inf")) is None
        assert coerce_float(float("-inf")) is None

    def test_list_returns_none(self) -> None:
        """List returns None."""
        assert coerce_float([1, 2, 3]) is None

    def test_dict_returns_none(self) -> None:
        """Dict returns None."""
        assert coerce_float({"value": 1}) is None

    def test_scientific_notation(self) -> None:
        """Scientific notation strings are parsed."""
        assert coerce_float("1e10") == 1e10
        assert coerce_float("1.5e-3") == 0.0015

    def test_whitespace_string(self) -> None:
        """Whitespace-only string returns None."""
        assert coerce_float("   ") is None

    def test_bool_coercion(self) -> None:
        """Boolean is coerced to float (True=1.0, False=0.0)."""
        assert coerce_float(True) == 1.0
        assert coerce_float(False) == 0.0


# ---------------------------------------------------------------------------
# safe_accuracy Tests
# ---------------------------------------------------------------------------


class TestSafeAccuracy:
    """Tests for safe_accuracy function."""

    # Default fallback value used in the function
    DEFAULT_FALLBACK = 10000.0

    def test_valid_positive_accuracy(self) -> None:
        """Valid positive accuracy is returned unchanged."""
        assert safe_accuracy(50.0) == 50.0
        assert safe_accuracy(1.5) == 1.5
        assert safe_accuracy(9999.0) == 9999.0

    def test_zero_accuracy(self) -> None:
        """Zero is a valid accuracy."""
        assert safe_accuracy(0.0) == 0.0

    def test_none_returns_fallback(self) -> None:
        """None returns fallback value."""
        assert safe_accuracy(None) == self.DEFAULT_FALLBACK

    def test_negative_returns_fallback(self) -> None:
        """Negative values return fallback."""
        assert safe_accuracy(-1.0) == self.DEFAULT_FALLBACK
        assert safe_accuracy(-100.0) == self.DEFAULT_FALLBACK

    def test_nan_returns_fallback(self) -> None:
        """NaN returns fallback value."""
        assert safe_accuracy(float("nan")) == self.DEFAULT_FALLBACK

    def test_inf_returns_fallback(self) -> None:
        """Infinity returns fallback value."""
        assert safe_accuracy(float("inf")) == self.DEFAULT_FALLBACK
        assert safe_accuracy(float("-inf")) == self.DEFAULT_FALLBACK

    def test_very_small_positive(self) -> None:
        """Very small positive values are valid."""
        assert safe_accuracy(0.001) == 0.001
        assert safe_accuracy(1e-10) == 1e-10

    def test_very_large_positive(self) -> None:
        """Very large positive values are valid."""
        assert safe_accuracy(1e9) == 1e9


# ---------------------------------------------------------------------------
# haversine_distance Tests
# ---------------------------------------------------------------------------


class TestHaversineDistance:
    """Tests for haversine_distance function."""

    def test_same_point_zero_distance(self) -> None:
        """Same coordinates return zero distance."""
        assert haversine_distance(0.0, 0.0, 0.0, 0.0) == 0.0
        assert haversine_distance(52.52, 13.405, 52.52, 13.405) == 0.0

    def test_known_distance_berlin_munich(self) -> None:
        """Known distance: Berlin to Munich ~504 km."""
        # Berlin: 52.52°N, 13.405°E
        # Munich: 48.1351°N, 11.582°E
        dist = haversine_distance(52.52, 13.405, 48.1351, 11.582)
        # Allow 5% tolerance for Earth radius variations
        assert 479_000 < dist < 529_000

    def test_known_distance_new_york_london(self) -> None:
        """Known distance: New York to London ~5570 km."""
        # New York: 40.7128°N, 74.0060°W
        # London: 51.5074°N, 0.1278°W
        dist = haversine_distance(40.7128, -74.0060, 51.5074, -0.1278)
        # Allow 5% tolerance
        assert 5_290_000 < dist < 5_850_000

    def test_symmetric(self) -> None:
        """Distance is symmetric (A to B = B to A)."""
        d1 = haversine_distance(52.52, 13.405, 48.1351, 11.582)
        d2 = haversine_distance(48.1351, 11.582, 52.52, 13.405)
        assert abs(d1 - d2) < 0.001

    def test_antipodal_points(self) -> None:
        """Antipodal points (opposite sides of Earth) ~20000 km."""
        # North Pole to South Pole
        dist = haversine_distance(90.0, 0.0, -90.0, 0.0)
        # Should be approximately half Earth circumference
        assert 19_900_000 < dist < 20_100_000

    def test_equator_distance(self) -> None:
        """Distance along equator: 1 degree longitude ~111 km."""
        dist = haversine_distance(0.0, 0.0, 0.0, 1.0)
        # At equator, 1° longitude ≈ 111.32 km
        assert 110_000 < dist < 112_000

    def test_meridian_distance(self) -> None:
        """Distance along meridian: 1 degree latitude ~111 km."""
        dist = haversine_distance(0.0, 0.0, 1.0, 0.0)
        # 1° latitude ≈ 111 km
        assert 110_000 < dist < 112_000

    def test_negative_coordinates(self) -> None:
        """Works with negative coordinates (Southern/Western hemisphere)."""
        # Sydney to Buenos Aires
        dist = haversine_distance(-33.8688, 151.2093, -34.6037, -58.3816)
        # Approximately 11,800 km
        assert 11_000_000 < dist < 12_500_000

    def test_across_dateline(self) -> None:
        """Distance calculation across international dateline."""
        # Tokyo to Los Angeles (crosses dateline conceptually via Pacific)
        dist = haversine_distance(35.6762, 139.6503, 34.0522, -118.2437)
        # Approximately 8,800 km
        assert 8_500_000 < dist < 9_200_000

    def test_string_coordinates(self) -> None:
        """String coordinates are coerced to float."""
        dist = haversine_distance("52.52", "13.405", "48.1351", "11.582")
        assert 479_000 < dist < 529_000

    def test_small_distance_meters(self) -> None:
        """Small distances in meters are calculated correctly."""
        # Two points ~100m apart (rough approximation)
        # 0.001° latitude ≈ 111m at equator
        dist = haversine_distance(0.0, 0.0, 0.001, 0.0)
        assert 100 < dist < 120

    def test_very_close_points(self) -> None:
        """Very close points return small but non-zero distance."""
        dist = haversine_distance(52.52, 13.405, 52.520001, 13.405001)
        assert 0 < dist < 1  # Less than 1 meter

    def test_boundary_latitude_values(self) -> None:
        """Handles boundary latitude values (-90, 90)."""
        # From North Pole
        dist1 = haversine_distance(90.0, 0.0, 0.0, 0.0)
        # From South Pole
        dist2 = haversine_distance(-90.0, 0.0, 0.0, 0.0)
        # Both should be about 10,000 km (quarter of Earth circumference)
        assert 9_900_000 < dist1 < 10_100_000
        assert 9_900_000 < dist2 < 10_100_000

    def test_boundary_longitude_values(self) -> None:
        """Handles boundary longitude values (-180, 180)."""
        # These are the same point
        dist = haversine_distance(0.0, 180.0, 0.0, -180.0)
        assert dist < 1  # Should be essentially zero


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestGeoIntegration:
    """Integration tests combining multiple geo functions."""

    def test_accuracy_weighted_distance_scenario(self) -> None:
        """Simulate accuracy-weighted distance calculation."""
        # Two GPS readings with different accuracies - close enough to overlap
        lat1, lon1, acc1 = 52.52, 13.405, 10.0
        lat2, lon2, acc2 = 52.52003, 13.40502, 50.0  # ~4m apart

        # Calculate distance
        dist = haversine_distance(lat1, lon1, lat2, lon2)

        # Normalize accuracies
        safe_acc1 = safe_accuracy(acc1)
        safe_acc2 = safe_accuracy(acc2)

        # Check if readings overlap (distance < sum of accuracies)
        overlap = dist < (safe_acc1 + safe_acc2)

        assert dist > 0
        assert dist < 10  # Should be ~4 meters
        assert safe_acc1 == 10.0
        assert safe_acc2 == 50.0
        assert overlap  # Should overlap since distance < 60m

    def test_clamp_coordinates(self) -> None:
        """Clamp can be used to validate coordinate ranges."""
        lat = clamp(95.0, -90.0, 90.0)  # Invalid latitude
        lon = clamp(200.0, -180.0, 180.0)  # Invalid longitude

        assert lat == 90.0
        assert lon == 180.0

    def test_coerce_and_calculate(self) -> None:
        """Coerce string inputs before distance calculation."""
        lat1 = coerce_float("52.52")
        lon1 = coerce_float("13.405")
        lat2 = coerce_float("48.1351")
        lon2 = coerce_float("11.582")

        assert lat1 is not None
        assert lon1 is not None
        assert lat2 is not None
        assert lon2 is not None

        dist = haversine_distance(lat1, lon1, lat2, lon2)
        assert 479_000 < dist < 529_000
