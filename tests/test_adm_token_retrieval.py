# tests/test_adm_token_retrieval.py
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

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
            return {"Token": "adm-token"}

        def fail_exchange(*args: Any, **kwargs: Any) -> dict[str, str]:
            raise AssertionError(
                "OAuth exchange must not be invoked for cached AAS path"
            )

        async def fail_provider(*args: Any, **kwargs: Any) -> str:
            raise AssertionError(
                "AAS provider must not be called when cached token exists"
            )

        monkeypatch.setattr(
            token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )
        monkeypatch.setattr(
            aas_token_retrieval.gpsoauth, "exchange_token", fail_exchange
        )
        monkeypatch.setattr(adm_token_retrieval, "async_get_aas_token", fail_provider)

        token = await adm_token_retrieval._generate_adm_token(
            "user@example.com", cache=cache
        )

        assert token == "adm-token"
        assert perform_calls == [("user@example.com", "aas_et/CACHED")]

    asyncio.run(_exercise())


def test_resolve_android_id_for_isolated_flow_prefers_cached_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cached android_id entries must be reused before generating a new one."""

    async def _exercise() -> None:
        cache = _DummyTokenCache(
            {"android_id_user@example.com": "0x1234", "fcm_credentials": {}}
        )

        android_id = await adm_token_retrieval._resolve_android_id_for_isolated_flow(
            "user@example.com",
            secrets_bundle=None,
            cache_get=cache.get,
            cache_set=cache.set,
        )

        assert android_id == int("0x1234", 16)

    asyncio.run(_exercise())


def test_resolve_android_id_for_isolated_flow_generates_and_persists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing android_id must be generated and stored for later reuse."""

    async def _exercise() -> None:
        cache = _DummyTokenCache()

        # secrets.randbelow(0xF000000000000000) + 0x1000000000000000 = final value
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_, **__: 0xABCDEF
        )
        expected_id = 0xABCDEF + 0x1000000000000000

        android_id = await adm_token_retrieval._resolve_android_id_for_isolated_flow(
            "user@example.com",
            secrets_bundle=None,
            cache_get=cache.get,
            cache_set=cache.set,
        )

        assert android_id == expected_id
        assert cache._data["android_id_user@example.com"] == expected_id

    asyncio.run(_exercise())


def test_normalize_service_accepts_full_scope() -> None:
    """Full OAuth scope strings must normalize back to the scope suffix."""

    normalized = adm_token_retrieval._normalize_service(
        "oauth2:https://www.googleapis.com/auth/android_device_manager"
    )

    assert normalized == "android_device_manager"


def test_is_non_retryable_auth_for_invalid_aas_token_error() -> None:
    """InvalidAasTokenError must be treated as non-retryable."""

    err = InvalidAasTokenError("cached token expired")

    assert adm_token_retrieval._is_non_retryable_auth(err) is True


def test_is_non_retryable_auth_for_missing_auth_marker() -> None:
    """Errors with the gpsoauth missing-auth marker must not be retried."""

    err = RuntimeError("missing 'auth' in gpsoauth response")

    assert adm_token_retrieval._is_non_retryable_auth(err) is True


def test_is_non_retryable_auth_allows_transient_errors() -> None:
    """Unrelated transient failures must remain retryable."""

    err = ConnectionError("temporary backend outage, please retry")

    assert adm_token_retrieval._is_non_retryable_auth(err) is False


def test_is_non_retryable_auth_via_error_kind_exchange_error_is_retryable() -> None:
    """exchange_error mirrors the aas-side contract: catch-all wrapper for
    executor failures (including transient errors), MUST remain retryable.
    Only explicit auth markers (auth_error, badauthentication,
    invalid_grant) surface as non-retryable through the attribute path.
    """

    err = RuntimeError("sanitized message")
    err.error_kind = "exchange_error"  # type: ignore[attr-defined]

    assert adm_token_retrieval._is_non_retryable_auth(err) is False


def test_is_non_retryable_auth_via_error_kind_badauthentication() -> None:
    """gpsoauth Error field values surface via error_kind (case-insensitive)."""

    err = RuntimeError("sanitized message")
    err.error_kind = "BadAuthentication"  # type: ignore[attr-defined]

    assert adm_token_retrieval._is_non_retryable_auth(err) is True


def test_is_non_retryable_auth_string_fallback_still_works() -> None:
    """RuntimeError without error_kind attribute still matches via substring."""

    err = RuntimeError("server returned 401 unauthorized")

    assert adm_token_retrieval._is_non_retryable_auth(err) is True


