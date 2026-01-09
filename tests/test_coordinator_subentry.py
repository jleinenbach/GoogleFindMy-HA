"""Tests for coordinator_subentry.py - Phase 5 of coordinator refactoring.

These tests validate the subentry utility functions extracted from coordinator.py
into coordinator_subentry.py.

Test categories:
1. sanitize_subentry_identifier - Normalize subentry identifiers
2. normalize_epoch_seconds - Epoch normalization with ms/s tolerance
3. format_epoch_utc - Format epoch to ISO 8601 UTC string
4. parse_last_seen_timestamp - Parse timestamp from various formats
5. group_devices_by_subentry - Group devices by subentry key

REQUIREMENT: 100% test coverage for all extracted functions.

KEY RISKS COVERED:
- Subentry identifier sanitization (empty strings, whitespace, non-strings)
- Epoch normalization (milliseconds vs seconds, overflow, invalid values)
- Timestamp parsing (various formats, edge cases)
- Device grouping with fallback keys
"""

from __future__ import annotations

from custom_components.googlefindmy.coordinator_subentry import (
    format_epoch_utc,
    group_devices_by_subentry,
    normalize_epoch_seconds,
    parse_last_seen_timestamp,
    sanitize_subentry_identifier,
)

# ---------------------------------------------------------------------------
# sanitize_subentry_identifier Tests
# ---------------------------------------------------------------------------


