# tests/test_nova_request.py
"""Tests for Nova API async request helpers and TTL policy."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from collections.abc import Awaitable, Callable

import pytest

from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.Auth.token_retrieval import InvalidAasTokenError
from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import (
    async_request_device_list,
)
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
    assert len(session.calls) == 2
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
            if self._initial_calls >= 2:
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
    assert statuses.count(401) == 2
    assert statuses.count(200) == 2
    successful_auths = [call["auth"] for call in calls if call["status"] == 200]
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


def test_async_ttl_policy_refresh_preserves_existing_startup_probe() -> None:
    """401 refresh clears stale token keys without resetting startup probe counters."""

    async def _run() -> None:
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