def test_is_non_retryable_auth_falsy_error_kind_falls_back_to_string() -> None:
    """F4: empty error_kind must not short-circuit the string path."""

    err = RuntimeError("temporary network outage")
    err.error_kind = ""  # type: ignore[attr-defined]

    assert adm_token_retrieval._is_non_retryable_auth(err) is False


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
            assert service == "android_device_manager"
            return await aas_provider()

        monkeypatch.setattr(adm_token_retrieval, "async_get_aas_token", fake_provider)
        monkeypatch.setattr(
            adm_token_retrieval, "async_request_token", fake_request_token
        )

        token = await adm_token_retrieval._generate_adm_token(
            "user@example.com", cache=cache
        )

        assert token == "aas_et/FALLBACK"
        assert len(provider_calls) == 1

    asyncio.run(_exercise())


def test_generate_adm_token_uses_provider_for_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        def fake_exchange_token(
            username: str, oauth_token: str, android_id: int
        ) -> dict[str, str]:
            exchange_calls.append((username, oauth_token))
            return {"Token": "aas_et/NEW"}

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            perform_calls.append(aas_token)
            return {"Token": "adm-token"}

        monkeypatch.setattr(
            aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange_token
        )
        monkeypatch.setattr(
            token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )

        token = await adm_token_retrieval._generate_adm_token(
            "user@example.com", cache=cache
        )

        assert token == "adm-token"
        assert exchange_calls == [("user@example.com", "oauth-token")]
        assert perform_calls == ["aas_et/NEW"]
        assert cache._data.get(DATA_AAS_TOKEN) == "aas_et/NEW"

    asyncio.run(_exercise())


def test_generate_adm_token_refreshes_android_id_from_fcm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FCM credentials must refresh cached android_id entries before token request."""

    async def _exercise() -> None:
        user = "user@example.com"
        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/MASTER",
                f"android_id_{user}": 0xDEADBEEF,
                "fcm_credentials": {"gcm": {"android_id": "0x1234"}},
            }
        )

        recorded_android_ids: list[int | None] = []

        async def fake_request_token(
            username: str,
            service: str,
            *,
            cache: _DummyTokenCache,
            aas_token: str | None,
            aas_provider: Callable[[], Awaitable[str]] | None,
        ) -> str:
            recorded_android_ids.append(cache._data.get(f"android_id_{username}"))
            return "adm-token"

        monkeypatch.setattr(
            adm_token_retrieval, "async_request_token", fake_request_token
        )

        token = await adm_token_retrieval._generate_adm_token(user, cache=cache)

        assert token == "adm-token"
        assert recorded_android_ids == [int("0x1234", 16)]
        assert cache._data[f"android_id_{user}"] == int("0x1234", 16)

    asyncio.run(_exercise())


def test_async_request_token_uses_cached_android_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
            return {"Token": "adm-token"}

        monkeypatch.setattr(
            token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )

        cache = _DummyTokenCache(
            {"fcm_credentials": {"gcm": {"android_id": "0x1A2B3C"}}}
        )

        token = await token_retrieval.async_request_token(
            "user@example.com",
            "android_device_manager",
            cache=cache,
            aas_token="aas-token",
        )

        assert token == "adm-token"
        assert recorded["android_id"] == int("0x1A2B3C", 16)
        assert cache._data["android_id_user@example.com"] == int("0x1A2B3C", 16)
        assert recorded["kwargs"] == {
            "service": "oauth2:https://www.googleapis.com/auth/android_device_manager",
            "app": "com.google.android.apps.adm",
            "client_sig": "38918a453d07199354f8b19af05ec6562ced5788",
        }

    asyncio.run(_exercise())


def test_async_request_token_generates_android_id_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing android_id should be generated and persisted for reuse."""

    async def _exercise() -> None:
        recorded: dict[str, Any] = {}
        generated_id = 0xCAFEBABE12345678

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            recorded["android_id"] = android_id
            return {"Token": "adm-token"}

        monkeypatch.setattr(
            token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )
        monkeypatch.setattr(
            token_retrieval.secrets,
            "randbelow",
            lambda _: generated_id - 0x1000000000000000,
        )

        cache = _DummyTokenCache()

        token = await token_retrieval.async_request_token(
            "user@example.com",
            "android_device_manager",
            cache=cache,
            aas_token="aas-token",
        )

        assert token == "adm-token"
        assert recorded["android_id"] == generated_id
        assert cache._data["android_id_user@example.com"] == generated_id

    asyncio.run(_exercise())


