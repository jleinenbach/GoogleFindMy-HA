# tests/test_nova_request.py
"""Tests for Nova API async request helpers and TTL policy."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

MAX_INITIAL_CALLS = 2
UNAUTHORIZED_STATUS = 401
SUCCESS_STATUS = 200
EXPECTED_RETRY_COUNT = 2

from custom_components.googlefindmy.api import _EphemeralCache
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.Auth.token_retrieval import InvalidAasTokenError
from custom_components.googlefindmy.Auth.username_provider import username_string
from custom_components.googlefindmy.const import DATA_AAS_TOKEN
from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import (
    async_request_device_list,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    AsyncTTLPolicy,
    NovaAuthError,
    async_nova_request,
    register_cache_provider,
    unregister_cache_provider,
)


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


class _FakeHass:
    """Minimal Home Assistant stub for TokenCache interactions."""

    async def async_add_executor_job(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        return func(*args, **kwargs)


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
    """Ensure NovaAuthError is raised instead of NameError on 401 responses.

    After the initial 401 and token refresh, the code now retries 3 times
    with exponential backoff (6s, 12s, 24s) before raising NovaAuthError.
    This test provides 4 consecutive 401 responses to trigger the error.
    """

    cache = _StubCache()
    # Need 4 responses: 1 initial + 3 retries with backoff
    session = _DummySession(
        [
            _DummyResponse(401, b"<html><body>Unauthorized</body></html>"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
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

        # Mock asyncio.sleep to skip the 6s+12s+24s backoff delays
        async def _instant_sleep(_: float) -> None:
            pass

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _seed_initial,
        )
        monkeypatch.setattr("asyncio.sleep", _instant_sleep)

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

    original_on_401 = AsyncTTLPolicy.async_on_401

    async def _spy_on_401(self: AsyncTTLPolicy, adaptive_downshift: bool = True) -> Any:
        on_401_calls.append(adaptive_downshift)
        return await original_on_401(self, adaptive_downshift=adaptive_downshift)

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.AsyncTTLPolicy.async_on_401",
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
    assert len(session.calls) == EXPECTED_RETRY_COUNT
    second_headers = session.calls[1]["kwargs"].get("headers", {})
    assert second_headers.get("Authorization") == "Bearer adm-new"


class _CoordinatedSession:
    """Session stub orchestrating overlapping ADM refresh behavior."""

    def __init__(self, allow_refresh: asyncio.Event) -> None:
        self._allow_refresh = allow_refresh
        self._initial_calls = 0
        self.calls: list[dict[str, Any]] = []

    def post(self, *_args: object, **kwargs: Any) -> _DummyResponse:
        headers = kwargs.get("headers", {})
        auth = headers.get("Authorization")
        status: int
        body: bytes

        if auth == "Bearer initial-token":
            self._initial_calls += 1
            if self._initial_calls >= MAX_INITIAL_CALLS:
                self._allow_refresh.set()
            status, body = 401, b"unauthorized"
        elif auth == "Bearer refreshed-token":
            status, body = 200, b"ok"
        else:  # pragma: no cover - defensive guard for unexpected headers
            raise AssertionError(f"Unexpected Authorization header: {auth!r}")

        self.calls.append(
            {
                "auth": auth,
                "status": status,
                "headers": dict(headers),
            }
        )
        return _DummyResponse(status, body)


def test_async_nova_request_reuses_cached_token_after_recent_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overlapping 401 retries reuse the freshly cached ADM token."""

    cache = _StubCache()
    username = "user@example.com"
    namespace = "entry-id"
    bare_token_key = f"adm_token_{username}"
    namespaced_token_key = f"{namespace}:{bare_token_key}"

    async def _exercise() -> tuple[list[str], list[dict[str, Any]], int]:
        allow_refresh = asyncio.Event()
        session = _CoordinatedSession(allow_refresh)

        await cache.set(bare_token_key, "initial-token")
        await cache.set(namespaced_token_key, "initial-token")

        refresh_calls = 0

        async def _fake_get_adm_token(
            user: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            assert user == username
            cached = await cache.get(bare_token_key)
            if isinstance(cached, str) and cached:
                return cached
            return "initial-token"

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )

        async def _refresh_override() -> str:
            nonlocal refresh_calls
            refresh_calls += 1
            await allow_refresh.wait()
            token = "refreshed-token"
            await cache.set(bare_token_key, token)
            await cache.set(namespaced_token_key, token)
            return token

        tasks = [
            asyncio.create_task(
                async_nova_request(
                    "scope",
                    "00",
                    username=username,
                    cache=cache,
                    session=session,
                    namespace=namespace,
                    refresh_override=_refresh_override,
                )
            )
            for _ in range(2)
        ]

        results = await asyncio.gather(*tasks)
        return results, session.calls, refresh_calls

    results, calls, refreshes = asyncio.run(_exercise())

    assert results == ["6f6b", "6f6b"]
    assert refreshes == 1

    statuses = [call["status"] for call in calls]
    assert statuses.count(UNAUTHORIZED_STATUS) == EXPECTED_RETRY_COUNT
    assert statuses.count(SUCCESS_STATUS) == EXPECTED_RETRY_COUNT
    successful_auths = [
        call["auth"] for call in calls if call["status"] == SUCCESS_STATUS
    ]
    assert successful_auths == ["Bearer refreshed-token", "Bearer refreshed-token"]


