# tests/test_adm_token_retrieval.py
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

import pytest

from custom_components.googlefindmy.Auth import (
    aas_token_retrieval,
    adm_token_retrieval,
    token_retrieval,
)
from custom_components.googlefindmy.Auth.token_retrieval import InvalidAasTokenError
from custom_components.googlefindmy.Auth.username_provider import username_string
from custom_components.googlefindmy.const import (
    CONF_OAUTH_TOKEN,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
)


class _DummyTokenCache:
    """Minimal async cache stub capturing reads/writes for assertions."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = dict(initial or {})
        self.set_calls: list[tuple[str, Any]] = []
        self.get_calls: list[str] = []

    async def get(self, name: str) -> Any:
        self.get_calls.append(name)
        return self._data.get(name)

    async def set(self, name: str, value: Any) -> None:
        self.set_calls.append((name, value))
        if value is None:
            self._data.pop(name, None)
        else:
            self._data[name] = value

    async def get_or_set(
        self,
        name: str,
        generator: Callable[[], Awaitable[Any] | Any],
    ) -> Any:
        if name in self._data and self._data[name] is not None:
            return self._data[name]

        candidate = generator()
        if asyncio.iscoroutine(candidate):
            candidate = await candidate

        await self.set(name, candidate)
        return candidate

    def values_for(self, key: str) -> list[Any]:
        """Return the recorded values written to a cache key."""

        return [value for recorded_key, value in self.set_calls if recorded_key == key]


def test_generate_adm_token_reuses_cached_aas(monkeypatch: pytest.MonkeyPatch) -> None:
    """AAS-based refresh must reuse the cached AAS token and avoid the provider."""

    async def _exercise() -> None:
        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/CACHED",
            }
        )

        perform_calls: list[tuple[str, str]] = []

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            perform_calls.append((username, aas_token))
            return {"Auth": "adm-token"}

        def fail_exchange(*args: Any, **kwargs: Any) -> dict[str, str]:
            raise AssertionError("OAuth exchange must not be invoked for cached AAS path")

        async def fail_provider(*args: Any, **kwargs: Any) -> str:
            raise AssertionError("AAS provider must not be called when cached token exists")

        monkeypatch.setattr(token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)
        monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fail_exchange)
        monkeypatch.setattr(adm_token_retrieval, "async_get_aas_token", fail_provider)

        token = await adm_token_retrieval._generate_adm_token("user@example.com", cache=cache)

        assert token == "adm-token"
        assert perform_calls == [("user@example.com", "aas_et/CACHED")]

    asyncio.run(_exercise())


def test_generate_adm_token_falls_back_to_provider_when_aas_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the cached AAS token is missing, the provider must be invoked exactly once."""

    async def _exercise() -> None:
        cache = _DummyTokenCache({DATA_AUTH_METHOD: "secrets_json"})

        provider_calls: list[str] = []

        async def fake_provider(*, cache: _DummyTokenCache) -> str:
            provider_calls.append("called")
            assert isinstance(cache, _DummyTokenCache)
            return "aas_et/FALLBACK"

        async def fake_request_token(
            username: str,
            service: str,
            *,
            cache: Any,
            aas_token: str | None,
            aas_provider: Callable[[], Awaitable[str]] | None,
        ) -> str:
            assert aas_token is None
            assert callable(aas_provider)
            return await aas_provider()

        monkeypatch.setattr(adm_token_retrieval, "async_get_aas_token", fake_provider)
        monkeypatch.setattr(adm_token_retrieval, "async_request_token", fake_request_token)

        token = await adm_token_retrieval._generate_adm_token("user@example.com", cache=cache)

        assert token == "aas_et/FALLBACK"
        assert len(provider_calls) == 1

    asyncio.run(_exercise())


