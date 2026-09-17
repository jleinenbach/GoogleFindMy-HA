# tests/test_get_eid_info_request_logging.py
"""The `DecodeError` warning names the body's content class, not its bytes.

`AGENTS.md` section 5: raw API payloads are never logged. The warning used to
carry the first 16 bytes of the undecodable body as hex; what the reader
needs is the length and whether the body was an HTML error page, JSON or
binary, and that is what `kind=` says now. Async and sync entry points share
the wording and are both covered.
"""

from __future__ import annotations

import hashlib
import logging

import pytest

from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
    get_eid_info_request as module,
)
from google.protobuf.message import DecodeError

_HTML = b"  <!DOCTYPE html><html><body>Sign in</body></html>"
_BINARY = hashlib.sha256(b"garbage-frame").digest() + b"\xff" * 8


def _windows(body: bytes) -> list[str]:
    head = body[:16].hex()
    return [head[i : i + 8] for i in range(0, len(head) - 7)]


@pytest.mark.parametrize(
    ("body", "kind"), [(_HTML, "html"), (_BINARY, "binary"), (b'{"error":1}', "json")]
)
@pytest.mark.asyncio
async def test_async_decode_error_logs_kind_not_bytes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    body: bytes,
    kind: str,
) -> None:
    async def _spot(*_a: object, **_k: object) -> bytes:
        return body

    monkeypatch.setattr(module, "_spot_call_async", _spot)
    caplog.set_level(logging.WARNING, logger=module._LOGGER.name)

    with pytest.raises(DecodeError):
        await module.async_get_eid_info(cache=object())  # type: ignore[arg-type]

    messages = [
        r.getMessage() for r in caplog.records if "DecodeError" in r.getMessage()
    ]
    assert len(messages) == 1
    assert f"len={len(body)}" in messages[0]
    assert f"kind={kind}" in messages[0]
    assert body[:16].hex() not in caplog.text
    assert all(w not in caplog.text for w in _windows(body)), (
        "a window of the undecodable body reached the log"
    )


@pytest.mark.asyncio
async def test_async_empty_body_logs_kind_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _spot(*_a: object, **_k: object) -> bytes:
        return b""

    monkeypatch.setattr(module, "_spot_call_async", _spot)
    caplog.set_level(logging.WARNING, logger=module._LOGGER.name)

    with pytest.raises(module.SpotApiEmptyResponseError):
        await module.async_get_eid_info(cache=object())  # type: ignore[arg-type]

    assert "(len=0, kind=empty)" in caplog.text


def test_sync_empty_body_logs_kind_empty(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(module.spot_request_module, "spot_request", lambda *_a: b"")
    caplog.set_level(logging.WARNING, logger=module._LOGGER.name)

    with pytest.raises(module.SpotApiEmptyResponseError):
        module.get_eid_info()

    assert "(len=0, kind=empty)" in caplog.text


def test_sync_decode_error_logs_kind_not_bytes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(module.spot_request_module, "spot_request", lambda *_a: _HTML)
    caplog.set_level(logging.WARNING, logger=module._LOGGER.name)

    with pytest.raises(DecodeError):
        module.get_eid_info()

    messages = [
        r.getMessage() for r in caplog.records if "DecodeError" in r.getMessage()
    ]
    assert len(messages) == 1
    assert "kind=html" in messages[0]
    assert all(w not in caplog.text for w in _windows(_HTML))


@pytest.mark.parametrize(
    ("head", "kind"),
    [
        (b"", "empty"),
        (b"   \n", "empty"),
        (b"<html>", "html"),
        (b"\n\t<!DOCTYPE", "html"),
        (b"{}", "json"),
        (b"[1]", "json"),
        (b"\x00\x01", "binary"),
        (b"plain text", "binary"),
    ],
)
def test_classify_body(head: bytes, kind: str) -> None:
    assert module._classify_body(head) == kind
