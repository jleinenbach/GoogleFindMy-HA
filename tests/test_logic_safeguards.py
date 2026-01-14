# tests/test_logic_safeguards.py
"""Logic safeguard tests for data integrity and identity protection.

These tests verify two critical invariants:
1. Identity Integrity: Lower-ranked reports cannot "steal" a higher-ranked report's
   identity by having the same timestamp.
2. Zero Accuracy Handling: Reports with accuracy=0.0 are NOT rejected, but the
   accuracy is sanitized (removed or replaced with fallback).

BACKGROUND:
- Google's API sometimes returns accuracy=0.0 as "privacy masking" or "unknown"
- Batch updates often have identical timestamps for multiple reports
- Without proper protection, these could corrupt the data integrity
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.ProtoDecoders.decoder import (
    _get_rank_tuple,
    _merge_semantics_if_near_ts,
    _normalize_location_dict,
    _select_best_location,
)

# =============================================================================
# SECTION 1: Identity Integrity at Timestamp Collision
# =============================================================================


class TestIdentityIntegrityAtCollision:
    """Test that timestamp collisions don't cause identity theft.

    SCENARIO:
    - Candidate A (Own Device): High rank, has or doesn't have coordinates
    - Candidate B (Crowdsourced): Lower rank, has different coordinates
    - Both have the SAME timestamp

    INVARIANT: Candidate A must NOT inherit B's coordinates just because
    B happens to be in the same batch update. Only if B is STRICTLY NEWER
    should its coordinates be considered.
    """

    def test_own_device_without_coords_does_not_steal_crowdsourced_coords(
        self,
    ) -> None:
        """When Own Device has no coords, it must NOT borrow from same-timestamp Crowdsourced.

        This is the core "identity theft" scenario:
        - Own Device (high rank) has semantic info but no coordinates
        - Crowdsourced (lower rank) has coordinates, same timestamp
        - Result must NOT have Crowdsourced coordinates attached to Own Device identity
        """
        # Own Device: High rank, semantic-only (no coordinates)
        own_device: dict[str, Any] = {
            "semantic_name": "Home",
            "last_seen": 1700000100,
            "is_own_report": True,
            "status": "LAST_KNOWN",
            "status_code": 1,
            # NO latitude/longitude - this is a semantic-only report
        }

        # Crowdsourced: Lower rank, has coordinates, SAME timestamp
        crowdsourced: dict[str, Any] = {
            "latitude": 50.0,
            "longitude": 10.0,
            "accuracy": 20.0,
            "last_seen": 1700000100,  # SAME timestamp as Own Device
            "is_own_report": False,
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        # Normalize both
        normed_own = _normalize_location_dict(own_device)
        normed_crowd = _normalize_location_dict(crowdsourced)

        # Verify ranking: Own Device should rank higher
        rank_own = _get_rank_tuple(normed_own)
        rank_crowd = _get_rank_tuple(normed_crowd)
        assert rank_own > rank_crowd, (
            f"Own Device should rank higher than Crowdsourced.\n"
            f"Own: {rank_own}\nCrowd: {rank_crowd}"
        )

        # Run merge with Own Device as "best" (sorted by rank, highest first)
        normed_cands = [normed_own, normed_crowd]
        result = _merge_semantics_if_near_ts(dict(normed_own), normed_cands)

        # CRITICAL: Result must NOT have Crowdsourced coordinates
        # because Crowdsourced is not NEWER, only EQUAL timestamp
        result_lat = result.get("latitude")
        result_lon = result.get("longitude")

        has_coords = result_lat is not None and result_lon is not None

        assert not has_coords, (
            f"\n"
            f"{'=' * 70}\n"
            f"IDENTITY THEFT DETECTED!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"Own Device (semantic-only) inherited Crowdsourced coordinates\n"
            f"even though Crowdsourced has the SAME timestamp (not newer).\n"
            f"\n"
            f"Result coordinates: ({result_lat}, {result_lon})\n"
            f"Expected: No coordinates (semantic-only should remain semantic-only)\n"
            f"\n"
            f"This is IDENTITY THEFT: attaching untrusted coordinates\n"
            f"to a trusted 'Own Device' identity.\n"
            f"\n"
            f"FIX: In _merge_semantics_if_near_ts, initialize best_coordinate_ts\n"
            f"to t_best (not -inf) to prevent same-timestamp stealing.\n"
            f"{'=' * 70}"
        )

        # Verify semantic name is preserved
        assert result.get("semantic_name") == "Home", (
            "Own Device's semantic name should be preserved"
        )

    def test_own_device_with_coords_is_not_overwritten_by_crowdsourced(
        self,
    ) -> None:
        """When Own Device HAS coordinates, they must not be overwritten by same-timestamp Crowdsourced."""
        # Own Device: High rank, has coordinates
        own_device: dict[str, Any] = {
            "latitude": 48.1,
            "longitude": 11.1,
            "accuracy": 25.0,
            "last_seen": 1700000100,
            "is_own_report": True,
            "status": "LAST_KNOWN",
            "status_code": 1,
        }

        # Crowdsourced: Lower rank, DIFFERENT coordinates, SAME timestamp
        crowdsourced: dict[str, Any] = {
            "latitude": 50.0,  # Different!
            "longitude": 10.0,  # Different!
            "accuracy": 15.0,  # Better accuracy (but lower rank)
            "last_seen": 1700000100,  # SAME timestamp
            "is_own_report": False,
            "status": "CROWDSOURCED",
            "status_code": 2,
        }

        normed_own = _normalize_location_dict(own_device)
        normed_crowd = _normalize_location_dict(crowdsourced)
        normed_cands = [normed_own, normed_crowd]

        result = _merge_semantics_if_near_ts(dict(normed_own), normed_cands)

        # Own Device coordinates must be preserved
        assert result.get("latitude") == pytest.approx(48.1), (
            f"Own Device latitude should be preserved, got {result.get('latitude')}"
        )
        assert result.get("longitude") == pytest.approx(11.1), (
            f"Own Device longitude should be preserved, got {result.get('longitude')}"
        )

    def test_strictly_newer_timestamp_can_update_coordinates(self) -> None:
        """A STRICTLY newer report CAN update coordinates (this is valid behavior)."""
        # Own Device: Has coordinates
        own_device: dict[str, Any] = {
            "latitude": 48.1,
            "longitude": 11.1,
            "accuracy": 25.0,
            "last_seen": 1700000100,  # OLDER
            "is_own_report": True,
        }

        # Crowdsourced: NEWER timestamp
        crowdsourced: dict[str, Any] = {
            "latitude": 50.0,
            "longitude": 10.0,
            "accuracy": 15.0,
            "last_seen": 1700000200,  # STRICTLY NEWER (+100s)
            "is_own_report": False,
        }

        normed_own = _normalize_location_dict(own_device)
        normed_crowd = _normalize_location_dict(crowdsourced)
        normed_cands = [normed_own, normed_crowd]

        result = _merge_semantics_if_near_ts(dict(normed_own), normed_cands)

        # Newer coordinates CAN be used (this is valid)
        assert result.get("latitude") == pytest.approx(50.0), (
            "Strictly newer timestamp should update coordinates"
        )
        assert result.get("longitude") == pytest.approx(10.0)


# =============================================================================
# SECTION 2: Zero Accuracy Handling (Downgrade, not Reject)
# =============================================================================


class TestZeroAccuracyDowngrade:
    """Test that accuracy=0.0 is sanitized, not rejected.

    PRINCIPLE: 0.0 means "unknown/masked", not "perfect precision".
    The REPORT should be kept, but accuracy should be treated as unmeasured.

    INVARIANT:
    1. Reports with accuracy=0.0 are NOT rejected
    2. The accuracy field is either removed or set to fallback
    3. In fusion, such reports don't dominate (low weight due to fallback)
    """

    def test_zero_accuracy_report_is_accepted(self) -> None:
        """A report with accuracy=0.0 must be ACCEPTED (not rejected)."""
        report: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,  # This means "unknown", not "perfect"
            "last_seen": 1700000100,
            "is_own_report": True,
        }

        normalized = _normalize_location_dict(report)

        # Report must still have coordinates (was not rejected)
        assert normalized.get("latitude") == pytest.approx(48.123), (
            "Report with 0.0 accuracy should be ACCEPTED, coordinates preserved"
        )
        assert normalized.get("longitude") == pytest.approx(11.456)

    def test_zero_accuracy_is_sanitized_to_none(self) -> None:
        """accuracy=0.0 must be REMOVED (not kept as 0.0)."""
        report: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,
            "last_seen": 1700000100,
        }

        normalized = _normalize_location_dict(report)

        # Accuracy must be removed
        assert "accuracy" not in normalized or normalized.get("accuracy") is None, (
            f"\n"
            f"{'=' * 70}\n"
            f"ZERO ACCURACY NOT SANITIZED!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"accuracy=0.0 was kept instead of being removed.\n"
            f"Stored accuracy: {normalized.get('accuracy')}\n"
            f"Expected: None (key should not exist)\n"
            f"\n"
            f"This will corrupt ranking: 0.0 would get acc_rank = -0.0,\n"
            f"which is BETTER than valid entries like -20.0.\n"
            f"\n"
            f"FIX: In _normalize_location_dict, remove accuracy if < 1.0m.\n"
            f"{'=' * 70}"
        )

    def test_zero_accuracy_gets_worst_ranking(self) -> None:
        """After sanitization, the report should get acc_rank = -inf."""
        report: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,
            "last_seen": 1700000100,
        }

        normalized = _normalize_location_dict(report)
        rank = _get_rank_tuple(normalized)
        acc_rank = rank[3]

        assert math.isinf(acc_rank) and acc_rank < 0, (
            f"After sanitization (accuracy removed), acc_rank should be -inf.\n"
            f"Got: {acc_rank}"
        )

    def test_valid_report_beats_sanitized_zero_accuracy(self) -> None:
        """A valid 20m report should beat a sanitized 0.0m report when other factors are equal.

        IMPORTANT: The ranking tuple is (seen_rank, status_rank, has_coords_rank, acc_rank).
        Timestamp and status are evaluated BEFORE accuracy. To test accuracy tiebreaker,
        we must make timestamp and status EQUAL so only accuracy differs.
        """
        # Report with 0.0 accuracy (will be sanitized)
        masked_report: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 0.0,  # This will be sanitized → acc_rank = -inf
            "last_seen": 1700000100,  # SAME timestamp
            "is_own_report": False,  # SAME status
        }

        # Valid report with real accuracy
        valid_report: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 20.0,  # Valid GPS accuracy → acc_rank = -20.0
            "last_seen": 1700000100,  # SAME timestamp
            "is_own_report": False,  # SAME status
        }

        best, _rest = _select_best_location([masked_report, valid_report])

        assert best is not None, "Should select a location"

        # When timestamp and status are EQUAL, accuracy becomes the tiebreaker:
        # - masked_report has accuracy=0.0 → sanitized to None → acc_rank = -inf
        # - valid_report has accuracy=20.0 → acc_rank = -20.0 (better than -inf)
        # Therefore valid_report should win.
        best_acc = best.get("accuracy")
        assert best_acc == pytest.approx(20.0), (
            f"\n"
            f"{'=' * 70}\n"
            f"MASKED REPORT INCORRECTLY WON!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"A report with accuracy=0.0 (now sanitized) beat a valid 20m report\n"
            f"when timestamp and status were EQUAL.\n"
            f"\n"
            f"Selected accuracy: {best_acc}\n"
            f"Expected: 20.0 (from valid report)\n"
            f"\n"
            f"This proves acc_rank = -inf is not being applied correctly.\n"
            f"{'=' * 70}"
        )

    def test_cache_accepts_zero_accuracy_with_sanitization(self) -> None:
        """cache.py must ACCEPT updates with accuracy=0.0 (sanitize, not reject)."""
        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        # Update with 0.0 accuracy
        update_with_zero_acc = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,
            "last_seen": 1700000100,
        }

        result = is_sig(mock_coord, "device-1", update_with_zero_acc)

        # Update must be ACCEPTED (not rejected)
        assert result is True, (
            f"\n"
            f"{'=' * 70}\n"
            f"CACHE REJECTED VALID UPDATE!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"An update with accuracy=0.0 was REJECTED instead of SANITIZED.\n"
            f"\n"
            f"The report contains valid location data. Only the accuracy\n"
            f"is invalid and should be removed, not the entire update.\n"
            f"\n"
            f"FIX: In _is_significant_update, sanitize invalid accuracy\n"
            f"(remove from dict) instead of returning False.\n"
            f"{'=' * 70}"
        )

        # Accuracy should have been removed from the update dict
        assert "accuracy" not in update_with_zero_acc, (
            "Invalid accuracy should be removed from update dict"
        )

        # Verify sanitization was counted
        mock_coord.increment_stat.assert_called_with("accuracy_sanitized_count")


# =============================================================================
# SECTION 3: Fusion Behavior with Sanitized Accuracy
# =============================================================================


class TestFusionWithSanitizedAccuracy:
    """Test that fusion correctly handles sanitized (fallback) accuracy."""

    def test_valid_accuracy_dominates_over_fallback(self) -> None:
        """When fusing, a valid 20m accuracy should dominate over 50m fallback.

        If a cached entry has accuracy=0.0 (now treated as 50m fallback),
        a new entry with accuracy=20m should have MORE weight in fusion.
        """
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            DEFAULT_ACCURACY_FALLBACK_M,
        )

        # Fallback accuracy (used for sanitized 0.0)
        fallback_acc = DEFAULT_ACCURACY_FALLBACK_M  # 50m

        # Valid accuracy
        valid_acc = 20.0

        # Calculate weights
        w_fallback = 1 / (fallback_acc**2)  # 1/2500 = 0.0004
        w_valid = 1 / (valid_acc**2)  # 1/400 = 0.0025

        total_w = w_fallback + w_valid
        valid_weight_fraction = w_valid / total_w

        # Valid accuracy should have > 50% weight
        assert valid_weight_fraction > 0.5, (
            f"Valid 20m accuracy should dominate over 50m fallback.\n"
            f"Valid weight fraction: {valid_weight_fraction:.1%}\n"
            f"Expected: > 50%"
        )

        # Specifically, 20m vs 50m → valid gets 6.25x more weight
        weight_ratio = w_valid / w_fallback
        assert weight_ratio > 6, (
            f"20m accuracy should have ~6x weight of 50m fallback.\n"
            f"Actual ratio: {weight_ratio:.2f}x"
        )