def test_generate_adm_token_uses_provider_for_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth-based setups must exchange the OAuth token and perform AAS→ADM once."""

    async def _exercise() -> None:
        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "individual_tokens",
                CONF_OAUTH_TOKEN: "oauth-token",
                username_string: "user@example.com",
            }
        )

        exchange_calls: list[tuple[str, str]] = []
        perform_calls: list[str] = []

        def fake_exchange_token(username: str, oauth_token: str, android_id: int) -> dict[str, str]:
            exchange_calls.append((username, oauth_token))
            return {"Token": "aas_et/NEW"}

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            perform_calls.append(aas_token)
            return {"Auth": "adm-token"}

        monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange_token)
        monkeypatch.setattr(token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

        token = await adm_token_retrieval._generate_adm_token("user@example.com", cache=cache)

        assert token == "adm-token"
        assert exchange_calls == [("user@example.com", "oauth-token")]
        assert perform_calls == ["aas_et/NEW"]
        assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/NEW"

    asyncio.run(_exercise())


def test_async_request_token_uses_cached_android_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """async_request_token should use the android_id stored in FCM credentials."""

    async def _exercise() -> None:
        recorded: dict[str, Any] = {}

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            recorded["android_id"] = android_id
            recorded["username"] = username
            recorded["aas_token"] = aas_token
            recorded["kwargs"] = kwargs
            return {"Auth": "adm-token"}

        monkeypatch.setattr(token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

        cache = _DummyTokenCache({"fcm_credentials": {"gcm": {"android_id": "0x1A2B3C"}}})

        token = await token_retrieval.async_request_token(
            "user@example.com",
            "android_device_manager",
            cache=cache,
            aas_token="aas-token",
        )

        assert token == "adm-token"
        assert recorded["android_id"] == int("0x1A2B3C", 16)
        assert recorded["kwargs"]["service"].endswith("android_device_manager")

    asyncio.run(_exercise())


def test_async_request_token_uses_constant_android_id_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no android_id is cached, the legacy constant must be used."""

    async def _exercise() -> None:
        recorded: dict[str, Any] = {}

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            recorded["android_id"] = android_id
            return {"Auth": "adm-token"}

        monkeypatch.setattr(token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

        cache = _DummyTokenCache()

        token = await token_retrieval.async_request_token(
            "user@example.com",
            "android_device_manager",
            cache=cache,
            aas_token="aas-token",
        )

        assert token == "adm-token"
        assert recorded["android_id"] == token_retrieval._ANDROID_ID

    asyncio.run(_exercise())


def test_async_get_adm_token_retries_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient failures should retry without clearing unrelated cache entries."""

    async def _exercise() -> None:
        user = "user@example.com"
        attempts: list[int] = []
        sleep_durations: list[float] = []

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("temporary failure")
            assert username == user
            return "adm-success"

        async def fake_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)
        monkeypatch.setattr(adm_token_retrieval.asyncio, "sleep", fake_sleep)

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/MASTER",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        token = await adm_token_retrieval.async_get_adm_token(
            user,
            retries=1,
            backoff=1.0,
            cache=cache,
        )

        assert token == "adm-success"
        assert len(attempts) == 2
        assert sleep_durations == [1.0]
        assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/MASTER"
        assert cache._data.get(DATA_AUTH_METHOD) == "secrets_json"
        assert cache._data.get(CONF_OAUTH_TOKEN) == "oauth-token"
        assert cache._data.get(f"adm_token_{user}") == "adm-success"
        assert f"adm_token_issued_at_{user}" in cache._data
        assert f"adm_probe_startup_left_{user}" in cache._data
        assert (DATA_AAS_TOKEN, None) not in cache.set_calls

    asyncio.run(_exercise())


def test_async_get_adm_token_invalid_aas_without_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid AAS tokens without OAuth fallback must raise and clear cached AAS."""

    async def _exercise() -> None:
        user = "user@example.com"

        def fake_perform_oauth_sync(
            username: str,
            aas_token: str,
            scope: str,
            play_services: bool,
            *,
            android_id: int,
        ) -> str:
            raise InvalidAasTokenError("invalid AAS")

        monkeypatch.setattr(token_retrieval, "_perform_oauth_sync", fake_perform_oauth_sync)

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/STALE",
            }
        )

        with pytest.raises(InvalidAasTokenError):
            await adm_token_retrieval.async_get_adm_token(user, retries=1, cache=cache)

        assert cache._data.get(DATA_AUTH_METHOD) == "secrets_json"
        assert DATA_AAS_TOKEN not in cache._data
        assert cache._data.get(f"adm_token_{user}") is None

    asyncio.run(_exercise())