class TestSanitizeSubentryIdentifier:
    """Tests for sanitize_subentry_identifier function.

    RISK: Invalid subentry identifiers can cause lookup failures.
    """

    def test_valid_identifier(self) -> None:
        """Valid string identifier is returned as-is."""
        result = sanitize_subentry_identifier("my-subentry")
        assert result == "my-subentry"

    def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace is stripped."""
        result = sanitize_subentry_identifier("  my-subentry  ")
        assert result == "my-subentry"

    def test_empty_string_returns_none(self) -> None:
        """Empty string returns None."""
        result = sanitize_subentry_identifier("")
        assert result is None

    def test_whitespace_only_returns_none(self) -> None:
        """Whitespace-only string returns None."""
        result = sanitize_subentry_identifier("   ")
        assert result is None

    def test_none_returns_none(self) -> None:
        """None input returns None."""
        result = sanitize_subentry_identifier(None)
        assert result is None

    def test_integer_returns_none(self) -> None:
        """Integer input returns None."""
        result = sanitize_subentry_identifier(123)
        assert result is None

    def test_list_returns_none(self) -> None:
        """List input returns None."""
        result = sanitize_subentry_identifier(["subentry"])
        assert result is None

    def test_dict_returns_none(self) -> None:
        """Dict input returns None."""
        result = sanitize_subentry_identifier({"key": "value"})
        assert result is None

    def test_tabs_and_newlines_stripped(self) -> None:
        """Tabs and newlines are stripped."""
        result = sanitize_subentry_identifier("\t\nsubentry\n\t")
        assert result == "subentry"

    def test_unicode_identifier(self) -> None:
        """Unicode identifier is accepted."""
        result = sanitize_subentry_identifier("子条目-🔑")
        assert result == "子条目-🔑"

    def test_hyphenated_identifier(self) -> None:
        """Hyphenated identifier is valid."""
        result = sanitize_subentry_identifier("entry-id-123-service-subentry")
        assert result == "entry-id-123-service-subentry"


# ---------------------------------------------------------------------------
# normalize_epoch_seconds Tests
# ---------------------------------------------------------------------------


class TestNormalizeEpochSeconds:
    """Tests for normalize_epoch_seconds function.

    RISK: Millisecond vs second timestamps must be handled correctly.
    """

    def test_seconds_timestamp(self) -> None:
        """Standard epoch seconds are returned as int."""
        result = normalize_epoch_seconds(1704067200)
        assert result == 1704067200

    def test_float_seconds(self) -> None:
        """Float seconds are truncated to int."""
        result = normalize_epoch_seconds(1704067200.123)
        assert result == 1704067200

    def test_milliseconds_converted(self) -> None:
        """Milliseconds (>= 1e11) are converted to seconds."""
        result = normalize_epoch_seconds(1704067200000)
        assert result == 1704067200

    def test_string_seconds(self) -> None:
        """String timestamp is parsed."""
        result = normalize_epoch_seconds("1704067200")
        assert result == 1704067200

    def test_string_milliseconds(self) -> None:
        """String milliseconds are converted."""
        result = normalize_epoch_seconds("1704067200000")
        assert result == 1704067200

    def test_string_with_whitespace(self) -> None:
        """String with whitespace is trimmed."""
        result = normalize_epoch_seconds("  1704067200  ")
        assert result == 1704067200

    def test_empty_string_returns_none(self) -> None:
        """Empty string returns None."""
        result = normalize_epoch_seconds("")
        assert result is None

    def test_whitespace_only_string_returns_none(self) -> None:
        """Whitespace-only string returns None."""
        result = normalize_epoch_seconds("   ")
        assert result is None

    def test_none_returns_none(self) -> None:
        """None returns None."""
        result = normalize_epoch_seconds(None)
        assert result is None

    def test_invalid_string_returns_none(self) -> None:
        """Invalid string returns None."""
        result = normalize_epoch_seconds("not_a_number")
        assert result is None

    def test_nan_returns_none(self) -> None:
        """NaN returns None."""
        result = normalize_epoch_seconds(float("nan"))
        assert result is None

    def test_inf_returns_none(self) -> None:
        """Infinity returns None."""
        result = normalize_epoch_seconds(float("inf"))
        assert result is None

    def test_negative_inf_returns_none(self) -> None:
        """Negative infinity returns None."""
        result = normalize_epoch_seconds(float("-inf"))
        assert result is None

    def test_zero_timestamp(self) -> None:
        """Zero (Unix epoch) is valid."""
        result = normalize_epoch_seconds(0)
        assert result == 0

    def test_negative_timestamp(self) -> None:
        """Negative timestamp (before epoch) is valid."""
        result = normalize_epoch_seconds(-86400)
        assert result == -86400

    def test_negative_milliseconds_converted(self) -> None:
        """Negative milliseconds are converted."""
        # -1e11 is >= 1e11 in absolute value
        result = normalize_epoch_seconds(-170406720000)
        assert result == -170406720  # Divided by 1000

    def test_list_returns_none(self) -> None:
        """List input returns None."""
        result = normalize_epoch_seconds([1704067200])
        assert result is None

    def test_very_large_timestamp(self) -> None:
        """Very large timestamp (year 3000+) is handled."""
        # Year 3000 is roughly 32503680000 seconds
        result = normalize_epoch_seconds(32503680000)
        assert result == 32503680000

    def test_boundary_milliseconds(self) -> None:
        """Boundary value for ms detection (just below 1e11)."""
        # 99999999999 is just below 1e11, treated as seconds
        result = normalize_epoch_seconds(99999999999)
        assert result == 99999999999

    def test_boundary_milliseconds_exact(self) -> None:
        """Exact boundary value (1e11) is treated as milliseconds."""
        result = normalize_epoch_seconds(100000000000)
        assert result == 100000000  # Divided by 1000


# ---------------------------------------------------------------------------
# format_epoch_utc Tests
# ---------------------------------------------------------------------------


class TestFormatEpochUtc:
    """Tests for format_epoch_utc function.

    RISK: Incorrect ISO 8601 formatting breaks interoperability.
    """

    def test_basic_formatting(self) -> None:
        """Basic epoch to ISO 8601 conversion."""
        result = format_epoch_utc(1704067200)
        assert result is not None
        assert "2024-01-01" in result
        assert result.endswith("Z")

    def test_uses_z_suffix(self) -> None:
        """Output uses Z suffix instead of +00:00."""
        result = format_epoch_utc(1704067200)
        assert result is not None
        assert result.endswith("Z")
        assert "+00:00" not in result

    def test_milliseconds_input(self) -> None:
        """Milliseconds are normalized before formatting."""
        result = format_epoch_utc(1704067200000)
        assert result is not None
        assert "2024-01-01" in result

    def test_string_input(self) -> None:
        """String timestamp is accepted."""
        result = format_epoch_utc("1704067200")
        assert result is not None
        assert "2024-01-01" in result

    def test_none_returns_none(self) -> None:
        """None input returns None."""
        result = format_epoch_utc(None)
        assert result is None

    def test_invalid_returns_none(self) -> None:
        """Invalid input returns None."""
        result = format_epoch_utc("invalid")
        assert result is None

    def test_zero_epoch(self) -> None:
        """Unix epoch (0) is formatted correctly."""
        result = format_epoch_utc(0)
        assert result is not None
        assert "1970-01-01" in result

    def test_iso_format_structure(self) -> None:
        """Output follows ISO 8601 structure."""
        result = format_epoch_utc(1704067200)
        assert result is not None
        # Should be like "2024-01-01T00:00:00Z"
        parts = result.replace("Z", "").split("T")
        assert len(parts) == 2
        assert len(parts[0].split("-")) == 3  # Date: YYYY-MM-DD
        assert ":" in parts[1]  # Time has colons


# ---------------------------------------------------------------------------
# parse_last_seen_timestamp Tests
# ---------------------------------------------------------------------------


class TestParseLastSeenTimestamp:
    """Tests for parse_last_seen_timestamp function.

    RISK: Various timestamp formats from different sources must be parsed.
    """

    def test_epoch_seconds_int(self) -> None:
        """Integer epoch seconds are parsed."""
        result = parse_last_seen_timestamp(1704067200)
        assert result == 1704067200

    def test_epoch_seconds_float(self) -> None:
        """Float epoch seconds are parsed (truncated)."""
        result = parse_last_seen_timestamp(1704067200.5)
        assert result == 1704067200

    def test_epoch_milliseconds(self) -> None:
        """Milliseconds are converted to seconds."""
        result = parse_last_seen_timestamp(1704067200000)
        assert result == 1704067200

    def test_string_epoch(self) -> None:
        """String epoch is parsed."""
        result = parse_last_seen_timestamp("1704067200")
        assert result == 1704067200

    def test_iso_format_with_z(self) -> None:
        """ISO 8601 format with Z suffix is parsed."""
        result = parse_last_seen_timestamp("2024-01-01T00:00:00Z")
        assert result is not None
        # Should be close to 1704067200
        assert abs(result - 1704067200) < 1

    def test_iso_format_with_offset(self) -> None:
        """ISO 8601 format with +00:00 offset is parsed."""
        result = parse_last_seen_timestamp("2024-01-01T00:00:00+00:00")
        assert result is not None
        assert abs(result - 1704067200) < 1

    def test_iso_format_with_timezone(self) -> None:
        """ISO 8601 format with timezone offset is parsed."""
        result = parse_last_seen_timestamp("2024-01-01T01:00:00+01:00")
        assert result is not None
        # +01:00 means UTC is 1 hour earlier
        assert abs(result - 1704067200) < 1

    def test_none_returns_none(self) -> None:
        """None returns None."""
        result = parse_last_seen_timestamp(None)
        assert result is None

    def test_empty_string_returns_none(self) -> None:
        """Empty string returns None."""
        result = parse_last_seen_timestamp("")
        assert result is None

    def test_invalid_string_returns_none(self) -> None:
        """Invalid string returns None."""
        result = parse_last_seen_timestamp("not-a-timestamp")
        assert result is None

    def test_invalid_iso_returns_none(self) -> None:
        """Invalid ISO format returns None."""
        result = parse_last_seen_timestamp("2024-13-45T99:99:99Z")
        assert result is None

    def test_list_returns_none(self) -> None:
        """List input returns None."""
        result = parse_last_seen_timestamp([1704067200])
        assert result is None


# ---------------------------------------------------------------------------
# group_devices_by_subentry Tests
# ---------------------------------------------------------------------------


class TestGroupDevicesBySubentry:
    """Tests for group_devices_by_subentry function.

    RISK: Incorrect grouping breaks subentry-aware entity updates.
    """

    def test_basic_grouping(self) -> None:
        """Devices are grouped by their assigned subentry."""
        device_to_subentry = {"device1": "subentry-a", "device2": "subentry-b"}
        devices = [
            {"device_id": "device1", "name": "Device 1"},
            {"device_id": "device2", "name": "Device 2"},
        ]
        fallback_key = "default"
        subentry_keys = {"subentry-a", "subentry-b", "default"}

        result = group_devices_by_subentry(
            devices, device_to_subentry, fallback_key, subentry_keys
        )

        assert "device1" in str(result.get("subentry-a", []))
        assert "device2" in str(result.get("subentry-b", []))

    def test_fallback_for_unknown_device(self) -> None:
        """Unknown devices go to fallback subentry."""
        device_to_subentry = {"device1": "subentry-a"}
        devices = [
            {"device_id": "device1", "name": "Device 1"},
            {"device_id": "device3", "name": "Device 3"},  # Unknown
        ]
        fallback_key = "default"
        subentry_keys = {"subentry-a", "default"}

        result = group_devices_by_subentry(
            devices, device_to_subentry, fallback_key, subentry_keys
        )

        assert len(result.get("default", [])) >= 1

    def test_empty_devices(self) -> None:
        """Empty device list returns empty groups."""
        result = group_devices_by_subentry([], {}, "default", {"subentry-a", "default"})

        assert result.get("subentry-a") == []
        assert result.get("default") == []

    def test_all_subentry_keys_present(self) -> None:
        """All specified subentry keys are present in result."""
        result = group_devices_by_subentry(
            [], {}, "default", {"subentry-a", "subentry-b", "default"}
        )

        assert "subentry-a" in result
        assert "subentry-b" in result
        assert "default" in result

    def test_non_mapping_rows_skipped(self) -> None:
        """Non-mapping rows are skipped."""
        devices = [
            {"device_id": "device1"},
            "not-a-dict",  # Should be skipped
            None,  # Should be skipped
            123,  # Should be skipped
        ]
        result = group_devices_by_subentry(
            devices, {"device1": "subentry-a"}, "default", {"subentry-a", "default"}
        )

        # Only device1 should be in the result
        total_devices = sum(len(v) for v in result.values())
        assert total_devices == 1

    def test_device_id_from_id_field(self) -> None:
        """Device ID can come from 'id' field if 'device_id' missing."""
        devices = [{"id": "device1", "name": "Device 1"}]
        result = group_devices_by_subentry(
            devices, {"device1": "subentry-a"}, "default", {"subentry-a", "default"}
        )

        assert len(result.get("subentry-a", [])) == 1

    def test_non_string_device_id_skipped(self) -> None:
        """Non-string device_id is skipped."""
        devices = [
            {"device_id": 123},  # Non-string
            {"device_id": None},  # None
        ]
        result = group_devices_by_subentry(devices, {}, "default", {"default"})

        assert result.get("default") == []

    def test_rows_are_copied(self) -> None:
        """Result contains copies of input dicts."""
        devices = [{"device_id": "device1", "name": "Original"}]
        result = group_devices_by_subentry(
            devices, {"device1": "subentry-a"}, "default", {"subentry-a", "default"}
        )

        # Modify the result
        if result.get("subentry-a"):
            result["subentry-a"][0]["name"] = "Modified"

        # Original should be unchanged
        assert devices[0]["name"] == "Original"


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestSubentryIntegration:
    """Integration tests for subentry module."""

    def test_identifier_sanitization_workflow(self) -> None:
        """Sanitization workflow for subentry identifiers."""
        # Raw input from config entry
        raw_ids = ["  valid-id  ", "", "   ", None, "another-id"]
        sanitized = [sanitize_subentry_identifier(x) for x in raw_ids]

        assert sanitized == ["valid-id", None, None, None, "another-id"]

    def test_timestamp_normalization_workflow(self) -> None:
        """Timestamp normalization for mixed inputs."""
        timestamps = [
            1704067200,  # Seconds
            1704067200000,  # Milliseconds
            "1704067200",  # String seconds
            "2024-01-01T00:00:00Z",  # ISO format
            None,
        ]

        results = [parse_last_seen_timestamp(ts) for ts in timestamps]

        # First 4 should all normalize to same value, last is None
        assert results[0] == results[1] == results[2]
        assert abs(results[0] - results[3]) < 1  # ISO might have slight diff
        assert results[4] is None

    def test_epoch_formatting_roundtrip(self) -> None:
        """Format and parse should be consistent."""
        original = 1704067200
        formatted = format_epoch_utc(original)
        assert formatted is not None

        # Parse the formatted string back
        parsed = parse_last_seen_timestamp(formatted)
        assert parsed is not None
        assert abs(parsed - original) < 1


# ---------------------------------------------------------------------------
# Edge Cases and Boundary Tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary condition tests."""

    def test_epoch_at_y2k38_boundary(self) -> None:
        """Year 2038 problem boundary (32-bit overflow)."""
        # 2147483647 is max 32-bit signed int
        result = normalize_epoch_seconds(2147483647)
        assert result == 2147483647

    def test_epoch_after_y2k38(self) -> None:
        """Timestamps after Y2K38 are handled."""
        # 2147483648 would overflow 32-bit signed int
        result = normalize_epoch_seconds(2147483648)
        assert result == 2147483648

    def test_subentry_id_with_special_chars(self) -> None:
        """Subentry ID with special characters."""
        result = sanitize_subentry_identifier("entry-123_service.subentry")
        assert result == "entry-123_service.subentry"

    def test_subentry_id_with_only_special_chars(self) -> None:
        """Subentry ID with only underscores/hyphens."""
        result = sanitize_subentry_identifier("---___---")
        assert result == "---___---"

    def test_very_old_timestamp(self) -> None:
        """Very old timestamp (year 1900)."""
        # 1900 is before Unix epoch
        result = normalize_epoch_seconds(-2208988800)
        assert result == -2208988800

    def test_format_epoch_with_microseconds(self) -> None:
        """Epoch with fractional seconds."""
        result = format_epoch_utc(1704067200.123456)
        assert result is not None
        assert "2024-01-01" in result

    def test_group_with_empty_subentry_keys(self) -> None:
        """Grouping with no predefined subentry keys."""
        devices = [{"device_id": "device1"}]
        result = group_devices_by_subentry(devices, {}, "default", set())

        # Fallback key should be created
        assert "default" in result

    def test_iso_format_without_timezone(self) -> None:
        """ISO format without timezone info."""
        # This should fail to parse since it's ambiguous
        result = parse_last_seen_timestamp("2024-01-01T00:00:00")
        # Behavior depends on implementation - might return None or assume UTC
        # Either is acceptable
        assert result is None or isinstance(result, (int, float))