def test_device_list_namespace_override_does_not_double_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Namespace-aware overrides must not prefix keys twice when 401 triggers a refresh."""

    cache = _StubCache()
    namespace = "entry-double"
    username = "user@example.com"

    get_keys: list[str] = []
    set_keys: list[str] = []

    async def _cache_get_override(key: str) -> Any:
        get_keys.append(key)
        return await cache.get(key)

    async def _cache_set_override(key: str, value: Any) -> None:
        set_keys.append(key)
        await cache.set(key, value)

    session = _DummySession(
        [
            _DummyResponse(401, b"unauthorized"),
            _DummyResponse(200, b"\xde\xad\xbe\xef"),
        ]
    )

    async def _fake_initial_token(
        user: str | None = None,
        *,
        retries: int = 2,
        backoff: float = 1.0,
        cache: Any,
    ) -> str:
        resolved = (user or username).lower()
        await cache.set(f"adm_token_{resolved}", "initial-token")
        return "initial-token"

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_initial_token,
    )

    async def _refresh_override() -> str:
        token = "refreshed-token"
        await cache.set(f"adm_token_{username}", token)
        await cache.set(f"{namespace}:adm_token_{username}", token)
        return token

    async def _exercise() -> str:
        return await async_request_device_list(
            username,
            session=session,
            cache=cache,
            cache_get=_cache_get_override,
            cache_set=_cache_set_override,
            refresh_override=_refresh_override,
            namespace=namespace,
        )

    result_hex = asyncio.run(_exercise())

    assert result_hex == "deadbeef"
    double_prefixed = [
        key
        for key in [*get_keys, *set_keys]
        if key.startswith(f"{namespace}:{namespace}:")
    ]
    assert not double_prefixed
    assert f"{namespace}:adm_token_issued_at_{username}" in set_keys


def test_async_ttl_policy_refresh_preserves_existing_startup_probe() -> None:  # noqa: PLR0915
    """401 refresh clears stale token keys without resetting startup probe counters."""

    async def _run() -> None:  # noqa: PLR0915
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-refresh")
        try:
            logger = logging.getLogger("test_async_ttl_policy_refresh")
            username = "user@example.com"
            namespace = "entry-refresh"
            bare_token_key = f"adm_token_{username}"
            namespaced_token_key = f"{namespace}:{bare_token_key}"
            issued_bare_key = f"adm_token_issued_at_{username}"
            issued_ns_key = f"{namespace}:{issued_bare_key}"
            probe_bare_key = f"adm_probe_startup_left_{username}"

            await cache.set(bare_token_key, "stale-cache-token")
            await cache.set(namespaced_token_key, "stale-ns-token")
            await cache.set(issued_bare_key, 1.0)
            await cache.set(issued_ns_key, 2.0)
            await cache.set(probe_bare_key, 1)

            minted_tokens = ["fresh-token"]
            header: dict[str, str] = {}

            async def _cache_get(key: str) -> Any:
                return await cache.get(key)

            async def _cache_set(key: str, value: Any) -> None:
                await cache.set(key, value)

            async def _refresh() -> str:
                cached = await cache.get(bare_token_key)
                if isinstance(cached, str) and cached:
                    return cached
                if not minted_tokens:
                    raise AssertionError("Expected to mint a fresh ADM token")
                token = minted_tokens.pop(0)
                await cache.set(bare_token_key, token)
                return token

            policy = AsyncTTLPolicy(
                username=username,
                logger=logger,
                get_value=_cache_get,
                set_value=_cache_set,
                refresh_fn=_refresh,
                set_auth_header_fn=lambda bearer: header.__setitem__("value", bearer),
                ns_prefix=namespace,
            )

            issued_at = time.time() - 900
            await cache.set(policy.k_issued, issued_at)
            await cache.set(issued_bare_key, issued_at - 30)
            await cache.set(policy.k_startleft, 1)
            await cache.set(probe_bare_key, 1)

            assert await cache.get(policy.k_startleft) == 1
            assert await cache.get(probe_bare_key) == 1

            result = await policy.async_on_401()

            assert result == "fresh-token"
            assert not minted_tokens
            assert header["value"] == "Bearer fresh-token"
            assert await cache.get(bare_token_key) == "fresh-token"
            assert await cache.get(namespaced_token_key) == "fresh-token"

            updated_issued_ns = await cache.get(policy.k_issued)
            updated_issued_bare = await cache.get(issued_bare_key)
            assert isinstance(updated_issued_ns, (int, float))
            assert isinstance(updated_issued_bare, (int, float))
            assert updated_issued_ns >= issued_at
            assert updated_issued_bare >= issued_at

            assert await cache.get(policy.k_startleft) == 1
            assert await cache.get(probe_bare_key) == 1
        finally:
            await cache.close()

    asyncio.run(_run())


def test_async_ttl_policy_clears_namespaced_aas_token_on_invalid_refresh() -> None:
    """Invalid AAS tokens remove both namespaced and bare cache keys."""

    async def _run() -> None:
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-invalid-aas")
        try:
            namespace = "entry-invalid-aas"

            await cache.set(DATA_AAS_TOKEN, "seed-bare")
            await cache.set(f"{namespace}:{DATA_AAS_TOKEN}", "seed-ns")

            async def _cache_get(key: str) -> Any:
                return await cache.get(key)

            async def _cache_set(key: str, value: Any) -> None:
                await cache.set(key, value)

            async def _refresh() -> str:
                raise InvalidAasTokenError("expired")

            policy = AsyncTTLPolicy(
                username="user@example.com",
                logger=logging.getLogger("test_async_ttl_invalid_aas"),
                get_value=_cache_get,
                set_value=_cache_set,
                refresh_fn=_refresh,
                set_auth_header_fn=lambda _: None,
                ns_prefix=namespace,
            )

            with pytest.raises(NovaAuthError):
                await policy._do_refresh_async(time.time())

            assert await cache.get(DATA_AAS_TOKEN) is None
            assert await cache.get(f"{namespace}:{DATA_AAS_TOKEN}") is None
        finally:
            await cache.close()

    asyncio.run(_run())


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


def test_async_nova_request_uses_registered_cache_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider fallback supplies cache when async_nova_request cache arg is omitted."""

    cache = _StubCache()
    session = _DummySession([_DummyResponse(200, b"\x01\x02")])

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
        return "provider-token"

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )

    async def _exercise() -> str:
        await cache.set(username_string, "user@example.com")
        register_cache_provider(lambda: cache)
        try:
            return await async_nova_request(
                "testScope",
                "00",
                session=session,
            )
        finally:
            unregister_cache_provider()

    result = asyncio.run(_exercise())

    assert result == "0102"
    assert calls and calls[0]["cache"] is cache
    assert session.calls
    headers = session.calls[0]["kwargs"].get("headers", {})
    assert headers.get("Authorization") == "Bearer provider-token"


