# tests/test_fcmpushclient_heartbeat_ack_log.py
"""The heartbeat-ack DEBUG record names the message type, never its fields.

`_handle_message` used to pass the `HeartbeatAck` itself to `%s`, which
renders the protobuf text format with every server-supplied value. The
message carries only stream counters, but the record is a raw wire dump and
`AGENTS.md` section 5 keeps raw API payloads out of the log at every level;
the record now goes through `_msg_str` like the other MCS messages.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    FcmPushClient,
)
from custom_components.googlefindmy.Auth.firebase_messaging.proto.mcs_pb2 import (  # pylint: disable=no-name-in-module
    HeartbeatAck,
)


class _HandleAckSlim:
    """Composition stub binding the real `_handle_message` and `_msg_str`.

    Only the attributes the heartbeat-ack branch reads are mirrored (same
    additive discipline as `tests/test_fcmpushclient_drop_not_delivered.py`).
    """

    def __init__(self, verbose: bool) -> None:
        self.logger = logging.getLogger(__name__ + "._HandleAckSlim")
        self.logger.propagate = True
        self.config = SimpleNamespace(log_debug_verbose=verbose)
        self._reset_error_count = Mock()
        self.last_message_time: float = 0.0
        self.input_stream_id = 0

    _handle_message = FcmPushClient._handle_message  # type: ignore[assignment]
    _msg_str = FcmPushClient._msg_str  # type: ignore[assignment]


@pytest.mark.asyncio
@pytest.mark.parametrize("verbose", [False, True])
async def test_heartbeat_ack_record_names_type_not_values(
    caplog: pytest.LogCaptureFixture, verbose: bool
) -> None:
    client = _HandleAckSlim(verbose=verbose)
    ack = HeartbeatAck()
    ack.stream_id = 4_242_017
    ack.last_stream_id_received = 9_090_913
    ack.status = 77_777_001

    with caplog.at_level(logging.DEBUG, logger=client.logger.name):
        await client._handle_message(ack)

    records = [
        r.getMessage() for r in caplog.records if "heartbeat ack" in r.getMessage()
    ]
    assert len(records) == 1, caplog.text
    logged = records[0]
    assert "HeartbeatAck" in logged
    for value in ("4242017", "9090913", "77777001"):
        assert value not in logged, logged
    assert ("stream_id" in logged) is verbose  # field names only in verbose mode
    assert client._reset_error_count.call_count == 2  # READ, then CONNECTION
