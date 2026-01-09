"""Registry utilities for the coordinator.

This module contains pure functions for device registry operations extracted from
coordinator.py for improved testability and maintainability (Phase 4).

Contents:
- extract_device_display_name(): Get human-friendly device name
- build_legacy_device_registry_kwargs(): Translate modern kwargs to legacy
- needs_legacy_kwarg_retry(): Check if legacy retry is needed
- parse_device_identifier(): Parse identifier tuple with multi-account support
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any

# Re-export constants used by this module's functions
from .const import (
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
)

__all__ = [
    "LEGACY_SERVICE_IDENTIFIER",
    "SERVICE_DEVICE_IDENTIFIER_PREFIX",
    "build_canonical_unique_id",
    "build_entity_unique_id_candidates",
    "build_legacy_device_registry_kwargs",
    "detect_extraneous_service_identifiers",
    "determine_removal_subentry_id",
    "extract_canonical_device_id",
    "extract_device_display_name",
    "extract_service_subentry_ids",
    "extract_subentry_links",
    "has_hub_link",
    "has_subentry_link",
    "has_user_defined_name",
    "is_hub_device_check",
    "is_legacy_unique_id",
    "match_entity_by_device_id",
    "needs_legacy_kwarg_retry",
    "normalize_device_name",
    "parse_device_identifier",
    "resolve_tracker_subentry_candidate",
    "sanitize_entry_title",
    "should_defer_service_subentry",
]


# ---------------------------------------------------------------------------
# Device Display Name
# ---------------------------------------------------------------------------


def extract_device_display_name(
    name_by_user: str | None,
    name: str | None,
    fallback: str | None,
) -> str:
    """Return the best human-friendly device name without sensitive data.

    Priority order:
    1. User-set name (name_by_user)
    2. Device name (name)
    3. Fallback

    Args:
        name_by_user: User-customized name from device registry.
        name: Default device name from device registry.
        fallback: Fallback name if others are unavailable.

    Returns:
        The best available name, stripped of leading/trailing whitespace.
        Returns empty string if all inputs are None/empty.
    """
    return (name_by_user or name or fallback or "").strip()


# ---------------------------------------------------------------------------
# Legacy Device Registry Kwargs
# ---------------------------------------------------------------------------


def build_legacy_device_registry_kwargs(
    kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate modern device-registry kwargs to their legacy names.

    Home Assistant 2025.11+ uses new keyword argument names for device registry:
    - add_config_entry_id -> config_entry_id
    - add_config_subentry_id -> config_subentry_id
    - remove_config_subentry_id -> (dropped, not supported in legacy)

    Args:
        kwargs: Modern keyword arguments for device registry calls.

    Returns:
        A new dict with legacy keyword argument names.
        The original dict is not modified.
    """
    legacy_kwargs = dict(kwargs)

    if "add_config_entry_id" in legacy_kwargs:
        legacy_kwargs["config_entry_id"] = legacy_kwargs.pop("add_config_entry_id")

    if "add_config_subentry_id" in legacy_kwargs:
        legacy_kwargs["config_subentry_id"] = legacy_kwargs.pop(
            "add_config_subentry_id"
        )

    if "remove_config_subentry_id" in legacy_kwargs:
        legacy_kwargs.pop("remove_config_subentry_id")

    return legacy_kwargs


# ---------------------------------------------------------------------------
# Legacy Retry Detection
# ---------------------------------------------------------------------------


