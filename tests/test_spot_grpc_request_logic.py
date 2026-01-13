from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock

import grpclib.client as grpclib_client
import grpclib.exceptions
import pytest
from grpclib.const import Status

from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.SpotApi import spot_request as spot_request_module


class _DummyCache(TokenCache):
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class _StubStream:
    def __init__(
        self,
        outcomes: list[Any],
        metadata_sink: list[Iterable[tuple[str, str]]] | None = None,
    ) -> None:
        self._outcomes = outcomes
        self._metadata_sink = metadata_sink

    async def __aenter__(self) -> _StubStream:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def send_message(self, message: bytes, end: bool = True) -> None:
        return None

    async def recv_message(self) -> Any:
        if not self._outcomes:
            return None
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _StubMethod:
    def __init__(
        self,
        outcomes: list[Any],
        metadata_sink: list[Iterable[tuple[str, str]]] | None = None,
    ) -> None:
        self._outcomes = outcomes
        self.calls = 0
        self.metadata_sink = metadata_sink

    def open(
        self,
        *,
        metadata: Iterable[tuple[str, str]] | None = None,
        timeout: float | None = None,
    ) -> _StubStream:
        self.calls += 1
        if self.metadata_sink is not None and metadata is not None:
            self.metadata_sink.append(tuple(metadata))
        return _StubStream(self._outcomes, self.metadata_sink)


def _install_stub_method(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[Any],
    metadata_sink: list[Iterable[tuple[str, str]]] | None = None,
) -> _StubMethod:
    stub = _StubMethod(list(outcomes), metadata_sink)
    monkeypatch.setattr(
        spot_request_module, "UnaryUnaryMethod", lambda *args, **kwargs: stub
    )
    return stub


@pytest.fixture(autouse=True)
async def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    awaitable_sleep = AsyncMock()
    monkeypatch.setattr(spot_request_module, "_async_sleep", awaitable_sleep)
    monkeypatch.setattr(spot_request_module, "_compute_delay", lambda attempt: 0.0)


@pytest.mark.asyncio
async def test_metadata_contains_authorization_and_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPOT requests should send authorization and user-agent metadata without gzip."""

    metadata_seen: list[Iterable[tuple[str, str]]] = []
    _install_stub_method(monkeypatch, [b"ok"], metadata_sink=metadata_seen)

    cache = _DummyCache()
    await cache.set("username", "user")

    monkeypatch.setattr(
        spot_request_module, "async_get_username", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_spot_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_adm_token_api", AsyncMock(return_value="adm")
    )

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())

    result = await spot_request_module.async_spot_request(
        "Scope",
        b"payload",
        cache=cache,
        transport=transport,  # type: ignore[arg-type]
    )

    assert result == b"ok"
    assert metadata_seen
    metadata_map = {k: v for k, v in metadata_seen[0]}
    assert metadata_map == {"authorization": "Bearer token"}
    assert grpclib_client.USER_AGENT == spot_request_module._USER_AGENT


@pytest.mark.asyncio
async def test_transient_status_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient statuses should retry and eventually succeed."""

    plan = [grpclib.exceptions.GRPCError(Status.UNAVAILABLE, "fail"), b"ok"]
    stub = _install_stub_method(monkeypatch, plan)

    cache = _DummyCache()
    await cache.set("username", "user")

    monkeypatch.setattr(
        spot_request_module, "async_get_username", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_spot_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_adm_token_api", AsyncMock(return_value="adm")
    )

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())

    result = await spot_request_module.async_spot_request(
        "Scope",
        b"payload",
        cache=cache,
        transport=transport,  # type: ignore[arg-type]
    )

    assert result == b"ok"
    assert stub.calls == 2


