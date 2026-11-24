# tests/test_coordinator_auth_transition_log.py
"""The switch into the re-authentication state is logged once.

Polling keeps failing while an entry waits for re-authentication. The first
failure is the event worth an ERROR line; repeats are only DEBUG, so the log
is not flooded with the same error every poll. After a recovery, the next
failure is a new transition and gets its ERROR line again.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.coordinator import main as coordinator_main

_ENTRY_ID = "entry-auth-1"
_REASON = "Nova auth error: Rejected for owner@example.com"


def _coordinator() -> Any:
    """Only the attributes the auth state machine reads and writes."""

    return SimpleNamespace(
        _auth_error_active=False,
        _auth_error_since=None,
        _auth_error_message=None,
        config_entry=SimpleNamespace(entry_id=_ENTRY_ID),
        hass=SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock())),
        data={},
        _create_auth_issue=MagicMock(),
        _dismiss_auth_issue=MagicMock(return_value=True),
        _get_account_email=MagicMock(return_value="owner@example.com"),
        async_set_updated_data=MagicMock(),
    )


def _set(coordinator: Any, *, failed: bool) -> None:
    coordinator_main.GoogleFindMyCoordinator._set_auth_state(
        coordinator, failed=failed, reason=_REASON if failed else None
    )


def _records(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == level and "Authentication for entry" in record.getMessage()
    ]


def test_two_failures_in_a_row_log_one_error_then_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = _coordinator()

    with caplog.at_level(logging.DEBUG, logger=coordinator_main.__name__):
        _set(coordinator, failed=True)
        _set(coordinator, failed=True)

    errors = _records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert _ENTRY_ID in errors[0].getMessage()
    assert len(_records(caplog, logging.DEBUG)) == 1
    # The reason can carry producer text; it stays out of the log line.
    assert "owner@example.com" not in caplog.text


def test_recovery_and_new_failure_log_a_second_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = _coordinator()

    with caplog.at_level(logging.DEBUG, logger=coordinator_main.__name__):
        _set(coordinator, failed=True)
        _set(coordinator, failed=False)
        _set(coordinator, failed=True)

    assert len(_records(caplog, logging.ERROR)) == 2
    assert _records(caplog, logging.DEBUG) == []
