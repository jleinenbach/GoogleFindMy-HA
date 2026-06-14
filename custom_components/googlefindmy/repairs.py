# custom_components/googlefindmy/repairs.py
"""Repair flows for the Google Find My Device integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

_LOGGER = logging.getLogger(__name__)


class FcmReauthRepairFlow(RepairsFlow):  # type: ignore[misc]
    """Guide the user from the fixable ``fcm_reauth_required`` issue into reauth.

    The issue is raised at the FCM supervisor's terminal give-up points
    (404/401 endpoint exhaustion and the registration backoff). Confirming the
    repair starts Home Assistant's existing config-entry reauth flow, which
    renews credentials while preserving the device identity.
    """

    def __init__(self, entry_id: str | None) -> None:
        """Store the config entry id forwarded via the issue ``data`` payload."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Entry point: show the confirmation step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm the repair and start the reauth flow for the entry."""
        if user_input is not None:
            self._async_start_reauth()
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="confirm", data_schema=vol.Schema({})
        )

    def _async_start_reauth(self) -> None:
        """Start the config-entry reauth flow, defensively.

        ``ConfigEntry.async_start_reauth`` is a synchronous ``@callback`` that
        returns ``None``: it schedules the reauth flow itself, or no-ops when a
        reauth/reconfigure flow is already active for the entry (idempotent).
        It must therefore be called without ``await`` -- awaiting its ``None``
        result would raise ``TypeError`` on every confirmation, which the broad
        guard below would then swallow while the issue is still dismissed.

        ``entry_id`` is used solely as a config-entry registry lookup key; it is
        never interpreted as a path or executable input. A missing or already
        removed entry simply dismisses the (now stale) repair without raising.
        """
        if not self._entry_id:
            _LOGGER.warning(
                "fcm_reauth_required repair confirmed without an entry_id; "
                "dismissing stale issue"
            )
            return

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            _LOGGER.warning(
                "fcm_reauth_required repair confirmed but config entry %s is "
                "gone; dismissing stale issue",
                self._entry_id,
            )
            return

        try:
            entry.async_start_reauth(self.hass)
        except Exception as err:  # noqa: BLE001 - defensive: keep the repair UI robust
            _LOGGER.error(
                "Failed to start reauth flow from repair for entry %s: %s",
                self._entry_id,
                err,
            )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create the fix flow for a fixable Google Find My Device issue.

    Currently every fixable issue (``fcm_reauth_required_{entry_id}``) maps to
    the reauth repair. The affected entry id is read from the issue ``data``
    payload set by ``_async_raise_reauth_issue`` in ``Auth/fcm_receiver_ha.py``.
    """
    entry_id_raw = (data or {}).get("entry_id")
    entry_id = entry_id_raw if isinstance(entry_id_raw, str) else None
    return FcmReauthRepairFlow(entry_id)
