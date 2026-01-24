"""Tests for coordinator_cache.py Phase 13 cache update functions.

These tests validate the cache update helper functions extracted from
update_device_cache into coordinator_cache.py.

Test categories:
1. normalize_location_fields - Normalize field types
2. preserve_metadata_fields - Preserve metadata from previous cache
3. should_clear_metadata_only_flag - Determine if flag should be cleared

REQUIREMENT: 100% test coverage for all extracted functions.

KEY RISKS COVERED:
- Field type normalization
- Metadata preservation
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.coordinator.helpers.cache import (
    normalize_location_fields,
    preserve_metadata_fields,
    should_clear_metadata_only_flag,
)

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
