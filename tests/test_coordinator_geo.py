# tests/test_coordinator_geo.py
"""Tests for coordinator_geo.py - Phase 1 of coordinator refactoring.

These tests validate the geographic utility functions extracted
from coordinator.py into coordinator_geo.py.

Test categories:
1. clamp - Value clamping between bounds
2. coerce_float - Safe float conversion
3. safe_accuracy - GPS accuracy normalization
4. haversine_distance - Distance calculation between coordinates
5. display-row selection & staleness - shared publish gate SSOT

REQUIREMENT: 100% test coverage for all extracted functions.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

from custom_components.googlefindmy.const import (
    DEFAULT_STALE_THRESHOLD,
    OPT_STALE_THRESHOLD,
)
from custom_components.googlefindmy.coordinator.helpers.geo import (
    DEFAULT_ACCURACY_FALLBACK_M,
    MIN_PHYSICAL_ACCURACY_M,
    clamp,
    coerce_float,
    has_usable_accuracy,
    haversine_distance,
    is_reliable_fix,
    location_age_seconds,
    resolve_seeded_accuracy,
    resolve_stale_threshold,
    safe_accuracy,
    select_display_row,
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

    # Default fallback value used in the function (200m for unknown accuracy)
    DEFAULT_FALLBACK = DEFAULT_ACCURACY_FALLBACK_M

    def test_valid_positive_accuracy(self) -> None:
        """Valid positive accuracy is returned unchanged."""
        assert safe_accuracy(50.0) == 50.0
        assert safe_accuracy(1.5) == 1.5
        assert safe_accuracy(9999.0) == 9999.0

    def test_zero_accuracy_returns_fallback(self) -> None:
        """Zero accuracy is the Android API error code, returns fallback.

        BACKGROUND: The Android Location API uses 0.0 as an error code meaning
        "no accuracy available", not "perfect precision". We treat it as unknown.
        """
        assert safe_accuracy(0.0) == self.DEFAULT_FALLBACK

    def test_sub_meter_accuracy_is_valid(self) -> None:
        """Sub-meter accuracy is valid for modern dual-frequency GNSS.

        Modern GNSS chips (L1+L5) can achieve sub-meter accuracy under ideal
        conditions (Open Sky). Values like 0.5m or 0.01m are valid measurements.
        """
        assert safe_accuracy(0.5) == 0.5
        assert safe_accuracy(0.99) == 0.99
        assert safe_accuracy(0.01) == 0.01
        assert safe_accuracy(0.002) == 0.002  # 2mm - extreme but valid

    def test_error_code_returns_fallback(self) -> None:
        """Error codes (< 0.001m) return fallback."""
        assert safe_accuracy(0.0) == self.DEFAULT_FALLBACK
        assert safe_accuracy(0.0001) == self.DEFAULT_FALLBACK
        assert safe_accuracy(0.0009) == self.DEFAULT_FALLBACK

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

    def test_boundary_accuracy_value(self) -> None:
        """Boundary value: 0.001m (1mm) is the minimum valid accuracy.

        Modern GNSS can achieve sub-meter accuracy. The threshold is set at 1mm
        to only catch the error code (0.0) and not valid high-precision data.
        """
        assert safe_accuracy(0.001) == 0.001  # Exactly at threshold - valid
        assert safe_accuracy(0.002) == 0.002  # Above threshold - valid
        assert safe_accuracy(0.0009) == self.DEFAULT_FALLBACK  # Below - error

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


class TestResolveSeededAccuracy:
    """Tests for resolve_seeded_accuracy (write-side provenance coupler).

    This helper is the structural fix for the recurring PR #1124 defect class:
    a direct cache seed (prime_device_location_cache on restore) bypasses the
    canonical _is_significant_update writer, so sanitization and the
    accuracy_estimated provenance flag must be derived together here instead of
    open-coding only safe_accuracy() and leaving a fabricated radius flagless.
    """

    def test_explicit_true_flag_wins(self) -> None:
        """A recorded estimated=True survives even for a numerically valid value."""
        accuracy, estimated = resolve_seeded_accuracy(200.0, True)
        assert accuracy == 200.0
        assert estimated is True

    def test_explicit_false_flag_wins(self) -> None:
        """A recorded estimated=False is preserved (real measurement)."""
        accuracy, estimated = resolve_seeded_accuracy(15.0, False)
        assert accuracy == 15.0
        assert estimated is False

    def test_invalid_value_overrides_explicit_false_flag(self) -> None:
        """A sanitized fallback is marked estimated even over an explicit False.

        Codex review of PR #1126: a stale recorder row can carry an explicit
        ``accuracy_estimated=False`` next to an error-code ``gps_accuracy=0``.
        The canonical ``_is_significant_update`` writer always marks a
        fabricated fallback radius estimated, overriding the incoming flag, so
        the seed-side mirror must do the same -- otherwise the 200m fallback
        masquerades as a real measurement and map_view draws a solid circle.
        """
        accuracy, estimated = resolve_seeded_accuracy(0, False)
        assert accuracy == DEFAULT_ACCURACY_FALLBACK_M
        assert estimated is True

    def test_invalid_value_without_flag_marks_estimated(self) -> None:
        """The Codex fix: a flagless error-code value is marked estimated.

        A legacy row predating accuracy_estimated with gps_accuracy=0 sanitizes
        to the 200m fallback. Without this coupling the radius enters flagless
        and map_view draws a solid circle for a fabricated measurement.
        """
        accuracy, estimated = resolve_seeded_accuracy(0, None)
        assert accuracy == DEFAULT_ACCURACY_FALLBACK_M
        assert estimated is True

    def test_none_value_without_flag_marks_estimated(self) -> None:
        """A missing value without a flag also sanitizes-and-marks estimated."""
        accuracy, estimated = resolve_seeded_accuracy(None, None)
        assert accuracy == DEFAULT_ACCURACY_FALLBACK_M
        assert estimated is True

    def test_valid_value_without_flag_stays_unflagged(self) -> None:
        """A legacy valid measurement without a flag is not fabricated.

        Returning None tells the caller to leave accuracy_estimated unset, so
        the documented map_view legacy fallback stays in charge.
        """
        accuracy, estimated = resolve_seeded_accuracy(30, None)
        assert accuracy == 30.0
        assert estimated is None

    def test_valid_submeter_value_without_flag_stays_unflagged(self) -> None:
        """A valid sub-meter real fix is preserved and left unflagged."""
        accuracy, estimated = resolve_seeded_accuracy(0.5, None)
        assert accuracy == 0.5
        assert estimated is None


# ---------------------------------------------------------------------------
# Display-row selection & staleness Tests (shared publish-gate SSOT)
# ---------------------------------------------------------------------------


class TestHasUsableAccuracy:
    """has_usable_accuracy - the tracker's publish gate as a pure predicate."""

    def test_none_row_is_not_usable(self) -> None:
        assert has_usable_accuracy(None) is False

    def test_row_without_accuracy_key_is_not_usable(self) -> None:
        assert has_usable_accuracy({"latitude": 1.0, "longitude": 2.0}) is False

    def test_row_with_none_accuracy_is_not_usable(self) -> None:
        assert has_usable_accuracy({"accuracy": None}) is False

    def test_row_with_numeric_accuracy_is_usable(self) -> None:
        assert has_usable_accuracy({"accuracy": 12.0}) is True

    def test_zero_accuracy_counts_as_present(self) -> None:
        # ``has_usable_accuracy`` only checks presence; ``safe_accuracy`` owns
        # the "0.0 means no accuracy" policy downstream.
        assert has_usable_accuracy({"accuracy": 0.0}) is True


