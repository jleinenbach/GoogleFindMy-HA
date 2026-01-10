"""Identity operations for GoogleFindMyCoordinator.

This module contains identity-related methods extracted from main.py.

Methods moved here:
- _get_account_email: Get configured Google account email
- _create_auth_issue: Create Repairs issue for auth problems
- _dismiss_auth_issue: Dismiss auth Repairs issue
- _schedule_eid_resolver_refresh: Refresh the global EID resolver
- _register_identity_key: Register device identity key for shared tracker detection
- _reset_resolver_offset: Clear resolver offsets when identity keys rotate
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from ..const import (
    CONF_GOOGLE_EMAIL,
    DATA_EID_RESOLVER,
    DOMAIN,
    ISSUE_AUTH_EXPIRED_KEY,
    issue_id_for,
)

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)


class IdentityOperations:
    """Identity operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage device identities,
    including identity key registration and account information.
    """

    def _get_account_email(self: "GoogleFindMyCoordinator") -> str:
        """Return the configured Google account email for this entry (empty if unknown)."""
        entry = self.config_entry
        if entry is not None:
            email_value = entry.data.get(CONF_GOOGLE_EMAIL)
            if isinstance(email_value, str):
                return email_value
        return ""

    def _create_auth_issue(self: "GoogleFindMyCoordinator") -> None:
        """Create (idempotent) a Repairs issue for an authentication problem.

        Uses:
            - domain: `googlefindmy`
            - issue_id: stable per-entry (via `issue_id_for(entry_id)`)
            - translation_key: `ISSUE_AUTH_EXPIRED_KEY` (localizable title/description)
            - placeholders: `email` (shown in repairs UI)
        """
        entry = self.config_entry
        if not entry:
            return
        issue_id = issue_id_for(entry.entry_id)
        email = self._get_account_email() or "unknown"
        try:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_AUTH_EXPIRED_KEY,
                translation_placeholders={"email": email},
            )
        except Exception as err:
            _LOGGER.debug("Failed to create Repairs issue: %s", err)

    def _dismiss_auth_issue(self: "GoogleFindMyCoordinator") -> bool:
        """Dismiss (idempotently) the Repairs issue if present.

        Returns True when an issue existed and was removed, False otherwise.
        """
        entry = self.config_entry
        if not entry:
            return False

        issue_id = issue_id_for(entry.entry_id)

        issue_present = False
        try:
            registry = ir.async_get(self.hass)
        except Exception:  # pragma: no cover - defensive fallback
            registry = None

        if registry and hasattr(registry, "async_get_issue"):
            try:
                issue_present = registry.async_get_issue(DOMAIN, issue_id) is not None
            except Exception:  # pragma: no cover - defensive fallback
                issue_present = False

        try:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        except Exception:
            # Deleting a non-existent issue is fine; keep silent.
            return False

        return issue_present

    def _schedule_eid_resolver_refresh(self: "GoogleFindMyCoordinator") -> None:
        """Refresh the global EID resolver when active device sets change."""

        hass = getattr(self, "hass", None)
        hass_data = getattr(hass, "data", None)
        if not isinstance(hass_data, dict):
            return

        bucket = hass_data.get(DOMAIN)
        if not isinstance(bucket, dict):
            return

        resolver = bucket.get(DATA_EID_RESOLVER)
        refresh = getattr(resolver, "async_refresh", None)
        if callable(refresh):
            create_task = getattr(self.hass, "async_create_task", None)
            if callable(create_task):
                create_task(refresh())

    def _register_identity_key(
        self: "GoogleFindMyCoordinator", device_id: str, identity_key: bytes
    ) -> None:
        """Register a device's identity_key for shared tracker detection.

        Maintains a mapping from identity_key to all device_ids that share the
        same physical tracker. This enables location propagation across accounts.

        Args:
            device_id: Canonical device identifier.
            identity_key: Normalized 32-byte identity key.
        """
        if not isinstance(identity_key, bytes) or len(identity_key) != 32:
            return

        device_set = self._identity_key_to_devices.setdefault(identity_key, set())
        if device_id not in device_set:
            device_set.add(device_id)
            if len(device_set) > 1:
                _LOGGER.info(
                    "Shared tracker detected: identity_key=%s... shared by %d devices: %s",
                    identity_key[:8].hex(),
                    len(device_set),
                    sorted(device_set),
                )

    def _reset_resolver_offset(
        self: "GoogleFindMyCoordinator", device_id: str
    ) -> None:
        """Clear resolver offsets using registry IDs when identity keys rotate."""

        hass = getattr(self, "hass", None)
        if hass is None:
            return

        registry_id: str | None = None
        entry_id = self._entry_id()

        dev_reg = dr.async_get(hass)
        if entry_id and dev_reg:
            identifiers = {
                (DOMAIN, f"{entry_id}:{device_id}"),
                (DOMAIN, device_id),
            }
            device = dev_reg.async_get_device(identifiers=identifiers)
            if device:
                registry_id = device.id

        if not registry_id:
            _LOGGER.debug(
                "Could not resolve Registry ID for canonical %s; skipping offset reset.",
                device_id,
            )
            return

        hass_data = getattr(hass, "data", None)
        if not isinstance(hass_data, dict):
            return

        bucket = hass_data.get(DOMAIN)
        if not isinstance(bucket, dict):
            return

        resolver = bucket.get(DATA_EID_RESOLVER)
        if resolver is None:
            return

        reset = getattr(resolver, "reset_device_offset", None)
        if callable(reset):
            _LOGGER.debug(
                "Triggering resolver offset reset for %s (Registry ID: %s)",
                device_id,
                registry_id,
            )
            reset(registry_id)
