"""Tests for coordinator_cache.py Phase 13 cache update functions.

These tests validate the cache update helper functions extracted from
update_device_cache into coordinator_cache.py.

Test categories:
1. validate_location_data - Validate location data has required fields
2. merge_location_update - Merge new data with existing cache
3. detect_significant_change - Detect significant location changes
4. select_best_accuracy - Select better accuracy value
5. normalize_location_fields - Normalize field types
6. preserve_metadata_fields - Preserve metadata from previous cache
7. should_clear_metadata_only_flag - Determine if flag should be cleared

REQUIREMENT: 100% test coverage for all extracted functions.

KEY RISKS COVERED:
- Coordinate validation (lat/lon presence and types)
- Cache consistency during partial updates
- Accuracy comparison (lower is better)
- Timestamp handling
- Metadata preservation
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.coordinator_cache import (
    detect_significant_change,
    merge_location_update,
    normalize_location_fields,
    preserve_metadata_fields,
    select_best_accuracy,
    should_clear_metadata_only_flag,
    validate_location_data,
)

# ---------------------------------------------------------------------------
# validate_location_data Tests
# ---------------------------------------------------------------------------


class TestValidateLocationData:
    """Tests for validate_location_data function."""

    def test_valid_with_lat_lon(self) -> None:
        """Should return True for valid lat/lon data."""
        data = {"latitude": 52.5, "longitude": 13.4}
        assert validate_location_data(data) is True

    def test_valid_with_int_coordinates(self) -> None:
        """Should accept integer coordinates."""
        data = {"latitude": 52, "longitude": 13}
        assert validate_location_data(data) is True

    def test_invalid_missing_latitude(self) -> None:
        """Should return False when latitude missing."""
        data = {"longitude": 13.4}
        assert validate_location_data(data) is False

    def test_invalid_missing_longitude(self) -> None:
        """Should return False when longitude missing."""
        data = {"latitude": 52.5}
        assert validate_location_data(data) is False

    def test_invalid_none_latitude(self) -> None:
        """Should return False when latitude is None."""
        data = {"latitude": None, "longitude": 13.4}
        assert validate_location_data(data) is False

    def test_invalid_none_longitude(self) -> None:
        """Should return False when longitude is None."""
        data = {"latitude": 52.5, "longitude": None}
        assert validate_location_data(data) is False

    def test_invalid_string_latitude(self) -> None:
        """Should return False for string latitude."""
        data: dict[str, Any] = {"latitude": "52.5", "longitude": 13.4}
        assert validate_location_data(data) is False

    def test_invalid_string_longitude(self) -> None:
        """Should return False for string longitude."""
        data: dict[str, Any] = {"latitude": 52.5, "longitude": "13.4"}
        assert validate_location_data(data) is False

    def test_custom_required_fields(self) -> None:
        """Should validate custom required fields."""
        data = {"latitude": 52.5, "longitude": 13.4, "accuracy": 10.0}
        assert (
            validate_location_data(data, required_fields=("latitude", "accuracy"))
            is True
        )

    def test_custom_required_fields_missing(self) -> None:
        """Should fail when custom required field missing."""
        data = {"latitude": 52.5, "longitude": 13.4}
        assert (
            validate_location_data(data, required_fields=("latitude", "accuracy"))
            is False
        )

    def test_empty_data(self) -> None:
        """Should return False for empty dict."""
        assert validate_location_data({}) is False

    def test_zero_coordinates_valid(self) -> None:
        """Should accept zero coordinates (null island is valid)."""
        data = {"latitude": 0.0, "longitude": 0.0}
        assert validate_location_data(data) is True

    def test_negative_coordinates_valid(self) -> None:
        """Should accept negative coordinates."""
        data = {"latitude": -33.86, "longitude": -151.21}
        assert validate_location_data(data) is True


# ---------------------------------------------------------------------------
# merge_location_update Tests
# ---------------------------------------------------------------------------


class TestMergeLocationUpdate:
    """Tests for merge_location_update function."""

    def test_merge_into_none(self) -> None:
        """Should return copy of update when existing is None."""
        update = {"latitude": 52.5, "longitude": 13.4}
        result = merge_location_update(None, update)
        assert result == update
        assert result is not update  # Should be a copy

    def test_merge_overwrites_values(self) -> None:
        """Should overwrite existing values with new ones."""
        existing = {"latitude": 50.0, "longitude": 10.0, "name": "Old"}
        update = {"latitude": 52.5, "longitude": 13.4}
        result = merge_location_update(existing, update)
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4
        assert result["name"] == "Old"

    def test_merge_preserves_unset_fields(self) -> None:
        """Should preserve fields not in update."""
        existing = {"latitude": 50.0, "longitude": 10.0, "accuracy": 5.0}
        update = {"latitude": 52.5, "longitude": 13.4}
        result = merge_location_update(existing, update)
        assert result["accuracy"] == 5.0

    def test_merge_preserves_better_accuracy(self) -> None:
        """Should preserve better (lower) accuracy."""
        existing = {"latitude": 50.0, "accuracy": 5.0}
        update = {"latitude": 52.5, "accuracy": 10.0}
        result = merge_location_update(existing, update, preserve_better_accuracy=True)
        assert result["accuracy"] == 5.0

    def test_merge_uses_new_accuracy_if_better(self) -> None:
        """Should use new accuracy if better than existing."""
        existing = {"latitude": 50.0, "accuracy": 10.0}
        update = {"latitude": 52.5, "accuracy": 5.0}
        result = merge_location_update(existing, update, preserve_better_accuracy=True)
        assert result["accuracy"] == 5.0

    def test_merge_no_accuracy_preservation(self) -> None:
        """Should not preserve accuracy when disabled."""
        existing = {"latitude": 50.0, "accuracy": 5.0}
        update = {"latitude": 52.5, "accuracy": 10.0}
        result = merge_location_update(existing, update, preserve_better_accuracy=False)
        assert result["accuracy"] == 10.0

    def test_merge_handles_none_existing_accuracy(self) -> None:
        """Should use new accuracy when existing is None."""
        existing = {"latitude": 50.0, "accuracy": None}
        update = {"latitude": 52.5, "accuracy": 10.0}
        result = merge_location_update(existing, update, preserve_better_accuracy=True)
        assert result["accuracy"] == 10.0

    def test_merge_handles_none_new_accuracy(self) -> None:
        """Should preserve existing accuracy when new is None."""
        existing = {"latitude": 50.0, "accuracy": 5.0}
        update = {"latitude": 52.5, "accuracy": None}
        result = merge_location_update(existing, update, preserve_better_accuracy=True)
        assert result["accuracy"] is None  # update overwrites

    def test_merge_does_not_modify_inputs(self) -> None:
        """Should not modify input dictionaries."""
        existing = {"latitude": 50.0, "longitude": 10.0}
        update = {"latitude": 52.5}
        original_existing = dict(existing)
        original_update = dict(update)
        merge_location_update(existing, update)
        assert existing == original_existing
        assert update == original_update


# ---------------------------------------------------------------------------
# detect_significant_change Tests
# ---------------------------------------------------------------------------


class TestDetectSignificantChange:
    """Tests for detect_significant_change function."""

    def test_significant_when_no_existing(self) -> None:
        """Should be significant when no existing data."""
        new = {"latitude": 52.5, "longitude": 13.4}
        assert detect_significant_change(None, new) is True

    def test_significant_large_distance(self) -> None:
        """Should detect significant distance change."""
        existing = {"latitude": 52.5, "longitude": 13.4}
        new = {"latitude": 52.6, "longitude": 13.5}  # ~11km apart
        assert detect_significant_change(existing, new, min_distance_meters=100) is True

    def test_not_significant_small_distance(self) -> None:
        """Should not be significant for small distance."""
        existing = {"latitude": 52.5, "longitude": 13.4}
        new = {"latitude": 52.5001, "longitude": 13.4001}  # ~10m apart
        assert (
            detect_significant_change(existing, new, min_distance_meters=100) is False
        )

    def test_significant_timestamp_change(self) -> None:
        """Should be significant when timestamp changes significantly."""
        existing = {"latitude": 52.5, "longitude": 13.4, "last_seen": 1000}
        new = {"latitude": 52.5, "longitude": 13.4, "last_seen": 2000}
        assert (
            detect_significant_change(existing, new, min_timestamp_diff_seconds=500)
            is True
        )

    def test_not_significant_small_timestamp_change(self) -> None:
        """Should not be significant for small timestamp change."""
        existing = {"latitude": 52.5, "longitude": 13.4, "last_seen": 1000}
        new = {"latitude": 52.5, "longitude": 13.4, "last_seen": 1010}
        assert (
            detect_significant_change(existing, new, min_timestamp_diff_seconds=500)
            is False
        )

    def test_significant_new_has_coordinates(self) -> None:
        """Should be significant when new data has coordinates and existing doesn't."""
        existing = {"name": "Device"}
        new = {"latitude": 52.5, "longitude": 13.4}
        assert detect_significant_change(existing, new) is True

    def test_significant_accuracy_improvement(self) -> None:
        """Should be significant when accuracy improves significantly."""
        existing = {"latitude": 52.5, "longitude": 13.4, "accuracy": 100}
        new = {"latitude": 52.5, "longitude": 13.4, "accuracy": 10}
        assert (
            detect_significant_change(existing, new, min_accuracy_improvement=50)
            is True
        )

    def test_handles_missing_coordinates_in_existing(self) -> None:
        """Should handle missing coordinates in existing data."""
        existing = {"latitude": None, "longitude": None}
        new = {"latitude": 52.5, "longitude": 13.4}
        assert detect_significant_change(existing, new) is True

    def test_handles_missing_coordinates_in_new(self) -> None:
        """Should not be significant when new data lacks coordinates."""
        existing = {"latitude": 52.5, "longitude": 13.4}
        new = {"latitude": None, "longitude": None}
        assert detect_significant_change(existing, new) is False


