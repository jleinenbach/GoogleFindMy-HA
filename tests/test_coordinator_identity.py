"""Tests for coordinator_identity module.

Phase 7 pre-extraction tests with 100% coverage for all pure helper functions
to be extracted from get_active_device_identities.

Test categories per PRE_REFACTORING_TEST_PLAN.md:
- Input Validation (RISK: Crash)
- Type Handling (RISK: Incorrect coercion)
- Priority Logic (RISK: Wrong source selection)
- Timestamp Extraction (RISK: Missing dates)
"""

from __future__ import annotations

from typing import Any

# ===========================================================================
# Test: normalize_device_type
# ===========================================================================


class TestNormalizeDeviceType:
    """Tests for normalize_device_type helper.

    The function converts device type values to int, handling:
    - bool inputs (return None - bools should not be treated as int)
    - int inputs (pass through)
    - string inputs (convert if digit-only)
    - other types (return None)
    """

    def test_int_passthrough(self) -> None:
        """Int values should be returned as-is."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_device_type,
        )

        assert normalize_device_type(42) == 42
        assert normalize_device_type(0) == 0
        assert normalize_device_type(-1) == -1
        assert normalize_device_type(999) == 999

    def test_bool_returns_none(self) -> None:
        """Bool values should return None (not coerced to 0/1)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_device_type,
        )

        assert normalize_device_type(True) is None
        assert normalize_device_type(False) is None

    def test_string_digit_converted(self) -> None:
        """Digit-only strings should be converted to int."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_device_type,
        )

        assert normalize_device_type("42") == 42
        assert normalize_device_type("0") == 0
        assert normalize_device_type("999") == 999

    def test_string_non_digit_returns_none(self) -> None:
        """Non-digit strings should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_device_type,
        )

        assert normalize_device_type("abc") is None
        assert normalize_device_type("12abc") is None
        assert normalize_device_type("-1") is None  # isdigit returns False for negative
        assert normalize_device_type("1.5") is None
        assert normalize_device_type("") is None
        assert normalize_device_type(" ") is None

    def test_none_returns_none(self) -> None:
        """None input should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_device_type,
        )

        assert normalize_device_type(None) is None

    def test_other_types_return_none(self) -> None:
        """Non-int/bool/str types should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_device_type,
        )

        assert normalize_device_type(3.14) is None
        assert normalize_device_type([1, 2, 3]) is None
        assert normalize_device_type({"type": 1}) is None
        assert normalize_device_type(object()) is None


# ===========================================================================
# Test: normalize_fast_pair_model_id
# ===========================================================================


class TestNormalizeFastPairModelId:
    """Tests for normalize_fast_pair_model_id helper.

    The function normalizes Fast Pair model IDs from:
    - string inputs (strip whitespace, return None if empty)
    - bytes/bytearray inputs (decode or hex-encode)
    - other types (return None)
    """

    def test_string_passthrough_trimmed(self) -> None:
        """String values should be trimmed."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_fast_pair_model_id,
        )

        assert normalize_fast_pair_model_id("ABC123") == "ABC123"
        assert normalize_fast_pair_model_id("  ABC123  ") == "ABC123"
        assert normalize_fast_pair_model_id("\tmodel\n") == "model"

    def test_empty_string_returns_none(self) -> None:
        """Empty or whitespace-only strings should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_fast_pair_model_id,
        )

        assert normalize_fast_pair_model_id("") is None
        assert normalize_fast_pair_model_id("   ") is None
        assert normalize_fast_pair_model_id("\t\n") is None

    def test_bytes_decoded(self) -> None:
        """Bytes should be decoded to string."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_fast_pair_model_id,
        )

        assert normalize_fast_pair_model_id(b"ABC123") == "ABC123"
        assert normalize_fast_pair_model_id(b"  model  ") == "model"

    def test_bytearray_decoded(self) -> None:
        """Bytearray should be decoded to string."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_fast_pair_model_id,
        )

        assert normalize_fast_pair_model_id(bytearray(b"ABC123")) == "ABC123"

    def test_bytes_invalid_utf8_hex_encoded(self) -> None:
        """Non-UTF8 bytes should be hex-encoded."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_fast_pair_model_id,
        )

        # Invalid UTF-8 sequence
        invalid_bytes = bytes([0xFF, 0xFE, 0x00, 0x01])
        result = normalize_fast_pair_model_id(invalid_bytes)
        assert result == "fffe0001"

    def test_empty_bytes_returns_none(self) -> None:
        """Empty bytes should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_fast_pair_model_id,
        )

        assert normalize_fast_pair_model_id(b"") is None
        assert normalize_fast_pair_model_id(bytearray(b"")) is None

    def test_none_returns_none(self) -> None:
        """None input should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_fast_pair_model_id,
        )

        assert normalize_fast_pair_model_id(None) is None

    def test_other_types_return_none(self) -> None:
        """Non-string/bytes types should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_fast_pair_model_id,
        )

        assert normalize_fast_pair_model_id(123) is None
        assert normalize_fast_pair_model_id(["model"]) is None
        assert normalize_fast_pair_model_id({"id": "model"}) is None


