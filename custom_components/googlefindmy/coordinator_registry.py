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

from collections.abc import Mapping
from typing import Any

# Re-export constants used by this module's functions
from .const import (
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
)

__all__ = [
    "LEGACY_SERVICE_IDENTIFIER",
    "SERVICE_DEVICE_IDENTIFIER_PREFIX",
    "build_legacy_device_registry_kwargs",
    "extract_device_display_name",
    "needs_legacy_kwarg_retry",
    "parse_device_identifier",
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
