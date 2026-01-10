"""Polling operations for GoogleFindMyCoordinator.

This module contains polling-related methods extracted from main.py.

Methods moved here:
- _set_api_status: Update API polling status
- _set_fcm_status: Update FCM push transport status
- _is_on_hass_loop: Check if on HA event loop
- _run_on_hass_loop: Schedule callable on HA loop
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Callable

from .helpers.stats import FcmStatus, StatusSnapshot

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator


class PollingOperations:
    """Polling operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage the polling lifecycle,
    including status tracking and event loop helpers.
    """

    def _set_api_status(
        self: "GoogleFindMyCoordinator", status: str, *, reason: str | None = None
    ) -> None:
        """Update the API polling status and notify listeners if it changed."""
        if status == self._api_status_state and reason == self._api_status_reason:
            return

        self._api_status_state = status
        self._api_status_reason = reason
        self._api_status_changed_at = time.time()

        try:
            self.async_set_updated_data(self.data)
        except Exception:
            # Fallback for very early startup when listeners are not ready yet.
            pass

    def _set_fcm_status(
        self: "GoogleFindMyCoordinator", status: str, *, reason: str | None = None
    ) -> None:
        """Update the push transport status while avoiding noisy churn."""
        if status == self._fcm_status_state and reason == self._fcm_status_reason:
            return

        self._fcm_status_state = status
        self._fcm_status_reason = reason
        self._fcm_status_changed_at = time.time()

        try:
            self.async_set_updated_data(self.data)
        except Exception:
            pass

    @property
    def api_status(self: "GoogleFindMyCoordinator") -> StatusSnapshot:
        """Return a snapshot describing the current API polling health."""
        return StatusSnapshot(
            state=self._api_status_state,
            reason=self._api_status_reason,
            changed_at=self._api_status_changed_at,
        )

    @property
    def fcm_status(self: "GoogleFindMyCoordinator") -> StatusSnapshot:
        """Return a snapshot describing the current push transport health."""
        return StatusSnapshot(
            state=self._fcm_status_state,
            reason=self._fcm_status_reason,
            changed_at=self._fcm_status_changed_at,
        )

    @property
    def is_fcm_connected(self: "GoogleFindMyCoordinator") -> bool:
        """Convenience boolean for entities relying on push transport availability."""
        return self._fcm_status_state == FcmStatus.CONNECTED

    @property
    def consecutive_timeouts(self: "GoogleFindMyCoordinator") -> int:
        """Return the number of consecutive poll timeouts."""
        return self._consecutive_timeouts

    @property
    def last_poll_result(self: "GoogleFindMyCoordinator") -> str | None:
        """Return the last recorded poll result ("success"/"failed")."""
        return self._last_poll_result

    def _is_on_hass_loop(self: "GoogleFindMyCoordinator") -> bool:
        """Return True if currently executing on the HA event loop thread."""
        loop = self.hass.loop
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def _run_on_hass_loop(
        self: "GoogleFindMyCoordinator",
        func: Callable[..., None],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Schedule a plain callable to run on the HA loop thread ASAP.

        Note:
        - This is intentionally **fire-and-forget**; `call_soon_threadsafe` does not
          return the callable's result to the caller. Only use with functions that
          **return None** and are safe to run on the HA loop.
        """
        self.hass.loop.call_soon_threadsafe(func, *args, **kwargs)
