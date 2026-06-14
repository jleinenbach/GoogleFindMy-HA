from __future__ import annotations

import asyncio
import ssl
from typing import Final

from grpclib.client import Channel
from grpclib.encoding.base import CodecBase

__all__ = [
    "RawCodec",
    "SpotGrpcTransport",
    "SPOT_GRPC_TRANSPORT",
    "is_ssl_transport_teardown_error",
]


def is_ssl_transport_teardown_error(exc: BaseException) -> bool:
    """Return True for the asyncio SSL-transport teardown race (AP9, finding 6a).

    During a connection teardown there is a narrow window in which asyncio has
    already detached the SSL transport (``_SSLProtocolTransport._ssl_protocol``
    is ``None``) but grpclib's ``handler.connection_lost`` flag is not set yet.
    A write that lands in that window raises
    ``AttributeError: 'NoneType' object has no attribute '_write_appdata'`` from
    deep inside ``asyncio.sslproto`` — code we do not own, so we cannot attach a
    custom exception type at the throw site. Isolating the fragile asyncio
    signature in this single predicate is the legitimate boundary discriminator:
    every other ``AttributeError`` is a real bug and must propagate.
    """
    return isinstance(exc, AttributeError) and "_write_appdata" in str(exc)


class RawCodec(CodecBase):
    """
    A grpclib codec that treats messages as opaque bytes.

    This integration intentionally bypasses protobuf serialization in grpclib.
    Callers provide already-serialized request bytes and receive raw response bytes.

    Compatibility requirement (critical):
    Google's SPOT frontend is strict about the gRPC media type. The on-wire HTTP/2
    header must be exactly:

        content-type: application/grpc

    and must not include any "+proto" or other suffix.

    grpclib derives content-type validation from `__content_subtype__` but allows
    codecs to advertise a custom base content type. This codec disables the
    subtype and explicitly reports the base "application/grpc" media type so
    strict servers accept it without a "+proto" suffix.
    """

    __content_subtype__: str = "proto"

    @property
    def content_type(self) -> str:
        """
        Return the base gRPC media type.

        This must remain exactly "application/grpc" to avoid strict backend rejects.
        """
        return "application/grpc"

    def encode(self, message: bytes, _message_type: type) -> bytes:
        """
        Return outgoing message bytes without transformation.

        Raises:
            TypeError: If `message` is not bytes-like.
        """
        if isinstance(message, bytes):
            return message
        if isinstance(message, (bytearray, memoryview)):
            return bytes(message)
        raise TypeError(f"RawCodec expects bytes-like, got {type(message)!r}")

    def decode(self, data: bytes, _message_type: type) -> bytes:
        """
        Return response payload bytes without transformation.

        Raises:
            TypeError: If `data` is not bytes-like.
        """
        if isinstance(data, bytes):
            return data
        if isinstance(data, (bytearray, memoryview)):
            return bytes(data)
        raise TypeError(f"RawCodec expects bytes-like, got {type(data)!r}")


class SpotGrpcTransport:
    """
    Shared grpclib transport for SPOT gRPC calls.

    Design goals:
    - Lazy initialization: no sockets/SSL at import time.
    - Non-blocking SSL context creation using asyncio.to_thread.
    - HTTP/2 ALPN negotiation via "h2".
    - Concurrency safety for channel lifecycle using an internal asyncio.Lock.
    - Explicit reset/close primitives for callers to use on fatal transport errors.
    """

    def __init__(
        self,
        *,
        host: str = "spot-pa.googleapis.com",
        port: int = 443,
        use_ssl: bool = True,
        codec: CodecBase | None = None,
    ) -> None:
        self._host: Final[str] = host
        self._port: Final[int] = port
        self._use_ssl: Final[bool] = use_ssl
        self._codec: Final[CodecBase] = codec or RawCodec()

        self._lock = asyncio.Lock()
        self._channel: Channel | None = None
        self._ssl_context: ssl.SSLContext | None = None

    async def _get_ssl_context(self) -> ssl.SSLContext:
        """
        Lazily create and cache an SSLContext without blocking the event loop.

        Returns:
            ssl.SSLContext: Context configured for gRPC over HTTP/2 (ALPN "h2").
        """
        if self._ssl_context is not None:
            return self._ssl_context

        ctx = await asyncio.to_thread(ssl.create_default_context)
        try:
            ctx.set_alpn_protocols(["h2"])
        except NotImplementedError:
            pass
        except AttributeError:
            pass

        self._ssl_context = ctx
        return ctx

    async def get_channel(self) -> Channel:
        """
        Return a shared grpclib Channel, creating it lazily and safely.

        Returns:
            grpclib.client.Channel: A reusable HTTP/2 channel suitable for multiplexing.

        Notes:
            This method is concurrency-safe. Exactly one channel is created per transport
            instance unless `reset()` / `async_close()` clears it.
        """
        if self._channel is not None:
            return self._channel

        async with self._lock:
            if self._channel is not None:
                return self._channel

            ssl_ctx = await self._get_ssl_context() if self._use_ssl else None
            self._channel = Channel(
                self._host, self._port, ssl=ssl_ctx, codec=self._codec
            )
            return self._channel

    async def reset(self) -> None:
        """
        Close and drop the cached channel so the next call recreates it.

        This is intended for callers to use on fatal channel/protocol failures
        where a clean reconnection is required.
        """
        async with self._lock:
            if self._channel is None:
                return
            self._channel.close()
            self._channel = None

    async def async_close(self) -> None:
        """
        Idempotently close the cached channel.

        This should be awaited during integration unload when the transport is no
        longer needed.
        """
        async with self._lock:
            if self._channel is None:
                return
            self._channel.close()
            self._channel = None


SPOT_GRPC_TRANSPORT = SpotGrpcTransport()