def test_async_nova_request_requires_cache_when_provider_missing() -> None:
    """Missing cache and provider should raise before issuing the request."""

    async def _exercise() -> None:
        with pytest.raises(ValueError):
            await async_nova_request("testScope", "00", username="user@example.com")

    asyncio.run(_exercise())


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
    assert calls[0]["retries"] == EXPECTED_RETRY_COUNT
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


def test_async_ttl_policy_invalidate_aas_token_clears_both_bare_and_namespaced() -> (
    None
):
    """async_invalidate_aas_token must clear AAS token from both bare and namespaced keys.

    This ensures that when persistent 401 errors indicate a potentially invalid
    AAS token (even without explicit BadAuthentication), subsequent poll cycles
    can generate a fresh OAuth -> AAS -> ADM token chain.
    """

    async def _run() -> None:
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-invalidate-aas")
        try:
            namespace = "entry-invalidate-aas"
            username = "user@example.com"

            # Seed both bare and namespaced AAS tokens
            await cache.set(DATA_AAS_TOKEN, "stale-bare-aas")
            await cache.set(f"{namespace}:{DATA_AAS_TOKEN}", "stale-ns-aas")

            async def _cache_get(key: str) -> Any:
                return await cache.get(key)

            async def _cache_set(key: str, value: Any) -> None:
                await cache.set(key, value)

            async def _refresh() -> str:
                return "fresh-token"

            policy = AsyncTTLPolicy(
                username=username,
                logger=logging.getLogger("test_invalidate_aas"),
                get_value=_cache_get,
                set_value=_cache_set,
                refresh_fn=_refresh,
                set_auth_header_fn=lambda _: None,
                ns_prefix=namespace,
            )

            # Verify tokens exist before invalidation
            assert await cache.get(DATA_AAS_TOKEN) == "stale-bare-aas"
            assert await cache.get(f"{namespace}:{DATA_AAS_TOKEN}") == "stale-ns-aas"

            # Call the new invalidation method
            await policy.async_invalidate_aas_token()

            # Both keys must be cleared
            assert await cache.get(DATA_AAS_TOKEN) is None
            assert await cache.get(f"{namespace}:{DATA_AAS_TOKEN}") is None
        finally:
            await cache.close()

    asyncio.run(_run())


