# tests/test_fcm_repair_reauth.py
"""Tests for the fixable FCM reauth repair and its escalation thresholds.

Covers the three terminal give-up points in the FCM supervisor that now raise
the single shared fixable repair ``fcm_reauth_required_{entry_id}`` and the
fix flow in ``custom_components/googlefindmy/repairs.py`` that drives the user
into the config-entry reauth flow.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy import repairs
from custom_components.googlefindmy.Auth import fcm_receiver_ha
from custom_components.googlefindmy.Auth.fcm_receiver_ha import DOMAIN, FcmReceiverHA
from custom_components.googlefindmy.exceptions import FatalRegistrationError


class _StubClient:
    """Minimal FCM client stub; ``start`` ends the supervisor loop."""

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


def _make_receiver(
    monkeypatch: pytest.MonkeyPatch, entry_id: str
) -> tuple[FcmReceiverHA, SimpleNamespace, MagicMock, MagicMock]:
    """Build a receiver wired to capture issue-registry calls.

    Mirrors the proven harness in ``test_fcm_backoff_escalation`` but for the
    fatal-registration (404/401) escalation paths.
    """
    receiver = FcmReceiverHA()
    hass = SimpleNamespace()
    receiver.attach_hass(hass)

    stop_evt = asyncio.Event()
    receiver._stop_evts[entry_id] = stop_evt

    pc = _StubClient(stop_evt)
    monkeypatch.setattr(
        receiver, "_ensure_client_for_entry", AsyncMock(return_value=pc)
    )
    # 404/401 give-up paths sleep between retries; make them instant.
    monkeypatch.setattr(fcm_receiver_ha.asyncio, "sleep", AsyncMock())
    # The 401 path renews tokens before retrying.
    monkeypatch.setattr(receiver, "_invalidate_fcm_tokens", AsyncMock())
    monkeypatch.setattr(
        fcm_receiver_ha.ir,
        "IssueSeverity",
        SimpleNamespace(WARNING="warning", ERROR="error"),
    )

    create_issue = MagicMock()
    delete_issue = MagicMock()
    monkeypatch.setattr(fcm_receiver_ha.ir, "async_create_issue", create_issue)
    monkeypatch.setattr(fcm_receiver_ha.ir, "async_delete_issue", delete_issue)

    return receiver, hass, create_issue, delete_issue


def _reauth_calls(create_issue: MagicMock, entry_id: str) -> list:
    """Return the create_issue calls that raised the fixable reauth issue."""
    return [
        call
        for call in create_issue.call_args_list
        if len(call.args) >= 3 and call.args[2] == f"fcm_reauth_required_{entry_id}"
    ]


@pytest.mark.asyncio
async def test_transient_404_below_cap_does_not_escalate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1: 404 rotation below ``_MAX_FATAL_ENDPOINT_RETRIES`` must not escalate."""
    entry_id = "entry-id"
    receiver, hass, create_issue, delete_issue = _make_receiver(
        monkeypatch, entry_id
    )

    # Three transient 404s (n=1..3, well below the cap of 7), then success.
    register_mock = AsyncMock(
        side_effect=[
            FatalRegistrationError("404 endpoint", is_auth_error=False),
            FatalRegistrationError("404 endpoint", is_auth_error=False),
            FatalRegistrationError("404 endpoint", is_auth_error=False),
            True,
        ]
    )
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert register_mock.await_count == 4
    # No fixable issue raised: transient rotation cleared before the give-up.
    assert _reauth_calls(create_issue, entry_id) == []
    # On the successful registration the recovery delete still fires.
    delete_issue.assert_any_call(
        hass, DOMAIN, f"fcm_reauth_required_{entry_id}"
    )


