# tests/test_api_guard_logging.py
"""Tests for the API guard logging helpers."""

from __future__ import annotations

import inspect
import logging
from typing import Any

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy.Auth.username_provider import username_string
from tests.helpers.api_stub import StubCache

_PROBE_ADDRESS = "guard-probe-7f3a@example.invalid"
_REQUEST_MARKER = "<device list request>"


def test_guard_log_names_entry_without_suffix_artifacts(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ensure `_maybe_log_guard_once` names the entry cleanly.

    The guard state is reset with ``monkeypatch`` rather than by reloading the
    module: a reload rebinds every class in ``api``, and tests in other files
    that imported a class before it then fail their ``isinstance`` checks.
    """

    monkeypatch.setattr(api_module, "_GUARD_LOGGED_ONCE", False)

    caplog.set_level(logging.INFO)
    api_module._maybe_log_guard_once("guard context", entry_id="entry-123")

    assert caplog.records, "Expected guard log message to be emitted"
    record = caplog.records[-1]
    message = record.getMessage()

    assert record.levelno == logging.INFO
    assert "(entry_id=entry-123)" in message
    assert "if extra else" not in message


def test_guard_log_has_no_email_parameter() -> None:
    """The guard log helper offers no way to pass the account e-mail address."""

    parameters = inspect.signature(api_module._maybe_log_guard_once).parameters
    assert "email" not in parameters
    assert "entry_id" in parameters


class _RecordingCache(StubCache):
    """Stub cache that records every key it is asked for, in order."""

    def __init__(self, events: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._events = events

    async def async_get_cached_value(self, key: str) -> Any:
        self._events.append(key)
        return await super().async_get_cached_value(key)


@pytest.mark.asyncio
async def test_device_list_guard_never_logs_the_account_address(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The multi-entry guard path keeps the account address out of the log.

    The address sits in the cache under ``username_string``, where the guard
    path used to read it for the log line. The caller passes ``username`` so
    the read before the request is skipped; any read of ``username_string``
    left is the guard path's own. Two passes cover the first record (INFO) and
    the suppressed repeat (DEBUG).
    """

    events: list[str] = []

    async def _guard(*_a: Any, **_k: Any) -> str:
        events.append(_REQUEST_MARKER)
        raise RuntimeError("Multiple config entries active here")

    monkeypatch.setattr(api_module, "async_request_device_list", _guard)
    monkeypatch.setattr(api_module, "_GUARD_LOGGED_ONCE", False)
    cache = _RecordingCache(
        events, entry_id="entry-1", values={username_string: _PROBE_ADDRESS}
    )
    api = api_module.GoogleFindMyAPI(cache=cache, session=None)
    caplog.set_level(logging.DEBUG, logger=api_module._LOGGER.name)

    for _ in range(2):
        with pytest.raises(UpdateFailed):
            await api.async_get_basic_device_list(username="caller-supplied")

    records = [r for r in caplog.records if r.name == api_module._LOGGER.name]
    for record in records:
        # ``vars(record)`` covers ``args``, ``exc_info``, ``exc_text``,
        # ``stack_info`` and every ``extra`` key, not only the message.
        text = f"{record.getMessage()} {vars(record)!r}"
        for needle in (_PROBE_ADDRESS, "guard-probe-7f3a", "example.invalid", "@"):
            assert needle not in text, (record.levelname, needle)

    guard = [r for r in records if "Auth guard" in r.getMessage()]
    info = [r for r in guard if r.levelno == logging.INFO]
    debug = [r for r in guard if r.levelno == logging.DEBUG]
    assert len(info) == 1
    assert len(debug) >= 1
    for record in (info[0], debug[0]):
        assert "entry_id=entry-1" in record.getMessage()

    assert events.count(_REQUEST_MARKER) == 2
    assert username_string not in events