# ===========================================================================
# Test: lookup_prio
# ===========================================================================


class TestLookupPrio:
    """Tests for lookup_prio helper.

    Priority lookup across multiple source mappings, returning first match.
    """

    def test_returns_first_match_from_first_source(self) -> None:
        """Should return value from first source that has the key."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        source1 = {"key1": "value1"}
        source2 = {"key1": "value2", "key2": "value2"}

        result = lookup_prio(("key1",), source1, source2)
        assert result == "value1"

    def test_falls_back_to_later_source(self) -> None:
        """Should fall back to later source if key not in earlier ones."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        source1 = {"other": "value1"}
        source2 = {"key1": "value2"}

        result = lookup_prio(("key1",), source1, source2)
        assert result == "value2"

    def test_multiple_lookup_keys_tried_in_order(self) -> None:
        """Should try lookup keys in order within each source."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        source = {"key2": "value2", "key1": "value1"}

        # key1 should be tried first
        result = lookup_prio(("key1", "key2"), source)
        assert result == "value1"

    def test_none_keys_filtered(self) -> None:
        """None values in lookup_keys should be filtered out."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        source = {"key1": "value1"}

        result = lookup_prio((None, "key1", None), source)
        assert result == "value1"

    def test_returns_none_when_no_match(self) -> None:
        """Should return None when no key matches in any source."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        source = {"other": "value"}

        result = lookup_prio(("key1", "key2"), source)
        assert result is None

    def test_handles_empty_sources(self) -> None:
        """Should handle empty source dicts."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        result = lookup_prio(("key1",), {}, {})
        assert result is None

    def test_handles_no_sources(self) -> None:
        """Should handle no sources provided."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        result = lookup_prio(("key1",))
        assert result is None

    def test_handles_non_mapping_sources(self) -> None:
        """Should skip non-mapping sources."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        valid_source = {"key1": "value1"}

        result = lookup_prio(("key1",), None, "not_a_dict", valid_source)  # type: ignore[arg-type]
        assert result == "value1"

    def test_preserves_value_types(self) -> None:
        """Should preserve the type of values."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
        )

        source: dict[str, Any] = {
            "int_key": 42,
            "bytes_key": b"data",
            "list_key": [1, 2, 3],
            "dict_key": {"nested": True},
        }

        assert lookup_prio(("int_key",), source) == 42
        assert lookup_prio(("bytes_key",), source) == b"data"
        assert lookup_prio(("list_key",), source) == [1, 2, 3]
        assert lookup_prio(("dict_key",), source) == {"nested": True}


# ===========================================================================
# Test: lookup_prio_with_source
# ===========================================================================


class TestLookupPrioWithSource:
    """Tests for lookup_prio_with_source helper.

    Same as lookup_prio but returns (value, source_label) tuple.
    """

    def test_returns_value_and_source_label(self) -> None:
        """Should return both value and source label."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio_with_source,
        )

        source1 = {"key1": "value1"}

        value, label = lookup_prio_with_source(
            ("key1",),
            (source1, "source1"),
        )
        assert value == "value1"
        assert label == "source1"

    def test_returns_first_non_none_value(self) -> None:
        """Should skip None values and return first non-None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio_with_source,
        )

        source1 = {"key1": None}
        source2 = {"key1": "value2"}

        value, label = lookup_prio_with_source(
            ("key1",),
            (source1, "source1"),
            (source2, "source2"),
        )
        assert value == "value2"
        assert label == "source2"

    def test_falls_back_to_later_sources(self) -> None:
        """Should fall back to later sources if key not found."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio_with_source,
        )

        source1 = {"other": "value1"}
        source2 = {"key1": "value2"}

        value, label = lookup_prio_with_source(
            ("key1",),
            (source1, "cache"),
            (source2, "live"),
        )
        assert value == "value2"
        assert label == "live"

    def test_returns_none_tuple_when_no_match(self) -> None:
        """Should return (None, None) when no match found."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio_with_source,
        )

        source = {"other": "value"}

        value, label = lookup_prio_with_source(("key1",), (source, "source"))
        assert value is None
        assert label is None

    def test_handles_non_mapping_sources(self) -> None:
        """Should skip non-mapping sources."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio_with_source,
        )

        valid_source = {"key1": "value1"}

        value, label = lookup_prio_with_source(
            ("key1",),
            (None, "null"),  # type: ignore[arg-type]
            (valid_source, "valid"),
        )
        assert value == "value1"
        assert label == "valid"

    def test_none_keys_filtered(self) -> None:
        """None values in lookup_keys should be filtered out."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio_with_source,
        )

        source = {"key1": "value1"}

        value, label = lookup_prio_with_source(
            (None, "key1", None),
            (source, "source"),
        )
        assert value == "value1"
        assert label == "source"


# ===========================================================================
# Test: store_if_value
# ===========================================================================


class TestStoreIfValue:
    """Tests for store_if_value helper.

    Stores value to dict only if value is not None.
    """

    def test_stores_non_none_value(self) -> None:
        """Should store non-None values."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            store_if_value,
        )

        target: dict[str, Any] = {}
        store_if_value(target, "key1", "value1")
        assert target == {"key1": "value1"}

    def test_skips_none_value(self) -> None:
        """Should not store None values."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            store_if_value,
        )

        target: dict[str, Any] = {"existing": "value"}
        store_if_value(target, "key1", None)
        assert target == {"existing": "value"}

    def test_stores_falsy_non_none_values(self) -> None:
        """Should store falsy values that are not None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            store_if_value,
        )

        target: dict[str, Any] = {}
        store_if_value(target, "zero", 0)
        store_if_value(target, "empty_str", "")
        store_if_value(target, "empty_list", [])
        store_if_value(target, "false", False)

        assert target == {
            "zero": 0,
            "empty_str": "",
            "empty_list": [],
            "false": False,
        }

    def test_overwrites_existing_value(self) -> None:
        """Should overwrite existing values."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            store_if_value,
        )

        target = {"key1": "old_value"}
        store_if_value(target, "key1", "new_value")
        assert target == {"key1": "new_value"}


# ===========================================================================
# Test: normalize_identity_timestamp
# ===========================================================================


class TestNormalizeIdentityTimestamp:
    """Tests for normalize_identity_timestamp helper.

    Normalizes timestamps from various formats including Timestamp-like mappings.
    Returns None for values <= 0 to prevent zero timestamps from shadowing valid values.
    """

    def test_int_epoch_seconds(self) -> None:
        """Should accept epoch seconds as int."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        assert normalize_identity_timestamp(1704067200) == 1704067200

    def test_float_epoch_seconds(self) -> None:
        """Should accept epoch seconds as float, truncate to int."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        # Float should be normalized via normalize_epoch_seconds
        result = normalize_identity_timestamp(1704067200.5)
        assert result == 1704067200

    def test_string_epoch_seconds(self) -> None:
        """Should accept epoch seconds as string."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        assert normalize_identity_timestamp("1704067200") == 1704067200

    def test_milliseconds_converted(self) -> None:
        """Should convert milliseconds to seconds."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        # 1704067200000 ms = 1704067200 s
        assert normalize_identity_timestamp(1704067200000) == 1704067200

    def test_zero_returns_none(self) -> None:
        """Zero timestamps should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        assert normalize_identity_timestamp(0) is None

    def test_negative_returns_none(self) -> None:
        """Negative timestamps should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        assert normalize_identity_timestamp(-1) is None
        assert normalize_identity_timestamp(-1704067200) is None

    def test_mapping_with_seconds_field(self) -> None:
        """Should extract seconds from Timestamp-like mapping."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        ts_mapping = {"seconds": 1704067200, "nanos": 500000000}
        assert normalize_identity_timestamp(ts_mapping) == 1704067200

    def test_mapping_with_zero_seconds_returns_none(self) -> None:
        """Mapping with zero seconds should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        ts_mapping = {"seconds": 0, "nanos": 500000000}
        assert normalize_identity_timestamp(ts_mapping) is None

    def test_mapping_nanos_only_returns_none(self) -> None:
        """Mapping with only nanos (no valid seconds) should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        ts_mapping = {"nanos": 500000000}
        assert normalize_identity_timestamp(ts_mapping) is None

        ts_mapping_nsec = {"nsec": 500000000}
        assert normalize_identity_timestamp(ts_mapping_nsec) is None

    def test_none_returns_none(self) -> None:
        """None input should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        assert normalize_identity_timestamp(None) is None

    def test_invalid_string_returns_none(self) -> None:
        """Invalid string should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        assert normalize_identity_timestamp("not_a_number") is None
        assert normalize_identity_timestamp("") is None