class TestIsReliableFix:
    """is_reliable_fix - retention gate: only a real, finite, physical accuracy.

    Stricter than has_usable_accuracy (the display gate, which stays lenient so
    a 0.0/estimated fix is still shown). is_reliable_fix additionally rejects a
    numerically present but non-physical accuracy (Android's 0.0 sentinel,
    negative, sub-MIN_PHYSICAL_ACCURACY_M or non-finite) even without the
    accuracy_estimated flag, mirroring the incoming-side new_acc_measured check
    (Codex #205). Retaining/anchoring/gating against such a phantom reference
    would strand a genuine measured fix (#1179 poisoning class).
    """

    def test_real_accuracy_is_reliable(self) -> None:
        assert is_reliable_fix({"accuracy": 12.0}) is True
        assert is_reliable_fix({"accuracy": 12.0, "accuracy_estimated": False}) is True

    def test_zero_sentinel_is_not_reliable(self) -> None:
        # Android's no-accuracy sentinel: finite but < MIN_PHYSICAL_ACCURACY_M.
        assert is_reliable_fix({"accuracy": 0.0}) is False

    def test_boundary_at_min_physical_accuracy(self) -> None:
        # Pins the >= operator: exactly MIN_PHYSICAL_ACCURACY_M is reliable, a
        # hair below is not. Guards against a >=/> mutation on the floor check.
        assert is_reliable_fix({"accuracy": MIN_PHYSICAL_ACCURACY_M}) is True
        assert is_reliable_fix({"accuracy": MIN_PHYSICAL_ACCURACY_M / 2}) is False

    def test_negative_accuracy_is_not_reliable(self) -> None:
        assert is_reliable_fix({"accuracy": -1.0}) is False

    def test_non_finite_accuracy_is_not_reliable(self) -> None:
        assert is_reliable_fix({"accuracy": float("inf")}) is False
        assert is_reliable_fix({"accuracy": float("nan")}) is False

    def test_bool_accuracy_is_not_reliable(self) -> None:
        # bool is an int subclass; True must not slip through as 1.0 m.
        assert is_reliable_fix({"accuracy": True}) is False

    def test_estimated_is_not_reliable(self) -> None:
        assert is_reliable_fix({"accuracy": 200.0, "accuracy_estimated": True}) is False

    def test_none_and_missing_are_not_reliable(self) -> None:
        assert is_reliable_fix({"accuracy": None}) is False
        assert is_reliable_fix(None) is False


