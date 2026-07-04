# tests/test_token_retrieval.py
"""Regression tests for the AAS-token error classifier in token_retrieval.

These tests pin the FIX 5 matcher hardening: HTTP-generic tokens
("unauthorized"/"forbidden"/status codes) must no longer promote an arbitrary
transport/network exception string to :class:`InvalidAasTokenError`, while the
gpsoauth-specific ``Error`` vocabulary (``badauthentication``) keeps doing so.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import token_retrieval
from custom_components.googlefindmy.Auth.token_retrieval import (
    InvalidAasTokenError,
    _is_invalid_aas_error_text,
)

# ---------------------------------------------------------------------------
# _is_invalid_aas_error_text: HTTP-generic gating
# ---------------------------------------------------------------------------


def test_http_generic_forbidden_gated_off_by_default() -> None:
    """A bare "403 forbidden" string is NOT an invalid-AAS signal by default."""
    assert (
        _is_invalid_aas_error_text(
            "server returned 403 forbidden", allow_http_generic=False
        )
        is False
    )


def test_http_generic_forbidden_honoured_when_opted_in() -> None:
    """The structured gpsoauth Error field opts in and keeps the old behavior."""
    assert (
        _is_invalid_aas_error_text(
            "server returned 403 forbidden", allow_http_generic=True
        )
        is True
    )


def test_http_generic_unauthorized_gated() -> None:
    """ "unauthorized" mirrors "forbidden": gated off by default, on when opted in."""
    assert (
        _is_invalid_aas_error_text("401 unauthorized", allow_http_generic=False)
        is False
    )
    assert (
        _is_invalid_aas_error_text("401 unauthorized", allow_http_generic=True) is True
    )


def test_gpsoauth_badauthentication_always_matches() -> None:
    """gpsoauth-specific tokens stay active regardless of the HTTP-generic gate."""
    assert _is_invalid_aas_error_text("BadAuthentication") is True
    assert (
        _is_invalid_aas_error_text("BadAuthentication", allow_http_generic=True) is True
    )


def test_gpsoauth_invalid_credential_vocabulary_always_matches() -> None:
    """ "invalid" + credential vocabulary is gpsoauth-specific, never gated."""
    assert _is_invalid_aas_error_text("invalid credential") is True
    assert _is_invalid_aas_error_text("needsbrowser") is True


# ---------------------------------------------------------------------------
# _perform_oauth_sync: the generic except-Exception call site (Z.222)
# ---------------------------------------------------------------------------


def _install_fake_gpsoauth(monkeypatch: pytest.MonkeyPatch, perform_oauth: Any) -> None:
    """Patch the module-local gpsoauth accessor with a fake providing perform_oauth."""
    fake = SimpleNamespace(perform_oauth=perform_oauth)
    monkeypatch.setattr(token_retrieval, "_gpsoauth", lambda: fake)


def test_generic_network_exception_does_not_discard_aas_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generic transport failure whose text contains "403 Forbidden" must NOT
    be reclassified as InvalidAasTokenError (which would discard the AAS token);
    it stays a plain RuntimeError so the caller treats it as retryable."""

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise OSError("WAF blocked request: HTTP 403 Forbidden (getaddrinfo)")

    _install_fake_gpsoauth(monkeypatch, _boom)

    with pytest.raises(RuntimeError) as excinfo:
        token_retrieval._perform_oauth_sync(
            "user@example.com",
            "aas_et/fake",
            "android_device_manager",
            False,
            android_id=0x1234,
        )
    assert not isinstance(excinfo.value, InvalidAasTokenError)


def test_genuine_badauthentication_still_discards_aas_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A structured gpsoauth Error=BadAuthentication response must still raise
    InvalidAasTokenError so the stale AAS token is discarded (no regression)."""

    def _bad_auth(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"Error": "BadAuthentication"}

    _install_fake_gpsoauth(monkeypatch, _bad_auth)

    with pytest.raises(InvalidAasTokenError):
        token_retrieval._perform_oauth_sync(
            "user@example.com",
            "aas_et/fake",
            "android_device_manager",
            False,
            android_id=0x1234,
        )


def test_generic_exception_with_gpsoauth_vocabulary_still_discards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even via the generic except-Exception path, a gpsoauth-specific token in
    the exception text still promotes to InvalidAasTokenError."""

    def _boom(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("gpsoauth rejected: BadAuthentication")

    _install_fake_gpsoauth(monkeypatch, _boom)

    with pytest.raises(InvalidAasTokenError):
        token_retrieval._perform_oauth_sync(
            "user@example.com",
            "aas_et/fake",
            "android_device_manager",
            False,
            android_id=0x1234,
        )


# ---------------------------------------------------------------------------
# _perform_oauth_sync: typed gpsoauth.AuthError structural channel
# ---------------------------------------------------------------------------


class _MockAuthError(Exception):
    """Stand-in for ``gpsoauth.exceptions.AuthError`` (gpsoauth is absent in CI)."""


def test_typed_gpsoauth_autherror_http_only_discards_aas_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typed gpsoauth AuthError with HTTP-generic-only text must discard the
    AAS token. gpsoauth may raise its typed AuthError instead of returning an
    Error dict; classifying it by type (mirroring the AAS exchange path) must
    promote it to InvalidAasTokenError even though the text matcher gates the
    HTTP-generic tokens off (``allow_http_generic=False``)."""

    monkeypatch.setattr(
        token_retrieval,
        "gpsoauth_exceptions",
        SimpleNamespace(AuthError=_MockAuthError),
    )

    def _typed_auth_error(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise _MockAuthError("HTTP 403 Forbidden")

    _install_fake_gpsoauth(monkeypatch, _typed_auth_error)

    with pytest.raises(InvalidAasTokenError):
        token_retrieval._perform_oauth_sync(
            "user@example.com",
            "aas_et/fake",
            "android_device_manager",
            False,
            android_id=0x1234,
        )


def test_typed_channel_absent_http_only_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the gpsoauth exceptions module is unavailable (dependency absent),
    the typed channel is skipped and an HTTP-generic-only exception text keeps
    the safe FIX-5 default: it stays a retryable RuntimeError and does not
    masquerade as an invalid AAS token."""

    monkeypatch.setattr(token_retrieval, "gpsoauth_exceptions", None)

    def _http_only(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("HTTP 403 Forbidden")

    _install_fake_gpsoauth(monkeypatch, _http_only)

    with pytest.raises(RuntimeError) as excinfo:
        token_retrieval._perform_oauth_sync(
            "user@example.com",
            "aas_et/fake",
            "android_device_manager",
            False,
            android_id=0x1234,
        )
    assert not isinstance(excinfo.value, InvalidAasTokenError)
