# tests/test_nova_auth_resend_limit.py
"""Bounded 401 sequence of the Nova transport for the phone actions.

``max_auth_resends`` limits how often the same request is resent after a 401,
counting the own refresh steps and every wait for a refresh that another
request is running. ``None`` keeps the full tracker sequence (ADM refresh 6 s,
61 s + 6 s, 501 s). The phone paths pass ``PHONE_MAX_AUTH_RESENDS`` (1), so a
phone never rings or reports its location minutes after the trigger.

The clock is not slept: ``asyncio.sleep`` is replaced by a recorder, so the
tests read the waits the transport asked for (C-12).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from custom_components.googlefindmy.const import PHONE_MAX_AUTH_RESENDS
from custom_components.googlefindmy.NovaApi import nova_request as nova_module
from custom_components.googlefindmy.NovaApi.nova_request import (
    AUTH_RETRY_DEADLINE_KEY,
    NovaAuthError,
    async_nova_request,
    is_credential_rejection,
)


class _Response:
    def __init__(self, status: int, body: bytes = b"unauthorized") -> None:
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Session:
    def __init__(self, statuses: list[int]) -> None:
        self._responses = [
            _Response(s, b"\x01\x02" if s == 200 else b"unauthorized") for s in statuses
        ]
        self.calls: list[dict[str, Any]] = []

    def post(self, *args: object, **kwargs: object) -> _Response:
        if not self._responses:
            raise AssertionError("more POSTs than the test allows")
        self.calls.append({"args": args, "kwargs": kwargs})
        return self._responses.pop(0)


class _Cache:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any) -> None:
        if value is None:
            self.data.pop(key, None)
        else:
            self.data[key] = value

    async def get_or_set(self, key: str, generator: Any) -> Any:
        if key not in self.data:
            self.data[key] = generator()
        return self.data[key]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def _record(delay: float) -> None:
        recorded.append(float(delay))

    async def _adm(*_a: object, **_k: object) -> str:
        return "adm-initial"

    monkeypatch.setattr("asyncio.sleep", _record)
    monkeypatch.setattr(nova_module, "async_get_adm_token_api", _adm)
    return recorded


async def _send(
    session: _Session,
    cache: _Cache,
    refreshes: list[str],
    **kwargs: Any,
) -> str:
    async def _refresh() -> str:
        refreshes.append("r")
        return f"adm-{len(refreshes)}"

    return await async_nova_request(
        "testScope",
        "00",
        username="user@example.com",
        token="adm-initial",
        cache=cache,  # type: ignore[arg-type]
        session=session,  # type: ignore[arg-type]
        refresh_override=_refresh,
        **kwargs,
    )


def test_phone_constant_is_one_resend() -> None:
    assert PHONE_MAX_AUTH_RESENDS == 1


@pytest.mark.asyncio
async def test_limit_one_stops_after_one_refresh_and_one_resend(
    sleeps: list[float],
) -> None:
    """401, refresh, 6 s, resend, 401 → transient NovaAuthError; no 61 s, no 501 s."""
    session = _Session([401, 401])
    refreshes: list[str] = []

    with pytest.raises(NovaAuthError) as info:
        await _send(session, _Cache(), refreshes, max_auth_resends=1)

    assert len(session.calls) == 2
    assert len(refreshes) == 1
    assert sleeps == [6.0]
    assert info.value.status == 401
    assert info.value.is_permanent is False
    # Still a credential rejection, so the caller counts it as transient auth.
    assert is_credential_rejection(info.value)
    second = session.calls[1]["kwargs"]["headers"]["Authorization"]
    assert second == "Bearer adm-1"


@pytest.mark.asyncio
async def test_limit_one_succeeds_after_the_single_resend(sleeps: list[float]) -> None:
    session = _Session([401, 200])
    refreshes: list[str] = []

    result = await _send(session, _Cache(), refreshes, max_auth_resends=1)

    assert result == "0102"
    assert len(session.calls) == 2
    assert sleeps == [6.0]


@pytest.mark.asyncio
async def test_limit_zero_sends_once_and_never_refreshes(sleeps: list[float]) -> None:
    session = _Session([401])
    refreshes: list[str] = []

    with pytest.raises(NovaAuthError) as info:
        await _send(session, _Cache(), refreshes, max_auth_resends=0)

    assert len(session.calls) == 1
    assert refreshes == []
    assert sleeps == []
    assert info.value.is_permanent is False


@pytest.mark.asyncio
async def test_default_keeps_the_full_tracker_sequence(sleeps: list[float]) -> None:
    """Regression: without the argument, three resends and a permanent error."""
    session = _Session([401, 401, 401, 401])
    refreshes: list[str] = []

    with pytest.raises(NovaAuthError) as info:
        await _send(session, _Cache(), refreshes)

    assert len(session.calls) == 4
    assert refreshes  # step 2 may coalesce into the refresh of step 1
    assert sleeps == [6.0, 61.0, 6.0, 501.0]
    assert info.value.is_permanent is True


@pytest.mark.asyncio
async def test_limit_one_does_not_wait_long_for_another_refresh(
    sleeps: list[float],
) -> None:
    """A refresh running elsewhere that needs more than 9 s is not waited for."""
    session = _Session([401])
    cache = _Cache()
    cache.data[AUTH_RETRY_DEADLINE_KEY] = time.time() + 100.0
    refreshes: list[str] = []

    with pytest.raises(NovaAuthError) as info:
        await _send(session, cache, refreshes, max_auth_resends=1)

    assert len(session.calls) == 1
    assert sleeps == []
    assert refreshes == []
    assert info.value.is_permanent is False


@pytest.mark.asyncio
async def test_limit_one_counts_a_short_wait_as_the_resend(
    sleeps: list[float],
) -> None:
    """A short wait for another request's refresh uses up the single resend.

    The first wait is the pre-request gate (nothing sent yet, no resend); the
    second is the wait after the first 401, which counts as the resend.
    """
    session = _Session([401, 401])
    cache = _Cache()
    cache.data[AUTH_RETRY_DEADLINE_KEY] = time.time() + 3.0
    refreshes: list[str] = []

    with pytest.raises(NovaAuthError) as info:
        await _send(session, cache, refreshes, max_auth_resends=1)

    assert len(session.calls) == 2
    assert refreshes == []
    assert len(sleeps) == 2
    assert all(0.0 < s <= 9.0 for s in sleeps)
    assert info.value.is_permanent is False


@pytest.mark.asyncio
async def test_default_still_waits_for_a_long_refresh_elsewhere(
    sleeps: list[float],
) -> None:
    """Regression: the tracker path keeps waiting (pre-request gate, then 30 s)."""
    session = _Session([401, 200])
    cache = _Cache()
    cache.data[AUTH_RETRY_DEADLINE_KEY] = time.time() + 100.0
    refreshes: list[str] = []

    assert await _send(session, cache, refreshes) == "0102"
    assert len(sleeps) == 2
    assert sleeps[0] == pytest.approx(101.0, abs=1.0)
    assert sleeps[1] == 30.0


@pytest.mark.asyncio
async def test_limit_one_does_not_wait_before_sending_for_a_long_refresh(
    sleeps: list[float],
) -> None:
    """Pre-request gate: no long wait; the request goes out at once."""
    session = _Session([200])
    cache = _Cache()
    cache.data[AUTH_RETRY_DEADLINE_KEY] = time.time() + 100.0

    assert await _send(session, cache, [], max_auth_resends=1) == "0102"
    assert sleeps == []
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_limit_one_still_takes_a_short_pre_request_wait(
    sleeps: list[float],
) -> None:
    session = _Session([200])
    cache = _Cache()
    cache.data[AUTH_RETRY_DEADLINE_KEY] = time.time() + 3.0

    assert await _send(session, cache, [], max_auth_resends=1) == "0102"
    assert len(sleeps) == 1
    assert 0.0 < sleeps[0] <= 9.0


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [-1, True, 1.5, "1"])
async def test_invalid_limit_raises_before_sending(bad: Any) -> None:
    session = _Session([200])
    with pytest.raises(ValueError, match="max_auth_resends"):
        await _send(session, _Cache(), [], max_auth_resends=bad)
    assert session.calls == []
