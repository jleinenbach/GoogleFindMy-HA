# tests/test_fcmpushclient_drop_not_delivered.py
"""Regression: a dropped foreign-subtype push must not count as delivered.

PR #1167 added a subtype-mismatch drop inside ``_handle_data_message`` (a push
encrypted for a *superseded* GCM registration returns early, before decrypt).
The caller ``_handle_message`` used to append ``msg.persistent_id`` to
``persistent_ids`` unconditionally. ``fcm_receiver_ha._needs_first_locate_
reconnect`` reads a non-empty ``persistent_ids`` as proof that the fresh session
has already delivered its first data message -- so a superseded/orphan push
arriving before any real locate falsely suppressed the needed first-locate
reconnect, leaving the entry to the slower starvation-recovery path (Codex
review on merge commit 5d2d280).

The fix makes ``_handle_data_message`` report whether a real payload was
delivered; ``_handle_message`` records ``persistent_ids`` only for genuine
deliveries while still selective-acking every message. These tests drive the
real (unbound) ``_handle_message`` against real ``DataMessageStanza`` protos and
pin both halves of the contract: dropped -> acked but NOT recorded; delivered ->
acked AND recorded.
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


async def test_dropped_subtype_is_acked_but_not_recorded() -> None:
    """Foreign-subtype push: selective-acked, but NOT added to ``persistent_ids``."""
    client = _HandleMessageSlim()
    msg = _make_stanza(persistent_id="pid-drop", subtype="OTHER")

    await client._handle_message(msg)

    # Not a delivery -> the reconnect "has delivered" proof stays empty. This is
    # the core of the Codex fix: an orphan push no longer suppresses the reconnect.
    assert client.persistent_ids == []
    # Still selective-acked so the server stops redelivering the orphan push.
    client._send_selective_ack.assert_awaited_once_with("pid-drop")
    # Dropped before decrypt: the notification callback never fired.
    assert not client.callback.called


async def test_delivered_message_is_acked_and_recorded() -> None:
    """Matching-subtype push: dispatched, recorded in ``persistent_ids`` AND acked."""
    client = _HandleMessageSlim()
    msg = _make_stanza(persistent_id="pid-ok", subtype="APPID")

    await client._handle_message(msg)

    # A genuine delivery -> recorded as the first-locate "has delivered" proof.
    assert client.persistent_ids == ["pid-ok"]
    client._send_selective_ack.assert_awaited_once_with("pid-ok")
    assert client.callback.called


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