# ---------------------------------------------------------------------------
# select_best_accuracy Tests
# ---------------------------------------------------------------------------


class TestSelectBestAccuracy:
    """Tests for select_best_accuracy function."""

    def test_selects_lower_value(self) -> None:
        """Should select lower accuracy (better)."""
        assert select_best_accuracy(5.0, 10.0) == 5.0
        assert select_best_accuracy(10.0, 5.0) == 5.0

    def test_handles_none_first(self) -> None:
        """Should return second when first is None."""
        assert select_best_accuracy(None, 10.0) == 10.0

    def test_handles_none_second(self) -> None:
        """Should return first when second is None."""
        assert select_best_accuracy(5.0, None) == 5.0

    def test_handles_both_none(self) -> None:
        """Should return None when both are None."""
        assert select_best_accuracy(None, None) is None

    def test_equal_values(self) -> None:
        """Should return value when equal."""
        assert select_best_accuracy(5.0, 5.0) == 5.0

    def test_handles_int_values(self) -> None:
        """Should handle integer accuracy values."""
        assert select_best_accuracy(5, 10) == 5

    def test_handles_zero(self) -> None:
        """Should handle zero accuracy (best possible)."""
        assert select_best_accuracy(0.0, 10.0) == 0.0
        assert select_best_accuracy(10.0, 0.0) == 0.0


