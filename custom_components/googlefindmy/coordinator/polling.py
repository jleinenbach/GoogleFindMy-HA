"""Polling operations for GoogleFindMyCoordinator.

This module contains polling-related methods extracted from main.py.

Methods moved here:
- _set_api_status: Update API polling status
- _set_fcm_status: Update FCM push transport status
- api_status: StatusSnapshot property for API health
- fcm_status: StatusSnapshot property for push transport health
- is_fcm_connected: Convenience boolean for push availability
- consecutive_timeouts: Number of consecutive poll timeouts
- last_poll_result: Last recorded poll result
- _is_on_hass_loop: Check if on HA event loop
- _run_on_hass_loop: Schedule callable on HA loop
- _dispatch_async_request_refresh: Safe refresh dispatch
- _schedule_short_retry: Coalesced short retry scheduling
- _handle_dr_event: Handle device registry changes
- _compute_type_cooldown_seconds: Server-aware cooldown duration
- _apply_report_type_cooldown: Apply per-device poll cooldown
- is_polling: Property for current polling state
- get_fcm_acquire_duration_seconds: Duration to acquire FCM
- get_last_poll_duration_seconds: Duration of last poll cycle
- _is_fcm_ready_soft: Check if push transport appears ready
- _note_fcm_deferral: Escalation timeline for FCM not ready
- _clear_fcm_deferral: Clear escalation on FCM ready
- _get_predicted_poll_time: Predict next update time
- _note_push_transport_problem: Enter cooldown after push failure
- force_poll_due: Force next poll immediately
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from statistics import mean, stdev
from typing import Any

from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.core import Event
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import UpdateFailed

from ..const import (
    DEVICE_LIST_POLL_INTERVAL,
    DOMAIN,
    LOCATION_REQUEST_TIMEOUT_S,
)
from ..NovaApi.nova_request import NovaAuthError, NovaAuthPermanentError
from ..SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
)
from ..SpotApi.spot_request import SpotAuthPermanentError
from ._mixin_typing import _MixinBase
from .helpers.cache import sanitize_decoder_row as _sanitize_decoder_row
from .helpers.stats import ApiStatus, FcmStatus, StatusSnapshot
from .helpers.subentry import normalize_epoch_seconds as _normalize_epoch_seconds
from .helpers.update import (
    calculate_presence_ttl as _calculate_presence_ttl_impl,
)
from .helpers.update import (
    filter_and_dedupe_devices as _filter_and_dedupe_impl,
)
from .helpers.update import (
    is_fatal_fcm_auth_error as _is_fatal_fcm_auth_error_impl,
)
from .helpers.update import (
    is_poll_cycle_due as _is_poll_cycle_due_impl,
)
from .helpers.update import (
    normalize_device_list_payload as _normalize_device_list_impl,
)
from .helpers.update import (
    should_defer_empty_list as _should_defer_empty_list_impl,
)

_LOGGER = logging.getLogger(__name__)

# Cooldown constants for crowdsourced reports
_COOLDOWN_MIN_IN_ALL_AREAS_S = 10 * 60  # 10 minutes
_COOLDOWN_MIN_HIGH_TRAFFIC_S = 5 * 60  # 5 minutes

# Accept an empty device list only on the 2nd consecutive result (defers once)
_EMPTY_LIST_QUORUM = 2

# Maximum number of transient auth failures before triggering re-auth
_MAX_TRANSIENT_AUTH_FAILURES = 3

# FCM error retry threshold before triggering re-auth
_FCM_ERROR_RETRY_THRESHOLD = 3

# Timeout for FCM not ready before forcing poll (seconds)
_PUSH_NOT_READY_TIMEOUT_S = 15

# Predictive polling buffer to avoid requesting data before it is available server-side
_PREDICTION_BUFFER_S = 45


class PollingOperations(_MixinBase):
    """Polling operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage the polling lifecycle,
    including status tracking and event loop helpers.
    """

    # Attribute declarations for mypy (actual values set in GoogleFindMyCoordinator.__init__)
    _push_ready_memo: bool | None
    _last_poll_result: str | None
    _api_status_state: str
    _api_status_reason: str | None
    _api_status_changed_at: float | None
    _fcm_status_state: str
    _fcm_status_reason: str | None
    _fcm_status_changed_at: float | None
    _force_device_list_reason: str | None
    _short_retry_cancel: Callable[[], None] | None
    _fcm_last_error: str | None
    _last_transient_auth_error: str | None
    _is_polling: bool
    _startup_complete: bool

    def _set_api_status(
        self, status: str, *, reason: str | None = None
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
        self, status: str, *, reason: str | None = None
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
    def api_status(self) -> StatusSnapshot:
        """Return a snapshot describing the current API polling health."""
        return StatusSnapshot(
            state=self._api_status_state,
            reason=self._api_status_reason,
            changed_at=self._api_status_changed_at,
        )

    @property
    def fcm_status(self) -> StatusSnapshot:
        """Return a snapshot describing the current push transport health."""
        return StatusSnapshot(
            state=self._fcm_status_state,
            reason=self._fcm_status_reason,
            changed_at=self._fcm_status_changed_at,
        )

    @property
    def is_fcm_connected(self) -> bool:
        """Convenience boolean for entities relying on push transport availability."""
        return self._fcm_status_state == FcmStatus.CONNECTED

    @property
    def consecutive_timeouts(self) -> int:
        """Return the number of consecutive poll timeouts."""
        return self._consecutive_timeouts

    @property
    def last_poll_result(self) -> str | None:
        """Return the last recorded poll result ("success"/"failed")."""
        return self._last_poll_result

    def _is_on_hass_loop(self) -> bool:
        """Return True if currently executing on the HA event loop thread."""
        loop = self.hass.loop
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def _run_on_hass_loop(
        self,
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
        if kwargs:
            self.hass.loop.call_soon_threadsafe(
                functools.partial(func, *args, **kwargs)
            )
        else:
            self.hass.loop.call_soon_threadsafe(func, *args)

    def _dispatch_async_request_refresh(
        self, *, task_name: str, log_context: str
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
        self, delay_s: float = 5.0
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

    async def _handle_dr_event(self, _event: Event) -> None:
        """Handle Device Registry changes by rebuilding poll targets (rare)."""
        self._reindex_poll_targets_from_device_registry()
        # After changes, request a refresh so the next tick uses the new target sets.
        self._dispatch_async_request_refresh(
            task_name=f"{DOMAIN}.dr_event_refresh",
            log_context="device registry event",
        )

    def _compute_type_cooldown_seconds(
        self, report_hint: str | None
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
        self, device_id: str, report_hint: str | None
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

    # -------------------- Public read-only state for diagnostics/UI --------------------
    @property
    def is_polling(self) -> bool:
        """Expose current polling state (public read-only API).

        Returns:
            True if a polling cycle is currently in progress.
        """
        return self._is_polling

    def get_fcm_acquire_duration_seconds(
        self,
    ) -> float | None:
        """Duration between 'setup_start_monotonic' and 'fcm_acquired_monotonic'."""
        from .helpers.stats import get_duration as _get_duration_impl

        return _get_duration_impl(
            self.get_metric("setup_start_monotonic"),
            self.get_metric("fcm_acquired_monotonic"),
        )

    def get_last_poll_duration_seconds(
        self,
    ) -> float | None:
        """Duration of the most recent sequential polling cycle (if recorded)."""
        return self._get_duration("last_poll_start_mono", "last_poll_end_mono")

    # -------------------- FCM readiness checks --------------------
    def _is_fcm_ready_soft(self) -> bool:
        """Return True if push transport appears ready (no awaits, no I/O).

        Priority order:
          1) Ask API (single source of truth if available).
          2) Receiver-level booleans.
          3) Push-client heuristic (run_state + do_listen).
        """
        try:
            # 1) API knowledge (preferred)
            try:
                fn = getattr(self.api, "is_push_ready", None)
                if callable(fn):
                    return bool(fn())
            except Exception:
                pass

            # 2) Receiver-level flags
            fcm = self.hass.data.get(DOMAIN, {}).get("fcm_receiver")
            if not fcm:
                return False
            fatal_error: str | None = getattr(fcm, "_fatal_error", None)
            entry_id = self._entry_id()
            fatal_by_entry = getattr(fcm, "_fatal_errors", None)
            if isinstance(fatal_by_entry, Mapping) and entry_id:
                fatal_error = fatal_by_entry.get(entry_id) or fatal_error
            if isinstance(fatal_error, str) and fatal_error:
                return False
            for attr in ("is_ready", "ready"):
                val = getattr(fcm, attr, None)
                if isinstance(val, bool):
                    return val

            # 3) Heuristic: push client state (no enum import)
            pc = getattr(fcm, "pc", None)
            if pc is not None:
                state = getattr(pc, "run_state", None)
                state_name = getattr(state, "name", state)
                if state_name == "STARTED" and bool(getattr(pc, "do_listen", False)):
                    return True

            return False
        except Exception:
            return False

    def _note_fcm_deferral(self, now_mono: float) -> None:
        """Advance a quiet escalation timeline while FCM is not ready.

        FIX: Use less aggressive log levels to reduce log spam (#124).
        Emits at most:
            - one INFO after ~2 minutes (was WARNING after 60s)
            - one WARNING after ~5 minutes (was ERROR after 300s)
        Resets when readiness returns.
        """
        if self._fcm_defer_started_mono == 0.0:
            self._fcm_defer_started_mono = now_mono
            self._fcm_last_stage = 0
            self._set_fcm_status(
                FcmStatus.DEGRADED,
                reason="Push transport not ready; awaiting connection",
            )
            return
        elapsed = now_mono - self._fcm_defer_started_mono
        # Stage 1: After 2 minutes, log at INFO level (not WARNING)
        if elapsed >= 120 and self._fcm_last_stage < 1:
            self._fcm_last_stage = 1
            _LOGGER.info(
                "Push transport connection taking longer than expected (2 min). "
                "Push updates may be delayed, but polling continues."
            )
            self._set_fcm_status(
                FcmStatus.DEGRADED,
                reason="Push transport waiting for connection (2 min elapsed)",
            )
        # Stage 2: After 5 minutes, log at WARNING level (not ERROR)
        if elapsed >= 300 and self._fcm_last_stage < 2:
            self._fcm_last_stage = 2
            _LOGGER.warning(
                "Push transport not connected after 5 minutes. "
                "Check network connectivity and credentials if this persists."
            )
            self._set_fcm_status(
                FcmStatus.DISCONNECTED,
                reason="Push transport not connected after prolonged wait",
            )

    def _clear_fcm_deferral(self) -> None:
        """Clear the escalation timeline once FCM becomes ready (log once)."""
        if self._fcm_defer_started_mono:
            _LOGGER.info("FCM/push is ready; resuming scheduled polling.")
        self._fcm_defer_started_mono = 0.0
        self._fcm_last_stage = 0
        self._set_fcm_status(FcmStatus.CONNECTED)

    # -------------------- Poll timing prediction --------------------
    def _get_predicted_poll_time(self) -> float | None:
        """Predict the earliest next update time based on device histories."""

        history_store = getattr(self, "_device_update_history", None)
        if not history_store:
            return None

        predictions: list[float] = []

        for history in history_store.values():
            if len(history) < 2:
                continue

            intervals = [
                history[idx + 1] - history[idx] for idx in range(len(history) - 1)
            ]
            avg_interval = mean(intervals)

            if len(intervals) >= 2 and stdev(intervals) > 300:
                continue

            predictions.append(history[-1] + avg_interval)

        if not predictions:
            return None

        return min(predictions)

    # -------------------- Push transport error handling --------------------
    def _note_push_transport_problem(
        self, cooldown_s: int = 90
    ) -> None:
        """Enter a temporary cooldown after a push transport failure to avoid spamming.

        Args:
            cooldown_s: The duration of the cooldown in seconds.
        """
        self._push_cooldown_until = time.monotonic() + cooldown_s
        self._push_ready_memo = False
        _LOGGER.debug(
            "Entering push cooldown for %ss after transport failure", cooldown_s
        )
        self._set_fcm_status(
            FcmStatus.DEGRADED,
            reason=f"Push transport recovering from error (cooldown {cooldown_s}s)",
        )

    def force_poll_due(self) -> None:
        """Force the next poll to be due immediately (no private access required externally)."""
        effective_interval = max(self.location_poll_interval, self.min_poll_interval)
        # Move the baseline back so that (now - _last_poll_mono) >= effective_interval
        self._last_poll_mono = time.monotonic() - float(effective_interval)

    # ---------------------------- HA Coordinator ----------------------------
    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Provide cached device data; trigger background poll if due.

        Discovery semantics:
        - Refresh the **full** lightweight device list (no executor) on a paced interval
          and reuse the cached list between refreshes.
        - Update presence and metadata caches for **all** devices.
        - The published snapshot (`self.data`) contains **all** devices (for dynamic entity creation).
        - The sequential **polling cycle** polls devices that are enabled in HA's Device Registry
          **for this config entry** and not explicitly ignored in integration options.

        Returns:
            A list of dictionaries, where each dictionary represents a device's state.

        Raises:
            ConfigEntryAuthFailed: If authentication fails during device list fetching.
            UpdateFailed: For other transient or unexpected errors.
        """
        try:
            # Check for fatal FCM errors (for example, 404/401 during registration) to trigger re-auth
            # FIX: Only trigger re-auth after multiple consecutive failures (#114)
            entry = self.config_entry
            runtime = getattr(entry, "runtime_data", None)
            fcm_receiver = getattr(runtime, "fcm_receiver", None)

            fatal_error: str | None = None
            if fcm_receiver is not None:
                fatal_by_entry = getattr(fcm_receiver, "_fatal_errors", None)
                entry_id = self._entry_id()

                if isinstance(fatal_by_entry, Mapping) and entry_id:
                    fatal_error = fatal_by_entry.get(entry_id)

            if isinstance(fatal_error, str) and fatal_error:
                # Check if this is a fatal auth error that should fail immediately
                # (e.g., 404/401 with credentials issues - no point retrying)
                is_fatal_auth = _is_fatal_fcm_auth_error_impl(fatal_error)

                if is_fatal_auth:
                    # Fatal auth errors - fail immediately, no retry
                    _LOGGER.error(
                        "Fatal FCM authentication error: %s. Triggering re-authentication.",
                        fatal_error,
                    )
                    self._set_auth_state(failed=True, reason=fatal_error)
                    raise ConfigEntryAuthFailed(fatal_error)

                # Track consecutive FCM errors for transient issues
                if fatal_error != self._fcm_last_error:
                    # New error type, reset counter
                    self._fcm_error_count = 1
                    self._fcm_last_error = fatal_error
                else:
                    self._fcm_error_count += 1

                # Only trigger re-auth after _FCM_ERROR_RETRY_THRESHOLD consecutive failures
                if self._fcm_error_count >= _FCM_ERROR_RETRY_THRESHOLD:
                    _LOGGER.error(
                        "FCM error persisted after %d attempts: %s. Triggering re-authentication.",
                        self._fcm_error_count,
                        fatal_error,
                    )
                    self._set_auth_state(failed=True, reason=fatal_error)
                    raise ConfigEntryAuthFailed(fatal_error)
                else:
                    _LOGGER.warning(
                        "FCM error detected (%d/%d): %s. Will retry before triggering re-auth.",
                        self._fcm_error_count,
                        _FCM_ERROR_RETRY_THRESHOLD,
                        fatal_error,
                    )
            # No error - reset counter on successful check
            elif self._fcm_error_count > 0:
                _LOGGER.debug(
                    "FCM error cleared after %d attempts", self._fcm_error_count
                )
                self._fcm_error_count = 0
                self._fcm_last_error = None

            # One-time wait for FCM on first run.
            # FIX: Better user feedback during FCM initialization (#116)
            if not self._startup_complete:
                fcm_evt = getattr(self, "fcm_ready_event", None)
                if isinstance(fcm_evt, asyncio.Event) and not fcm_evt.is_set():
                    _LOGGER.info(
                        "Waiting for push notification service (FCM)... "
                        "This may take up to 15 seconds on first start."
                    )
                    try:
                        # Wait in 5-second increments with progress logging
                        for attempt in range(3):
                            try:
                                await asyncio.wait_for(fcm_evt.wait(), timeout=5.0)
                                _LOGGER.info("Push notification service is ready.")
                                break
                            except TimeoutError:
                                if attempt < 2:
                                    _LOGGER.debug(
                                        "FCM not ready yet, waiting... (%d/3)",
                                        attempt + 1,
                                    )
                        else:
                            # All 3 attempts exhausted
                            _LOGGER.warning(
                                "Push notification service not ready after 15s; "
                                "continuing in degraded mode. Push updates may be delayed."
                            )
                    except Exception as fcm_wait_err:
                        _LOGGER.debug("FCM wait error: %s", fcm_wait_err)
                self._startup_complete = True

            fcm_now_ready = self._is_fcm_ready_soft()
            if fcm_now_ready:
                self._set_fcm_status(FcmStatus.CONNECTED)
                # FIX: Clear deferral state as soon as FCM comes back, regardless
                # of whether a poll is due.  Without this, a forced poll during
                # an outage would update _last_poll_mono and the deferral would
                # linger until the next due window (up to effective_interval).
                if self._fcm_defer_started_mono:
                    self._clear_fcm_deferral()
                    # Trigger an immediate poll so we don't wait the full
                    # interval after a prolonged outage.
                    self.force_poll_due()
            elif self._fcm_last_stage < 2:
                self._set_fcm_status(
                    FcmStatus.DEGRADED,
                    reason="Push transport not ready; continuing with cached data",
                )

            list_check_mono = time.monotonic()
            list_due = (
                list_check_mono - self._last_list_poll_mono
            ) >= DEVICE_LIST_POLL_INTERVAL
            force_list_refresh = self._force_device_list_refresh
            if force_list_refresh:
                self._force_device_list_refresh = False
                force_reason = self._force_device_list_reason
                self._force_device_list_reason = None
                use_cached_list = False
                self._last_list_poll_mono = 0.0
                _LOGGER.debug(
                    "[%s] Device list refresh forced%s",
                    self._entry_id() or "unknown",
                    f" ({force_reason})" if force_reason else "",
                )
            else:
                use_cached_list = not list_due and bool(self._last_device_list)

            filtered_devices: list[dict[str, Any]]
            if use_cached_list:
                filtered_devices = list(self._last_device_list)
                self._set_api_status(ApiStatus.OK)
                _LOGGER.debug(
                    "Skipping device list refresh (next in %.0fs); using cached list (%d devices)",
                    max(
                        0.0,
                        DEVICE_LIST_POLL_INTERVAL
                        - (list_check_mono - self._last_list_poll_mono),
                    ),
                    len(filtered_devices),
                )
            else:
                # 1) Fetch the lightweight FULL device list using the native async API
                payload = await self.api.async_get_basic_device_list()

                # Success path: if we were in an auth error state, clear it now.
                self._set_auth_state(failed=False)
                self._set_api_status(ApiStatus.OK)

                # Normalize payloads and filter/dedupe devices using pure helpers
                device_candidates = _normalize_device_list_impl(payload)
                filtered_devices, seen_ids = _filter_and_dedupe_impl(device_candidates)

                # Log skipped devices for debugging (pure helper doesn't log)
                _logged_ids: set[str] = set()
                for candidate in device_candidates:
                    if not isinstance(candidate, Mapping):
                        continue
                    dev_id_raw = candidate.get("id")
                    if not isinstance(dev_id_raw, str) or not dev_id_raw.strip():
                        _LOGGER.debug("Skipping device without valid id: %r", candidate)
                        continue
                    dev_id = dev_id_raw.strip()
                    if dev_id in _logged_ids:
                        _LOGGER.debug(
                            "Skipping duplicate device entry for id=%s", dev_id
                        )
                    _logged_ids.add(dev_id)

                # Minimal hardening against false empties (keep prior behaviour)
                if not filtered_devices:
                    self._empty_list_streak += 1
                    # Use helper to decide if we should defer the empty list
                    if _should_defer_empty_list_impl(
                        self._empty_list_streak,
                        _EMPTY_LIST_QUORUM,
                        bool(self._last_device_list),
                    ):
                        # Defer clearing once; keep previous view stable.
                        _LOGGER.debug(
                            "Successful empty device list received (%d/%d). Deferring clear until quorum is met.",
                            self._empty_list_streak,
                            _EMPTY_LIST_QUORUM,
                        )
                        filtered_devices = list(self._last_device_list)
                    else:
                        _LOGGER.debug(
                            "Accepting empty device list after %d consecutive empties.",
                            self._empty_list_streak,
                        )
                        # Once accepted, forget any prior list so snapshot becomes empty below.
                        self._last_device_list = []
                        self._last_list_poll_mono = list_check_mono
                else:
                    # Non-empty result: reset streak and remember latest good list.
                    self._empty_list_streak = 0
                    self._last_device_list = list(filtered_devices)
                    self._last_list_poll_mono = list_check_mono

            # Presence TTL derives from the effective poll cadence
            effective_interval = max(
                self.location_poll_interval, self.min_poll_interval
            )
            self._presence_ttl_s = _calculate_presence_ttl_impl(effective_interval, 120)
            now_mono = time.monotonic()

            predicted_target = self._get_predicted_poll_time()
            predictive_due = False
            predictive_block = False
            if predicted_target is not None:
                wall_now = time.time()
                time_until = predicted_target - wall_now

                if 0 < time_until <= effective_interval:
                    delay = time_until + _PREDICTION_BUFFER_S
                    _LOGGER.debug(
                        "Predictive polling: update expected in %.1fs; scheduling retry in %.1fs (buffer=%ss)",
                        time_until,
                        delay,
                        _PREDICTION_BUFFER_S,
                    )
                    self._schedule_short_retry(delay)
                    predictive_block = True
                elif time_until <= 0:
                    _LOGGER.debug(
                        "Predictive polling: update overdue by %.1fs; polling now if limits allow.",
                        abs(time_until),
                    )
                    predictive_due = True

            # Cold-start guard: if the very first seen list is empty, treat it as transient
            if not filtered_devices and self._last_nonempty_wall == 0.0:
                raise UpdateFailed(
                    "Cold start: empty device list; treating as transient."
                )

            # Maintain owner index for FCM fallback routing (entry-scoped).
            self._sync_owner_index(filtered_devices)

            ignored = self._get_ignored_set()

            # Record presence timestamps from the full list (unfiltered by ignore)
            if filtered_devices:
                for dev in filtered_devices:
                    dev_id = dev["id"]
                    self._present_last_seen[dev_id] = now_mono
                # Keep a diagnostics-only set mirroring the latest non-empty list
                self._present_device_ids = {dev["id"] for dev in filtered_devices}
                self._last_nonempty_wall = now_mono
            # If the list is empty, leave _present_last_seen untouched; TTL will decide availability.

            # 2) Update internal name/capability caches for ALL devices
            name_cache = self._ensure_device_name_cache()
            for dev in filtered_devices:
                dev_id = dev["id"]
                raw_name = dev.get("name")
                if isinstance(raw_name, str) and raw_name.strip():
                    name_cache[dev_id] = raw_name
                elif dev_id not in name_cache:
                    name_cache[dev_id] = dev_id

                # Normalize and cache the "can ring" capability
                if "can_ring" in dev:
                    can_ring = bool(dev.get("can_ring"))
                    slot = self._device_caps.setdefault(dev_id, {})
                    slot["can_ring"] = can_ring

            # 2.2) Seed the location cache with list-provided coordinates when available
            for dev in filtered_devices:
                dev_id = dev["id"]
                if dev_id in ignored:
                    continue

                lat = dev.get("latitude")
                lon = dev.get("longitude")
                if lat is None or lon is None:
                    continue

                if not self._normalize_coords(dev, device_label=dev_id):
                    continue

                cache_seed = dict(dev)

                # Avoid poisoning the cache with explicit None accuracy values so
                # stationary clamps do not overwrite fresher accuracy readings.
                if cache_seed.get("accuracy") is None:
                    cache_seed.pop("accuracy", None)

                self.update_device_cache(dev_id, cache_seed)

            # 2.5) Ensure Device Registry entries exist (service device + end-devices, namespaced)
            self._ensure_service_device_exists()
            created = self._ensure_registry_for_devices(filtered_devices, ignored)
            if created:
                _LOGGER.debug(
                    "Device Registry ensured/updated for %d device(s).", created
                )

            # 3) Decide whether to trigger a poll cycle (monotonic clock)
            # Build list of devices to POLL:
            # Poll devices that have at least one enabled DR entry for this config entry;
            # if a device has no DR entry yet, include it to allow initial discovery.
            devices_to_poll: list[dict[str, Any]] = []
            for dev in filtered_devices:
                dev_id = dev["id"]
                if dev_id in ignored:
                    continue
                if (
                    dev_id in self._enabled_poll_device_ids
                    or dev_id not in self._devices_with_entry
                ):
                    devices_to_poll.append(dev)

            # Apply per-device poll cooldowns
            if self._device_poll_cooldown_until and devices_to_poll:
                devices_to_poll = [
                    d
                    for d in devices_to_poll
                    if now_mono >= self._device_poll_cooldown_until.get(d["id"], 0.0)
                ]

            # Cold start detection: force immediate poll on first install when devices have no location data
            is_cold_start = bool(
                self._last_poll_mono == 0.0
                and filtered_devices
                and not any(
                    self._device_location_data.get(dev["id"])
                    for dev in filtered_devices
                )
            )

            elapsed_since_poll = now_mono - self._last_poll_mono
            hard_limit_passed = elapsed_since_poll >= self.min_poll_interval

            # Use helper to determine if poll cycle is due
            due = _is_poll_cycle_due_impl(
                elapsed=elapsed_since_poll,
                effective_interval=effective_interval,
                predictive_block=predictive_block,
                predictive_due=predictive_due,
                hard_limit_passed=hard_limit_passed,
                is_cold_start=is_cold_start,
            )
            if due and not self._is_polling and devices_to_poll:
                force_poll = False
                fcm_ready = self._is_fcm_ready_soft()
                if not fcm_ready:
                    # No baseline jump; schedule a short retry and escalate politely.
                    self._note_fcm_deferral(now_mono)
                    defer_started = self._fcm_defer_started_mono or 0.0
                    if defer_started:
                        elapsed = now_mono - defer_started
                        if elapsed >= _PUSH_NOT_READY_TIMEOUT_S:
                            force_poll = True
                        else:
                            self._schedule_short_retry(
                                min(5.0, effective_interval / 2.0)
                            )
                    else:
                        self._schedule_short_retry(min(5.0, effective_interval / 2.0))
                elif self._fcm_defer_started_mono:
                    self._clear_fcm_deferral()

                if fcm_ready or force_poll:
                    if force_poll:
                        _LOGGER.warning(
                            "Push transport unavailable for %ds; forcing poll cycle.",
                            _PUSH_NOT_READY_TIMEOUT_S,
                        )
                    _LOGGER.debug(
                        "Scheduling background polling cycle (devices=%d, interval=%ds)",
                        len(devices_to_poll),
                        effective_interval,
                    )
                    self.hass.async_create_task(
                        self._async_start_poll_cycle(devices_to_poll, force=force_poll),
                        name=f"{DOMAIN}.poll_cycle",
                    )
            else:
                _LOGGER.debug(
                    "Poll not due (elapsed=%.1fs/%ss) or already running=%s",
                    now_mono - self._last_poll_mono,
                    effective_interval,
                    self._is_polling,
                )

            # 4) Build data snapshot for devices visible to the user (ignore-filter applied)
            visible_devices = [
                dev for dev in filtered_devices if dev["id"] not in ignored
            ]
            for dev in visible_devices:
                dev_id = dev["id"]
                cached_name = name_cache.get(dev_id)
                name = dev.get("name")
                if cached_name and (not isinstance(name, str) or not name.strip()):
                    dev["name"] = cached_name
            self._refresh_subentry_index(visible_devices)
            snapshot = await self._async_build_device_snapshot_with_fallbacks(
                visible_devices
            )
            self._store_subentry_snapshots(snapshot)

            # 4.5) Close the initial discovery window once we have a non-empty full list
            if not self._initial_discovery_done and filtered_devices:
                self._initial_discovery_done = True
                _LOGGER.info(
                    "Initial discovery window closed; newly discovered devices will be created disabled by default."
                )

            _LOGGER.debug(
                "Returning %d device entries; next poll in ~%ds",
                len(snapshot),
                int(
                    max(
                        0,
                        effective_interval - (time.monotonic() - self._last_poll_mono),
                    )
                ),
            )
            return snapshot

        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed as auth_exc:
            # Surface up to HA to trigger re-auth flow; create Repairs issue & flag before bubbling up.
            reason = self._short_error_message(auth_exc)
            self._set_api_status(ApiStatus.REAUTH, reason=reason)
            self._set_auth_state(
                failed=True,
                reason=f"Auth failed while fetching device list: {reason}",
            )
            raise auth_exc
        except UpdateFailed as update_err:
            # Let pre-wrapped UpdateFailed bubble as-is after updating status
            self._set_api_status(
                ApiStatus.ERROR,
                reason=self._short_error_message(update_err),
            )
            raise
        except Exception as err:
            # Record and raise as UpdateFailed per coordinator contract
            self.note_error(err, where="_async_update_data")
            message = self._short_error_message(err)
            self._set_api_status(ApiStatus.ERROR, reason=message)
            _LOGGER.exception("Unexpected error during coordinator update")
            raise UpdateFailed(f"{type(err).__name__}: {err}") from err

    # ---------------------------- Polling Cycle -----------------------------
    async def _async_start_poll_cycle(
        self,
        devices: list[dict[str, Any]],
        *,
        force: bool = False,
    ) -> None:
        """Run a full sequential polling cycle in a background task.

        This runs with a lock to avoid overlapping cycles, updates the
        internal cache, and pushes snapshots at start and end.

        Throttling awareness:
        - If a device returns a crowdsourced location with `_report_hint` equal to
          "in_all_areas" (~10 min throttle) or "high_traffic" (~5 min throttle),
          we apply a per-device cooldown so subsequent polls avoid the throttled window.
          (See POPETS'25 for measured behaviour.)
        - The cooldown is at least the server minimum and at least one user poll interval.

        Args:
            devices: A list of device dictionaries to poll.
        """
        if not devices:
            return

        async with self._poll_lock:
            if self._is_polling:
                return

            # Double-check FCM readiness inside the lock to avoid a narrow race:
            # if readiness regressed between scheduling and execution, skip cleanly
            # unless we are explicitly forcing the cycle after a prolonged outage.
            if not self._is_fcm_ready_soft():
                if not force:
                    # No baseline jump; schedule a short retry and keep escalation ticking.
                    self._note_fcm_deferral(time.monotonic())
                    self._schedule_short_retry(5.0)
                    return
                _LOGGER.warning(
                    "Starting poll cycle without push transport; continuing in degraded mode."
                )
            elif self._fcm_defer_started_mono:
                # If we were deferring previously, clear the escalation timeline.
                self._clear_fcm_deferral()

            self._is_polling = True
            self.safe_update_metric("last_poll_start_mono", time.monotonic())
            _LOGGER.debug("Starting sequential poll of %d devices", len(devices))

            google_home_filter = self._get_google_home_filter()

            last_exception: Exception | None = None
            _any_device_got_data = False
            try:
                cycle_failed = False
                for idx, dev in enumerate(devices):
                    dev_id = dev["id"]
                    dev_name = dev.get("name", dev_id)
                    _LOGGER.debug(
                        "Sequential poll: requesting location for %s (%d/%d)",
                        dev_name,
                        idx + 1,
                        len(devices),
                    )

                    try:
                        # Protect API awaitable with timeout
                        location = await asyncio.wait_for(
                            self.api.async_get_device_location(dev_id, dev_name),
                            timeout=LOCATION_REQUEST_TIMEOUT_S,
                        )

                        # Success path: ensure any previous auth error is cleared
                        self._set_auth_state(failed=False)
                        # Reset transient auth failure counter on success
                        if self._consecutive_transient_auth_failures > 0:
                            _LOGGER.info(
                                "Location request succeeded; clearing %d transient auth failure(s).",
                                self._consecutive_transient_auth_failures,
                            )
                            self._consecutive_transient_auth_failures = 0
                            self._last_transient_auth_error = None

                        if not location:
                            _LOGGER.warning(
                                "No location data returned for %s", dev_name
                            )
                            continue

                        self._record_semantic_label(location, device_id=dev_id)
                        raw_semantic_name = (
                            location.get("semantic_name")
                            if isinstance(location.get("semantic_name"), str)
                            else None
                        )
                        semantic_replaced = False

                        cached_loc = self._device_location_data.get(dev_id)
                        is_replay = False
                        if isinstance(cached_loc, Mapping):
                            new_ts = _normalize_epoch_seconds(location.get("last_seen"))
                            old_ts = _normalize_epoch_seconds(
                                cached_loc.get("last_seen")
                            )
                            if (
                                new_ts is not None
                                and old_ts is not None
                                and new_ts == old_ts
                            ):
                                is_replay = True

                        location["is_replayed"] = is_replay
                        mapping_applied = self._apply_semantic_mapping(location)

                        # --- Apply Google Home filter (keep parity with FCM push path) ---
                        # Consume coordinate substitution from the filter when needed.
                        semantic_name = location.get("semantic_name")
                        if (
                            not mapping_applied
                            and not is_replay
                            and semantic_name
                            and google_home_filter is not None
                        ):
                            try:
                                (
                                    should_filter,
                                    replacement_attrs,
                                ) = google_home_filter.should_filter_detection(
                                    dev_id, semantic_name
                                )
                            except Exception as gf_err:
                                _LOGGER.debug(
                                    "Google Home filter error for %s: %s",
                                    dev_name,
                                    gf_err,
                                )
                            else:
                                if should_filter:
                                    _LOGGER.warning(
                                        "Filtering out Google Home spam detection for %s",
                                        dev_name,
                                    )
                                    continue
                                if replacement_attrs:
                                    prev_location = self._device_location_data.get(
                                        dev_id
                                    )
                                    keep_previous_precise = (
                                        self._should_preserve_precise_home_coordinates(
                                            prev_location, replacement_attrs
                                        )
                                    )

                                    location = dict(location)
                                    if (
                                        keep_previous_precise
                                        and prev_location is not None
                                    ):
                                        _LOGGER.debug(
                                            "Google Home filter: %s detected at '%s', preserving previous precise coordinates",
                                            dev_name,
                                            semantic_name,
                                        )
                                        location["latitude"] = prev_location["latitude"]
                                        location["longitude"] = prev_location[
                                            "longitude"
                                        ]
                                        location["accuracy"] = prev_location["accuracy"]
                                    else:
                                        _LOGGER.info(
                                            "Google Home filter: %s detected at '%s', substituting with Home coordinates",
                                            dev_name,
                                            semantic_name,
                                        )
                                        if (
                                            "latitude" in replacement_attrs
                                            and "longitude" in replacement_attrs
                                        ):
                                            location["latitude"] = (
                                                replacement_attrs.get("latitude")
                                            )
                                            location["longitude"] = (
                                                replacement_attrs.get("longitude")
                                            )
                                        if (
                                            "radius" in replacement_attrs
                                            and replacement_attrs.get("radius")
                                            is not None
                                        ):
                                            location["accuracy"] = (
                                                replacement_attrs.get("radius")
                                            )
                                    # Clear semantic name so HA Core's zone engine determines the final state.
                                    location["semantic_name"] = None
                                    semantic_replaced = True
                        location.pop("is_replayed", None)
                        # ------------------------------------------------------------------

                        if raw_semantic_name and not semantic_replaced:
                            location["semantic_name"] = raw_semantic_name

                        # If we only got a semantic location, preserve previous coordinates.
                        # Use Phase 11 helper for the check
                        from .helpers.polling import (
                            calculate_location_age_hours,
                            get_age_log_level,
                            should_preserve_previous_coordinates,
                        )

                        if should_preserve_previous_coordinates(location):
                            prev = self._device_location_data.get(dev_id, {})
                            if prev:
                                location["latitude"] = prev.get("latitude")
                                location["longitude"] = prev.get("longitude")
                                location["accuracy"] = prev.get("accuracy")
                                location["status"] = (
                                    "Semantic location; preserving previous coordinates"
                                )

                        # Validate/normalize coordinates (and accuracy if present).
                        clear_metadata_only = (
                            location.get("latitude") is not None
                            or location.get("longitude") is not None
                        ) and location.get("metadata_only") is not True
                        self._persist_anchor_metadata(
                            dev_id, location, clear_metadata_only=clear_metadata_only
                        )
                        if not self._normalize_coords(location, device_label=dev_name):
                            if not location.get("semantic_name"):
                                _LOGGER.warning(
                                    "No location data (coordinates or semantic name) available for %s in this update.",
                                    dev_name,
                                )
                            # Nothing to commit/update in cache
                            # Strip any internal hint before dropping to avoid accidental exposure
                            location.pop("_report_hint", None)
                            continue

                        # Sanitize invariants + enrich fields (label, utc-string)
                        location = _sanitize_decoder_row(location)

                        if not self._apply_weighted_location_fusion(dev_id, location):
                            continue

                        # Skip redundant fusion inside update_device_cache when reusing this payload.
                        location["_fusion_preapplied"] = True

                        # Age diagnostics (informational) - use Phase 11 helpers
                        wall_now = time.time()
                        last_seen = location.get("last_seen", 0)
                        age_hours = calculate_location_age_hours(last_seen, wall_now)
                        if age_hours is not None:
                            log_level, should_log = get_age_log_level(age_hours)
                            if should_log and log_level == "info":
                                _LOGGER.info(
                                    "Using old location data for %s (age=%.1fh)",
                                    dev_name,
                                    age_hours,
                                )
                            elif should_log and log_level == "debug":
                                _LOGGER.debug(
                                    "Using location data for %s (age=%.1fh)",
                                    dev_name,
                                    age_hours,
                                )

                        # Commit via the shared cache helper when available;
                        # test doubles may stub this method, so fall back to a
                        # minimal cache write in that case.
                        from .main import GoogleFindMyCoordinator as _CoordinatorClass

                        helper = getattr(self.update_device_cache, "__func__", None)
                        if helper is _CoordinatorClass.update_device_cache:
                            self.update_device_cache(dev_id, location)
                        else:
                            location.pop("_fusion_preapplied", None)
                            location.pop("_report_hint", None)
                            location.setdefault("last_updated", wall_now)
                            merged_location = self._merge_with_existing_cache_row(
                                dev_id, location
                            )
                            self._device_location_data[dev_id] = merged_location

                        self.increment_stat("polled_updates")
                        self._consecutive_timeouts = 0
                        _any_device_got_data = True

                        # Immediate per-device update for more responsive UI during long poll cycles.
                        self.push_updated([dev_id])

                    except TimeoutError as terr:
                        if self.is_fcm_connected:
                            _LOGGER.warning(
                                "Poll timed out for %s (FCM connected); ignoring error to keep status healthy.",
                                dev_name,
                            )
                            self.increment_stat("timeouts")
                        else:
                            _LOGGER.info(
                                "Location request timed out for %s after %s seconds",
                                dev_name,
                                LOCATION_REQUEST_TIMEOUT_S,
                            )
                            self.increment_stat("timeouts")
                            self._consecutive_timeouts += 1
                            cycle_failed = True
                            self.note_error(terr, where="poll_timeout", device=dev_name)
                            if last_exception is None:
                                last_exception = UpdateFailed(
                                    f"Location request timed out for {dev_name}"
                                )
                                last_exception.__cause__ = terr
                    except SpotAuthPermanentError as auth_err:
                        _LOGGER.warning(
                            "Authentication failed for %s; triggering reauth flow.",
                            dev_name,
                        )
                        self._set_auth_state(
                            failed=True,
                            reason=f"Auth failed during poll for {dev_name}: session invalid",
                        )
                        cycle_failed = True
                        self._last_poll_result = "failed"
                        self._consecutive_timeouts = 0
                        reauth_exc = ConfigEntryAuthFailed(
                            "Google session invalid; re-authentication required"
                        )
                        last_exception = reauth_exc
                        raise reauth_exc from auth_err
                    except SpotApiEmptyResponseError:
                        _LOGGER.warning(
                            "Authentication failed for %s; triggering reauth flow.",
                            dev_name,
                        )
                        self._set_auth_state(
                            failed=True,
                            reason=f"Auth failed during poll for {dev_name}: session invalid",
                        )
                        cycle_failed = True
                        self._last_poll_result = "failed"
                        self._consecutive_timeouts = 0
                        reauth_exc = ConfigEntryAuthFailed(
                            "Google session invalid; re-authentication required"
                        )
                        last_exception = reauth_exc
                        raise reauth_exc
                    except NovaAuthPermanentError as perm_err:
                        # Permanent auth failure (AAS token invalid) - immediate reauth
                        _LOGGER.error(
                            "Permanent authentication failure for %s: %s. Re-authentication required.",
                            dev_name,
                            perm_err,
                        )
                        self._set_auth_state(
                            failed=True,
                            reason=f"Permanent auth failure for {dev_name}: credentials invalid",
                        )
                        cycle_failed = True
                        self._last_poll_result = "failed"
                        self._consecutive_timeouts = 0
                        self._consecutive_transient_auth_failures = 0
                        reauth_exc = ConfigEntryAuthFailed(
                            "Google credentials invalid; re-authentication required"
                        )
                        last_exception = reauth_exc
                        raise reauth_exc from perm_err
                    except NovaAuthError as transient_err:
                        # Transient auth failure - may self-heal in subsequent poll cycles.
                        # Only trigger reauth after multiple consecutive failures.
                        self._consecutive_transient_auth_failures += 1
                        self._last_transient_auth_error = str(transient_err)

                        if (
                            self._consecutive_transient_auth_failures
                            >= _MAX_TRANSIENT_AUTH_FAILURES
                        ):
                            _LOGGER.error(
                                "Transient auth failure for %s persisted across %d poll cycles: %s. "
                                "Triggering re-authentication.",
                                dev_name,
                                self._consecutive_transient_auth_failures,
                                transient_err,
                            )
                            self._set_auth_state(
                                failed=True,
                                reason=f"Auth failed for {dev_name} after {self._consecutive_transient_auth_failures} attempts",
                            )
                            cycle_failed = True
                            self._last_poll_result = "failed"
                            self._consecutive_timeouts = 0
                            reauth_exc = ConfigEntryAuthFailed(
                                f"Authentication failed after {self._consecutive_transient_auth_failures} attempts; re-authentication required"
                            )
                            last_exception = reauth_exc
                            raise reauth_exc from transient_err

                        # Not yet at threshold - log warning and continue to next device
                        _LOGGER.warning(
                            "Transient auth failure for %s (%d/%d): %s. "
                            "Will retry in next poll cycle.",
                            dev_name,
                            self._consecutive_transient_auth_failures,
                            _MAX_TRANSIENT_AUTH_FAILURES,
                            transient_err,
                        )
                        cycle_failed = True
                        if last_exception is None:
                            last_exception = transient_err
                        continue  # Try next device instead of aborting entire cycle
                    except ConfigEntryAuthFailed as auth_exc:
                        # Mark auth failures to HA; abort remaining devices by re-raising.
                        self._set_auth_state(
                            failed=True,
                            reason=f"Auth failed during poll for {dev_name}: {auth_exc}",
                        )
                        cycle_failed = True
                        self._last_poll_result = "failed"
                        self._consecutive_timeouts = 0
                        last_exception = auth_exc
                        raise
                    except Exception as err:
                        _LOGGER.error(
                            "Failed to get location for %s: %s", dev_name, err
                        )
                        cycle_failed = True
                        self._consecutive_timeouts = 0
                        self.note_error(err, where="poll_exception", device=dev_name)
                        if last_exception is None:
                            last_exception = err

                    # Inter-device delay (except after the last one)
                    if idx < len(devices) - 1 and self.device_poll_delay > 0:
                        await asyncio.sleep(self.device_poll_delay)

                _LOGGER.debug("Completed polling cycle for %d devices", len(devices))
            finally:
                # Update scheduling baseline and clear flag, then push end snapshot.
                # Always advance the poll baseline to prevent high-frequency
                # forced-poll loops that bypass min_poll_interval.  When push
                # transport reconnects, push_updated() resets the baseline so
                # there is no "dead zone" risk.
                if force and not _any_device_got_data:
                    _LOGGER.debug(
                        "Forced poll returned no data; scheduling retry "
                        "after min_poll_interval (%ds).",
                        self.min_poll_interval,
                    )
                    self._schedule_short_retry(self.min_poll_interval)
                self._last_poll_mono = time.monotonic()
                self._is_polling = False
                self.safe_update_metric("last_poll_end_mono", time.monotonic())
                if cycle_failed:
                    self._last_poll_result = "failed"
                else:
                    self._last_poll_result = "success"
                # Always publish the full snapshot so cached devices remain visible
                ignored = self._get_ignored_set()
                # Use the latest remembered full list; filter ignored
                visible_devices = [
                    d
                    for d in (self._last_device_list or [])
                    if d.get("id") not in ignored
                ]
                end_snapshot = self._build_snapshot_from_cache(
                    visible_devices, wall_now=time.time()
                )
                self.async_set_updated_data(end_snapshot)
                self._schedule_eid_resolver_refresh()
                if last_exception:
                    self.async_set_update_error(last_exception)
