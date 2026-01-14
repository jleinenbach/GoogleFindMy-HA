# tests/test_logic_safeguards.py
"""Logic safeguard tests for data integrity and identity protection.

These tests verify three critical invariants:

1. **Micro-Precision Survival**: Modern dual-frequency GNSS (L1+L5) can achieve
   sub-meter accuracy. Values like 0.002m (2mm) must be preserved, NOT filtered.

2. **Zero-Accuracy Downgrade**: The Android Location API uses 0.0 as an error code
   meaning "no accuracy available". This must be sanitized (removed) but the report
   itself must NOT be rejected.

3. **Identity Integrity**: Lower-ranked reports cannot "steal" a higher-ranked
   report's identity by having the same timestamp (First-Win Principle).

CONSTANTS:
- MIN_VALID_ACCURACY = 0.001 (1mm threshold for error code detection)
- DEFAULT_ACCURACY_FALLBACK = 2000.0 (conservative fallback for unknown)
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.coordinator.helpers.geo import (
    DEFAULT_ACCURACY_FALLBACK,
    MIN_VALID_ACCURACY,
    is_valid_accuracy,
    safe_accuracy,
)
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    _get_rank_tuple,
    _merge_semantics_if_near_ts,
    _normalize_location_dict,
    _select_best_location,
)


# =============================================================================
# SECTION 1: Micro-Precision Survival (Sub-Meter Accuracy)
# =============================================================================


class TestMicroPrecisionSurvival:
    """Test that valid sub-meter accuracy is preserved, not filtered.

    Modern dual-frequency GNSS chips (L1+L5) can achieve accuracies like:
    - 0.5m under ideal Open Sky conditions
    - 0.01m (1cm) in theoretical edge cases
    - 0.002m (2mm) as an extreme test case

    These MUST be preserved because they represent real measurements from
    high-end hardware, NOT error codes.
    """

    @pytest.mark.parametrize(
        ("accuracy", "expected_preserved"),
        [
            (0.002, True),    # 2mm - extreme precision, MUST be preserved
            (0.01, True),     # 1cm - centimeter precision, valid
            (0.1, True),      # 10cm - sub-meter, valid
            (0.5, True),      # 50cm - common sub-meter, valid
            (1.0, True),      # 1m - valid
            (5.0, True),      # 5m - typical GPS, valid
            (50.0, True),     # 50m - moderate GPS, valid
            (0.0, False),     # Error code - MUST be removed
            (0.0001, False),  # Below threshold - error code
            (-1.0, False),    # Negative - error code
        ],
        ids=[
            "2mm_preserved",
            "1cm_preserved",
            "10cm_preserved",
            "50cm_preserved",
            "1m_preserved",
            "5m_preserved",
            "50m_preserved",
            "0.0_removed",
            "0.0001_removed",
            "negative_removed",
        ],
    )
    def test_normalize_preserves_valid_accuracy(
        self, accuracy: float, expected_preserved: bool
    ) -> None:
        """_normalize_location_dict must preserve valid accuracy values."""
        loc: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": accuracy,
            "last_seen": 1700000000,
        }

        result = _normalize_location_dict(loc)

        if expected_preserved:
            assert "accuracy" in result, (
                f"Valid accuracy {accuracy}m was incorrectly removed!\n"
                f"Modern GNSS can achieve sub-meter accuracy.\n"
                f"Only the error code (0.0) should be filtered."
            )
            assert result["accuracy"] == pytest.approx(accuracy)
        else:
            assert "accuracy" not in result, (
                f"Error code accuracy {accuracy}m was NOT removed!\n"
                f"Values < {MIN_VALID_ACCURACY}m should be filtered."
            )

    def test_micro_precision_beats_moderate_accuracy_in_ranking(self) -> None:
        """A 2mm report must beat a 500mm report (smaller accuracy = better)."""
        micro_precision: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 0.002,  # 2mm - extreme precision
            "last_seen": 1700000100,
            "is_own_report": False,
        }

        moderate_accuracy: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 0.5,  # 500mm - moderate
            "last_seen": 1700000100,  # SAME timestamp
            "is_own_report": False,   # SAME status
        }

        best, _rest = _select_best_location([micro_precision, moderate_accuracy])

        assert best is not None, "Should select a location"
        best_acc = best.get("accuracy")

        # 0.002m should win because smaller = more precise = higher rank
        assert best_acc == pytest.approx(0.002), (
            f"\n"
            f"{'=' * 70}\n"
            f"MICRO-PRECISION LOST TO MODERATE ACCURACY!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"Expected: 0.002m (2mm precision)\n"
            f"Got: {best_acc}m\n"
            f"\n"
            f"Smaller accuracy = more precise = should win.\n"
            f"Modern GNSS can achieve sub-meter accuracy.\n"
            f"{'=' * 70}"
        )


# =============================================================================
# SECTION 2: Zero-Accuracy Downgrade
# =============================================================================


class TestZeroAccuracyDowngrade:
    """Test that accuracy=0.0 (error code) is sanitized but report is kept.

    The Android Location API uses 0.0 as an error code meaning "no accuracy
    available". This is NOT "perfect precision" but rather "unknown".

    BEHAVIOR:
    - The accuracy KEY is removed from the dict
    - The REPORT is kept (location data is valid)
    - In ranking, missing accuracy gets acc_rank = -inf (lowest priority)
    """

    def test_zero_accuracy_is_sanitized_not_rejected(self) -> None:
        """accuracy=0.0 must be REMOVED from dict, not reject the report."""
        loc: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,  # Error code
            "last_seen": 1700000000,
        }

        result = _normalize_location_dict(loc)

        # Report must be kept (has valid coordinates)
        assert result.get("latitude") == pytest.approx(48.123), (
            "Report should be kept, only accuracy removed"
        )
        assert result.get("longitude") == pytest.approx(11.456), (
            "Report should be kept, only accuracy removed"
        )

        # Accuracy must be removed
        assert "accuracy" not in result, (
            "accuracy=0.0 (error code) should be REMOVED from dict"
        )

    def test_zero_accuracy_loses_to_real_data_in_ranking(self) -> None:
        """A report with sanitized 0.0 accuracy must lose to real data."""
        zero_acc_report: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 0.0,  # Will be sanitized → acc_rank = -inf
            "last_seen": 1700000100,
            "is_own_report": False,
        }

        real_data_report: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 50.0,  # Valid accuracy → acc_rank = -50.0
            "last_seen": 1700000100,  # SAME timestamp
            "is_own_report": False,   # SAME status
        }

        best, _rest = _select_best_location([zero_acc_report, real_data_report])

        assert best is not None
        best_acc = best.get("accuracy")

        # Real data (50m) should win because 0.0 is sanitized to None → -inf
        assert best_acc == pytest.approx(50.0), (
            f"\n"
            f"{'=' * 70}\n"
            f"ZERO-ACCURACY REPORT WON!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"Expected: 50.0m (real data)\n"
            f"Got: {best_acc}\n"
            f"\n"
            f"accuracy=0.0 means 'unknown', not 'perfect'.\n"
            f"It should lose to any real measurement.\n"
            f"{'=' * 70}"
        )

    def test_safe_accuracy_fallback_for_zero(self) -> None:
        """safe_accuracy(0.0) must return DEFAULT_ACCURACY_FALLBACK."""
        result = safe_accuracy(0.0)

        assert result == DEFAULT_ACCURACY_FALLBACK, (
            f"safe_accuracy(0.0) should return {DEFAULT_ACCURACY_FALLBACK}m, "
            f"got {result}m"
        )

    def test_is_valid_accuracy_rejects_error_code(self) -> None:
        """is_valid_accuracy must return False for error codes."""
        assert is_valid_accuracy(0.0) is False, "0.0 is error code"
        assert is_valid_accuracy(0.0001) is False, f"< {MIN_VALID_ACCURACY} is error"
        assert is_valid_accuracy(-1.0) is False, "Negative is error"
        assert is_valid_accuracy(None) is False, "None is invalid"

    def test_is_valid_accuracy_accepts_sub_meter(self) -> None:
        """is_valid_accuracy must return True for valid sub-meter values."""
        assert is_valid_accuracy(0.002) is True, "2mm is valid GNSS"
        assert is_valid_accuracy(0.5) is True, "50cm is valid GNSS"
        assert is_valid_accuracy(1.0) is True, "1m is valid"
        assert is_valid_accuracy(50.0) is True, "50m is valid"


# =============================================================================
# SECTION 3: Identity Integrity at Timestamp Collision
# =============================================================================


class TestIdentityIntegrity:
    """Test that timestamp collisions don't cause identity theft.

    SCENARIO:
    - Candidate A: is_own_report=True, lat=10 (higher ranked, WINNER)
    - Candidate B: is_own_report=False, lat=50 (lower ranked)
    - Both have the SAME timestamp

    INVARIANT: Result must be lat=10 (from A).
    B must NOT steal A's identity by having the same timestamp.
    """

    def test_own_device_wins_at_timestamp_collision(self) -> None:
        """Own device report wins against crowdsourced at same timestamp."""
        own_device: dict[str, Any] = {
            "latitude": 10.0,
            "longitude": 20.0,
            "accuracy": 20.0,
            "last_seen": 1700000100,  # SAME timestamp
            "is_own_report": True,     # HIGHER status → WINNER
        }

        crowdsourced: dict[str, Any] = {
            "latitude": 50.0,
            "longitude": 60.0,
            "accuracy": 15.0,  # Even better accuracy
            "last_seen": 1700000100,  # SAME timestamp
            "is_own_report": False,    # Lower status
        }

        best, _rest = _select_best_location([own_device, crowdsourced])

        assert best is not None
        assert best.get("latitude") == pytest.approx(10.0), (
            f"\n"
            f"{'=' * 70}\n"
            f"IDENTITY THEFT DETECTED!\n"
            f"{'=' * 70}\n"
            f"\n"
            f"Expected latitude: 10.0 (from own_device)\n"
            f"Got latitude: {best.get('latitude')}\n"
            f"\n"
            f"Own device (is_own_report=True) must win against crowdsourced\n"
            f"even when they have the same timestamp.\n"
            f"{'=' * 70}"
        )

    def test_merge_semantics_respects_best_coordinates(self) -> None:
        """_merge_semantics_if_near_ts must not let B steal A's coordinates."""
        best: dict[str, Any] = {
            "latitude": 10.0,
            "longitude": 20.0,
            "accuracy": 20.0,
            "last_seen": 1700000100,
        }

        # Candidate with SAME timestamp should NOT steal coordinates
        same_ts_candidate: dict[str, Any] = {
            "latitude": 50.0,  # Different coords
            "longitude": 60.0,
            "accuracy": 10.0,  # Better accuracy
            "last_seen": 1700000100,  # SAME timestamp as best
        }

        result = _merge_semantics_if_near_ts(best, [same_ts_candidate])

        # Best's coordinates must be preserved
        assert result.get("latitude") == pytest.approx(10.0), (
            "Best's latitude must be preserved at timestamp collision"
        )
        assert result.get("longitude") == pytest.approx(20.0), (
            "Best's longitude must be preserved at timestamp collision"
        )

    def test_newer_timestamp_can_update_coordinates(self) -> None:
        """A STRICTLY NEWER candidate CAN update coordinates (by design)."""
        best: dict[str, Any] = {
            "latitude": 10.0,
            "longitude": 20.0,
            "accuracy": 20.0,
            "last_seen": 1700000100,
        }

        # Candidate with NEWER timestamp CAN provide coordinates
        newer_candidate: dict[str, Any] = {
            "latitude": 50.0,
            "longitude": 60.0,
            "accuracy": 10.0,
            "last_seen": 1700000200,  # NEWER than best
        }

        result = _merge_semantics_if_near_ts(best, [newer_candidate])

        # Newer candidate's coordinates should be used
        assert result.get("latitude") == pytest.approx(50.0), (
            "Strictly newer candidate should provide coordinates"
        )
        assert result.get("longitude") == pytest.approx(60.0), (
            "Strictly newer candidate should provide coordinates"
        )