def test_perform_oauth_sync_missing_keys_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gpsoauth responses without Token/Auth must raise a clear runtime error."""

    def fake_perform_oauth(
        username: str,
        aas_token: str,
        android_id: str,
        **kwargs: Any,
    ) -> dict[str, str]:
        return {"Error": "SomeOtherFailure"}

    monkeypatch.setattr(token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth)

    with pytest.raises(RuntimeError, match="Neither 'Token' nor 'Auth'"):
        token_retrieval._perform_oauth_sync(
            "user@example.com",
            "aas-token",
            "android_device_manager",
            play_services=False,
            android_id=0x1234,
        )


def test_async_get_adm_token_retries_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_async_get_adm_token_invalid_aas_without_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        monkeypatch.setattr(
            token_retrieval, "_perform_oauth_sync", fake_perform_oauth_sync
        )

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


def test_async_get_adm_token_oauth_fallback_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        def fake_exchange_token(
            username: str, oauth_token: str, android_id: int
        ) -> dict[str, str]:
            exchange_log.append(oauth_token)
            return {"Token": "aas_et/NEW"}

        monkeypatch.setattr(
            token_retrieval, "_perform_oauth_sync", fake_perform_oauth_sync
        )
        monkeypatch.setattr(
            aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange_token
        )

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                DATA_AAS_TOKEN: "aas_et/OLD",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        token = await adm_token_retrieval.async_get_adm_token(
            user, retries=1, cache=cache
        )

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


def test_async_get_adm_token_oauth_fallback_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        def fake_exchange_token(
            username: str, oauth_token: str, android_id: int
        ) -> dict[str, str]:
            exchange_log.append(oauth_token)
            return {"Token": "aas_et/NEW"}

        monkeypatch.setattr(
            token_retrieval, "_perform_oauth_sync", fake_perform_oauth_sync
        )
        monkeypatch.setattr(
            aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange_token
        )

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
        assert (
            auth_method_writes.count(adm_token_retrieval._AUTH_METHOD_INDIVIDUAL_TOKENS)
            == 1
        )
        assert auth_method_writes[-1] == "secrets_json"
        assert cache._data.get(DATA_AUTH_METHOD) == "secrets_json"
        assert cache._data.get(CONF_OAUTH_TOKEN) == "oauth-token"

    asyncio.run(_exercise())


def test_async_get_adm_token_oauth_path_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

        def fake_exchange_token(
            username: str, oauth_token: str, android_id: int
        ) -> dict[str, str]:
            exchange_log.append(oauth_token)
            return {"Token": "aas_et/NEW"}

        monkeypatch.setattr(
            token_retrieval, "_perform_oauth_sync", fake_perform_oauth_sync
        )
        monkeypatch.setattr(
            aas_token_retrieval.gpsoauth, "exchange_token", fake_exchange_token
        )

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
        return {"Token": "adm-token"}

    monkeypatch.setattr(
        adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
    )

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
        return {"Token": "adm-token"}

    monkeypatch.setattr(
        adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
    )

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
        return {"Token": "adm-token"}

    monkeypatch.setattr(
        adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
    )

    async def cache_get(key: str) -> Any:
        return None

    async def cache_set(key: str, value: Any) -> None:
        return None

    # secrets.randbelow(0xF000000000000000) + 0x1000000000000000 = final value
    monkeypatch.setattr(
        adm_token_retrieval.secrets, "randbelow", lambda *_, **__: 0xABCDEF01
    )
    expected_android_id = 0xABCDEF01 + 0x1000000000000000

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
    assert recorded["android_id"] == expected_android_id


# ---------------------------------------------------------------------------
# Additional tests for 100% coverage of adm_token_retrieval.py
# ---------------------------------------------------------------------------


def test_mask_email_no_at_sign() -> None:
    """Emails without @ should return <unknown>."""
    assert adm_token_retrieval._mask_email("noemail") == "<unknown>"
    assert adm_token_retrieval._mask_email("") == "<unknown>"
    assert adm_token_retrieval._mask_email(None) == "<unknown>"


def test_mask_email_empty_local_part() -> None:
    """Emails with empty local part should mask properly."""
    assert adm_token_retrieval._mask_email("@example.com") == "*@example.com"


def test_mask_email_single_char_local() -> None:
    """Emails with single-char local part should return single asterisk."""
    assert adm_token_retrieval._mask_email("a@example.com") == "*@example.com"


def test_clip_truncates_long_strings() -> None:
    """Strings longer than the limit should be truncated with ellipsis."""
    long_string = "x" * 300
    result = adm_token_retrieval._clip(long_string, limit=200)
    assert len(result) == 200
    assert result.endswith("…")


def test_coerce_android_id_string_invalid() -> None:
    """Invalid string android_id should return None."""
    assert adm_token_retrieval._coerce_android_id("not_a_number", "test") is None


def test_coerce_android_id_unsupported_type() -> None:
    """Unsupported types should return None."""
    assert adm_token_retrieval._coerce_android_id([1, 2, 3], "test") is None


def test_coerce_android_id_none() -> None:
    """None should return None without logging."""
    assert adm_token_retrieval._coerce_android_id(None, "test") is None


def test_normalize_service_adm_alias() -> None:
    """ADM aliases should normalize correctly."""
    assert (
        adm_token_retrieval._normalize_service("android_device_manager")
        == "android_device_manager"
    )
    assert adm_token_retrieval._normalize_service("ADM") == "android_device_manager"
    assert adm_token_retrieval._normalize_service("adm") == "android_device_manager"


def test_normalize_service_custom() -> None:
    """Custom service names should pass through unchanged."""
    assert adm_token_retrieval._normalize_service("custom_service") == "custom_service"


def test_normalize_service_whitespace() -> None:
    """Whitespace should be stripped."""
    assert (
        adm_token_retrieval._normalize_service("  android_device_manager  ")
        == "android_device_manager"
    )


def test_is_non_retryable_auth_http_signals() -> None:
    """HTTP-style auth denials should not be retryable."""
    assert adm_token_retrieval._is_non_retryable_auth(RuntimeError("401")) is True
    assert adm_token_retrieval._is_non_retryable_auth(RuntimeError("403")) is True


def test_is_non_retryable_auth_neither_marker() -> None:
    """Errors with neither Token nor Auth markers should not be retryable."""
    err = RuntimeError("Neither 'Token' nor 'Auth' found")
    assert adm_token_retrieval._is_non_retryable_auth(err) is True


def test_seed_username_in_cache_requires_cache() -> None:
    """_seed_username_in_cache must raise ValueError without cache."""
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        asyncio.run(
            adm_token_retrieval._seed_username_in_cache("user@example.com", cache=None)
        )


def test_seed_username_in_cache_exception_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache exceptions during seeding should be handled gracefully."""

    async def _exercise() -> None:
        class FailingCache(_DummyTokenCache):
            async def get(self, name: str) -> Any:
                raise RuntimeError("Cache read failed")

        cache = FailingCache()
        # Should not raise
        await adm_token_retrieval._seed_username_in_cache(
            "user@example.com", cache=cache
        )

    asyncio.run(_exercise())