# ===========================================================================
# Test: extract_timestamp_from_keys
# ===========================================================================


class TestExtractTimestampFromKeys:
    """Tests for extract_timestamp_from_keys helper.

    Extracts timestamp from first matching key in payload.
    """

    def test_extracts_from_first_key(self) -> None:
        """Should extract from first matching key."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_timestamp_from_keys,
        )

        payload = {"key1": 1704067200, "key2": 1704153600}
        result = extract_timestamp_from_keys(payload, "key1", "key2")
        assert result == 1704067200

    def test_falls_back_to_later_key(self) -> None:
        """Should fall back to later key if first not present."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_timestamp_from_keys,
        )

        payload = {"key2": 1704153600}
        result = extract_timestamp_from_keys(payload, "key1", "key2")
        assert result == 1704153600

    def test_skips_invalid_timestamps(self) -> None:
        """Should skip keys with invalid timestamps."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_timestamp_from_keys,
        )

        payload = {"key1": 0, "key2": 1704153600}
        result = extract_timestamp_from_keys(payload, "key1", "key2")
        assert result == 1704153600

    def test_returns_none_when_no_valid_key(self) -> None:
        """Should return None when no valid timestamp found."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_timestamp_from_keys,
        )

        payload = {"other": 1704067200}
        result = extract_timestamp_from_keys(payload, "key1", "key2")
        assert result is None

    def test_none_payload_returns_none(self) -> None:
        """None payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_timestamp_from_keys,
        )

        result = extract_timestamp_from_keys(None, "key1")
        assert result is None

    def test_non_mapping_payload_returns_none(self) -> None:
        """Non-mapping payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_timestamp_from_keys,
        )

        result = extract_timestamp_from_keys("not_a_dict", "key1")  # type: ignore[arg-type]
        assert result is None


