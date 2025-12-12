from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import grpclib.exceptions
import pytest
from grpclib import protocol as grpclib_protocol
from grpclib.const import Cardinality
from grpclib.const import Handler as GrpcHandler
from grpclib.server import Server, Stream
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.SpotApi import spot_request as spot_request_module
from custom_components.googlefindmy.SpotApi.spot_grpc_transport import (
    RawCodec,
    SpotGrpcTransport,
)
from tests.test_spot_grpc_client import (  # package-relative reuse per AGENTS guidance
    _AuthErrorAPI,
    _DummyCacheProtocol,
    _DummyEntry,
    _DummyHass,
    _prepare_coordinator,
)


class _DummyCache:
    """Minimal async cache stub used for SPOT resilience tests."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def get(self, name: str) -> Any:
        return self.values.get(name)

    async def set(self, name: str, value: Any | None) -> None:
        self.values[name] = value


class _StubStream:
    """Unary stream stub that replays a planned behavior."""

    def __init__(self, behavior: Exception | bytes | None) -> None:
        self._behavior = behavior

    async def __aenter__(self) -> _StubStream:  # noqa: D401
        return self

    async def __aexit__(self, exc_type, exc: Exception | None, tb) -> bool:  # noqa: D401
        return False

    async def send_message(self, message: bytes, end: bool = True) -> None:  # noqa: D401
        return None

    async def recv_message(self) -> bytes | None:  # noqa: D401
        if isinstance(self._behavior, Exception):
            raise self._behavior
        return self._behavior


def _install_stub_method(
    monkeypatch: pytest.MonkeyPatch, plan: list[Exception | bytes | None]
):
    """Patch UnaryUnaryMethod to replay planned responses/errors."""

    class _StubMethod:
        call_count = 0

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self._plan = plan

        def open(self, metadata=None, timeout: float | None = None):  # noqa: D401
            idx = min(_StubMethod.call_count, len(self._plan) - 1)
            behavior = self._plan[idx]
            _StubMethod.call_count += 1
            return _StubStream(behavior)

    _StubMethod.call_count = 0
    monkeypatch.setattr(spot_request_module, "UnaryUnaryMethod", _StubMethod)
    return _StubMethod


@pytest.fixture(autouse=True)
def _patch_spot_token_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep SPOT client tests deterministic and offline."""

    monkeypatch.setattr(spot_request_module, "_async_sleep", AsyncMock())
    monkeypatch.setattr(spot_request_module, "_compute_delay", lambda *_: 0.0)
    monkeypatch.setattr(
        spot_request_module,
        "async_get_username",
        AsyncMock(return_value="user@example.com"),
    )
    monkeypatch.setattr(
        spot_request_module, "async_get_spot_token", AsyncMock(return_value="spot-token")
    )
    monkeypatch.setattr(
        spot_request_module,
        "async_get_adm_token_api",
        AsyncMock(return_value="adm-token"),
    )


@pytest.mark.enable_socket
@pytest.mark.asyncio
async def test_content_type_on_wire_is_base_grpc(monkeypatch: pytest.MonkeyPatch) -> None:
    """The raw codec must emit exactly application/grpc on the wire."""

    headers_seen: list[tuple[str, str]] = []
    sent_headers: list[list[tuple[str, str]]] = []

    original_send_request = grpclib_protocol.Stream.send_request

    def _recording_send_request(
        self: grpclib_protocol.Stream,
        headers: list[tuple[str, str]],
        end_stream: bool = False,
        *,
        _processor: grpclib_protocol.EventsProcessor,
    ) -> grpclib_protocol.ReleaseCallback:
        sent_headers.append(list(headers))
        return original_send_request(
            self,
            headers,
            end_stream=end_stream,
            _processor=_processor,
        )

    monkeypatch.setattr(grpclib_protocol.Stream, "send_request", _recording_send_request)

    class _RecordingService:
        def __init__(self) -> None:
            self.requests: list[bytes | None] = []

        async def Test(self, stream: Stream[bytes, bytes]) -> None:  # noqa: D401
            nonlocal headers_seen
            headers_seen = list(stream.metadata or [])
            self.requests.append(await stream.recv_message())
            await stream.send_message(b"ack")

        def __mapping__(self) -> dict[str, GrpcHandler]:  # noqa: D401
            return {
                "/google.internal.spot.v1.SpotService/Test": GrpcHandler(
                    self.Test,
                    Cardinality.UNARY_UNARY,
                    bytes,
                    bytes,
                )
            }

    service = _RecordingService()
    server = Server([service], codec=RawCodec())
    await server.start("127.0.0.1", 0)
    transport: SpotGrpcTransport | None = None
    try:
        assert server._server is not None  # type: ignore[attr-defined]
        sockets = getattr(server._server, "sockets", [])  # type: ignore[attr-defined]
        port = sockets[0].getsockname()[1]
        transport = SpotGrpcTransport(host="127.0.0.1", port=port, use_ssl=False)
        cache = _DummyCache()

        reply = await asyncio.wait_for(
            spot_request_module.async_spot_request(
                "Test",
                b"payload",
                cache=cache,
                transport=transport,
            ),
            timeout=5.0,
        )

        assert reply == b"ack"
        assert sent_headers
        header_map = {k.lower(): v for k, v in sent_headers[0]}
        assert header_map.get("content-type") == "application/grpc"
    finally:
        if transport is not None:
            await transport.async_close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_empty_ok_payload_retries_without_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty OK payloads should retry without resetting the shared channel."""

    plan = [b"", b"", b"restored"]
    _install_stub_method(monkeypatch, plan)
    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())
    transport.reset = AsyncMock()
    cache = _DummyCache()

    result = await spot_request_module.async_spot_request(
        "Scope",
        b"payload",
        cache=cache,
        transport=transport,  # type: ignore[arg-type]
    )

    assert result == b"restored"
    assert transport.reset.await_count == 0


@pytest.mark.asyncio
async def test_protocol_error_triggers_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Protocol errors should reset the channel before retrying."""

    plan = [grpclib.exceptions.ProtocolError("boom"), b"reply"]
    _install_stub_method(monkeypatch, plan)
    transport = AsyncMock()
    transport.get_channel = AsyncMock(return_value=object())
    transport.reset = AsyncMock()
    cache = _DummyCache()

    result = await spot_request_module.async_spot_request(
        "Scope",
        b"payload",
        cache=cache,
        transport=transport,  # type: ignore[arg-type]
    )

    assert result == b"reply"
    transport.reset.assert_awaited_once()


def test_polling_path_translates_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """SpotAuthPermanentError from the API should surface as ConfigEntryAuthFailed."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hass_coordinator = _prepare_coordinator(loop)
    hass_coordinator.api = _AuthErrorAPI()

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        try:
            loop.run_until_complete(
                hass_coordinator._async_start_poll_cycle(
                    [{"id": "dev-1", "name": "Device"}], force=True
                )
            )
        finally:
            from tests.helpers import drain_loop

            drain_loop(loop)
            loop.close()

    assert isinstance(hass_coordinator.last_exception, ConfigEntryAuthFailed)


@pytest.mark.asyncio
async def test_manual_locate_reports_reauth_status() -> None:
    """Manual locate should start reauth and raise a user-facing error."""

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
    coordinator._device_location_data = {}
    coordinator.data = []
    coordinator.async_set_updated_data = Mock()

    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_locate_device("dev-1")

    assert "re-authentication" in str(err.value)
    assert entry.reauth_calls == 1