def test_resolve_android_id_for_entry_requires_cache() -> None:
    """_resolve_android_id_for_entry must raise ValueError without cache."""
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        asyncio.run(
            adm_token_retrieval._resolve_android_id_for_entry(
                "user@example.com", cache=None
            )
        )


def test_resolve_android_id_for_entry_cache_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache write failures should be handled gracefully."""

    async def _exercise() -> None:
        class FailingWriteCache(_DummyTokenCache):
            async def set(self, name: str, value: Any) -> None:
                if "android_id" in name:
                    raise RuntimeError("Cache write failed")
                await super().set(name, value)

        cache = FailingWriteCache()
        cache._data["fcm_credentials"] = {"gcm": {"android_id": "0x1234"}}

        # Should still return the android_id despite cache write failure
        result = await adm_token_retrieval._resolve_android_id_for_entry(
            "user@example.com", cache=cache
        )
        assert result == 0x1234

    asyncio.run(_exercise())


def test_resolve_android_id_for_entry_generates_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing android_id should be generated and persisted."""

    async def _exercise() -> None:
        cache = _DummyTokenCache()
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )
        expected = 0xABCDEF + 0x1000000000000000

        result = await adm_token_retrieval._resolve_android_id_for_entry(
            "user@example.com", cache=cache
        )

        assert result == expected
        assert cache._data["android_id_user@example.com"] == expected

    asyncio.run(_exercise())


