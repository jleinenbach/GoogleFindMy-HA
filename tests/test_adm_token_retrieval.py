# tests/test_adm_token_retrieval.py

"""Regression tests for ADM token retrieval Android ID handling."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import pytest

from custom_components.googlefindmy.Auth import adm_token_retrieval, token_retrieval
from custom_components.googlefindmy.const import DATA_AAS_TOKEN, DATA_AUTH_METHOD


class _DummyTokenCache:
    """Minimal cache stub exposing the subset used by async_request_token."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = initial or {}

    async def get(self, name: str) -> Any:
        return self._data.get(name)

    async def set(self, name: str, value: Any) -> None:
        if value is None:
            self._data.pop(name, None)
        else:
            self._data[name] = value


def test_generate_adm_token_reuses_cached_aas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets-based auth must reuse the cached AAS token without provider calls."""

    async def _exercise() -> None:
        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/MASTER",
            }
        )

        provider_called = False

        async def _fail_provider(*_: Any, **__: Any) -> str:
            nonlocal provider_called
            provider_called = True
            return "aas_et/SHOULD_NOT_BE_USED"

        async def _fake_request_token(
            username: str,
            service: str,
            *,
            cache: Any,
            aas_token: str | None,
            aas_provider: Callable[[], Awaitable[str]] | None,
        ) -> str:
            assert username == "user@example.com"
            assert service.endswith("android_device_manager")
            assert aas_token == "aas_et/MASTER"
            assert aas_provider is None
            return "adm-token"

        monkeypatch.setattr(adm_token_retrieval, "async_get_aas_token", _fail_provider)
        monkeypatch.setattr(adm_token_retrieval, "async_request_token", _fake_request_token)

        token = await adm_token_retrieval._generate_adm_token("user@example.com", cache=cache)

        assert token == "adm-token"
        assert not provider_called

    asyncio.run(_exercise())


def test_generate_adm_token_requires_cached_aas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing cached AAS tokens for secrets-based auth must raise."""

    async def _exercise() -> None:
        cache = _DummyTokenCache({DATA_AUTH_METHOD: "secrets_json"})

        async def _fake_request_token(*_: Any, **__: Any) -> str:
            raise AssertionError("async_request_token must not be called when AAS token is missing")

        monkeypatch.setattr(adm_token_retrieval, "async_request_token", _fake_request_token)

        with pytest.raises(RuntimeError, match=r"Required AAS token \(aas_token\) not found"):
            await adm_token_retrieval._generate_adm_token("user@example.com", cache=cache)

    asyncio.run(_exercise())


