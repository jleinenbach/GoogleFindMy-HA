# tests/test_config_flow_initial_auth.py
"""Tests ensuring config flow initial auth preserves scoped tokens."""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import pytest

from custom_components.googlefindmy.api import GoogleFindMyAPI
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
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, *_args: object, **kwargs: Any) -> _DummyResponse:
        if not self._responses:
            raise AssertionError("No responses left for dummy session")
        self.calls.append({"args": _args, "kwargs": kwargs})
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

    async def async_get_cached_value(self, key: str) -> Any:
        return await self.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        await self.set(key, value)

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


@pytest.fixture
def stub_cache() -> _StubCache:
    """Provide a fresh stub cache for each test."""

    return _StubCache()


@pytest.fixture
def dummy_session_factory() -> Callable[[list[_DummyResponse]], _DummySession]:
    """Return a factory producing dummy sessions with queued responses."""

    def _factory(responses: list[_DummyResponse]) -> _DummySession:
        return _DummySession(list(responses))

    return _factory


def test_async_initial_auth_preserves_aas_token_and_uses_adm(
    monkeypatch: pytest.MonkeyPatch,
    stub_cache: _StubCache,
    dummy_session_factory: Callable[[list[_DummyResponse]], _DummySession],
) -> None:
    """Ensure config-flow device list exchange uses ADM token without mutating AAS."""

    adm_calls: list[dict[str, Any]] = []

    async def _fake_generate(username: str, *, cache: Any) -> str:
        adm_calls.append({"username": username, "cache": cache})
        return "adm-token/test"

    monkeypatch.setattr(
        "custom_components.googlefindmy.Auth.adm_token_retrieval._generate_adm_token",
        _fake_generate,
    )

    processed_payloads: list[str] = []

    def _fake_process(self: GoogleFindMyAPI, payload: str) -> list[dict[str, str]]:
        processed_payloads.append(payload)
        return [{"id": "device-1"}]

    monkeypatch.setattr(
        "custom_components.googlefindmy.api.GoogleFindMyAPI._process_device_list_response",
        _fake_process,
    )

    session = dummy_session_factory([_DummyResponse(200, b"\x10\x20")])
    aas_token = "aas_et/MASTER"

    async def _exercise() -> tuple[list[dict[str, str]], str]:
        await stub_cache.set(username_string, "User@Example.COM")
        await stub_cache.set(DATA_AAS_TOKEN, aas_token)
        api = GoogleFindMyAPI(cache=stub_cache, session=session)
        result = await api.async_get_basic_device_list(token=aas_token)
        final_aas = await stub_cache.get(DATA_AAS_TOKEN)
        return result, final_aas

    result, final_aas = asyncio.run(_exercise())

    assert adm_calls and len(adm_calls) == 1
    assert adm_calls[0]["username"] == "user@example.com"
    assert adm_calls[0]["cache"] is stub_cache

    assert session.calls, "Expected Nova request to be issued"
    headers = session.calls[0]["kwargs"].get("headers", {})
    assert headers.get("Authorization") == "Bearer adm-token/test"

    assert final_aas == aas_token
    assert processed_payloads == ["1020"]
    assert result == [{"id": "device-1"}]
