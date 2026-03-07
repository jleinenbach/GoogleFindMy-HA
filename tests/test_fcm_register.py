# tests/test_fcm_register.py
"""Unit tests for the GCM registration flow."""

from __future__ import annotations

import asyncio
import logging
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

    async def __aenter__(self) -> _FakeResponse:  # noqa: D401 - context manager contract
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

    def post(
        self, *, url: str, headers: dict[str, str], data: dict[str, Any], timeout: Any
    ) -> _FakeResponse:
        self.calls.append({"url": url, "data": dict(data), "headers": dict(headers)})
        if not self._responses:
            raise AssertionError("No more responses configured for FakeSession")
        return self._responses.pop(0)


def test_gcm_register_prefers_legacy_sender_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The initial request starts with the legacy server key sender."""

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
    assert session.calls[0]["url"] == GCM_REGISTER3_URL
    assert session.calls[0]["data"]["sender"] == GCM_SERVER_KEY_B64


def test_gcm_register_html_response_rotates_endpoint_not_sender(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """HTML/404 responses rotate the endpoint but never switch sender (upstream alignment)."""

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

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            register.gcm_register({"androidId": 42, "securityToken": 99})
        )

    assert result["token"] == "abc123"
    assert result["android_id"] == 42
    # Endpoint rotates, but sender stays as legacy key
    assert [call["url"] for call in session.calls] == [
        GCM_REGISTER3_URL,
        GCM_REGISTER_URL,
    ]
    assert [call["data"]["sender"] for call in session.calls] == [
        GCM_SERVER_KEY_B64,
        GCM_SERVER_KEY_B64,
    ]


def test_gcm_register_rotation_logs_reason_and_sender(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Endpoint rotation still logs details when no fallback sender is available."""

    responses = [
        _FakeResponse(404, "<!doctype html>not found", {"Content-Type": "text/html"}),
        _FakeResponse(200, "token=abc123", {"Content-Type": "text/plain"}),
    ]
    session = _FakeSession(responses)
    config = FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id=GCM_SERVER_KEY_B64,
        bundle_id="bundle",
    )
    register = FcmRegister(config, http_client_session=session)

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            register.gcm_register({"androidId": 11, "securityToken": 22})
        )

    assert result["token"] == "abc123"
    assert any(
        "GCM register switching endpoint /c2dm/register3 -> /c2dm/register due to HTTP 404"
        in record.getMessage()
        and f"sender={GCM_SERVER_KEY_B64} (legacy server key)" in record.getMessage()
        for record in caplog.records
    )


def test_gcm_register_404_retries_with_same_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 rotates endpoint but keeps the same legacy sender."""

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

    result = asyncio.run(register.gcm_register({"androidId": 11, "securityToken": 22}))

    assert result["token"] == "abc123"
    # Endpoint rotates: register3 → register
    assert [call["url"] for call in session.calls] == [
        GCM_REGISTER3_URL,
        GCM_REGISTER_URL,
    ]
    # Sender stays legacy — no switch to numeric
    assert [call["data"]["sender"] for call in session.calls] == [
        GCM_SERVER_KEY_B64,
        GCM_SERVER_KEY_B64,
    ]


def test_gcm_register_success_log_includes_sender(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Success log records endpoint and sender fallback context."""

    responses = [
        _FakeResponse(200, "token=success", {"Content-Type": "text/plain"}),
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

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            register.gcm_register({"androidId": 1, "securityToken": 2})
        )

    assert result["token"] == "success"
    assert any(
        "GCM register succeeded via /c2dm/register3" in record.getMessage()
        and f"using sender={GCM_SERVER_KEY_B64} (legacy server key)"
        in record.getMessage()
        for record in caplog.records
    )


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

    result = asyncio.run(
        register.gcm_register({"androidId": 1, "securityToken": 2}, retries=2)
    )

    assert result is None
    assert len(session.calls) == 2


def test_gcm_register_phone_registration_error_retries_same_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PHONE_REGISTRATION_ERROR is transient — retries with the same sender (no switch)."""

    responses = [
        _FakeResponse(
            200, "Error=PHONE_REGISTRATION_ERROR", {"Content-Type": "text/plain"}
        ),
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

    result = asyncio.run(
        register.gcm_register({"androidId": 7, "securityToken": 9}, retries=3)
    )

    assert result["token"] == "xyz"
    assert len(session.calls) == 2
    # Both attempts use the same legacy sender — no switch to numeric
    assert session.calls[0]["data"]["sender"] == GCM_SERVER_KEY_B64
    assert session.calls[1]["data"]["sender"] == GCM_SERVER_KEY_B64


def test_gcm_register_phone_registration_error_logs_transient(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """PHONE_REGISTRATION_ERROR log reports the error as transient."""

    responses = [
        _FakeResponse(
            200, "Error=PHONE_REGISTRATION_ERROR", {"Content-Type": "text/plain"}
        ),
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

    with caplog.at_level(logging.INFO):
        result = asyncio.run(
            register.gcm_register({"androidId": 7, "securityToken": 9}, retries=3)
        )

    assert result["token"] == "xyz"
    assert any(
        "PHONE_REGISTRATION_ERROR" in record.getMessage()
        and "transient" in record.getMessage()
        for record in caplog.records
    )


def test_checkin_or_register_reuses_cached_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        raise AssertionError(
            "Unexpected register() invocation when cached credentials exist"
        )

    register.gcm_check_in = types.MethodType(fake_gcm_check_in, register)
    register.register = types.MethodType(fail_register, register)

    result = asyncio.run(register.checkin_or_register())

    assert result is cached_creds
    assert recorded["android_id"] == cached_creds["gcm"]["android_id"]
    assert recorded["security_token"] == cached_creds["gcm"]["security_token"]