# =============================================================================
# SECTION 4: Cache Gatekeeper Tests
# =============================================================================


class TestCacheGatekeeper:
    """Test cache.py _is_significant_update with new thresholds."""

    def test_cache_sanitizes_zero_accuracy(self) -> None:
        """_is_significant_update must SANITIZE 0.0 to fallback value."""
        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        update = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,  # Will be sanitized to fallback
            "last_seen": 1700000100,
        }

        result = is_sig(mock_coord, "device-1", update)

        # Must be ACCEPTED
        assert result is True, "Update should be ACCEPTED (sanitized)"

        # Accuracy must be SET to fallback (not None, not removed)
        assert update.get("accuracy") == DEFAULT_ACCURACY_FALLBACK, (
            f"0.0 accuracy should be set to fallback ({DEFAULT_ACCURACY_FALLBACK}m)"
        )

        # Stat must be incremented
        mock_coord.increment_stat.assert_called_with("accuracy_sanitized_count")

    def test_cache_preserves_sub_meter_accuracy(self) -> None:
        """_is_significant_update must PRESERVE valid sub-meter accuracy."""
        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        for valid_acc in [0.002, 0.5, 1.0, 50.0]:
            update = {
                "latitude": 48.123,
                "longitude": 11.456,
                "accuracy": valid_acc,
                "last_seen": 1700000100,
            }

            result = is_sig(mock_coord, "device-1", update)

            assert result is True, f"Update with {valid_acc}m should be accepted"
            assert update.get("accuracy") == valid_acc, (
                f"Valid accuracy {valid_acc}m should be PRESERVED"
            )


