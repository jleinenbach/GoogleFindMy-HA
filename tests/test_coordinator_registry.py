"""Tests for coordinator_registry.py - Phase 4 of coordinator refactoring.

These tests validate the registry utility functions extracted from coordinator.py
into coordinator_registry.py.

Test categories:
1. extract_device_display_name - Human-friendly device name selection
2. build_legacy_device_registry_kwargs - Modern-to-legacy kwargs translation
3. needs_legacy_kwarg_retry - Determine if legacy retry is needed
4. parse_device_identifier - Parse identifier tuple with multi-account support

REQUIREMENT: 100% test coverage for all extracted functions.

KEY RISKS COVERED:
- Multi-account compatibility with namespaced identifiers (entry_id:device_id)
- Legacy kwargs compatibility between Home Assistant versions
- Malformed identifier tuples (3-tuples, empty strings, None values)
- Service device identifier filtering
"""

from __future__ import annotations

from custom_components.googlefindmy.coordinator_registry import (
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
    build_legacy_device_registry_kwargs,
    extract_device_display_name,
    needs_legacy_kwarg_retry,
    parse_device_identifier,
)

# ---------------------------------------------------------------------------
# extract_device_display_name Tests
# ---------------------------------------------------------------------------


class TestExtractDeviceDisplayName:
    """Tests for extract_device_display_name function."""

    def test_user_name_preferred(self) -> None:
        """User-set name takes priority over device name."""
        result = extract_device_display_name(
            name_by_user="My Custom Name",
            name="Default Name",
            fallback="fallback",
        )
        assert result == "My Custom Name"

    def test_device_name_when_no_user_name(self) -> None:
        """Device name used when user name is None."""
        result = extract_device_display_name(
            name_by_user=None,
            name="Device Name",
            fallback="fallback",
        )
        assert result == "Device Name"

    def test_fallback_when_no_names(self) -> None:
        """Fallback used when both names are None."""
        result = extract_device_display_name(
            name_by_user=None,
            name=None,
            fallback="Fallback Name",
        )
        assert result == "Fallback Name"

    def test_empty_string_treated_as_none(self) -> None:
        """Empty string user name falls through to device name."""
        result = extract_device_display_name(
            name_by_user="",
            name="Device Name",
            fallback="fallback",
        )
        assert result == "Device Name"

    def test_all_empty_returns_empty(self) -> None:
        """All empty strings return empty string."""
        result = extract_device_display_name(
            name_by_user="",
            name="",
            fallback="",
        )
        assert result == ""

    def test_all_none_returns_empty(self) -> None:
        """All None values return empty string."""
        result = extract_device_display_name(
            name_by_user=None,
            name=None,
            fallback=None,
        )
        assert result == ""

    def test_whitespace_stripped(self) -> None:
        """Whitespace is stripped from result."""
        result = extract_device_display_name(
            name_by_user="  My Name  ",
            name="Other",
            fallback="fallback",
        )
        assert result == "My Name"

    def test_whitespace_only_treated_as_empty(self) -> None:
        """Whitespace-only string is treated as empty after strip."""
        result = extract_device_display_name(
            name_by_user="   ",
            name="Device Name",
            fallback="fallback",
        )
        # "   " is truthy, so it's selected, then stripped to ""
        assert result == ""

    def test_priority_chain(self) -> None:
        """Verify complete priority chain."""
        # All present - user name wins
        assert extract_device_display_name("User", "Device", "Fallback") == "User"
        # No user name - device wins
        assert extract_device_display_name(None, "Device", "Fallback") == "Device"
        # No user or device - fallback wins
        assert extract_device_display_name(None, None, "Fallback") == "Fallback"
        # Empty user name - device wins
        assert extract_device_display_name("", "Device", "Fallback") == "Device"
        # Empty user and device - fallback wins
        assert extract_device_display_name("", "", "Fallback") == "Fallback"


# ---------------------------------------------------------------------------
# build_legacy_device_registry_kwargs Tests
# ---------------------------------------------------------------------------


class TestBuildLegacyDeviceRegistryKwargs:
    """Tests for build_legacy_device_registry_kwargs function.

    RISK: Home Assistant version compatibility.
    Modern HA uses add_config_entry_id/add_config_subentry_id.
    Legacy HA uses config_entry_id/config_subentry_id.
    """

    def test_add_config_entry_id_renamed(self) -> None:
        """add_config_entry_id renamed to config_entry_id."""
        kwargs = {"add_config_entry_id": "entry123", "name": "Test"}
        result = build_legacy_device_registry_kwargs(kwargs)

        assert "config_entry_id" in result
        assert result["config_entry_id"] == "entry123"
        assert "add_config_entry_id" not in result
        assert result["name"] == "Test"

    def test_add_config_subentry_id_renamed(self) -> None:
        """add_config_subentry_id renamed to config_subentry_id."""
        kwargs = {"add_config_subentry_id": "sub456"}
        result = build_legacy_device_registry_kwargs(kwargs)

        assert "config_subentry_id" in result
        assert result["config_subentry_id"] == "sub456"
        assert "add_config_subentry_id" not in result

    def test_remove_config_subentry_id_dropped(self) -> None:
        """remove_config_subentry_id is dropped entirely."""
        kwargs = {"remove_config_subentry_id": "sub789", "name": "Test"}
        result = build_legacy_device_registry_kwargs(kwargs)

        assert "remove_config_subentry_id" not in result
        assert result["name"] == "Test"

    def test_all_modern_kwargs_converted(self) -> None:
        """All modern kwargs converted in single call."""
        kwargs = {
            "add_config_entry_id": "entry1",
            "add_config_subentry_id": "sub1",
            "remove_config_subentry_id": "sub2",
            "name": "Device",
            "model": "Model1",
        }
        result = build_legacy_device_registry_kwargs(kwargs)

        assert result == {
            "config_entry_id": "entry1",
            "config_subentry_id": "sub1",
            "name": "Device",
            "model": "Model1",
        }

    def test_no_conversion_needed(self) -> None:
        """Kwargs without modern names pass through unchanged."""
        kwargs = {"name": "Test", "model": "Model", "manufacturer": "Mfg"}
        result = build_legacy_device_registry_kwargs(kwargs)

        assert result == kwargs
        assert result is not kwargs  # Should be a copy

    def test_empty_kwargs(self) -> None:
        """Empty kwargs returns empty dict."""
        result = build_legacy_device_registry_kwargs({})
        assert result == {}

    def test_original_not_modified(self) -> None:
        """Original kwargs dict is not modified."""
        kwargs = {"add_config_entry_id": "entry1", "name": "Test"}
        original_keys = set(kwargs.keys())

        build_legacy_device_registry_kwargs(kwargs)

        assert set(kwargs.keys()) == original_keys
        assert "add_config_entry_id" in kwargs

    def test_none_values_preserved(self) -> None:
        """None values in kwargs are preserved."""
        kwargs = {"add_config_entry_id": None, "name": "Test"}
        result = build_legacy_device_registry_kwargs(kwargs)

        assert result["config_entry_id"] is None
        assert result["name"] == "Test"


