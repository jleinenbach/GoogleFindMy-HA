"""Tests for coordinator_registry.py Phase 12 entity lookup functions.

These tests validate the entity lookup helper functions extracted from
_find_tracker_entity_entry into coordinator_registry.py.

Test categories:
1. extract_canonical_device_id - Extract device ID from registry identifiers
2. build_entity_unique_id_candidates - Generate unique_id lookup candidates
3. build_canonical_unique_id - Build canonical unique_id format
4. is_legacy_unique_id - Detect legacy unique_id formats
5. match_entity_by_device_id - Match entity registry entry by device ID

REQUIREMENT: 100% test coverage for all extracted functions.

KEY RISKS COVERED:
- ID extraction from various identifier formats
- Legacy unique_id detection and patterns
- Multi-account scoping with entry_id
- Malformed or missing identifiers
"""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.coordinator_registry import (
    build_canonical_unique_id,
    build_entity_unique_id_candidates,
    extract_canonical_device_id,
    is_legacy_unique_id,
    match_entity_by_device_id,
)

# ---------------------------------------------------------------------------
# extract_canonical_device_id Tests
# ---------------------------------------------------------------------------


class TestExtractCanonicalDeviceId:
    """Tests for extract_canonical_device_id function.

    Extracts canonical device ID from device registry identifiers.
    """

    def test_extracts_simple_device_id(self) -> None:
        """Should extract device ID from simple identifier."""
        identifiers = {("googlefindmy", "device-123")}
        result = extract_canonical_device_id(identifiers, "googlefindmy")
        assert result == "device-123"

    def test_extracts_namespaced_device_id(self) -> None:
        """Should extract device ID from namespaced identifier."""
        identifiers = {("googlefindmy", "entry123:device-456")}
        result = extract_canonical_device_id(
            identifiers, "googlefindmy", entry_id="entry123"
        )
        assert result == "device-456"

    def test_ignores_wrong_domain(self) -> None:
        """Should ignore identifiers from wrong domain."""
        identifiers = {("other_domain", "device-123")}
        result = extract_canonical_device_id(identifiers, "googlefindmy")
        assert result is None

    def test_ignores_service_device_prefix(self) -> None:
        """Should ignore service device identifiers."""
        identifiers = {("googlefindmy", "integration_service_device")}
        result = extract_canonical_device_id(
            identifiers, "googlefindmy", service_prefix="integration_"
        )
        assert result is None

    def test_ignores_legacy_service_identifier(self) -> None:
        """Should ignore legacy service identifiers."""
        identifiers = {("googlefindmy", "legacy_service")}
        result = extract_canonical_device_id(
            identifiers, "googlefindmy", legacy_service_id="legacy_service"
        )
        assert result is None

    def test_handles_multiple_identifiers(self) -> None:
        """Should find device ID among multiple identifiers."""
        identifiers = {
            ("other_domain", "other-id"),
            ("googlefindmy", "device-123"),
        }
        result = extract_canonical_device_id(identifiers, "googlefindmy")
        assert result == "device-123"

    def test_prefers_namespaced_over_simple(self) -> None:
        """Should prefer namespaced identifier when entry_id matches."""
        identifiers = {
            ("googlefindmy", "simple-id"),
            ("googlefindmy", "entry123:namespaced-id"),
        }
        result = extract_canonical_device_id(
            identifiers, "googlefindmy", entry_id="entry123"
        )
        assert result == "namespaced-id"

    def test_ignores_namespaced_wrong_entry(self) -> None:
        """Should ignore namespaced identifier from different entry."""
        identifiers = {("googlefindmy", "other_entry:device-123")}
        result = extract_canonical_device_id(
            identifiers, "googlefindmy", entry_id="my_entry"
        )
        assert result is None

    def test_handles_empty_identifiers(self) -> None:
        """Should return None for empty identifiers."""
        result = extract_canonical_device_id(set(), "googlefindmy")
        assert result is None

    def test_handles_none_identifiers(self) -> None:
        """Should return None for None input."""
        result = extract_canonical_device_id(None, "googlefindmy")
        assert result is None

    def test_handles_malformed_identifier_tuple(self) -> None:
        """Should ignore malformed identifier tuples."""
        identifiers = {("googlefindmy",), ("googlefindmy", "a", "b")}
        result = extract_canonical_device_id(identifiers, "googlefindmy")
        assert result is None

    def test_handles_non_string_identifier_value(self) -> None:
        """Should ignore non-string identifier values."""
        identifiers: set[Any] = {("googlefindmy", 123)}
        result = extract_canonical_device_id(identifiers, "googlefindmy")
        assert result is None

    def test_handles_empty_string_identifier(self) -> None:
        """Should ignore empty string identifier."""
        identifiers = {("googlefindmy", "")}
        result = extract_canonical_device_id(identifiers, "googlefindmy")
        assert result is None

    def test_extracts_first_valid_when_no_entry_id(self) -> None:
        """Should extract any valid device ID when no entry_id filter."""
        identifiers = {("googlefindmy", "device-abc")}
        result = extract_canonical_device_id(identifiers, "googlefindmy")
        assert result == "device-abc"