# =============================================================================
# SECTION 5: Fusion Self-Healing Tests
# =============================================================================


class TestFusionSelfHealing:
    """Test that cache fusion properly handles corrupted accuracy values."""

    def test_safe_accuracy_self_healing(self) -> None:
        """safe_accuracy must replace corrupted values with fallback."""
        # Error codes should get fallback
        assert safe_accuracy(0.0) == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy(-1.0) == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy(0.0001) == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy(None) == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy(float("nan")) == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy(float("inf")) == DEFAULT_ACCURACY_FALLBACK

        # Valid values should be preserved
        assert safe_accuracy(0.002) == 0.002
        assert safe_accuracy(0.5) == 0.5
        assert safe_accuracy(50.0) == 50.0

    def test_fusion_weight_calculation(self) -> None:
        """Corrupted accuracy (0.0) should have near-zero weight in fusion."""
        # Weight is 1/accuracy² - smaller accuracy = higher weight
        # 0.0 → fallback (2000m) → weight = 1/2000² = 0.00000025
        # 50m → weight = 1/50² = 0.0004

        fallback_weight = 1 / (DEFAULT_ACCURACY_FALLBACK ** 2)
        real_weight = 1 / (50.0 ** 2)

        # Real data should have ~1600x more weight than fallback
        weight_ratio = real_weight / fallback_weight

        assert weight_ratio > 1000, (
            f"Real data (50m) should have >1000x more weight than fallback.\n"
            f"Ratio: {weight_ratio:.1f}x\n"
            f"This ensures corrupted values (0.0) are quickly overwritten."
        )


