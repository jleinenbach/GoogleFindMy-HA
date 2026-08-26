# tests/test_location_request_r6_name_sweep.py
"""R6 device-name redaction sweep for the remaining locate-request log sites.

PR #1193 redacted the raw device display name in the single ``not fcm_token``
diagnostic. This sweep completes the same R6 class (AGENTS.md Section 5): every
remaining *user-facing* (``>= WARNING``) record in ``location_request`` must state
the failure and its cause without the raw device name, and expose the name only
at DEBUG. The mirror of the established Count@WARNING / Name@DEBUG pattern.

Each test is a mutation gate: the sentinel display name is passed *only* via
``name=`` (never inside an exception's message), so the sole way it can appear in
a ``>= WARNING`` record is the pre-fix ``... for %s`` argument. It is RED on the
old code (name at WARNING/ERROR) and GREEN with the redaction.

Two groups are covered:

* ``get_location_data_for_device`` -- the FCM-registration failure, the Nova
  request exception ladder (rate-limit / HTTP / auth / network / generic), and the
  ``finally`` FCM-unregister failure.
* ``_make_location_callback`` -- the parse failure, the per-device decrypt
  failure (warning and error severities), and the empty-after-decrypt path.

The flows are exercised through the same stubbing style the sibling suites use
(no ``asyncio.run`` in tests).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    location_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    OwnerKeyLookupTransientError,
    StaleOwnerKeyError,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    NovaAuthError,
    NovaHTTPError,
    NovaRateLimitError,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
)
from custom_components.googlefindmy.SpotApi.spot_request import SpotAuthPermanentError

pytestmark = pytest.mark.asyncio

_SENTINEL_NAME = "Jens-Privat-Tracker-SENTINEL-7Q"
_CANONIC = "device-canonic-id"


class _FakeTokenCache:
    """Minimal entry-scoped cache stub for the locate flow."""

    def __init__(self, label: str = "entry-r6-sweep") -> None:
        self.entry_id = label
        self.values: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        return self.values.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.values[key] = value

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.values[key] = value


class _TokenReceiver:
    """Receiver whose registration yields a token (the locate fails downstream)."""

    def __init__(self, *, unregister_exc: Exception | None = None) -> None:
        self._unregister_exc = unregister_exc

    async def async_register_for_location_updates(
        self, device_id: str, callback: Callable[[str, str], None]
    ) -> str:
        return "fcm-token"

    async def async_unregister_for_location_updates(self, device_id: str) -> None:
        if self._unregister_exc is not None:
            raise self._unregister_exc


def _assert_name_only_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """The sentinel name is absent from every ``>= WARNING`` record, present at DEBUG."""
    user_facing = [rec for rec in caplog.records if rec.levelno >= logging.WARNING]
    assert user_facing, "expected at least one user-facing (>= WARNING) record"
    for rec in user_facing:
        assert _SENTINEL_NAME not in rec.getMessage(), (
            f"R6 leak: device name in {logging.getLevelName(rec.levelno)} record: "
            f"{rec.getMessage()!r}"
        )
    debug_text = " ".join(
        rec.getMessage() for rec in caplog.records if rec.levelno == logging.DEBUG
    )
    assert _SENTINEL_NAME in debug_text, "device name must stay available at DEBUG"


# ---------------------------------------------------------------------------
# Group 1: get_location_data_for_device
# ---------------------------------------------------------------------------
def _wire_nova_raises(monkeypatch: pytest.MonkeyPatch, *, nova_exc: Exception) -> None:
    """Stub the flow so ``async_nova_request`` raises ``nova_exc``.

    Registration and request build succeed, so control reaches the Nova-request
    exception ladder; the FCM callback is never fired.
    """
    receiver = _TokenReceiver()
    monkeypatch.setattr(location_request, "_FCM_ReceiverGetter", lambda *_a: receiver)

    def _fake_make_callback(**_: Any) -> Callable[[str, str], None]:
        return lambda *_a: None

    monkeypatch.setattr(
        location_request, "_make_location_callback", _fake_make_callback
    )
    monkeypatch.setattr(
        location_request, "create_location_request", lambda *a, **k: "deadbeef"
    )

    async def _raise_nova(*_a: object, **_k: object) -> bytes:
        raise nova_exc

    monkeypatch.setattr(location_request, "async_nova_request", _raise_nova)


@pytest.mark.parametrize(
    "nova_exc",
    [
        # Note: no exception message contains the sentinel name, so any sentinel
        # in a >= WARNING record can only come from the pre-fix ``name`` arg.
        NovaRateLimitError("upstream quota exceeded"),
        NovaHTTPError(503, "backend unavailable"),
        aiohttp.ClientError("connection reset"),
        RuntimeError("unclassified nova failure"),
    ],
)
async def test_nova_request_ladder_redacts_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    nova_exc: Exception,
) -> None:
    """RED pre-fix: rate-limit / HTTP / network / generic Nova failures logged the
    raw device name at WARNING/ERROR. The name must move to DEBUG only."""
    _wire_nova_raises(monkeypatch, nova_exc=nova_exc)
    caplog.set_level(logging.DEBUG, logger=location_request.__name__)

    result = await location_request.get_location_data_for_device(
        canonic_device_id=_CANONIC,
        name=_SENTINEL_NAME,
        session=None,
        username="user@example.com",
        cache=_FakeTokenCache(),
    )
    assert result == []
    _assert_name_only_at_debug(caplog)


async def test_nova_auth_error_redacts_name_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The auth path logs a WARNING (then re-raises for the reauth flow); the raw
    device name must not ride along in that user-facing record."""
    _wire_nova_raises(monkeypatch, nova_exc=NovaAuthError(401, "token rejected"))
    caplog.set_level(logging.DEBUG, logger=location_request.__name__)

    with pytest.raises(NovaAuthError):
        await location_request.get_location_data_for_device(
            canonic_device_id=_CANONIC,
            name=_SENTINEL_NAME,
            session=None,
            username="user@example.com",
            cache=_FakeTokenCache(),
        )
    _assert_name_only_at_debug(caplog)
    # Pin the wording, not just the redaction: without this the branch could
    # say anything at all about a 401 and stay green, which is exactly how the
    # non-credential counterpart below would lose its only counter-test.
    assert "Authentication error while requesting location" in caplog.text