def test_async_nova_request_invalidates_aas_token_after_exhausted_401_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent 401 errors after token refresh must invalidate the AAS token.

    With the new retry sequence:
    - Step 1: First 401 → refresh ADM → 6s → retry
    - Step 2: Second 401 → 61s → invalidate AAS, refresh AAS+ADM → 6s → retry
    - Step 3: Third 401 → 501s → retry
    - Step 4: Fourth 401 → permanent error

    The AAS token is invalidated in step 2, enabling fresh token generation
    in subsequent poll cycles.
    """

    cache = _StubCache()
    # Need 4 responses: 4 consecutive 401s to exhaust all retry steps
    session = _DummySession(
        [
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
        ]
    )

    aas_invalidation_calls: list[str] = []

    async def _exercise() -> None:
        # Seed the AAS token that should be invalidated
        await cache.set(DATA_AAS_TOKEN, "stale-aas-token")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return "initial-adm"

        async def _refresh() -> str:
            return "refreshed-adm"

        # Mock asyncio.sleep to skip delays
        async def _instant_sleep(_: float) -> None:
            pass

        # Spy on async_invalidate_aas_token to verify it's called
        original_invalidate = AsyncTTLPolicy.async_invalidate_aas_token

        async def _spy_invalidate(self: AsyncTTLPolicy) -> None:
            aas_invalidation_calls.append(self.username)
            await original_invalidate(self)

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr("asyncio.sleep", _instant_sleep)
        monkeypatch.setattr(
            AsyncTTLPolicy,
            "async_invalidate_aas_token",
            _spy_invalidate,
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

    # After all retries exhausted, error is permanent (requires re-auth)
    assert err.value.status == 401
    assert err.value.is_permanent is True

    # AAS invalidation happens in step 2 (after second 401)
    assert len(aas_invalidation_calls) == 1
    assert aas_invalidation_calls[0] == "user@example.com"


def test_async_nova_request_aas_token_cleared_enables_fresh_chain_on_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After AAS invalidation, next poll cycle should generate fresh token chain.

    This test verifies the complete bug fix scenario:
    1. First poll cycle: persistent 401s -> AAS token invalidated
    2. Second poll cycle: AAS token is None -> success (simulating fresh generation)
    """

    cache = _StubCache()
    poll_cycle = 0
    aas_states_before_request: list[tuple[int, str | None]] = []

    async def _exercise() -> tuple[str | None, str | None]:
        nonlocal poll_cycle

        # Seed initial stale AAS token
        await cache.set(DATA_AAS_TOKEN, "stale-aas")
        await cache.set(username_string, "user@example.com")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return f"adm-cycle-{poll_cycle}"

        async def _refresh() -> str:
            return f"refreshed-adm-cycle-{poll_cycle}"

        async def _instant_sleep(_: float) -> None:
            pass

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr("asyncio.sleep", _instant_sleep)

        # First poll cycle: all 401s -> should invalidate AAS token
        poll_cycle = 1
        aas_states_before_request.append((poll_cycle, await cache.get(DATA_AAS_TOKEN)))
        session1 = _DummySession(
            [
                _DummyResponse(401, b"Unauthorized"),
                _DummyResponse(401, b"Unauthorized"),
                _DummyResponse(401, b"Unauthorized"),
                _DummyResponse(401, b"Unauthorized"),
            ]
        )

        try:
            await async_nova_request(
                "testScope",
                "00",
                username="user@example.com",
                cache=cache,
                session=session1,
                refresh_override=_refresh,
            )
        except NovaAuthError:
            pass  # Expected

        # Verify AAS token was cleared after first cycle
        aas_after_first = await cache.get(DATA_AAS_TOKEN)

        # Second poll cycle: success with fresh AAS
        poll_cycle = 2
        session2 = _DummySession([_DummyResponse(200, b"\xca\xfe")])

        second_result = await async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=cache,
            session=session2,
            refresh_override=_refresh,
        )

        return aas_after_first, second_result

    aas_after_first, second_result = asyncio.run(_exercise())

    # AAS should be None after first failed cycle
    assert aas_after_first is None

    # Second cycle should succeed with fresh token chain
    assert second_result == "cafe"

    # Verify AAS state progression:
    # Cycle 1: AAS = "stale-aas" (before request)
    # After cycle 1: AAS = None (invalidated due to persistent 401s)
    assert aas_states_before_request[0] == (1, "stale-aas")