def test_resolve_android_id_for_entry_generation_cache_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache write failures during generation should be handled gracefully."""

    async def _exercise() -> None:
        class FailingWriteCache(_DummyTokenCache):
            async def set(self, name: str, value: Any) -> None:
                if "android_id" in name:
                    raise RuntimeError("Cache write failed")
                await super().set(name, value)

        cache = FailingWriteCache()
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )
        expected = 0xABCDEF + 0x1000000000000000

        result = await adm_token_retrieval._resolve_android_id_for_entry(
            "user@example.com", cache=cache
        )
        assert result == expected

    asyncio.run(_exercise())


def test_generate_adm_token_requires_cache() -> None:
    """_generate_adm_token must raise ValueError without cache."""
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        asyncio.run(
            adm_token_retrieval._generate_adm_token("user@example.com", cache=None)
        )


def test_async_get_adm_token_requires_cache() -> None:
    """async_get_adm_token must raise ValueError without cache."""
    with pytest.raises(ValueError, match="TokenCache instance is required"):
        asyncio.run(adm_token_retrieval.async_get_adm_token(cache=None))


def test_async_get_adm_token_empty_username() -> None:
    """Empty username should raise RuntimeError."""

    async def _exercise() -> None:
        cache = _DummyTokenCache()
        with pytest.raises(RuntimeError, match="Username is empty/invalid"):
            await adm_token_retrieval.async_get_adm_token("", cache=cache)

    asyncio.run(_exercise())


def test_async_get_adm_token_whitespace_username() -> None:
    """Whitespace-only username should raise RuntimeError."""

    async def _exercise() -> None:
        cache = _DummyTokenCache()
        with pytest.raises(RuntimeError, match="Username is empty/invalid"):
            await adm_token_retrieval.async_get_adm_token("   ", cache=cache)

    asyncio.run(_exercise())


def test_resolve_android_id_for_isolated_flow_cache_get_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache get failures should be handled gracefully."""

    async def _exercise() -> None:
        call_count = 0

        async def failing_get(key: str) -> Any:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Cache read failed")
            return None

        async def cache_set(key: str, value: Any) -> None:
            pass

        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )
        expected = 0xABCDEF + 0x1000000000000000

        result = await adm_token_retrieval._resolve_android_id_for_isolated_flow(
            "user@example.com",
            secrets_bundle=None,
            cache_get=failing_get,
            cache_set=cache_set,
        )
        assert result == expected

    asyncio.run(_exercise())


def test_resolve_android_id_for_isolated_flow_fcm_cache_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FCM credentials cache failures should be handled gracefully."""

    async def _exercise() -> None:
        async def failing_get(key: str) -> Any:
            if key == "fcm_credentials":
                raise RuntimeError("FCM cache read failed")
            return None

        async def cache_set(key: str, value: Any) -> None:
            pass

        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )
        expected = 0xABCDEF + 0x1000000000000000

        result = await adm_token_retrieval._resolve_android_id_for_isolated_flow(
            "user@example.com",
            secrets_bundle=None,
            cache_get=failing_get,
            cache_set=cache_set,
        )
        assert result == expected

    asyncio.run(_exercise())


def test_resolve_android_id_for_isolated_flow_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache set failures during android_id persistence should be handled."""

    async def _exercise() -> None:
        async def cache_get(key: str) -> Any:
            return None

        async def failing_set(key: str, value: Any) -> None:
            raise RuntimeError("Cache write failed")

        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )
        expected = 0xABCDEF + 0x1000000000000000

        result = await adm_token_retrieval._resolve_android_id_for_isolated_flow(
            "user@example.com",
            secrets_bundle=None,
            cache_get=cache_get,
            cache_set=failing_set,
        )
        assert result == expected

    asyncio.run(_exercise())


def test_resolve_android_id_for_isolated_flow_secrets_bundle_persist_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache set failures after reading from secrets bundle should be handled."""

    async def _exercise() -> None:
        async def cache_get(key: str) -> Any:
            return None

        async def failing_set(key: str, value: Any) -> None:
            raise RuntimeError("Cache write failed")

        bundle = {"fcm_credentials": {"gcm": {"android_id": "0x1234"}}}

        result = await adm_token_retrieval._resolve_android_id_for_isolated_flow(
            "user@example.com",
            secrets_bundle=bundle,
            cache_get=cache_get,
            cache_set=failing_set,
        )
        assert result == 0x1234

    asyncio.run(_exercise())


def test_perform_oauth_with_provided_aas_non_dict_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-dict gpsoauth response should raise RuntimeError."""

    async def _exercise() -> None:
        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> list[Any]:
            return []

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )

        with pytest.raises(RuntimeError, match="non-dict response"):
            await adm_token_retrieval._perform_oauth_with_provided_aas(
                "user@example.com", "aas-token", android_id=0x1234
            )

    asyncio.run(_exercise())


