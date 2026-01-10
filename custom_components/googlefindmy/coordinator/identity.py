"""Identity operations for GoogleFindMyCoordinator.

This module contains identity-related methods extracted from main.py.

Methods moved here:
- _get_account_email: Get configured Google account email
- _create_auth_issue: Create Repairs issue for auth problems
- _dismiss_auth_issue: Dismiss auth Repairs issue
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.helpers import issue_registry as ir

from ..const import CONF_GOOGLE_EMAIL, DOMAIN, ISSUE_AUTH_EXPIRED_KEY, issue_id_for

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