# ---------------------------------------------------------------------------
# build_entity_unique_id_candidates Tests
# ---------------------------------------------------------------------------


class TestBuildEntityUniqueIdCandidates:
    """Tests for build_entity_unique_id_candidates function.

    Generates list of unique_id candidates for entity lookup.
    """

    def test_generates_canonical_format(self) -> None:
        """Should include canonical unique_id format."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id="entry456",
            subentry_identifier="tracker",
            domain="googlefindmy",
        )
        assert "entry456:tracker:device-123" in candidates

    def test_generates_legacy_domain_prefix(self) -> None:
        """Should include legacy domain prefix format."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id="entry456",
            subentry_identifier="tracker",
            domain="googlefindmy",
        )
        assert "googlefindmy_device-123" in candidates

    def test_generates_entry_device_format(self) -> None:
        """Should include entry:device format."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id="entry456",
            subentry_identifier="tracker",
            domain="googlefindmy",
        )
        assert "entry456:device-123" in candidates

    def test_generates_domain_entry_device_format(self) -> None:
        """Should include domain_entry_device format."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id="entry456",
            subentry_identifier="tracker",
            domain="googlefindmy",
        )
        assert "googlefindmy_entry456_device-123" in candidates

    def test_handles_none_entry_id(self) -> None:
        """Should handle None entry_id gracefully."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id=None,
            subentry_identifier="tracker",
            domain="googlefindmy",
        )
        assert "googlefindmy_device-123" in candidates
        # Should not contain entry-based formats
        assert not any(":" in c for c in candidates if c != "googlefindmy_device-123")

    def test_handles_none_subentry_identifier(self) -> None:
        """Should handle None subentry_identifier."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id="entry456",
            subentry_identifier=None,
            domain="googlefindmy",
        )
        assert "entry456:device-123" in candidates

    def test_returns_unique_candidates(self) -> None:
        """Should not return duplicates."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id="entry456",
            subentry_identifier="tracker",
            domain="googlefindmy",
        )
        assert len(candidates) == len(set(candidates))

    def test_order_prioritizes_canonical(self) -> None:
        """Should prioritize canonical format first."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id="entry456",
            subentry_identifier="tracker",
            domain="googlefindmy",
        )
        assert candidates[0] == "entry456:tracker:device-123"

    def test_handles_empty_device_id(self) -> None:
        """Should handle empty device_id."""
        candidates = build_entity_unique_id_candidates(
            device_id="",
            entry_id="entry456",
            subentry_identifier="tracker",
            domain="googlefindmy",
        )
        # Should return empty or minimal list
        assert all(c == "" or not c.endswith(":") for c in candidates)

    def test_generates_subentry_key_variant(self) -> None:
        """Should include subentry key variant when different from identifier."""
        candidates = build_entity_unique_id_candidates(
            device_id="device-123",
            entry_id="entry456",
            subentry_identifier="stable-id",
            domain="googlefindmy",
            subentry_key="tracker",
        )
        assert "entry456:tracker:device-123" in candidates
        assert "entry456:stable-id:device-123" in candidates


# ---------------------------------------------------------------------------
# build_canonical_unique_id Tests
# ---------------------------------------------------------------------------