# =============================================================================
# SECTION 6: Missing Key Crash Test
# =============================================================================


class TestMissingKeyCrashPrevention:
    """Test that missing accuracy key doesn't crash downstream code.

    When the decoder strips the 'accuracy' key (because it was 0.0),
    the cache must handle this gracefully and write back a fallback value.
    Home Assistant requires a numeric gps_accuracy attribute.
    """

    def test_cache_handles_stripped_accuracy_gracefully(self) -> None:
        """_is_significant_update must handle missing accuracy key without crash.

        Scenario:
        1. Decoder receives accuracy=0.0 and REMOVES the key
        2. Cache receives dict WITHOUT accuracy key
        3. Cache must NOT crash and must SET a fallback value
        """
        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        # Dict WITHOUT accuracy key (simulating decoder stripping it)
        update_without_accuracy = {
            "latitude": 48.123,
            "longitude": 11.456,
            # NO "accuracy" key!
            "last_seen": 1700000100,
        }

        # This must NOT raise an exception
        result = is_sig(mock_coord, "device-1", update_without_accuracy)

        # Must be accepted
        assert result is True, "Update should be accepted even without accuracy key"

        # CRITICAL: Accuracy must be set to fallback (not None, not missing)
        assert "accuracy" in update_without_accuracy, (
            "Cache must SET accuracy key when it was missing"
        )
        assert update_without_accuracy["accuracy"] == DEFAULT_ACCURACY_FALLBACK, (
            f"Missing accuracy should be set to fallback ({DEFAULT_ACCURACY_FALLBACK}m), "
            f"got {update_without_accuracy.get('accuracy')}"
        )

    def test_safe_accuracy_handles_any_input(self) -> None:
        """safe_accuracy must handle ANY input type without crashing."""
        # All of these must return fallback without exception
        assert safe_accuracy(None) == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy("invalid") == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy([1, 2, 3]) == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy({"key": "value"}) == DEFAULT_ACCURACY_FALLBACK
        assert safe_accuracy(object()) == DEFAULT_ACCURACY_FALLBACK

        # Valid inputs should be preserved
        assert safe_accuracy(50.0) == 50.0
        assert safe_accuracy(0.5) == 0.5
        assert safe_accuracy("20.0") == 20.0  # String that can be converted


