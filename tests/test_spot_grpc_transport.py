from __future__ import annotations

import asyncio
import ssl
from typing import Any
from unittest.mock import AsyncMock

import h2.config
import h2.connection
import h2.events
import pytest

from custom_components.googlefindmy.SpotApi.spot_grpc_transport import (
    RawCodec,
    SpotGrpcTransport,
)


@pytest.mark.enable_socket
@pytest.mark.asyncio
async def test_raw_codec_content_type_on_wire_is_application_grpc(
    socket_enabled: None,
) -> None:
    """
    Validate the HTTP/2 headers emitted by grpclib against a minimal h2 server.
    """
    received: list[list[tuple[str, str]]] = []

    async def _handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        connection = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False)
        )
        connection.initiate_connection()
        writer.write(connection.data_to_send())
        await writer.drain()

        stream_id: int | None = None
        responded = False

        while True:
            data = await reader.read(65535)
            if not data:
                break
            events = connection.receive_data(data)
            for event in events:
                if isinstance(event, h2.events.RequestReceived):
                    received.append(event.headers)
                    stream_id = event.stream_id
                elif isinstance(event, h2.events.DataReceived):
                    connection.acknowledge_received_data(
                        event.flow_controlled_length, event.stream_id
                    )
                elif (
                    isinstance(event, h2.events.StreamEnded)
                    and stream_id is not None
                    and not responded
                ):
                    payload = b"ok"
                    response_headers = [
                        (":status", "200"),
                        ("content-type", "application/grpc"),
                    ]
                    frame = b"\x00" + len(payload).to_bytes(4, "big") + payload
                    connection.send_headers(stream_id, response_headers)
                    connection.send_data(stream_id, frame)
                    connection.send_headers(
                        stream_id, [("grpc-status", "0")], end_stream=True
                    )
                    writer.write(connection.data_to_send())
                    await writer.drain()
                    responded = True
            if responded:
                break

        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    transport: SpotGrpcTransport | None = None
    try:
        sockets = server.sockets or []
        port = sockets[0].getsockname()[1]

        transport = SpotGrpcTransport(
            host="127.0.0.1", port=port, use_ssl=False, codec=RawCodec()
        )
        channel = await transport.get_channel()

        from grpclib.client import UnaryUnaryMethod

        method = UnaryUnaryMethod(
            channel, "/google.internal.spot.v1.SpotService/Test", bytes, bytes
        )
        async with method.open(timeout=5.0) as stream:
            await stream.send_message(b"ping", end=True)
            reply = await stream.recv_message()

        assert reply == b"ok"
        assert received, "Expected HTTP/2 headers to be captured"
        header_map = {k.decode().lower(): v.decode() for k, v in received[0]}
        assert header_map.get("content-type") == "application/grpc"
        assert "+proto" not in header_map.get("content-type", "")
    finally:
        if transport is not None:
            await transport.async_close()
        server.close()
        await server.wait_closed()


def test_raw_codec_reports_base_content_type() -> None:
    """
    Ensure RawCodec exposes the base gRPC media type.
    """

    codec = RawCodec()

    assert codec.content_type == "application/grpc"


@pytest.mark.asyncio
async def test_transport_is_lazy_and_reuses_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Prove: no Channel is constructed at transport init, and get_channel() reuses it.
    """
    created: list[Any] = []

    class _DummyChannel:
        def __init__(
            self,
            host: str,
            port: int,
            *,
            ssl: ssl.SSLContext | None = None,
            codec: Any = None,
        ) -> None:
            self.host = host
            self.port = port
            self.ssl = ssl
            self.codec = codec
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "custom_components.googlefindmy.SpotApi.spot_grpc_transport.Channel",
        _DummyChannel,
    )

    transport = SpotGrpcTransport(host="example.invalid", port=443, use_ssl=False)

    assert created == []

    ch1 = await transport.get_channel()
    ch2 = await transport.get_channel()

    assert ch1 is ch2
    assert len(created) == 1


@pytest.mark.asyncio
async def test_transport_reset_closes_and_recreates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Prove: reset() closes the current channel and forces a new one on next use.
    """
    created: list[Any] = []

    class _DummyChannel:
        def __init__(
            self,
            host: str,
            port: int,
            *,
            ssl: ssl.SSLContext | None = None,
            codec: Any = None,
        ) -> None:
            self.closed = False
            created.append(self)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        "custom_components.googlefindmy.SpotApi.spot_grpc_transport.Channel",
        _DummyChannel,
    )

    transport = SpotGrpcTransport(host="example.invalid", port=443, use_ssl=False)

    ch1 = await transport.get_channel()
    await transport.reset()
    ch2 = await transport.get_channel()

    assert ch1 is not ch2
    assert len(created) == 2
    assert getattr(ch1, "closed", False) is True


@pytest.mark.asyncio
async def test_ssl_context_created_lazily_via_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Prove: SSLContext creation is lazy and uses asyncio.to_thread.
    """
    dummy_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    to_thread = AsyncMock(return_value=dummy_ctx)
    monkeypatch.setattr(asyncio, "to_thread", to_thread)

    created: list[Any] = []

    class _DummyChannel:
        def __init__(
            self,
            host: str,
            port: int,
            *,
            ssl: ssl.SSLContext | None = None,
            codec: Any = None,
        ) -> None:
            created.append(self)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "custom_components.googlefindmy.SpotApi.spot_grpc_transport.Channel",
        _DummyChannel,
    )

    transport = SpotGrpcTransport(host="example.invalid", port=443, use_ssl=True)

    assert to_thread.await_count == 0

    await transport.get_channel()

    assert to_thread.await_count == 1
