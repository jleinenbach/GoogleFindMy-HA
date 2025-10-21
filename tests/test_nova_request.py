# tests/test_nova_request.py
"""Tests for Nova API async request helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from collections.abc import Awaitable, Callable

import pytest

from custom_components.googlefindmy.NovaApi.nova_request import (
    AsyncTTLPolicy,
    NovaAuthError,
    async_nova_request,
)
from custom_components.googlefindmy.api import _EphemeralCache
from custom_components.googlefindmy.const import DATA_AAS_TOKEN
from custom_components.googlefindmy.Auth.username_provider import username_string


class _DummyResponse:
    """Minimal async context manager mimicking aiohttp.ClientResponse."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body
        self.headers: dict[str, str] = {}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> _DummyResponse:
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

    async def get_or_set(
        self, key: str, generator: Callable[[], Awaitable[Any] | Any]
    ) -> Any:
        if key in self._data:
            return self._data[key]
        result = generator()
        if asyncio.iscoroutine(result):
            result = await result
        await self.set(key, result)
        return result


def test_async_nova_request_returns_auth_error_on_repeated_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        async def _seed_initial(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return "initial-adm"

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _seed_initial,
        )

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


def test_async_nova_request_refreshes_token_after_initial_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401 triggers an ADM refresh and retries with the rotated token."""

    cache = _StubCache()
    session = _DummySession(
        [
            _DummyResponse(401, b"unauthorized"),
            _DummyResponse(200, b"\xba\xad\xf0\r"),
        ]
    )

    adm_calls: list[str | None] = []
    refresh_calls: list[None] = []
    on_401_calls: list[bool] = []

    async def _fake_get_adm_token(
        username: str | None = None,
        *,
        retries: int = 2,
        backoff: float = 1.0,
        cache: Any,
    ) -> str:
        adm_calls.append(username)
        return "adm-old"

    async def _refresh_override() -> str:
        refresh_calls.append(None)
        return "adm-new"

    original_on_401 = AsyncTTLPolicy.on_401

    async def _spy_on_401(self: AsyncTTLPolicy, adaptive_downshift: bool = True) -> Any:
        on_401_calls.append(adaptive_downshift)
        return await original_on_401(self, adaptive_downshift=adaptive_downshift)

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.AsyncTTLPolicy.on_401",
        _spy_on_401,
    )

    async def _exercise() -> tuple[str, Any]:
        await cache.set(DATA_AAS_TOKEN, "aas-original")
        result = await async_nova_request(
            "testScope",
            "deadbeef",
            username="user@example.com",
            cache=cache,
            session=session,
            refresh_override=_refresh_override,
        )
        final_aas = await cache.get(DATA_AAS_TOKEN)
        return result, final_aas

    result, final_aas = asyncio.run(_exercise())

    assert result == "baadf00d"
    assert final_aas == "aas-original"
    assert adm_calls == ["user@example.com"]
    assert len(refresh_calls) == 1
    assert len(on_401_calls) == 1
    assert len(session.calls) == 2
    second_headers = session.calls[1]["kwargs"].get("headers", {})
    assert second_headers.get("Authorization") == "Bearer adm-new"


def test_async_nova_request_fetches_token_when_not_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        calls.append(
            {
                "username": username,
                "cache": cache,
                "retries": retries,
                "backoff": backoff,
            }
        )
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


def test_async_nova_request_invokes_adm_exchange_even_with_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Providing a token kwarg must still route through async_get_adm_token_api."""

    cache = _StubCache()
    session = _DummySession([_DummyResponse(200, b"\xaa\xbb")])

    calls: list[dict[str, Any]] = []

    async def _fake_get_adm_token(
        username: str | None = None,
        *,
        retries: int = 2,
        backoff: float = 1.0,
        cache: Any,
    ) -> str:
        stored = await cache.get(DATA_AAS_TOKEN)
        calls.append(
            {
                "username": username,
                "cache": cache,
                "retries": retries,
                "backoff": backoff,
                "stored_aas": stored,
            }
        )
        return "adm-from-override"

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )

    async def _exercise() -> str:
        return await async_nova_request(
            "testScope",
            "beef",
            username="User@Example.COM",
            token="aas_et/FLOW",
            cache=cache,
            session=session,
        )

    result = asyncio.run(_exercise())

    assert result == "aabb"
    assert calls and calls[0]["username"] == "user@example.com"
    assert calls[0]["stored_aas"] == "aas_et/FLOW"
    assert session.calls
    headers = session.calls[0]["kwargs"].get("headers", {})
    assert headers.get("Authorization") == "Bearer adm-from-override"


