"""End-to-end regression for the orphaned-subscription unregister.

PR #1169 shipped ``gcm_unregister`` plus a capture of ``old_app_id`` inside
``reregister_keeping_identity``, and every unit test passed — yet the fix was a
production no-op. The unit tests constructed ``FcmRegister`` with ``app_id``
already present in ``credentials["gcm"]``, a precondition the *real* caller never
establishes: the FCM-renew button (and a restart) first runs
``FcmReceiverHA._invalidate_fcm_tokens``, which cleared the ``app_id`` slot
BEFORE ``reregister_keeping_identity`` ran, so ``old_app_id`` was always ``None``
and the unregister guard never fired.

These tests exercise the genuine chain — ``_invalidate_fcm_tokens`` →
``reregister_keeping_identity`` → ``gcm_unregister`` — with the exact credential
object the invalidation produces, so a regression to either half of the fix
(dropping ``app_id`` without rescuing it, or reading only ``app_id`` at capture)
fails here. It is the coverage the pure-unit view structurally could not give:
100% diff coverage of a method under an unrealistic precondition proves nothing
about the integration path that establishes that precondition.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.Auth.firebase_messaging.const import (
    GCM_REGISTER3_URL,
)
from custom_components.googlefindmy.Auth.firebase_messaging.fcmregister import (
    FcmRegister,
    FcmRegisterConfig,
)

# Real-shaped values: an int-convertible android_id/security_token (so the
# identity survives invalidation) and two distinct webpush subtypes.
_ANDROID_ID = 1234567890123456
_SECURITY_TOKEN = "9876543210987654"
_OLD_APP_ID = "wp:com.google.android.apps.adm#0d7d2715-75d9-47a0-88ec-32e9d91e88dd"
_NEW_APP_ID = "wp:com.google.android.apps.adm#f23ca93d-683d-4fb9-aaee-fdbd82dd079a"


@dataclass
class _FakeResponse:
    status: int
    text_value: str
    headers: dict[str, str]

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def text(self) -> str:
        return self.text_value


class _RecordingSession:
    """aiohttp session stub: records every post, replays queued responses.

    Every network step of ``reregister_keeping_identity`` is stubbed on the
    ``FcmRegister`` instance EXCEPT the unregister POST, so any recorded call is
    the orphan unregister under test.
    """

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    def post(
        self, *, url: str, headers: dict[str, str], data: dict[str, Any], timeout: Any
    ) -> _FakeResponse:
        self.calls.append({"url": url, "data": dict(data), "headers": dict(headers)})
        if not self._responses:
            raise AssertionError("No more responses configured")
        return self._responses.pop(0)


def _full_creds() -> dict[str, Any]:
    """Credentials exactly as they exist for a live, fully-registered entry."""
    return {
        "gcm": {
            "android_id": _ANDROID_ID,
            "security_token": _SECURITY_TOKEN,
            "token": "old-gcm-token",
            "app_id": _OLD_APP_ID,
        },
        "fcm": {"registration": {"token": "old-fcm-token"}},
        "keys": {"private": "x"},
    }


def _build_register_on(
    credentials: dict[str, Any], session: _RecordingSession
) -> FcmRegister:
    """Wire an ``FcmRegister`` on the given (invalidated) creds with every
    network step stubbed except the unregister POST."""
    config = FcmRegisterConfig(
        project_id="proj",
        app_id="app",
        api_key="key",
        messaging_sender_id="1234567890123",
        bundle_id="bundle",
    )
    register = FcmRegister(config, credentials=credentials, http_client_session=session)

    async def fake_check_in(self, android_id=None, security_token=None):  # type: ignore[no-untyped-def]
        return {"androidId": android_id, "securityToken": security_token}

    async def fake_gcm_register(self, gcm_response, retries=5):  # type: ignore[no-untyped-def]
        return {
            "token": "new-gcm-token",
            "app_id": _NEW_APP_ID,
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
async def test_invalidate_then_reregister_unregisters_orphan() -> None:
    """The genuine button/restart chain must unregister the orphan.

    Reproduces exactly what production does: full creds → the real
    ``_invalidate_fcm_tokens`` → hand the resulting partial creds to
    ``reregister_keeping_identity`` → assert one ``delete=true`` POST carrying the
    OLD subtype. Pre-fix this recorded ZERO calls (``old_app_id`` was ``None``),
    which is the exact production no-op the live button test exposed.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-e2e-orphan"
    receiver.creds[entry_id] = _full_creds()

    # 1. The real invalidation the FCM-renew button triggers.
    await receiver._invalidate_fcm_tokens(entry_id)
    invalidated = receiver.creds[entry_id]

    # Sanity: this is the partial-creds state that drives reregister, and the
    # superseded subtype survived the invalidation (the fix under test).
    assert "fcm" not in invalidated
    assert "gcm" in invalidated
    assert "app_id" not in invalidated["gcm"]
    assert invalidated["gcm"]["orphan_app_id"] == _OLD_APP_ID

    # 2. Feed the EXACT invalidated creds to FcmRegister, as the receiver does.
    session = _RecordingSession(
        [_FakeResponse(200, f"deleted={_OLD_APP_ID}", {"Content-Type": "text/plain"})]
    )
    register = _build_register_on(invalidated, session)

    result = await register.reregister_keeping_identity()

    # 3. Re-registration produced fresh creds ...
    assert result["gcm"]["app_id"] == _NEW_APP_ID
    # ... and the orphan MUST have been unregistered (the bug: 0 calls).
    assert len(session.calls) == 1, "orphan unregister did not fire"
    call = session.calls[0]
    assert call["url"] == GCM_REGISTER3_URL
    assert call["data"]["delete"] == "true"
    assert call["data"]["X-subtype"] == _OLD_APP_ID
    assert call["data"]["device"] == _ANDROID_ID
    assert call["headers"]["Authorization"] == (
        f"AidLogin {_ANDROID_ID}:{_SECURITY_TOKEN}"
    )

    # 4. The rescue slot must not leak into the freshly persisted credentials.
    assert "orphan_app_id" not in result["gcm"]


@pytest.mark.asyncio
async def test_invalidate_then_reregister_survives_unregister_failure() -> None:
    """A best-effort unregister failure on the real chain never costs the fresh
    registration: the caller still returns valid new creds, attempted once."""
    receiver = FcmReceiverHA()
    entry_id = "entry-e2e-orphan-fail"
    receiver.creds[entry_id] = _full_creds()
    await receiver._invalidate_fcm_tokens(entry_id)
    invalidated = receiver.creds[entry_id]

    class _RaisingSession(_RecordingSession):
        def post(self, **kwargs: Any) -> Any:  # type: ignore[override]
            self.calls.append({"url": kwargs["url"], "data": dict(kwargs["data"])})
            raise OSError("network down")

    session = _RaisingSession([])
    register = _build_register_on(invalidated, session)

    result = await register.reregister_keeping_identity()

    assert result["gcm"]["app_id"] == _NEW_APP_ID
    assert result["fcm"]["registration"]["token"] == "new-fcm-token"
    assert len(session.calls) == 1  # attempted exactly once, no retry
