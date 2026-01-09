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

from typing import Any

import pytest

from custom_components.googlefindmy.coordinator_registry import (
    build_legacy_device_registry_kwargs,
    extract_device_display_name,
    needs_legacy_kwarg_retry,
    parse_device_identifier,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
    LEGACY_SERVICE_IDENTIFIER,
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