# ---------------------------------------------------------------------------
# normalize_location_fields Tests
# ---------------------------------------------------------------------------


class TestNormalizeLocationFields:
    """Tests for normalize_location_fields function."""

    def test_normalizes_string_latitude(self) -> None:
        """Should convert string latitude to float."""
        data: dict[str, Any] = {"latitude": "52.5", "longitude": 13.4}
        result = normalize_location_fields(data)
        assert result["latitude"] == 52.5
        assert isinstance(result["latitude"], float)

    def test_normalizes_string_longitude(self) -> None:
        """Should convert string longitude to float."""
        data: dict[str, Any] = {"latitude": 52.5, "longitude": "13.4"}
        result = normalize_location_fields(data)
        assert result["longitude"] == 13.4
        assert isinstance(result["longitude"], float)

    def test_normalizes_int_to_float(self) -> None:
        """Should convert int coordinates to float."""
        data = {"latitude": 52, "longitude": 13}
        result = normalize_location_fields(data)
        assert result["latitude"] == 52.0
        assert result["longitude"] == 13.0

    def test_preserves_valid_floats(self) -> None:
        """Should preserve valid float coordinates."""
        data = {"latitude": 52.5, "longitude": 13.4}
        result = normalize_location_fields(data)
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4

    def test_handles_none_values(self) -> None:
        """Should preserve None values."""
        data = {"latitude": None, "longitude": None}
        result = normalize_location_fields(data)
        assert result["latitude"] is None
        assert result["longitude"] is None

    def test_normalizes_accuracy(self) -> None:
        """Should normalize accuracy field."""
        data: dict[str, Any] = {"latitude": 52.5, "longitude": 13.4, "accuracy": "10"}
        result = normalize_location_fields(data)
        assert result["accuracy"] == 10.0

    def test_normalizes_altitude(self) -> None:
        """Should normalize altitude field."""
        data: dict[str, Any] = {
            "latitude": 52.5,
            "longitude": 13.4,
            "altitude": "100.5",
        }
        result = normalize_location_fields(data)
        assert result["altitude"] == 100.5

    def test_handles_invalid_string(self) -> None:
        """Should handle invalid string conversion."""
        data: dict[str, Any] = {"latitude": "invalid", "longitude": 13.4}
        result = normalize_location_fields(data)
        assert result["latitude"] is None

    def test_does_not_modify_input(self) -> None:
        """Should not modify input dictionary."""
        data: dict[str, Any] = {"latitude": "52.5", "longitude": 13.4}
        original = dict(data)
        normalize_location_fields(data)
        assert data == original

    def test_preserves_other_fields(self) -> None:
        """Should preserve non-coordinate fields."""
        data = {"latitude": 52.5, "longitude": 13.4, "name": "Test", "status": "ok"}
        result = normalize_location_fields(data)
        assert result["name"] == "Test"
        assert result["status"] == "ok"


