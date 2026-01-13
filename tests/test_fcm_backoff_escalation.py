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
    register_mock = AsyncMock(side_effect=[False] * 12 + [True])
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)

    monkeypatch.setattr(fcm_receiver_ha.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        fcm_receiver_ha.random, "uniform", lambda *_args, **_kwargs: 0.0
    )

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
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=1.0)

    assert register_mock.await_count == 13
    assert create_issue.call_count == 2
    assert create_issue_attempts == [11, 12]

    for call in create_issue.call_args_list:
        assert call.args[0] is hass
        assert call.args[1] == DOMAIN
        assert call.args[2] == f"fcm_stuck_{entry_id}"
        assert call.kwargs["is_fixable"] is False
        assert call.kwargs["severity"] == fcm_receiver_ha.ir.IssueSeverity.WARNING
        assert call.kwargs["translation_key"] == "fcm_connection_stuck"

    delete_issue.assert_called_once_with(hass, DOMAIN, f"fcm_stuck_{entry_id}")