def test_perform_oauth_with_provided_aas_uses_auth_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy Auth field should be used if Token is missing."""

    async def _exercise() -> None:
        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            return {"Auth": "adm-from-auth"}

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )

        result = await adm_token_retrieval._perform_oauth_with_provided_aas(
            "user@example.com", "aas-token", android_id=0x1234
        )
        assert result == "adm-from-auth"

    asyncio.run(_exercise())


def test_perform_oauth_with_provided_aas_error_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error response should raise RuntimeError with error details."""

    async def _exercise() -> None:
        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            return {"Error": "BadAuthentication"}

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )

        with pytest.raises(RuntimeError, match="Missing 'Token'/'Auth'"):
            await adm_token_retrieval._perform_oauth_with_provided_aas(
                "user@example.com", "aas-token", android_id=0x1234
            )

    asyncio.run(_exercise())


def test_perform_oauth_with_provided_aas_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions during oauth should be re-raised."""

    async def _exercise() -> None:
        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            raise ValueError("Unexpected error")

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )

        with pytest.raises(ValueError, match="Unexpected error"):
            await adm_token_retrieval._perform_oauth_with_provided_aas(
                "user@example.com", "aas-token", android_id=0x1234
            )

    asyncio.run(_exercise())


def test_async_get_adm_token_isolated_empty_username() -> None:
    """Empty username should raise RuntimeError."""
    with pytest.raises(RuntimeError, match="Username is empty/invalid"):
        asyncio.run(
            adm_token_retrieval.async_get_adm_token_isolated("", aas_token="aas-token")
        )


def test_async_get_adm_token_isolated_no_aas_token() -> None:
    """Missing AAS token should raise RuntimeError."""
    with pytest.raises(RuntimeError, match="requires an AAS token"):
        asyncio.run(
            adm_token_retrieval.async_get_adm_token_isolated("user@example.com")
        )


def test_async_get_adm_token_isolated_extracts_from_bundle() -> None:
    """AAS token should be extracted from secrets bundle."""

    async def _exercise(monkeypatch: pytest.MonkeyPatch) -> None:
        recorded: dict[str, Any] = {}

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            recorded["aas_token"] = aas_token
            return {"Token": "adm-token"}

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )

        result = await adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            secrets_bundle={"aas_token": "bundle-aas-token"},
        )

        assert result == "adm-token"
        assert recorded["aas_token"] == "bundle-aas-token"

    import pytest

    monkeypatch = pytest.MonkeyPatch()
    asyncio.run(_exercise(monkeypatch))


def test_async_get_adm_token_isolated_retries_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient errors should be retried with backoff."""

    async def _exercise() -> None:
        attempts: list[int] = []
        sleep_durations: list[float] = []

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            attempts.append(1)
            if len(attempts) < 2:
                raise RuntimeError("Temporary failure")
            return {"Token": "adm-token"}

        async def fake_sleep(duration: float) -> None:
            sleep_durations.append(duration)

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )
        monkeypatch.setattr(adm_token_retrieval.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )

        result = await adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            retries=1,
            backoff=1.0,
        )

        assert result == "adm-token"
        assert len(attempts) == 2
        assert sleep_durations == [1.0]

    asyncio.run(_exercise())


def test_async_get_adm_token_isolated_no_retry_on_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth errors should not be retried."""

    async def _exercise() -> None:
        attempts: list[int] = []

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            attempts.append(1)
            raise RuntimeError("BadAuthentication")

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )

        with pytest.raises(RuntimeError, match="BadAuthentication"):
            await adm_token_retrieval.async_get_adm_token_isolated(
                "user@example.com",
                aas_token="aas-token",
                retries=3,
            )

        assert len(attempts) == 1

    asyncio.run(_exercise())


def test_async_get_adm_token_isolated_persists_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token metadata should be persisted via cache_set."""

    async def _exercise() -> None:
        persisted: dict[str, Any] = {}

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            return {"Token": "adm-token"}

        async def cache_get(key: str) -> Any:
            return None

        async def cache_set(key: str, value: Any) -> None:
            persisted[key] = value

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )

        result = await adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            cache_get=cache_get,
            cache_set=cache_set,
        )

        assert result == "adm-token"
        assert "adm_token_user@example.com" in persisted
        assert "adm_token_issued_at_user@example.com" in persisted
        assert "adm_probe_startup_left_user@example.com" in persisted

    asyncio.run(_exercise())


