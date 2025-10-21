# tests/test_config_flow_initial_auth.py
"""Tests ensuring config flow initial auth preserves scoped tokens."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable, Optional

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
)
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
        self.entry_id = "stub-entry"

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


def test_manual_config_flow_with_master_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manual flow must store aas_et tokens like the secrets path."""

    async def _fake_pick(
        email: str,
        candidates: list[tuple[str, str]],
        *,
        secrets_bundle: dict[str, Any] | None = None,
    ) -> str | None:
        assert secrets_bundle is None
        return candidates[0][1] if candidates else None

    async def _fake_probe(api: Any, *, email: str, token: str) -> list[dict[str, Any]]:
        assert token.startswith("aas_et/")
        return []

    monkeypatch.setattr(config_flow, "async_pick_working_token", _fake_pick)
    monkeypatch.setattr(config_flow, "_try_probe_devices", _fake_probe)

    class _ConfigEntries:
        def async_entries(self, domain: str) -> list[Any]:
            assert domain == config_flow.DOMAIN
            return []

    class _FlowHass:
        def __init__(self) -> None:
            self.config_entries = _ConfigEntries()
            self.data: dict[str, Any] = {config_flow.DOMAIN: {}}

    captured: dict[str, Any] = {}

    async def _create_entry(
        *,
        title: str,
        data: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured["result"] = {"title": title, "data": data, "options": options}
        return {"type": "create_entry", "title": title, "data": data, "options": options}

    async def _exercise() -> None:
        hass = _FlowHass()
        flow = config_flow.ConfigFlow()
        flow.hass = hass  # type: ignore[assignment]
        flow.context = {}
        flow.unique_id = None  # type: ignore[attr-defined]

        async def _set_unique_id(value: str) -> None:
            flow.unique_id = value  # type: ignore[attr-defined]

        flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
        flow._abort_if_unique_id_configured = lambda: None  # type: ignore[assignment]
        flow.async_create_entry = _create_entry  # type: ignore[assignment]

        manual_token = "aas_et/MANUAL_MASTER"
        first = await flow.async_step_individual_tokens(
            {
                CONF_GOOGLE_EMAIL: "ManualUser@Example.COM",
                CONF_OAUTH_TOKEN: manual_token,
            }
        )
        if inspect.isawaitable(first):
            first = await first
        assert isinstance(first, dict)
        assert first.get("type") == "form"

        final = await flow.async_step_device_selection({})
        if inspect.isawaitable(final):
            final = await final
        assert isinstance(final, dict)
        assert final.get("type") == "create_entry"

    asyncio.run(_exercise())

    assert captured, "Expected config entry creation payload to be captured"
    payload = captured["result"]
    data = payload["data"]
    assert data[CONF_OAUTH_TOKEN] == "aas_et/MANUAL_MASTER"
    assert data[DATA_AAS_TOKEN] == "aas_et/MANUAL_MASTER"
    assert data[DATA_AUTH_METHOD] == config_flow._AUTH_METHOD_SECRETS


def test_ephemeral_probe_cache_allows_missing_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config-flow probes must tolerate ephemeral caches without entry IDs."""

    captured: dict[str, Any] = {}

    async def _fake_async_request_device_list(
        username: str,
        *,
        session: Any = None,
        cache: Any,
        token: str | None = None,
        cache_get: Callable[[str], Awaitable[Any]] | None = None,
        cache_set: Callable[[str, Any], Awaitable[None]] | None = None,
        refresh_override: Callable[[], Awaitable[Optional[str]]] | None = None,
        namespace: str | None = None,
    ) -> str:
        captured["username"] = username
        captured["cache"] = cache
        captured["token"] = token
        captured["namespace"] = namespace
        assert cache_get is not None
        assert cache_set is not None
        return "00"

    def _fake_process(self: GoogleFindMyAPI, result_hex: str) -> list[dict[str, Any]]:
        captured["processed_hex"] = result_hex
        return [{"id": "device"}]

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices.async_request_device_list",
        _fake_async_request_device_list,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.api.async_request_device_list",
        _fake_async_request_device_list,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.api.GoogleFindMyAPI._process_device_list_response",
        _fake_process,
    )

    async def _exercise() -> list[dict[str, Any]]:
        api = GoogleFindMyAPI(oauth_token="aas_et/PROBE", google_email="Probe@Example.com")
        return await api.async_get_basic_device_list(token="aas_et/PROBE")

    result = asyncio.run(_exercise())

    assert result == [{"id": "device"}]
    assert captured["username"] == "Probe@Example.com"
    assert captured["token"] == "aas_et/PROBE"
    assert captured["namespace"] is None
    cache = captured["cache"]
    assert not hasattr(cache, "entry_id")
    assert hasattr(cache, "async_get_cached_value")
    assert hasattr(cache, "async_set_cached_value")
