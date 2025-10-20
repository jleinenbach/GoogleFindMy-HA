# tests/test_fcm_register.py
"""Unit tests for the GCM registration flow."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.const import (
    GCM_REGISTER3_URL,
    GCM_REGISTER_URL,
    GCM_SERVER_KEY_B64,
)
from custom_components.googlefindmy.Auth.firebase_messaging.fcmregister import (
    FcmRegister,
    FcmRegisterConfig,
)


@dataclass
class _FakeResponse:
    status: int
    text_value: str
    headers: dict[str, str]

    async def __aenter__(self) -> "_FakeResponse":  # noqa: D401 - context manager contract
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def text(self) -> str:
        return self.text_value


class _FakeSession:
    """Minimal aiohttp session stub that records post requests."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def post(self, *, url: str, headers: dict[str, str], data: dict[str, Any], timeout: Any) -> _FakeResponse:
        self.calls.append({"url": url, "data": dict(data), "headers": dict(headers)})
        if not self._responses:
            raise AssertionError("No more responses configured for FakeSession")
        return self._responses.pop(0)


def test_gcm_register_switches_to_register3(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """A 404/HTML response triggers the legacy /register3 fallback."""

    responses = [
        _FakeResponse(404, "<!doctype html>not found", {"Content-Type": "text/html"}),
        _FakeResponse(200, "token=abc123", {"Content-Type": "text/plain"}),
    ]
    session = _FakeSession(responses)
    config = FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id="1234567890123",
        bundle_id="bundle",
    )
    register = FcmRegister(config, http_client_session=session)

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    caplog.set_level(logging.WARNING, logger="custom_components.googlefindmy.Auth.firebase_messaging.fcmregister")

    result = asyncio.run(register.gcm_register({"androidId": 42, "securityToken": 99}))

    assert result["token"] == "abc123"
    assert result["android_id"] == 42
    assert result["security_token"] == 99
    assert result["app_id"].startswith("wp:bundle#")

    assert session.calls[0]["url"] == GCM_REGISTER_URL
    assert session.calls[1]["url"] == GCM_REGISTER3_URL
    assert session.calls[0]["data"]["sender"] == "1234567890123"

    warning_messages = [record.message for record in caplog.records if record.levelno >= logging.WARNING]
    assert any("toggling endpoint" in message for message in warning_messages)


def test_gcm_register_non_retryable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-retryable error code stops the retry loop and returns None."""

    responses = [
        _FakeResponse(200, "Error=INVALID_SENDER", {"Content-Type": "text/plain"}),
    ]
    session = _FakeSession(responses)
    config = FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id="1234567890123",
        bundle_id="bundle",
    )
    register = FcmRegister(config, http_client_session=session)

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    result = asyncio.run(register.gcm_register({"androidId": 1, "securityToken": 2}, retries=3))

    assert result is None
    assert len(session.calls) == 1


def test_gcm_register_falls_back_to_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """PHONE_REGISTRATION_ERROR triggers a second attempt using the server key sender."""

    responses = [
        _FakeResponse(200, "Error=PHONE_REGISTRATION_ERROR", {"Content-Type": "text/plain"}),
        _FakeResponse(200, "token=xyz", {"Content-Type": "text/plain"}),
    ]
    session = _FakeSession(responses)
    config = FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id="1234567890123",
        bundle_id="bundle",
    )
    register = FcmRegister(config, http_client_session=session)

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    result = asyncio.run(register.gcm_register({"androidId": 7, "securityToken": 9}, retries=3))

    assert result["token"] == "xyz"
    assert len(session.calls) == 2
    assert session.calls[0]["data"]["sender"] == "1234567890123"
    assert session.calls[1]["data"]["sender"] == GCM_SERVER_KEY_B64