async def test_a_client_error_is_not_logged_as_an_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NovaAuthError covers every non-retryable 4xx, so this WARNING must not
    pre-empt api.py's verdict by calling a 404 an authentication problem. The
    re-raise is unchanged: this layer still does not classify, it just stops
    claiming to."""
    _wire_nova_raises(monkeypatch, nova_exc=NovaAuthError(404, "gone"))
    caplog.set_level(logging.DEBUG, logger=location_request.__name__)

    with pytest.raises(NovaAuthError):
        await location_request.get_location_data_for_device(
            canonic_device_id=_CANONIC,
            name=_SENTINEL_NAME,
            session=None,
            username="user@example.com",
            cache=_FakeTokenCache(),
        )
    assert "Client error (HTTP 404) while requesting location" in caplog.text
    assert "Authentication error" not in caplog.text
    _assert_name_only_at_debug(caplog)


async def test_fcm_registration_failure_redacts_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED pre-fix: a failing FCM registration logged the raw device name at ERROR."""

    class _RaisingReceiver:
        async def async_register_for_location_updates(
            self, device_id: str, callback: Callable[[str, str], None]
        ) -> str:
            raise RuntimeError("registration backend down")

        async def async_unregister_for_location_updates(self, device_id: str) -> None:
            return None

    monkeypatch.setattr(
        location_request, "_FCM_ReceiverGetter", lambda *_a: _RaisingReceiver()
    )
    monkeypatch.setattr(
        location_request,
        "_make_location_callback",
        lambda **_k: lambda *_a: None,
    )
    caplog.set_level(logging.DEBUG, logger=location_request.__name__)

    result = await location_request.get_location_data_for_device(
        canonic_device_id=_CANONIC,
        name=_SENTINEL_NAME,
        session=None,
        username="user@example.com",
        cache=_FakeTokenCache(),
    )
    assert result == []
    _assert_name_only_at_debug(caplog)