def test_async_get_adm_token_oauth_fallback_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed AAS path should fall back to OAuth once and restore the auth method."""

    async def _exercise() -> None:
        user = "user@example.com"
        perform_log: list[str] = []
        exchange_log: list[str] = []

        def fake_perform_oauth_sync(
            username: str,
            aas_token: str,
            scope: str,
            play_services: bool,
            *,
            android_id: int,
        ) -> str:
            perform_log.append(aas_token)
            if aas_token == "aas_et/OLD":
                raise InvalidAasTokenError("stale AAS")
            assert aas_token == "aas_et/NEW"
            return "adm-token-new"

        def fake_exchange_token(username: str, oauth_token: str, android_id: int) -> dict[str, str]:
            exchange_log.append(oauth_token)
            return {"Token": "aas_et/NEW"}

        monkeypatch.setattr(token_retrieval, "_perform_oauth_sync", fake_perform_oauth_sync)
        monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange_token)

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/OLD",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        token = await adm_token_retrieval.async_get_adm_token(user, retries=1, cache=cache)

        assert token == "adm-token-new"
        assert perform_log == ["aas_et/OLD", "aas_et/NEW"]
        assert exchange_log == ["oauth-token"]
        assert cache._data.get(DATA_AUTH_METHOD) == "secrets_json"
        assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/NEW"
        assert cache._data.get(f"adm_token_{user}") == "adm-token-new"
        assert f"adm_token_issued_at_{user}" in cache._data
        assert f"adm_probe_startup_left_{user}" in cache._data

    asyncio.run(_exercise())


def test_async_get_adm_token_oauth_fallback_success_after_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OAuth fallback must restore the auth method even after transient retries."""

    async def _exercise() -> None:
        user = "user@example.com"
        attempts: list[int] = []
        sleep_durations: list[float] = []

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            attempts.append(1)
            assert username == user
            if len(attempts) == 1:
                raise InvalidAasTokenError("stale AAS")
            if len(attempts) == 2:
                raise RuntimeError("temporary failure")
            assert cache._data.get(DATA_AUTH_METHOD) == "individual_tokens"
            return "adm-success"

        async def fake_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)
        monkeypatch.setattr(adm_token_retrieval.asyncio, "sleep", fake_sleep)

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/OLD",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        token = await adm_token_retrieval.async_get_adm_token(
            user,
            retries=2,
            backoff=1.0,
            cache=cache,
        )

        assert token == "adm-success"
        assert len(attempts) == 3
        assert sleep_durations == [2.0]
        assert cache._data.get(DATA_AUTH_METHOD) == "secrets_json"
        auth_method_writes = cache.values_for(DATA_AUTH_METHOD)
        assert auth_method_writes.count("individual_tokens") == 1
        assert auth_method_writes[-1] == "secrets_json"

    asyncio.run(_exercise())


def test_async_get_adm_token_oauth_fallback_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If both AAS and OAuth paths fail, the last auth error must surface."""

    async def _exercise() -> None:
        user = "user@example.com"
        perform_log: list[str] = []
        exchange_log: list[str] = []

        def fake_perform_oauth_sync(
            username: str,
            aas_token: str,
            scope: str,
            play_services: bool,
            *,
            android_id: int,
        ) -> str:
            perform_log.append(aas_token)
            raise InvalidAasTokenError("still invalid")

        def fake_exchange_token(username: str, oauth_token: str, android_id: int) -> dict[str, str]:
            exchange_log.append(oauth_token)
            return {"Token": "aas_et/NEW"}

        monkeypatch.setattr(token_retrieval, "_perform_oauth_sync", fake_perform_oauth_sync)
        monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange_token)

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/OLD",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        with pytest.raises(InvalidAasTokenError):
            await adm_token_retrieval.async_get_adm_token(user, retries=1, cache=cache)

        assert perform_log == ["aas_et/OLD", "aas_et/NEW"]
        assert exchange_log == ["oauth-token"]
        assert cache._data.get(DATA_AUTH_METHOD) == "secrets_json"
        assert DATA_AAS_TOKEN not in cache._data
        assert cache._data.get(f"adm_token_{user}") is None

    asyncio.run(_exercise())


def test_async_get_adm_token_oauth_fallback_not_reinvoked_after_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure after OAuth fallback must not trigger a second fallback."""

    async def _exercise() -> None:
        user = "user@example.com"
        attempts: list[int] = []
        sleep_durations: list[float] = []

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            attempts.append(1)
            idx = len(attempts)
            assert username == user
            if idx == 1:
                raise InvalidAasTokenError("stale AAS")
            if idx == 2:
                raise RuntimeError("temporary failure")
            if idx == 3:
                raise InvalidAasTokenError("still invalid")
            raise AssertionError("Unexpected additional ADM token generation attempt")

        async def fake_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)
        monkeypatch.setattr(adm_token_retrieval.asyncio, "sleep", fake_sleep)

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/OLD",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        with pytest.raises(InvalidAasTokenError):
            await adm_token_retrieval.async_get_adm_token(
                user,
                retries=2,
                cache=cache,
            )

        assert len(attempts) == 3
        assert sleep_durations == [2.0]

        auth_method_writes = cache.values_for(DATA_AUTH_METHOD)
        assert auth_method_writes.count(adm_token_retrieval._AUTH_METHOD_INDIVIDUAL_TOKENS) == 1
        assert auth_method_writes[-1] == "secrets_json"
        assert cache._data.get(DATA_AUTH_METHOD) == "secrets_json"
        assert cache._data.get(CONF_OAUTH_TOKEN) == "oauth-token"

    asyncio.run(_exercise())


