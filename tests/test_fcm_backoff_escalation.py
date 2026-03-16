# tests/test_fcm_backoff_escalation.py
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha
from custom_components.googlefindmy.Auth.fcm_receiver_ha import DOMAIN, FcmReceiverHA


class _StubClient:
    def __init__(self, stop_evt: asyncio.Event) -> None:
        self.stop_evt = stop_evt
        self.do_listen = True
        self.run_state = None
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.stop_evt.set()

    async def stop(self) -> None:
        self.stop_calls += 1


@pytest.mark.asyncio
async def test_fcm_backoff_escalation_creates_and_clears_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_id = "entry-id"
    receiver = FcmReceiverHA()
    hass = SimpleNamespace()
    receiver.attach_hass(hass)

    stop_evt = asyncio.Event()
    receiver._stop_evts[entry_id] = stop_evt

    pc = _StubClient(stop_evt)
    monkeypatch.setattr(
        receiver, "_ensure_client_for_entry", AsyncMock(return_value=pc)
    )

    # The supervisor doubles backoff from 1.0 each failure.
    # Backoff reaches _BACKOFF_WARNING_THRESHOLD_S (64) after 6 failures
    # (1->2->4->8->16->32->64).  So create_issue fires on failures 7-12
    # (6 calls total), then attempt 13 succeeds.
    register_mock = AsyncMock(side_effect=[False] * 12 + [True])
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)

    monkeypatch.setattr(fcm_receiver_ha.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        fcm_receiver_ha.random, "uniform", lambda *_args, **_kwargs: 0.0
    )

    # Patch asyncio.wait_for (used by _nudge_sleep) to raise TimeoutError
    # immediately, so the supervisor loop doesn't actually wait.
    _real_wait_for = asyncio.wait_for

    async def _instant_wait_for(fut, *, timeout=None):
        # For the nudge event waits, raise TimeoutError immediately so
        # backoff escalates without real delays.  Pass through everything
        # else (e.g. the outer test's wait_for on the supervisor task).
        if timeout is not None and timeout > 0:
            # Check if this is a nudge_evt.wait() call by inspecting the
            # coroutine name.  If it's something else, use real wait_for.
            coro_name = getattr(fut, "__name__", "") or getattr(
                getattr(fut, "cr_code", None), "co_name", ""
            )
            if coro_name == "wait":
                # This is an Event.wait() inside _nudge_sleep — skip it
                # by cancelling the coroutine and raising TimeoutError.
                if asyncio.iscoroutine(fut):
                    fut.close()
                raise TimeoutError
        return await _real_wait_for(fut, timeout=timeout)

    monkeypatch.setattr(fcm_receiver_ha.asyncio, "wait_for", _instant_wait_for)

    create_issue_attempts: list[int] = []
    create_issue = MagicMock(
        side_effect=lambda *args, **kwargs: create_issue_attempts.append(
            register_mock.await_count
        )
    )
    delete_issue = MagicMock()
    monkeypatch.setattr(fcm_receiver_ha.ir, "async_create_issue", create_issue)
    monkeypatch.setattr(fcm_receiver_ha.ir, "async_delete_issue", delete_issue)
    monkeypatch.setattr(
        fcm_receiver_ha.ir,
        "IssueSeverity",
        SimpleNamespace(WARNING="warning"),
    )

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert register_mock.await_count == 13
    # create_issue fires for every failure where backoff >= 64 (failures 7..12)
    assert create_issue.call_count == 6
    assert create_issue_attempts == [7, 8, 9, 10, 11, 12]

    for call in create_issue.call_args_list:
        assert call.args[0] is hass
        assert call.args[1] == DOMAIN
        assert call.args[2] == f"fcm_stuck_{entry_id}"
        assert call.kwargs["is_fixable"] is False
        assert call.kwargs["severity"] == fcm_receiver_ha.ir.IssueSeverity.WARNING
        assert call.kwargs["translation_key"] == "fcm_connection_stuck"

    delete_issue.assert_called_once_with(hass, DOMAIN, f"fcm_stuck_{entry_id}")
