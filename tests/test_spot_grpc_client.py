from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import grpclib.client as grpclib_client
import grpclib.exceptions
import pytest
from grpclib.const import Status
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.SpotApi import spot_request as spot_request_module
from custom_components.googlefindmy.SpotApi.spot_grpc_transport import (
    RawCodec,
    SpotGrpcTransport,
)
from custom_components.googlefindmy.SpotApi.spot_request import (
    SpotAuthPermanentError,
    SpotGrpcStatusError,
    SpotRateLimitError,
    SpotTrailersOnlyError,
)
from tests.helpers import drain_loop


class _DummyCache:
    """Minimal async cache stub used for SPOT client tests."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.set_calls: list[tuple[str, Any | None]] = []

    async def get(self, name: str) -> Any:
        return self.values.get(name)

    async def set(self, name: str, value: Any | None) -> None:
        self.values[name] = value
        self.set_calls.append((name, value))


class _SentinelChannel:
    """Lightweight channel stub that records close calls."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _StubStream:
    """Async context manager that returns a planned response or raises an error."""

    def __init__(self, behavior: Exception | bytes | None) -> None:
        self._behavior = behavior

    async def __aenter__(self) -> _StubStream:
        return self

    async def __aexit__(self, exc_type, exc: Exception | None, tb) -> bool:
        return False

    async def send_message(self, message: bytes, end: bool = True) -> None:
        return None

    async def recv_message(self) -> bytes | None:
        if isinstance(self._behavior, Exception):
            raise self._behavior
        return self._behavior


def _install_stub_method(
    monkeypatch: pytest.MonkeyPatch, plan: list[Exception | bytes | None]
):
    """Patch UnaryUnaryMethod to replay planned responses/errors."""

    class _StubMethod:
        call_count = 0
        last_metadata: tuple[tuple[str, str], ...] | None = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._plan = plan

        def open(self, metadata=None, timeout: float | None = None):  # noqa: D401
            idx = min(_StubMethod.call_count, len(self._plan) - 1)
            behavior = self._plan[idx]
            _StubMethod.call_count += 1
            _StubMethod.last_metadata = (
                tuple(metadata) if metadata is not None else None
            )
            return _StubStream(behavior)

    _StubMethod.call_count = 0
    _StubMethod.last_metadata = None
    monkeypatch.setattr(spot_request_module, "UnaryUnaryMethod", _StubMethod)
    return _StubMethod


def test_raw_codec_uses_base_grpc_content_type() -> None:
    """RawCodec should emit the base gRPC media type without a subtype suffix."""

    codec = RawCodec()

    assert codec.content_type == "application/grpc"


@pytest.mark.asyncio
async def test_spot_request_metadata_excludes_gzip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPOT requests should avoid advertising gzip when decompression isn't enabled."""

    plan = [b"reply"]
    stub_method = _install_stub_method(monkeypatch, plan)
    cache = _DummyCache()

    result = await spot_request_module.async_spot_request(
        "Scope", b"payload", cache=cache
    )

    assert result == b"reply"
    assert stub_method.last_metadata is not None
    metadata = dict(stub_method.last_metadata)
    assert "grpc-accept-encoding" not in metadata
    assert metadata.get("authorization") == "Bearer spot-token"
    assert set(metadata) == {"authorization"}
    assert grpclib_client.USER_AGENT == spot_request_module._USER_AGENT, (
        "SPOT requires a Google Mobile Services User-Agent; removing it causes Google "
        "to reject or reroute requests."
    )


@pytest.fixture(autouse=True)
def _patch_token_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub token helpers to keep SPOT client tests isolated."""

    monkeypatch.setattr(spot_request_module, "_async_sleep", AsyncMock())
    monkeypatch.setattr(spot_request_module, "_compute_delay", lambda *_: 0.0)
    monkeypatch.setattr(
        spot_request_module,
        "async_get_username",
        AsyncMock(return_value="user@example.com"),
    )
    monkeypatch.setattr(
        spot_request_module,
        "async_get_spot_token",
        AsyncMock(return_value="spot-token"),
    )
    monkeypatch.setattr(
        spot_request_module,
        "async_get_adm_token_api",
        AsyncMock(return_value="adm-token"),
    )
    monkeypatch.setattr(
        spot_request_module.SPOT_GRPC_TRANSPORT,
        "get_channel",
        AsyncMock(return_value=object()),
    )
    monkeypatch.setattr(spot_request_module.SPOT_GRPC_TRANSPORT, "reset", AsyncMock())


@pytest.mark.asyncio
async def test_spot_request_retries_transient_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNAVAILABLE errors should be retried until success."""

    plan = [
        grpclib.exceptions.GRPCError(Status.UNAVAILABLE, "unavailable"),
        b"reply",
    ]
    stub_method = _install_stub_method(monkeypatch, plan)
    cache = _DummyCache()

    result = await spot_request_module.async_spot_request(
        "Scope", b"payload", cache=cache
    )

    assert result == b"reply"
    assert stub_method.call_count == 2