# =============================================================================
# SECTION 7: Retention Test (Zero Accuracy Data Preservation)
# =============================================================================


class TestZeroAccuracyRetention:
    """Test that data with accuracy=0.0 is KEPT but downgraded.

    The "Sanitize-not-Reject" strategy means:
    - Coordinates are preserved (no data loss)
    - Accuracy is set to fallback (honest about uncertainty)
    - Report loses to real GPS data in ranking (correct prioritization)
    """

    def test_zero_accuracy_is_kept_but_downgraded(self) -> None:
        """Data with accuracy=0.0 must be retained with downgraded accuracy.

        Chain:
        1. Decoder: Sees 0.0 -> removes key (for ranking purposes)
        2. Cache: Sees missing key -> sets fallback 2000.0
        3. Result: Location preserved, marked as "inaccurate"
        """
        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        # Simulate: decoder already stripped the accuracy (was 0.0)
        update_with_stripped_accuracy = {
            "latitude": 48.123,
            "longitude": 11.456,
            # accuracy was 0.0, decoder removed it
            "last_seen": 1700000100,
        }

        result = is_sig(mock_coord, "device-1", update_with_stripped_accuracy)

        # Assert 1: No data loss - coordinates are preserved
        assert result is True, "Update must be accepted (no data loss)"
        assert update_with_stripped_accuracy.get("latitude") == 48.123
        assert update_with_stripped_accuracy.get("longitude") == 11.456

        # Assert 2: Accuracy is set to fallback (not None, not 0.0)
        final_acc = update_with_stripped_accuracy.get("accuracy")
        assert final_acc == DEFAULT_ACCURACY_FALLBACK, (
            f"Accuracy should be fallback ({DEFAULT_ACCURACY_FALLBACK}m), got {final_acc}"
        )
        assert final_acc != 0.0, "Accuracy must NOT be 0.0"
        assert final_acc is not None, "Accuracy must NOT be None"

    def test_downgraded_report_loses_to_real_gps(self) -> None:
        """A downgraded report (2000m) must lose to real GPS (20m) in ranking.

        This ensures the "honest uncertainty" marking actually works.
        """
        # Downgraded report (was 0.0, now 2000.0)
        downgraded_report: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": DEFAULT_ACCURACY_FALLBACK,  # 2000m (was 0.0)
            "last_seen": 1700000100,
            "is_own_report": False,
        }

        # Real GPS report with actual measurement
        real_gps_report: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 20.0,  # Real GPS accuracy
            "last_seen": 1700000100,  # SAME timestamp
            "is_own_report": False,   # SAME status
        }

        best, _rest = _select_best_location([downgraded_report, real_gps_report])

        assert best is not None
        best_acc = best.get("accuracy")

        # Real GPS (20m) must win against downgraded (2000m)
        assert best_acc == 20.0, (
            f"Real GPS (20m) should beat downgraded report (2000m).\n"
            f"Got: {best_acc}m\n"
            f"Ranking: smaller accuracy = better precision = wins"
        )

    def test_end_to_end_zero_accuracy_handling(self) -> None:
        """End-to-end test: 0.0 accuracy through decoder and into cache.

        Full chain verification:
        1. Input: accuracy=0.0
        2. Decoder: removes key (< MIN_VALID_ACCURACY)
        3. Cache: sets fallback
        4. Output: valid location with 2000m accuracy
        """
        # Step 1: Simulate decoder behavior
        from custom_components.googlefindmy.ProtoDecoders.decoder import (
            _normalize_location_dict,
        )

        raw_location = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,  # Error code
            "last_seen": 1700000100,
        }

        # Decoder strips the accuracy key
        decoded = _normalize_location_dict(raw_location)
        assert "accuracy" not in decoded, "Decoder should remove 0.0 accuracy"
        assert decoded.get("latitude") == 48.123, "Coordinates preserved"

        # Step 2: Cache receives decoded data
        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update
        result = is_sig(mock_coord, "device-1", decoded)

        assert result is True, "Cache should accept the update"

        # Step 3: Verify final state
        assert decoded.get("accuracy") == DEFAULT_ACCURACY_FALLBACK, (
            f"Final accuracy should be {DEFAULT_ACCURACY_FALLBACK}m"
        )
        assert decoded.get("latitude") == 48.123, "Latitude preserved"
        assert decoded.get("longitude") == 11.456, "Longitude preserved"