def test_async_nova_request_skips_seeding_without_username(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Do not seed non-AAS override tokens when username kwarg is omitted."""

    cache = _StubCache()
    session = _DummySession([_DummyResponse(200, b"\x01\x02")])

    calls: list[Any] = []
    final_state: dict[str, Any] = {}

    async def _fake_get_adm_token(
        username: str | None = None,
        *,
        retries: int = 2,
        backoff: float = 1.0,
        cache: Any,
    ) -> str:
        calls.append(await cache.get(DATA_AAS_TOKEN))
        return "adm-fallback"

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )

    async def _exercise() -> str:
        await cache.set(username_string, "user@example.com")
        result = await async_nova_request(
            "testScope",
            "c0de",
            token="fcm-registration-token",
            cache=cache,
            session=session,
        )
        final_state["seeded"] = await cache.get(DATA_AAS_TOKEN)
        return result

    result = asyncio.run(_exercise())

    assert result == "0102"
    assert calls == [None]
    assert final_state["seeded"] is None
    assert session.calls
    headers = session.calls[0]["kwargs"].get("headers", {})
    assert headers.get("Authorization") == "Bearer adm-fallback"


def test_async_nova_request_preserves_existing_aas_when_username_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-seeded AAS token must survive flow token usage without username kwarg."""

    cache = _StubCache()
    session = _DummySession([_DummyResponse(200, b"\xfa\xce")])

    calls: list[dict[str, Any]] = []

    async def _fake_get_adm_token(
        username: str | None = None,
        *,
        retries: int = 2,
        backoff: float = 1.0,
        cache: Any,
    ) -> str:
        calls.append(
            {
                "username": username,
                "cache": cache,
                "retries": retries,
                "backoff": backoff,
            }
        )
        return "adm-preseed"

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )

    async def _exercise() -> tuple[str, Any]:
        await cache.set(username_string, "user@example.com")
        await cache.set(DATA_AAS_TOKEN, "cached-aas-token")
        result = await async_nova_request(
            "testScope",
            "face",
            token="fcm-registration-token",
            cache=cache,
            session=session,
        )
        final_aas = await cache.get(DATA_AAS_TOKEN)
        return result, final_aas

    result, final_aas = asyncio.run(_exercise())

    assert result == "face"
    assert calls and calls[0]["username"] == "user@example.com"
    assert calls[0]["cache"] is cache
    assert calls[0]["retries"] == 2
    assert calls[0]["backoff"] == 1.0
    assert final_aas == "cached-aas-token"
    assert session.calls
    headers = session.calls[0]["kwargs"].get("headers", {})
    assert headers.get("Authorization") == "Bearer adm-preseed"


def test_async_nova_request_converts_flow_token_with_ephemeral_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config-flow style caches must convert AAS tokens before Nova POST."""

    cache = _EphemeralCache(oauth_token=None, email="User@Example.COM")
    session = _DummySession([_DummyResponse(200, b"\x99\x33")])

    calls: list[str] = []

    async def _fake_get_adm_token(
        username: str | None = None,
        *,
        retries: int = 2,
        backoff: float = 1.0,
        cache: Any,
    ) -> str:
        calls.append(await cache.get(DATA_AAS_TOKEN))
        return "adm-token"

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )

    async def _exercise() -> str:
        return await async_nova_request(
            "testScope",
            "cafe",
            username="user@example.com",
            token="aas_et/CONFIG_FLOW",
            cache=cache,
            session=session,
        )

    result = asyncio.run(_exercise())

    assert result == "9933"
    assert calls == ["aas_et/CONFIG_FLOW"]
    assert session.calls
    headers = session.calls[0]["kwargs"].get("headers", {})
    assert headers.get("Authorization") == "Bearer adm-token"