# ===========================================================================
# Test: extract_pair_date
# ===========================================================================


class TestExtractPairDate:
    """Tests for extract_pair_date helper.

    Extracts pair date from various payload shapes and nested structures.
    """

    def test_extracts_pair_date(self) -> None:
        """Should extract pair_date from direct key."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {"pair_date": 1704067200}
        assert extract_pair_date(payload) == 1704067200

    def test_extracts_pairDate_camelcase(self) -> None:
        """Should extract pairDate (camelCase)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {"pairDate": 1704067200}
        assert extract_pair_date(payload) == 1704067200

    def test_extracts_pair_date_unix(self) -> None:
        """Should extract pair_date_unix."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {"pair_date_unix": 1704067200}
        assert extract_pair_date(payload) == 1704067200

    def test_extracts_pairingDate(self) -> None:
        """Should extract pairingDate."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {"pairingDate": 1704067200}
        assert extract_pair_date(payload) == 1704067200

    def test_extracts_pairedAt(self) -> None:
        """Should extract pairedAt."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {"pairedAt": 1704067200}
        assert extract_pair_date(payload) == 1704067200

    def test_extracts_paired_at(self) -> None:
        """Should extract paired_at."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {"paired_at": 1704067200}
        assert extract_pair_date(payload) == 1704067200

    def test_extracts_from_device_registration(self) -> None:
        """Should extract from nested deviceRegistration."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {
            "deviceRegistration": {
                "pair_date": 1704067200,
            }
        }
        assert extract_pair_date(payload) == 1704067200

    def test_extracts_from_device_registration_snake_case(self) -> None:
        """Should extract from nested device_registration (snake_case)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {
            "device_registration": {
                "pair_date": 1704067200,
            }
        }
        assert extract_pair_date(payload) == 1704067200

    def test_extracts_from_information_nested(self) -> None:
        """Should extract from nested information dict."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {
            "information": {
                "pair_date": 1704067200,
            }
        }
        assert extract_pair_date(payload) == 1704067200

    def test_direct_takes_priority_over_nested(self) -> None:
        """Direct keys should take priority over nested."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {
            "pair_date": 1704153600,
            "deviceRegistration": {
                "pair_date": 1704067200,
            },
        }
        assert extract_pair_date(payload) == 1704153600

    def test_none_payload_returns_none(self) -> None:
        """None payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        assert extract_pair_date(None) is None

    def test_non_mapping_payload_returns_none(self) -> None:
        """Non-mapping payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        assert extract_pair_date("not_a_dict") is None  # type: ignore[arg-type]

    def test_empty_payload_returns_none(self) -> None:
        """Empty payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        assert extract_pair_date({}) is None


# ===========================================================================
# Test: extract_secrets_creation_date
# ===========================================================================


class TestExtractSecretsCreationDate:
    """Tests for extract_secrets_creation_date helper.

    Extracts creation time for encrypted secrets bundles from various payload shapes.
    """

    def test_extracts_secrets_creation_date(self) -> None:
        """Should extract secrets_creation_date."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {"secrets_creation_date": 1704067200}
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_extracts_secretsCreationDate_camelcase(self) -> None:
        """Should extract secretsCreationDate (camelCase)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {"secretsCreationDate": 1704067200}
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_extracts_creation_date(self) -> None:
        """Should extract generic creation_date."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {"creation_date": 1704067200}
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_extracts_creationDate_camelcase(self) -> None:
        """Should extract creationDate (camelCase)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {"creationDate": 1704067200}
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_extracts_from_encrypted_user_secrets(self) -> None:
        """Should extract from nested encrypted_user_secrets."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {
            "encrypted_user_secrets": {
                "creation_date": 1704067200,
            }
        }
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_extracts_from_encryptedUserSecrets_camelcase(self) -> None:
        """Should extract from nested encryptedUserSecrets (camelCase)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {
            "encryptedUserSecrets": {
                "creationDate": 1704067200,
            }
        }
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_extracts_from_device_registration(self) -> None:
        """Should extract from nested deviceRegistration."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {
            "deviceRegistration": {
                "secrets_creation_date": 1704067200,
            }
        }
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_extracts_from_device_registration_snake_case(self) -> None:
        """Should extract from nested device_registration (snake_case)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {
            "device_registration": {
                "secrets_creation_date": 1704067200,
            }
        }
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_direct_takes_priority_over_nested(self) -> None:
        """Direct keys should take priority over nested."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {
            "secrets_creation_date": 1704153600,
            "encrypted_user_secrets": {
                "creation_date": 1704067200,
            },
        }
        assert extract_secrets_creation_date(payload) == 1704153600

    def test_none_payload_returns_none(self) -> None:
        """None payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        assert extract_secrets_creation_date(None) is None

    def test_non_mapping_payload_returns_none(self) -> None:
        """Non-mapping payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        assert extract_secrets_creation_date("not_a_dict") is None  # type: ignore[arg-type]


