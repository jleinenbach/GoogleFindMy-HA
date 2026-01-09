"""Tests for coordinator_locate.py Phase 14 locate helper functions.

These tests validate the locate helper functions extracted from
async_locate_device into coordinator_locate.py.

Test categories:
1. parse_locate_response - Parse location from API response
2. validate_locate_result - Validate location result
3. detect_null_island - Detect invalid (0,0) coordinates
4. build_locate_cache_entry - Build cache entry from location data
5. classify_locate_error - Classify locate errors

REQUIREMENT: 100% test coverage for all extracted functions.

KEY RISKS COVERED:
- API response parsing with various field combinations
- Null island detection with configurable threshold
- Error classification for different exception types
- Cache entry building with metadata preservation
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.coordinator_locate import (
    build_locate_cache_entry,
    classify_locate_error,
    detect_null_island,
    extract_location_from_response,
    normalize_locate_coordinates,
    parse_locate_response,
    validate_locate_result,
)

# ---------------------------------------------------------------------------
# parse_locate_response Tests
# ---------------------------------------------------------------------------


class TestParseLocateResponse:
    """Tests for parse_locate_response function."""

    def test_parses_valid_response(self) -> None:
        """Should parse valid location response."""
        response = {
            "location": {
                "latitude": 52.5,
                "longitude": 13.4,
                "accuracy": 10.0,
                "timestamp": 1234567890,
            }
        }
        result = parse_locate_response(response)
        assert result is not None
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4
        assert result["accuracy"] == 10.0

    def test_parses_minimal_response(self) -> None:
        """Should parse response with only lat/lon."""
        response = {"location": {"latitude": 52.5, "longitude": 13.4}}
        result = parse_locate_response(response)
        assert result is not None
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4

    def test_returns_none_for_missing_location(self) -> None:
        """Should return None when location key missing."""
        response: dict[str, Any] = {}
        assert parse_locate_response(response) is None

    def test_returns_none_for_none_location(self) -> None:
        """Should return None when location is None."""
        response = {"location": None}
        assert parse_locate_response(response) is None

    def test_returns_none_for_non_dict_location(self) -> None:
        """Should return None when location is not a dict."""
        response: dict[str, Any] = {"location": "invalid"}
        assert parse_locate_response(response) is None

    def test_returns_none_for_missing_latitude(self) -> None:
        """Should return None when latitude missing."""
        response = {"location": {"longitude": 13.4}}
        assert parse_locate_response(response) is None

    def test_returns_none_for_missing_longitude(self) -> None:
        """Should return None when longitude missing."""
        response = {"location": {"latitude": 52.5}}
        assert parse_locate_response(response) is None

    def test_returns_none_for_none_latitude(self) -> None:
        """Should return None when latitude is None."""
        response = {"location": {"latitude": None, "longitude": 13.4}}
        assert parse_locate_response(response) is None

    def test_returns_none_for_none_longitude(self) -> None:
        """Should return None when longitude is None."""
        response = {"location": {"latitude": 52.5, "longitude": None}}
        assert parse_locate_response(response) is None

    def test_preserves_optional_fields(self) -> None:
        """Should preserve optional fields from response."""
        response = {
            "location": {
                "latitude": 52.5,
                "longitude": 13.4,
                "accuracy": 15.0,
                "altitude": 100.0,
                "last_seen": 1234567890,
                "semantic_name": "Home",
            }
        }
        result = parse_locate_response(response)
        assert result is not None
        assert result.get("accuracy") == 15.0
        assert result.get("altitude") == 100.0
        assert result.get("last_seen") == 1234567890
        assert result.get("semantic_name") == "Home"

    def test_converts_string_coordinates(self) -> None:
        """Should convert string coordinates to float."""
        response: dict[str, Any] = {
            "location": {"latitude": "52.5", "longitude": "13.4"}
        }
        result = parse_locate_response(response)
        assert result is not None
        assert result["latitude"] == 52.5
        assert isinstance(result["latitude"], float)

    def test_handles_integer_coordinates(self) -> None:
        """Should handle integer coordinates."""
        response = {"location": {"latitude": 52, "longitude": 13}}
        result = parse_locate_response(response)
        assert result is not None
        assert result["latitude"] == 52.0
        assert result["longitude"] == 13.0


# ---------------------------------------------------------------------------
# validate_locate_result Tests
# ---------------------------------------------------------------------------


class TestValidateLocateResult:
    """Tests for validate_locate_result function."""

    def test_valid_with_coordinates(self) -> None:
        """Should validate result with valid coordinates."""
        result = {"latitude": 52.5, "longitude": 13.4}
        assert validate_locate_result(result) is True

    def test_valid_with_semantic_name_only(self) -> None:
        """Should validate result with semantic name only."""
        result = {"semantic_name": "Home"}
        assert validate_locate_result(result, require_coordinates=False) is True

    def test_invalid_without_coordinates_when_required(self) -> None:
        """Should be invalid without coordinates when required."""
        result = {"semantic_name": "Home"}
        assert validate_locate_result(result, require_coordinates=True) is False

    def test_invalid_empty_result(self) -> None:
        """Should be invalid for empty result."""
        assert validate_locate_result({}) is False

    def test_invalid_none_result(self) -> None:
        """Should be invalid for None result."""
        assert validate_locate_result(None) is False

    def test_invalid_with_null_island(self) -> None:
        """Should be invalid for null island coordinates."""
        result = {"latitude": 0.0, "longitude": 0.0}
        assert validate_locate_result(result, reject_null_island=True) is False

    def test_valid_with_null_island_when_allowed(self) -> None:
        """Should be valid for null island when not rejected."""
        result = {"latitude": 0.0, "longitude": 0.0}
        assert validate_locate_result(result, reject_null_island=False) is True

    def test_invalid_with_none_coordinates(self) -> None:
        """Should be invalid when coordinates are None."""
        result = {"latitude": None, "longitude": None}
        assert validate_locate_result(result) is False

    def test_valid_near_null_island(self) -> None:
        """Should be valid for coordinates near but not at null island."""
        result = {"latitude": 0.001, "longitude": 0.001}
        assert validate_locate_result(result, reject_null_island=True) is True


# ---------------------------------------------------------------------------
# detect_null_island Tests
# ---------------------------------------------------------------------------


class TestDetectNullIsland:
    """Tests for detect_null_island function."""

    def test_detects_exact_zero(self) -> None:
        """Should detect exact (0, 0) coordinates."""
        assert detect_null_island(0.0, 0.0) is True

    def test_detects_near_zero(self) -> None:
        """Should detect near-zero coordinates within threshold."""
        assert detect_null_island(0.00001, 0.00001) is True

    def test_not_null_island_above_threshold(self) -> None:
        """Should not detect as null island above threshold."""
        assert detect_null_island(0.001, 0.001) is False

    def test_custom_threshold(self) -> None:
        """Should use custom threshold."""
        assert detect_null_island(0.01, 0.01, threshold=0.1) is True
        assert detect_null_island(0.01, 0.01, threshold=0.001) is False

    def test_not_null_island_normal_coordinates(self) -> None:
        """Should not detect normal coordinates as null island."""
        assert detect_null_island(52.5, 13.4) is False

    def test_not_null_island_negative_coordinates(self) -> None:
        """Should not detect negative coordinates as null island."""
        assert detect_null_island(-33.86, 151.21) is False

    def test_detects_negative_near_zero(self) -> None:
        """Should detect negative near-zero as null island."""
        assert detect_null_island(-0.00001, -0.00001) is True

    def test_partial_zero_latitude(self) -> None:
        """Should not detect when only latitude is zero."""
        assert detect_null_island(0.0, 13.4) is False

    def test_partial_zero_longitude(self) -> None:
        """Should not detect when only longitude is zero."""
        assert detect_null_island(52.5, 0.0) is False


# ---------------------------------------------------------------------------
# extract_location_from_response Tests
# ---------------------------------------------------------------------------


class TestExtractLocationFromResponse:
    """Tests for extract_location_from_response function."""

    def test_extracts_nested_location(self) -> None:
        """Should extract location from nested structure."""
        response = {"location": {"latitude": 52.5, "longitude": 13.4}}
        result = extract_location_from_response(response)
        assert result == {"latitude": 52.5, "longitude": 13.4}

    def test_extracts_flat_location(self) -> None:
        """Should extract location from flat structure."""
        response = {"latitude": 52.5, "longitude": 13.4}
        result = extract_location_from_response(response)
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4

    def test_prefers_nested_over_flat(self) -> None:
        """Should prefer nested location over flat."""
        response = {
            "latitude": 1.0,
            "longitude": 1.0,
            "location": {"latitude": 52.5, "longitude": 13.4},
        }
        result = extract_location_from_response(response)
        assert result["latitude"] == 52.5

    def test_returns_empty_dict_for_no_location(self) -> None:
        """Should return empty dict when no location found."""
        response: dict[str, Any] = {"status": "ok"}
        result = extract_location_from_response(response)
        assert result == {}

    def test_returns_empty_dict_for_none(self) -> None:
        """Should return empty dict for None input."""
        assert extract_location_from_response(None) == {}

    def test_returns_empty_dict_for_non_dict(self) -> None:
        """Should return empty dict for non-dict input."""
        assert extract_location_from_response("invalid") == {}


# ---------------------------------------------------------------------------
# normalize_locate_coordinates Tests
# ---------------------------------------------------------------------------


class TestNormalizeLocateCoordinates:
    """Tests for normalize_locate_coordinates function."""

    def test_normalizes_string_latitude(self) -> None:
        """Should convert string latitude to float."""
        data: dict[str, Any] = {"latitude": "52.5", "longitude": 13.4}
        result = normalize_locate_coordinates(data)
        assert result["latitude"] == 52.5
        assert isinstance(result["latitude"], float)

    def test_normalizes_string_longitude(self) -> None:
        """Should convert string longitude to float."""
        data: dict[str, Any] = {"latitude": 52.5, "longitude": "13.4"}
        result = normalize_locate_coordinates(data)
        assert result["longitude"] == 13.4

    def test_normalizes_accuracy(self) -> None:
        """Should normalize accuracy to float."""
        data: dict[str, Any] = {"latitude": 52.5, "longitude": 13.4, "accuracy": "10"}
        result = normalize_locate_coordinates(data)
        assert result["accuracy"] == 10.0

    def test_handles_invalid_string(self) -> None:
        """Should set None for invalid string."""
        data: dict[str, Any] = {"latitude": "invalid", "longitude": 13.4}
        result = normalize_locate_coordinates(data)
        assert result["latitude"] is None

    def test_preserves_none_values(self) -> None:
        """Should preserve None coordinate values."""
        data = {"latitude": None, "longitude": None}
        result = normalize_locate_coordinates(data)
        assert result["latitude"] is None
        assert result["longitude"] is None

    def test_does_not_modify_input(self) -> None:
        """Should not modify input dict."""
        data: dict[str, Any] = {"latitude": "52.5", "longitude": 13.4}
        original = dict(data)
        normalize_locate_coordinates(data)
        assert data == original


# ---------------------------------------------------------------------------
# build_locate_cache_entry Tests
# ---------------------------------------------------------------------------


class TestBuildLocateCacheEntry:
    """Tests for build_locate_cache_entry function."""

    def test_builds_basic_entry(self) -> None:
        """Should build basic cache entry."""
        location_data = {"latitude": 52.5, "longitude": 13.4}
        result = build_locate_cache_entry(location_data, device_id="dev123")
        assert result["latitude"] == 52.5
        assert result["longitude"] == 13.4
        assert result["device_id"] == "dev123"

    def test_sets_source_to_manual(self) -> None:
        """Should set source to manual locate."""
        location_data = {"latitude": 52.5, "longitude": 13.4}
        result = build_locate_cache_entry(location_data, device_id="dev123")
        assert result.get("source") == "manual"

    def test_adds_last_updated(self) -> None:
        """Should add last_updated timestamp."""
        location_data = {"latitude": 52.5, "longitude": 13.4}
        result = build_locate_cache_entry(
            location_data, device_id="dev123", timestamp=1234567890.0
        )
        assert result["last_updated"] == 1234567890.0

    def test_preserves_existing_fields(self) -> None:
        """Should preserve existing fields from location data."""
        location_data = {
            "latitude": 52.5,
            "longitude": 13.4,
            "accuracy": 10.0,
            "semantic_name": "Office",
        }
        result = build_locate_cache_entry(location_data, device_id="dev123")
        assert result["accuracy"] == 10.0
        assert result["semantic_name"] == "Office"

    def test_does_not_modify_input(self) -> None:
        """Should not modify input dict."""
        location_data = {"latitude": 52.5, "longitude": 13.4}
        original = dict(location_data)
        build_locate_cache_entry(location_data, device_id="dev123")
        assert location_data == original


# ---------------------------------------------------------------------------
# classify_locate_error Tests
# ---------------------------------------------------------------------------


class TestClassifyLocateError:
    """Tests for classify_locate_error function."""

    def test_classifies_timeout(self) -> None:
        """Should classify TimeoutError."""
        assert classify_locate_error(TimeoutError()) == "timeout"

    def test_classifies_asyncio_timeout(self) -> None:
        """Should classify asyncio.TimeoutError."""
        assert classify_locate_error(TimeoutError()) == "timeout"

    def test_classifies_connection_error(self) -> None:
        """Should classify ConnectionError."""
        assert classify_locate_error(ConnectionError()) == "connection"

    def test_classifies_os_error(self) -> None:
        """Should classify OSError."""
        assert classify_locate_error(OSError()) == "connection"

    def test_classifies_value_error(self) -> None:
        """Should classify ValueError as data error."""
        assert classify_locate_error(ValueError()) == "data"

    def test_classifies_key_error(self) -> None:
        """Should classify KeyError as data error."""
        assert classify_locate_error(KeyError()) == "data"

    def test_classifies_type_error(self) -> None:
        """Should classify TypeError as data error."""
        assert classify_locate_error(TypeError()) == "data"

    def test_classifies_permission_error(self) -> None:
        """Should classify PermissionError as auth error."""
        assert classify_locate_error(PermissionError()) == "auth"

    def test_classifies_unknown_error(self) -> None:
        """Should classify unknown error as unknown."""
        assert classify_locate_error(RuntimeError()) == "unknown"

    def test_classifies_none(self) -> None:
        """Should return unknown for None."""
        assert classify_locate_error(None) == "unknown"

    def test_classifies_exception_with_auth_message(self) -> None:
        """Should detect auth from error message."""
        err = Exception("Authentication failed")
        assert classify_locate_error(err) == "auth"

    def test_classifies_exception_with_expired_message(self) -> None:
        """Should detect auth from expired message."""
        err = Exception("Token expired")
        assert classify_locate_error(err) == "auth"