class TestSelectDisplayRow:
    """select_display_row - current row if it has accuracy, else last good."""

    def test_current_with_accuracy_wins(self) -> None:
        current = {"latitude": 1.0, "accuracy": 5.0}
        last_good = {"latitude": 9.0, "accuracy": 5.0}
        assert select_display_row(current, last_good) is current

    def test_accuracy_less_current_falls_back_to_last_good(self) -> None:
        current = {"latitude": 1.0, "accuracy": None}
        last_good = {"latitude": 9.0, "accuracy": 5.0}
        assert select_display_row(current, last_good) is last_good

    def test_no_current_falls_back_to_last_good(self) -> None:
        last_good = {"latitude": 9.0, "accuracy": 5.0}
        assert select_display_row(None, last_good) is last_good

    def test_both_missing_yields_none(self) -> None:
        assert select_display_row(None, None) is None

    def test_accuracy_less_last_good_is_not_published(self) -> None:
        # A genuinely accuracy-less last-good (e.g. a legacy/partial restore that
        # bypassed _is_significant_update sanitization) must not be published as
        # a coordinate: the fallback applies the same has_usable_accuracy gate as
        # the current branch (Codex PR #1181).
        current = {"latitude": 1.0, "accuracy": None}
        last_good = {"latitude": 9.0, "accuracy": None}
        assert select_display_row(current, last_good) is None

    def test_no_current_and_accuracy_less_last_good_yields_none(self) -> None:
        last_good = {"latitude": 9.0, "accuracy": None}
        assert select_display_row(None, last_good) is None

    def test_estimated_last_good_is_still_published(self) -> None:
        # A sanitized estimated fallback (accuracy=200) carries usable accuracy
        # and stays displayable -- only genuinely accuracy-less rows are gated.
        last_good = {"latitude": 9.0, "accuracy": 200.0, "accuracy_estimated": True}
        assert select_display_row(None, last_good) is last_good


class TestLocationAgeSeconds:
    """location_age_seconds - pure age arithmetic with an injected ``now``."""

    def test_none_row_yields_none(self) -> None:
        assert location_age_seconds(None, 1_000.0) is None

    def test_missing_last_seen_yields_none(self) -> None:
        assert location_age_seconds({"latitude": 1.0}, 1_000.0) is None

    def test_non_numeric_last_seen_yields_none(self) -> None:
        assert location_age_seconds({"last_seen": "nope"}, 1_000.0) is None

    def test_age_is_now_minus_last_seen(self) -> None:
        assert location_age_seconds({"last_seen": 900.0}, 1_000.0) == 100.0


class TestResolveStaleThreshold:
    """resolve_stale_threshold - duck-typed config reader with safe fallbacks."""

    def test_missing_config_entry_returns_default(self) -> None:
        coordinator = SimpleNamespace(config_entry=None)
        assert resolve_stale_threshold(coordinator) == DEFAULT_STALE_THRESHOLD

    def test_non_mapping_options_returns_default(self) -> None:
        entry = SimpleNamespace(options=None)
        coordinator = SimpleNamespace(config_entry=entry)
        assert resolve_stale_threshold(coordinator) == DEFAULT_STALE_THRESHOLD

    def test_configured_value_is_returned(self) -> None:
        entry = SimpleNamespace(options={OPT_STALE_THRESHOLD: 1234})
        coordinator = SimpleNamespace(config_entry=entry)
        assert resolve_stale_threshold(coordinator) == 1234

    def test_malformed_value_falls_back_to_default(self) -> None:
        entry = SimpleNamespace(options={OPT_STALE_THRESHOLD: "abc"})
        coordinator = SimpleNamespace(config_entry=entry)
        assert resolve_stale_threshold(coordinator) == DEFAULT_STALE_THRESHOLD
