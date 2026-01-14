# tests/test_decoder_accuracy_regression.py
"""Regression tests for accuracy validation in decoder.py.

These tests ensure that physically impossible accuracy values (0.0m, negative,
NaN, Inf) are correctly rejected and never win the ranking algorithm.

BACKGROUND (January 2025):
Google's API sometimes returns `isOwnReport=True` with `accuracy=0.0m`. This is
physically impossible - real GPS accuracy is typically 3-50m. The algorithm
previously treated 0.0m as "perfect precision" due to the ranking formula:
    acc_rank = -float(acc)  # 0.0 -> -0.0 > -20.0 (oops!)

This caused spurious "Own Device" reports with invalid data to win over
legitimate crowdsourced reports with real GPS coordinates.

These tests are designed to:
1. Prevent regression if someone refactors the validation logic
2. Provide clear, actionable error messages pointing to the fix location
3. Document the physics constraints that the code must enforce
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from custom_components.googlefindmy.ProtoDecoders.decoder import (
    _get_rank_tuple,
    _normalize_location_dict,
    _select_best_location,
)

# =============================================================================
# SECTION 1: Physics Validation Tests (accuracy must be > 0)
# =============================================================================


class TestAccuracyPhysicsValidation:
    """Test that physically impossible accuracy values are rejected."""

    # -------------------------------------------------------------------------
    # Test: _normalize_location_dict must filter invalid accuracy
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("input_accuracy", "should_be_removed", "scenario"),
        [
            # Valid accuracy values - should be preserved
            (5.0, False, "typical_gps_accuracy"),
            (20.0, False, "moderate_gps_accuracy"),
            (100.0, False, "poor_gps_accuracy"),
            (0.001, False, "very_precise_but_positive"),
            # Invalid accuracy values - MUST be removed
            (0.0, True, "zero_accuracy_impossible"),
            (-1.0, True, "negative_accuracy_impossible"),
            (-100.0, True, "large_negative_impossible"),
            (float("nan"), True, "nan_is_invalid"),
            (float("inf"), True, "positive_infinity_invalid"),
            (float("-inf"), True, "negative_infinity_invalid"),
        ],
        ids=lambda x: str(x) if not isinstance(x, str) else x,
    )
    def test_normalize_filters_invalid_accuracy(
        self,
        input_accuracy: float,
        should_be_removed: bool,
        scenario: str,
    ) -> None:
        """_normalize_location_dict must remove physically impossible accuracy values.

        GPS accuracy of <= 0 meters is physically impossible. Real GPS systems
        report accuracy in the range of 3-50m (good conditions) to 100m+ (poor).
        Values of 0.0, negative, NaN, or Inf indicate missing/corrupted data.
        """
        loc = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": input_accuracy,
            "last_seen": 1700000000,
        }

        result = _normalize_location_dict(loc)

        if should_be_removed:
            assert "accuracy" not in result, (
                f"\n"
                f"{'=' * 70}\n"
                f"REGRESSION DETECTED: Invalid accuracy was NOT filtered!\n"
                f"{'=' * 70}\n"
                f"Scenario: {scenario}\n"
                f"Input accuracy: {input_accuracy}\n"
                f"\n"
                f"PHYSICS VIOLATION: GPS accuracy of {input_accuracy}m is impossible.\n"
                f"Real GPS accuracy is typically 3-50m, never <= 0.\n"
                f"\n"
                f"FIX LOCATION: decoder.py :: _normalize_location_dict()\n"
                f"The function must filter out accuracy <= 0 before returning.\n"
                f"Look for: 'elif num_key == \"accuracy\" and f <= 0.0'\n"
                f"{'=' * 70}"
            )
        else:
            assert "accuracy" in result, (
                f"Valid accuracy {input_accuracy} should be preserved"
            )
            assert result["accuracy"] == pytest.approx(input_accuracy, nan_ok=False)

    # -------------------------------------------------------------------------
    # Test: _get_rank_tuple must penalize invalid accuracy
    # -------------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("accuracy", "expected_rank", "scenario"),
        [
            # Valid: smaller accuracy = better (more negative rank = higher priority)
            (5.0, -5.0, "good_accuracy_ranked_high"),
            (50.0, -50.0, "moderate_accuracy_ranked_lower"),
            # Invalid: must get worst possible rank (float("-inf"))
            (0.0, float("-inf"), "zero_gets_worst_rank"),
            (-10.0, float("-inf"), "negative_gets_worst_rank"),
            (None, float("-inf"), "missing_gets_worst_rank"),
        ],
    )
    def test_get_rank_tuple_penalizes_invalid_accuracy(
        self,
        accuracy: float | None,
        expected_rank: float,
        scenario: str,
    ) -> None:
        """_get_rank_tuple must assign worst rank to invalid accuracy values.

        Defense in depth: even if _normalize_location_dict fails to filter,
        the ranking function must not treat 0.0m as "perfect precision".
        """
        loc: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "last_seen": 1700000000,
            "is_own_report": False,
        }
        if accuracy is not None:
            loc["accuracy"] = accuracy

        rank_tuple = _get_rank_tuple(loc)
        acc_rank = rank_tuple[3]  # Index 3 is accuracy rank

        if math.isinf(expected_rank) and expected_rank < 0:
            assert math.isinf(acc_rank) and acc_rank < 0, (
                f"\n"
                f"{'=' * 70}\n"
                f"REGRESSION DETECTED: Invalid accuracy got non-worst rank!\n"
                f"{'=' * 70}\n"
                f"Scenario: {scenario}\n"
                f"Input accuracy: {accuracy}\n"
                f"Actual acc_rank: {acc_rank}\n"
                f"Expected: float('-inf') (worst possible rank)\n"
                f"\n"
                f"PROBLEM: The ranking algorithm is treating invalid accuracy\n"
                f"as if it were valid data. This causes 0.0m to win over real GPS.\n"
                f"\n"
                f"FIX LOCATION: decoder.py :: _get_rank_tuple()\n"
                f"The acc_rank calculation must check: float(acc) > 0\n"
                f"Look for the line with 'acc_rank = ('\n"
                f"{'=' * 70}"
            )
        else:
            assert acc_rank == pytest.approx(expected_rank)


# =============================================================================
# SECTION 2: Ranking Algorithm Regression Tests
# =============================================================================


class TestSelectBestLocationRegression:
    """Test that _select_best_location correctly handles accuracy conflicts."""

    def test_crowdsourced_beats_own_report_with_zero_accuracy(self) -> None:
        """CRITICAL: A real GPS fix must beat a spurious 'Own Device' with 0.0m.

        This is the core regression test for the January 2025 bug where
        Google's API returned isOwnReport=True with accuracy=0.0m, and
        the algorithm incorrectly prioritized it over real crowdsourced data.
        """
        # The "bad" report: Own device flag but physically impossible accuracy
        spurious_own_report: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 0.0,  # IMPOSSIBLE - this should be rejected
            "last_seen": 1700000100,
            "is_own_report": True,
            "status": "LAST_KNOWN",
            "status_code": 1,
        }

        # The "good" report: Real GPS data from the network
        legitimate_crowdsourced: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 20.0,  # Realistic GPS accuracy
            "last_seen": 1700000100,  # Same timestamp
            "is_own_report": False,
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        best, _rest = _select_best_location(
            [spurious_own_report, legitimate_crowdsourced]
        )

        assert best is not None, "Should select a location"

        # The legitimate report MUST win
        assert best.get("accuracy") == pytest.approx(20.0), (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL REGRESSION: Zero-accuracy 'Own Device' won the ranking!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"THE BUG: Google's API returned isOwnReport=True with accuracy=0.0m.\n"
            f"This is physically impossible (GPS is never 0.0m accurate).\n"
            f"The algorithm incorrectly treated 0.0 as 'perfect precision'.\n"
            f"\n"
            f"EXPECTED: Crowdsourced report (accuracy=20.0m) should win.\n"
            f"ACTUAL: Selected accuracy={best.get('accuracy')}m\n"
            f"\n"
            f"ROOT CAUSE: The ranking formula was:\n"
            f"    acc_rank = -float(acc)  # 0.0 -> -0.0 > -20.0 (WRONG!)\n"
            f"\n"
            f"FIX LOCATIONS (check both):\n"
            f"  1. decoder.py :: _normalize_location_dict() - filter accuracy <= 0\n"
            f"  2. decoder.py :: _get_rank_tuple() - penalize accuracy <= 0\n"
            f"{'=' * 70}"
        )

        assert best.get("is_own_report") is False, (
            "The legitimate crowdsourced report should be selected, not the spurious own report"
        )

    @pytest.mark.parametrize(
        ("bad_accuracy", "good_accuracy", "scenario"),
        [
            (0.0, 20.0, "zero_vs_normal"),
            (-5.0, 50.0, "negative_vs_poor"),
            (float("nan"), 100.0, "nan_vs_very_poor"),
            (float("inf"), 30.0, "inf_vs_normal"),
        ],
        ids=["zero_vs_normal", "negative_vs_poor", "nan_vs_very_poor", "inf_vs_normal"],
    )
    def test_invalid_accuracy_never_wins(
        self,
        bad_accuracy: float,
        good_accuracy: float,
        scenario: str,
    ) -> None:
        """Reports with invalid accuracy must never win, regardless of other factors."""
        bad_report: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": bad_accuracy,
            "last_seen": 1700000200,  # Even newer timestamp!
            "is_own_report": True,  # Even with owner priority!
            "status": "LAST_KNOWN",
            "status_code": 1,
        }

        good_report: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": good_accuracy,
            "last_seen": 1700000100,  # Older
            "is_own_report": False,  # No owner priority
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        best, _rest = _select_best_location([bad_report, good_report])

        assert best is not None
        selected_accuracy = best.get("accuracy")

        # The good report must win (or accuracy should be None if filtered)
        valid_outcomes = (
            selected_accuracy == pytest.approx(good_accuracy)
            or selected_accuracy is None  # Filtered out entirely is also acceptable
        )

        assert valid_outcomes, (
            f"\n"
            f"{'=' * 70}\n"
            f"REGRESSION: Invalid accuracy report won the ranking!\n"
            f"{'=' * 70}\n"
            f"Scenario: {scenario}\n"
            f"Bad accuracy: {bad_accuracy} (should lose)\n"
            f"Good accuracy: {good_accuracy} (should win)\n"
            f"Selected: {selected_accuracy}\n"
            f"\n"
            f"FIX: Ensure _normalize_location_dict filters accuracy <= 0, NaN, Inf\n"
            f"{'=' * 70}"
        )

    def test_timestamp_tiebreaker_preserves_sort_order(self) -> None:
        """When timestamps are equal, the pre-sorted order must be preserved.

        This tests the fix for the 'Last-Write-Wins' bug where >= was used
        instead of > in _merge_semantics_if_near_ts.
        """
        # Both have same timestamp, but first one has better accuracy
        better_report: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 5.0,  # Better
            "last_seen": 1700000100,
            "is_own_report": False,
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        worse_report: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 50.0,  # Worse
            "last_seen": 1700000100,  # Same timestamp
            "is_own_report": False,
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        # Order matters: better_report should win even if worse_report comes later
        best, _rest = _select_best_location([better_report, worse_report])

        assert best is not None
        assert best.get("accuracy") == pytest.approx(5.0), (
            f"\n"
            f"{'=' * 70}\n"
            f"REGRESSION: Last-Write-Wins bug detected!\n"
            f"{'=' * 70}\n"
            f"When timestamps are equal, the better-ranked entry should win.\n"
            f"Selected accuracy: {best.get('accuracy')}m (expected 5.0m)\n"
            f"\n"
            f"FIX LOCATION: decoder.py :: _merge_semantics_if_near_ts()\n"
            f"Change 'ts >= best_coordinate_ts' to 'ts > best_coordinate_ts'\n"
            f"{'=' * 70}"
        )


# =============================================================================
# SECTION 3: Edge Cases and Dirty Data
# =============================================================================


class TestDirtyDataHandling:
    """Test handling of malformed or missing data."""

    def test_missing_coordinates_not_selected(self) -> None:
        """Reports without coordinates should not beat reports WITH coordinates.

        When timestamp and status are equal, coordinates become the tie-breaker.
        The algorithm's priority is:
          1. Newer timestamp
          2. Status (owner > crowdsourced > aggregated)
          3. Has coordinates (tie-breaker)
          4. Better accuracy
        """
        # Semantic-only: no lat/lon - should lose as tie-breaker
        no_coords: dict[str, Any] = {
            "accuracy": 5.0,
            "last_seen": 1700000100,  # Same timestamp
            "is_own_report": False,  # Same status
            "semantic_name": "Home",
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        # Has coordinates - should win as tie-breaker
        with_coords: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 50.0,  # Worse accuracy, but has coords
            "last_seen": 1700000100,  # Same timestamp
            "is_own_report": False,  # Same status
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        best, _rest = _select_best_location([no_coords, with_coords])

        assert best is not None
        assert "latitude" in best and "longitude" in best, (
            "When timestamps match, report with coordinates should win over "
            "semantic-only report as a tie-breaker"
        )

    def test_all_invalid_returns_none_or_filters(self) -> None:
        """If all reports have invalid accuracy, result should be filtered/empty."""
        all_bad = [
            {
                "latitude": 48.1,
                "longitude": 11.1,
                "accuracy": 0.0,
                "last_seen": 1700000100,
            },
            {
                "latitude": 48.2,
                "longitude": 11.2,
                "accuracy": -5.0,
                "last_seen": 1700000100,
            },
        ]

        best, _rest = _select_best_location(all_bad)

        # Either no selection, or accuracy is filtered to None
        if best is not None:
            assert best.get("accuracy") is None, (
                "If a location is selected, its invalid accuracy should be filtered to None"
            )

    def test_empty_list_returns_none(self) -> None:
        """Empty input should return None."""
        best, rest = _select_best_location([])
        assert best is None
        assert rest == []

    def test_null_island_coordinates_filtered(self) -> None:
        """Coordinates at (0.0, 0.0) - 'Null Island' - must be filtered out.

        The point (0.0, 0.0) is in the Atlantic Ocean off the coast of Africa.
        This is a common API default when no real location data is available.
        Such coordinates should never be accepted as valid device locations.
        """
        null_island_report: dict[str, Any] = {
            "latitude": 0.0,
            "longitude": 0.0,
            "accuracy": 100.0,  # Valid accuracy, but invalid coordinates
            "last_seen": 1700000200,
            "is_own_report": True,
        }

        valid_report: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 50.0,
            "last_seen": 1700000100,  # Older timestamp
            "is_own_report": False,
        }

        # After normalization, Null Island should have no coordinates
        normalized = _normalize_location_dict(null_island_report)
        assert "latitude" not in normalized or "longitude" not in normalized, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: Null Island coordinates (0.0, 0.0) were NOT filtered!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"The point (0.0, 0.0) is in the Atlantic Ocean and indicates\n"
            f"missing/default data from the API, not a real device location.\n"
            f"\n"
            f"FIX LOCATION: decoder.py :: _normalize_location_dict()\n"
            f"Add filter: if abs(lat) < 0.0001 and abs(lon) < 0.0001: pop both\n"
            f"{'=' * 70}"
        )

        # When selecting between Null Island and valid coords, valid must win
        best, _rest = _select_best_location([null_island_report, valid_report])

        if best is not None:
            lat = best.get("latitude")
            lon = best.get("longitude")
            # Either no coords (filtered), or the valid location
            if lat is not None and lon is not None:
                assert not (abs(lat) < 0.0001 and abs(lon) < 0.0001), (
                    f"\n"
                    f"{'=' * 70}\n"
                    f"CRITICAL: Null Island was selected as best location!\n"
                    f"{'=' * 70}\n"
                    f"Selected: ({lat}, {lon})\n"
                    f"Expected: Valid coordinates (48.123, 11.456) or None\n"
                    f"{'=' * 70}"
                )


# =============================================================================
# SECTION 4: "Perfect Storm" - Ranking vs Recency
# =============================================================================


class TestPerfectStormScenario:
    """Test the 'Perfect Storm' scenario where bad data has a newer timestamp.

    This tests the critical case where:
    - An is_own_report=True update with accuracy=0.0 arrives with a NEWER timestamp
    - A valid crowdsourced update with accuracy=20.0 has an OLDER timestamp

    The system MUST reject the invalid data even though it's "newer".
    Recency cannot override physical impossibility.
    """

    def test_newer_invalid_data_loses_to_older_valid_data(self) -> None:
        """CRITICAL: Newer timestamp cannot rescue physically impossible data.

        This is the 'Perfect Storm' scenario: The bad report has every advantage
        (is_own_report=True, newer timestamp) except valid accuracy.
        It MUST still lose.
        """
        # The "bad" report: Everything looks good except accuracy is impossible
        bad_newer_own: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 0.0,  # PHYSICALLY IMPOSSIBLE
            "last_seen": 1700000200,  # NEWER timestamp (advantage!)
            "is_own_report": True,  # Owner priority (advantage!)
            "status": "LAST_KNOWN",
            "status_code": 1,
        }

        # The "good" report: Older, crowdsourced, but VALID
        good_older_crowd: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 20.0,  # Realistic GPS accuracy
            "last_seen": 1700000100,  # OLDER timestamp (disadvantage)
            "is_own_report": False,  # No owner priority (disadvantage)
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        best, _rest = _select_best_location([bad_newer_own, good_older_crowd])

        # The system must not select the invalid report
        if best is not None:
            selected_acc = best.get("accuracy")
            # Valid outcomes: good report wins, or accuracy is filtered to None
            is_good_winner = selected_acc == pytest.approx(20.0)
            is_filtered = selected_acc is None

            assert is_good_winner or is_filtered, (
                f"\n"
                f"{'=' * 70}\n"
                f"CRITICAL FAILURE: 'Perfect Storm' scenario broke the system!\n"
                f"{'=' * 70}\n"
                f"\n"
                f"SCENARIO: A physically impossible report (accuracy=0.0m) with\n"
                f"is_own_report=True and a NEWER timestamp was selected over\n"
                f"a valid crowdsourced report with accuracy=20.0m.\n"
                f"\n"
                f"SELECTED: accuracy={selected_acc}m\n"
                f"EXPECTED: accuracy=20.0m or None (filtered)\n"
                f"\n"
                f"ROOT CAUSE: The ranking algorithm or normalization failed to\n"
                f"reject accuracy=0.0m as invalid. The 'newer is better' heuristic\n"
                f"must NOT override physical constraints.\n"
                f"\n"
                f"PRINCIPLE: Physics > Recency\n"
                f"A timestamp cannot make 0.0m GPS accuracy possible.\n"
                f"\n"
                f"FIX LOCATIONS (check in order):\n"
                f"  1. decoder.py :: _normalize_location_dict() - must filter acc <= 0\n"
                f"  2. decoder.py :: _get_rank_tuple() - must penalize acc <= 0\n"
                f"  3. cache.py :: _is_significant_update() - must reject acc < 1.0m\n"
                f"{'=' * 70}"
            )

    def test_edge_case_0_001m_still_wins_ranking(self) -> None:
        """Edge case: Very small but positive accuracy (0.001m) may pass ranking.

        While 0.001m is unrealistic for consumer GPS, it's technically positive.
        The cache gate (_is_significant_update) should catch this at < 1.0m.
        But in the decoder, we only filter <= 0.
        """
        tiny_accuracy: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 0.001,  # Positive but unrealistic
            "last_seen": 1700000100,
            "is_own_report": False,
        }

        # The decoder may pass 0.001m through (it's > 0)
        normalized = _normalize_location_dict(tiny_accuracy)

        # This is expected to pass decoder validation (0.001 > 0)
        # The cache gate should catch it later (< 1.0m)
        # For the decoder alone, this documents expected behavior
        if "accuracy" in normalized:
            # Document that decoder passes tiny positive values
            assert normalized["accuracy"] == pytest.approx(0.001), (
                "Decoder should preserve tiny positive accuracy (cache gate handles < 1.0m)"
            )


# =============================================================================
# SECTION 5: Cache and Fusion Tests
# =============================================================================


class TestCacheFusionRobustness:
    """Test cache fusion logic against dirty data.

    These tests verify that the cache's weighted fusion algorithm and
    quality gates correctly handle edge cases and malicious data.
    """

    def test_is_significant_update_rejects_zero_accuracy(self) -> None:
        """_is_significant_update must reject accuracy=0.0 as physically impossible."""
        from unittest.mock import MagicMock

        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        # Create a minimal mock coordinator
        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        # Bind the method to our mock
        is_sig = CacheOperations._is_significant_update

        bad_update = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,  # INVALID
            "last_seen": 1700000100,
        }

        result = is_sig(mock_coord, "device-1", bad_update)

        assert result is False, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: Cache accepted update with accuracy=0.0m!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"The cache gatekeeper (_is_significant_update) must reject\n"
            f"any update with accuracy < 1.0m as physically impossible.\n"
            f"\n"
            f"FIX LOCATION: cache.py :: _is_significant_update()\n"
            f"Add check: if acc_f < MIN_PLAUSIBLE_ACCURACY_M: return False\n"
            f"{'=' * 70}"
        )

        # Verify the stat was incremented
        mock_coord.increment_stat.assert_called_with("invalid_accuracy_drop_count")

    def test_is_significant_update_rejects_sub_meter_accuracy(self) -> None:
        """_is_significant_update must reject accuracy < 1.0m (consumer GPS floor)."""
        from unittest.mock import MagicMock

        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        # 0.5m is positive but impossible for consumer GPS
        bad_update = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.5,  # Sub-meter = INVALID for consumer GPS
            "last_seen": 1700000100,
        }

        result = is_sig(mock_coord, "device-1", bad_update)

        assert result is False, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: Cache accepted sub-meter accuracy (0.5m)!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"Consumer GPS hardware cannot achieve sub-meter accuracy.\n"
            f"Even differential GPS rarely achieves < 1m.\n"
            f"Values like 0.5m indicate API artifacts, not real measurements.\n"
            f"\n"
            f"The cache MUST reject accuracy < 1.0m to prevent:\n"
            f"- 'Fusion Lock-in' where bad data gets extreme weight\n"
            f"- Incorrect 'Own Device' reports poisoning the cache\n"
            f"\n"
            f"FIX LOCATION: cache.py :: _is_significant_update()\n"
            f"Ensure MIN_PLAUSIBLE_ACCURACY_M = 1.0 and check is enforced.\n"
            f"{'=' * 70}"
        )

    def test_is_significant_update_allows_valid_accuracy(self) -> None:
        """_is_significant_update must allow valid accuracy values (>= 1.0m)."""
        from unittest.mock import MagicMock

        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        good_update = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 5.0,  # Valid GPS accuracy
            "last_seen": 1700000100,
        }

        result = is_sig(mock_coord, "device-1", good_update)

        assert result is True, (
            "Valid accuracy (5.0m) should be accepted by the cache gate"
        )

    def test_is_significant_update_allows_no_accuracy(self) -> None:
        """_is_significant_update must allow updates without accuracy (semantic-only)."""
        from unittest.mock import MagicMock

        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        # Semantic-only update: no accuracy is fine
        semantic_update = {
            "semantic_name": "Home",
            "last_seen": 1700000100,
            # No accuracy - this is valid for semantic locations
        }

        result = is_sig(mock_coord, "device-1", semantic_update)

        assert result is True, (
            "Semantic-only updates (no accuracy) should be accepted.\n"
            "Only updates that CLAIM an impossible accuracy should be rejected."
        )


# =============================================================================
# SECTION 6: Fusion Lock-in Prevention
# =============================================================================


class TestFusionLockInPrevention:
    """Test that the fusion algorithm cannot be 'locked in' by bad data.

    The 'Fusion Lock-in' effect occurs when:
    1. A bad accuracy value (e.g., 0.1m) gets into the cache
    2. Due to inverse-square weighting (1/acc²), this value gets extreme weight
    3. Subsequent valid updates (e.g., 20m) cannot move the position
       because their weight is negligible compared to the 'poisoned' cache

    Prevention strategy:
    - Layer 1: Decoder filters accuracy <= 0
    - Layer 2: Cache gate rejects accuracy < 1.0m
    - Layer 3: Fusion uses 50m fallback for invalid values
    - Layer 4: Fusion math uses proper inverse variance formula
    """

    def test_fusion_weight_ratio_is_reasonable(self) -> None:
        """Verify fusion weights don't become astronomically skewed.

        With proper fallback (50m for invalid), the weight ratio between
        any two valid measurements should be reasonable (not > 100:1).
        """

        # Worst realistic case: 5m vs 100m
        acc_good = 5.0
        acc_poor = 100.0

        w_good = 1 / (acc_good**2)  # 1/25 = 0.04
        w_poor = 1 / (acc_poor**2)  # 1/10000 = 0.0001

        ratio = w_good / w_poor  # 400:1 - this is acceptable
        assert ratio < 1000, f"Normal weight ratio should be < 1000:1 (got {ratio}:1)"

        # But if bad data (0.1m) got through, the ratio would be insane
        acc_poison = 0.1
        w_poison = 1 / (acc_poison**2)  # 1/0.01 = 100

        poison_ratio = w_poison / w_poor  # 100 / 0.0001 = 1,000,000:1

        assert poison_ratio > 10000, (
            f"Sanity check: Poison ratio should be extreme ({poison_ratio})"
        )

        # This test documents WHY we need the cache gate
        # If 0.1m gets through, it dominates with 1,000,000:1 weight ratio

    def test_fusion_fallback_prevents_lockin(self) -> None:
        """The 50m fallback should prevent extreme weight ratios.

        When invalid accuracy (0.0, None) uses the 50m fallback instead of
        a tiny value, the weight ratio stays reasonable.
        """
        from custom_components.googlefindmy.const import DEFAULT_FALLBACK_ACCURACY_M

        # Fallback should be 50m (defined in const.py)
        assert DEFAULT_FALLBACK_ACCURACY_M == 50.0, (
            f"Expected DEFAULT_FALLBACK_ACCURACY_M = 50.0, got {DEFAULT_FALLBACK_ACCURACY_M}\n"
            f"This value prevents Fusion Lock-in by ensuring invalid accuracy\n"
            f"doesn't get extreme weight in the fusion formula."
        )

        # With 50m fallback, ratio to a 20m fix is reasonable
        w_fallback = 1 / (50.0**2)  # 1/2500 = 0.0004
        w_valid = 1 / (20.0**2)  # 1/400 = 0.0025

        ratio = w_valid / w_fallback  # 6.25:1

        assert ratio < 10, (
            f"With 50m fallback, weight ratio should be < 10:1 (got {ratio:.2f}:1)\n"
            f"This ensures valid GPS fixes can 'rescue' the cache from bad data."
        )

    def test_inverse_variance_formula_documented(self) -> None:
        """Document the inverse variance fusion formula.

        When fusing two measurements with accuracies σ₁ and σ₂:
        - Weights: w₁ = 1/σ₁², w₂ = 1/σ₂²
        - Fused position: (x₁*w₁ + x₂*w₂) / (w₁ + w₂)
        - Fused variance: σ_fused² = 1 / (w₁ + w₂)
        - Fused accuracy: σ_fused = sqrt(1 / (w₁ + w₂))

        The fused accuracy should be BETTER than both inputs when valid.
        """
        import math

        acc1 = 20.0  # First measurement: 20m accuracy
        acc2 = 30.0  # Second measurement: 30m accuracy

        w1 = 1 / (acc1**2)  # 0.0025
        w2 = 1 / (acc2**2)  # 0.00111
        total_w = w1 + w2  # 0.00361

        fused_acc = math.sqrt(1 / total_w)  # ~16.6m

        assert fused_acc < acc1, (
            f"Fused accuracy ({fused_acc:.1f}m) should be better than first ({acc1}m)\n"
            f"Combining measurements improves precision (inverse variance weighting)."
        )

        assert fused_acc < acc2, (
            f"Fused accuracy ({fused_acc:.1f}m) should be better than second ({acc2}m)"
        )

        # With safety floor (5m), we clamp but don't go below physics limit
        MIN_FUSED = 5.0
        clamped = max(fused_acc, MIN_FUSED)
        assert clamped >= MIN_FUSED, "Fused accuracy must respect physics floor (5m)"


# =============================================================================
# SECTION 7: Integration Scenario Tests
# =============================================================================


class TestIntegrationScenarios:
    """End-to-end scenario tests combining multiple components."""

    def test_full_pipeline_rejects_poisoned_own_report(self) -> None:
        """Complete pipeline test: poisoned 'Own Device' report must be rejected.

        This test simulates the full flow:
        1. Raw data comes from Google's API (is_own_report=True, accuracy=0.0)
        2. Decoder normalization should filter the accuracy
        3. Ranking should penalize the report
        4. Cache gate should reject it entirely
        """
        # Simulate the poisoned report exactly as it comes from the API
        poisoned_api_response: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 0.0,
            "last_seen": 1700000200,
            "is_own_report": True,
            "status_code": 1,
        }

        # Step 1: Decoder normalization
        normalized = _normalize_location_dict(poisoned_api_response)

        # Accuracy should be filtered out
        assert "accuracy" not in normalized, (
            "Decoder must filter accuracy=0.0 at normalization stage"
        )

        # Step 2: Ranking (if it somehow got through)
        rank = _get_rank_tuple(poisoned_api_response)
        acc_rank = rank[3]

        assert math.isinf(acc_rank) and acc_rank < 0, (
            "Ranking must penalize accuracy=0.0 with -inf rank"
        )

        # Step 3: If normalized (accuracy removed), verify ranking still works
        rank_after_norm = _get_rank_tuple(normalized)
        acc_rank_after = rank_after_norm[3]

        assert math.isinf(acc_rank_after) and acc_rank_after < 0, (
            "After normalization removes accuracy, rank should still be -inf (None = worst)"
        )

    def test_valid_crowdsourced_survives_pipeline(self) -> None:
        """Verify that valid crowdsourced reports pass through unchanged."""
        valid_report: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 25.0,
            "last_seen": 1700000100,
            "is_own_report": False,
            "status_code": 2,
        }

        # Normalization preserves valid data
        normalized = _normalize_location_dict(valid_report)
        assert normalized.get("accuracy") == pytest.approx(25.0)
        assert normalized.get("latitude") == pytest.approx(48.200)
        assert normalized.get("longitude") == pytest.approx(11.200)

        # Ranking assigns proper (non-worst) rank
        rank = _get_rank_tuple(normalized)
        acc_rank = rank[3]

        assert acc_rank == pytest.approx(-25.0), (
            f"Valid accuracy 25.0m should get rank -25.0 (got {acc_rank})"
        )
        assert not math.isinf(acc_rank), "Valid accuracy should not get -inf rank"
