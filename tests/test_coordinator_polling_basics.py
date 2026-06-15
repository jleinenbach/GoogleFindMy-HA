# tests/test_coordinator_polling_basics.py
"""Branch-coverage tests for the simple ``PollingOperations`` mixin methods.

PR 1.A.3 (Phase 2, AP-B). Scope: ``coordinator/polling.py`` simple helpers
(``_set_api_status``, ``_set_fcm_status``, ``api_status``, ``fcm_status``,
``is_fcm_connected``, ``consecutive_timeouts``, ``last_poll_result``,
``_is_on_hass_loop``, ``_run_on_hass_loop``,
``_compute_type_cooldown_seconds``, ``_apply_report_type_cooldown``,
``is_polling``, ``get_fcm_acquire_duration_seconds``,
``get_last_poll_duration_seconds``, ``_clear_fcm_deferral``,
``_nudge_fcm_supervisor``, ``_get_predicted_poll_time``,
``_note_push_transport_problem``, ``force_poll_due``). The complex async
methods (``_async_update_data``, ``_async_start_poll_cycle``,
``_handle_dr_event``, ``_dispatch_async_request_refresh``,
``_schedule_short_retry``, ``_is_fcm_ready_soft``, ``_note_fcm_deferral``)
remain out of scope here; they land in a later AP.

Aniche-style adequacy progression: Specification → Boundary → Structural.
:class:`PollingStub` (``tests.helpers.polling_mixin_stub``) seeds every
attribute ``_MixinBase`` declares and pre-mocks cross-mixin methods
(``_entry_id``, ``get_metric``, ``_get_duration``,
``async_set_updated_data``, ``_reindex_poll_targets_from_device_registry``).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator.helpers.stats import (
    ApiStatus,
    FcmStatus,
    StatusSnapshot,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.polling_mixin_stub import PollingStub, make_hass_stub

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def coord() -> PollingStub:
    """Return a default :class:`PollingStub` bound to a synthetic config entry."""

    entry = make_config_entry(entry_id="entry-xyz")
    return PollingStub(hass=make_hass_stub(), config_entry=entry)


# ---------------------------------------------------------------------------
# T1 ``_set_api_status``
# ---------------------------------------------------------------------------


class TestSetApiStatus:
    """B1: no change → early return; B2: change → update + notify;
    B3: ``async_set_updated_data`` raises → swallow."""

    def test_no_change_is_noop(self, coord: PollingStub) -> None:
        coord._api_status_state = ApiStatus.OK
        coord._api_status_reason = "stable"
        coord._api_status_changed_at = 12345.0
        coord._set_api_status(ApiStatus.OK, reason="stable")
        # No state mutation, no listener notification.
        assert coord._api_status_changed_at == 12345.0
        coord.async_set_updated_data.assert_not_called()

    def test_state_change_updates_and_notifies(self, coord: PollingStub) -> None:
        before = time.time()
        coord._set_api_status(ApiStatus.ERROR, reason="boom")
        assert coord._api_status_state == ApiStatus.ERROR
        assert coord._api_status_reason == "boom"
        assert coord._api_status_changed_at is not None
        assert coord._api_status_changed_at >= before
        coord.async_set_updated_data.assert_called_once_with(coord.data)

    def test_reason_change_only_still_notifies(self, coord: PollingStub) -> None:
        coord._api_status_state = ApiStatus.OK
        coord._api_status_reason = None
        coord._set_api_status(ApiStatus.OK, reason="hint")
        assert coord._api_status_reason == "hint"
        coord.async_set_updated_data.assert_called_once_with(coord.data)

    def test_set_updated_data_exception_is_swallowed(self, coord: PollingStub) -> None:
        coord.async_set_updated_data = MagicMock(side_effect=RuntimeError("early"))
        coord._set_api_status(ApiStatus.OK, reason="startup")
        # State persists despite the listener failure.
        assert coord._api_status_state == ApiStatus.OK
        assert coord._api_status_reason == "startup"


# ---------------------------------------------------------------------------
# T2 ``_set_fcm_status``
# ---------------------------------------------------------------------------


class TestSetFcmStatus:
    """B1: no change → early return; B2: change → update + notify;
    B3: ``async_set_updated_data`` raises → swallow."""

    def test_no_change_is_noop(self, coord: PollingStub) -> None:
        coord._fcm_status_state = FcmStatus.CONNECTED
        coord._fcm_status_reason = None
        coord._fcm_status_changed_at = 999.0
        coord._set_fcm_status(FcmStatus.CONNECTED, reason=None)
        assert coord._fcm_status_changed_at == 999.0
        coord.async_set_updated_data.assert_not_called()

    def test_state_change_updates_and_notifies(self, coord: PollingStub) -> None:
        coord._set_fcm_status(FcmStatus.DEGRADED, reason="slow")
        assert coord._fcm_status_state == FcmStatus.DEGRADED
        assert coord._fcm_status_reason == "slow"
        coord.async_set_updated_data.assert_called_once_with(coord.data)

    def test_set_updated_data_exception_is_swallowed(self, coord: PollingStub) -> None:
        coord.async_set_updated_data = MagicMock(side_effect=RuntimeError("early"))
        coord._set_fcm_status(FcmStatus.DISCONNECTED, reason="gone")
        assert coord._fcm_status_state == FcmStatus.DISCONNECTED


# ---------------------------------------------------------------------------
# T3 / T4 / T5 / T6 Property getters
# ---------------------------------------------------------------------------


class TestStatusProperties:
    """Snapshot dataclasses + scalar getters mirror the underlying attrs."""

    def test_api_status_snapshot(self, coord: PollingStub) -> None:
        coord._api_status_state = ApiStatus.OK
        coord._api_status_reason = "happy"
        coord._api_status_changed_at = 42.0
        snapshot = coord.api_status
        assert isinstance(snapshot, StatusSnapshot)
        assert snapshot.state == ApiStatus.OK
        assert snapshot.reason == "happy"
        assert snapshot.changed_at == 42.0

    def test_fcm_status_snapshot(self, coord: PollingStub) -> None:
        coord._fcm_status_state = FcmStatus.DEGRADED
        coord._fcm_status_reason = "slow"
        coord._fcm_status_changed_at = 1.5
        snapshot = coord.fcm_status
        assert isinstance(snapshot, StatusSnapshot)
        assert snapshot.state == FcmStatus.DEGRADED
        assert snapshot.reason == "slow"
        assert snapshot.changed_at == 1.5

    def test_is_fcm_connected_true(self, coord: PollingStub) -> None:
        coord._fcm_status_state = FcmStatus.CONNECTED
        assert coord.is_fcm_connected is True

    @pytest.mark.parametrize(
        "state",
        [FcmStatus.UNKNOWN, FcmStatus.DEGRADED, FcmStatus.DISCONNECTED, "other"],
    )
    def test_is_fcm_connected_false(self, coord: PollingStub, state: str) -> None:
        coord._fcm_status_state = state
        assert coord.is_fcm_connected is False

    def test_scalar_getters_read_through(self, coord: PollingStub) -> None:
        coord._consecutive_timeouts = 5
        coord._last_poll_result = "success"
        coord._is_polling = True
        assert coord.consecutive_timeouts == 5
        assert coord.last_poll_result == "success"
        assert coord.is_polling is True


# ---------------------------------------------------------------------------
# T7 ``_is_on_hass_loop``
# ---------------------------------------------------------------------------


class TestIsOnHassLoop:
    """B1: running loop equals hass.loop → True; B2: differs → False;
    B3: RuntimeError (no running loop) → False."""

    def test_returns_false_when_no_running_loop(self, coord: PollingStub) -> None:
        # No event loop is running in this sync test → RuntimeError → False.
        assert coord._is_on_hass_loop() is False

    async def test_returns_true_when_running_loop_matches(self) -> None:
        # ``asyncio_mode = "auto"`` (pyproject.toml) supplies the running loop;
        # avoid ``asyncio.run()`` here so the architecture guard
        # (tests/test_guard_asyncio_run_antipattern.py) stays satisfied.
        loop = asyncio.get_running_loop()
        stub = PollingStub(hass=make_hass_stub(loop=loop))
        assert stub._is_on_hass_loop() is True

    async def test_returns_false_when_running_loop_differs(self) -> None:
        # hass.loop is a MagicMock (different identity from the running loop),
        # so the equality check returns ``False`` even on the HA loop thread.
        stub = PollingStub(hass=make_hass_stub())
        assert stub._is_on_hass_loop() is False


# ---------------------------------------------------------------------------
# T8 ``_run_on_hass_loop``
# ---------------------------------------------------------------------------


class TestRunOnHassLoop:
    """B1: no kwargs → loop.call_soon_threadsafe(func, *args);
    B2: kwargs → loop.call_soon_threadsafe(functools.partial(...))."""

    def test_without_kwargs_passes_func_and_args(self, coord: PollingStub) -> None:
        captured: list[tuple[object, ...]] = []
        coord.hass.loop.call_soon_threadsafe = MagicMock(
            side_effect=lambda *args: captured.append(args)
        )

        def _noop(*_args: object) -> None:
            pass

        coord._run_on_hass_loop(_noop, "a", "b")
        assert len(captured) == 1
        assert captured[0][0] is _noop
        assert captured[0][1:] == ("a", "b")

    def test_with_kwargs_wraps_in_partial(self, coord: PollingStub) -> None:
        captured: list[tuple[object, ...]] = []
        coord.hass.loop.call_soon_threadsafe = MagicMock(
            side_effect=lambda *args: captured.append(args)
        )

        def _noop(*_args: object, **_kwargs: object) -> None:
            pass

        coord._run_on_hass_loop(_noop, 1, key="value")
        assert len(captured) == 1
        # functools.partial wraps the function; first positional must be callable.
        wrapped = captured[0][0]
        assert callable(wrapped)
        # Confirm the partial captured the right args/kwargs without invoking.
        assert getattr(wrapped, "args", ()) == (1,)
        assert getattr(wrapped, "keywords", {}) == {"key": "value"}


# ---------------------------------------------------------------------------
# T9 ``_compute_type_cooldown_seconds``
# ---------------------------------------------------------------------------


class TestComputeTypeCooldownSeconds:
    """B1: no hint → 0; B2: ``in_all_areas`` base; B3: ``high_traffic`` base;
    B4: unknown hint → 0; B5: ``max(base, effective_poll)``."""

    def test_none_hint_returns_zero(self, coord: PollingStub) -> None:
        assert coord._compute_type_cooldown_seconds(None) == 0

    def test_empty_hint_returns_zero(self, coord: PollingStub) -> None:
        assert coord._compute_type_cooldown_seconds("") == 0

    def test_in_all_areas_uses_10_minute_floor(self, coord: PollingStub) -> None:
        coord.location_poll_interval = 60
        assert coord._compute_type_cooldown_seconds("in_all_areas") == 10 * 60

    def test_high_traffic_uses_5_minute_floor(self, coord: PollingStub) -> None:
        coord.location_poll_interval = 60
        assert coord._compute_type_cooldown_seconds("high_traffic") == 5 * 60

    def test_unknown_hint_returns_zero(self, coord: PollingStub) -> None:
        assert coord._compute_type_cooldown_seconds("unknown") == 0

    def test_long_poll_interval_dominates_base(self, coord: PollingStub) -> None:
        coord.location_poll_interval = 30 * 60  # 30 min
        assert coord._compute_type_cooldown_seconds("in_all_areas") == 30 * 60

    def test_zero_poll_interval_clamped_to_one_minimum(
        self, coord: PollingStub
    ) -> None:
        coord.location_poll_interval = 0
        # base 10*60 still wins over effective_poll=max(1,0)=1
        assert coord._compute_type_cooldown_seconds("in_all_areas") == 10 * 60


# ---------------------------------------------------------------------------
# T10 ``_apply_report_type_cooldown``
# ---------------------------------------------------------------------------


class TestApplyReportTypeCooldown:
    """B1: zero seconds → skip; B2: new > prev → set;
    B3: new <= prev → no-op; B4: ``_compute_type_cooldown_seconds`` raises → swallow."""

    def test_zero_seconds_skips(self, coord: PollingStub) -> None:
        coord._apply_report_type_cooldown("dev-1", None)
        assert "dev-1" not in coord._device_poll_cooldown_until

    def test_new_deadline_writes_entry(self, coord: PollingStub) -> None:
        coord.location_poll_interval = 60
        before = time.monotonic()
        coord._apply_report_type_cooldown("dev-1", "in_all_areas")
        deadline = coord._device_poll_cooldown_until["dev-1"]
        assert deadline >= before + 10 * 60 - 1  # tolerate clock skew

    def test_existing_longer_deadline_kept(self, coord: PollingStub) -> None:
        coord.location_poll_interval = 60
        future = time.monotonic() + 9999
        coord._device_poll_cooldown_until["dev-1"] = future
        coord._apply_report_type_cooldown("dev-1", "high_traffic")
        assert coord._device_poll_cooldown_until["dev-1"] == future

    def test_compute_exception_is_swallowed(self, coord: PollingStub) -> None:
        original = coord._compute_type_cooldown_seconds
        try:
            coord._compute_type_cooldown_seconds = MagicMock(  # type: ignore[method-assign]
                side_effect=RuntimeError("boom")
            )
            coord._apply_report_type_cooldown("dev-1", "in_all_areas")
            assert "dev-1" not in coord._device_poll_cooldown_until
        finally:
            coord._compute_type_cooldown_seconds = original  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# T11 ``get_fcm_acquire_duration_seconds`` + ``get_last_poll_duration_seconds``
# ---------------------------------------------------------------------------


class TestDurationGetters:
    """B1: ``get_fcm_acquire_duration_seconds`` delegates to ``helpers.stats``;
    B2: ``get_last_poll_duration_seconds`` delegates to ``_get_duration``."""

    def test_get_fcm_acquire_duration_uses_recorded_metrics(
        self, coord: PollingStub
    ) -> None:
        coord.get_metric = MagicMock(side_effect=[10.0, 25.5])
        assert coord.get_fcm_acquire_duration_seconds() == pytest.approx(15.5)

    def test_get_fcm_acquire_duration_returns_none_when_missing(
        self, coord: PollingStub
    ) -> None:
        coord.get_metric = MagicMock(return_value=None)
        assert coord.get_fcm_acquire_duration_seconds() is None

    def test_get_last_poll_duration_delegates(self, coord: PollingStub) -> None:
        coord._get_duration = MagicMock(return_value=7.25)  # type: ignore[method-assign]
        assert coord.get_last_poll_duration_seconds() == 7.25
        coord._get_duration.assert_called_once_with(
            "last_poll_start_mono", "last_poll_end_mono"
        )


# ---------------------------------------------------------------------------
# T12 ``_clear_fcm_deferral``
# ---------------------------------------------------------------------------


class TestClearFcmDeferral:
    """B1: was deferred → log info + clear; B2: not deferred → silent clear."""

    def test_clears_state_when_previously_deferred(self, coord: PollingStub) -> None:
        coord._fcm_defer_started_mono = 100.0
        coord._fcm_last_stage = 2
        coord._degraded_mode_warned = True
        coord._clear_fcm_deferral()
        assert coord._fcm_defer_started_mono == 0.0
        assert coord._fcm_last_stage == 0
        assert coord._degraded_mode_warned is False
        assert coord._fcm_status_state == FcmStatus.CONNECTED

    def test_clear_when_not_deferred_still_sets_connected(
        self, coord: PollingStub
    ) -> None:
        coord._fcm_defer_started_mono = 0.0
        coord._fcm_status_state = FcmStatus.UNKNOWN
        coord._clear_fcm_deferral()
        assert coord._fcm_status_state == FcmStatus.CONNECTED


# ---------------------------------------------------------------------------
# T13 ``_nudge_fcm_supervisor``
# ---------------------------------------------------------------------------


class TestNudgeFcmSupervisor:
    """B1: no fcm receiver → silent; B2: receiver lacks ``nudge_retry`` → silent;
    B3: callable nudge → invoked with entry_id; B4: nudge raises → swallow."""

    def test_no_fcm_is_silent(self, coord: PollingStub) -> None:
        coord.hass.data = {DOMAIN: {}}
        # Should not raise.
        coord._nudge_fcm_supervisor()

    def test_missing_nudge_retry_is_silent(self, coord: PollingStub) -> None:
        receiver = MagicMock(spec_set=["other_attr"])
        receiver.other_attr = "x"
        coord.hass.data = {DOMAIN: {"fcm_receiver": receiver}}
        coord._nudge_fcm_supervisor()

    def test_callable_nudge_invoked_with_entry_id(self, coord: PollingStub) -> None:
        receiver = MagicMock()
        coord.hass.data = {DOMAIN: {"fcm_receiver": receiver}}
        coord._entry_id = MagicMock(return_value="entry-xyz")
        coord._nudge_fcm_supervisor()
        receiver.nudge_retry.assert_called_once_with("entry-xyz")

    def test_nudge_exception_is_swallowed(self, coord: PollingStub) -> None:
        receiver = MagicMock()
        receiver.nudge_retry = MagicMock(side_effect=RuntimeError("boom"))
        coord.hass.data = {DOMAIN: {"fcm_receiver": receiver}}
        coord._nudge_fcm_supervisor()


# ---------------------------------------------------------------------------
# T14 ``_get_predicted_poll_time``
# ---------------------------------------------------------------------------


class TestGetPredictedPollTime:
    """B1: no history store → None; B2: empty store → None;
    B3: histories with <2 entries skipped; B4: high stdev skipped;
    B5: returns ``min(predictions)``."""

    def test_no_history_attribute_returns_none(self, coord: PollingStub) -> None:
        # Remove the attribute so getattr returns None.
        del coord._device_update_history
        assert coord._get_predicted_poll_time() is None

    def test_empty_history_returns_none(self, coord: PollingStub) -> None:
        coord._device_update_history = {}
        assert coord._get_predicted_poll_time() is None

    def test_only_short_histories_returns_none(self, coord: PollingStub) -> None:
        # len(history) < 2 → no intervals computable.
        coord._device_update_history = {"dev-1": deque([100.0])}
        assert coord._get_predicted_poll_time() is None

    def test_steady_history_predicts_next_update(self, coord: PollingStub) -> None:
        # Three samples spaced 60s apart → avg interval 60s → next at 220.0.
        coord._device_update_history = {"dev-1": deque([100.0, 160.0, 220.0])}
        assert coord._get_predicted_poll_time() == pytest.approx(280.0)

    def test_chooses_earliest_prediction(self, coord: PollingStub) -> None:
        coord._device_update_history = {
            "early": deque([0.0, 30.0, 60.0]),  # next at 90.0
            "late": deque([100.0, 160.0, 220.0]),  # next at 280.0
        }
        assert coord._get_predicted_poll_time() == pytest.approx(90.0)

    def test_high_stdev_history_is_skipped(self, coord: PollingStub) -> None:
        # Intervals [10, 1000] → stdev far above 300 → device excluded.
        coord._device_update_history = {
            "noisy": deque([0.0, 10.0, 1010.0]),
            "calm": deque([100.0, 160.0, 220.0]),
        }
        assert coord._get_predicted_poll_time() == pytest.approx(280.0)

    def test_all_high_stdev_returns_none(self, coord: PollingStub) -> None:
        coord._device_update_history = {
            "noisy": deque([0.0, 10.0, 1010.0]),
        }
        assert coord._get_predicted_poll_time() is None


# ---------------------------------------------------------------------------
# T15 ``_note_push_transport_problem`` + ``force_poll_due``
# ---------------------------------------------------------------------------


class TestNotePushTransportProblem:
    """B1: default 90s cooldown applied; B2: explicit cooldown honored;
    B3: ``_push_ready_memo`` invalidated; B4: FCM status set to DEGRADED."""

    def test_default_cooldown_sets_state(self, coord: PollingStub) -> None:
        before = time.monotonic()
        coord._note_push_transport_problem()
        assert coord._push_cooldown_until >= before + 89  # tolerate skew
        assert coord._push_ready_memo is False
        assert coord._fcm_status_state == FcmStatus.DEGRADED

    def test_custom_cooldown_used(self, coord: PollingStub) -> None:
        before = time.monotonic()
        coord._note_push_transport_problem(cooldown_s=30)
        assert coord._push_cooldown_until >= before + 29
        assert coord._push_cooldown_until < before + 120


class TestForcePollDue:
    """B1: backshift baseline by effective interval."""

    def test_backshifts_last_poll_mono(self, coord: PollingStub) -> None:
        coord.location_poll_interval = 90
        coord.min_poll_interval = 60
        coord.force_poll_due()
        # _last_poll_mono = now - max(90, 60) = now - 90.
        elapsed_since_baseline = time.monotonic() - coord._last_poll_mono
        assert elapsed_since_baseline >= 90 - 0.5  # tolerate skew

    def test_uses_min_when_location_smaller(self, coord: PollingStub) -> None:
        coord.location_poll_interval = 10
        coord.min_poll_interval = 60
        coord.force_poll_due()
        elapsed_since_baseline = time.monotonic() - coord._last_poll_mono
        assert elapsed_since_baseline >= 60 - 0.5