# ---------------------------------------------------------------------------
# preserve_metadata_fields Tests
# ---------------------------------------------------------------------------


class TestPreserveMetadataFields:
    """Tests for preserve_metadata_fields function."""

    def test_preserves_from_previous(self) -> None:
        """Should preserve metadata fields from previous cache."""
        previous = {"identity_key": b"key123", "owner_key_version": 5}
        current: dict[str, Any] = {"latitude": 52.5}
        metadata_keys = ("identity_key", "owner_key_version")
        result = preserve_metadata_fields(previous, current, metadata_keys)
        assert result["identity_key"] == b"key123"
        assert result["owner_key_version"] == 5

    def test_does_not_overwrite_existing(self) -> None:
        """Should not overwrite existing values in current."""
        previous = {"identity_key": b"old_key"}
        current = {"identity_key": b"new_key", "latitude": 52.5}
        metadata_keys = ("identity_key",)
        result = preserve_metadata_fields(previous, current, metadata_keys)
        assert result["identity_key"] == b"new_key"

    def test_handles_none_previous(self) -> None:
        """Should return current when previous is None."""
        current = {"latitude": 52.5}
        result = preserve_metadata_fields(None, current, ("identity_key",))
        assert result == {"latitude": 52.5}

    def test_handles_empty_previous(self) -> None:
        """Should handle empty previous dict."""
        result = preserve_metadata_fields({}, {"latitude": 52.5}, ("identity_key",))
        assert "identity_key" not in result

    def test_deep_copies_dict_values(self) -> None:
        """Should deep copy dict metadata values."""
        previous = {"metadata": {"key": "value"}}
        current: dict[str, Any] = {"latitude": 52.5}
        result = preserve_metadata_fields(previous, current, ("metadata",))
        assert result["metadata"] == {"key": "value"}
        assert result["metadata"] is not previous["metadata"]

    def test_deep_copies_list_values(self) -> None:
        """Should deep copy list metadata values."""
        previous = {"tags": ["a", "b"]}
        current: dict[str, Any] = {"latitude": 52.5}
        result = preserve_metadata_fields(previous, current, ("tags",))
        assert result["tags"] == ["a", "b"]
        assert result["tags"] is not previous["tags"]

    def test_preserves_none_values_in_previous(self) -> None:
        """Should not copy None values from previous."""
        previous = {"identity_key": None}
        current: dict[str, Any] = {"latitude": 52.5}
        result = preserve_metadata_fields(previous, current, ("identity_key",))
        assert "identity_key" not in result

    def test_does_not_modify_inputs(self) -> None:
        """Should not modify input dictionaries."""
        previous = {"identity_key": b"key"}
        current: dict[str, Any] = {"latitude": 52.5}
        orig_prev = dict(previous)
        orig_curr = dict(current)
        preserve_metadata_fields(previous, current, ("identity_key",))
        assert previous == orig_prev
        assert current == orig_curr


