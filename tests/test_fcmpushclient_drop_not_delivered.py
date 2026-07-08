# tests/test_fcmpushclient_drop_not_delivered.py
"""Regression: split the delivery proof from the RMQ2 receipt list.

``persistent_ids`` has two consumers with *conflicting* contracts on one field:

* ``_login`` extends ``received_persistent_id`` from it (RMQ2 dedup) -- it must
  record **every** selective-acked message, or the server replays an already
  received orphan/control push across reconnects and it is delivered+acked
  repeatedly.
* ``fcm_receiver_ha._needs_first_locate_reconnect`` reads it as a "has
  delivered" proof -- it must count **only** genuine deliveries, or a superseded
  push arriving before the first real locate falsely suppresses the needed
  first-locate reconnect.

PR #1167 added a foreign-``subtype`` drop; the caller then appended every push
unconditionally, breaking the reconnect proof (first Codex finding). PR #1173
gated the append on delivery -- which fixed the reconnect proof but broke the
RMQ2 dedup (second Codex finding: dropped-but-acked IDs vanished from the login
receipt list). The correct fix splits the two signals: ``persistent_ids`` again
records every acked message (RMQ2), while a dedicated
``_first_data_message_delivered`` flag carries the delivery proof.

These tests drive the real (unbound) ``_handle_message`` against real
``DataMessageStanza`` protos and pin all halves of the contract: dropped ->
acked AND recorded in ``persistent_ids`` (RMQ2) but delivery flag stays False;
delivered -> acked AND recorded AND delivery flag True.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    FcmPushClient,
)
from custom_components.googlefindmy.Auth.firebase_messaging.proto.mcs_pb2 import (  # pylint: disable=no-name-in-module
    DataMessageStanza,
)

pytestmark = pytest.mark.asyncio


def _make_stanza(*, persistent_id: str, subtype: str) -> DataMessageStanza:
    """Build a real ``DataMessageStanza`` with the app_data the handler reads.

    Includes ``crypto-key``/``encryption`` (parsed on every path before the
    subtype check) plus the ``subtype`` under test. A real proto is required
    because ``_handle_message`` dispatches on ``isinstance(msg,
    DataMessageStanza)``.
    """
    msg = DataMessageStanza()
    msg.persistent_id = persistent_id
    msg.raw_data = b"raw-body"
    for key, value in (
        ("crypto-key", "dh=BPK_value"),
        ("encryption", "salt=SALT_value"),
        ("subtype", subtype),
    ):
        app_datum = msg.app_data.add()
        app_datum.key = key
        app_datum.value = value
    return msg


class _HandleMessageSlim:
    """Composition stub binding the real ``_handle_message`` + ``_handle_data_message``.

    Mirrors only the attributes those methods read on the ``DataMessageStanza``
    branch. ``_decrypt_raw_data`` is stubbed so the *delivered* case needs no real
    crypto; the *dropped* case returns before ever reaching it. Same additive
    composition discipline as ``tests/helpers/fcm_handle_stub.FcmHandleSlim``.
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger(__name__ + "._HandleMessageSlim")
        self.logger.propagate = True
        self.credentials: Any = {"gcm": {"app_id": "APPID"}, "keys": {}}
        self.callback = Mock()  # synchronous: production calls it un-awaited
        self.callback_context = object()
        self._reset_error_count = Mock()
        self._try_increment_error_count = Mock()
        self._consecutive_decrypt_failures = 0
        self.persistent_ids: list[str] = []
        # Delivery proof, split from ``persistent_ids`` (see module docstring).
        self._first_data_message_delivered = False
        self.last_message_time: float = 0.0
        self.input_stream_id = 0
        # Reached only on the matching-subtype (delivered) path.
        self._decrypt_raw_data = lambda credentials, crypto_key, salt, raw_data: (
            b'{"ok": true}'
        )
        self._send_selective_ack = AsyncMock(return_value=None)

    def _log_warn_with_limit(self, msg: str, *args: object) -> None:
        self.logger.warning(msg, *args)

    def _log_verbose(self, msg: str, *args: object) -> None:
        self.logger.debug(msg, *args)

    # Bind the real production coroutines/functions to this light container.
    _handle_message = FcmPushClient._handle_message  # type: ignore[assignment]
    _handle_data_message = FcmPushClient._handle_data_message  # type: ignore[assignment]
    _app_data_by_key = FcmPushClient._app_data_by_key  # type: ignore[assignment]
    _extract_header_param = staticmethod(FcmPushClient._extract_header_param)


async def test_dropped_subtype_is_acked_and_recorded_but_not_delivered() -> None:
    """Foreign-subtype push: selective-acked AND kept in ``persistent_ids`` (RMQ2),
    but the delivery proof stays False so the reconnect is not suppressed."""
    client = _HandleMessageSlim()
    msg = _make_stanza(persistent_id="pid-drop", subtype="OTHER")

    await client._handle_message(msg)

    # RMQ2 dedup: the dropped-but-acked id MUST survive in ``persistent_ids`` --
    # this is the exact list ``_login`` extends into ``received_persistent_id``
    # (fcmpushclient.py:_login), so the server does not replay the orphan push.
    assert client.persistent_ids == ["pid-drop"]
    # Reconnect proof: NOT a delivery -> the flag stays False, so an orphan push
    # no longer falsely suppresses the first-locate reconnect (core of the fix).
    assert client._first_data_message_delivered is False
    # Still selective-acked so the server stops redelivering the orphan push.
    client._send_selective_ack.assert_awaited_once_with("pid-drop")
    # Dropped before decrypt: the notification callback never fired.
    assert not client.callback.called


async def test_delivered_message_is_acked_recorded_and_flagged() -> None:
    """Matching-subtype push: dispatched, recorded in ``persistent_ids`` AND acked
    AND the delivery proof flag is set."""
    client = _HandleMessageSlim()
    msg = _make_stanza(persistent_id="pid-ok", subtype="APPID")

    await client._handle_message(msg)

    assert client.persistent_ids == ["pid-ok"]
    # A genuine delivery -> recorded as the first-locate "has delivered" proof.
    assert client._first_data_message_delivered is True
    client._send_selective_ack.assert_awaited_once_with("pid-ok")
    assert client.callback.called


async def test_dropped_then_delivered_records_both_ids_for_rmq2() -> None:
    """Mixed sequence: an orphan drop followed by a real locate.

    Both ids stay in ``persistent_ids`` (RMQ2 must dedup both across a restart),
    while the delivery proof flips True only on the genuine delivery. Guards the
    exact second Codex finding: the dropped id must not be lost from the receipt
    list even though it did not count as a delivery.
    """
    client = _HandleMessageSlim()

    await client._handle_message(
        _make_stanza(persistent_id="pid-drop", subtype="OTHER")
    )
    # After only the drop: recorded for RMQ2, but not yet "delivered".
    assert client.persistent_ids == ["pid-drop"]
    assert client._first_data_message_delivered is False

    await client._handle_message(_make_stanza(persistent_id="pid-ok", subtype="APPID"))
    # Both ids present for the RMQ2 receipt list; delivery proof now True.
    assert client.persistent_ids == ["pid-drop", "pid-ok"]
    assert client._first_data_message_delivered is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