@pytest.mark.asyncio
async def test_auth_retry_once_then_permanent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authentication errors trigger one invalidation before raising permanently."""

    plan = [
        grpclib.exceptions.GRPCError(Status.UNAUTHENTICATED, "fail"),
        grpclib.exceptions.GRPCError(Status.UNAUTHENTICATED, "fail"),
    ]
    _install_stub_method(monkeypatch, plan)

    cache = _DummyCache()
    await cache.set("username", "user")

    invalidate = AsyncMock()
    monkeypatch.setattr(spot_request_module, "_invalidate_token_async", invalidate)
    monkeypatch.setattr(
        spot_request_module, "async_get_username", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_spot_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_adm_token_api", AsyncMock(return_value="adm")
    )

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())

    with pytest.raises(spot_request_module.SpotAuthPermanentError):
        await spot_request_module.async_spot_request(
            "Scope",
            b"payload",
            cache=cache,
            transport=transport,  # type: ignore[arg-type]
        )

    invalidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resource exhaustion should raise after bounded retries."""

    plan = [grpclib.exceptions.GRPCError(Status.RESOURCE_EXHAUSTED, "slow")] * (
        spot_request_module._SPOT_MAX_RETRIES + 1
    )
    _install_stub_method(monkeypatch, plan)

    cache = _DummyCache()
    await cache.set("username", "user")

    monkeypatch.setattr(
        spot_request_module, "async_get_username", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_spot_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_adm_token_api", AsyncMock(return_value="adm")
    )

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())

    with pytest.raises(spot_request_module.SpotRateLimitError):
        await spot_request_module.async_spot_request(
            "Scope",
            b"payload",
            cache=cache,
            transport=transport,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_trailers_only_after_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty replies retry before surfacing trailers-only errors."""

    plan = [b""] * (spot_request_module._SPOT_MAX_RETRIES + 1)
    _install_stub_method(monkeypatch, plan)

    cache = _DummyCache()
    await cache.set("username", "user")

    monkeypatch.setattr(
        spot_request_module, "async_get_username", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_spot_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_adm_token_api", AsyncMock(return_value="adm")
    )

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())
    transport.reset = AsyncMock()

    with pytest.raises(spot_request_module.SpotTrailersOnlyError):
        await spot_request_module.async_spot_request(
            "Scope",
            b"payload",
            cache=cache,
            transport=transport,  # type: ignore[arg-type]
        )

    assert transport.reset.await_count == 0


@pytest.mark.asyncio
async def test_protocol_error_triggers_transport_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Protocol errors should reset the shared channel and then succeed."""

    plan = [grpclib.exceptions.ProtocolError("boom"), b"reply"]
    stub = _install_stub_method(monkeypatch, plan)

    cache = _DummyCache()
    await cache.set("username", "user")

    monkeypatch.setattr(
        spot_request_module, "async_get_username", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_spot_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_adm_token_api", AsyncMock(return_value="adm")
    )

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())
    transport.reset = AsyncMock()

    result = await spot_request_module.async_spot_request(
        "Scope",
        b"payload",
        cache=cache,
        transport=transport,  # type: ignore[arg-type]
    )

    assert result == b"reply"
    assert stub.calls == 2
    transport.reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_os_error_resets_after_final_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSError scenarios reset the transport on the terminal failure."""

    plan = [OSError("boom")] * (spot_request_module._SPOT_MAX_RETRIES + 1)
    _install_stub_method(monkeypatch, plan)

    cache = _DummyCache()
    await cache.set("username", "user")

    monkeypatch.setattr(
        spot_request_module, "async_get_username", AsyncMock(return_value="user")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_spot_token", AsyncMock(return_value="token")
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_adm_token_api", AsyncMock(return_value="adm")
    )

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())
    transport.reset = AsyncMock()

    with pytest.raises(spot_request_module.SpotNetworkError):
        await spot_request_module.async_spot_request(
            "Scope",
            b"payload",
            cache=cache,
            transport=transport,  # type: ignore[arg-type]
        )

    transport.reset.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalid_aas_token_clears_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid AAS tokens are cleared once before surfacing a permanent failure."""

    attempts = {"count": 0}

    async def _username(cache: TokenCache) -> str:
        return "user"

    async def _spot_token(username: str, cache: TokenCache) -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise spot_request_module.InvalidAasTokenError()
        return "token"

    monkeypatch.setattr(spot_request_module, "async_get_username", _username)
    monkeypatch.setattr(spot_request_module, "async_get_spot_token", _spot_token)
    monkeypatch.setattr(
        spot_request_module, "async_get_adm_token_api", AsyncMock(return_value="adm")
    )

    cache = _DummyCache()
    await cache.set(spot_request_module.DATA_AAS_TOKEN, "stale")

    _install_stub_method(monkeypatch, [b"ok"])

    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())

    result = await spot_request_module.async_spot_request(
        "Scope",
        b"payload",
        cache=cache,
        transport=transport,  # type: ignore[arg-type]
    )

    assert result == b"ok"
    assert cache._data.get(spot_request_module.DATA_AAS_TOKEN) is None