def needs_legacy_kwarg_retry(
    kwarg_name: str | None,
    err_str: str,
    kwargs: Mapping[str, Any],
) -> bool:
    """Determine if a TypeError requires legacy kwargs retry.

    When calling device registry APIs, modern Home Assistant versions accept
    new keyword names (add_config_entry_id, add_config_subentry_id). Older
    versions will raise TypeError for these unknown keywords.

    Args:
        kwarg_name: The config_subentry kwarg name supported by the API.
                    If "add_config_subentry_id", the API is modern and no retry needed.
        err_str: The string representation of the TypeError.
        kwargs: The keyword arguments that caused the error.

    Returns:
        True if the error indicates a legacy API that needs kwargs rewriting.
        False if the error is unrelated or the API is modern.
    """
    # Modern registries accept the renamed "add_config_subentry_id" keyword
    # and should surface the original TypeError to callers. Only older
    # versions that reject the new keyword should trigger a legacy rewrite.
    if kwarg_name == "add_config_subentry_id":
        return False

    # Check if each modern kwarg appears in both the error and our kwargs
    if "add_config_entry_id" in kwargs and "add_config_entry_id" in err_str:
        return True

    if "add_config_subentry_id" in kwargs and "add_config_subentry_id" in err_str:
        return True

    if "remove_config_subentry_id" in kwargs and "remove_config_subentry_id" in err_str:
        return True

    return False


# ---------------------------------------------------------------------------
# Device Identifier Parsing
# ---------------------------------------------------------------------------


def parse_device_identifier(
    identifier: Any,
    domain: str,
    entry_id: str | None,
    service_prefix: str,
    legacy_service_id: str,
) -> str | None:
    """Parse a device identifier tuple and extract the device ID.

    Multi-account compatibility:
    - Since 2025.5+ we use **entry-scoped device identifiers** in the Device Registry
      to guarantee global uniqueness across multiple accounts:
          (DOMAIN, f"{entry_id}:{device_id}")
    - For backward compatibility we also recognize legacy identifiers:
          (DOMAIN, device_id)

    Args:
        identifier: A (domain, identifier) tuple or list from device registry.
        domain: The integration domain to match (e.g., "googlefindmy").
        entry_id: The current config entry ID for namespaced matching.
        service_prefix: Prefix for service device identifiers to filter out.
        legacy_service_id: Legacy service device identifier to filter out.

    Returns:
        The canonical device_id if the identifier belongs to this entry.
        None if the identifier doesn't match, is malformed, or is a service device.
    """
    # Robust check: strict unpacking causes crashes with 3-tuple identifiers
    if not isinstance(identifier, (tuple, list)) or len(identifier) != 2:
        return None

    ident_domain, ident = identifier

    # Must be our domain with a non-empty string identifier
    if ident_domain != domain or not isinstance(ident, str) or not ident:
        return None

    # Handle namespaced format "<entry_id>:<device_id>"
    if ":" in ident:
        if entry_id and ident.startswith(entry_id + ":"):
            return ident.split(":", 1)[1]  # return canonical device_id
        # Identifier belongs to a different entry; ignore.
        return None

    # Skip service device identifiers
    if ident.startswith(service_prefix) or ident == legacy_service_id:
        return None

    # Legacy format -> accept as-is
    return ident


# ---------------------------------------------------------------------------
# Phase 8: Device Name Normalization
# ---------------------------------------------------------------------------


def normalize_device_name(name: Any) -> str | None:
    """Normalize device name to lowercase for comparison.

    Strips whitespace and converts to lowercase (casefold).

    Args:
        name: Device name (any type).

    Returns:
        Normalized lowercase string, or None if invalid/empty.
    """
    if not isinstance(name, str):
        return None
    stripped = name.strip()
    if not stripped:
        return None
    return stripped.casefold()


# ---------------------------------------------------------------------------
# Phase 8: Subentry Links Extraction
# ---------------------------------------------------------------------------


def extract_subentry_links(device: Any, entry_id: str | None) -> set[str | None]:
    """Extract subentry links from a device object for a given entry_id.

    Checks config_entries_subentries mapping first, falls back to
    config_subentry_id attribute.

    Args:
        device: Device registry entry object.
        entry_id: The config entry ID to look up.

    Returns:
        Set of subentry link strings (may include None for hub links).
    """
    if device is None or not entry_id:
        return set()

    # Try config_entries_subentries mapping first
    mapping_obj = getattr(device, "config_entries_subentries", None)
    if isinstance(mapping_obj, Mapping):
        raw_links = mapping_obj.get(entry_id)
        if isinstance(raw_links, Collection) and not isinstance(
            raw_links, (str, bytes, Mapping)
        ):
            typed_links: set[str | None] = set()
            for item in raw_links:
                if item is None:
                    typed_links.add(None)
                elif isinstance(item, str):
                    typed_links.add(item)
            return typed_links
        if raw_links is None:
            return set()

    # Fallback to config_subentry_id attribute
    fallback = getattr(device, "config_subentry_id", None)
    if isinstance(fallback, str):
        return {fallback}

    # If device has config_entries but no subentry, return {None}
    config_entries = getattr(device, "config_entries", None)
    if config_entries is not None and fallback is None:
        return {None}

    return set()