def test_async_nova_request_preserves_aas_token_on_successful_401_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AAS token must be preserved when 401 recovery succeeds within retry window.

    If the first 401 triggers a token refresh and the retry succeeds, the AAS
    token should NOT be invalidated. This prevents unnecessary token regeneration
    when the issue was transient (e.g., backend propagation delay).
    """

    cache = _StubCache()
    # First request: 401, second request after refresh: success
    session = _DummySession(
        [
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(200, b"\xbe\xef"),
        ]
    )

    aas_invalidation_calls: list[str] = []

    async def _exercise() -> tuple[str, str | None]:
        await cache.set(DATA_AAS_TOKEN, "original-aas")
        await cache.set(username_string, "user@example.com")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return "initial-adm"

        async def _refresh() -> str:
            return "refreshed-adm"

        # Spy on async_invalidate_aas_token to verify it's NOT called
        original_invalidate = AsyncTTLPolicy.async_invalidate_aas_token

        async def _spy_invalidate(self: AsyncTTLPolicy) -> None:
            aas_invalidation_calls.append(self.username)
            await original_invalidate(self)

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr(
            AsyncTTLPolicy,
            "async_invalidate_aas_token",
            _spy_invalidate,
        )

        # Don't pass token kwarg to avoid overwriting AAS in cache
        result = await async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=cache,
            session=session,
            refresh_override=_refresh,
        )

        final_aas = await cache.get(DATA_AAS_TOKEN)
        return result, final_aas

    result, final_aas = asyncio.run(_exercise())

    # Request should succeed
    assert result == "beef"

    # AAS token should be preserved (not invalidated)
    assert final_aas == "original-aas"

    # async_invalidate_aas_token should NOT have been called
    assert len(aas_invalidation_calls) == 0


def test_async_nova_request_no_double_aas_invalidation_on_repeated_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AAS invalidation must happen exactly once per failed request cycle.

    When 401s persist through all retries, async_invalidate_aas_token should
    be called exactly once, not multiple times per retry.
    """

    cache = _StubCache()
    session = _DummySession(
        [
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
        ]
    )

    invalidation_call_count = 0

    async def _exercise() -> None:
        nonlocal invalidation_call_count

        await cache.set(DATA_AAS_TOKEN, "stale-aas")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return "initial-adm"

        async def _refresh() -> str:
            return "refreshed-adm"

        async def _instant_sleep(_: float) -> None:
            pass

        original_invalidate = AsyncTTLPolicy.async_invalidate_aas_token

        async def _counting_invalidate(self: AsyncTTLPolicy) -> None:
            nonlocal invalidation_call_count
            invalidation_call_count += 1
            await original_invalidate(self)

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr("asyncio.sleep", _instant_sleep)
        monkeypatch.setattr(
            AsyncTTLPolicy,
            "async_invalidate_aas_token",
            _counting_invalidate,
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

    with pytest.raises(NovaAuthError):
        asyncio.run(_exercise())

    # Invalidation should happen exactly once per failed cycle
    assert invalidation_call_count == 1


def test_async_nova_request_loop_prevention_across_multiple_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token renewal loop prevention must work across multiple consecutive failed cycles.

    This test simulates the exact bug scenario: multiple poll cycles where each
    cycle exhausts retries. The fix ensures that each cycle invalidates AAS,
    allowing fresh token generation on subsequent cycles, eventually breaking
    the loop when valid tokens are available.
    """

    cache = _StubCache()
    cycle_count = 0
    aas_invalidations: list[int] = []
    aas_states_at_cycle_start: list[tuple[int, str | None]] = []

    async def _exercise() -> str:
        nonlocal cycle_count

        await cache.set(DATA_AAS_TOKEN, "initial-stale-aas")
        await cache.set(username_string, "user@example.com")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return f"adm-cycle-{cycle_count}"

        async def _refresh() -> str:
            return f"refreshed-adm-cycle-{cycle_count}"

        async def _instant_sleep(_: float) -> None:
            pass

        original_invalidate = AsyncTTLPolicy.async_invalidate_aas_token

        async def _tracking_invalidate(self: AsyncTTLPolicy) -> None:
            aas_invalidations.append(cycle_count)
            await original_invalidate(self)

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr("asyncio.sleep", _instant_sleep)
        monkeypatch.setattr(
            AsyncTTLPolicy,
            "async_invalidate_aas_token",
            _tracking_invalidate,
        )

        # Simulate 3 failed cycles, then 1 successful cycle
        for i in range(1, 4):
            cycle_count = i
            # Record AAS state at the start of each cycle
            current_aas = await cache.get(DATA_AAS_TOKEN)
            aas_states_at_cycle_start.append((i, current_aas))

            session = _DummySession(
                [
                    _DummyResponse(401, b"Unauthorized"),
                    _DummyResponse(401, b"Unauthorized"),
                    _DummyResponse(401, b"Unauthorized"),
                    _DummyResponse(401, b"Unauthorized"),
                ]
            )
            try:
                await async_nova_request(
                    "testScope",
                    "00",
                    username="user@example.com",
                    cache=cache,
                    session=session,
                    refresh_override=_refresh,
                )
            except NovaAuthError:
                pass  # Expected for cycles 1-3

        # Record final AAS state before successful cycle
        cycle_count = 4
        final_aas_before_success = await cache.get(DATA_AAS_TOKEN)
        aas_states_at_cycle_start.append((4, final_aas_before_success))

        session_success = _DummySession([_DummyResponse(200, b"\xde\xad")])

        return await async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=cache,
            session=session_success,
            refresh_override=_refresh,
        )

    result = asyncio.run(_exercise())

    # Final cycle should succeed
    assert result == "dead"

    # AAS should be invalidated once per failed cycle (cycles 1, 2, 3)
    assert aas_invalidations == [1, 2, 3]

    # Verify the AAS token state progression:
    # Cycle 1: AAS = "initial-stale-aas" (before invalidation)
    # Cycle 2: AAS = None (after cycle 1 invalidated it)
    # Cycle 3: AAS = None (after cycle 2 invalidated it)
    # Cycle 4: AAS = None (after cycle 3 invalidated it)
    assert aas_states_at_cycle_start[0] == (1, "initial-stale-aas")
    assert aas_states_at_cycle_start[1][1] is None  # Cycle 2 sees None
    assert aas_states_at_cycle_start[2][1] is None  # Cycle 3 sees None
    assert aas_states_at_cycle_start[3][1] is None  # Cycle 4 sees None


def test_async_nova_request_adm_only_failure_recovers_on_second_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADM refresh alone should fix most 401 errors without invalidating AAS.

    This test verifies the new retry sequence:
    - Step 1: First 401 → refresh ADM only → 6s propagation → retry
    - Success on second request proves ADM-only refresh was sufficient

    The AAS token should NOT be invalidated in this scenario.
    """

    cache = _StubCache()
    # First request: 401, second request after ADM refresh: success
    session = _DummySession(
        [
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(200, b"\xca\xfe\xba\xbe"),
        ]
    )

    aas_invalidation_calls: list[str] = []
    adm_refresh_calls: list[str] = []
    sleep_delays: list[float] = []

    async def _exercise() -> tuple[str, str | None]:
        await cache.set(DATA_AAS_TOKEN, "valid-aas")
        await cache.set(username_string, "user@example.com")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return "initial-adm"

        async def _refresh() -> str:
            adm_refresh_calls.append("refresh")
            return "refreshed-adm"

        async def _tracking_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        original_invalidate = AsyncTTLPolicy.async_invalidate_aas_token

        async def _spy_invalidate(self: AsyncTTLPolicy) -> None:
            aas_invalidation_calls.append(self.username)
            await original_invalidate(self)

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr("asyncio.sleep", _tracking_sleep)
        monkeypatch.setattr(
            AsyncTTLPolicy,
            "async_invalidate_aas_token",
            _spy_invalidate,
        )

        result = await async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=cache,
            session=session,
            refresh_override=_refresh,
        )

        final_aas = await cache.get(DATA_AAS_TOKEN)
        return result, final_aas

    result, final_aas = asyncio.run(_exercise())

    # Request should succeed
    assert result == "cafebabe"

    # ADM refresh should have been called once
    assert len(adm_refresh_calls) == 1

    # AAS token should NOT be invalidated (ADM refresh was sufficient)
    assert len(aas_invalidation_calls) == 0
    assert final_aas == "valid-aas"

    # Should have waited 6s for propagation delay
    assert 6.0 in sleep_delays


