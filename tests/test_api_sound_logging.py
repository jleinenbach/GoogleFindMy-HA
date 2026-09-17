# tests/test_api_sound_logging.py
"""The Play/Stop Sound DEBUG records never carry the raw Nova response.

`AGENTS.md` section 5: raw API payloads are never logged, at any level. The
Nova reply to an ExecuteAction is an opaque body that is never parsed
(`docs/PLAY_SOUND_ARCHITECTURE.md`), so the record keeps the byte count and
nothing of the content. The fake body below has no repeating pattern on
purpose: with `"ab" * 120` every eight-character window would match every
other and the window assertion would be trivially true.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import pytest

from custom_components.googlefindmy import api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI
from tests.helpers.api_stub import (
    FakeReceiver,
    StubCache,
    install_receiver_provider,
    run_coro,
)

# 240 hex characters (120 bytes) with no repeating substring: eight SHA-256
# digests of distinct seeds, concatenated, then cut to length.
_HEX = "".join(hashlib.sha256(f"nova-body-{i}".encode()).hexdigest() for i in range(8))[
    :240
]
assert len(_HEX) == 240
assert len({_HEX[i : i + 8] for i in range(0, len(_HEX) - 7)}) == len(_HEX) - 7


@pytest.fixture(autouse=True)
def _reset_fcm_provider() -> Any:
    saved = api_module._FCM_ReceiverGetter
    yield
    api_module._FCM_ReceiverGetter = saved


def _api_with_token(monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyAPI:
    install_receiver_provider(monkeypatch, FakeReceiver(token="sound-token-1234567"))
    return GoogleFindMyAPI(cache=StubCache(entry_id="e"))


def _assert_body_absent(text: str) -> None:
    assert _HEX not in text
    # No eight-character window of the body either: a truncated dump
    # (`[:200]`) is still a dump.
    assert all(_HEX[i : i + 8] not in text for i in range(0, len(_HEX) - 7)), (
        "a window of the raw Nova response reached the log"
    )


def test_play_sound_debug_record_omits_raw_response(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _api_with_token(monkeypatch)

    async def _submit(*_a: Any, **_k: Any) -> Any:
        return (_HEX, "uuid-1234")

    monkeypatch.setattr(api_module, "async_submit_start_sound_request", _submit)
    caplog.set_level(logging.DEBUG, logger=api_module._LOGGER.name)

    result = run_coro(api.async_play_sound("device-1"))

    assert result.accepted
    _assert_body_absent(caplog.text)
    # The byte count stays: that is the one fact about the body the record keeps.
    assert any(
        "Play Sound Nova response" in record.getMessage()
        and f"{len(_HEX) // 2} bytes" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG
    )


def test_stop_sound_debug_record_omits_raw_response(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    api = _api_with_token(monkeypatch)

    async def _submit(*_a: Any, **_k: Any) -> Any:
        return _HEX

    monkeypatch.setattr(api_module, "async_submit_stop_sound_request", _submit)
    caplog.set_level(logging.DEBUG, logger=api_module._LOGGER.name)

    run_coro(api.async_stop_sound("device-1", "uuid-1234"))

    _assert_body_absent(caplog.text)
    assert any(
        "Stop Sound Nova response" in record.getMessage()
        and f"{len(_HEX) // 2} bytes" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.DEBUG
    )