# ---------------------------------------------------------------------------
# Phase 8: Subentry Link Checks
# ---------------------------------------------------------------------------


def has_subentry_link(links: set[str | None], target_id: str | None) -> bool:
    """Check if target subentry ID is in links set.

    Args:
        links: Set of subentry links (may include None).
        target_id: Target subentry ID to check.

    Returns:
        True if target_id is in links and target_id is not None.
    """
    if target_id is None:
        return False
    return target_id in links


def has_hub_link(links: set[str | None]) -> bool:
    """Check if device has a hub link (None in links set).

    Hub links are represented by None in the subentry links set,
    indicating the device is linked to the config entry without
    a specific subentry.

    Args:
        links: Set of subentry links.

    Returns:
        True if None is in links.
    """
    return None in links


# ---------------------------------------------------------------------------
# Phase 8: Hub Device Detection
# ---------------------------------------------------------------------------


def is_hub_device_check(
    device_id: str | None,
    hub_device_id: str | None,
    identifiers: Any,
    parent_identifier: tuple[str, str],
) -> bool:
    """Check if a device is the hub/service anchor device.

    A device is considered the hub if:
    1. Its ID matches the hub_device_id, OR
    2. Its identifiers contain the parent_identifier

    Args:
        device_id: The device's ID.
        hub_device_id: The known hub device ID.
        identifiers: The device's identifiers (set of tuples).
        parent_identifier: The parent/service device identifier tuple.

    Returns:
        True if the device is the hub/service device.
    """
    # Check by device ID match
    if (
        hub_device_id is not None
        and device_id is not None
        and device_id == hub_device_id
    ):
        return True

    # Check by identifier match
    if isinstance(identifiers, Collection) and not isinstance(
        identifiers, (str, bytes, Mapping)
    ):
        return parent_identifier in identifiers

    return False


# ---------------------------------------------------------------------------
# Phase 8: Tracker Subentry Resolution
# ---------------------------------------------------------------------------


def resolve_tracker_subentry_candidate(
    candidate: str | None,
    entry_tracker_id: str | None,
    tracker_subentry_ids: set[str],
) -> str | None:
    """Resolve a tracker subentry candidate to a valid subentry ID.

    Resolution rules:
    1. If candidate is None, return None
    2. If entry_tracker_id is set:
       - candidate must equal entry_tracker_id
       - if tracker_subentry_ids is non-empty, candidate must be in it
    3. If entry_tracker_id is None:
       - if tracker_subentry_ids is non-empty, candidate must be in it
       - if tracker_subentry_ids is empty, accept candidate as-is

    Args:
        candidate: The candidate subentry ID to validate.
        entry_tracker_id: The entry's tracker subentry ID (if set).
        tracker_subentry_ids: Set of valid tracker subentry IDs.

    Returns:
        The validated subentry ID, or None if validation fails.
    """
    if candidate is None:
        return None

    if entry_tracker_id is not None:
        # Must match entry_tracker_id
        if candidate != entry_tracker_id:
            return None
        # If tracker_subentry_ids exists, must be in it
        if tracker_subentry_ids and candidate not in tracker_subentry_ids:
            return None
        return candidate

    # No entry_tracker_id set
    if tracker_subentry_ids:
        # Must be in tracker_subentry_ids
        if candidate in tracker_subentry_ids:
            return candidate
        return None

    # No restrictions - accept as-is
    return candidate