def test_async_nova_request_aas_adm_failure_requires_full_chain_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ADM-only refresh fails, the entire AAS+ADM chain must be refreshed.

    This test verifies the new retry sequence:
    - Step 1: First 401 → refresh ADM only → 6s → retry
    - Step 2: Second 401 → 61s cooldown → invalidate AAS → refresh AAS+ADM → 6s → retry
    - Success on third request proves full chain refresh was needed

    The AAS token should be invalidated after the second 401.
    """

    cache = _StubCache()
    # Two 401s (ADM-only fails), then success after full chain refresh
    session = _DummySession(
        [
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(200, b"\xde\xad\xbe\xef"),
        ]
    )

    aas_invalidation_calls: list[str] = []
    adm_refresh_calls: list[str] = []
    sleep_delays: list[float] = []

    async def _exercise() -> tuple[str, str | None]:
        await cache.set(DATA_AAS_TOKEN, "stale-aas")
        await cache.set(username_string, "user@example.com")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return "initial-adm"

        async def _refresh() -> str:
            adm_refresh_calls.append("refresh")
            # After AAS invalidation, this simulates generating fresh tokens
            return "refreshed-adm"

        async def _tracking_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        original_invalidate = AsyncTTLPolicy.async_invalidate_aas_token

        async def _spy_invalidate(self: AsyncTTLPolicy) -> None:
            aas_invalidation_calls.append(self.username)
            await original_invalidate(self)

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr("asyncio.sleep", _tracking_sleep)
        monkeypatch.setattr(
            AsyncTTLPolicy,
            "async_invalidate_aas_token",
            _spy_invalidate,
        )

        result = await async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=cache,
            session=session,
            refresh_override=_refresh,
        )

        final_aas = await cache.get(DATA_AAS_TOKEN)
        return result, final_aas

    result, final_aas = asyncio.run(_exercise())

    # Request should succeed
    assert result == "deadbeef"

    # ADM refresh should have been called twice (once for step 1, once for step 2)
    assert len(adm_refresh_calls) == 2

    # AAS token should be invalidated exactly once (in step 2)
    assert len(aas_invalidation_calls) == 1
    assert aas_invalidation_calls[0] == "user@example.com"

    # AAS should be None after invalidation
    assert final_aas is None

    # Verify the delay sequence: 6s (step 1) + 61s (step 2 cooldown) + 6s (step 2 propagation)
    assert sleep_delays == [6.0, 61.0, 6.0]


def test_async_nova_request_full_retry_sequence_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When all 4 retry steps fail, a permanent auth error must be raised.

    This test verifies the complete retry sequence:
    - Step 1: First 401 → refresh ADM → 6s → retry
    - Step 2: Second 401 → 61s → invalidate AAS, refresh AAS+ADM → 6s → retry
    - Step 3: Third 401 → 501s long cooldown → retry
    - Step 4: Fourth 401 → permanent error (re-auth required)

    Total wait: 6s + 61s + 6s + 501s = 574s (~9.5 min)
    """

    cache = _StubCache()
    # Four consecutive 401s to exhaust all retries
    session = _DummySession(
        [
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
        ]
    )

    aas_invalidation_calls: list[str] = []
    adm_refresh_calls: list[str] = []
    sleep_delays: list[float] = []

    async def _exercise() -> None:
        await cache.set(DATA_AAS_TOKEN, "stale-aas")
        await cache.set(username_string, "user@example.com")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return "initial-adm"

        async def _refresh() -> str:
            adm_refresh_calls.append("refresh")
            return "refreshed-adm"

        async def _tracking_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        original_invalidate = AsyncTTLPolicy.async_invalidate_aas_token

        async def _spy_invalidate(self: AsyncTTLPolicy) -> None:
            aas_invalidation_calls.append(self.username)
            await original_invalidate(self)

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr("asyncio.sleep", _tracking_sleep)
        monkeypatch.setattr(
            AsyncTTLPolicy,
            "async_invalidate_aas_token",
            _spy_invalidate,
        )

        await async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=cache,
            session=session,
            refresh_override=_refresh,
        )

    with pytest.raises(NovaAuthError) as err:
        asyncio.run(_exercise())

    # Should be a permanent error requiring re-authentication
    assert err.value.status == 401
    assert err.value.is_permanent is True
    assert "re-authentication required" in str(err.value).lower()

    # ADM refresh should have been called twice (step 1 and step 2)
    assert len(adm_refresh_calls) == 2

    # AAS invalidation should have been called once (step 2)
    assert len(aas_invalidation_calls) == 1

    # Verify the complete delay sequence: 6s + 61s + 6s + 501s
    assert sleep_delays == [6.0, 61.0, 6.0, 501.0]