def test_async_get_adm_token_isolated_metadata_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metadata write failures should be handled gracefully."""

    async def _exercise() -> None:
        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            return {"Token": "adm-token"}

        async def cache_get(key: str) -> Any:
            return None

        async def failing_set(key: str, value: Any) -> None:
            if "issued_at" in key or "probe" in key:
                raise RuntimeError("Metadata write failed")

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )

        # Should still succeed despite metadata write failures
        result = await adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            cache_get=cache_get,
            cache_set=failing_set,
        )

        assert result == "adm-token"

    asyncio.run(_exercise())


def test_async_get_adm_token_isolated_checks_existing_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing metadata should prevent overwriting."""

    async def _exercise() -> None:
        persisted: dict[str, Any] = {}

        def fake_perform_oauth(
            username: str,
            aas_token: str,
            android_id: int,
            **kwargs: Any,
        ) -> dict[str, str]:
            return {"Token": "adm-token"}

        async def cache_get(key: str) -> Any:
            if "issued_at" in key or "probe" in key:
                return "existing"
            return None

        async def cache_set(key: str, value: Any) -> None:
            persisted[key] = value

        monkeypatch.setattr(
            adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
        )
        monkeypatch.setattr(
            adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF
        )

        await adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            cache_get=cache_get,
            cache_set=cache_set,
        )

        # Existing metadata should not be overwritten
        assert "adm_token_issued_at_user@example.com" not in persisted
        assert "adm_probe_startup_left_user@example.com" not in persisted

    asyncio.run(_exercise())


def test_async_get_adm_token_clear_cache_on_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient errors should clear the cache key for retry."""

    async def _exercise() -> None:
        user = "user@example.com"
        attempts: list[int] = []

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("temporary failure")
            return "adm-success"

        async def fake_sleep(duration: float) -> None:
            pass

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)
        monkeypatch.setattr(adm_token_retrieval.asyncio, "sleep", fake_sleep)

        cache = _DummyTokenCache({DATA_AUTH_METHOD: "secrets_json"})

        token = await adm_token_retrieval.async_get_adm_token(
            user, retries=1, cache=cache
        )

        assert token == "adm-success"
        assert len(attempts) == 2
        # Verify cache was cleared before retry
        cleared_calls = [
            (k, v) for k, v in cache.set_calls if k == f"adm_token_{user}" and v is None
        ]
        assert len(cleared_calls) >= 1

    asyncio.run(_exercise())


def test_async_get_adm_token_clear_cache_failure_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache clear failures should be silent (best-effort)."""

    async def _exercise() -> None:
        user = "user@example.com"
        attempts: list[int] = []

        class PartialFailCache(_DummyTokenCache):
            async def set(self, name: str, value: Any) -> None:
                if value is None and "adm_token" in name:
                    raise RuntimeError("Cache clear failed")
                await super().set(name, value)

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("temporary failure")
            return "adm-success"

        async def fake_sleep(duration: float) -> None:
            pass

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)
        monkeypatch.setattr(adm_token_retrieval.asyncio, "sleep", fake_sleep)

        cache = PartialFailCache({DATA_AUTH_METHOD: "secrets_json"})

        # Should succeed despite cache clear failure
        token = await adm_token_retrieval.async_get_adm_token(
            user, retries=1, cache=cache
        )
        assert token == "adm-success"

    asyncio.run(_exercise())


def test_async_get_adm_token_finally_restores_auth_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth method should be restored in finally block after OAuth fallback."""

    async def _exercise() -> None:
        user = "user@example.com"

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            if cache._data.get(DATA_AUTH_METHOD) == "secrets_json":
                raise InvalidAasTokenError("stale AAS")
            return "adm-success"

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)

        cache = _DummyTokenCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        token = await adm_token_retrieval.async_get_adm_token(user, cache=cache)

        assert token == "adm-success"
        # Auth method should be restored
        assert cache._data.get(DATA_AUTH_METHOD) == "secrets_json"

    asyncio.run(_exercise())


def test_async_get_adm_token_finally_block_read_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finally block should handle auth_method read failures."""

    async def _exercise() -> None:
        user = "user@example.com"
        call_count = [0]

        class PartialFailCache(_DummyTokenCache):
            async def get(self, name: str) -> Any:
                if name == DATA_AUTH_METHOD and call_count[0] > 2 and call_count[0] < 5:
                    call_count[0] += 1
                    raise RuntimeError("Read failed")
                call_count[0] += 1
                return await super().get(name)

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            if call_count[0] < 3:
                raise InvalidAasTokenError("stale AAS")
            return "adm-success"

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)

        cache = PartialFailCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        token = await adm_token_retrieval.async_get_adm_token(user, cache=cache)
        assert token == "adm-success"

    asyncio.run(_exercise())


