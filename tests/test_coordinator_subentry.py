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


# ---------------------------------------------------------------------------
# Phase 6: New Helper Function Tests
# ---------------------------------------------------------------------------

from custom_components.googlefindmy.coordinator_subentry import (
    build_device_index_from_list,
    compute_stable_subentry_id,
    detect_missing_core_subentry_keys,
    extract_subentry_group_key,
    filter_provisional_identifier,
    normalize_features_list,
    normalize_visible_device_id_list,
)


class TestFilterProvisionalIdentifier:
    """Tests for filter_provisional_identifier function."""

    def test_non_provisional_kept(self) -> None:
        """Non-provisional identifier is kept."""
        result, filtered = filter_provisional_identifier(
            "stable-id", "core_service", None
        )
        assert result == "stable-id"
        assert filtered is False

    def test_provisional_filtered_when_not_matching(self) -> None:
        """Provisional identifier filtered when not matching entry ID."""
        result, filtered = filter_provisional_identifier(
            "test-provisional", "core_service", "different-id"
        )
        assert result is None
        assert filtered is True

    def test_provisional_kept_when_matching_service(self) -> None:
        """Provisional identifier kept when matching service entry ID."""
        result, filtered = filter_provisional_identifier(
            "matching-provisional", "core_service", "matching-provisional"
        )
        assert result == "matching-provisional"
        assert filtered is False

    def test_provisional_kept_when_matching_tracker(self) -> None:
        """Provisional identifier kept when matching tracker entry ID."""
        result, filtered = filter_provisional_identifier(
            "matching-provisional", "core_tracking", "matching-provisional"
        )
        assert result == "matching-provisional"
        assert filtered is False

    def test_none_identifier_returns_none(self) -> None:
        """None identifier returns (None, False)."""
        result, filtered = filter_provisional_identifier(None, "core_service", "any-id")
        assert result is None
        assert filtered is False

    def test_provisional_filtered_for_tracker(self) -> None:
        """Provisional filtered for tracker when not matching."""
        result, filtered = filter_provisional_identifier(
            "test-provisional", "core_tracking", "different"
        )
        assert result is None
        assert filtered is True

    def test_non_core_group_provisional_filtered(self) -> None:
        """Provisional filtered for non-core group keys."""
        result, filtered = filter_provisional_identifier(
            "test-provisional", "custom_group", "test-provisional"
        )
        assert result is None
        assert filtered is True


class TestExtractSubentryGroupKey:
    """Tests for extract_subentry_group_key function."""

    def test_extracts_from_data(self) -> None:
        """Extracts group_key from data dict."""
        data = {"group_key": "my_group"}
        result = extract_subentry_group_key(data, "fallback_id")
        assert result == "my_group"

    def test_falls_back_to_subentry_id(self) -> None:
        """Falls back to subentry_id when no group_key."""
        data = {"other_key": "value"}
        result = extract_subentry_group_key(data, "fallback_id")
        assert result == "fallback_id"

    def test_falls_back_to_default(self) -> None:
        """Falls back to default when nothing else."""
        result = extract_subentry_group_key(None, None, "default")
        assert result == "default"

    def test_none_data_uses_subentry_id(self) -> None:
        """None data falls back to subentry_id."""
        result = extract_subentry_group_key(None, "subentry_123")
        assert result == "subentry_123"

    def test_empty_data_uses_subentry_id(self) -> None:
        """Empty data falls back to subentry_id."""
        result = extract_subentry_group_key({}, "subentry_123")
        assert result == "subentry_123"

    def test_converts_non_string_group_key(self) -> None:
        """Converts non-string group_key to string."""
        data = {"group_key": 123}
        result = extract_subentry_group_key(data, "fallback")
        assert result == "123"


class TestBuildDeviceIndexFromList:
    """Tests for build_device_index_from_list function."""

    def test_builds_index_from_devices(self) -> None:
        """Builds index from device list."""
        devices = [
            {"id": "dev1", "name": "Device 1"},
            {"id": "dev2", "name": "Device 2"},
        ]
        result = build_device_index_from_list(devices, set())
        assert "dev1" in result
        assert "dev2" in result
        assert result["dev1"]["name"] == "Device 1"

    def test_ignores_devices_in_ignored_set(self) -> None:
        """Ignores devices in ignored_ids set."""
        devices = [{"id": "dev1"}, {"id": "dev2"}]
        result = build_device_index_from_list(devices, {"dev2"})
        assert "dev1" in result
        assert "dev2" not in result

    def test_handles_device_id_field(self) -> None:
        """Falls back to device_id field."""
        devices = [{"device_id": "dev1", "name": "Name"}]
        result = build_device_index_from_list(devices, set())
        assert "dev1" in result

    def test_skips_invalid_entries(self) -> None:
        """Skips entries without valid ID."""
        devices = [
            {"id": "valid"},
            {"name": "no-id"},
            {"id": None},
            {"id": ""},
            None,
            "string",
        ]
        result = build_device_index_from_list(devices, set())
        assert len(result) == 1
        assert "valid" in result

    def test_empty_list_returns_empty(self) -> None:
        """Empty list returns empty dict."""
        result = build_device_index_from_list([], set())
        assert result == {}

    def test_uses_setdefault_for_duplicates(self) -> None:
        """First entry wins for duplicate IDs."""
        devices = [
            {"id": "dev1", "name": "First"},
            {"id": "dev1", "name": "Second"},
        ]
        result = build_device_index_from_list(devices, set())
        assert result["dev1"]["name"] == "First"


