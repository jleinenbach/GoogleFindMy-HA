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

        best, _rest = _select_best_location([spurious_own_report, legitimate_crowdsourced])

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
        """Reports without coordinates should not be selected as best location."""
        no_coords: dict[str, Any] = {
            "accuracy": 5.0,
            "last_seen": 1700000200,
            "is_own_report": True,
            "semantic_name": "Home",
        }

        with_coords: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 50.0,
            "last_seen": 1700000100,
            "is_own_report": False,
        }

        best, _rest = _select_best_location([no_coords, with_coords])

        assert best is not None
        assert "latitude" in best and "longitude" in best, (
            "Should select the report with actual coordinates"
        )

    def test_all_invalid_returns_none_or_filters(self) -> None:
        """If all reports have invalid accuracy, result should be filtered/empty."""
        all_bad = [
            {"latitude": 48.1, "longitude": 11.1, "accuracy": 0.0, "last_seen": 1700000100},
            {"latitude": 48.2, "longitude": 11.2, "accuracy": -5.0, "last_seen": 1700000100},
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