def test_async_get_adm_token_oauth_path_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth-configured entries must not attempt a fallback on auth errors."""

    async def _exercise() -> None:
        user = "user@example.com"
        perform_log: list[str] = []
        exchange_log: list[str] = []

        def fake_perform_oauth_sync(
            username: str,
            aas_token: str,
            scope: str,
            play_services: bool,
            *,
            android_id: int,
        ) -> str:
            perform_log.append(aas_token)
            raise InvalidAasTokenError("oauth auth failure")

        def fake_exchange_token(username: str, oauth_token: str, android_id: int) -> dict[str, str]:
            exchange_log.append(oauth_token)
            return {"Token": "aas_et/NEW"}

        monkeypatch.setattr(token_retrieval, "_perform_oauth_sync", fake_perform_oauth_sync)
        monkeypatch.setattr(aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange_token)

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "individual_tokens",
                CONF_OAUTH_TOKEN: "oauth-token",
                username_string: user,
            }
        )

        with pytest.raises(InvalidAasTokenError):
            await adm_token_retrieval.async_get_adm_token(user, retries=1, cache=cache)

        assert perform_log == ["aas_et/NEW"]
        assert exchange_log == ["oauth-token"]
        assert cache._data.get(DATA_AUTH_METHOD) == "individual_tokens"
        assert DATA_AAS_TOKEN not in cache._data

    asyncio.run(_exercise())


def test_async_get_adm_token_success_sets_cache_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful retrieval must populate the ADM token, issued time, and probe keys."""

    async def _exercise() -> None:
        user = "user@example.com"

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            assert username == user
            await cache.set(DATA_AAS_TOKEN, "aas_et/MASTER")
            return "adm-success"

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)

        cache = _DummyTokenCache({DATA_AUTH_METHOD: "secrets_json"})

        token = await adm_token_retrieval.async_get_adm_token(user, cache=cache)

        assert token == "adm-success"
        assert cache._data.get(f"adm_token_{user}") == "adm-success"
        assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/MASTER"
        assert f"adm_token_issued_at_{user}" in cache._data
        assert f"adm_probe_startup_left_{user}" in cache._data

    asyncio.run(_exercise())


def test_async_get_adm_token_isolated_uses_bundle_android_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isolated config-flow path should use the secrets bundle android_id."""

    recorded: dict[str, Any] = {}

    def fake_perform_oauth(
        username: str,
        aas_token: str,
        android_id: int,
        **kwargs: Any,
    ) -> dict[str, str]:
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

    def fake_perform_oauth(
        username: str,
        aas_token: str,
        android_id: int,
        **kwargs: Any,
    ) -> dict[str, str]:
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

    def fake_perform_oauth(
        username: str,
        aas_token: str,
        android_id: int,
        **kwargs: Any,
    ) -> dict[str, str]:
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
