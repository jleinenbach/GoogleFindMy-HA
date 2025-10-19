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

    def post(self, *_args: object, **_kwargs: object) -> _DummyResponse:
        if not self._responses:
            raise AssertionError("No responses left for nova_request test")
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