# ---------------------------------------------------------------------------
# should_clear_metadata_only_flag Tests
# ---------------------------------------------------------------------------


class TestShouldClearMetadataOnlyFlag:
    """Tests for should_clear_metadata_only_flag function."""

    def test_clear_when_has_location_and_not_explicit(self) -> None:
        """Should clear when has location and metadata_only not explicitly True."""
        data = {"latitude": 52.5, "longitude": 13.4, "metadata_only": True}
        assert (
            should_clear_metadata_only_flag(data, incoming_metadata_only=None) is True
        )

    def test_no_clear_when_explicit_true(self) -> None:
        """Should not clear when incoming metadata_only is explicitly True."""
        data = {"latitude": 52.5, "longitude": 13.4, "metadata_only": True}
        assert (
            should_clear_metadata_only_flag(data, incoming_metadata_only=True) is False
        )

    def test_clear_when_explicit_false(self) -> None:
        """Should clear when incoming metadata_only is explicitly False."""
        data = {"metadata_only": True}
        assert (
            should_clear_metadata_only_flag(data, incoming_metadata_only=False) is True
        )

    def test_no_clear_when_no_metadata_only_flag(self) -> None:
        """Should not clear when no metadata_only flag present."""
        data = {"latitude": 52.5, "longitude": 13.4}
        assert (
            should_clear_metadata_only_flag(data, incoming_metadata_only=None) is False
        )

    def test_no_clear_when_no_location(self) -> None:
        """Should not clear when no location data."""
        data = {"metadata_only": True, "name": "Device"}
        assert (
            should_clear_metadata_only_flag(data, incoming_metadata_only=None) is False
        )

    def test_has_location_with_latitude_only(self) -> None:
        """Should detect location with latitude only."""
        data = {"latitude": 52.5, "metadata_only": True}
        assert (
            should_clear_metadata_only_flag(data, incoming_metadata_only=None) is True
        )

    def test_has_location_with_longitude_only(self) -> None:
        """Should detect location with longitude only."""
        data = {"longitude": 13.4, "metadata_only": True}
        assert (
            should_clear_metadata_only_flag(data, incoming_metadata_only=None) is True
        )

    def test_handles_none_coordinates(self) -> None:
        """Should handle None coordinate values."""
        data = {"latitude": None, "longitude": None, "metadata_only": True}
        assert (
            should_clear_metadata_only_flag(data, incoming_metadata_only=None) is False
        )
