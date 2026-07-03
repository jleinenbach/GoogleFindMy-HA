# custom_components/googlefindmy/repairs.py
"""Repair flows for the Google Find My Device integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .coordinator._reauth_reason import ReauthReasonCode

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
        """Confirm the repair, launch reauth, and keep the issue open.

        Confirming only *launches* the config-entry reauth flow; the stored
        credentials are not renewed yet at this point. Returning
        ``async_create_entry`` would make Home Assistant's repairs manager
        delete the issue immediately -- only a non-``ABORT`` result triggers the
        delete (see ``homeassistant.components.repairs.issue_handler``). That is
        unsafe in the *normal* case: the supervisor reaches this repair at its
        *terminal* give-up points, and the 401/404 exhaustion paths ``break``
        out of the loop, so the supervisor will not re-raise the issue on its
        own. If the user then cancels or fails the reauth flow, both this issue
        and the core ``config_entry_reauth`` issue (removed by
        ``async_flow_removed``) would vanish, stranding the account with no
        guidance.

        Returning ``async_abort`` keeps the issue. Its real resolution is owned
        by the supervisor's success path, which deletes
        ``fcm_reauth_required_{entry_id}`` on the next successful FCM
        (re-)registration once reauth has renewed credentials and reloaded the
        entry (see ``Auth/fcm_receiver_ha.py``).

        The keep-open contract only holds while a supervisor can still run for
        this entry. When the repair is *stale* -- the entry id is missing, or
        the config entry has already been removed -- no supervisor exists to
        ever clear this entry-scoped issue, and reauth can never start. In that
        case ``_async_start_reauth`` reports failure and we deliberately return
        ``async_create_entry`` so the repairs manager dismisses the orphaned
        issue instead of leaving an unfixable repair pinned in the UI.
        """
        if user_input is not None:
            if self._async_start_reauth():
                return self.async_abort(reason="reauth_started")
            return self.async_create_entry(data={})

        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))

    def _async_start_reauth(self) -> bool:
        """Start the config-entry reauth flow, defensively.

        ``ConfigEntry.async_start_reauth`` is a synchronous ``@callback`` that
        returns ``None``: it schedules the reauth flow itself, or no-ops when a
        reauth/reconfigure flow is already active for the entry (idempotent).
        It must therefore be called without ``await`` -- awaiting its ``None``
        result would raise ``TypeError`` on every confirmation, which the broad
        guard below would then swallow while the issue is still dismissed.

        ``entry_id`` is used solely as a config-entry registry lookup key; it is
        never interpreted as a path or executable input.

        Returns ``True`` when the fixable issue must stay open: either reauth
        was launched, or it failed transiently for an entry that still exists
        (the user can retry and the supervisor success path can still clear the
        issue). Returns ``False`` when the repair is *stale* -- a missing entry
        id or an already removed config entry -- so the caller dismisses the now
        unfixable issue instead of pinning it open forever.
        """
        if not self._entry_id:
            _LOGGER.warning(
                "fcm_reauth_required repair confirmed without an entry_id; "
                "dismissing stale issue"
            )
            return False

        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        if entry is None:
            _LOGGER.warning(
                "fcm_reauth_required repair confirmed but config entry %s is "
                "gone; dismissing stale issue",
                self._entry_id,
            )
            return False

        # FIX 3: direct reauth site. NOTE (SA-11): the plan's AE-5 assumed a
        # coordinator ``self`` is available here, but this method lives on the
        # ``FcmReauthRepairFlow`` (a RepairsFlow), not on the coordinator. The
        # coordinator is reachable only via ``entry.runtime_data`` and only when
        # the entry is loaded, so record defensively and never let a missing
        # coordinator break the repair UI.
        coordinator = getattr(getattr(entry, "runtime_data", None), "coordinator", None)
        recorder = getattr(coordinator, "record_reauth_reason", None)
        if callable(recorder):
            recorder(ReauthReasonCode.FCM_AUTH_FATAL, origin="repairs.py:112")

        try:
            entry.async_start_reauth(self.hass)
        except Exception as err:  # noqa: BLE001 - defensive: keep the repair UI robust
            _LOGGER.error(
                "Failed to start reauth flow from repair for entry %s: %s",
                self._entry_id,
                err,
            )
            # The entry still exists, so the issue is not stale: keep it open so
            # the user can retry and the supervisor can still resolve it.
            return True

        return True


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