def test_generate_adm_token_uses_provider_for_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth-based auth must route through the async AAS provider."""

    async def _exercise() -> None:
        cache = _DummyTokenCache({DATA_AUTH_METHOD: "individual_tokens"})

        provider_calls = 0

        async def _fake_provider(*, cache: Any) -> str:
            nonlocal provider_calls
            provider_calls += 1
            assert isinstance(cache, _DummyTokenCache)
            return "aas_et/NEW"

        async def _fake_request_token(
            username: str,
            service: str,
            *,
            cache: Any,
            aas_token: str | None,
            aas_provider: Callable[[], Awaitable[str]] | None,
        ) -> str:
            assert aas_token is None
            assert callable(aas_provider)
            result = await aas_provider()
            assert result == "aas_et/NEW"
            return "adm-token"

        monkeypatch.setattr(adm_token_retrieval, "async_get_aas_token", _fake_provider)
        monkeypatch.setattr(adm_token_retrieval, "async_request_token", _fake_request_token)

        token = await adm_token_retrieval._generate_adm_token("user@example.com", cache=cache)

        assert token == "adm-token"
        assert provider_calls == 1

    asyncio.run(_exercise())


def test_async_request_token_uses_cached_android_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure async_request_token forwards the android_id from cached FCM credentials."""

    recorded: dict[str, Any] = {}

    def fake_perform_oauth(username: str, aas_token: str, android_id: int, **kwargs: Any) -> dict[str, str]:
        recorded["android_id"] = android_id
        recorded["username"] = username
        recorded["aas_token"] = aas_token
        recorded["kwargs"] = kwargs
        return {"Auth": "adm-token"}

    monkeypatch.setattr(token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

    cache = _DummyTokenCache({"fcm_credentials": {"gcm": {"android_id": "0x1A2B3C"}}})

    token = asyncio.run(
        token_retrieval.async_request_token(
            "user@example.com",
            "android_device_manager",
            cache=cache,
            aas_token="aas-token",
        )
    )

    assert token == "adm-token"
    assert recorded["android_id"] == int("0x1A2B3C", 16)
    assert recorded["kwargs"]["service"].endswith("android_device_manager")


def test_async_request_token_falls_back_to_constant_without_android_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no android_id is cached, the legacy constant is used."""

    recorded: dict[str, Any] = {}

    def fake_perform_oauth(username: str, aas_token: str, android_id: int, **kwargs: Any) -> dict[str, str]:
        recorded["android_id"] = android_id
        return {"Auth": "adm-token"}

    monkeypatch.setattr(token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

    cache = _DummyTokenCache()

    token = asyncio.run(
        token_retrieval.async_request_token(
            "user@example.com",
            "android_device_manager",
            cache=cache,
            aas_token="aas-token",
        )
    )

    assert token == "adm-token"
    assert recorded["android_id"] == token_retrieval._ANDROID_ID


def test_async_get_adm_token_isolated_uses_bundle_android_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolated config-flow path should use the secrets bundle android_id."""

    recorded: dict[str, Any] = {}

    def fake_perform_oauth(username: str, aas_token: str, android_id: int, **kwargs: Any) -> dict[str, str]:
        recorded["android_id"] = android_id
        return {"Auth": "adm-token"}

    monkeypatch.setattr(adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

    bundle = {
        "aas_token": "aas-token",
        "fcm_credentials": {"gcm": {"android_id": "0xC0FFEE"}},
    }

    token = asyncio.run(
        adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            secrets_bundle=bundle,
        )
    )

    assert token == "adm-token"
    assert recorded["android_id"] == int("0xC0FFEE", 16)


def test_async_get_adm_token_isolated_prefers_cache_android_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the secrets bundle lacks the id, the flow cache should supply it."""

    recorded: dict[str, Any] = {}

    def fake_perform_oauth(username: str, aas_token: str, android_id: int, **kwargs: Any) -> dict[str, str]:
        recorded["android_id"] = android_id
        return {"Auth": "adm-token"}

    monkeypatch.setattr(adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

    async def cache_get(key: str) -> Any:
        if key == "fcm_credentials":
            return {"gcm": {"android_id": "0xF00D"}}
        return None

    async def cache_set(key: str, value: Any) -> None:
        return None

    token = asyncio.run(
        adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            secrets_bundle={"aas_token": "aas-token"},
            cache_get=cache_get,
            cache_set=cache_set,
        )
    )

    assert token == "adm-token"
    assert recorded["android_id"] == int("0xF00D", 16)


def test_async_get_adm_token_isolated_falls_back_without_android_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If neither secrets nor cache contain an ID, the constant is used."""

    recorded: dict[str, Any] = {}

    def fake_perform_oauth(username: str, aas_token: str, android_id: int, **kwargs: Any) -> dict[str, str]:
        recorded["android_id"] = android_id
        return {"Auth": "adm-token"}

    monkeypatch.setattr(adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

    async def cache_get(key: str) -> Any:
        return None

    async def cache_set(key: str, value: Any) -> None:
        return None

    token = asyncio.run(
        adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            secrets_bundle={"aas_token": "aas-token"},
            cache_get=cache_get,
            cache_set=cache_set,
        )
    )

    assert token == "adm-token"
    assert recorded["android_id"] == adm_token_retrieval._ANDROID_ID
