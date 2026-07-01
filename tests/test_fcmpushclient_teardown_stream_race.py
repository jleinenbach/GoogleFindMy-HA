# tests/test_fcmpushclient_teardown_stream_race.py
"""Regression suite for the shutdown-race stream guards in ``FcmPushClient``.

When a manual quit (or any ``stop()``) tears the connection down,
``_do_writer_close`` nulls ``self.writer`` while the listener task may still be
processing an in-flight inbound heartbeat. The resulting
``_handle_ping -> _send_msg`` then reads a ``None`` writer. The historic
``assert writer is not None`` raised ``AssertionError``, which -- being an
``Exception`` subclass -- fell through the listener's generic arm and surfaced
as an alarming ``"Unknown error in listener: StreamWriter is not initialized"``
traceback for a benign shutdown.

The guards now raise ``ConnectionError`` instead, which the listener already
treats as a normal life-cycle stop (``except (ConnectionError, ssl.SSLError)``).
These tests pin that contract for the whole error class: the send guard
(``_send_msg``) and both read guards (``_read_varint32`` / ``_receive_msg``).
Same additive-composition discipline as ``fcm_handle_stub.FcmMessageSlim`` --
the real unbound methods run without the heavy production constructor.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    FcmPushClient,
    HeartbeatAck,
)


class _SendSlim:
    """Binds the real async ``_send_msg`` with a controllable ``writer``."""

    def __init__(self, writer: object) -> None:
        self.logger = logging.getLogger(__name__ + "._SendSlim")
        self.writer = writer
        self.first_message = False

    def _log_verbose(self, msg: str, *args: object) -> None:
        self.logger.debug(msg, *args)

    def _msg_str(self, msg: object) -> str:  # noqa: PLR6301 - bound stub
        return "<msg>"

    _send_msg = FcmPushClient._send_msg  # type: ignore[assignment]


class _ReadSlim:
    """Binds the real async read helpers with a controllable ``reader``."""

    def __init__(self, reader: object) -> None:
        self.logger = logging.getLogger(__name__ + "._ReadSlim")
        self.reader = reader
        self.first_message = False

    def _log_warn_with_limit(self, msg: str, *args: object) -> None:
        self.logger.warning(msg, *args)

    _read_varint32 = FcmPushClient._read_varint32  # type: ignore[assignment]
    _receive_msg = FcmPushClient._receive_msg  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_send_on_torn_down_writer_raises_connection_error() -> None:
    """``_send_msg`` with a nulled writer -> ConnectionError, NOT AssertionError.

    This is the exact teardown race from the bug report: an in-flight heartbeat
    ack after ``stop()`` nulled ``self.writer``. ConnectionError routes into the
    listener's clean-stop arm; AssertionError would surface as "Unknown error".
    """
    client = _SendSlim(writer=None)

    with pytest.raises(ConnectionError, match="StreamWriter is not initialized"):
        await client._send_msg(HeartbeatAck())


@pytest.mark.asyncio
async def test_send_on_live_writer_writes_and_drains() -> None:
    """Happy path: a live writer still receives the packet and is drained."""
    writer = MagicMock()
    writer.drain = AsyncMock()
    client = _SendSlim(writer=writer)

    await client._send_msg(HeartbeatAck())

    writer.write.assert_called_once()
    writer.drain.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_varint32_on_unconnected_reader_raises_connection_error() -> None:
    """Class sibling: ``_read_varint32`` with a ``None`` reader -> ConnectionError.

    Note ``_do_writer_close`` only nulls ``self.writer``, never ``self.reader``,
    so this guard fires in the not-yet-connected state rather than the exact
    writer teardown race. It is fixed for class consistency (same assert-on-
    stream pattern) so no sibling can surface the "Unknown error" traceback.
    """
    client = _ReadSlim(reader=None)

    with pytest.raises(ConnectionError, match="StreamReader is not initialized"):
        await client._read_varint32()


@pytest.mark.asyncio
async def test_receive_msg_on_unconnected_reader_raises_connection_error() -> None:
    """Class sibling: ``_receive_msg`` with a ``None`` reader -> ConnectionError.

    Like ``_read_varint32`` above, the reader is never nulled during teardown;
    this covers the not-yet-connected state and keeps the whole assert-on-stream
    class consistent (ConnectionError, not AssertionError).
    """
    client = _ReadSlim(reader=None)

    with pytest.raises(ConnectionError, match="StreamReader is not initialized"):
        await client._receive_msg()
