# tests/test_nova_error_response_description.py
"""Nova error records describe the response body, they never quote it.

`AGENTS.md` section 5 keeps raw API payloads out of the log at every level.
A non-2xx Nova response used to reach the WARNING/ERROR records and the
`NovaHTTPError` message as a 512-character snippet of the body (a Google
error page echoes request parameters; a `google.rpc.Status` message is
server-supplied free text). The record now carries the HTTP status and, for
a Status, the RPC code by name with the size of its message; for anything
else the content class and the byte count.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from custom_components.googlefindmy.NovaApi import nova_request as nova_module
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaHTTPError,
    async_nova_request,
)
from tests.test_nova_request import _DummyResponse, _DummySession, _StubCache

_TOKEN = "ya29.a0AfB_byDq9x3EtH2kY7VzWc8ghUGrOpN1JmQ5aTe4bRxL7sKdZyCvIiHpMuWn"
_EMAIL = "someone.private@example.org"
_HTML_BODY = (
    "<!DOCTYPE html><html><body><h1>Error 501</h1><p>The request for "
    f"{_EMAIL} with token {_TOKEN} was not implemented.</p></body></html>"
).encode()
_RPC_MESSAGE = f"Request had invalid authentication credentials for {_EMAIL}"


def _windows(value: str) -> list[str]:
    return [value[i : i + 8] for i in range(0, len(value) - 7)]


def _rpc_status_body(code: int, message: str) -> bytes:
    assert nova_module.RpcStatus is not None, "google.rpc.Status is required here"
    status = nova_module.RpcStatus()
    status.code = code
    status.message = message
    return bytes(status.SerializeToString())


# --- the describer itself -------------------------------------------------


def test_describe_error_response_names_rpc_code_not_message() -> None:
    described = nova_module._describe_error_response(
        _rpc_status_body(7, _RPC_MESSAGE), 403
    )
    assert described == (
        f"HTTP 403 - RPC 7 PERMISSION_DENIED, message {len(_RPC_MESSAGE)} chars, "
        "0 detail message(s)"
    )
    assert _EMAIL not in described


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (_HTML_BODY, "html"),
        (b'  {"error": {"code": 400}}', "json"),
        (b"[1, 2]", "json"),
        (b"Error=BAD_AUTHENTICATION", "text"),
        (b"\xff\xfe\x00binary", "binary"),
        (b"<html><body>Fehler \xe4 hier</body></html>", "html"),
        (b" " * 70 + b"<html>", "html"),
        (b"x" * 63 + "ä".encode() + b" text", "text"),
        (b"   ", "empty"),
    ],
)
def test_describe_error_response_classifies_other_bodies(
    body: bytes, kind: str
) -> None:
    described = nova_module._describe_error_response(body, 502)
    assert described == f"HTTP 502 body={kind}, {len(body)} bytes"


def test_describe_error_response_empty_body() -> None:
    assert nova_module._describe_error_response(b"", 503) == (
        "HTTP 503 (empty response body)"
    )


# --- through the transport: log records and the exception message ---------


async def _fail_after_retries(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> NovaHTTPError:
    """Drive the retry path: every attempt logs the described response."""
    cache = _StubCache()
    # 503 is a transient 5xx: NOVA_MAX_RETRIES + 1 attempts, each one logged
    # ("Server response: ..."), then the ERROR record and NovaHTTPError.
    attempts = nova_module.NOVA_MAX_RETRIES + 1
    session = _DummySession([_DummyResponse(503, body) for _ in range(attempts)])

    async def _fake_get_adm_token(
        username: str | None = None,
        *,
        retries: int = 2,
        backoff: float = 1.0,
        cache: Any,
    ) -> str:
        return "resolved-token"

    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.nova_request.async_get_adm_token_api",
        _fake_get_adm_token,
    )

    async def _instant_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    with pytest.raises(NovaHTTPError) as exc_info:
        await async_nova_request(
            "testScope", "00", username="user@example.com", cache=cache, session=session
        )
    assert len(session.calls) == attempts
    return exc_info.value


@pytest.mark.asyncio
async def test_html_error_body_never_reaches_log_or_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=nova_module._LOGGER.name):
        error = await _fail_after_retries(monkeypatch, _HTML_BODY)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for window in _windows(_TOKEN):
        assert window not in logged, "a window of the echoed token reached the log"
        assert window not in str(error)
    assert _EMAIL not in logged
    assert _EMAIL not in str(error)
    described = f"Server response: HTTP 503 body=html, {len(_HTML_BODY)} bytes"
    # One retry record per failed attempt but the last, which logs the ERROR.
    assert logged.count(described) == nova_module.NOVA_MAX_RETRIES
    assert (
        f"Last server response: HTTP 503 body=html, {len(_HTML_BODY)} bytes" in logged
    )
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_rpc_status_message_never_reaches_log_or_exception(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=nova_module._LOGGER.name):
        error = await _fail_after_retries(
            monkeypatch, _rpc_status_body(16, _RPC_MESSAGE)
        )

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert _EMAIL not in logged
    assert "invalid authentication" not in logged
    assert _EMAIL not in str(error)
    assert "invalid authentication" not in str(error)
    assert "Last server response: HTTP 503 - RPC 16 UNAUTHENTICATED, message " in logged
