# tests/test_nova_request.py
"""Tests for Nova API async request helpers."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaAuthError,
    async_nova_request,
)


class _DummyResponse:
    """Minimal async context manager mimicking aiohttp.ClientResponse."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> "_DummyResponse":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _DummySession:
    """Async session stub returning pre-seeded responses."""

    def __init__(self, responses: list[_DummyResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def post(self, *_args: object, **_kwargs: object) -> _DummyResponse:
        if not self._responses:
            raise AssertionError("No responses left for nova_request test")
        self.calls.append({"args": _args, "kwargs": _kwargs})
        return self._responses.pop(0)


class _StubCache:
    """Entry-scoped cache stub implementing the minimal async API."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        if value is None:
            self._data.pop(key, None)
            return
        self._data[key] = value


def test_async_nova_request_returns_auth_error_on_repeated_401() -> None:
    """Ensure NovaAuthError is raised instead of NameError on 401 responses."""

    cache = _StubCache()
    session = _DummySession(
        [
            _DummyResponse(401, b"<html><body>Unauthorized</body></html>"),
            _DummyResponse(401, b"Unauthorized"),
        ]
    )

    async def _exercise() -> None:
        refresh_results: asyncio.Queue[str] = asyncio.Queue()
        refresh_results.put_nowait("token-one")
        refresh_results.put_nowait("token-two")

        async def _refresh() -> str:
            return await refresh_results.get()

        await async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            token="initial-token",
            cache=cache,
            session=session,
            refresh_override=_refresh,
        )

    with pytest.raises(NovaAuthError) as err:
        asyncio.run(_exercise())

    assert err.value.status == 401
    assert isinstance(err.value.detail, str)
    assert "Unauthorized" in err.value.detail


def test_async_nova_request_fetches_token_when_not_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nova should resolve an ADM token when `token` kwarg is omitted."""

    cache = _StubCache()
    session = _DummySession([_DummyResponse(200, b"\x10\x20")])

    calls: list[dict[str, Any]] = []

    async def _fake_get_adm_token(
        username: str | None = None,
        *,
        retries: int = 2,
        backoff: float = 1.0,
        cache: Any,
    ) -> str:
        calls.append({"username": username, "cache": cache, "retries": retries, "backoff": backoff})
        return "resolved-token"

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )

    async def _exercise() -> str:
        return await async_nova_request(
            "testScope",
            "00",
            username="User@Example.COM",
            cache=cache,
            session=session,
        )

    result = asyncio.run(_exercise())

    assert result == "1020"
    assert calls and calls[0]["username"] == "user@example.com"
    assert session.calls
    headers = session.calls[0]["kwargs"].get("headers", {})
    assert headers.get("Authorization") == "Bearer resolved-token"