def test_async_nova_request_recovers_on_long_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery on step 3 (after 501s long cooldown) should succeed.

    This test verifies that even if ADM-only and AAS+ADM refresh both fail,
    success after the long cooldown period should work.

    Sequence: 401 → ADM → 401 → AAS+ADM → 401 → 501s → success
    """

    cache = _StubCache()
    # Three 401s, then success after long cooldown
    session = _DummySession(
        [
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(401, b"Unauthorized"),
            _DummyResponse(200, b"\xfe\xed\xfa\xce"),
        ]
    )

    sleep_delays: list[float] = []

    async def _exercise() -> str:
        await cache.set(DATA_AAS_TOKEN, "stale-aas")
        await cache.set(username_string, "user@example.com")

        async def _fake_get_adm_token(
            username: str | None = None,
            *,
            retries: int = 2,
            backoff: float = 1.0,
            cache: Any,
        ) -> str:
            return "initial-adm"

        async def _refresh() -> str:
            return "refreshed-adm"

        async def _tracking_sleep(delay: float) -> None:
            sleep_delays.append(delay)

        monkeypatch.setattr(
            "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
            _fake_get_adm_token,
        )
        monkeypatch.setattr("asyncio.sleep", _tracking_sleep)

        return await async_nova_request(
            "testScope",
            "00",
            username="user@example.com",
            cache=cache,
            session=session,
            refresh_override=_refresh,
        )

    result = asyncio.run(_exercise())

    # Should succeed after long cooldown
    assert result == "feedface"

    # Verify delays: 6s + 61s + 6s + 501s
    assert sleep_delays == [6.0, 61.0, 6.0, 501.0]


def test_async_ttl_policy_aas_ttl_learning_records_observed_lifetime() -> None:
    """AAS TTL learning should record the observed token lifetime when invalidated.

    This test verifies that when an AAS token is invalidated:
    1. The observed lifetime is calculated from the issued timestamp
    2. The best TTL is updated with a 5% safety margin
    """

    async def _run() -> None:
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-aas-ttl")
        try:
            namespace = "entry-aas-ttl"
            username = "user@example.com"

            async def _cache_get(key: str) -> Any:
                return await cache.get(key)

            async def _cache_set(key: str, value: Any) -> None:
                await cache.set(key, value)

            async def _refresh() -> str:
                return "fresh-token"

            policy = AsyncTTLPolicy(
                username=username,
                logger=logging.getLogger("test_aas_ttl_learning"),
                get_value=_cache_get,
                set_value=_cache_set,
                refresh_fn=_refresh,
                set_auth_header_fn=lambda _: None,
                ns_prefix=namespace,
            )

            # Simulate AAS token issued 24 hours ago
            issued_time = time.time() - (24 * 3600)  # 24 hours ago
            await cache.set(policy.k_aas_issued, issued_time)
            await cache.set(DATA_AAS_TOKEN, "old-aas")

            # Invalidate the AAS token
            await policy.async_invalidate_aas_token()

            # Verify AAS token is cleared
            assert await cache.get(DATA_AAS_TOKEN) is None

            # Verify best TTL was learned (24 hours * 0.95 = ~82080 seconds)
            best_ttl = await cache.get(policy.k_aas_bestttl)
            assert best_ttl is not None
            expected_ttl = 24 * 3600 * 0.95
            assert abs(float(best_ttl) - expected_ttl) < 60  # Allow 1 min tolerance

        finally:
            await cache.close()

    asyncio.run(_run())


def test_async_ttl_policy_aas_proactive_refresh_triggers_near_expiry() -> None:
    """AAS proactive refresh should trigger when approaching learned TTL.

    When the AAS token age approaches the learned TTL minus margin,
    async_check_aas_proactive_refresh should return True and invalidate the token.
    """

    async def _run() -> None:
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-aas-proactive")
        try:
            namespace = "entry-aas-proactive"
            username = "user@example.com"

            async def _cache_get(key: str) -> Any:
                return await cache.get(key)

            async def _cache_set(key: str, value: Any) -> None:
                await cache.set(key, value)

            async def _refresh() -> str:
                return "fresh-token"

            policy = AsyncTTLPolicy(
                username=username,
                logger=logging.getLogger("test_aas_proactive"),
                get_value=_cache_get,
                set_value=_cache_set,
                refresh_fn=_refresh,
                set_auth_header_fn=lambda _: None,
                ns_prefix=namespace,
            )

            # Set best TTL to 1 hour (3600 seconds)
            await cache.set(policy.k_aas_bestttl, 3600.0)

            # AAS token issued 57 minutes ago (past the threshold + max jitter)
            # Threshold = 3600 - 300 = 3300s, jitter = ±90s, so max threshold = 3390s
            # 57 min = 3420s > 3390s, so it will always trigger
            issued_time = time.time() - (57 * 60)  # 57 minutes ago
            await cache.set(policy.k_aas_issued, issued_time)
            await cache.set(DATA_AAS_TOKEN, "old-aas")

            # Check proactive refresh - should trigger
            result = await policy.async_check_aas_proactive_refresh()

            # Should have triggered proactive refresh
            assert result is True

            # AAS token should be invalidated
            assert await cache.get(DATA_AAS_TOKEN) is None

        finally:
            await cache.close()

    asyncio.run(_run())


def test_async_ttl_policy_aas_proactive_refresh_skips_when_fresh() -> None:
    """AAS proactive refresh should NOT trigger when token is still fresh.

    When the AAS token is well within its TTL, proactive refresh should skip.
    """

    async def _run() -> None:
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-aas-fresh")
        try:
            namespace = "entry-aas-fresh"
            username = "user@example.com"

            async def _cache_get(key: str) -> Any:
                return await cache.get(key)

            async def _cache_set(key: str, value: Any) -> None:
                await cache.set(key, value)

            async def _refresh() -> str:
                return "fresh-token"

            policy = AsyncTTLPolicy(
                username=username,
                logger=logging.getLogger("test_aas_fresh"),
                get_value=_cache_get,
                set_value=_cache_set,
                refresh_fn=_refresh,
                set_auth_header_fn=lambda _: None,
                ns_prefix=namespace,
            )

            # Set best TTL to 24 hours
            await cache.set(policy.k_aas_bestttl, 24 * 3600.0)

            # AAS token issued 1 hour ago (well within 24 hour TTL)
            issued_time = time.time() - 3600  # 1 hour ago
            await cache.set(policy.k_aas_issued, issued_time)
            await cache.set(DATA_AAS_TOKEN, "fresh-aas")

            # Check proactive refresh - should NOT trigger
            result = await policy.async_check_aas_proactive_refresh()

            # Should NOT have triggered proactive refresh
            assert result is False

            # AAS token should still exist
            assert await cache.get(DATA_AAS_TOKEN) == "fresh-aas"

        finally:
            await cache.close()

    asyncio.run(_run())


def test_async_ttl_policy_ignores_very_short_ttl_in_recalibration() -> None:
    """Very short observed TTLs should be ignored to avoid race condition artifacts.

    When a token expires very quickly (< MIN_TTL_FOR_LEARNING_SEC), it typically
    indicates a transient server issue (rate-limit, propagation race) rather than
    the actual token lifetime. The TTL policy should ignore these observations
    to prevent permanent corruption of the learned TTL.
    """

    async def _run() -> None:
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-short-ttl")
        try:
            namespace = "entry-short-ttl"
            username = "user@example.com"

            async def _cache_get(key: str) -> Any:
                return await cache.get(key)

            async def _cache_set(key: str, value: Any) -> None:
                await cache.set(key, value)

            async def _refresh() -> str:
                return "fresh-token"

            policy = AsyncTTLPolicy(
                username=username,
                logger=logging.getLogger("test_short_ttl"),
                get_value=_cache_get,
                set_value=_cache_set,
                refresh_fn=_refresh,
                set_auth_header_fn=lambda _: None,
                ns_prefix=namespace,
            )

            # Set a known best TTL of 2 hours (7200 seconds)
            original_ttl = 7200.0
            await cache.set(policy.k_bestttl, original_ttl)

            # Simulate a token that was issued only 30 seconds ago (well below MIN_TTL)
            # MIN_TTL_FOR_LEARNING_SEC is 300 seconds (5 minutes)
            issued_time = time.time() - 30  # 30 seconds ago
            await cache.set(policy.k_issued, issued_time)

            # Trigger async_on_401 with adaptive downshift enabled
            # The observed TTL (30s) is far below MIN_TTL_FOR_LEARNING_SEC (300s)
            # so it should be ignored
            await policy.async_on_401(adaptive_downshift=True)

            # The best TTL should remain unchanged (not corrupted by the short TTL)
            best_ttl_after = await cache.get(policy.k_bestttl)
            assert best_ttl_after == original_ttl, (
                f"Expected TTL to remain {original_ttl}, but got {best_ttl_after}"
            )

        finally:
            await cache.close()

    asyncio.run(_run())


def test_async_ttl_policy_accepts_legitimate_short_ttl_above_threshold() -> None:
    """TTLs above the minimum threshold should still trigger recalibration.

    When a token expires after MIN_TTL_FOR_LEARNING_SEC but significantly shorter
    than the current best TTL, recalibration should still occur.
    """

    async def _run() -> None:
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-legit-short")
        try:
            namespace = "entry-legit-short"
            username = "user@example.com"

            async def _cache_get(key: str) -> Any:
                return await cache.get(key)

            async def _cache_set(key: str, value: Any) -> None:
                await cache.set(key, value)

            async def _refresh() -> str:
                return "fresh-token"

            policy = AsyncTTLPolicy(
                username=username,
                logger=logging.getLogger("test_legit_short"),
                get_value=_cache_get,
                set_value=_cache_set,
                refresh_fn=_refresh,
                set_auth_header_fn=lambda _: None,
                ns_prefix=namespace,
            )

            # Set a known best TTL of 2 hours (7200 seconds)
            original_ttl = 7200.0
            await cache.set(policy.k_bestttl, original_ttl)

            # Simulate a token that was issued 10 minutes ago (600s > MIN_TTL of 300s)
            # This is above the threshold but significantly shorter than 2h
            # 600 + 120 (TTL_MARGIN_SEC) = 720 < 0.9 * 7200 = 6480, so recalibration triggers
            observed_ttl = 600.0
            issued_time = time.time() - observed_ttl
            await cache.set(policy.k_issued, issued_time)

            # Trigger async_on_401 with adaptive downshift enabled
            await policy.async_on_401(adaptive_downshift=True)

            # The best TTL should be updated to approximately observed * 0.95
            # Use approximate comparison due to time elapsed between setting issued_time and now
            expected_new_ttl = observed_ttl * 0.95  # ~570s
            best_ttl_after = await cache.get(policy.k_bestttl)
            assert best_ttl_after is not None
            assert abs(best_ttl_after - expected_new_ttl) < 1.0, (
                f"Expected TTL to be recalibrated to ~{expected_new_ttl}, "
                f"but got {best_ttl_after}"
            )

        finally:
            await cache.close()

    asyncio.run(_run())