@pytest.mark.asyncio
async def test_permanent_404_at_cap_raises_fixable_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T2: 404 reaching the cap raises exactly one fixable reauth issue."""
    entry_id = "entry-id"
    receiver, hass, create_issue, _delete_issue = _make_receiver(
        monkeypatch, entry_id
    )

    # Seven consecutive 404s -> fatal_count == _MAX_FATAL_ENDPOINT_RETRIES (7).
    register_mock = AsyncMock(
        side_effect=[
            FatalRegistrationError("404 endpoint", is_auth_error=False)
            for _ in range(fcm_receiver_ha._MAX_FATAL_ENDPOINT_RETRIES)
        ]
    )
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert register_mock.await_count == fcm_receiver_ha._MAX_FATAL_ENDPOINT_RETRIES
    calls = _reauth_calls(create_issue, entry_id)
    assert len(calls) == 1
    call = calls[0]
    assert call.args[0] is hass
    assert call.args[1] == DOMAIN
    assert call.kwargs["is_fixable"] is True
    assert call.kwargs["severity"] == fcm_receiver_ha.ir.IssueSeverity.ERROR
    assert call.kwargs["translation_key"] == "fcm_reauth_required"
    assert call.kwargs["data"]["entry_id"] == entry_id
    assert call.kwargs["data"]["reason"] == "endpoint_404"


@pytest.mark.asyncio
async def test_permanent_401_at_cap_raises_fixable_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T3: 401 reaching the auth cap raises the fixable reauth issue."""
    entry_id = "entry-id"
    receiver, _hass, create_issue, _delete_issue = _make_receiver(
        monkeypatch, entry_id
    )

    # Three consecutive 401s -> fatal_count == _MAX_FATAL_AUTH_RETRIES (3).
    register_mock = AsyncMock(
        side_effect=[
            FatalRegistrationError("401 auth", is_auth_error=True)
            for _ in range(fcm_receiver_ha._MAX_FATAL_AUTH_RETRIES)
        ]
    )
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert register_mock.await_count == fcm_receiver_ha._MAX_FATAL_AUTH_RETRIES
    calls = _reauth_calls(create_issue, entry_id)
    assert len(calls) == 1
    assert calls[0].kwargs["data"]["reason"] == "auth_401"
    assert calls[0].kwargs["is_fixable"] is True


@pytest.mark.asyncio
async def test_fix_flow_factory_returns_reauth_flow() -> None:
    """T4a: the fix-flow factory carries the entry id from the issue data."""
    flow = await repairs.async_create_fix_flow(
        SimpleNamespace(), "fcm_reauth_required_entry-id", {"entry_id": "entry-id"}
    )
    assert isinstance(flow, repairs.FcmReauthRepairFlow)
    assert flow._entry_id == "entry-id"


@pytest.mark.asyncio
async def test_fix_flow_confirm_starts_reauth() -> None:
    """T4b: confirming the repair starts reauth for the resolved entry."""
    # ``ConfigEntry.async_start_reauth`` is a synchronous ``@callback`` returning
    # ``None``; mock it synchronously so the test fails if production re-adds an
    # erroneous ``await`` on that result.
    entry = SimpleNamespace(async_start_reauth=MagicMock(return_value=None))
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_entry=lambda entry_id: (
                entry if entry_id == "entry-id" else None
            )
        )
    )
    flow = repairs.FcmReauthRepairFlow("entry-id")
    flow.hass = hass  # normally injected by the repairs flow manager

    flow._async_start_reauth()

    entry.async_start_reauth.assert_called_once_with(hass)


@pytest.mark.asyncio
async def test_fix_flow_missing_entry_is_noop() -> None:
    """T4c: a stale issue (entry already gone) dismisses without raising."""
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=lambda entry_id: None)
    )
    flow = repairs.FcmReauthRepairFlow("entry-id")
    flow.hass = hass

    # Must not raise even though the entry cannot be resolved.
    flow._async_start_reauth()

    flow_no_id = repairs.FcmReauthRepairFlow(None)
    flow_no_id.hass = hass
    flow_no_id._async_start_reauth()


@pytest.mark.asyncio
async def test_successful_register_deletes_fixable_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T5: a successful (re-)registration clears the fixable issue."""
    entry_id = "entry-id"
    receiver, hass, _create_issue, delete_issue = _make_receiver(
        monkeypatch, entry_id
    )

    register_mock = AsyncMock(side_effect=[True])
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    delete_issue.assert_any_call(
        hass, DOMAIN, f"fcm_reauth_required_{entry_id}"
    )
    delete_issue.assert_any_call(hass, DOMAIN, f"fcm_stuck_{entry_id}")
