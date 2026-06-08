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
    GCM_SERVER_KEY_B64,
)
from custom_components.googlefindmy.Auth.firebase_messaging.fcmregister import (
    FcmRegister,
    FcmRegisterConfig,
    FcmRegisterHTTPError,
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
    """HTML/404 responses retry on the same endpoint without switching sender (upstream alignment)."""

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
    # Both attempts use the same endpoint — no rotation (upstream alignment)
    assert [call["url"] for call in session.calls] == [
        GCM_REGISTER3_URL,
        GCM_REGISTER3_URL,
    ]
    assert [call["data"]["sender"] for call in session.calls] == [
        GCM_SERVER_KEY_B64,
        GCM_SERVER_KEY_B64,
    ]


def test_gcm_register_404_retries_with_same_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 retries on the same endpoint with the same legacy sender."""

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
    # No endpoint rotation — always /c2dm/register3 (upstream alignment)
    assert [call["url"] for call in session.calls] == [
        GCM_REGISTER3_URL,
        GCM_REGISTER3_URL,
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


def test_gcm_register_raises_on_persistent_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the retry budget is exhausted on persistent 404 responses,
    gcm_register must raise FcmRegisterHTTPError(status=404) so the caller
    can run the dedicated endpoint retry budget instead of treating it as
    a transient runtime error.
    """
    responses = [
        _FakeResponse(404, "<!doctype html>not found", {"Content-Type": "text/html"})
        for _ in range(8)
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

    with pytest.raises(FcmRegisterHTTPError) as exc_info:
        asyncio.run(
            register.gcm_register({"androidId": 1, "securityToken": 2}, retries=8)
        )

    assert exc_info.value.status == 404


def test_gcm_register_raises_on_persistent_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the retry budget is exhausted on persistent 401 responses
    without a parsed ``Error=`` body (for example an HTML/empty
    unauthorized response), gcm_register must raise
    FcmRegisterHTTPError(status=401) so the supervisor's auth path
    (token invalidation via _invalidate_fcm_tokens) runs instead of
    treating the auth denial as a transient runtime error.
    """
    responses = [
        _FakeResponse(401, "Unauthorized", {"Content-Type": "text/plain"})
        for _ in range(8)
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

    with pytest.raises(FcmRegisterHTTPError) as exc_info:
        asyncio.run(
            register.gcm_register({"androidId": 1, "securityToken": 2}, retries=8)
        )

    assert exc_info.value.status == 401


async def test_gcm_register_non_fatal_status_returns_none_not_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative contract: persistent non-fatal HTTP status (500) drains the
    retry budget but must NOT raise FcmRegisterHTTPError — the caller must
    keep treating it as a transient RuntimeError so the regular retry path
    runs instead of the dedicated auth/endpoint budget. Pins that the
    numeric fatal-status cache stays ``None`` for codes outside
    ``_FATAL_HTTP_STATUSES``.
    """
    responses = [
        _FakeResponse(500, "Internal Server Error", {"Content-Type": "text/plain"})
        for _ in range(8)
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

    result = await register.gcm_register(
        {"androidId": 1, "securityToken": 2}, retries=8
    )

    assert result is None


async def test_gcm_register_mixed_status_caches_last_fatal_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache discipline: when a fatal response (401) sits among non-fatal
    ones (500), the numeric cache must end up pinned to the fatal code so
    the post-loop classifier raises with the right status. A subsequent
    non-fatal response must not clear the cached fatal code.
    """
    responses: list[_FakeResponse] = []
    # 3 transient failures, one persistent auth denial, then 4 more
    # transient failures — the fatal status must survive the trailing
    # non-fatal noise.
    responses.extend(
        _FakeResponse(500, "Internal Server Error", {"Content-Type": "text/plain"})
        for _ in range(3)
    )
    responses.append(
        _FakeResponse(401, "Unauthorized", {"Content-Type": "text/plain"})
    )
    responses.extend(
        _FakeResponse(500, "Internal Server Error", {"Content-Type": "text/plain"})
        for _ in range(4)
    )
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

    with pytest.raises(FcmRegisterHTTPError) as exc_info:
        await register.gcm_register(
            {"androidId": 1, "securityToken": 2}, retries=8
        )

    assert exc_info.value.status == 401


async def test_gcm_register_classifier_independent_of_logger_output(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wire-contract guard against regression on substring-based
    classification: even when every ``_logger.warning/info/error`` call in
    the fcmregister module is silenced, ``gcm_register`` must still raise
    ``FcmRegisterHTTPError`` for a persistent fatal status. This pins that
    the defense reads the numeric status cache, not a marker substring of
    the logger output. If a future refactor reroutes classification through
    the log string, this test fails — because the silenced logger no longer
    produces a ``status=N`` substring while the cache still does.
    """
    from custom_components.googlefindmy.Auth.firebase_messaging import fcmregister

    class _NullLogger:
        """Pyno-op stand-in for the module-level logger."""

        def debug(self, *_a: object, **_kw: object) -> None:
            return None

        def info(self, *_a: object, **_kw: object) -> None:
            return None

        def warning(self, *_a: object, **_kw: object) -> None:
            return None

        def error(self, *_a: object, **_kw: object) -> None:
            return None

    monkeypatch.setattr(fcmregister, "_logger", _NullLogger())

    responses = [
        _FakeResponse(404, "<!doctype html>not found", {"Content-Type": "text/html"})
        for _ in range(8)
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
        with pytest.raises(FcmRegisterHTTPError) as exc_info:
            await register.gcm_register(
                {"androidId": 1, "securityToken": 2}, retries=8
            )

    assert exc_info.value.status == 404
    # Defense is independent of logger output: the silenced logger
    # captured nothing, yet the classifier still surfaced the fatal.
    assert "status=404" not in caplog.text