# ---------------------------------------------------------------------------
# Phase 9: Service Device Helper Functions
# ---------------------------------------------------------------------------


def sanitize_entry_title(entry_title: Any) -> str | None:
    """Sanitize and validate an entry title.

    Strips leading/trailing whitespace. Returns None for empty
    or non-string inputs.

    Args:
        entry_title: The entry title to sanitize.

    Returns:
        Sanitized title string, or None if invalid/empty.
    """
    if not isinstance(entry_title, str):
        return None
    stripped = entry_title.strip()
    return stripped if stripped else None


def has_user_defined_name(name_by_user: str | None) -> bool:
    """Check if a user has set a custom device name.

    Returns True only for non-empty stripped strings.

    Args:
        name_by_user: The user-defined name to check.

    Returns:
        True if the user has set a non-empty name.
    """
    if not isinstance(name_by_user, str):
        return False
    return bool(name_by_user.strip())


def extract_service_subentry_ids(
    entry_subentries: Any,
    entry_service_subentry_id: str | None,
    subentry_type_service: str,
    service_subentry_key: str,
) -> set[str]:
    """Extract service subentry IDs from entry.subentries mapping.

    Identifies subentries that are service-related by checking:
    1. subentry_type == subentry_type_service
    2. data.group_key == service_subentry_key

    Provisional subentries (ending in '-provisional') are skipped unless
    they match entry_service_subentry_id.

    Args:
        entry_subentries: The entry.subentries mapping.
        entry_service_subentry_id: The entry's service subentry ID (if set).
        subentry_type_service: The subentry type constant for service.
        service_subentry_key: The group_key constant for service.

    Returns:
        Set of service subentry IDs.
    """
    if not isinstance(entry_subentries, Mapping):
        return set()

    result: set[str] = set()
    for subentry_id, subentry in entry_subentries.items():
        # Skip invalid subentry IDs
        if not isinstance(subentry_id, str) or not subentry_id:
            continue

        # Skip provisional unless it matches entry_service_subentry_id
        if (
            subentry_id.endswith("-provisional")
            and subentry_id != entry_service_subentry_id
        ):
            continue

        # Check subentry_type
        subentry_type = getattr(subentry, "subentry_type", None)

        # Check group_key in data
        group_key: Any = None
        data_obj = getattr(subentry, "data", None)
        if isinstance(data_obj, Mapping):
            group_key = data_obj.get("group_key")

        # Include if it's a service subentry
        if subentry_type == subentry_type_service or (
            isinstance(group_key, str) and group_key == service_subentry_key
        ):
            result.add(subentry_id)

    return result


def should_defer_service_subentry(
    service_subentry_id: str | None,
    current_subentries: Any,
    entry_id: str | None,
    service_subentry_key: str,
) -> bool:
    """Check if config_subentry_id should be deferred until registry catches up.

    Returns True if the subentry_id is not in current_subentries and is not
    the stable default pattern.

    Args:
        service_subentry_id: The service config_subentry_id to check.
        current_subentries: The current entry.subentries mapping.
        entry_id: The config entry ID.
        service_subentry_key: The group_key constant for service.

    Returns:
        True if the subentry_id should be deferred.
    """
    if service_subentry_id is None:
        return False

    if not isinstance(current_subentries, Mapping):
        return False

    # Check if subentry is in registry
    if service_subentry_id in current_subentries:
        return False

    # Check for stable default pattern
    if isinstance(entry_id, str) and entry_id:
        stable_default = f"{entry_id}-{service_subentry_key}-subentry"
        if service_subentry_id == stable_default:
            return False

    # Defer if not found
    return True


