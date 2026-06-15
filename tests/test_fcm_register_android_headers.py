# tests/test_fcm_register_android_headers.py
"""Regression tests pinning the ``X-Android-Package`` / ``X-Android-Cert`` headers.

These headers satisfy Google's API-key application restrictions on the Firebase
Installations and FCM registration endpoints. They are a Google protocol
constant mirrored from the upstream GoogleFindMyTools project (commit
``32cffa54``). If a future refactor silently dropped them from one of the
outgoing requests, FCM registration would start failing at Google's edge with
no local error path to point at the cause. The wiring tests below assert that
each of the three call sites (``fcm_install``, ``fcm_refresh_install_token``,
``fcm_register``) actually attaches the headers to the request, and that the
SHA-1 normalization performed in ``FcmRegisterConfig.__post_init__`` reaches the
wire.

Async tests carry an explicit ``@pytest.mark.asyncio`` decorator so the fallback
runner in ``tests/conftest.py::pytest_pyfunc_call`` picks them up in
third-party environments without ``pytest-asyncio`` installed. See
``tests/AGENTS.md`` §"Async tests".
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmregister import (
    FcmRegister,
    FcmRegisterConfig,
)

# The production certificate fingerprint wired in ``fcm_receiver_ha.py`` is the
# colon-stripped, lowercase form below. The raw input is intentionally given in
# the colon-separated uppercase shape so the test also proves that
# ``_normalize_sha1_fingerprint`` runs before the value reaches the header.
_RAW_CERT = "38:91:8A:45:3D:07:19:93:54:F8:B1:9A:F0:5E:C6:56:2C:ED:57:88"
_NORMALIZED_CERT = "38918a453d07199354f8b19af05ec6562ced5788"
_BUNDLE_ID = "com.example.app"


class _RecordingResponse:
    """Async-context-manager response stub recording nothing, returning a body."""

    def __init__(self, status: int, json_value: dict[str, Any]) -> None:
        self.status = status
        self._json = json_value

    async def __aenter__(self) -> _RecordingResponse:  # noqa: D401 - CM contract
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def json(self) -> dict[str, Any]:
        return self._json

    async def text(self) -> str:
        return ""


class _RecordingSession:
    """Minimal aiohttp session stub that records the headers of ``json=`` posts."""

    def __init__(self, response: _RecordingResponse) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        *,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: Any,
    ) -> _RecordingResponse:
        # Snapshot the headers: the production code mutates a single dict, so a
        # shallow copy is required to capture the state at call time.
        self.calls.append({"url": url, "headers": dict(headers), "json": json})
        return self._response


def _config(*, android_cert_sha1: str | None) -> FcmRegisterConfig:
    return FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id="1234567890123",
        bundle_id=_BUNDLE_ID,
        android_cert_sha1=android_cert_sha1,
    )


# ---------------------------------------------------------------------------
# Unit contract of the header helper
# ---------------------------------------------------------------------------
def test_helper_sets_both_headers_and_normalizes_cert() -> None:
    """Both headers are added, and the SHA-1 is normalized before emission."""

    register = FcmRegister(_config(android_cert_sha1=_RAW_CERT))
    headers: dict[str, str] = {}

    register._add_android_restriction_headers(headers)

    assert headers["X-Android-Package"] == _BUNDLE_ID
    assert headers["X-Android-Cert"] == _NORMALIZED_CERT


def test_helper_omits_cert_header_when_unset() -> None:
    """Without a fingerprint the cert header must be absent (package still set)."""

    register = FcmRegister(_config(android_cert_sha1=None))
    headers: dict[str, str] = {}

    register._add_android_restriction_headers(headers)

    assert headers["X-Android-Package"] == _BUNDLE_ID
    assert "X-Android-Cert" not in headers


# ---------------------------------------------------------------------------
# Wiring: each outgoing request carries the headers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fcm_install_attaches_android_headers() -> None:
    """``fcm_install`` attaches the restriction headers to its installation POST."""

    response = _RecordingResponse(
        200,
        {
            "authToken": {"token": "tok", "expiresIn": "3600s"},
            "refreshToken": "refresh",
            "fid": "fid",
        },
    )
    session = _RecordingSession(response)
    register = FcmRegister(
        _config(android_cert_sha1=_RAW_CERT), http_client_session=session
    )

    await register.fcm_install()

    assert len(session.calls) == 1
    sent = session.calls[0]["headers"]
    assert sent["X-Android-Package"] == _BUNDLE_ID
    assert sent["X-Android-Cert"] == _NORMALIZED_CERT


@pytest.mark.asyncio
async def test_fcm_refresh_install_token_attaches_android_headers() -> None:
    """``fcm_refresh_install_token`` attaches the headers to its refresh POST."""

    response = _RecordingResponse(200, {"token": "tok", "expiresIn": "3600s"})
    session = _RecordingSession(response)
    register = FcmRegister(
        _config(android_cert_sha1=_RAW_CERT),
        credentials={
            "fcm": {"installation": {"refresh_token": "refresh", "fid": "fid"}}
        },
        http_client_session=session,
    )

    await register.fcm_refresh_install_token()

    assert len(session.calls) == 1
    sent = session.calls[0]["headers"]
    assert sent["X-Android-Package"] == _BUNDLE_ID
    assert sent["X-Android-Cert"] == _NORMALIZED_CERT


@pytest.mark.asyncio
async def test_fcm_register_attaches_android_headers() -> None:
    """``fcm_register`` attaches the headers to its registration POST."""

    response = _RecordingResponse(200, {"token": "fcm-token"})
    session = _RecordingSession(response)
    register = FcmRegister(
        _config(android_cert_sha1=_RAW_CERT), http_client_session=session
    )

    await register.fcm_register(
        gcm_data={"token": "gcm-token"},
        installation={"token": "install-token"},
        keys={"secret": "secret", "public": "public", "private": "private"},
    )

    assert len(session.calls) == 1
    sent = session.calls[0]["headers"]
    assert sent["X-Android-Package"] == _BUNDLE_ID
    assert sent["X-Android-Cert"] == _NORMALIZED_CERT


@pytest.mark.asyncio
async def test_fcm_install_omits_cert_header_when_unset() -> None:
    """With no fingerprint configured the install POST omits the cert header."""

    response = _RecordingResponse(
        200,
        {
            "authToken": {"token": "tok", "expiresIn": "3600s"},
            "refreshToken": "refresh",
            "fid": "fid",
        },
    )
    session = _RecordingSession(response)
    register = FcmRegister(_config(android_cert_sha1=None), http_client_session=session)

    await register.fcm_install()

    assert len(session.calls) == 1
    sent = session.calls[0]["headers"]
    assert sent["X-Android-Package"] == _BUNDLE_ID
    assert "X-Android-Cert" not in sent