class TestBuildCanonicalUniqueId:
    """Tests for build_canonical_unique_id function.

    Builds canonical unique_id from components.
    """

    def test_builds_full_canonical_id(self) -> None:
        """Should build full entry:subentry:device format."""
        result = build_canonical_unique_id(
            entry_id="entry123",
            subentry_identifier="tracker",
            device_id="device-456",
        )
        assert result == "entry123:tracker:device-456"

    def test_handles_none_entry_id(self) -> None:
        """Should return None when entry_id is None."""
        result = build_canonical_unique_id(
            entry_id=None,
            subentry_identifier="tracker",
            device_id="device-456",
        )
        assert result is None

    def test_handles_empty_entry_id(self) -> None:
        """Should return None when entry_id is empty."""
        result = build_canonical_unique_id(
            entry_id="",
            subentry_identifier="tracker",
            device_id="device-456",
        )
        assert result is None

    def test_handles_none_subentry(self) -> None:
        """Should skip subentry when None."""
        result = build_canonical_unique_id(
            entry_id="entry123",
            subentry_identifier=None,
            device_id="device-456",
        )
        assert result == "entry123:device-456"

    def test_handles_empty_subentry(self) -> None:
        """Should skip subentry when empty string."""
        result = build_canonical_unique_id(
            entry_id="entry123",
            subentry_identifier="",
            device_id="device-456",
        )
        assert result == "entry123:device-456"

    def test_handles_whitespace_subentry(self) -> None:
        """Should skip subentry when whitespace only."""
        result = build_canonical_unique_id(
            entry_id="entry123",
            subentry_identifier="   ",
            device_id="device-456",
        )
        assert result == "entry123:device-456"

    def test_handles_none_device_id(self) -> None:
        """Should return None when device_id is None."""
        result = build_canonical_unique_id(
            entry_id="entry123",
            subentry_identifier="tracker",
            device_id=None,
        )
        assert result is None

    def test_handles_empty_device_id(self) -> None:
        """Should return None when device_id is empty."""
        result = build_canonical_unique_id(
            entry_id="entry123",
            subentry_identifier="tracker",
            device_id="",
        )
        assert result is None

    def test_strips_whitespace(self) -> None:
        """Should strip whitespace from all parts."""
        result = build_canonical_unique_id(
            entry_id="  entry123  ",
            subentry_identifier="  tracker  ",
            device_id="  device-456  ",
        )
        assert result == "entry123:tracker:device-456"

    def test_all_parts_none(self) -> None:
        """Should return None when all parts are None."""
        result = build_canonical_unique_id(
            entry_id=None,
            subentry_identifier=None,
            device_id=None,
        )
        assert result is None


# ---------------------------------------------------------------------------
# is_legacy_unique_id Tests
# ---------------------------------------------------------------------------


class TestIsLegacyUniqueId:
    """Tests for is_legacy_unique_id function.

    Detects if a unique_id is in legacy format.
    """

    def test_detects_domain_prefix_format(self) -> None:
        """Should detect domain_device format as legacy."""
        assert is_legacy_unique_id("googlefindmy_device-123", "googlefindmy") is True

    def test_detects_domain_entry_device_format(self) -> None:
        """Should detect domain_entry_device format as legacy."""
        assert (
            is_legacy_unique_id("googlefindmy_entry123_device-456", "googlefindmy")
            is True
        )

    def test_canonical_not_legacy(self) -> None:
        """Should not detect canonical format as legacy."""
        assert (
            is_legacy_unique_id("entry123:tracker:device-456", "googlefindmy") is False
        )

    def test_entry_device_not_legacy(self) -> None:
        """Should not detect entry:device format as legacy."""
        assert is_legacy_unique_id("entry123:device-456", "googlefindmy") is False

    def test_handles_none_unique_id(self) -> None:
        """Should return False for None unique_id."""
        assert is_legacy_unique_id(None, "googlefindmy") is False

    def test_handles_empty_unique_id(self) -> None:
        """Should return False for empty unique_id."""
        assert is_legacy_unique_id("", "googlefindmy") is False

    def test_handles_non_string_unique_id(self) -> None:
        """Should return False for non-string unique_id."""
        assert is_legacy_unique_id(123, "googlefindmy") is False

    def test_different_domain_not_legacy(self) -> None:
        """Should not detect as legacy if domain doesn't match."""
        assert is_legacy_unique_id("otherdomain_device-123", "googlefindmy") is False

    def test_just_domain_not_legacy(self) -> None:
        """Should not detect just domain prefix as legacy."""
        assert is_legacy_unique_id("googlefindmy_", "googlefindmy") is False

    def test_case_sensitive_domain(self) -> None:
        """Should be case-sensitive for domain matching."""
        assert is_legacy_unique_id("GOOGLEFINDMY_device-123", "googlefindmy") is False


