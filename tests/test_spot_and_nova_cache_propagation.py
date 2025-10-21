# tests/test_spot_and_nova_cache_propagation.py
"""Regression tests ensuring cache propagation in SPOT and Nova helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from custom_components.googlefindmy.SpotApi import spot_request as spot_module
from custom_components.googlefindmy.exceptions import MissingTokenCacheError
from custom_components.googlefindmy.NovaApi import nova_request as nova_module


class _DummyCache:
    """Minimal async cache implementation used for cache propagation tests."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        if value is None:
            self._data.pop(key, None)
            return
        self._data[key] = value


def test_pick_auth_token_prefers_spot_threads_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_pick_auth_token_async should reuse the provided cache for username/SPOT lookups."""

    cache = _DummyCache()
    recorded: dict[str, Any] = {}

    async def fake_async_get_username(*, cache) -> str:  # type: ignore[no-untyped-def]
        recorded["username_cache"] = cache
        return "user@example.com"

    async def fake_async_get_spot_token(
        username: str, *, cache, aas_provider=None
    ) -> str:  # type: ignore[no-untyped-def]
        recorded["spot_cache"] = cache
        recorded["spot_username"] = username
        return "spot-token"

    monkeypatch.setattr(spot_module, "async_get_username", fake_async_get_username)
    monkeypatch.setattr(spot_module, "async_get_spot_token", fake_async_get_spot_token)

    token, kind, username = asyncio.run(spot_module._pick_auth_token_async(cache=cache))

    assert token == "spot-token"
    assert kind == "spot"
    assert username == "user@example.com"
    assert recorded["username_cache"] is cache
    assert recorded["spot_cache"] is cache
    assert recorded["spot_username"] == "user@example.com"


def test_pick_auth_token_falls_back_to_adm_with_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback to ADM token must pass the entry cache to all helpers."""

    cache = _DummyCache()
    recorded: dict[str, Any] = {}

    async def fake_async_get_username(*, cache) -> str:  # type: ignore[no-untyped-def]
        recorded["username_cache"] = cache
        return "user@example.com"

    async def fake_async_get_spot_token(*args, **kwargs) -> str:  # type: ignore[no-untyped-def]
        recorded["spot_cache"] = kwargs["cache"]
        raise RuntimeError("no spot token")

    async def fake_async_get_adm_token_api(user: str, *, cache) -> str:  # type: ignore[no-untyped-def]
        recorded["adm_cache"] = cache
        recorded["adm_user"] = user
        return "adm-token"

    monkeypatch.setattr(spot_module, "async_get_username", fake_async_get_username)
    monkeypatch.setattr(spot_module, "async_get_spot_token", fake_async_get_spot_token)
    monkeypatch.setattr(
        spot_module, "async_get_adm_token_api", fake_async_get_adm_token_api
    )

    token, kind, username = asyncio.run(spot_module._pick_auth_token_async(cache=cache))

    assert token == "adm-token"
    assert kind == "adm"
    assert username == "user@example.com"
    assert recorded["spot_cache"] is cache
    assert recorded["adm_cache"] is cache
    assert recorded["adm_user"] == "user@example.com"


def test_async_spot_request_forwards_cache_to_picker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """async_spot_request must pass the entry cache to the token picker and HTTP layer."""

    cache = _DummyCache()
    recorded: dict[str, Any] = {}

    async def fake_pick_auth_token_async(*, prefer_adm: bool, cache):  # type: ignore[no-untyped-def]
        recorded["pick_cache"] = cache
        recorded["pick_prefer_adm"] = prefer_adm
        return ("spot-token", "spot", "user@example.com")

    class DummyResponse:
        def __init__(self) -> None:
            self.status_code = 200
            self.content = b"\x00\x00\x00\x00\x00"
            self.headers: dict[str, str] = {}

    class DummyClient:
        def __init__(self, *args, **kwargs) -> None:
            recorded["client_kwargs"] = kwargs

        async def __aenter__(self) -> DummyClient:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        async def post(
            self, url: str, *, headers=None, content=None, **_kwargs: Any
        ) -> DummyResponse:
            recorded["post_url"] = url
            recorded["post_headers"] = headers
            recorded["post_content"] = content
            return DummyResponse()

    monkeypatch.setattr(
        spot_module, "_pick_auth_token_async", fake_pick_auth_token_async
    )
    monkeypatch.setattr(spot_module.httpx, "AsyncClient", DummyClient)
    monkeypatch.setattr(
        spot_module.GrpcParser,
        "construct_grpc",
        staticmethod(lambda payload: payload),
    )
    monkeypatch.setattr(
        spot_module.GrpcParser,
        "extract_grpc_payload",
        staticmethod(lambda data: b"decoded"),
    )

    result = asyncio.run(
        spot_module.async_spot_request("Scope", b"payload", cache=cache)
    )

    assert result == b"decoded"
    assert recorded["pick_cache"] is cache
    assert recorded["pick_prefer_adm"] is False
    assert recorded["post_headers"]["Authorization"] == "Bearer spot-token"
    assert recorded["post_content"] == b"payload"
    assert recorded["client_kwargs"].get("http2") is True


def test_invalidate_token_async_requires_cache() -> None:
    """Token invalidation helper must not fall back to the global cache."""

    async def _run() -> None:
        with pytest.raises(MissingTokenCacheError):
            await spot_module._invalidate_token_async("spot", "user@example.com")

    asyncio.run(_run())


def test_clear_aas_token_async_requires_cache() -> None:
    """AAS cache clearer must require an entry-local cache."""

    async def _run() -> None:
        with pytest.raises(MissingTokenCacheError):
            await spot_module._clear_aas_token_async()

    asyncio.run(_run())


def test_get_initial_token_async_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nova initial token helper must fetch and store tokens via the provided cache."""

    cache = _DummyCache()
    recorded: dict[str, Any] = {}

    async def fake_async_get_adm_token_api(user: str, *, cache) -> str:  # type: ignore[no-untyped-def]
        recorded["adm_user"] = user
        recorded["adm_cache"] = cache
        return "adm-token"

    monkeypatch.setattr(
        nova_module, "async_get_adm_token_api", fake_async_get_adm_token_api
    )

    token = asyncio.run(
        nova_module._get_initial_token_async(
            "User@Example.com",
            logging.getLogger("test"),
            cache=cache,
        )
    )

    assert token == "adm-token"
    assert recorded["adm_user"] == "user@example.com"
    assert recorded["adm_cache"] is cache
