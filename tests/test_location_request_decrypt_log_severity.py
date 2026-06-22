# tests/test_location_request_decrypt_log_severity.py
"""Per-device decrypt-failure log severity in the FCM location callback.

A single device whose own server reports no longer decrypt -- e.g. a phone
powered off for days -- must be logged as a WARNING, not an ERROR: the callback
has no cross-device view, so the account-wide escalation decision (and its ERROR)
belongs to the coordinator's positive-proof-gated verdict
(``PollingOperations._resolve_cycle_decrypt_outcome``). Genuine session/auth
failures (``SpotApiEmptyResponseError``, ``SpotAuthPermanentError``) keep ERROR
severity because they themselves drive reauth. In every case ``ctx.error`` and the
propagation to the awaiting requester are unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    location_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    StaleOwnerKeyError,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
)
from custom_components.googlefindmy.SpotApi.spot_request import SpotAuthPermanentError

_CANONIC = "device-canonic-id"


def _patch_callback_imports(
    monkeypatch: pytest.MonkeyPatch, *, decrypt_exc: Exception
) -> None:
    """Stub the callback's lazy module imports so the decrypt step raises.

    The protobuf parse is reduced to an identity sentinel and the decrypt call is
    forced to raise ``decrypt_exc``; the real exception classes are wired in so the
    production ``except`` tuple and the severity ``isinstance`` check match.
    """

    async def _raise_decrypt(*_: Any, **__: Any) -> list[dict[str, Any]]:
        raise decrypt_exc

    decoder_module = SimpleNamespace(parse_device_update_protobuf=lambda _hex: object())
    decrypt_module = SimpleNamespace(
        async_decrypt_location_response_locations=_raise_decrypt,
        DecryptionError=DecryptionError,
        StaleOwnerKeyError=StaleOwnerKeyError,
    )
    eid_module = SimpleNamespace(SpotApiEmptyResponseError=SpotApiEmptyResponseError)

    monkeypatch.setattr(
        location_request, "_import_decoder_module", lambda: decoder_module
    )
    monkeypatch.setattr(
        location_request, "_import_decrypt_locations_module", lambda: decrypt_module
    )
    monkeypatch.setattr(location_request, "_import_eid_info_module", lambda: eid_module)


@pytest.mark.parametrize(
    ("decrypt_exc", "expected_level"),
    [
        # Per-device decrypt/key failure -> warning (the offline-phone case).
        (
            DecryptionError(
                "All own-report decryptions failed; the cached identity key no "
                "longer matches the server reports."
            ),
            logging.WARNING,
        ),
        # StaleOwnerKeyError is a DecryptionError subclass -> also warning.
        (StaleOwnerKeyError("tracker v1 < v2"), logging.WARNING),
        # Genuine session/auth failures keep ERROR severity (they drive reauth).
        (SpotApiEmptyResponseError("empty"), logging.ERROR),
        (SpotAuthPermanentError("invalid session"), logging.ERROR),
    ],
)
async def test_callback_decrypt_failure_log_severity(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    decrypt_exc: Exception,
    expected_level: int,
) -> None:
    """The callback logs per-device decrypt failures at the type-aware severity.

    Decrypt/key failures are warnings; session/auth failures stay errors. In all
    cases the error is surfaced via ``ctx.error`` so the requester still sees it.
    """
    _patch_callback_imports(monkeypatch, decrypt_exc=decrypt_exc)
    ctx = location_request._CallbackContext()
    callback = location_request._make_location_callback(
        name="Old Phone",
        canonic_device_id=_CANONIC,
        ctx=ctx,
        loop=asyncio.get_running_loop(),
        cache=MagicMock(),
    )

    with caplog.at_level(logging.WARNING):
        callback(_CANONIC, "00")
        await asyncio.wait_for(ctx.event.wait(), timeout=2.0)

    # The decrypt error is surfaced to the awaiting requester unchanged.
    assert ctx.error is decrypt_exc

    severity_records = [
        rec
        for rec in caplog.records
        if "Failed to process location data" in rec.message
    ]
    assert len(severity_records) == 1
    assert severity_records[0].levelno == expected_level
