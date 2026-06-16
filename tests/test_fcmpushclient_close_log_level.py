# tests/test_fcmpushclient_close_log_level.py
"""Regression suite for the server-initiated ``Close`` log level.

A server-initiated MCS ``Close`` is a normal part of the FCM connection
life-cycle: the worker stops cleanly and the supervisor reconnects. The
``Close`` branch of ``FcmPushClient._handle_message`` was downgraded from a
rate-limited WARNING to INFO so it no longer surfaces as a recurring false
alarm in the Home Assistant log panel, matching the sibling worker-stop paths
(``"FCM stream ended"`` / ``"FCM read ended"``) that already log at INFO.

These tests pin that contract: the path logs at INFO (never WARNING), does not
consume the warn rate-limit, and still signals the worker to stop. Before this
suite ``_handle_message`` ran at ~0 % on the unit level (the poison stub mocks
it away), so the changed branch had no coverage.
"""
from __future__ import annotations

import logging

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    Close,
)
from tests.helpers.fcm_handle_stub import FcmMessageSlim


class TestCloseLogLevel:
    """Behaviour of the ``isinstance(msg, Close)`` branch of ``_handle_message``."""

    async def test_close_logs_info_not_warning(
        self, caplog: logging.LogCaptureFixture
    ) -> None:
        """Server ``Close`` -> single INFO record, no WARNING (false-alarm fix)."""
        client = FcmMessageSlim()

        with caplog.at_level(logging.INFO, logger=client.logger.name):
            await client._handle_message(Close())

        records = [r for r in caplog.records if r.name == client.logger.name]
        # Exactly one record, emitted at INFO. The level IS the contract here
        # (the WARNING -> INFO downgrade); the exact wording is deliberately not
        # pinned so a future reword cannot break this regression test.
        assert len(records) == 1
        assert records[0].levelno == logging.INFO
        assert records[0].getMessage().strip()
        # No record at WARNING or above on this normal life-cycle path.
        assert not [r for r in records if r.levelno >= logging.WARNING]

    async def test_close_does_not_consume_warn_rate_limit(self) -> None:
        """Close path must not call ``_log_warn_with_limit`` after the downgrade."""
        client = FcmMessageSlim()

        await client._handle_message(Close())

        assert client.warnings == []

    async def test_close_stops_worker(self) -> None:
        """Close still sets ``do_listen=False`` so the supervisor reconnects."""
        client = FcmMessageSlim()
        assert client.do_listen is True

        await client._handle_message(Close())

        assert client.do_listen is False
        # The unconditional preamble ran (message accounted for).
        assert client.input_stream_id == 1