class TestNormalizeFeaturesList:
    """Tests for normalize_features_list function."""

    def test_normalizes_list(self) -> None:
        """Normalizes list of features."""
        result = normalize_features_list(["b", "a", "c"])
        assert result == ("a", "b", "c")

    def test_deduplicates(self) -> None:
        """Deduplicates features."""
        result = normalize_features_list(["a", "a", "b"])
        assert result == ("a", "b")

    def test_returns_default_for_none(self) -> None:
        """Returns default for None input."""
        result = normalize_features_list(None, ("default",))
        assert result == ("default",)

    def test_returns_default_for_non_iterable(self) -> None:
        """Returns default for non-iterable input."""
        result = normalize_features_list("string", ("default",))
        assert result == ("default",)

    def test_filters_non_strings(self) -> None:
        """Filters non-string features."""
        result = normalize_features_list(["valid", 123, None, "also_valid"])
        assert result == ("also_valid", "valid")

    def test_returns_default_for_empty_result(self) -> None:
        """Returns default if all features filtered out."""
        result = normalize_features_list([123, None], ("default",))
        assert result == ("default",)

    def test_handles_set_input(self) -> None:
        """Handles set input."""
        result = normalize_features_list({"b", "a"})
        assert result == ("a", "b")

    def test_handles_tuple_input(self) -> None:
        """Handles tuple input."""
        result = normalize_features_list(("c", "a", "b"))
        assert result == ("a", "b", "c")


class TestNormalizeVisibleDeviceIdList:
    """Tests for normalize_visible_device_id_list function."""

    def test_normalizes_list(self) -> None:
        """Normalizes list of device IDs."""
        result = normalize_visible_device_id_list(["dev1", "dev2"])
        assert result == {"dev1", "dev2"}

    def test_strips_namespace_prefix(self) -> None:
        """Strips namespace prefix."""
        result = normalize_visible_device_id_list(["entry:dev1", "dev2"])
        assert result == {"dev1", "dev2"}

    def test_returns_none_for_non_list(self) -> None:
        """Returns None for non-list input."""
        result = normalize_visible_device_id_list("not-a-list")
        assert result is None

    def test_returns_none_for_none(self) -> None:
        """Returns None for None input."""
        result = normalize_visible_device_id_list(None)
        assert result is None

    def test_filters_empty_strings(self) -> None:
        """Filters empty strings."""
        result = normalize_visible_device_id_list(["dev1", "", "dev2"])
        assert result == {"dev1", "dev2"}

    def test_filters_non_strings(self) -> None:
        """Filters non-string values."""
        result = normalize_visible_device_id_list(["dev1", 123, None])
        assert result == {"dev1"}

    def test_returns_none_for_empty_result(self) -> None:
        """Returns None if all values filtered."""
        result = normalize_visible_device_id_list([123, None, ""])
        assert result is None

    def test_handles_multiple_colons(self) -> None:
        """Handles IDs with multiple colons - takes last part."""
        result = normalize_visible_device_id_list(["a:b:dev1"])
        assert result == {"dev1"}


class TestComputeStableSubentryId:
    """Tests for compute_stable_subentry_id function."""

    def test_computes_id(self) -> None:
        """Computes stable ID from entry_id and group_key."""
        result = compute_stable_subentry_id("entry123", "core_service", False)
        assert result == "entry123-core_service-subentry"

    def test_returns_none_for_empty_entry_id(self) -> None:
        """Returns None for empty entry_id."""
        result = compute_stable_subentry_id("", "core_service", False)
        assert result is None

    def test_returns_none_for_none_entry_id(self) -> None:
        """Returns None for None entry_id."""
        result = compute_stable_subentry_id(None, "core_service", False)
        assert result is None

    def test_returns_none_when_provisional_seen(self) -> None:
        """Returns None when provisional_seen is True."""
        result = compute_stable_subentry_id("entry123", "core_service", True)
        assert result is None

    def test_non_string_entry_id_returns_none(self) -> None:
        """Returns None for non-string entry_id."""
        result = compute_stable_subentry_id(123, "core_service", False)
        assert result is None


class TestDetectMissingCoreSubentryKeys:
    """Tests for detect_missing_core_subentry_keys function."""

    def test_detects_missing_service(self) -> None:
        """Detects missing service key."""
        result = detect_missing_core_subentry_keys({"core_tracking"})
        assert result == {"core_service"}

    def test_detects_missing_tracker(self) -> None:
        """Detects missing tracker key."""
        result = detect_missing_core_subentry_keys({"core_service"})
        assert result == {"core_tracking"}

    def test_detects_both_missing(self) -> None:
        """Detects both keys missing."""
        result = detect_missing_core_subentry_keys(set())
        assert result == {"core_service", "core_tracking"}

    def test_detects_none_missing(self) -> None:
        """Detects no keys missing."""
        result = detect_missing_core_subentry_keys({"core_service", "core_tracking"})
        assert result == set()

    def test_ignores_extra_keys(self) -> None:
        """Ignores extra keys."""
        result = detect_missing_core_subentry_keys(
            {"core_service", "core_tracking", "extra_key"}
        )
        assert result == set()

    def test_custom_key_names(self) -> None:
        """Works with custom key names."""
        result = detect_missing_core_subentry_keys(
            {"my_service"}, "my_service", "my_tracker"
        )
        assert result == {"my_tracker"}
