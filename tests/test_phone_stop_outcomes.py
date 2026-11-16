# tests/test_phone_stop_outcomes.py
"""Every failure of a phone stop (action 5) ends as ``StopSoundOutcome.FAILED``.

The coordinator's phone stop shares the tracker stop's error ladder: an auth
failure marks the auth state and asks for a refresh, a transport error notes a
stop transport problem, any other error is reported, and a submission that the
API did not accept fails, noting a transport problem only for
``TRANSPORT_FAILED``. IDs are invented.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from aiohttp import ClientConnectionError, ClientError
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.googlefindmy.const import SoundDispatchOutcome
from custom_components.googlefindmy.coordinator.locate import StopSoundOutcome
from tests.test_phone_ring_paths import _PHONE, _api, _coordinator, _Receiver


def _stop_coordinator(
    monkeypatch: pytest.MonkeyPatch, answer: Any
) -> tuple[Any, dict[str, list[Any]]]:
    api = _api(monkeypatch, _Receiver())

    async def _stop(_device_id: str, _uuid: str | None = None) -> Any:
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(api, "async_stop_sound", _stop)
    coordinator = cast(Any, _coordinator(api))
    seen: dict[str, list[Any]] = {
        "auth": [],
        "refresh": [],
        "transport": [],
        "error": [],
    }
    coordinator._set_auth_state = lambda **kw: seen["auth"].append(kw)
    coordinator._note_stop_transport_problem_without_extending = lambda: seen[
        "transport"
    ].append(True)
    coordinator.note_error = lambda err, **_kw: seen["error"].append(type(err))

    async def _refresh() -> None:
        seen["refresh"].append(True)
        raise RuntimeError("refresh failed too")

    coordinator.async_request_refresh = _refresh
    return coordinator, seen


@pytest.mark.asyncio
async def test_auth_failure_marks_the_auth_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, seen = _stop_coordinator(monkeypatch, ConfigEntryAuthFailed("x"))
    assert await coordinator.async_stop_sound(_PHONE) is StopSoundOutcome.FAILED
    assert seen["auth"] and seen["auth"][0]["failed"] is True
    assert seen["refresh"] == [True]
    assert seen["transport"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [TimeoutError(), ClientConnectionError(), ClientError()]
)
async def test_transport_error_notes_a_stop_transport_problem(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    coordinator, seen = _stop_coordinator(monkeypatch, error)
    assert await coordinator.async_stop_sound(_PHONE) is StopSoundOutcome.FAILED
    assert seen["transport"] == [True]
    assert seen["error"] == [type(error)]
    assert seen["auth"] == []


@pytest.mark.asyncio
async def test_unexpected_error_is_reported_without_transport_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator, seen = _stop_coordinator(monkeypatch, RuntimeError("boom"))
    assert await coordinator.async_stop_sound(_PHONE) is StopSoundOutcome.FAILED
    assert seen["error"] == [RuntimeError]
    assert seen["transport"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "transport_noted"),
    [
        (SoundDispatchOutcome.TRANSPORT_FAILED, True),
        (SoundDispatchOutcome.NOT_SENT, False),
        (SoundDispatchOutcome.INTERNAL_ERROR, False),
    ],
)
async def test_not_accepted_submission_fails(
    monkeypatch: pytest.MonkeyPatch,
    answer: SoundDispatchOutcome,
    transport_noted: bool,
) -> None:
    coordinator, seen = _stop_coordinator(monkeypatch, answer)
    assert await coordinator.async_stop_sound(_PHONE) is StopSoundOutcome.FAILED
    assert seen["transport"] == ([True] if transport_noted else [])
    assert seen["auth"] == []
