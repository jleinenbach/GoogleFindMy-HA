# tests/test_coordinator_polling_branches.py
"""Branch-coverage tests for :mod:`coordinator.polling` (Phase 4 AP-J).

Phase 2 AP-B covered the simple read-through mixin methods. This module
exercises the harder branch logic: loop-detection dispatch, coalesced retry
scheduling, device-registry event handling, 3-tier FCM-ready probing and the
escalation timeline. Class names use the ``Branches`` suffix to keep
disjoint from ``test_coordinator_polling_basics.py`` (CA-F6).

Scope (Phase 4 AP-J):
- ``_dispatch_async_request_refresh`` (5 branches)
- ``_schedule_short_retry`` (6 branches)
- ``_handle_dr_event`` (1 branch)
- ``_is_fcm_ready_soft`` (3-tier priority, ~12 branches)
- ``_note_fcm_deferral`` (4 stages)

Out of scope (Phase 5):
- ``_async_update_data``, ``_async_start_poll_cycle`` (full poll-cycle roundtrip)
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.coordinator.helpers.stats import FcmStatus
from tests.helpers.polling_branches_stub import PollingBranchesStub


@pytest.fixture
def coord() -> PollingBranchesStub:
    """Return a fresh :class:`PollingBranchesStub` with default scalars."""

    return PollingBranchesStub()


# ---------------------------------------------------------------------------
# _dispatch_async_request_refresh
# ---------------------------------------------------------------------------


class TestDispatchAsyncRequestRefreshBranches:
    """Loop detection, awaitable scheduling, exception swallowing."""

    def test_no_async_request_refresh_callable_returns(
        self, coord: PollingBranchesStub
    ) -> None:
        coord.async_request_refresh = None  # type: ignore[assignment]
        coord._is_on_hass_loop = MagicMock(return_value=True)

        coord._dispatch_async_request_refresh(task_name="t", log_context="ctx")

        coord.hass.async_create_task.assert_not_called()

    def test_on_loop_with_non_awaitable_result_skips_task_create(
        self, coord: PollingBranchesStub
    ) -> None:
        coord.async_request_refresh = MagicMock(return_value=None)
        coord._is_on_hass_loop = MagicMock(return_value=True)

        coord._dispatch_async_request_refresh(task_name="t", log_context="ctx")

        coord.async_request_refresh.assert_called_once_with()
        coord.hass.async_create_task.assert_not_called()

    def test_on_loop_with_awaitable_schedules_task(
        self, coord: PollingBranchesStub
    ) -> None:
        async def _awaitable() -> None:
            return None

        coro = _awaitable()
        coord.async_request_refresh = MagicMock(return_value=coro)
        coord._is_on_hass_loop = MagicMock(return_value=True)

        coord._dispatch_async_request_refresh(task_name="task-x", log_context="on-loop")

        coord.hass.async_create_task.assert_called_once_with(coro, name="task-x")
        coro.close()

    def test_on_loop_exception_swallowed_and_logged(
        self, coord: PollingBranchesStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        coord.async_request_refresh = MagicMock(side_effect=RuntimeError("boom"))
        coord._is_on_hass_loop = MagicMock(return_value=True)

        with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
            coord._dispatch_async_request_refresh(task_name="t", log_context="explode")

        assert "explode" in caplog.text
        coord.hass.async_create_task.assert_not_called()

    def test_off_loop_uses_run_on_hass_loop(self, coord: PollingBranchesStub) -> None:
        coord.async_request_refresh = MagicMock(return_value=None)
        coord._is_on_hass_loop = MagicMock(return_value=False)

        captured: list[Any] = []
        coord._run_on_hass_loop = lambda fn, *a, **kw: captured.append(fn)  # type: ignore[assignment]

        coord._dispatch_async_request_refresh(task_name="t", log_context="off")

        assert len(captured) == 1
        # The deferred callable still works: invoke it and verify no task scheduled.
        captured[0]()
        coord.async_request_refresh.assert_called_once_with()


# ---------------------------------------------------------------------------
# _schedule_short_retry
# ---------------------------------------------------------------------------


class TestScheduleShortRetryBranches:
    """Coalesce, delay clamp, callback dispatch."""

    def test_on_loop_uses_async_call_later(
        self, coord: PollingBranchesStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = object()
        fake_call_later = MagicMock(return_value=sentinel)
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.polling.async_call_later",
            fake_call_later,
        )
        coord._is_on_hass_loop = MagicMock(return_value=True)

        coord._schedule_short_retry(delay_s=3.0)

        fake_call_later.assert_called_once()
        args, _kwargs = fake_call_later.call_args
        assert args[0] is coord.hass
        assert args[1] == 3.0
        assert callable(args[2])
        assert coord._short_retry_cancel is sentinel

    def test_off_loop_routes_through_run_on_hass_loop(
        self, coord: PollingBranchesStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_call_later = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.polling.async_call_later",
            fake_call_later,
        )
        coord._is_on_hass_loop = MagicMock(return_value=False)

        captured: list[Any] = []
        coord._run_on_hass_loop = lambda fn, *a, **kw: captured.append(fn)  # type: ignore[assignment]

        coord._schedule_short_retry(delay_s=1.5)

        fake_call_later.assert_not_called()
        assert len(captured) == 1
        captured[0]()  # invoke the deferred scheduler
        fake_call_later.assert_called_once()

    def test_pending_cancel_called_before_new_schedule(
        self, coord: PollingBranchesStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prev_cancel = MagicMock()
        coord._short_retry_cancel = prev_cancel
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.polling.async_call_later",
            MagicMock(return_value=MagicMock()),
        )
        coord._is_on_hass_loop = MagicMock(return_value=True)

        coord._schedule_short_retry(delay_s=2.0)

        prev_cancel.assert_called_once_with()

    def test_pending_cancel_exception_swallowed(
        self, coord: PollingBranchesStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prev_cancel = MagicMock(side_effect=RuntimeError("nope"))
        coord._short_retry_cancel = prev_cancel
        new_handle = MagicMock()
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.polling.async_call_later",
            MagicMock(return_value=new_handle),
        )
        coord._is_on_hass_loop = MagicMock(return_value=True)

        coord._schedule_short_retry(delay_s=1.0)

        # Cancel raised, schedule must still proceed and overwrite the handle.
        assert coord._short_retry_cancel is new_handle

    def test_negative_delay_clamped_to_zero(
        self, coord: PollingBranchesStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_call_later = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.polling.async_call_later",
            fake_call_later,
        )
        coord._is_on_hass_loop = MagicMock(return_value=True)

        coord._schedule_short_retry(delay_s=-5.0)

        args, _ = fake_call_later.call_args
        assert args[1] == 0.0

    def test_callback_clears_handle_and_dispatches(
        self, coord: PollingBranchesStub, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured_cb: dict[str, Any] = {}

        def _capture(_hass: Any, _delay: float, cb: Any) -> Any:
            captured_cb["cb"] = cb
            return MagicMock()  # the cancel handle

        monkeypatch.setattr(
            "custom_components.googlefindmy.coordinator.polling.async_call_later",
            _capture,
        )
        coord._is_on_hass_loop = MagicMock(return_value=True)
        dispatch_mock = MagicMock()
        coord._dispatch_async_request_refresh = dispatch_mock  # type: ignore[assignment]

        coord._schedule_short_retry(delay_s=1.0)
        assert coord._short_retry_cancel is not None
        captured_cb["cb"](object())  # fake datetime arg

        assert coord._short_retry_cancel is None
        dispatch_mock.assert_called_once()
        _, kwargs = dispatch_mock.call_args
        assert kwargs["log_context"] == "short retry"


# ---------------------------------------------------------------------------
# _handle_dr_event
# ---------------------------------------------------------------------------


class TestHandleDrEventBranches:
    """Device-registry event triggers reindex + refresh dispatch."""

    async def test_reindex_and_dispatch_invoked(
        self, coord: PollingBranchesStub
    ) -> None:
        dispatch_mock = MagicMock()
        coord._dispatch_async_request_refresh = dispatch_mock  # type: ignore[assignment]
        event = MagicMock()

        await coord._handle_dr_event(event)

        coord._reindex_poll_targets_from_device_registry.assert_called_once_with()
        dispatch_mock.assert_called_once()
        _, kwargs = dispatch_mock.call_args
        assert kwargs["log_context"] == "device registry event"


# ---------------------------------------------------------------------------
# _is_fcm_ready_soft (3-tier priority)
# ---------------------------------------------------------------------------


class TestIsFcmReadySoftBranches:
    """API > receiver-flags > push-client heuristic; defensive returns."""

    def test_api_is_push_ready_true_short_circuits(
        self, coord: PollingBranchesStub
    ) -> None:
        coord.api = MagicMock(is_push_ready=MagicMock(return_value=True))
        # Even with no receiver, the API short-circuit wins.
        coord.hass.data = {}

        assert coord._is_fcm_ready_soft() is True

    def test_api_is_push_ready_false_short_circuits(
        self, coord: PollingBranchesStub
    ) -> None:
        coord.api = MagicMock(is_push_ready=MagicMock(return_value=False))

        assert coord._is_fcm_ready_soft() is False

    def test_api_raises_falls_through_to_receiver(
        self, coord: PollingBranchesStub
    ) -> None:
        coord.api = MagicMock(
            is_push_ready=MagicMock(side_effect=RuntimeError("api fail"))
        )
        fcm = MagicMock(spec_set=["is_ready", "_fatal_error", "_fatal_errors", "pc"])
        fcm.is_ready = True
        fcm._fatal_error = None
        fcm._fatal_errors = None
        fcm.pc = None
        coord.hass.data = {"googlefindmy": {"fcm_receiver": fcm}}

        assert coord._is_fcm_ready_soft() is True

    def test_no_fcm_receiver_returns_false(self, coord: PollingBranchesStub) -> None:
        coord.api = MagicMock(spec_set=[])  # no is_push_ready attribute
        coord.hass.data = {}

        assert coord._is_fcm_ready_soft() is False

    def test_fatal_error_string_returns_false(self, coord: PollingBranchesStub) -> None:
        coord.api = MagicMock(spec_set=[])
        fcm = MagicMock(spec_set=["_fatal_error", "_fatal_errors", "is_ready", "pc"])
        fcm._fatal_error = "boom"
        fcm._fatal_errors = None
        fcm.is_ready = True
        fcm.pc = None
        coord.hass.data = {"googlefindmy": {"fcm_receiver": fcm}}

        assert coord._is_fcm_ready_soft() is False

    def test_fatal_error_by_entry_returns_false(
        self, coord: PollingBranchesStub
    ) -> None:
        coord.api = MagicMock(spec_set=[])
        coord._entry_id = MagicMock(return_value="entry-xyz")
        fcm = MagicMock(spec_set=["_fatal_error", "_fatal_errors", "is_ready", "pc"])
        fcm._fatal_error = None
        fcm._fatal_errors = {"entry-xyz": "auth-broken"}
        fcm.is_ready = True
        fcm.pc = None
        coord.hass.data = {"googlefindmy": {"fcm_receiver": fcm}}

        assert coord._is_fcm_ready_soft() is False

    def test_receiver_ready_attribute_wins_over_pc(
        self, coord: PollingBranchesStub
    ) -> None:
        coord.api = MagicMock(spec_set=[])
        fcm = MagicMock(spec_set=["ready", "_fatal_error", "_fatal_errors", "pc"])
        fcm.ready = False
        fcm._fatal_error = None
        fcm._fatal_errors = None
        # ``pc`` would say True, but the explicit ready=False wins.
        fcm.pc = MagicMock(run_state=MagicMock(name="STARTED"), do_listen=True)
        coord.hass.data = {"googlefindmy": {"fcm_receiver": fcm}}

        assert coord._is_fcm_ready_soft() is False

    def test_pc_started_with_do_listen_true(self, coord: PollingBranchesStub) -> None:
        coord.api = MagicMock(spec_set=[])
        pc = MagicMock(spec_set=["run_state", "do_listen"])
        pc.run_state = MagicMock(name="STARTED")
        pc.run_state.name = "STARTED"
        pc.do_listen = True
        fcm = MagicMock(spec_set=["_fatal_error", "_fatal_errors", "is_ready", "pc"])
        fcm._fatal_error = None
        fcm._fatal_errors = None
        fcm.is_ready = None  # not a bool -> skip receiver-flag branch
        fcm.pc = pc
        coord.hass.data = {"googlefindmy": {"fcm_receiver": fcm}}

        assert coord._is_fcm_ready_soft() is True

    def test_pc_started_without_do_listen_returns_false(
        self, coord: PollingBranchesStub
    ) -> None:
        coord.api = MagicMock(spec_set=[])
        pc = MagicMock(spec_set=["run_state", "do_listen"])
        pc.run_state = MagicMock()
        pc.run_state.name = "STARTED"
        pc.do_listen = False
        fcm = MagicMock(spec_set=["_fatal_error", "_fatal_errors", "is_ready", "pc"])
        fcm._fatal_error = None
        fcm._fatal_errors = None
        fcm.is_ready = None
        fcm.pc = pc
        coord.hass.data = {"googlefindmy": {"fcm_receiver": fcm}}

        assert coord._is_fcm_ready_soft() is False

    def test_pc_other_state_returns_false(self, coord: PollingBranchesStub) -> None:
        coord.api = MagicMock(spec_set=[])
        pc = MagicMock(spec_set=["run_state", "do_listen"])
        pc.run_state = MagicMock()
        pc.run_state.name = "STOPPED"
        pc.do_listen = True
        fcm = MagicMock(spec_set=["_fatal_error", "_fatal_errors", "is_ready", "pc"])
        fcm._fatal_error = None
        fcm._fatal_errors = None
        fcm.is_ready = None
        fcm.pc = pc
        coord.hass.data = {"googlefindmy": {"fcm_receiver": fcm}}

        assert coord._is_fcm_ready_soft() is False

    def test_outer_exception_returns_false(self, coord: PollingBranchesStub) -> None:
        # An attribute that raises on access on coord.hass.data forces the
        # outer ``except Exception`` branch.
        broken_data = MagicMock()
        broken_data.get = MagicMock(side_effect=RuntimeError("data broken"))
        coord.hass.data = broken_data
        coord.api = MagicMock(spec_set=[])

        assert coord._is_fcm_ready_soft() is False


# ---------------------------------------------------------------------------
# _note_fcm_deferral (escalation timeline)
# ---------------------------------------------------------------------------


class TestNoteFcmDeferralBranches:
    """Stage 0 (start) -> Stage 1 (120s INFO) -> Stage 2 (300s WARNING)."""

    def test_first_call_sets_started_and_degraded(
        self, coord: PollingBranchesStub
    ) -> None:
        coord._fcm_defer_started_mono = 0.0
        coord._fcm_last_stage = 0

        coord._note_fcm_deferral(now_mono=1000.0)

        assert coord._fcm_defer_started_mono == 1000.0
        assert coord._fcm_last_stage == 0
        assert coord._fcm_status_state == FcmStatus.DEGRADED

    def test_subsequent_call_under_2min_does_not_escalate(
        self, coord: PollingBranchesStub
    ) -> None:
        coord._fcm_defer_started_mono = 1000.0
        coord._fcm_last_stage = 0

        coord._note_fcm_deferral(now_mono=1050.0)  # 50s elapsed

        assert coord._fcm_last_stage == 0

    def test_at_2min_emits_stage_1_info(
        self, coord: PollingBranchesStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        coord._fcm_defer_started_mono = 1000.0
        coord._fcm_last_stage = 0

        with caplog.at_level(logging.INFO, logger="custom_components.googlefindmy"):
            coord._note_fcm_deferral(now_mono=1120.0)

        assert coord._fcm_last_stage == 1
        assert coord._fcm_status_state == FcmStatus.DEGRADED
        assert any("2 min" in r.message for r in caplog.records)

    def test_at_5min_emits_stage_2_warning_and_disconnects(
        self, coord: PollingBranchesStub, caplog: pytest.LogCaptureFixture
    ) -> None:
        coord._fcm_defer_started_mono = 1000.0
        coord._fcm_last_stage = 1  # already emitted stage 1

        with caplog.at_level(logging.WARNING, logger="custom_components.googlefindmy"):
            coord._note_fcm_deferral(now_mono=1300.0)

        assert coord._fcm_last_stage == 2
        assert coord._fcm_status_state == FcmStatus.DISCONNECTED

    def test_after_stage2_no_further_escalation(
        self, coord: PollingBranchesStub
    ) -> None:
        coord._fcm_defer_started_mono = 1000.0
        coord._fcm_last_stage = 2

        coord._note_fcm_deferral(now_mono=2000.0)

        assert coord._fcm_last_stage == 2