# ---------------------------------------------------------------------------
# match_entity_by_device_id Tests
# ---------------------------------------------------------------------------


class TestMatchEntityByDeviceId:
    """Tests for match_entity_by_device_id function.

    Checks if an entity registry entry matches device criteria.
    """

    def test_matches_when_device_id_in_unique_id(self) -> None:
        """Should match when device_id is contained in unique_id."""
        result = match_entity_by_device_id(
            unique_id="entry:tracker:device-123",
            config_entry_id="entry",
            device_id="device-123",
            target_entry_id="entry",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="googlefindmy",
        )
        assert result is True

    def test_no_match_wrong_domain(self) -> None:
        """Should not match when domain differs."""
        result = match_entity_by_device_id(
            unique_id="entry:tracker:device-123",
            config_entry_id="entry",
            device_id="device-123",
            target_entry_id="entry",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="sensor",
            entity_platform="googlefindmy",
        )
        assert result is False

    def test_no_match_wrong_platform(self) -> None:
        """Should not match when platform differs."""
        result = match_entity_by_device_id(
            unique_id="entry:tracker:device-123",
            config_entry_id="entry",
            device_id="device-123",
            target_entry_id="entry",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="other_platform",
        )
        assert result is False

    def test_no_match_wrong_entry(self) -> None:
        """Should not match when config_entry_id differs."""
        result = match_entity_by_device_id(
            unique_id="entry:tracker:device-123",
            config_entry_id="other_entry",
            device_id="device-123",
            target_entry_id="entry",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="googlefindmy",
        )
        assert result is False

    def test_no_match_device_id_not_in_unique_id(self) -> None:
        """Should not match when device_id not in unique_id."""
        result = match_entity_by_device_id(
            unique_id="entry:tracker:other-device",
            config_entry_id="entry",
            device_id="device-123",
            target_entry_id="entry",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="googlefindmy",
        )
        assert result is False

    def test_handles_none_unique_id(self) -> None:
        """Should return False for None unique_id."""
        result = match_entity_by_device_id(
            unique_id=None,
            config_entry_id="entry",
            device_id="device-123",
            target_entry_id="entry",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="googlefindmy",
        )
        assert result is False

    def test_handles_none_config_entry_id(self) -> None:
        """Should match when entity has no config_entry_id."""
        result = match_entity_by_device_id(
            unique_id="entry:tracker:device-123",
            config_entry_id=None,
            device_id="device-123",
            target_entry_id="entry",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="googlefindmy",
        )
        # None config_entry_id means no filtering by entry
        assert result is True

    def test_handles_none_target_entry_id(self) -> None:
        """Should match when target_entry_id is None (no filtering)."""
        result = match_entity_by_device_id(
            unique_id="entry:tracker:device-123",
            config_entry_id="any_entry",
            device_id="device-123",
            target_entry_id=None,
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="googlefindmy",
        )
        assert result is True

    def test_partial_device_id_match(self) -> None:
        """Should match partial device ID in unique_id."""
        result = match_entity_by_device_id(
            unique_id="googlefindmy_entry123_device-123",
            config_entry_id="entry123",
            device_id="device-123",
            target_entry_id="entry123",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="googlefindmy",
        )
        assert result is True

    def test_handles_empty_device_id(self) -> None:
        """Should return False for empty device_id."""
        result = match_entity_by_device_id(
            unique_id="entry:tracker:device-123",
            config_entry_id="entry",
            device_id="",
            target_entry_id="entry",
            domain="device_tracker",
            platform="googlefindmy",
            entity_domain="device_tracker",
            entity_platform="googlefindmy",
        )
        assert result is False