@pytest.mark.asyncio
async def test_spot_request_auth_failure_retries_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth errors should invalidate the token once and then fail permanently."""

    plan = [
        grpclib.exceptions.GRPCError(Status.UNAUTHENTICATED, "expired"),
        grpclib.exceptions.GRPCError(Status.UNAUTHENTICATED, "still expired"),
    ]
    stub_method = _install_stub_method(monkeypatch, plan)
    cache = _DummyCache()

    with pytest.raises(SpotAuthPermanentError):
        await spot_request_module.async_spot_request("Scope", b"payload", cache=cache)

    assert stub_method.call_count == 2
    assert ("spot_token_user@example.com", None) in cache.set_calls
    assert (spot_request_module.DATA_AAS_TOKEN, None) in cache.set_calls


@pytest.mark.asyncio
async def test_spot_request_rate_limit_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resource exhaustion should honor the bounded retry policy."""

    plan = [
        grpclib.exceptions.GRPCError(Status.RESOURCE_EXHAUSTED, "slow down"),
        grpclib.exceptions.GRPCError(Status.RESOURCE_EXHAUSTED, "still slow"),
        grpclib.exceptions.GRPCError(Status.RESOURCE_EXHAUSTED, "again"),
        grpclib.exceptions.GRPCError(Status.RESOURCE_EXHAUSTED, "final"),
    ]
    stub_method = _install_stub_method(monkeypatch, plan)
    cache = _DummyCache()

    with pytest.raises(SpotRateLimitError):
        await spot_request_module.async_spot_request("Scope", b"payload", cache=cache)

    assert stub_method.call_count == 4


@pytest.mark.asyncio
async def test_spot_request_details_string_does_not_crash_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GRPCError.details may be a string; handler should still map the status."""

    plan = [
        grpclib.exceptions.GRPCError(Status.PERMISSION_DENIED, "denied"),
        grpclib.exceptions.GRPCError(Status.PERMISSION_DENIED, "still denied"),
    ]
    stub_method = _install_stub_method(monkeypatch, plan)
    cache = _DummyCache()

    with pytest.raises(SpotAuthPermanentError):
        await spot_request_module.async_spot_request("Scope", b"payload", cache=cache)

    assert stub_method.call_count == 2


@pytest.mark.asyncio
async def test_spot_request_unhandled_status_maps_to_grpc_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unhandled statuses should surface as SpotGrpcStatusError without retries."""

    plan = [grpclib.exceptions.GRPCError(Status.ABORTED, "boom")]
    stub_method = _install_stub_method(monkeypatch, plan)
    cache = _DummyCache()

    with pytest.raises(SpotGrpcStatusError):
        await spot_request_module.async_spot_request("Scope", b"payload", cache=cache)

    assert stub_method.call_count == 1


@pytest.mark.asyncio
async def test_spot_request_empty_payload_raises_trailers_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing or zero-length payload retries before surfacing trailers-only."""

    stub_method = _install_stub_method(
        monkeypatch,
        [b""] * (1 + spot_request_module._SPOT_MAX_RETRIES),
    )
    cache = _DummyCache()

    with pytest.raises(SpotTrailersOnlyError):
        await spot_request_module.async_spot_request("Scope", b"payload", cache=cache)

    assert stub_method.call_count == 1 + spot_request_module._SPOT_MAX_RETRIES
    spot_request_module.SPOT_GRPC_TRANSPORT.reset.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_spot_transport_async_close_holds_lock() -> None:
    """async_close should respect the transport lock to avoid races."""

    transport = SpotGrpcTransport()
    sentinel_channel = _SentinelChannel()
    transport._channel = sentinel_channel  # type: ignore[attr-defined]

    await transport._lock.acquire()  # type: ignore[attr-defined]
    close_task = asyncio.create_task(transport.async_close())

    # Yield to ensure async_close attempts to acquire the lock.
    await asyncio.sleep(0)
    assert not close_task.done()

    transport._lock.release()  # type: ignore[attr-defined]
    await close_task

    assert sentinel_channel.closed


@pytest.mark.asyncio
async def test_spot_request_oserror_retries_without_global_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport-layer OSError retries without resetting the shared channel."""

    plan = [OSError("net down"), b"reply"]
    stub_method = _install_stub_method(monkeypatch, plan)
    cache = _DummyCache()

    result = await spot_request_module.async_spot_request(
        "Scope", b"payload", cache=cache
    )

    assert result == b"reply"
    assert stub_method.call_count == 2
    spot_request_module.SPOT_GRPC_TRANSPORT.reset.assert_not_awaited()  # type: ignore[attr-defined]


class _AuthErrorAPI:
    """API stub raising SpotAuthPermanentError for manual locate tests."""

    async def async_get_device_location(
        self, _dev_id: str, _dev_name: str
    ) -> dict[str, Any]:
        raise SpotAuthPermanentError("session expired")