def test_async_get_adm_token_finally_block_write_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Finally block should handle auth_method write failures."""

    async def _exercise() -> None:
        user = "user@example.com"
        generate_count = [0]

        class PartialFailCache(_DummyTokenCache):
            async def set(self, name: str, value: Any) -> None:
                # Fail on restore attempt in finally block
                if name == DATA_AUTH_METHOD and value == "secrets_json":
                    if generate_count[0] > 1:
                        raise RuntimeError("Write failed")
                await super().set(name, value)

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            generate_count[0] += 1
            if generate_count[0] == 1:
                raise InvalidAasTokenError("stale AAS")
            return "adm-success"

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)

        cache = PartialFailCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        # Should succeed despite restore failure
        token = await adm_token_retrieval.async_get_adm_token(user, cache=cache)
        assert token == "adm-success"

    asyncio.run(_exercise())


def test_normalize_service_empty() -> None:
    """Empty or None service should return empty string."""
    assert adm_token_retrieval._normalize_service("") == ""
    assert adm_token_retrieval._normalize_service(None) == ""


def test_resolve_android_id_for_entry_fcm_without_gcm() -> None:
    """FCM credentials without gcm block should fall back to cached."""

    async def _exercise() -> None:
        cache = _DummyTokenCache(
            {"fcm_credentials": {"not_gcm": {}}, "android_id_user@example.com": 0x12345}
        )

        result = await adm_token_retrieval._resolve_android_id_for_entry(
            "user@example.com", cache=cache
        )
        assert result == 0x12345

    asyncio.run(_exercise())


def test_async_get_adm_token_auth_method_switch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth method switch failure should be logged but fallback continues."""

    async def _exercise() -> None:
        user = "user@example.com"
        switch_attempts = [0]

        class PartialFailCache(_DummyTokenCache):
            async def set(self, name: str, value: Any) -> None:
                if (
                    name == DATA_AUTH_METHOD
                    and value == adm_token_retrieval._AUTH_METHOD_INDIVIDUAL_TOKENS
                ):
                    switch_attempts[0] += 1
                    raise RuntimeError("Switch failed")
                await super().set(name, value)

        async def fake_generate(username: str, *, cache: _DummyTokenCache) -> str:
            # After first fail, succeed
            if switch_attempts[0] > 0:
                return "adm-success"
            raise InvalidAasTokenError("stale AAS")

        monkeypatch.setattr(adm_token_retrieval, "_generate_adm_token", fake_generate)

        cache = PartialFailCache(
            {
                DATA_AUTH_METHOD: "secrets_json",
                CONF_OAUTH_TOKEN: "oauth-token",
            }
        )

        # Should succeed via fallback despite switch failure
        token = await adm_token_retrieval.async_get_adm_token(user, cache=cache)
        assert token == "adm-success"
        assert switch_attempts[0] >= 1

    asyncio.run(_exercise())


def test_async_get_adm_token_isolated_without_cache_funcs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolated flow without cache functions should still work."""

    def fake_perform_oauth(
        username: str,
        aas_token: str,
        android_id: int,
        **kwargs: Any,
    ) -> dict[str, str]:
        return {"Token": "adm-token"}

    monkeypatch.setattr(
        adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
    )
    monkeypatch.setattr(adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF)

    token = asyncio.run(
        adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            # No cache_get or cache_set
        )
    )

    assert token == "adm-token"


def test_async_get_adm_token_isolated_without_cache_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Isolated flow with cache_set but no cache_get should handle metadata."""

    persisted: dict[str, Any] = {}

    def fake_perform_oauth(
        username: str,
        aas_token: str,
        android_id: int,
        **kwargs: Any,
    ) -> dict[str, str]:
        return {"Token": "adm-token"}

    async def cache_set(key: str, value: Any) -> None:
        persisted[key] = value

    monkeypatch.setattr(
        adm_token_retrieval.gpsoauth, "perform_oauth", fake_perform_oauth
    )
    monkeypatch.setattr(adm_token_retrieval.secrets, "randbelow", lambda *_: 0xABCDEF)

    token = asyncio.run(
        adm_token_retrieval.async_get_adm_token_isolated(
            "user@example.com",
            aas_token="aas-token",
            cache_set=cache_set,
            # No cache_get
        )
    )

    assert token == "adm-token"
    # Metadata should still be persisted
    assert "adm_token_issued_at_user@example.com" in persisted
    assert "adm_probe_startup_left_user@example.com" in persisted