# ---------------------------------------------------------------------------
# needs_legacy_kwarg_retry Tests
# ---------------------------------------------------------------------------


class TestNeedsLegacyKwargRetry:
    """Tests for needs_legacy_kwarg_retry function.

    RISK: Detecting when legacy retry is needed based on TypeError messages.
    """

    def test_modern_api_no_retry(self) -> None:
        """Modern API (add_config_subentry_id supported) never needs retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name="add_config_subentry_id",
            err_str="unexpected keyword argument 'add_config_entry_id'",
            kwargs={"add_config_entry_id": "entry1"},
        )
        assert result is False

    def test_add_config_entry_id_in_error(self) -> None:
        """Error mentioning add_config_entry_id triggers retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name="config_subentry_id",
            err_str="got an unexpected keyword argument 'add_config_entry_id'",
            kwargs={"add_config_entry_id": "entry1"},
        )
        assert result is True

    def test_add_config_subentry_id_in_error(self) -> None:
        """Error mentioning add_config_subentry_id triggers retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name="config_subentry_id",
            err_str="got an unexpected keyword argument 'add_config_subentry_id'",
            kwargs={"add_config_subentry_id": "sub1"},
        )
        assert result is True

    def test_remove_config_subentry_id_in_error(self) -> None:
        """Error mentioning remove_config_subentry_id triggers retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name="config_subentry_id",
            err_str="got an unexpected keyword argument 'remove_config_subentry_id'",
            kwargs={"remove_config_subentry_id": "sub1"},
        )
        assert result is True

    def test_kwarg_in_error_but_not_in_kwargs(self) -> None:
        """Kwarg mentioned in error but not in kwargs - no retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name="config_subentry_id",
            err_str="got an unexpected keyword argument 'add_config_entry_id'",
            kwargs={"name": "Test"},  # add_config_entry_id not in kwargs
        )
        assert result is False

    def test_kwarg_in_kwargs_but_not_in_error(self) -> None:
        """Kwarg in kwargs but not mentioned in error - no retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name="config_subentry_id",
            err_str="got an unexpected keyword argument 'something_else'",
            kwargs={"add_config_entry_id": "entry1"},
        )
        assert result is False

    def test_unrelated_error(self) -> None:
        """Completely unrelated error - no retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name="config_subentry_id",
            err_str="missing required argument 'identifiers'",
            kwargs={"add_config_entry_id": "entry1"},
        )
        assert result is False

    def test_none_kwarg_name(self) -> None:
        """None kwarg_name means legacy API, check for retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name=None,
            err_str="got an unexpected keyword argument 'add_config_entry_id'",
            kwargs={"add_config_entry_id": "entry1"},
        )
        assert result is True

    def test_empty_kwargs(self) -> None:
        """Empty kwargs never need retry."""
        result = needs_legacy_kwarg_retry(
            kwarg_name="config_subentry_id",
            err_str="got an unexpected keyword argument 'add_config_entry_id'",
            kwargs={},
        )
        assert result is False


# ---------------------------------------------------------------------------
# parse_device_identifier Tests
# ---------------------------------------------------------------------------


