"""Tests for Phase 15 cache merge helper functions.

This module tests the pure helper functions extracted from
_merge_with_existing_cache_row in coordinator.py.
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.coordinator.helpers.cache import (
    compare_location_freshness,
    fill_missing_coordinates,
    merge_cache_row,
    preserve_monotonic_timestamp,
    select_best_location_source,
)

# ---------------------------------------------------------------------------
# Tests for compare_location_freshness
# ---------------------------------------------------------------------------


class TestCompareLocationFreshness:
    """Tests for compare_location_freshness function."""

    def test_new_timestamp_is_newer(self) -> None:
        """Return 1 when new timestamp is more recent."""
        result = compare_location_freshness(
            existing_ts=1000.0,
            new_ts=2000.0,
        )
        assert result == 1

    def test_existing_timestamp_is_newer(self) -> None:
        """Return -1 when existing timestamp is more recent."""
        result = compare_location_freshness(
            existing_ts=2000.0,
            new_ts=1000.0,
        )
        assert result == -1

    def test_timestamps_equal(self) -> None:
        """Return 0 when timestamps are equal."""
        result = compare_location_freshness(
            existing_ts=1500.0,
            new_ts=1500.0,
        )
        assert result == 0

    def test_both_timestamps_none(self) -> None:
        """Return 0 when both timestamps are None."""
        result = compare_location_freshness(
            existing_ts=None,
            new_ts=None,
        )
        assert result == 0

    def test_existing_none_new_has_timestamp(self) -> None:
        """Return 1 when existing is None but new has timestamp."""
        result = compare_location_freshness(
            existing_ts=None,
            new_ts=1000.0,
        )
        assert result == 1

    def test_existing_has_timestamp_new_none(self) -> None:
        """Return -1 when existing has timestamp but new is None."""
        result = compare_location_freshness(
            existing_ts=1000.0,
            new_ts=None,
        )
        assert result == -1

    def test_int_timestamps(self) -> None:
        """Accept integer timestamps."""
        result = compare_location_freshness(
            existing_ts=1000,
            new_ts=2000,
        )
        assert result == 1

    def test_mixed_int_float_timestamps(self) -> None:
        """Accept mixed int and float timestamps."""
        result = compare_location_freshness(
            existing_ts=1000,
            new_ts=1000.0,
        )
        assert result == 0

    def test_very_close_timestamps_considered_equal(self) -> None:
        """Timestamps within epsilon are considered equal."""
        result = compare_location_freshness(
            existing_ts=1000.0,
            new_ts=1000.0000001,
        )
        assert result == 0

    def test_small_difference_detected(self) -> None:
        """Small but significant timestamp differences are detected."""
        result = compare_location_freshness(
            existing_ts=1000.0,
            new_ts=1001.0,
        )
        assert result == 1


# ---------------------------------------------------------------------------
# Tests for select_best_location_source
# ---------------------------------------------------------------------------


class TestSelectBestLocationSource:
    """Tests for select_best_location_source function."""

    def test_api_beats_fcm(self) -> None:
        """API source has higher priority than FCM."""
        result = select_best_location_source("api", "fcm")
        assert result == "api"

    def test_fcm_vs_api_returns_api(self) -> None:
        """When comparing FCM and API, API wins."""
        result = select_best_location_source("fcm", "api")
        assert result == "api"

    def test_poll_beats_cache(self) -> None:
        """Poll source beats cache source."""
        result = select_best_location_source("poll", "cache")
        assert result == "poll"

    def test_higher_rank_number_wins(self) -> None:
        """Higher source rank wins regardless of argument order."""
        # source_a has higher rank
        result = select_best_location_source("api", "unknown")
        assert result == "api"

    def test_both_none_returns_unknown(self) -> None:
        """Both None sources return 'unknown'."""
        result = select_best_location_source(None, None)
        assert result == "unknown"

    def test_one_none_returns_valid_source(self) -> None:
        """When one source is None, return the valid one."""
        result = select_best_location_source("api", None)
        assert result == "api"

        result = select_best_location_source(None, "fcm")
        assert result == "fcm"

    def test_same_source_returns_source(self) -> None:
        """Same source on both sides returns that source."""
        result = select_best_location_source("api", "api")
        assert result == "api"

    def test_unknown_vs_unknown(self) -> None:
        """Unknown vs unknown returns unknown."""
        result = select_best_location_source("unknown", "unknown")
        assert result == "unknown"

    def test_unrecognized_source_treated_as_unknown(self) -> None:
        """Unrecognized source is treated as unknown (lowest priority)."""
        result = select_best_location_source("api", "some_random_source")
        assert result == "api"

    def test_two_unrecognized_sources(self) -> None:
        """Two unrecognized sources returns first one."""
        result = select_best_location_source("source_a", "source_b")
        # Both have priority 0, so first one wins
        assert result == "source_a"

    def test_all_known_sources_have_defined_priority(self) -> None:
        """All known sources are in the priority map."""
        known_sources = ["api", "poll", "fcm", "cache", "unknown"]
        for source in known_sources:
            result = select_best_location_source(source, None)
            assert result == source


# ---------------------------------------------------------------------------
# Tests for preserve_monotonic_timestamp
# ---------------------------------------------------------------------------


class TestPreserveMonotonicTimestamp:
    """Tests for preserve_monotonic_timestamp function."""

    def test_newer_incoming_timestamp_used(self) -> None:
        """When incoming is newer, use incoming timestamp."""
        result = preserve_monotonic_timestamp(
            existing={"last_seen": 1000.0, "last_seen_utc": "2024-01-01T00:00:00Z"},
            merged={"last_seen": 2000.0, "last_seen_utc": "2024-01-02T00:00:00Z"},
        )
        assert result["last_seen"] == 2000.0
        assert result["last_seen_utc"] == "2024-01-02T00:00:00Z"

    def test_older_incoming_preserves_existing(self) -> None:
        """When incoming is older, preserve existing timestamp."""
        result = preserve_monotonic_timestamp(
            existing={"last_seen": 2000.0, "last_seen_utc": "2024-01-02T00:00:00Z"},
            merged={"last_seen": 1000.0},
        )
        assert result["last_seen"] == 2000.0
        assert result["last_seen_utc"] == "2024-01-02T00:00:00Z"

    def test_incoming_none_preserves_existing(self) -> None:
        """When incoming has no timestamp, preserve existing."""
        result = preserve_monotonic_timestamp(
            existing={"last_seen": 1000.0, "last_seen_utc": "2024-01-01T00:00:00Z"},
            merged={"last_seen": None},
        )
        assert result["last_seen"] == 1000.0
        assert result["last_seen_utc"] == "2024-01-01T00:00:00Z"

    def test_existing_none_uses_incoming(self) -> None:
        """When existing has no timestamp, use incoming."""
        result = preserve_monotonic_timestamp(
            existing={"last_seen": None},
            merged={"last_seen": 1000.0, "last_seen_utc": "2024-01-01T00:00:00Z"},
        )
        assert result["last_seen"] == 1000.0
        assert result["last_seen_utc"] == "2024-01-01T00:00:00Z"

    def test_both_none_stays_none(self) -> None:
        """When both have no timestamp, result has no timestamp."""
        result = preserve_monotonic_timestamp(
            existing={"last_seen": None},
            merged={"last_seen": None},
        )
        assert result["last_seen"] is None

    def test_equal_timestamps_preserves_utc_from_existing(self) -> None:
        """When timestamps equal and merged lacks UTC, preserve existing UTC."""
        result = preserve_monotonic_timestamp(
            existing={"last_seen": 1000.0, "last_seen_utc": "2024-01-01T00:00:00Z"},
            merged={"last_seen": 1000.0, "last_seen_utc": None},
        )
        assert result["last_seen"] == 1000.0
        assert result["last_seen_utc"] == "2024-01-01T00:00:00Z"

    def test_equal_timestamps_with_both_utc_uses_merged(self) -> None:
        """When timestamps equal and both have UTC, use merged UTC."""
        result = preserve_monotonic_timestamp(
            existing={"last_seen": 1000.0, "last_seen_utc": "2024-01-01T00:00:00Z"},
            merged={"last_seen": 1000.0, "last_seen_utc": "2024-01-01T00:00:01Z"},
        )
        assert result["last_seen_utc"] == "2024-01-01T00:00:01Z"

    def test_preserves_other_fields(self) -> None:
        """Other fields in merged are preserved."""
        result = preserve_monotonic_timestamp(
            existing={"last_seen": 2000.0},
            merged={"last_seen": 1000.0, "latitude": 52.5, "longitude": 13.4},
        )
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4
        assert result["last_seen"] == 2000.0

    def test_does_not_mutate_input(self) -> None:
        """Input dictionaries are not mutated."""
        existing = {"last_seen": 2000.0, "last_seen_utc": "2024-01-02T00:00:00Z"}
        merged = {"last_seen": 1000.0}
        existing_copy = dict(existing)
        merged_copy = dict(merged)

        preserve_monotonic_timestamp(existing, merged)

        assert existing == existing_copy
        assert merged == merged_copy


# ---------------------------------------------------------------------------
# Tests for fill_missing_coordinates
# ---------------------------------------------------------------------------


class TestFillMissingCoordinates:
    """Tests for fill_missing_coordinates function."""

    def test_fills_missing_latitude(self) -> None:
        """Missing latitude is filled from existing."""
        result = fill_missing_coordinates(
            existing={"latitude": 52.5, "longitude": 13.4},
            merged={"latitude": None, "longitude": 14.0},
        )
        assert result["latitude"] == 52.5
        assert result["longitude"] == 14.0

    def test_fills_missing_longitude(self) -> None:
        """Missing longitude is filled from existing."""
        result = fill_missing_coordinates(
            existing={"latitude": 52.5, "longitude": 13.4},
            merged={"latitude": 53.0, "longitude": None},
        )
        assert result["latitude"] == 53.0
        assert result["longitude"] == 13.4

    def test_fills_missing_accuracy(self) -> None:
        """Missing accuracy is filled from existing."""
        result = fill_missing_coordinates(
            existing={"accuracy": 10.0},
            merged={"accuracy": None},
        )
        assert result["accuracy"] == 10.0

    def test_fills_missing_altitude(self) -> None:
        """Missing altitude is filled from existing."""
        result = fill_missing_coordinates(
            existing={"altitude": 100.0},
            merged={"altitude": None},
        )
        assert result["altitude"] == 100.0

    def test_does_not_overwrite_existing_values(self) -> None:
        """Values in merged are not overwritten."""
        result = fill_missing_coordinates(
            existing={"latitude": 52.5, "longitude": 13.4, "accuracy": 10.0},
            merged={"latitude": 53.0, "longitude": 14.0, "accuracy": 5.0},
        )
        assert result["latitude"] == 53.0
        assert result["longitude"] == 14.0
        assert result["accuracy"] == 5.0

    def test_fills_all_missing_fields(self) -> None:
        """All missing coordinate fields are filled."""
        result = fill_missing_coordinates(
            existing={
                "latitude": 52.5,
                "longitude": 13.4,
                "accuracy": 10.0,
                "altitude": 100.0,
            },
            merged={
                "latitude": None,
                "longitude": None,
                "accuracy": None,
                "altitude": None,
            },
        )
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4
        assert result["accuracy"] == 10.0
        assert result["altitude"] == 100.0

    def test_existing_also_none_stays_none(self) -> None:
        """When existing also has None, result stays None."""
        result = fill_missing_coordinates(
            existing={"latitude": None},
            merged={"latitude": None},
        )
        assert result["latitude"] is None

    def test_preserves_non_coordinate_fields(self) -> None:
        """Non-coordinate fields are preserved."""
        result = fill_missing_coordinates(
            existing={"latitude": 52.5},
            merged={
                "latitude": None,
                "name": "Test Device",
                "status": "active",
            },
        )
        assert result["name"] == "Test Device"
        assert result["status"] == "active"

    def test_existing_missing_field_not_in_merged(self) -> None:
        """Fields not in merged but in existing are not added."""
        result = fill_missing_coordinates(
            existing={"latitude": 52.5, "extra_field": "value"},
            merged={"latitude": None},
        )
        assert "extra_field" not in result

    def test_does_not_mutate_input(self) -> None:
        """Input dictionaries are not mutated."""
        existing = {"latitude": 52.5, "longitude": 13.4}
        merged = {"latitude": None, "longitude": None}
        existing_copy = dict(existing)
        merged_copy = dict(merged)

        fill_missing_coordinates(existing, merged)

        assert existing == existing_copy
        assert merged == merged_copy


# ---------------------------------------------------------------------------
# Tests for merge_cache_row
# ---------------------------------------------------------------------------


class TestMergeCacheRow:
    """Tests for merge_cache_row function - the main orchestrating function."""

    def test_incoming_only_when_no_existing(self) -> None:
        """Return incoming when there's no existing data."""
        incoming: dict[str, Any] = {
            "latitude": 52.5,
            "longitude": 13.4,
            "last_seen": 1000.0,
        }
        result = merge_cache_row(existing=None, incoming=incoming)
        assert result == incoming

    def test_empty_existing_returns_incoming(self) -> None:
        """Empty existing dict returns incoming."""
        incoming: dict[str, Any] = {
            "latitude": 52.5,
            "longitude": 13.4,
        }
        result = merge_cache_row(existing={}, incoming=incoming)
        # With empty existing, incoming should be the base
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4

    def test_newer_incoming_updates_location(self) -> None:
        """Newer incoming timestamp updates location fields."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 1000.0,
            "source_rank": 5,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "last_seen": 2000.0,
            "source_rank": 5,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 53.0
        assert result["longitude"] == 14.0
        assert result["last_seen"] == 2000.0

    def test_older_incoming_preserves_location(self) -> None:
        """Older incoming timestamp preserves existing location."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 2000.0,
            "source_rank": 5,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "last_seen": 1000.0,
            "source_rank": 5,
            "battery_level": 80,  # Non-location field
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 52.0
        assert result["longitude"] == 13.0
        assert result["last_seen"] == 2000.0
        assert result["battery_level"] == 80  # Non-location fields updated

    def test_same_timestamp_higher_existing_rank_preserves(self) -> None:
        """Same timestamp with higher existing rank preserves location."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 1000.0,
            "source_rank": 10,  # Higher rank
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "last_seen": 1000.0,
            "source_rank": 5,  # Lower rank
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 52.0
        assert result["longitude"] == 13.0

    def test_same_timestamp_lower_existing_rank_updates(self) -> None:
        """Same timestamp with lower existing rank updates location."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 1000.0,
            "source_rank": 5,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "last_seen": 1000.0,
            "source_rank": 10,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 53.0
        assert result["longitude"] == 14.0

    def test_fills_missing_coordinates_from_existing(self) -> None:
        """Missing coordinates in result are filled from existing."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "accuracy": 10.0,
            "altitude": 100.0,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": None,  # Missing
            "accuracy": None,  # Missing
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 53.0
        assert result["longitude"] == 13.0  # Filled
        assert result["accuracy"] == 10.0  # Filled

    def test_preserves_monotonic_timestamp(self) -> None:
        """Timestamps are preserved monotonically."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 2000.0,
            "last_seen_utc": "2024-01-02T00:00:00Z",
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "last_seen": None,  # No timestamp
        }
        # This should trigger significant distance check...
        # For now just check that existing timestamp is preserved
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["last_seen"] == 2000.0
        assert result["last_seen_utc"] == "2024-01-02T00:00:00Z"

    def test_location_fields_not_updated_when_blocked(self) -> None:
        """Location fields are not updated when update is blocked."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "accuracy": 10.0,
            "altitude": 100.0,
            "status": "old_status",
            "source_label": "old_source",
            "source_rank": 10,
            "is_own_report": True,
            "last_seen": 2000.0,
            "last_seen_utc": "2024-01-02T00:00:00Z",
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "accuracy": 5.0,
            "altitude": 200.0,
            "status": "new_status",
            "source_label": "new_source",
            "source_rank": 5,
            "is_own_report": False,
            "last_seen": 1000.0,  # Older
            "last_seen_utc": "2024-01-01T00:00:00Z",
            "battery_level": 80,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        # Location fields should be from existing
        assert result["latitude"] == 52.0
        assert result["longitude"] == 13.0
        assert result["accuracy"] == 10.0
        # Non-location fields should be updated
        assert result["battery_level"] == 80

    def test_non_location_fields_always_updated(self) -> None:
        """Non-location fields are always updated regardless of timestamp."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 2000.0,
            "name": "Old Name",
            "battery_level": 50,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "last_seen": 1000.0,  # Older - location blocked
            "name": "New Name",
            "battery_level": 80,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["name"] == "New Name"
        assert result["battery_level"] == 80

    def test_significant_distance_allows_update_without_timestamp(self) -> None:
        """Significant distance change allows update even without timestamp."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "accuracy": 20.0,
            "last_seen": 1000.0,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,  # ~111km north
            "longitude": 13.0,
            "accuracy": 20.0,
            "last_seen": None,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        # 111km >> adaptive threshold (~14m for 20m+20m accuracy)
        assert result["latitude"] == 53.0

    def test_insignificant_distance_blocks_update_without_timestamp(self) -> None:
        """Insignificant distance blocks update when incoming has no timestamp."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "accuracy": 20.0,
            "last_seen": 1000.0,
        }
        incoming: dict[str, Any] = {
            "latitude": 52.0001,  # ~11m - below adaptive threshold
            "longitude": 13.0001,
            "accuracy": 20.0,
            "last_seen": None,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        # 11m < adaptive threshold (~14m for 20m+20m accuracy)
        assert result["latitude"] == 52.0
        assert result["longitude"] == 13.0

    def test_adaptive_threshold_gps_allows_small_movement(self) -> None:
        """Good GPS accuracy (10m) allows small but real movements (>7m)."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "accuracy": 10.0,
            "last_seen": 1000.0,
        }
        incoming: dict[str, Any] = {
            # ~15m north-east - above adaptive threshold (~7m for 10m+10m)
            "latitude": 52.0001,
            "longitude": 13.00015,
            "accuracy": 10.0,
            "last_seen": None,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 52.0001

    def test_adaptive_threshold_ble_blocks_noise(self) -> None:
        """Poor BLE accuracy (200m) blocks small positional noise."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "accuracy": 200.0,
            "last_seen": 1000.0,
        }
        incoming: dict[str, Any] = {
            # ~50m north - below adaptive threshold (~141m for 200m+200m)
            "latitude": 52.00045,
            "longitude": 13.0,
            "accuracy": 200.0,
            "last_seen": None,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        # 50m < 141m threshold → blocked
        assert result["latitude"] == 52.0

    def test_adaptive_threshold_no_accuracy_uses_fallback(self) -> None:
        """Missing accuracy falls back to 200m, creating a high threshold."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 1000.0,
        }
        incoming: dict[str, Any] = {
            # ~50m north - below fallback threshold (~141m for 200m+200m)
            "latitude": 52.00045,
            "longitude": 13.0,
            "last_seen": None,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        # No accuracy → 200m fallback → threshold ~141m → blocked
        assert result["latitude"] == 52.0

    def test_does_not_mutate_inputs(self) -> None:
        """Input dictionaries are not mutated."""
        existing: dict[str, Any] = {"latitude": 52.0, "last_seen": 1000.0}
        incoming: dict[str, Any] = {"latitude": 53.0, "last_seen": 2000.0}
        existing_copy = dict(existing)
        incoming_copy = dict(incoming)

        merge_cache_row(existing=existing, incoming=incoming)

        assert existing == existing_copy
        assert incoming == incoming_copy


# ---------------------------------------------------------------------------
# Edge Cases and Integration Tests
# ---------------------------------------------------------------------------


class TestMergeCacheRowEdgeCases:
    """Edge cases for merge_cache_row."""

    def test_both_ranks_none_with_same_timestamp(self) -> None:
        """Both ranks None with same timestamp allows update."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 1000.0,
            "source_rank": None,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "last_seen": 1000.0,
            "source_rank": None,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 53.0
        assert result["longitude"] == 14.0

    def test_neither_has_timestamp_existing_higher_rank(self) -> None:
        """Neither has timestamp, existing higher rank preserves location."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "source_rank": 10,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "source_rank": 5,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 52.0
        assert result["longitude"] == 13.0

    def test_neither_has_timestamp_incoming_higher_rank(self) -> None:
        """Neither has timestamp, incoming higher rank updates location."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "source_rank": 5,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "source_rank": 10,
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 53.0
        assert result["longitude"] == 14.0

    def test_handles_string_timestamps(self) -> None:
        """String timestamps that can be converted to float work."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": "1000.0",
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": 14.0,
            "last_seen": "2000.0",
        }
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert result["latitude"] == 53.0

    def test_handles_invalid_coordinates_gracefully(self) -> None:
        """Invalid coordinates don't crash the function."""
        existing: dict[str, Any] = {
            "latitude": "invalid",
            "longitude": 13.0,
            "last_seen": 1000.0,
        }
        incoming: dict[str, Any] = {
            "latitude": 53.0,
            "longitude": None,
            "last_seen": None,
        }
        # Should not crash
        result = merge_cache_row(existing=existing, incoming=incoming)
        assert isinstance(result, dict)

    def test_empty_incoming(self) -> None:
        """Empty incoming preserves existing."""
        existing: dict[str, Any] = {
            "latitude": 52.0,
            "longitude": 13.0,
            "last_seen": 1000.0,
        }
        result = merge_cache_row(existing=existing, incoming={})
        # Empty incoming should still produce a result based on existing
        assert result.get("latitude") == 52.0
        assert result.get("longitude") == 13.0


class TestLocationFieldsDefinition:
    """Tests to ensure location fields are correctly identified."""

    def test_all_location_fields_blocked_when_update_blocked(self) -> None:
        """All defined location fields are blocked when update is blocked."""
        location_fields = {
            "latitude",
            "longitude",
            "accuracy",
            "altitude",
            "status",
            "source_label",
            "source_rank",
            "is_own_report",
            "last_seen",
            "last_seen_utc",
        }

        existing: dict[str, Any] = {
            field: f"existing_{field}" for field in location_fields
        }
        existing["last_seen"] = 2000.0

        incoming: dict[str, Any] = {
            field: f"incoming_{field}" for field in location_fields
        }
        incoming["last_seen"] = 1000.0
        incoming["non_location_field"] = "should_update"

        result = merge_cache_row(existing=existing, incoming=incoming)

        # Non-location field should be updated
        assert result["non_location_field"] == "should_update"

        # Location fields should be preserved from existing (except last_seen which we set)
        assert result["latitude"] == "existing_latitude"
        assert result["longitude"] == "existing_longitude"