class _DummyBus:
    """Capture fired events for assertions (no-op for these tests)."""

    def async_fire(
        self, *_args: object, **_kwargs: object
    ) -> None:  # pragma: no cover - stub
        return None

    def async_listen(
        self, *_args: object, **_kwargs: object
    ) -> None:  # pragma: no cover - stub
        return None


class _DummyHass:
    """Lightweight Home Assistant stub for coordinator tests."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401 - stub signature
        return self.loop.create_task(coro, name=name)


class _DummyCacheProtocol:
    """Minimal cache protocol for the coordinator constructor."""

    async def async_get_cached_value(self, _key: str) -> Any:  # pragma: no cover - stub
        return None

    async def async_set_cached_value(
        self, _key: str, _value: Any
    ) -> None:  # pragma: no cover - stub
        return None


class _DummyEntry:
    """Config entry stub tracking reauth calls."""

    def __init__(self) -> None:
        self.entry_id = "entry-test"
        self.data = {"email": "user@example.com"}
        self.reauth_calls = 0

    def async_start_reauth(self, hass) -> None:  # noqa: D401 - stub signature
        # Mirror the real ``ConfigEntry.async_start_reauth``: a synchronous
        # ``@callback`` returning ``None``. An async stub would mask a stray
        # ``await`` in production code (see test_manual_locate_starts_reauth).
        self.reauth_calls += 1


def _prepare_coordinator(loop: asyncio.AbstractEventLoop) -> GoogleFindMyCoordinator:
    hass = _DummyHass(loop)
    coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCacheProtocol())
    coordinator.config_entry = SimpleNamespace(
        entry_id="entry-id",
        options={},
        data={},
        title="Test Entry",
        async_start_reauth=Mock(),
    )
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = [{"id": "dev-1", "name": "Device"}]
    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None

    def _set_update_error(exc: Exception) -> None:
        coordinator.last_update_success = False
        coordinator.last_exception = exc

    def _set_updated_data(data) -> None:
        coordinator.data = data
        coordinator.last_update_success = True
        coordinator.last_exception = None

    coordinator.async_set_update_error = _set_update_error
    coordinator.async_set_updated_data = _set_updated_data
    return coordinator


def test_poll_spot_auth_error_triggers_config_entry_auth_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SpotAuthPermanentError in polling should start the entry reauth flow.

    The poll cycle runs as a fire-and-forget task (``hass.async_create_task``), so a
    raised ``ConfigEntryAuthFailed`` would never reach the awaited coordinator
    refresh and HA's automatic reauth would never fire. The cycle therefore starts
    the entry-scoped reauth flow directly and still records ``last_exception`` so
    the ``finally`` block marks the update failed.
    """

    loop = asyncio.new_event_loop()
    hass_coordinator = _prepare_coordinator(loop)
    hass_coordinator.api = _AuthErrorAPI()

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    try:
        loop.run_until_complete(
            hass_coordinator._async_start_poll_cycle(
                [{"id": "dev-1", "name": "Device"}], force=True
            )
        )
    finally:
        drain_loop(loop)
        loop.close()

    hass_coordinator.config_entry.async_start_reauth.assert_called_once_with(
        hass_coordinator.hass
    )
    assert isinstance(hass_coordinator.last_exception, ConfigEntryAuthFailed)


@pytest.mark.asyncio
async def test_manual_locate_starts_reauth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Service-level locate should start an entry-linked reauth flow on auth failure."""

    loop = asyncio.get_running_loop()
    coordinator = GoogleFindMyCoordinator(_DummyHass(loop), cache=_DummyCacheProtocol())
    entry = _DummyEntry()
    coordinator.config_entry = entry
    coordinator.api = _AuthErrorAPI()
    coordinator._get_google_home_filter = lambda: None
    coordinator.can_request_location = lambda _dev_id: True
    coordinator._api_push_ready = lambda: True
    coordinator._locate_inflight = set()
    coordinator._locate_cooldown_until = {}
    coordinator._device_poll_cooldown_until = {}
    coordinator._device_last_poll_mono = {}
    coordinator._device_location_data = {}
    coordinator.data = []
    coordinator.push_updated = lambda *_args, **_kwargs: None
    coordinator.async_set_updated_data = lambda *_args, **_kwargs: None
    coordinator._record_semantic_label = lambda *_args, **_kwargs: None
    coordinator._apply_semantic_mapping = lambda *_args, **_kwargs: False

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    with pytest.raises(HomeAssistantError) as exc_info:
        await coordinator.async_locate_device("dev-1")

    assert entry.reauth_calls == 1
    # The success branch must report that reauth was started. A stray ``await``
    # on the synchronous ``@callback`` would raise ``TypeError`` (swallowed by
    # the defensive ``except``), leaving ``reauth_started`` False and emitting
    # the "please re-authenticate" message instead. Guard against that.
    assert "has been started" in str(exc_info.value)