def detect_extraneous_service_identifiers(
    device_identifiers: Collection[Any],
    target_identifiers: set[tuple[str, str]],
    domain: str,
) -> set[tuple[str, str]]:
    """Find :service identifiers not in target set.

    Identifies device identifiers that:
    1. Are 2-tuples with matching domain
    2. Have second element ending in ':service'
    3. Are not in the target_identifiers set

    Args:
        device_identifiers: Current device identifiers.
        target_identifiers: Target identifiers to keep.
        domain: The integration domain to match.

    Returns:
        Set of extraneous service identifier tuples.
    """
    extraneous: set[tuple[str, str]] = set()

    for identifier in device_identifiers:
        if not isinstance(identifier, tuple) or len(identifier) != 2:
            continue
        ident_domain, ident_value = identifier
        if ident_domain != domain:
            continue
        if not isinstance(ident_value, str):
            continue
        if not ident_value.endswith(":service"):
            continue
        if identifier not in target_identifiers:
            extraneous.add(identifier)

    return extraneous


def determine_removal_subentry_id(
    current_service_links: set[str],
    dev_config_subentry_id: str | None,
) -> str | None:
    """Determine which subentry ID to remove from device.

    Prefers items from current_service_links, falls back to
    dev_config_subentry_id.

    Args:
        current_service_links: Set of current service link IDs.
        dev_config_subentry_id: The device's config_subentry_id.

    Returns:
        Subentry ID to remove, or None if nothing to remove.
    """
    if current_service_links:
        return next(iter(current_service_links))

    if isinstance(dev_config_subentry_id, str) and dev_config_subentry_id.strip():
        return dev_config_subentry_id.strip()

    return None


# ---------------------------------------------------------------------------
# Phase 12: Entity Lookup Helper Functions
# ---------------------------------------------------------------------------


def extract_canonical_device_id(
    identifiers: Collection[Any] | None,
    domain: str,
    entry_id: str | None = None,
    service_prefix: str = "",
    legacy_service_id: str = "",
) -> str | None:
    """Extract canonical device ID from device registry identifiers.

    Scans identifiers looking for a device ID matching the domain.
    Supports both simple and namespaced (entry_id:device_id) formats.

    Args:
        identifiers: Set of identifier tuples from device registry.
        domain: The integration domain to match.
        entry_id: Optional entry_id for namespaced identifier matching.
        service_prefix: Prefix for service device identifiers to skip.
        legacy_service_id: Legacy service device identifier to skip.

    Returns:
        The canonical device ID, or None if not found.
    """
    if identifiers is None:
        return None

    if not isinstance(identifiers, Collection) or isinstance(
        identifiers, (str, bytes, Mapping)
    ):
        return None

    namespaced_result: str | None = None
    simple_result: str | None = None

    for identifier in identifiers:
        # Must be 2-tuple
        if not isinstance(identifier, (tuple, list)) or len(identifier) != 2:
            continue

        ident_domain, ident_value = identifier

        # Must match domain
        if ident_domain != domain:
            continue

        # Must be non-empty string
        if not isinstance(ident_value, str) or not ident_value:
            continue

        # Skip service device identifiers
        if service_prefix and ident_value.startswith(service_prefix):
            continue
        if legacy_service_id and ident_value == legacy_service_id:
            continue

        # Handle namespaced format
        if ":" in ident_value:
            if entry_id and ident_value.startswith(entry_id + ":"):
                namespaced_result = ident_value.split(":", 1)[1]
            # If entry_id doesn't match, skip this identifier
            continue

        # Simple format
        simple_result = ident_value

    # Prefer namespaced result when entry_id is provided
    if namespaced_result is not None:
        return namespaced_result

    return simple_result