async def test_unregister_failure_in_finally_redacts_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED pre-fix: a failing FCM unregister in ``finally`` logged the raw name.

    ``create_location_request`` is forced to raise so the flow lands in the broad
    surfacing handler (already R6-clean since PR #1129) and then runs ``finally``,
    where the raising unregister exercises the swept WARNING.
    """
    receiver = _TokenReceiver(unregister_exc=RuntimeError("unregister backend down"))
    monkeypatch.setattr(location_request, "_FCM_ReceiverGetter", lambda *_a: receiver)
    monkeypatch.setattr(
        location_request,
        "_make_location_callback",
        lambda **_k: lambda *_a: None,
    )

    def _boom(*_a: object, **_k: object) -> str:
        raise RuntimeError("payload build failed")

    monkeypatch.setattr(location_request, "create_location_request", _boom)
    caplog.set_level(logging.DEBUG, logger=location_request.__name__)

    result = await location_request.get_location_data_for_device(
        canonic_device_id=_CANONIC,
        name=_SENTINEL_NAME,
        session=None,
        username="user@example.com",
        cache=_FakeTokenCache(),
    )
    assert result == []
    assert any(
        "Error during FCM unregister" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    ), "expected the unregister WARNING to be exercised"
    _assert_name_only_at_debug(caplog)


# ---------------------------------------------------------------------------
# Group 2: _make_location_callback
# ---------------------------------------------------------------------------
def _patch_callback_imports(
    monkeypatch: pytest.MonkeyPatch,
    *,
    parse: Callable[[str], Any] | None = None,
    decrypt: Callable[..., Any] | None = None,
) -> None:
    """Stub the callback's lazy module imports with configurable parse/decrypt.

    Defaults: parse returns an identity sentinel, decrypt returns an empty list.
    The real exception classes are wired so the production ``except`` tuple and the
    severity ``isinstance`` check behave identically.
    """

    async def _default_decrypt(*_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    decoder_module = SimpleNamespace(
        parse_device_update_protobuf=parse or (lambda _hex: object())
    )
    decrypt_module = SimpleNamespace(
        async_decrypt_location_response_locations=decrypt or _default_decrypt,
        DecryptionError=DecryptionError,
        StaleOwnerKeyError=StaleOwnerKeyError,
        OwnerKeyLookupTransientError=OwnerKeyLookupTransientError,
    )
    eid_module = SimpleNamespace(SpotApiEmptyResponseError=SpotApiEmptyResponseError)

    monkeypatch.setattr(
        location_request, "_import_decoder_module", lambda: decoder_module
    )
    monkeypatch.setattr(
        location_request, "_import_decrypt_locations_module", lambda: decrypt_module
    )
    monkeypatch.setattr(location_request, "_import_eid_info_module", lambda: eid_module)


async def _fire_callback(caplog: pytest.LogCaptureFixture) -> None:
    """Build the real callback with the sentinel name and drive it once."""
    caplog.set_level(logging.DEBUG, logger=location_request.__name__)
    ctx = location_request._CallbackContext()
    callback = location_request._make_location_callback(
        name=_SENTINEL_NAME,
        canonic_device_id=_CANONIC,
        ctx=ctx,
        loop=asyncio.get_running_loop(),
        cache=MagicMock(),
    )
    callback(_CANONIC, "00")
    await asyncio.wait_for(ctx.event.wait(), timeout=2.0)


async def test_callback_parse_failure_redacts_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED pre-fix: a protobuf parse failure logged the raw name at ERROR."""

    def _raise_parse(_hex: str) -> Any:
        raise ValueError("malformed protobuf")

    _patch_callback_imports(monkeypatch, parse=_raise_parse)
    await _fire_callback(caplog)
    _assert_name_only_at_debug(caplog)


@pytest.mark.parametrize(
    "decrypt_exc",
    [
        DecryptionError("own reports predate current identity key"),  # WARNING
        SpotAuthPermanentError("invalid session"),  # ERROR
    ],
)
async def test_callback_decrypt_failure_redacts_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    decrypt_exc: Exception,
) -> None:
    """RED pre-fix: a per-device decrypt failure logged the raw name (warning and
    error severities alike)."""

    async def _raise_decrypt(*_: Any, **__: Any) -> list[dict[str, Any]]:
        raise decrypt_exc

    _patch_callback_imports(monkeypatch, decrypt=_raise_decrypt)
    await _fire_callback(caplog)
    _assert_name_only_at_debug(caplog)


async def test_callback_empty_after_decrypt_redacts_name(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED pre-fix: the empty-after-decrypt path logged the raw name at WARNING."""
    _patch_callback_imports(monkeypatch)  # default decrypt returns []
    await _fire_callback(caplog)
    assert any(
        "No location data found after decryption" in rec.getMessage()
        for rec in caplog.records
        if rec.levelno == logging.WARNING
    ), "expected the empty-after-decrypt WARNING to be exercised"
    _assert_name_only_at_debug(caplog)
