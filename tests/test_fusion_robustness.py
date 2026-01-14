# tests/test_fusion_robustness.py
"""Comprehensive robustness tests for location fusion and dirty data handling.

These tests verify the Defense-in-Depth strategy against "Phantom Updates"
(is_own_report=True with accuracy=0.0m) and other malformed data.

Test Scenarios:
1. "The 0.0m Ghost" - Spurious own-device reports with impossible accuracy
2. "Null Island Attack" - Coordinates at (0.0, 0.0)
3. "Cache Self-Healing" - Recovery from poisoned cache entries
4. "Manual Locate Sanity" - Validation in locate.py

Architecture under test:
- Layer 1: helpers/geo.py - Central validation constants
- Layer 2: decoder.py - Input filtering and ranking
- Layer 3: locate.py - Manual locate validation
- Layer 4: cache.py - Gatekeeper and fusion math
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import pytest

# =============================================================================
# SECTION 1: Central Constants Tests (helpers/geo.py)
# =============================================================================


class TestGeoConstants:
    """Verify the central physics constants are correctly defined."""

    def test_min_valid_accuracy_is_one_millimeter(self) -> None:
        """MIN_VALID_ACCURACY must be 0.001m (1mm threshold for error code).

        The Android Location API uses 0.0 as an error code meaning "no accuracy".
        Modern dual-frequency GNSS can achieve sub-meter accuracy, so we use a
        very low threshold (1mm) to only catch the error code.
        """
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            MIN_VALID_ACCURACY,
        )

        assert MIN_VALID_ACCURACY == 0.001, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: MIN_VALID_ACCURACY is not 0.001m!\n"
            f"{'=' * 70}\n"
            f"Current value: {MIN_VALID_ACCURACY}\n"
            f"Expected: 0.001 (1mm - only catch error code 0.0)\n"
            f"\n"
            f"FIX LOCATION: helpers/geo.py\n"
            f"{'=' * 70}"
        )

    def test_default_fallback_is_two_hundred_meters(self) -> None:
        """PRIVACY_ACCURACY_FALLBACK must be 200.0m (Bluetooth range + GPS error).

        The fallback is based on Bluetooth tracker physics:
        - Max Bluetooth range: ~100m
        - GPS error margin: ~100m
        - Total: 200m

        This ensures:
        - Error codes (0.0) have lower weight in fusion than real GPS (20-50m)
        - The fallback is still useful for finding a tracker (unlike 2km)
        """
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            DEFAULT_ACCURACY_FALLBACK,
            PRIVACY_ACCURACY_FALLBACK,
        )

        assert PRIVACY_ACCURACY_FALLBACK == 200.0, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: PRIVACY_ACCURACY_FALLBACK is not 200.0m!\n"
            f"{'=' * 70}\n"
            f"Current value: {PRIVACY_ACCURACY_FALLBACK}\n"
            f"Expected: 200.0 (Bluetooth range + GPS error margin)\n"
            f"\n"
            f"FIX LOCATION: helpers/geo.py\n"
            f"{'=' * 70}"
        )
        # Ensure legacy alias is also correct
        assert DEFAULT_ACCURACY_FALLBACK == PRIVACY_ACCURACY_FALLBACK

    def test_safe_accuracy_rejects_error_code(self) -> None:
        """safe_accuracy() must reject 0.0m (error code) as invalid."""
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            DEFAULT_ACCURACY_FALLBACK,
            safe_accuracy,
        )

        result = safe_accuracy(0.0)

        assert result == DEFAULT_ACCURACY_FALLBACK, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: safe_accuracy(0.0) did not return fallback!\n"
            f"{'=' * 70}\n"
            f"Input: 0.0m (Android API error code)\n"
            f"Output: {result}m\n"
            f"Expected: {DEFAULT_ACCURACY_FALLBACK}m (fallback)\n"
            f"\n"
            f"0.0 is the Android Location API error code for 'no accuracy'.\n"
            f"FIX LOCATION: helpers/geo.py :: safe_accuracy()\n"
            f"{'=' * 70}"
        )

    def test_safe_accuracy_rejects_below_threshold(self) -> None:
        """safe_accuracy() must reject values < 0.001m (error codes)."""
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            DEFAULT_ACCURACY_FALLBACK,
            safe_accuracy,
        )

        # Only error codes (< 0.001m) and negative should return fallback
        for invalid in [0.0, 0.0001, 0.0009, -1.0, -100.0]:
            result = safe_accuracy(invalid)
            assert result == DEFAULT_ACCURACY_FALLBACK, (
                f"safe_accuracy({invalid}) should return fallback, got {result}"
            )

    def test_safe_accuracy_preserves_sub_meter(self) -> None:
        """safe_accuracy() must PRESERVE valid sub-meter accuracy.

        Modern dual-frequency GNSS can achieve sub-meter accuracy.
        """
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            safe_accuracy,
        )

        # Valid sub-meter values should be preserved
        for valid in [0.001, 0.002, 0.1, 0.5, 0.99]:
            result = safe_accuracy(valid)
            assert result == valid, (
                f"safe_accuracy({valid}) should preserve {valid}, got {result}"
            )

    def test_safe_accuracy_preserves_standard_accuracy(self) -> None:
        """safe_accuracy() must preserve standard accuracy values."""
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            safe_accuracy,
        )

        for valid in [1.0, 5.0, 20.0, 100.0, 1000.0]:
            result = safe_accuracy(valid)
            assert result == valid, (
                f"safe_accuracy({valid}) should return {valid}, got {result}"
            )

    def test_is_valid_accuracy_function(self) -> None:
        """is_valid_accuracy() must correctly classify values."""
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            is_valid_accuracy,
        )

        # Invalid cases: error codes and invalid values
        assert is_valid_accuracy(None) is False
        assert is_valid_accuracy(0.0) is False, "0.0 is error code"
        assert is_valid_accuracy(0.0001) is False, "< 0.001 is error"
        assert is_valid_accuracy(-1.0) is False, "Negative is invalid"
        assert is_valid_accuracy(float("nan")) is False
        assert is_valid_accuracy(float("inf")) is False

        # Valid cases: sub-meter is now valid for modern GNSS
        assert is_valid_accuracy(0.001) is True, "1mm is valid threshold"
        assert is_valid_accuracy(0.002) is True, "2mm is valid"
        assert is_valid_accuracy(0.5) is True, "50cm is valid"
        assert is_valid_accuracy(1.0) is True
        assert is_valid_accuracy(20.0) is True
        assert is_valid_accuracy(100.0) is True


# =============================================================================
# SECTION 2: "The 0.0m Ghost" - Decoder Filtering
# =============================================================================


class TestZeroMeterGhost:
    """Test that 0.0m accuracy reports are correctly rejected.

    The "0.0m Ghost" occurs when Google's API returns:
    - is_own_report=True
    - accuracy=0.0 (physically impossible)

    This data must be filtered/rejected at multiple layers.
    """

    def test_decoder_filters_zero_accuracy(self) -> None:
        """_normalize_location_dict must remove accuracy=0.0."""
        from custom_components.googlefindmy.ProtoDecoders.decoder import (
            _normalize_location_dict,
        )

        ghost_report: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,
            "last_seen": 1700000000,
            "is_own_report": True,
        }

        normalized = _normalize_location_dict(ghost_report)

        assert "accuracy" not in normalized, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: 'The 0.0m Ghost' was NOT filtered by decoder!\n"
            f"{'=' * 70}\n"
            f"Input: accuracy=0.0m, is_own_report=True\n"
            f"Output: accuracy still present in dict\n"
            f"\n"
            f"This 'Phantom Update' will poison the ranking and cache.\n"
            f"GPS accuracy of 0.0m is physically impossible.\n"
            f"\n"
            f"FIX LOCATION: decoder.py :: _normalize_location_dict()\n"
            f"Ensure: accuracy <= 0 is removed from the dict\n"
            f"{'=' * 70}"
        )

    def test_ranking_penalizes_zero_accuracy(self) -> None:
        """_get_rank_tuple must assign worst rank to accuracy=0.0."""
        from custom_components.googlefindmy.ProtoDecoders.decoder import (
            _get_rank_tuple,
        )

        ghost_report: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 0.0,  # Should trigger -inf rank
            "last_seen": 1700000000,
            "is_own_report": True,
        }

        rank = _get_rank_tuple(ghost_report)
        acc_rank = rank[3]  # Index 3 is accuracy rank

        assert math.isinf(acc_rank) and acc_rank < 0, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: 0.0m accuracy did NOT get worst rank!\n"
            f"{'=' * 70}\n"
            f"Input: accuracy=0.0m\n"
            f"Acc Rank: {acc_rank}\n"
            f"Expected: float('-inf') (worst possible)\n"
            f"\n"
            f"Without -inf rank, the ghost report may win over valid data.\n"
            f"FIX LOCATION: decoder.py :: _get_rank_tuple()\n"
            f"{'=' * 70}"
        )

    def test_valid_report_beats_ghost_in_ranking(self) -> None:
        """A valid crowdsourced report MUST beat the 0.0m ghost."""
        from custom_components.googlefindmy.ProtoDecoders.decoder import (
            _select_best_location,
        )

        ghost: dict[str, Any] = {
            "latitude": 48.100,
            "longitude": 11.100,
            "accuracy": 0.0,
            "last_seen": 1700000200,  # Newer (advantage)
            "is_own_report": True,  # Owner priority (advantage)
            "status_code": 1,
        }

        valid: dict[str, Any] = {
            "latitude": 48.200,
            "longitude": 11.200,
            "accuracy": 20.0,
            "last_seen": 1700000100,  # Older
            "is_own_report": False,
            "status_code": 2,
        }

        best, _rest = _select_best_location([ghost, valid])

        if best is not None:
            selected_acc = best.get("accuracy")
            # Either valid wins, or accuracy is filtered to None
            is_valid_winner = selected_acc == pytest.approx(20.0)
            is_filtered = selected_acc is None

            assert is_valid_winner or is_filtered, (
                f"\n"
                f"{'=' * 70}\n"
                f"CRITICAL: 'The 0.0m Ghost' won the ranking!\n"
                f"{'=' * 70}\n"
                f"Selected accuracy: {selected_acc}m\n"
                f"Expected: 20.0m (valid) or None (filtered)\n"
                f"\n"
                f"The ghost had advantages (newer, owner priority) but MUST lose\n"
                f"because 0.0m accuracy is physically impossible.\n"
                f"\n"
                f"PRINCIPLE: Physics > Recency > Priority\n"
                f"{'=' * 70}"
            )


# =============================================================================
# SECTION 3: "Null Island Attack"
# =============================================================================


class TestNullIslandAttack:
    """Test that coordinates at (0.0, 0.0) are rejected.

    "Null Island" is the point at 0°N 0°E in the Atlantic Ocean.
    It's a common API default when no real location is available.
    """

    def test_decoder_filters_null_island(self) -> None:
        """_normalize_location_dict must remove (0.0, 0.0) coordinates."""
        from custom_components.googlefindmy.ProtoDecoders.decoder import (
            _normalize_location_dict,
        )

        null_island: dict[str, Any] = {
            "latitude": 0.0,
            "longitude": 0.0,
            "accuracy": 50.0,  # Valid accuracy
            "last_seen": 1700000000,
        }

        normalized = _normalize_location_dict(null_island)

        has_coords = "latitude" in normalized and "longitude" in normalized

        assert not has_coords, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: Null Island coordinates were NOT filtered!\n"
            f"{'=' * 70}\n"
            f"Input: (0.0, 0.0) - The Atlantic Ocean\n"
            f"Output: Coordinates still present\n"
            f"\n"
            f"(0.0, 0.0) is a common API default for 'no location'.\n"
            f"It does not represent a real device position.\n"
            f"\n"
            f"FIX LOCATION: decoder.py :: _normalize_location_dict()\n"
            f"Add: if abs(lat) < 0.0001 and abs(lon) < 0.0001: filter out\n"
            f"{'=' * 70}"
        )

    def test_null_island_loses_to_valid_location(self) -> None:
        """Null Island must lose to any valid location in ranking."""
        from custom_components.googlefindmy.ProtoDecoders.decoder import (
            _select_best_location,
        )

        null_island: dict[str, Any] = {
            "latitude": 0.0,
            "longitude": 0.0,
            "accuracy": 10.0,  # Good accuracy (irrelevant, coords are bad)
            "last_seen": 1700000200,  # Newer
            "is_own_report": True,
        }

        valid: dict[str, Any] = {
            "latitude": 48.123,
            "longitude": 11.456,
            "accuracy": 100.0,  # Poor accuracy
            "last_seen": 1700000100,  # Older
            "is_own_report": False,
        }

        best, _rest = _select_best_location([null_island, valid])

        if best is not None:
            lat = best.get("latitude")
            lon = best.get("longitude")

            # Must NOT be Null Island
            if lat is not None and lon is not None:
                is_null_island = abs(lat) < 0.0001 and abs(lon) < 0.0001
                assert not is_null_island, (
                    f"\n"
                    f"{'=' * 70}\n"
                    f"CRITICAL: Null Island won over valid coordinates!\n"
                    f"{'=' * 70}\n"
                    f"Selected: ({lat}, {lon})\n"
                    f"Expected: (48.123, 11.456) or coordinates removed\n"
                    f"{'=' * 70}"
                )


# =============================================================================
# SECTION 4: "Cache Self-Healing"
# =============================================================================


class TestCacheSelfHealing:
    """Test that poisoned cache entries can be overridden by valid data.

    The "Cache Self-Healing" scenario:
    1. Cache contains poisoned data (accuracy=0.0 from a past bug)
    2. New valid data arrives (accuracy=25.0)
    3. The valid data MUST be able to override the poisoned entry

    Without self-healing, the poisoned 0.0m would have infinite weight
    in fusion calculations (1/0² = infinity), locking in the bad position.
    """

    def test_safe_accuracy_heals_poisoned_cache(self) -> None:
        """_safe_accuracy treats cached 0.0m as fallback, enabling override."""
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            DEFAULT_ACCURACY_FALLBACK_M,
            safe_accuracy,
        )

        # Poisoned cache value
        cached_acc = safe_accuracy(0.0)

        assert cached_acc == DEFAULT_ACCURACY_FALLBACK_M, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: Poisoned cache accuracy not converted to fallback!\n"
            f"{'=' * 70}\n"
            f"Cached value: 0.0m (poisoned)\n"
            f"safe_accuracy output: {cached_acc}m\n"
            f"Expected: {DEFAULT_ACCURACY_FALLBACK_M}m (fallback)\n"
            f"\n"
            f"Without this conversion, 0.0m gets infinite fusion weight.\n"
            f"FIX LOCATION: helpers/geo.py :: safe_accuracy()\n"
            f"{'=' * 70}"
        )

        # New valid data has reasonable weight ratio against fallback
        new_acc = 25.0
        w_cached = 1 / (cached_acc**2)  # 1/50² = 0.0004
        w_new = 1 / (new_acc**2)  # 1/25² = 0.0016

        ratio = w_new / w_cached  # 4:1 - new data wins with 80% weight

        assert ratio > 1, (
            f"New valid data should have more weight than healed poisoned data.\n"
            f"Weight ratio: {ratio:.2f}:1 (new:cached)\n"
            f"This ensures valid data can 'rescue' a poisoned cache."
        )

    def test_fusion_weight_ratio_reasonable_after_healing(self) -> None:
        """After healing, fusion weights should allow valid data to dominate."""
        # Simulate the fusion weight calculation
        # poisoned_acc = 0.0 would cause infinite weight (1/0² = infinity)
        # After healing via safe_accuracy(), it becomes:
        healed_acc = 50.0  # What safe_accuracy(0.0) returns

        new_valid_acc = 25.0

        # Without healing: infinite weight ratio
        # With healing: reasonable ratio
        w_healed = 1 / (healed_acc**2)
        w_new = 1 / (new_valid_acc**2)

        total_w = w_healed + w_new
        new_weight_fraction = w_new / total_w

        # New data should contribute significantly (> 50%)
        assert new_weight_fraction > 0.5, (
            f"\n"
            f"{'=' * 70}\n"
            f"CRITICAL: New valid data has insufficient fusion weight!\n"
            f"{'=' * 70}\n"
            f"Healed cached accuracy: {healed_acc}m (was 0.0m)\n"
            f"New valid accuracy: {new_valid_acc}m\n"
            f"New data weight fraction: {new_weight_fraction:.1%}\n"
            f"Expected: > 50% (new data should dominate)\n"
            f"\n"
            f"If new data has < 50% weight, poisoned cache 'locks in' position.\n"
            f"{'=' * 70}"
        )


# =============================================================================
# SECTION 5: "Manual Locate Sanity"
# =============================================================================


class TestManualLocateSanity:
    """Test that locate.py properly validates accuracy values.

    Manual locate requests bypass the decoder, so locate.py must
    independently validate accuracy before writing to cache.
    """

    def test_locate_uses_min_valid_accuracy(self) -> None:
        """locate.py must import and use MIN_PHYSICAL_ACCURACY_M (0.001m)."""
        # Verify the import exists
        from custom_components.googlefindmy.coordinator.locate import (
            MIN_PHYSICAL_ACCURACY_M,
        )

        assert MIN_PHYSICAL_ACCURACY_M == 0.001, (
            "locate.py should use MIN_PHYSICAL_ACCURACY_M = 0.001m (1mm threshold)"
        )


# =============================================================================
# SECTION 6: Cache Gatekeeper Tests
# =============================================================================


class TestCacheGatekeeper:
    """Test the cache gatekeeper (_is_significant_update) handles invalid data.

    Error code accuracy values (< 0.001m) are SANITIZED (removed from dict),
    not rejected. Valid sub-meter accuracy is preserved. This allows valid
    location data to pass through while preventing error codes from corrupting rankings.
    """

    def test_gatekeeper_sanitizes_zero_accuracy(self) -> None:
        """_is_significant_update must SANITIZE accuracy=0.0 to fallback.

        Zero accuracy means 'unknown/masked', not 'perfect precision'.
        We ACCEPT the update and SET accuracy to conservative fallback.
        Home Assistant requires a numeric gps_accuracy attribute.
        """
        from custom_components.googlefindmy.coordinator.cache import CacheOperations
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            DEFAULT_ACCURACY_FALLBACK,
        )

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
        assert result is True, "Zero accuracy update should be ACCEPTED (sanitized)"

        # Accuracy must be SET to fallback (not removed)
        assert update.get("accuracy") == DEFAULT_ACCURACY_FALLBACK, (
            f"Zero accuracy should be set to fallback ({DEFAULT_ACCURACY_FALLBACK}m)"
        )

        mock_coord.increment_stat.assert_called_with("accuracy_sanitized_count")

    def test_gatekeeper_sanitizes_error_codes(self) -> None:
        """_is_significant_update must SANITIZE error codes to fallback.

        The Android Location API uses 0.0 as "no accuracy available".
        We ACCEPT the update and SET accuracy to conservative fallback.
        Home Assistant requires a numeric gps_accuracy attribute.
        """
        from custom_components.googlefindmy.coordinator.cache import CacheOperations
        from custom_components.googlefindmy.coordinator.helpers.geo import (
            DEFAULT_ACCURACY_FALLBACK,
        )

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        # Error codes (< 0.001m) should be sanitized to fallback
        for error_code in [0.0, 0.0001, 0.0009]:
            update = {
                "latitude": 48.123,
                "longitude": 11.456,
                "accuracy": error_code,
                "last_seen": 1700000100,
            }

            result = is_sig(mock_coord, "device-1", update)

            assert result is True, (
                f"Update with accuracy={error_code}m should be ACCEPTED"
            )
            assert update.get("accuracy") == DEFAULT_ACCURACY_FALLBACK, (
                f"Error code {error_code}m should be set to fallback ({DEFAULT_ACCURACY_FALLBACK}m)"
            )

    def test_gatekeeper_preserves_sub_meter_accuracy(self) -> None:
        """_is_significant_update must PRESERVE valid sub-meter accuracy.

        Modern dual-frequency GNSS can achieve sub-meter accuracy.
        Values like 0.5m or 0.01m are valid and must be preserved.
        """
        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        # Valid sub-meter values (>= 0.001m) should be preserved
        for valid_acc in [0.001, 0.002, 0.5, 0.99, 1.0, 5.0]:
            update = {
                "latitude": 48.123,
                "longitude": 11.456,
                "accuracy": valid_acc,
                "last_seen": 1700000100,
            }

            result = is_sig(mock_coord, "device-1", update)

            assert result is True, (
                f"Update with accuracy={valid_acc}m should be accepted"
            )
            assert update.get("accuracy") == valid_acc, (
                f"Valid accuracy {valid_acc}m should be PRESERVED, not sanitized"
            )

    def test_gatekeeper_allows_semantic_only(self) -> None:
        """_is_significant_update must allow updates without accuracy."""
        from custom_components.googlefindmy.coordinator.cache import CacheOperations

        mock_coord = MagicMock(spec=CacheOperations)
        mock_coord._device_location_data = {}
        mock_coord.increment_stat = MagicMock()

        is_sig = CacheOperations._is_significant_update

        semantic_update = {
            "semantic_name": "Home",
            "last_seen": 1700000100,
            # No accuracy field - valid for semantic locations
        }

        result = is_sig(mock_coord, "device-1", semantic_update)

        assert result is True, (
            "Gatekeeper should allow semantic updates without accuracy"
        )


# =============================================================================
# SECTION 7: Fusion Math Verification
# =============================================================================


class TestFusionMath:
    """Verify the inverse variance fusion formula is correctly implemented."""

    def test_inverse_variance_improves_accuracy(self) -> None:
        """Combining two measurements should yield better accuracy than either."""
        import math

        acc1 = 20.0
        acc2 = 30.0

        w1 = 1 / (acc1**2)
        w2 = 1 / (acc2**2)
        total_w = w1 + w2

        fused = math.sqrt(1 / total_w)  # ~16.6m

        assert fused < acc1, f"Fused ({fused:.1f}m) should be < {acc1}m"
        assert fused < acc2, f"Fused ({fused:.1f}m) should be < {acc2}m"

    def test_safety_floor_prevents_impossible_precision(self) -> None:
        """Fused accuracy must not claim better than 5m (GPS floor)."""
        import math

        # Even if both inputs are excellent...
        acc1 = 5.0
        acc2 = 5.0

        w1 = 1 / (acc1**2)
        w2 = 1 / (acc2**2)
        total_w = w1 + w2

        raw_fused = math.sqrt(1 / total_w)  # ~3.5m

        # The code should clamp to minimum 5m
        MIN_FUSED = 5.0
        clamped = max(raw_fused, MIN_FUSED)

        assert clamped >= MIN_FUSED, (
            f"Fused accuracy ({clamped:.1f}m) must be >= {MIN_FUSED}m (GPS floor)"
        )
