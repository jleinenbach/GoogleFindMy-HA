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
    FcmCheckinTransientError,
    FcmRegister,
    FcmRegisterConfig,
    FcmRegisterHTTPError,
)

# Coroutine regression tests below carry an explicit ``@pytest.mark.asyncio``
# decorator so the fallback runner in ``tests/conftest.py::pytest_pyfunc_call``
# (which only executes coroutine tests carrying the ``asyncio`` marker when
# ``pytest-asyncio`` is absent) picks them up in third-party environments
# without ``pytest-asyncio`` installed. Real CI installs ``pytest-asyncio`` in
# ``auto`` mode (pyproject.toml), where the decorator is idempotent. A
# module-level ``pytestmark`` was rejected because it would annotate the
# synchronous tests in this file as ``asyncio``, which produces pytest-asyncio
# warnings under ``auto`` mode. See ``tests/AGENTS.md`` §"Async tests".


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
        self.calls.append(
            {
                "url": url,
                "data": dict(data),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        if not self._responses:
            raise AssertionError("No more responses configured for FakeSession")
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_gcm_register_prefers_legacy_sender_first(
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

    result = await register.gcm_register({"androidId": 1, "securityToken": 2})

    assert result["token"] == "abc123"
    assert session.calls[0]["url"] == GCM_REGISTER3_URL
    assert session.calls[0]["data"]["sender"] == GCM_SERVER_KEY_B64


@pytest.mark.asyncio
async def test_gcm_register_html_response_rotates_endpoint_not_sender(
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
        result = await register.gcm_register({"androidId": 42, "securityToken": 99})

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


@pytest.mark.asyncio
async def test_gcm_register_404_retries_with_same_sender(
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

    result = await register.gcm_register({"androidId": 11, "securityToken": 22})

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


@pytest.mark.asyncio
async def test_gcm_register_success_log_includes_sender(
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
        result = await register.gcm_register({"androidId": 1, "securityToken": 2})

    assert result["token"] == "success"
    assert any(
        "GCM register succeeded via /c2dm/register3" in record.getMessage()
        and f"using sender={GCM_SERVER_KEY_B64} (legacy server key)"
        in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_gcm_register_non_retryable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    result = await register.gcm_register(
        {"androidId": 1, "securityToken": 2}, retries=2
    )

    assert result is None
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_gcm_register_phone_registration_error_retries_same_sender(
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

    result = await register.gcm_register(
        {"androidId": 7, "securityToken": 9}, retries=3
    )

    assert result["token"] == "xyz"
    assert len(session.calls) == 2
    # Both attempts use the same legacy sender — no switch to numeric
    assert session.calls[0]["data"]["sender"] == GCM_SERVER_KEY_B64
    assert session.calls[1]["data"]["sender"] == GCM_SERVER_KEY_B64


@pytest.mark.asyncio
async def test_gcm_register_phone_registration_error_logs_transient(
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
        result = await register.gcm_register(
            {"androidId": 7, "securityToken": 9}, retries=3
        )

    assert result["token"] == "xyz"
    assert any(
        "PHONE_REGISTRATION_ERROR" in record.getMessage()
        and "transient" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_checkin_or_register_reuses_cached_credentials(
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

    result = await register.checkin_or_register()

    assert result is cached_creds
    assert recorded["android_id"] == cached_creds["gcm"]["android_id"]
    assert recorded["security_token"] == cached_creds["gcm"]["security_token"]


@pytest.mark.asyncio
async def test_gcm_register_raises_on_persistent_404(
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
        await register.gcm_register({"androidId": 1, "securityToken": 2}, retries=8)

    assert exc_info.value.status == 404


@pytest.mark.asyncio
async def test_gcm_register_raises_on_persistent_401(
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
        await register.gcm_register({"androidId": 1, "securityToken": 2}, retries=8)

    assert exc_info.value.status == 401


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_gcm_register_fatal_followed_by_transient_clears_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-wins cache discipline: when a fatal response (401) is followed
    by transient ones (500), the FINAL attempt's status determines the
    classification. The trailing transient attempts must clear the stale
    fatal latch so the caller continues to treat the failure as a transient
    runtime error (regular retry path), not as an auth denial (which would
    trigger token invalidation via ``_invalidate_fcm_tokens``).

    Regression for Codex PR #1087 review on commit 559afde82f: previous
    behaviour stored the first fatal status and kept it across non-fatal
    follow-ups, escalating ordinary transient failures through the wrong
    recovery path. Mirrors the pre-existing ``last_error`` last-wins
    update discipline.
    """
    responses: list[_FakeResponse] = []
    # 3 transient failures, one auth denial, then 4 more transient
    # failures — the FINAL response is transient, so the cached fatal
    # latch must have been cleared by the time the retry budget is
    # exhausted.
    responses.extend(
        _FakeResponse(500, "Internal Server Error", {"Content-Type": "text/plain"})
        for _ in range(3)
    )
    responses.append(_FakeResponse(401, "Unauthorized", {"Content-Type": "text/plain"}))
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

    result = await register.gcm_register(
        {"androidId": 1, "securityToken": 2}, retries=8
    )

    # No raise: trailing transients cleared the stale fatal latch.
    assert result is None


@pytest.mark.asyncio
async def test_gcm_register_transient_followed_by_fatal_raises_final_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-wins, other direction: when transient failures (500) are
    followed by a persistent fatal (401), the FINAL status wins and
    surfaces as ``FcmRegisterHTTPError(status=401)``. Pins that the
    last-wins clear path does not accidentally suppress a real terminal
    fatal at the end of the retry budget.
    """
    responses: list[_FakeResponse] = []
    responses.extend(
        _FakeResponse(500, "Internal Server Error", {"Content-Type": "text/plain"})
        for _ in range(3)
    )
    responses.extend(
        _FakeResponse(401, "Unauthorized", {"Content-Type": "text/plain"})
        for _ in range(5)
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
        await register.gcm_register({"androidId": 1, "securityToken": 2}, retries=8)

    assert exc_info.value.status == 401


@pytest.mark.asyncio
async def test_gcm_register_fatal_followed_by_error_code_clears_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-wins by HTTP STATUS (SSOT) across response branches: a fatal
    HTTP status (401) followed by structured GCM error bodies with a
    NON-FATAL HTTP status (200, ``Error=PHONE_REGISTRATION_ERROR``) must
    clear the cached fatal because the final attempt's HTTP status is
    not in ``_FATAL_HTTP_STATUSES``. Pins that the error_code branch
    resets the latch when the HTTP status is non-fatal (this is the
    common case where the server replies 200 with a protocol-level
    error body).
    """
    responses: list[_FakeResponse] = []
    responses.append(_FakeResponse(401, "Unauthorized", {"Content-Type": "text/plain"}))
    responses.extend(
        _FakeResponse(
            200,
            "Error=PHONE_REGISTRATION_ERROR",
            {"Content-Type": "text/plain"},
        )
        for _ in range(7)
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

    result = await register.gcm_register(
        {"androidId": 1, "securityToken": 2}, retries=8
    )

    # No raise: error_code branch cleared the stale fatal latch.
    assert result is None


@pytest.mark.asyncio
async def test_gcm_register_error_code_with_fatal_status_preserves_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-wins by HTTP STATUS (SSOT) — the structured ``Error=...``
    body branch must NOT clear the fatal latch when the HTTP status
    itself is fatal (401/404). The server can return a structured error
    body together with an HTTP-fatal status; the body shape is
    orthogonal to the auth/endpoint classification used by the caller.

    Regression for Codex iter-2 finding on PR #1087, commit 7a89e2e321:
        "When /c2dm/register3 returns a structured Error=... body with
        an HTTP 401/404 status on the final retry, this branch
        unconditionally clears last_fatal_status, so gcm_register falls
        through and returns None instead of raising FcmRegisterHTTPError."
    """
    responses = [
        _FakeResponse(
            401,
            "Error=PHONE_REGISTRATION_ERROR",
            {"Content-Type": "text/plain"},
        )
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
        await register.gcm_register({"androidId": 1, "securityToken": 2}, retries=8)

    assert exc_info.value.status == 401


@pytest.mark.asyncio
async def test_gcm_register_transient_then_fatal_error_code_status_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-wins by HTTP STATUS: a transient 5xx response followed by a
    final error-code body with a fatal HTTP status (401) must still
    classify as fatal via the error_code branch. Pins that the
    error_code branch promotes the latch back to fatal when the HTTP
    status warrants it (the inverse of
    test_gcm_register_fatal_followed_by_error_code_clears_latch).

    Note: 404 is handled by the dedicated 404/HTML branch BEFORE the
    error_code branch ever sees the body, so 401 is the relevant fatal
    status for testing the error_code branch in isolation.
    """
    responses: list[_FakeResponse] = []
    responses.extend(
        _FakeResponse(503, "Server busy", {"Content-Type": "text/plain"})
        for _ in range(7)
    )
    responses.append(
        _FakeResponse(
            401,
            "Error=PHONE_REGISTRATION_ERROR",
            {"Content-Type": "text/plain"},
        )
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
        await register.gcm_register({"androidId": 1, "securityToken": 2}, retries=8)

    assert exc_info.value.status == 401


@pytest.mark.asyncio
async def test_gcm_register_fatal_followed_by_exception_clears_latch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-wins applies across the network-exception branch: a fatal
    HTTP status (401) followed by network failures must clear the cached
    fatal because the network-exception branch carries no HTTP status
    and is transient by definition. Pins that an aiohttp/network
    exception resets the latch even though it never sees a status code.
    """

    class _ExceptionThenResponseSession:
        """Returns one fatal response then raises ``OSError`` for the
        remaining attempts (mirrors a network drop after one bad reply).
        """

        def __init__(self, first_response: _FakeResponse) -> None:
            self._first = first_response
            self._first_served = False
            self.calls: list[dict[str, Any]] = []

        def post(
            self,
            *,
            url: str,
            headers: dict[str, str],
            data: dict[str, Any],
            timeout: Any,
        ) -> _FakeResponse:
            self.calls.append(
                {"url": url, "data": dict(data), "headers": dict(headers)}
            )
            if not self._first_served:
                self._first_served = True
                return self._first
            raise OSError("simulated network failure")

    session = _ExceptionThenResponseSession(
        _FakeResponse(401, "Unauthorized", {"Content-Type": "text/plain"})
    )
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

    # No raise: exception branch cleared the stale fatal latch.
    assert result is None
    # All eight attempts were made (1 response + 7 exceptions).
    assert len(session.calls) == 8


@pytest.mark.asyncio
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
            await register.gcm_register({"androidId": 1, "securityToken": 2}, retries=8)

    assert exc_info.value.status == 404
    # Defense is independent of logger output: the silenced logger
    # captured nothing, yet the classifier still surfaced the fatal.
    assert "status=404" not in caplog.text


# --------------------------------------------------------------------------
# gcm_check_in transient-vs-permanent taxonomy (FcmCheckinTransientError)
# --------------------------------------------------------------------------


class _RaisingCheckinResponse:
    """Async CM whose ``__aenter__`` raises a transport error (HTTP never reached)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> _RaisingCheckinResponse:
        raise self._exc

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _SequencedCheckinSession:
    """Session stub yielding transport failures and/or HTTP responses in order.

    Each configured item is either an ``Exception`` (simulating a transport-layer
    failure such as a DNS timeout, so the endpoint is never reached) or a
    ``_FakeResponse`` (the server answered at the HTTP layer).
    """

    def __init__(self, items: list[Exception | _FakeResponse]) -> None:
        self._items = items
        self.calls = 0

    def post(self, **_kwargs: Any) -> _RaisingCheckinResponse | _FakeResponse:
        self.calls += 1
        if not self._items:
            raise AssertionError("No more check-in items configured")
        item = self._items.pop(0)
        if isinstance(item, Exception):
            return _RaisingCheckinResponse(item)
        return item


def _checkin_config() -> FcmRegisterConfig:
    return FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id="1234567890123",
        bundle_id="bundle",
    )


async def _fast_sleep(_: float) -> None:
    return None


@pytest.mark.asyncio
async def test_gcm_check_in_transient_exhaustion_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pure transport exhaustion (endpoint never reached) raises transient, not None."""
    session = _SequencedCheckinSession([TimeoutError("DNS timeout")] * 8)
    register = FcmRegister(_checkin_config(), http_client_session=session)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    with pytest.raises(FcmCheckinTransientError):
        await register.gcm_check_in(1, 2)
    assert session.calls == 8  # full retry budget consumed


@pytest.mark.asyncio
async def test_gcm_check_in_authoritative_non_ok_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated non-OK HTTP responses (server reachable) still return None."""
    session = _SequencedCheckinSession(
        [_FakeResponse(500, "err", {"Content-Type": "text/plain"}) for _ in range(8)]
    )
    register = FcmRegister(_checkin_config(), http_client_session=session)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    result = await register.gcm_check_in(1, 2)
    assert result is None


@pytest.mark.asyncio
async def test_gcm_check_in_mixed_reached_server_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport blip followed by non-OK HTTP is authoritative, not transient.

    Guards the ``saw_http_response`` discriminator: once the endpoint answered at
    the HTTP layer, exhaustion must return ``None`` (defer to the register
    fallback), even though a transport exception was seen earlier.
    """
    session = _SequencedCheckinSession(
        [TimeoutError("DNS blip"), TimeoutError("DNS blip")]
        + [_FakeResponse(500, "err", {"Content-Type": "text/plain"}) for _ in range(6)]
    )
    register = FcmRegister(_checkin_config(), http_client_session=session)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    result = await register.gcm_check_in(1, 2)
    assert result is None


@pytest.mark.asyncio
async def test_reregister_keeping_identity_preserves_on_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient check-in outage propagates without rotating the device identity."""
    session = _SequencedCheckinSession([TimeoutError("DNS timeout")] * 8)
    creds = {"gcm": {"android_id": 1, "security_token": 2}}
    register = FcmRegister(_checkin_config(), creds, http_client_session=session)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    register_called = False

    async def _spy_register() -> Any:
        nonlocal register_called
        register_called = True
        return None

    monkeypatch.setattr(register, "register", _spy_register)

    with pytest.raises(FcmCheckinTransientError):
        await register.reregister_keeping_identity()
    assert register_called is False  # no full register() -> identity kept
    assert register.credentials == creds


@pytest.mark.asyncio
async def test_checkin_or_register_preserves_on_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient check-in outage propagates instead of falling through to register()."""
    session = _SequencedCheckinSession([TimeoutError("DNS timeout")] * 8)
    creds = {"gcm": {"android_id": 1, "security_token": 2}}
    register = FcmRegister(_checkin_config(), creds, http_client_session=session)
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    register_called = False

    async def _spy_register() -> Any:
        nonlocal register_called
        register_called = True
        return None

    monkeypatch.setattr(register, "register", _spy_register)

    with pytest.raises(FcmCheckinTransientError):
        await register.checkin_or_register()
    assert register_called is False


# ---------------------------------------------------------------------
# gcm_unregister — best-effort orphan cleanup on re-registration
# ---------------------------------------------------------------------

_OLD_APP_ID = "wp:bundle#0d7d2715-75d9-47a0-88ec-32e9d91e88dd"
_NEW_APP_ID = "wp:bundle#f23ca93d-683d-4fb9-aaee-fdbd82dd079a"
_ANDROID_ID = "1234567890"
_SECURITY_TOKEN = "9876543210"


class _RaisingSession:
    """Session stub whose ``post`` records the call, then raises (best-effort
    failure path for the unregister)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def post(
        self, *, url: str, headers: dict[str, str], data: dict[str, Any], timeout: Any
    ) -> Any:
        self.calls.append({"url": url, "data": dict(data), "headers": dict(headers)})
        raise OSError("network down")


def _build_reregister(
    session: Any,
    *,
    old_app_id: str | None,
    new_app_id: str = _NEW_APP_ID,
) -> FcmRegister:
    """FcmRegister wired for ``reregister_keeping_identity`` with every network
    step EXCEPT the unregister POST stubbed out, so the only ``session.post``
    that can occur is the orphan unregister under test."""
    config = FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id="1234567890123",
        bundle_id="bundle",
    )
    gcm_creds: dict[str, Any] = {
        "android_id": _ANDROID_ID,
        "security_token": _SECURITY_TOKEN,
    }
    if old_app_id is not None:
        gcm_creds["app_id"] = old_app_id
    credentials = {"gcm": gcm_creds, "fcm": {"registration": {"token": "old"}}}
    register = FcmRegister(config, credentials=credentials, http_client_session=session)

    async def fake_check_in(self, android_id=None, security_token=None):  # type: ignore[no-untyped-def]
        return {"androidId": android_id, "securityToken": security_token}

    async def fake_gcm_register(self, gcm_response, retries=5):  # type: ignore[no-untyped-def]
        return {
            "token": "new-gcm-token",
            "app_id": new_app_id,
            "android_id": gcm_response["androidId"],
            "security_token": gcm_response["securityToken"],
        }

    def fake_generate_keys(self):  # type: ignore[no-untyped-def]
        return {"public": "pub", "private": "priv", "auth_secret": "sec"}

    async def fake_fcm_install_and_register(self, gcm_data, keys):  # type: ignore[no-untyped-def]
        return {"registration": {"token": "new-fcm-token"}}

    register.gcm_check_in = types.MethodType(fake_check_in, register)
    register.gcm_register = types.MethodType(fake_gcm_register, register)
    register.generate_keys = types.MethodType(fake_generate_keys, register)
    register.fcm_install_and_register = types.MethodType(
        fake_fcm_install_and_register, register
    )
    return register


@pytest.mark.asyncio
async def test_reregister_unregisters_old_subscription() -> None:
    """(a) A successful re-registration fires exactly one ``delete=true`` POST
    to register3 carrying the OLD app_id as X-subtype."""
    session = _FakeSession(
        [_FakeResponse(200, f"deleted={_OLD_APP_ID}", {"Content-Type": "text/plain"})]
    )
    register = _build_reregister(session, old_app_id=_OLD_APP_ID)

    result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == GCM_REGISTER3_URL
    assert call["data"]["delete"] == "true"
    assert call["data"]["X-subtype"] == _OLD_APP_ID
    assert call["data"]["device"] == _ANDROID_ID
    assert call["headers"]["Authorization"] == (
        f"AidLogin {_ANDROID_ID}:{_SECURITY_TOKEN}"
    )


@pytest.mark.asyncio
async def test_unregister_uses_short_bounded_timeout() -> None:
    """(a2) The best-effort unregister runs inside the re-registration critical
    path, so it MUST use the short ``UNREGISTER_TIMEOUT`` (5 s), never the
    class-wide 100 s ``CLIENT_TIMEOUT`` — otherwise a stalled delete keeps push
    reception offline for up to 100 s after the new subscription is already
    persisted (Codex P2)."""
    session = _FakeSession(
        [_FakeResponse(200, f"deleted={_OLD_APP_ID}", {"Content-Type": "text/plain"})]
    )
    register = _build_reregister(session, old_app_id=_OLD_APP_ID)

    await register.reregister_keeping_identity()

    assert len(session.calls) == 1
    used = session.calls[0]["timeout"]
    assert used is FcmRegister.UNREGISTER_TIMEOUT
    assert used is not FcmRegister.CLIENT_TIMEOUT
    assert used.total == 5
    # Intent guard: the cleanup bound must stay well below the register path.
    assert used.total < FcmRegister.CLIENT_TIMEOUT.total


@pytest.mark.asyncio
async def test_reregister_survives_unregister_failure() -> None:
    """(b) An unregister that raises must not break the re-registration: the
    caller still returns valid fresh credentials, and it was attempted once."""
    session = _RaisingSession()
    register = _build_reregister(session, old_app_id=_OLD_APP_ID)

    result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID
    assert result["fcm"]["registration"]["token"] == "new-fcm-token"
    assert len(session.calls) == 1  # exactly one best-effort attempt, no retry


@pytest.mark.asyncio
async def test_reregister_without_old_app_id_skips_unregister() -> None:
    """(c) Without a prior app_id (first-time / legacy identity) no delete POST
    is emitted."""
    session = _FakeSession([])  # any post would raise "no responses configured"
    register = _build_reregister(session, old_app_id=None)

    result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID
    assert session.calls == []


@pytest.mark.asyncio
async def test_reregister_unchanged_app_id_skips_unregister() -> None:
    """(c2) A no-op re-registration that yields the SAME app_id must not
    self-delete the still-current subscription."""
    session = _FakeSession([])
    register = _build_reregister(
        session, old_app_id=_OLD_APP_ID, new_app_id=_OLD_APP_ID
    )

    result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _OLD_APP_ID
    assert session.calls == []


@pytest.mark.asyncio
async def test_reregister_unregister_redacts_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(d) No unredacted secret/id (old app_id, android_id, security_token, or
    the AidLogin auth header) may appear in the DEBUG log; only the ``•••``
    redacted tail form is emitted."""
    session = _FakeSession(
        [_FakeResponse(200, f"deleted={_OLD_APP_ID}", {"Content-Type": "text/plain"})]
    )
    register = _build_reregister(session, old_app_id=_OLD_APP_ID)
    register._log_debug_verbose = True  # exercise the verbose request log too

    with caplog.at_level(logging.DEBUG):
        result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert _OLD_APP_ID not in logged
    assert _ANDROID_ID not in logged
    assert _SECURITY_TOKEN not in logged
    assert "AidLogin" not in logged
    assert "•••" in logged


# ---------------------------------------------------------------------
# gcm_unregister — outcome-log contract (each of the four terminal
# outcomes must be observable at DEBUG, so a user who enables debug
# logging and presses the "regenerate FCM token" button can tell whether
# the orphan cleanup succeeded or not). These also close the parse-branch
# coverage for the ``Error=`` and "no marker" responses.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reregister_unregister_success_is_visible_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(e) A ``deleted=`` response logs an *effective* outcome at DEBUG,
    without needing the verbose flag — this is what the user sees on a
    successful button-triggered cleanup."""
    session = _FakeSession(
        [_FakeResponse(200, f"deleted={_OLD_APP_ID}", {"Content-Type": "text/plain"})]
    )
    register = _build_reregister(session, old_app_id=_OLD_APP_ID)

    with caplog.at_level(logging.DEBUG):
        result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "GCM unregister effective" in logged
    assert "deleted" in logged
    assert _OLD_APP_ID not in logged  # still redacted


@pytest.mark.asyncio
async def test_reregister_unregister_error_is_visible_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(f) A server ``Error=<code>`` response is a non-exceptional, ineffective
    outcome: no raise, exactly one attempt, and a DEBUG line naming the code so
    the user sees the cleanup did NOT succeed. Closes the ``Error`` parse/log
    branch."""
    session = _FakeSession(
        [
            _FakeResponse(
                200, "Error=PHONE_REGISTRATION_ERROR", {"Content-Type": "text/plain"}
            )
        ]
    )
    register = _build_reregister(session, old_app_id=_OLD_APP_ID)

    with caplog.at_level(logging.DEBUG):
        result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID  # re-registration unaffected
    assert len(session.calls) == 1  # one best-effort attempt, no retry
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "ineffective" in logged
    assert "PHONE_REGISTRATION_ERROR" in logged
    assert _OLD_APP_ID not in logged  # still redacted


@pytest.mark.asyncio
async def test_reregister_unregister_no_marker_is_visible_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(g) A 200 response with neither ``deleted`` nor ``Error`` (e.g. an HTML
    error page) is reported as an ineffective "no marker" outcome at DEBUG,
    never raising. Closes the else-branch of the parser."""
    session = _FakeSession(
        [_FakeResponse(200, "<html>gateway</html>", {"Content-Type": "text/html"})]
    )
    register = _build_reregister(session, old_app_id=_OLD_APP_ID)

    with caplog.at_level(logging.DEBUG):
        result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID
    assert len(session.calls) == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "no deleted/Error marker" in logged
    assert _OLD_APP_ID not in logged  # still redacted


@pytest.mark.asyncio
async def test_reregister_unregister_network_failure_is_visible_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """(h) A network/aiohttp failure is swallowed but reported at DEBUG as a
    best-effort failure, so the user sees the cleanup was attempted and did not
    succeed. Complements (b), which asserts survival without the log contract."""
    session = _RaisingSession()
    register = _build_reregister(session, old_app_id=_OLD_APP_ID)

    with caplog.at_level(logging.DEBUG):
        result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID
    assert len(session.calls) == 1
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "failed (ignored, best-effort)" in logged
    assert _OLD_APP_ID not in logged  # still redacted
