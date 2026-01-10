"""Polling operations for GoogleFindMyCoordinator.

This module contains polling-related methods extracted from main.py.

Methods moved here:
- _set_api_status: Update API polling status
- _set_fcm_status: Update FCM push transport status
- _is_on_hass_loop: Check if on HA event loop
- _run_on_hass_loop: Schedule callable on HA loop
- _dispatch_async_request_refresh: Safe refresh dispatch
- _schedule_short_retry: Coalesced short retry scheduling
- _handle_dr_event: Handle device registry changes
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from homeassistant.core import Event
from homeassistant.helpers.event import async_call_later

from ..const import DOMAIN
from .helpers.stats import FcmStatus, StatusSnapshot

_LOGGER = logging.getLogger(__name__)

# Cooldown constants for crowdsourced reports
_COOLDOWN_MIN_IN_ALL_AREAS_S = 10 * 60  # 10 minutes
_COOLDOWN_MIN_HIGH_TRAFFIC_S = 5 * 60  # 5 minutes

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

    def _dispatch_async_request_refresh(
        self: "GoogleFindMyCoordinator", *, task_name: str, log_context: str
    ) -> None:
        """Invoke ``async_request_refresh`` safely regardless of its implementation."""
        fn = getattr(self, "async_request_refresh", None)
        if not callable(fn):
            return

        def _invoke() -> None:
            try:
                result = fn()
                if inspect.isawaitable(result):
                    self.hass.async_create_task(result, name=task_name)
            except Exception as err:
                _LOGGER.debug(
                    "async_request_refresh dispatch failed (%s): %s", log_context, err
                )

        if self._is_on_hass_loop():
            _invoke()
        else:
            self._run_on_hass_loop(_invoke)

    def _schedule_short_retry(
        self: "GoogleFindMyCoordinator", delay_s: float = 5.0
    ) -> None:
        """Schedule a short, coalesced refresh instead of shifting the poll baseline.

        Rationale:
        - When FCM/push is not ready, we *do not* advance `_last_poll_mono`.
          Advancing the baseline hides readiness transitions and can put the
          scheduler to "sleep". Instead, we request a short follow-up refresh.

        Behavior:
        - Coalesces multiple calls by cancelling a pending callback first.
        - Always runs on the HA event loop.

        Args:
            delay_s: Delay in seconds before requesting a coordinator refresh.
        """

        def _do_schedule() -> None:
            # Cancel a pending short retry (coalesce)
            if self._short_retry_cancel is not None:
                try:
                    self._short_retry_cancel()
                except Exception:  # defensive
                    pass
                finally:
                    self._short_retry_cancel = None

            def _cb(_now: datetime) -> None:
                # Clear handle and request a refresh (non-blocking)
                self._short_retry_cancel = None
                self._dispatch_async_request_refresh(
                    task_name=f"{DOMAIN}.short_retry_refresh",
                    log_context="short retry",
                )

            self._short_retry_cancel = async_call_later(
                self.hass, max(0.0, float(delay_s)), _cb
            )

        if self._is_on_hass_loop():
            _do_schedule()
        else:
            self._run_on_hass_loop(_do_schedule)

    async def _handle_dr_event(self: "GoogleFindMyCoordinator", _event: Event) -> None:
        """Handle Device Registry changes by rebuilding poll targets (rare)."""
        self._reindex_poll_targets_from_device_registry()
        # After changes, request a refresh so the next tick uses the new target sets.
        self._dispatch_async_request_refresh(
            task_name=f"{DOMAIN}.dr_event_refresh",
            log_context="device registry event",
        )

    def _compute_type_cooldown_seconds(
        self: "GoogleFindMyCoordinator", report_hint: str | None
    ) -> int:
        """Return a server-aware cooldown duration in seconds for a crowdsourced report type.

        Derived from POPETS'25 observations:
        - "in_all_areas": ~10 min throttle window (minimum).
        - "high_traffic": ~5 min throttle window (minimum).

        IMPORTANT:
        - To guarantee effect, the applied cooldown is **never shorter than** the
          configured `location_poll_interval`. This ensures at least one scheduled
          poll cycle is skipped in practice.
        """
        if not report_hint:
            return 0

        # Guarantee the cooldown always spans at least one poll interval
        effective_poll = max(1, int(self.location_poll_interval))
        if report_hint == "in_all_areas":
            base_cooldown = _COOLDOWN_MIN_IN_ALL_AREAS_S
        elif report_hint == "high_traffic":
            base_cooldown = _COOLDOWN_MIN_HIGH_TRAFFIC_S
        else:
            return 0

        return max(base_cooldown, effective_poll)

    def _apply_report_type_cooldown(
        self: "GoogleFindMyCoordinator", device_id: str, report_hint: str | None
    ) -> None:
        """Apply a per-device **poll** cooldown based on the crowdsourced report type.

        - Does nothing for None/unknown hints.
        - Uses monotonic time, and **extends** any existing cooldown (takes the max).
        - Internal only; does not touch public APIs or entity attributes.
        """
        try:
            seconds = int(self._compute_type_cooldown_seconds(report_hint))
        except Exception:  # defensive
            seconds = 0
        if seconds <= 0:
            return

        now_mono = time.monotonic()
        new_deadline = now_mono + float(seconds)
        prev_deadline = self._device_poll_cooldown_until.get(device_id, 0.0)
        if new_deadline > prev_deadline:
            self._device_poll_cooldown_until[device_id] = new_deadline
            _LOGGER.debug(
                "Applied %ss poll cooldown for %s (hint='%s', poll_interval=%ss)",
                seconds,
                device_id,
                report_hint,
                self.location_poll_interval,
            )
