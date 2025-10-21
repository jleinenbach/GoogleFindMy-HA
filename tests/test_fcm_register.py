# tests/test_fcm_register.py
"""Unit tests for the GCM registration flow."""

from __future__ import annotations

# tests/test_fcm_register.py

import asyncio

import types
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


def test_gcm_register_uses_numeric_sender_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The initial request uses the configured numeric sender value."""

    responses = [
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

    result = asyncio.run(register.gcm_register({"androidId": 1, "securityToken": 2}))

    assert result["token"] == "abc123"
    assert session.calls[0]["data"]["sender"] == "1234567890123"


def test_gcm_register_html_retry_uses_legacy_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """HTML/404 response triggers a single legacy retry before returning to the primary URL."""

    responses = [
        _FakeResponse(404, "<!doctype html>not found", {"Content-Type": "text/html"}),
        _FakeResponse(404, "<!doctype html>legacy not found", {"Content-Type": "text/html"}),
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

    result = asyncio.run(register.gcm_register({"androidId": 42, "securityToken": 99}))

    assert result["token"] == "abc123"
    assert result["android_id"] == 42
    assert [call["url"] for call in session.calls] == [
        GCM_REGISTER_URL,
        GCM_REGISTER3_URL,
        GCM_REGISTER_URL,
    ]
    assert all(call["data"]["sender"] == "1234567890123" for call in session.calls)


def test_gcm_register_non_retryable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-retryable error code stops the retry loop and returns None."""

    responses = [
        _FakeResponse(200, "Error=INVALID_SENDER", {"Content-Type": "text/plain"}),
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

    result = asyncio.run(register.gcm_register({"androidId": 1, "securityToken": 2}, retries=2))

    assert result is None
    assert len(session.calls) == 2


def test_gcm_register_falls_back_to_server_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """PHONE_REGISTRATION_ERROR triggers a second attempt using the legacy server key."""

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


def test_checkin_or_register_reuses_cached_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing credentials trigger a check-in using cached android/security tokens."""

    config = FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id="1234567890123",
        bundle_id="bundle",
    )
    cached_creds = {
        "gcm": {"android_id": "1234567890", "security_token": "9876543210"},
        "fcm": {"registration": {"token": "cached-token"}},
    }
    register = FcmRegister(config, credentials=cached_creds)

    recorded: dict[str, Any] = {}

    async def fake_gcm_check_in(self, android_id=None, security_token=None):
        recorded["android_id"] = android_id
        recorded["security_token"] = security_token
        return {"androidId": android_id, "securityToken": security_token}

    async def fail_register(self):  # pragma: no cover - should not be invoked
        raise AssertionError("Unexpected register() invocation when cached credentials exist")

    register.gcm_check_in = types.MethodType(fake_gcm_check_in, register)
    register.register = types.MethodType(fail_register, register)

    result = asyncio.run(register.checkin_or_register())

    assert result is cached_creds
    assert recorded["android_id"] == cached_creds["gcm"]["android_id"]
    assert recorded["security_token"] == cached_creds["gcm"]["security_token"]