def build_entity_unique_id_candidates(
    device_id: str,
    entry_id: str | None,
    subentry_identifier: str | None,
    domain: str,
    subentry_key: str | None = None,
) -> list[str]:
    """Generate list of unique_id candidates for entity lookup.

    Produces various unique_id formats in priority order for finding
    an entity that may have been created with different ID schemes.

    Args:
        device_id: The device ID.
        entry_id: The config entry ID.
        subentry_identifier: The stable subentry identifier.
        domain: The integration domain.
        subentry_key: Optional subentry key if different from identifier.

    Returns:
        List of unique_id candidates in priority order (canonical first).
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add(candidate: str) -> None:
        if candidate and candidate not in seen:
            candidates.append(candidate)
            seen.add(candidate)

    # Build canonical format first (highest priority)
    if entry_id and subentry_identifier and device_id:
        add(f"{entry_id}:{subentry_identifier}:{device_id}")

    # Subentry key variant (if different from identifier)
    if entry_id and subentry_key and subentry_key != subentry_identifier and device_id:
        add(f"{entry_id}:{subentry_key}:{device_id}")

    # Entry:device format
    if entry_id and device_id:
        add(f"{entry_id}:{device_id}")

    # Domain_entry_device format
    if entry_id and device_id:
        add(f"{domain}_{entry_id}_{device_id}")

    # Legacy domain_device format
    if device_id:
        add(f"{domain}_{device_id}")

    return candidates


def build_canonical_unique_id(
    entry_id: str | None,
    subentry_identifier: str | None,
    device_id: str | None,
) -> str | None:
    """Build canonical unique_id from components.

    Format: entry_id:subentry_identifier:device_id
    Skips empty components (except device_id which is required).

    Args:
        entry_id: The config entry ID.
        subentry_identifier: The stable subentry identifier.
        device_id: The device ID.

    Returns:
        Canonical unique_id string, or None if required parts are missing.
    """
    # Entry_id is required
    if not entry_id or not isinstance(entry_id, str):
        return None
    entry_id = entry_id.strip()
    if not entry_id:
        return None

    # Device_id is required
    if not device_id or not isinstance(device_id, str):
        return None
    device_id = device_id.strip()
    if not device_id:
        return None

    # Subentry_identifier is optional
    parts: list[str] = [entry_id]

    if subentry_identifier and isinstance(subentry_identifier, str):
        stripped = subentry_identifier.strip()
        if stripped:
            parts.append(stripped)

    parts.append(device_id)

    return ":".join(parts)


def is_legacy_unique_id(unique_id: Any, domain: str) -> bool:
    """Detect if a unique_id is in legacy format.

    Legacy formats use underscore separation:
    - domain_device_id
    - domain_entry_id_device_id

    Modern formats use colon separation:
    - entry_id:device_id
    - entry_id:subentry:device_id

    Args:
        unique_id: The unique_id to check.
        domain: The integration domain.

    Returns:
        True if unique_id is in legacy underscore format.
    """
    if not isinstance(unique_id, str) or not unique_id:
        return False

    prefix = f"{domain}_"

    # Must start with domain_
    if not unique_id.startswith(prefix):
        return False

    # Must have content after prefix
    suffix = unique_id[len(prefix) :]
    if not suffix:
        return False

    # Legacy format uses underscores, not colons
    # If it contains colons, it's the modern format
    if ":" in unique_id:
        return False

    return True


def match_entity_by_device_id(
    unique_id: Any,
    config_entry_id: str | None,
    device_id: str,
    target_entry_id: str | None,
    domain: str,
    platform: str,
    entity_domain: str,
    entity_platform: str,
) -> bool:
    """Check if an entity registry entry matches device criteria.

    Used for fallback entity lookup when direct unique_id match fails.

    Args:
        unique_id: The entity's unique_id.
        config_entry_id: The entity's config_entry_id.
        device_id: The device ID to match (must be contained in unique_id).
        target_entry_id: The target entry ID to filter by (or None for no filter).
        domain: The expected domain (e.g., "device_tracker").
        platform: The expected platform (e.g., "googlefindmy").
        entity_domain: The entity's actual domain.
        entity_platform: The entity's actual platform.

    Returns:
        True if the entity matches all criteria.
    """
    # Validate required inputs
    if not device_id or not isinstance(device_id, str):
        return False

    # Check domain and platform match
    if entity_domain != domain or entity_platform != platform:
        return False

    # Check config_entry_id if target is specified
    if target_entry_id is not None and config_entry_id is not None:
        if config_entry_id != target_entry_id:
            return False

    # Check unique_id contains device_id
    if not isinstance(unique_id, str):
        return False

    return device_id in unique_id