class TestParseDeviceIdentifier:
    """Tests for parse_device_identifier function.

    RISKS:
    - Multi-account compatibility with namespaced identifiers
    - Malformed identifier tuples (3-tuples, empty strings)
    - Service device identifier filtering
    - Legacy identifier format support
    """

    def test_simple_legacy_identifier(self) -> None:
        """Simple legacy identifier (DOMAIN, device_id) is returned."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "device123"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result == "device123"

    def test_namespaced_identifier_matching_entry(self) -> None:
        """Namespaced identifier for current entry returns device_id."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "entry456:device789"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result == "device789"

    def test_namespaced_identifier_different_entry(self) -> None:
        """Namespaced identifier for different entry returns None."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "other_entry:device789"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None

    def test_wrong_domain(self) -> None:
        """Identifier with wrong domain returns None."""
        result = parse_device_identifier(
            identifier=("other_domain", "device123"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None

    def test_service_device_prefix_filtered(self) -> None:
        """Service device identifiers with prefix are filtered."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "integration_entry456"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix="integration_",
            legacy_service_id="integration",
        )
        assert result is None

    def test_legacy_service_identifier_filtered(self) -> None:
        """Legacy service identifier is filtered."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "integration"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix="integration_",
            legacy_service_id="integration",
        )
        assert result is None

    def test_three_tuple_identifier_skipped(self) -> None:
        """3-tuple identifier (e.g., from 'hon' integration) is skipped."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "device123", "extra"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None

    def test_single_element_tuple_skipped(self) -> None:
        """Single element tuple is skipped."""
        result = parse_device_identifier(
            identifier=("googlefindmy",),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None

    def test_empty_identifier_string(self) -> None:
        """Empty identifier string returns None."""
        result = parse_device_identifier(
            identifier=("googlefindmy", ""),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None

    def test_non_string_identifier(self) -> None:
        """Non-string identifier returns None."""
        result = parse_device_identifier(
            identifier=("googlefindmy", 12345),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None

    def test_none_entry_id_with_namespaced(self) -> None:
        """None entry_id cannot match namespaced identifiers."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "entry456:device789"),
            domain="googlefindmy",
            entry_id=None,
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None

    def test_none_entry_id_with_legacy(self) -> None:
        """None entry_id still accepts legacy identifiers."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "device123"),
            domain="googlefindmy",
            entry_id=None,
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result == "device123"

    def test_list_identifier_accepted(self) -> None:
        """List identifier (not tuple) is also accepted."""
        result = parse_device_identifier(
            identifier=["googlefindmy", "device123"],
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result == "device123"

    def test_colon_in_device_id(self) -> None:
        """Device ID with colon after entry prefix works correctly."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "entry456:device:with:colons"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result == "device:with:colons"

    def test_non_tuple_or_list_skipped(self) -> None:
        """Non-tuple/list identifier is skipped."""
        result = parse_device_identifier(
            identifier="not_a_tuple",
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None

    def test_none_identifier(self) -> None:
        """None identifier returns None."""
        result = parse_device_identifier(
            identifier=None,
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    """Integration tests for registry module."""

    def test_display_name_workflow(self) -> None:
        """Typical display name selection workflow."""
        # New device - only fallback
        name = extract_device_display_name(None, None, "Unknown Device")
        assert name == "Unknown Device"

        # Device registered - has default name
        name = extract_device_display_name(None, "AirTag", "Unknown Device")
        assert name == "AirTag"

        # User customized name
        name = extract_device_display_name("My Keys", "AirTag", "Unknown Device")
        assert name == "My Keys"

    def test_legacy_kwargs_workflow(self) -> None:
        """Legacy kwargs conversion workflow."""
        # Modern kwargs that would fail on old HA
        modern_kwargs = {
            "identifiers": {("googlefindmy", "device1")},
            "add_config_entry_id": "entry123",
            "add_config_subentry_id": "sub456",
            "name": "My Device",
        }

        legacy_kwargs = build_legacy_device_registry_kwargs(modern_kwargs)

        assert "add_config_entry_id" not in legacy_kwargs
        assert "add_config_subentry_id" not in legacy_kwargs
        assert legacy_kwargs["config_entry_id"] == "entry123"
        assert legacy_kwargs["config_subentry_id"] == "sub456"
        assert legacy_kwargs["name"] == "My Device"

    def test_multi_account_identifier_workflow(self) -> None:
        """Multi-account identifier parsing workflow."""
        domain = "googlefindmy"
        entry1_id = "account1_entry"
        entry2_id = "account2_entry"

        # Device from account 1
        id1 = parse_device_identifier(
            identifier=(domain, f"{entry1_id}:device_abc"),
            domain=domain,
            entry_id=entry1_id,
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert id1 == "device_abc"

        # Same device seen from account 2's perspective - should not match
        id2 = parse_device_identifier(
            identifier=(domain, f"{entry1_id}:device_abc"),
            domain=domain,
            entry_id=entry2_id,
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert id2 is None

        # Account 2's own device
        id3 = parse_device_identifier(
            identifier=(domain, f"{entry2_id}:device_xyz"),
            domain=domain,
            entry_id=entry2_id,
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert id3 == "device_xyz"

    def test_legacy_retry_decision_workflow(self) -> None:
        """Legacy retry decision workflow."""
        modern_kwargs = {"add_config_entry_id": "entry1", "name": "Test"}

        # Simulate TypeError from old HA
        err_str = "async_get_or_create() got an unexpected keyword argument 'add_config_entry_id'"

        # Check if retry needed (old API)
        should_retry = needs_legacy_kwarg_retry(
            kwarg_name="config_subentry_id",  # Old API
            err_str=err_str,
            kwargs=modern_kwargs,
        )
        assert should_retry is True

        # Convert to legacy and retry would succeed
        legacy_kwargs = build_legacy_device_registry_kwargs(modern_kwargs)
        assert "config_entry_id" in legacy_kwargs
        assert "add_config_entry_id" not in legacy_kwargs


# ---------------------------------------------------------------------------
# Edge Cases and Boundary Tests
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and boundary condition tests."""

    def test_identifier_with_only_colon(self) -> None:
        """Identifier that is just a colon."""
        result = parse_device_identifier(
            identifier=("googlefindmy", ":"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        # ":" has a colon, but entry_id doesn't match "" (before colon)
        assert result is None

    def test_identifier_starting_with_colon(self) -> None:
        """Identifier starting with colon (empty entry_id prefix)."""
        result = parse_device_identifier(
            identifier=("googlefindmy", ":device123"),
            domain="googlefindmy",
            entry_id="",  # Empty entry_id is falsy
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        # Empty string entry_id is falsy, so namespaced check fails, returns None
        assert result is None

    def test_display_name_with_newlines(self) -> None:
        """Display name with newlines gets stripped at edges."""
        result = extract_device_display_name(
            name_by_user="\nMy Name\n",
            name="Other",
            fallback="fallback",
        )
        assert result == "My Name"

    def test_display_name_with_tabs(self) -> None:
        """Display name with tabs gets stripped at edges."""
        result = extract_device_display_name(
            name_by_user="\tTabbed Name\t",
            name="Other",
            fallback="fallback",
        )
        assert result == "Tabbed Name"

    def test_legacy_kwargs_preserves_unknown_keys(self) -> None:
        """Unknown kwargs keys are preserved in legacy conversion."""
        kwargs = {
            "add_config_entry_id": "entry1",
            "custom_key": "custom_value",
            "another_key": 42,
        }
        result = build_legacy_device_registry_kwargs(kwargs)

        assert result["custom_key"] == "custom_value"
        assert result["another_key"] == 42

    def test_service_prefix_partial_match(self) -> None:
        """Service prefix must be at start, not partial match."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "device_integration_test"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix="integration_",
            legacy_service_id="integration",
        )
        # "device_integration_test" doesn't START with "integration_"
        assert result == "device_integration_test"

    def test_entry_id_with_colon(self) -> None:
        """Entry ID containing a colon - edge case with split behavior."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "entry:456:device789"),
            domain="googlefindmy",
            entry_id="entry:456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        # Matches "entry:456:" prefix, but split(":", 1) splits at first colon
        # so returns "456:device789" not "device789"
        # Real entry_ids don't contain colons, so this is an edge case
        assert result == "456:device789"

    def test_very_long_identifier(self) -> None:
        """Very long identifier is handled correctly."""
        long_id = "a" * 10000
        result = parse_device_identifier(
            identifier=("googlefindmy", f"entry456:{long_id}"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result == long_id

    def test_unicode_in_identifier(self) -> None:
        """Unicode characters in identifier are handled."""
        result = parse_device_identifier(
            identifier=("googlefindmy", "entry456:设备🎯"),
            domain="googlefindmy",
            entry_id="entry456",
            service_prefix=SERVICE_DEVICE_IDENTIFIER_PREFIX,
            legacy_service_id=LEGACY_SERVICE_IDENTIFIER,
        )
        assert result == "设备🎯"

    def test_unicode_in_display_name(self) -> None:
        """Unicode characters in display name are handled."""
        result = extract_device_display_name(
            name_by_user="我的设备 🏠",
            name="Device",
            fallback="fallback",
        )
        assert result == "我的设备 🏠"


# ---------------------------------------------------------------------------
# Phase 8: New Helper Function Tests
# ---------------------------------------------------------------------------

from unittest.mock import Mock

from custom_components.googlefindmy.coordinator_registry import (
    extract_subentry_links,
    has_hub_link,
    has_subentry_link,
    is_hub_device_check,
    normalize_device_name,
    resolve_tracker_subentry_candidate,
)


class TestNormalizeDeviceName:
    """Tests for normalize_device_name function.

    This function normalizes device names to lowercase for comparison.
    Returns None for invalid/empty inputs.
    """

    def test_string_normalized_to_lowercase(self) -> None:
        """String should be normalized to lowercase."""
        assert normalize_device_name("My Device") == "my device"
        assert normalize_device_name("UPPERCASE") == "uppercase"
        assert normalize_device_name("MiXeD CaSe") == "mixed case"

    def test_whitespace_stripped(self) -> None:
        """Whitespace should be stripped before normalization."""
        assert normalize_device_name("  Device  ") == "device"
        assert normalize_device_name("\tDevice\n") == "device"

    def test_empty_string_returns_none(self) -> None:
        """Empty string should return None."""
        assert normalize_device_name("") is None
        assert normalize_device_name("   ") is None

    def test_none_returns_none(self) -> None:
        """None input should return None."""
        assert normalize_device_name(None) is None

    def test_non_string_returns_none(self) -> None:
        """Non-string input should return None."""
        assert normalize_device_name(123) is None
        assert normalize_device_name([]) is None
        assert normalize_device_name({}) is None

    def test_unicode_normalized(self) -> None:
        """Unicode strings should be normalized."""
        assert normalize_device_name("Gerät 🏠") == "gerät 🏠"


class TestExtractSubentryLinks:
    """Tests for extract_subentry_links function.

    Extracts subentry links from a device object for a given entry_id.
    """

    def test_extracts_from_config_entries_subentries_mapping(self) -> None:
        """Should extract from config_entries_subentries mapping."""
        device = Mock()
        device.config_entries_subentries = {
            "entry1": {"subentry1", "subentry2"},
            "entry2": {"other"},
        }

        result = extract_subentry_links(device, "entry1")
        assert result == {"subentry1", "subentry2"}

    def test_returns_empty_for_none_entry_id(self) -> None:
        """Should return empty set for None entry_id."""
        device = Mock()
        device.config_entries_subentries = {"entry1": {"subentry1"}}

        result = extract_subentry_links(device, None)
        assert result == set()

    def test_returns_empty_for_missing_entry(self) -> None:
        """Should return empty set when entry not in mapping."""
        device = Mock()
        device.config_entries_subentries = {"entry1": {"subentry1"}}

        result = extract_subentry_links(device, "other_entry")
        assert result == set()

    def test_handles_none_in_links(self) -> None:
        """Should handle None values in link sets."""
        device = Mock()
        device.config_entries_subentries = {
            "entry1": {None, "subentry1"},
        }

        result = extract_subentry_links(device, "entry1")
        assert result == {None, "subentry1"}

    def test_falls_back_to_config_subentry_id(self) -> None:
        """Should fall back to config_subentry_id attribute."""
        device = Mock()
        device.config_entries_subentries = None
        device.config_subentry_id = "fallback_subentry"
        device.config_entries = {"entry1"}

        result = extract_subentry_links(device, "entry1")
        assert result == {"fallback_subentry"}

    def test_handles_config_entries_present_but_no_subentry(self) -> None:
        """Should return {None} when device has config_entries but no subentry."""
        device = Mock()
        device.config_entries_subentries = None
        device.config_subentry_id = None
        device.config_entries = {"entry1"}

        result = extract_subentry_links(device, "entry1")
        assert result == {None}

    def test_returns_empty_when_no_config_entries(self) -> None:
        """Should return empty when device has no config_entries."""
        device = Mock()
        device.config_entries_subentries = None
        device.config_subentry_id = None
        device.config_entries = None

        result = extract_subentry_links(device, "entry1")
        assert result == set()

    def test_handles_none_device(self) -> None:
        """Should handle None device."""
        result = extract_subentry_links(None, "entry1")
        assert result == set()

    def test_handles_list_instead_of_set(self) -> None:
        """Should handle list of links instead of set."""
        device = Mock()
        device.config_entries_subentries = {
            "entry1": ["subentry1", "subentry2"],
        }

        result = extract_subentry_links(device, "entry1")
        assert result == {"subentry1", "subentry2"}

    def test_filters_non_string_non_none_items(self) -> None:
        """Should filter out items that are neither string nor None."""
        device = Mock()
        device.config_entries_subentries = {
            "entry1": ["subentry1", 123, None, {"dict": "value"}, "subentry2"],
        }

        result = extract_subentry_links(device, "entry1")
        assert result == {"subentry1", "subentry2", None}

    def test_handles_non_collection_raw_links(self) -> None:
        """Should handle non-collection raw_links (not None, not Collection)."""
        device = Mock()
        device.config_entries_subentries = {
            "entry1": "string_not_collection",  # String is excluded by check
        }
        device.config_subentry_id = "fallback"
        device.config_entries = {"entry1"}

        result = extract_subentry_links(device, "entry1")
        # Falls through to fallback since string is not valid Collection
        assert result == {"fallback"}


class TestHasSubentryLink:
    """Tests for has_subentry_link function."""

    def test_returns_true_when_link_present(self) -> None:
        """Should return True when target is in links."""
        links: set[str | None] = {"sub1", "sub2", None}
        assert has_subentry_link(links, "sub1") is True

    def test_returns_false_when_link_absent(self) -> None:
        """Should return False when target is not in links."""
        links: set[str | None] = {"sub1", "sub2"}
        assert has_subentry_link(links, "sub3") is False

    def test_returns_false_for_none_target_id(self) -> None:
        """Should return False for None target_id."""
        links: set[str | None] = {"sub1", None}
        assert has_subentry_link(links, None) is False

    def test_empty_links_returns_false(self) -> None:
        """Should return False for empty links set."""
        assert has_subentry_link(set(), "sub1") is False


class TestHasHubLink:
    """Tests for has_hub_link function."""

    def test_returns_true_when_none_in_links(self) -> None:
        """Should return True when None is in links."""
        links: set[str | None] = {"sub1", None}
        assert has_hub_link(links) is True

    def test_returns_false_when_no_none(self) -> None:
        """Should return False when None not in links."""
        links: set[str | None] = {"sub1", "sub2"}
        assert has_hub_link(links) is False

    def test_empty_links_returns_false(self) -> None:
        """Should return False for empty links set."""
        assert has_hub_link(set()) is False

    def test_only_none_returns_true(self) -> None:
        """Should return True when only None in links."""
        links: set[str | None] = {None}
        assert has_hub_link(links) is True


class TestIsHubDeviceCheck:
    """Tests for is_hub_device_check function."""

    def test_returns_true_when_device_id_matches_hub(self) -> None:
        """Should return True when device_id matches hub_device_id."""
        result = is_hub_device_check(
            device_id="hub-123",
            hub_device_id="hub-123",
            identifiers=set(),
            parent_identifier=("domain", "service"),
        )
        assert result is True

    def test_returns_true_when_identifier_matches_parent(self) -> None:
        """Should return True when identifiers contain parent_identifier."""
        parent_id = ("googlefindmy", "service_device")
        result = is_hub_device_check(
            device_id="some-id",
            hub_device_id="other-id",
            identifiers={parent_id, ("other", "ident")},
            parent_identifier=parent_id,
        )
        assert result is True

    def test_returns_false_when_no_match(self) -> None:
        """Should return False when no match found."""
        result = is_hub_device_check(
            device_id="device-1",
            hub_device_id="hub-123",
            identifiers={("other", "ident")},
            parent_identifier=("googlefindmy", "service"),
        )
        assert result is False

    def test_returns_false_for_none_device_id(self) -> None:
        """Should return False for None device_id."""
        result = is_hub_device_check(
            device_id=None,
            hub_device_id="hub-123",
            identifiers=set(),
            parent_identifier=("domain", "service"),
        )
        assert result is False

    def test_returns_false_for_none_hub_device_id(self) -> None:
        """Should return False when hub_device_id is None and no identifier match."""
        result = is_hub_device_check(
            device_id="some-id",
            hub_device_id=None,
            identifiers=set(),
            parent_identifier=("domain", "service"),
        )
        assert result is False

    def test_handles_none_identifiers(self) -> None:
        """Should handle None identifiers."""
        result = is_hub_device_check(
            device_id="hub-123",
            hub_device_id="hub-123",
            identifiers=None,
            parent_identifier=("domain", "service"),
        )
        assert result is True  # Matches by device_id

    def test_rejects_string_identifiers(self) -> None:
        """Should reject string identifiers (not a collection of tuples)."""
        result = is_hub_device_check(
            device_id="some-id",
            hub_device_id="other-id",
            identifiers="not_a_set",
            parent_identifier=("domain", "service"),
        )
        assert result is False


class TestResolveTrackerSubentryCandidate:
    """Tests for resolve_tracker_subentry_candidate function."""

    def test_returns_candidate_when_matches_entry_tracker(self) -> None:
        """Should return candidate when it matches entry_tracker_id."""
        result = resolve_tracker_subentry_candidate(
            candidate="tracker-sub-1",
            entry_tracker_id="tracker-sub-1",
            tracker_subentry_ids={"tracker-sub-1"},
        )
        assert result == "tracker-sub-1"

    def test_returns_none_when_candidate_not_matches_entry_tracker(self) -> None:
        """Should return None when candidate doesn't match entry_tracker_id."""
        result = resolve_tracker_subentry_candidate(
            candidate="tracker-sub-2",
            entry_tracker_id="tracker-sub-1",
            tracker_subentry_ids={"tracker-sub-1", "tracker-sub-2"},
        )
        assert result is None

    def test_returns_candidate_when_in_tracker_ids_no_entry_tracker(self) -> None:
        """Should return candidate when in tracker_subentry_ids and no entry_tracker_id."""
        result = resolve_tracker_subentry_candidate(
            candidate="tracker-sub-1",
            entry_tracker_id=None,
            tracker_subentry_ids={"tracker-sub-1", "tracker-sub-2"},
        )
        assert result == "tracker-sub-1"

    def test_returns_none_when_not_in_tracker_ids(self) -> None:
        """Should return None when candidate not in tracker_subentry_ids."""
        result = resolve_tracker_subentry_candidate(
            candidate="other-sub",
            entry_tracker_id=None,
            tracker_subentry_ids={"tracker-sub-1"},
        )
        assert result is None

    def test_returns_candidate_when_no_tracker_ids(self) -> None:
        """Should return candidate when tracker_subentry_ids is empty."""
        result = resolve_tracker_subentry_candidate(
            candidate="any-sub",
            entry_tracker_id=None,
            tracker_subentry_ids=set(),
        )
        assert result == "any-sub"

    def test_returns_none_for_none_candidate(self) -> None:
        """Should return None for None candidate."""
        result = resolve_tracker_subentry_candidate(
            candidate=None,
            entry_tracker_id="tracker-1",
            tracker_subentry_ids={"tracker-1"},
        )
        assert result is None

    def test_returns_none_when_entry_tracker_set_but_not_in_ids(self) -> None:
        """Should return None when entry_tracker_id set but candidate not in tracker_ids."""
        result = resolve_tracker_subentry_candidate(
            candidate="tracker-sub-1",
            entry_tracker_id="tracker-sub-1",
            tracker_subentry_ids={"other-tracker"},  # candidate not in this set
        )
        assert result is None

    def test_handles_provisional_suffix(self) -> None:
        """Should handle provisional suffix in candidate."""
        result = resolve_tracker_subentry_candidate(
            candidate="tracker-provisional",
            entry_tracker_id="tracker-provisional",
            tracker_subentry_ids={"tracker-provisional"},
        )
        assert result == "tracker-provisional"


# ---------------------------------------------------------------------------
# Phase 9: Service Device Helper Functions
# ---------------------------------------------------------------------------

from custom_components.googlefindmy.coordinator_registry import (
    detect_extraneous_service_identifiers,
    determine_removal_subentry_id,
    extract_service_subentry_ids,
    has_user_defined_name,
    sanitize_entry_title,
    should_defer_service_subentry,
)


class TestSanitizeEntryTitle:
    """Tests for sanitize_entry_title function.

    This function strips and validates entry titles.
    Returns None for invalid/empty inputs.
    """

    def test_valid_string_returned(self) -> None:
        """Valid string should be returned stripped."""
        assert sanitize_entry_title("My Entry") == "My Entry"

    def test_whitespace_stripped(self) -> None:
        """Leading/trailing whitespace should be stripped."""
        assert sanitize_entry_title("  Entry Title  ") == "Entry Title"
        assert sanitize_entry_title("\t\nTitle\n\t") == "Title"

    def test_empty_string_returns_none(self) -> None:
        """Empty string should return None."""
        assert sanitize_entry_title("") is None

    def test_whitespace_only_returns_none(self) -> None:
        """Whitespace-only string should return None."""
        assert sanitize_entry_title("   ") is None
        assert sanitize_entry_title("\t\n") is None

    def test_none_returns_none(self) -> None:
        """None input should return None."""
        assert sanitize_entry_title(None) is None

    def test_non_string_returns_none(self) -> None:
        """Non-string types should return None."""
        assert sanitize_entry_title(123) is None
        assert sanitize_entry_title(["list"]) is None
        assert sanitize_entry_title({"key": "value"}) is None
        assert sanitize_entry_title(True) is None


class TestHasUserDefinedName:
    """Tests for has_user_defined_name function.

    This function checks if a user has set a custom name.
    Returns True only for non-empty stripped strings.
    """

    def test_non_empty_string_returns_true(self) -> None:
        """Non-empty string should return True."""
        assert has_user_defined_name("Custom Name") is True

    def test_whitespace_stripped_before_check(self) -> None:
        """Whitespace should be stripped before checking."""
        assert has_user_defined_name("  Name  ") is True

    def test_empty_string_returns_false(self) -> None:
        """Empty string should return False."""
        assert has_user_defined_name("") is False

    def test_whitespace_only_returns_false(self) -> None:
        """Whitespace-only string should return False."""
        assert has_user_defined_name("   ") is False
        assert has_user_defined_name("\t\n") is False

    def test_none_returns_false(self) -> None:
        """None input should return False."""
        assert has_user_defined_name(None) is False


class TestExtractServiceSubentryIds:
    """Tests for extract_service_subentry_ids function.

    Extracts service subentry IDs from entry.subentries mapping.
    """

    def test_extracts_service_type_subentries(self) -> None:
        """Should extract subentries with service type."""

        class MockSubentry:
            def __init__(self, subentry_type: str, data: dict | None = None):
                self.subentry_type = subentry_type
                self.data = data

        subentries = {
            "sub-1": MockSubentry("service"),
            "sub-2": MockSubentry("tracker"),
            "sub-3": MockSubentry("service"),
        }
        result = extract_service_subentry_ids(
            entry_subentries=subentries,
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == {"sub-1", "sub-3"}

    def test_extracts_by_group_key(self) -> None:
        """Should extract subentries with service group_key."""

        class MockSubentry:
            def __init__(self, subentry_type: str | None, data: dict | None):
                self.subentry_type = subentry_type
                self.data = data

        subentries = {
            "sub-1": MockSubentry(None, {"group_key": "_service_"}),
            "sub-2": MockSubentry(None, {"group_key": "tracker"}),
        }
        result = extract_service_subentry_ids(
            entry_subentries=subentries,
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == {"sub-1"}

    def test_skips_provisional_unless_matches_entry_id(self) -> None:
        """Should skip provisional subentries unless they match entry_service_subentry_id."""

        class MockSubentry:
            def __init__(self, subentry_type: str):
                self.subentry_type = subentry_type
                self.data = None

        subentries = {
            "sub-1-provisional": MockSubentry("service"),
            "sub-2": MockSubentry("service"),
        }
        # Without matching entry_service_subentry_id, provisional is skipped
        result = extract_service_subentry_ids(
            entry_subentries=subentries,
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == {"sub-2"}

        # With matching entry_service_subentry_id, provisional is included
        result = extract_service_subentry_ids(
            entry_subentries=subentries,
            entry_service_subentry_id="sub-1-provisional",
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == {"sub-1-provisional", "sub-2"}

    def test_handles_none_subentries(self) -> None:
        """Should return empty set for None subentries."""
        result = extract_service_subentry_ids(
            entry_subentries=None,
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == set()

    def test_handles_non_mapping_subentries(self) -> None:
        """Should return empty set for non-Mapping subentries."""
        result = extract_service_subentry_ids(
            entry_subentries="not_a_mapping",
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == set()

    def test_skips_invalid_subentry_ids(self) -> None:
        """Should skip None and non-string subentry IDs."""

        class MockSubentry:
            def __init__(self, subentry_type: str):
                self.subentry_type = subentry_type
                self.data = None

        subentries = {
            None: MockSubentry("service"),
            123: MockSubentry("service"),
            "valid": MockSubentry("service"),
        }
        result = extract_service_subentry_ids(
            entry_subentries=subentries,
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == {"valid"}

    def test_handles_subentry_without_data_attribute(self) -> None:
        """Should handle subentries without data attribute."""

        class MockSubentryNoData:
            def __init__(self, subentry_type: str):
                self.subentry_type = subentry_type

        subentries = {"sub-1": MockSubentryNoData("service")}
        result = extract_service_subentry_ids(
            entry_subentries=subentries,
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == {"sub-1"}

    def test_handles_subentry_with_non_mapping_data(self) -> None:
        """Should handle subentries with non-Mapping data."""

        class MockSubentry:
            def __init__(self, subentry_type: str | None, data):
                self.subentry_type = subentry_type
                self.data = data

        subentries = {
            "sub-1": MockSubentry(None, "not_a_mapping"),
            "sub-2": MockSubentry("service", None),
        }
        result = extract_service_subentry_ids(
            entry_subentries=subentries,
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == {"sub-2"}

    def test_empty_string_subentry_id_skipped(self) -> None:
        """Should skip empty string subentry IDs."""

        class MockSubentry:
            def __init__(self, subentry_type: str):
                self.subentry_type = subentry_type
                self.data = None

        subentries = {"": MockSubentry("service"), "valid": MockSubentry("service")}
        result = extract_service_subentry_ids(
            entry_subentries=subentries,
            entry_service_subentry_id=None,
            subentry_type_service="service",
            service_subentry_key="_service_",
        )
        assert result == {"valid"}


class TestShouldDeferServiceSubentry:
    """Tests for should_defer_service_subentry function.

    Checks if config_subentry_id should be deferred until registry catches up.
    """

    def test_returns_false_when_subentry_id_none(self) -> None:
        """Should return False when service_subentry_id is None."""
        result = should_defer_service_subentry(
            service_subentry_id=None,
            current_subentries={"sub-1": object()},
            entry_id="entry-123",
            service_subentry_key="_service_",
        )
        assert result is False

    def test_returns_false_when_subentry_in_registry(self) -> None:
        """Should return False when subentry_id is in current_subentries."""
        result = should_defer_service_subentry(
            service_subentry_id="sub-1",
            current_subentries={"sub-1": object()},
            entry_id="entry-123",
            service_subentry_key="_service_",
        )
        assert result is False

    def test_returns_true_when_subentry_not_in_registry(self) -> None:
        """Should return True when subentry_id not in current_subentries."""
        result = should_defer_service_subentry(
            service_subentry_id="sub-2",
            current_subentries={"sub-1": object()},
            entry_id="entry-123",
            service_subentry_key="_service_",
        )
        assert result is True

    def test_returns_false_for_stable_default_pattern(self) -> None:
        """Should return False for stable default subentry ID pattern."""
        result = should_defer_service_subentry(
            service_subentry_id="entry-123-_service_-subentry",
            current_subentries={"other-sub": object()},
            entry_id="entry-123",
            service_subentry_key="_service_",
        )
        assert result is False

    def test_returns_false_when_subentries_none(self) -> None:
        """Should return False when current_subentries is None."""
        result = should_defer_service_subentry(
            service_subentry_id="sub-1",
            current_subentries=None,
            entry_id="entry-123",
            service_subentry_key="_service_",
        )
        assert result is False

    def test_returns_false_when_subentries_non_mapping(self) -> None:
        """Should return False when current_subentries is not a Mapping."""
        result = should_defer_service_subentry(
            service_subentry_id="sub-1",
            current_subentries="not_a_mapping",
            entry_id="entry-123",
            service_subentry_key="_service_",
        )
        assert result is False

    def test_returns_true_when_entry_id_none(self) -> None:
        """Should return True when entry_id is None and not in registry."""
        result = should_defer_service_subentry(
            service_subentry_id="sub-1",
            current_subentries={"other": object()},
            entry_id=None,
            service_subentry_key="_service_",
        )
        assert result is True

    def test_handles_empty_subentries(self) -> None:
        """Should return True when current_subentries is empty."""
        result = should_defer_service_subentry(
            service_subentry_id="sub-1",
            current_subentries={},
            entry_id="entry-123",
            service_subentry_key="_service_",
        )
        assert result is True


class TestDetectExtraneousServiceIdentifiers:
    """Tests for detect_extraneous_service_identifiers function.

    Finds :service identifiers not in target set.
    """

    def test_finds_extraneous_service_identifiers(self) -> None:
        """Should find :service identifiers not in target set."""
        device_identifiers = {
            ("domain", "entry-1:sub-1:service"),
            ("domain", "entry-1:sub-2:service"),
            ("domain", "integration_entry-1"),
        }
        target_identifiers = {
            ("domain", "entry-1:sub-1:service"),
            ("domain", "integration_entry-1"),
        }
        result = detect_extraneous_service_identifiers(
            device_identifiers=device_identifiers,
            target_identifiers=target_identifiers,
            domain="domain",
        )
        assert result == {("domain", "entry-1:sub-2:service")}

    def test_returns_empty_when_no_extraneous(self) -> None:
        """Should return empty set when no extraneous identifiers."""
        identifiers = {
            ("domain", "entry-1:sub-1:service"),
            ("domain", "integration_entry-1"),
        }
        result = detect_extraneous_service_identifiers(
            device_identifiers=identifiers,
            target_identifiers=identifiers,
            domain="domain",
        )
        assert result == set()

    def test_ignores_non_service_identifiers(self) -> None:
        """Should ignore identifiers not ending in :service."""
        device_identifiers = {
            ("domain", "entry-1:sub-1:service"),
            ("domain", "regular-identifier"),
            ("domain", "another-id"),
        }
        target_identifiers = {("domain", "entry-1:sub-1:service")}
        result = detect_extraneous_service_identifiers(
            device_identifiers=device_identifiers,
            target_identifiers=target_identifiers,
            domain="domain",
        )
        assert result == set()

    def test_ignores_different_domain(self) -> None:
        """Should ignore identifiers from different domain."""
        device_identifiers = {
            ("domain", "entry-1:sub-1:service"),
            ("other_domain", "entry-1:sub-2:service"),
        }
        target_identifiers = {("domain", "entry-1:sub-1:service")}
        result = detect_extraneous_service_identifiers(
            device_identifiers=device_identifiers,
            target_identifiers=target_identifiers,
            domain="domain",
        )
        assert result == set()

    def test_handles_empty_device_identifiers(self) -> None:
        """Should return empty set for empty device_identifiers."""
        result = detect_extraneous_service_identifiers(
            device_identifiers=set(),
            target_identifiers={("domain", "entry-1:sub-1:service")},
            domain="domain",
        )
        assert result == set()

    def test_handles_non_tuple_identifiers(self) -> None:
        """Should skip non-tuple identifiers."""
        device_identifiers = {
            ("domain", "entry-1:sub-1:service"),
            "not_a_tuple",
        }
        target_identifiers = set()
        result = detect_extraneous_service_identifiers(
            device_identifiers=device_identifiers,
            target_identifiers=target_identifiers,
            domain="domain",
        )
        assert result == {("domain", "entry-1:sub-1:service")}

    def test_handles_wrong_length_tuple(self) -> None:
        """Should skip tuples with wrong length."""
        device_identifiers = {
            ("domain", "entry-1:sub-1:service"),
            ("domain",),
            ("domain", "a", "b"),
        }
        target_identifiers = set()
        result = detect_extraneous_service_identifiers(
            device_identifiers=device_identifiers,
            target_identifiers=target_identifiers,
            domain="domain",
        )
        assert result == {("domain", "entry-1:sub-1:service")}

    def test_handles_non_string_second_element(self) -> None:
        """Should skip tuples with non-string second element."""
        device_identifiers = {
            ("domain", "entry-1:sub-1:service"),
            ("domain", 123),
            ("domain", None),
        }
        target_identifiers = set()
        result = detect_extraneous_service_identifiers(
            device_identifiers=device_identifiers,
            target_identifiers=target_identifiers,
            domain="domain",
        )
        assert result == {("domain", "entry-1:sub-1:service")}


class TestDetermineRemovalSubentryId:
    """Tests for determine_removal_subentry_id function.

    Determines which subentry ID to remove from device.
    """

    def test_returns_first_from_service_links(self) -> None:
        """Should return first item from current_service_links."""
        result = determine_removal_subentry_id(
            current_service_links={"sub-1", "sub-2"},
            dev_config_subentry_id="other",
        )
        # Order is not guaranteed in sets, just check it's one of them
        assert result in {"sub-1", "sub-2"}

    def test_returns_config_subentry_when_no_links(self) -> None:
        """Should return dev_config_subentry_id when no service links."""
        result = determine_removal_subentry_id(
            current_service_links=set(),
            dev_config_subentry_id="config-sub",
        )
        assert result == "config-sub"

    def test_returns_stripped_config_subentry(self) -> None:
        """Should strip whitespace from dev_config_subentry_id."""
        result = determine_removal_subentry_id(
            current_service_links=set(),
            dev_config_subentry_id="  config-sub  ",
        )
        assert result == "config-sub"

    def test_returns_none_when_no_links_and_empty_config(self) -> None:
        """Should return None when no links and empty config subentry."""
        result = determine_removal_subentry_id(
            current_service_links=set(),
            dev_config_subentry_id="",
        )
        assert result is None

    def test_returns_none_when_no_links_and_none_config(self) -> None:
        """Should return None when no links and None config subentry."""
        result = determine_removal_subentry_id(
            current_service_links=set(),
            dev_config_subentry_id=None,
        )
        assert result is None

    def test_returns_none_when_no_links_and_whitespace_config(self) -> None:
        """Should return None when no links and whitespace-only config."""
        result = determine_removal_subentry_id(
            current_service_links=set(),
            dev_config_subentry_id="   ",
        )
        assert result is None

    def test_prefers_links_over_config_subentry(self) -> None:
        """Should prefer service links over config subentry ID."""
        result = determine_removal_subentry_id(
            current_service_links={"link-sub"},
            dev_config_subentry_id="config-sub",
        )
        assert result == "link-sub"