# ===========================================================================
# Test: extract_time_anchors_debug
# ===========================================================================


class TestExtractTimeAnchorsDebug:
    """Tests for extract_time_anchors_debug helper.

    Extracts optional time anchor hints for diagnostics.
    """

    def test_extracts_time_anchors_debug(self) -> None:
        """Should extract time_anchors_debug."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        debug_data = {"hint1": 123, "hint2": 456}
        payload = {"time_anchors_debug": debug_data}
        assert extract_time_anchors_debug(payload) == debug_data

    def test_extracts_timeAnchorsDebug_camelcase(self) -> None:
        """Should extract timeAnchorsDebug (camelCase)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        debug_data = {"hint": 123}
        payload = {"timeAnchorsDebug": debug_data}
        assert extract_time_anchors_debug(payload) == debug_data

    def test_extracts_time_anchors(self) -> None:
        """Should extract time_anchors (shorter form)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        anchors = [100, 200, 300]
        payload = {"time_anchors": anchors}
        assert extract_time_anchors_debug(payload) == anchors

    def test_extracts_timeAnchors_camelcase(self) -> None:
        """Should extract timeAnchors (camelCase, shorter form)."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        anchors = {"base": 123}
        payload = {"timeAnchors": anchors}
        assert extract_time_anchors_debug(payload) == anchors

    def test_first_key_takes_priority(self) -> None:
        """First matching key should take priority."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        payload = {
            "time_anchors_debug": {"debug": True},
            "timeAnchorsDebug": {"other": True},
        }
        assert extract_time_anchors_debug(payload) == {"debug": True}

    def test_none_payload_returns_none(self) -> None:
        """None payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        assert extract_time_anchors_debug(None) is None

    def test_non_mapping_payload_returns_none(self) -> None:
        """Non-mapping payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        assert extract_time_anchors_debug("not_a_dict") is None  # type: ignore[arg-type]

    def test_empty_payload_returns_none(self) -> None:
        """Empty payload should return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        assert extract_time_anchors_debug({}) is None

    def test_preserves_any_value_type(self) -> None:
        """Should preserve any value type."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        # String
        assert extract_time_anchors_debug({"time_anchors_debug": "string"}) == "string"
        # Number
        assert extract_time_anchors_debug({"time_anchors_debug": 123}) == 123
        # None value (key present but value is None)
        assert extract_time_anchors_debug({"time_anchors_debug": None}) is None
        # List
        assert extract_time_anchors_debug({"time_anchors_debug": [1, 2]}) == [1, 2]


# ===========================================================================
# Integration Tests: Combined Functionality
# ===========================================================================


# ===========================================================================
# Additional Branch Coverage Tests
# ===========================================================================


class TestBranchCoverage:
    """Additional tests to ensure 100% branch coverage."""

    def test_normalize_identity_timestamp_mapping_with_valid_seconds(self) -> None:
        """Mapping with valid seconds should return at line 214, skipping nanos check."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        # Mapping with valid seconds - returns before checking nanos
        ts_mapping = {"seconds": 1704067200}
        assert normalize_identity_timestamp(ts_mapping) == 1704067200

    def test_normalize_identity_timestamp_mapping_no_seconds_no_nanos(self) -> None:
        """Mapping without seconds and without nanos should fall through to return None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            normalize_identity_timestamp,
        )

        # Mapping without seconds, without nanos - falls through to final return None
        ts_mapping = {"other_field": "value"}
        assert normalize_identity_timestamp(ts_mapping) is None

    def test_extract_pair_date_registration_returns_none(self) -> None:
        """Test when deviceRegistration exists but nested call returns None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        # deviceRegistration exists but has no valid pair date - nested call returns None
        payload = {
            "deviceRegistration": {
                "other_field": "value"  # No pair date fields
            }
        }
        assert extract_pair_date(payload) is None

    def test_extract_pair_date_information_returns_none(self) -> None:
        """Test when information exists but nested call returns None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        # Both deviceRegistration and information exist but have no valid dates
        payload = {
            "deviceRegistration": {"other": "value"},
            "information": {"also_other": "value"},
        }
        assert extract_pair_date(payload) is None

    def test_extract_pair_date_registration_none_falls_to_information(self) -> None:
        """Test when deviceRegistration returns None but information has value."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
        )

        payload = {
            "deviceRegistration": {"other": "value"},  # No date
            "information": {"pair_date": 1704067200},  # Has date
        }
        assert extract_pair_date(payload) == 1704067200

    def test_extract_secrets_creation_encrypted_returns_none(self) -> None:
        """Test when encrypted_user_secrets exists but returns None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        # encrypted_user_secrets exists but has no valid timestamp
        payload = {"encrypted_user_secrets": {"other_field": "value"}}
        assert extract_secrets_creation_date(payload) is None

    def test_extract_secrets_creation_registration_returns_none(self) -> None:
        """Test when deviceRegistration exists but nested call returns None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        # Both nested structures exist but return None
        payload = {
            "encrypted_user_secrets": {"other": "value"},
            "deviceRegistration": {"also_other": "value"},
        }
        assert extract_secrets_creation_date(payload) is None

    def test_extract_secrets_creation_encrypted_none_falls_to_registration(
        self,
    ) -> None:
        """Test when encrypted returns None but registration has value."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_secrets_creation_date,
        )

        payload = {
            "encrypted_user_secrets": {"other": "value"},  # No date
            "deviceRegistration": {"secrets_creation_date": 1704067200},  # Has date
        }
        assert extract_secrets_creation_date(payload) == 1704067200

    def test_extract_time_anchors_debug_not_found_in_any_key(self) -> None:
        """Test when payload has keys but none match anchor keys."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        payload = {"other_key": "value", "another_key": "other_value"}
        assert extract_time_anchors_debug(payload) is None

    def test_extract_time_anchors_debug_returns_none_for_none_value(self) -> None:
        """Test when anchor key exists but value is None - returns None."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_time_anchors_debug,
        )

        # Key exists with None value
        payload = {"time_anchors_debug": None}
        result = extract_time_anchors_debug(payload)
        # This returns the actual None value since the key exists
        assert result is None


class TestIdentityHelperIntegration:
    """Integration tests verifying combined helper functionality."""

    def test_full_payload_extraction_flow(self) -> None:
        """Test extracting all identity-related data from a full payload."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
            extract_secrets_creation_date,
            extract_time_anchors_debug,
            normalize_device_type,
            normalize_fast_pair_model_id,
        )

        payload = {
            "device_type": "2",
            "fast_pair_model_id": "ABC123",
            "pair_date": 1704067200,
            "secrets_creation_date": 1704153600,
            "time_anchors_debug": {"hint": 100},
        }

        assert normalize_device_type(payload["device_type"]) == 2
        assert normalize_fast_pair_model_id(payload["fast_pair_model_id"]) == "ABC123"
        assert extract_pair_date(payload) == 1704067200
        assert extract_secrets_creation_date(payload) == 1704153600
        assert extract_time_anchors_debug(payload) == {"hint": 100}

    def test_priority_lookup_with_multiple_sources(self) -> None:
        """Test priority lookup across cache, data, and last sources."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            lookup_prio,
            lookup_prio_with_source,
        )

        cache_dates = {"dev1": 1000}
        data_dates = {"dev1": 2000, "dev2": 2000}
        last_dates = {"dev1": 3000, "dev2": 3000, "dev3": 3000}

        # dev1 should come from cache
        assert lookup_prio(("dev1",), cache_dates, data_dates, last_dates) == 1000

        # dev2 should come from data
        assert lookup_prio(("dev2",), cache_dates, data_dates, last_dates) == 2000

        # dev3 should come from last
        assert lookup_prio(("dev3",), cache_dates, data_dates, last_dates) == 3000

        # With source labels
        value, label = lookup_prio_with_source(
            ("dev1",),
            (cache_dates, "cache"),
            (data_dates, "live"),
            (last_dates, "last"),
        )
        assert value == 1000
        assert label == "cache"

    def test_nested_registration_extraction(self) -> None:
        """Test extracting dates from nested device registration structures."""
        from custom_components.googlefindmy.coordinator.helpers.identity import (
            extract_pair_date,
            extract_secrets_creation_date,
        )

        payload = {
            "deviceRegistration": {
                "pair_date": 1704067200,
                "encrypted_user_secrets": {
                    "creation_date": 1704153600,
                },
            }
        }

        assert extract_pair_date(payload) == 1704067200
        assert extract_secrets_creation_date(payload) == 1704153600
