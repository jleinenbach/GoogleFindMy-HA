# tests/test_location_request_transient_owner_key.py
"""RED contract: a transient owner-key lookup failure must survive the decrypt callback.

``decrypt_locations.async_decrypt_location_response_locations`` can raise
``OwnerKeyLookupTransientError`` (base ``Exception``, NOT a ``DecryptionError``
subclass) when the per-tracker owner key cannot be resolved against a transient
server condition. The locate callback's inner decrypt body
(``location_request._make_location_callback`` -> ``_decrypt_and_store``) catches a
fixed tuple of ``(StaleOwnerKeyError, DecryptionError, SpotApiEmptyResponseError,
SpotAuthPermanentError, InvalidTag)`` and otherwise drops into a broad
``except Exception`` that wipes ``ctx.data`` to ``[]``, logs at ERROR, and leaves
``ctx.error`` as ``None``. The transient type is therefore swallowed instead of being
surfaced (as a DEBUG-level transient) to the awaiting coroutine through ``ctx.error``.

These tests pin the intended behavior: the transient type (and its
``_OwnerKeyRederiveRequired`` subclass) must reach ``ctx.error`` and be logged at
DEBUG (not ERROR). They are RED until the AP3 classification rework lands.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations,
    location_request,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    OwnerKeyLookupTransientError,
    _OwnerKeyRederiveRequired,
)

pytestmark = pytest.mark.asyncio

_CANONIC_ID = "device-abc"


class _FakeTokenCache:
    """Minimal entry-scoped cache stub for the locate callback."""

    def __init__(self, label: str = "entry-transient") -> None:
        self.entry_id = label
        self.values: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        return self.values.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.values[key] = value


def _wire_callback(
    monkeypatch: pytest.MonkeyPatch, *, decrypt_exc: Exception
) -> location_request._CallbackContext:
    """Stub the decoder + decrypt steps so the callback reaches ``_decrypt_and_store``.

    The callback resolves ``parse_device_update_protobuf`` from the decoder module
    and ``async_decrypt_location_response_locations`` from the decrypt module via
    ``getattr`` on the lazily imported module objects, so we patch the real module
    attributes (the names ``_decrypt_and_store`` actually resolves).
    """
    decoder_module = location_request._import_decoder_module()
    monkeypatch.setattr(
        decoder_module,
        "parse_device_update_protobuf",
        lambda _hex: MagicMock(),
    )

    async def _raise_decrypt(*_a: object, **_k: object) -> list[dict[str, Any]]:
        raise decrypt_exc

    monkeypatch.setattr(
        decrypt_locations,
        "async_decrypt_location_response_locations",
        _raise_decrypt,
    )
    return location_request._CallbackContext()


async def _drive_callback(
    ctx: location_request._CallbackContext,
) -> None:
    """Build the real locate callback, invoke it, and await the decrypt handoff."""
    loop = asyncio.get_running_loop()
    callback = location_request._make_location_callback(
        name="Tracker",
        canonic_device_id=_CANONIC_ID,
        ctx=ctx,
        loop=loop,
        cache=_FakeTokenCache(),
        cache_provider=None,
    )
    # The callback runs in the receiver's worker thread and hands the async decrypt
    # off to the loop via run_coroutine_threadsafe; calling it from the running loop
    # schedules the coroutine, which we then await via the context event.
    callback(_CANONIC_ID, "deadbeef")
    await asyncio.wait_for(ctx.event.wait(), timeout=5.0)


async def test_transient_owner_key_error_reaches_ctx_error_at_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """RED: a transient owner-key failure is surfaced via ctx.error at DEBUG severity.

    Today the broad ``except Exception`` swallows it: ``ctx.error is None``,
    ``ctx.data == []`` and the record is logged at ERROR.
    """
    transient = OwnerKeyLookupTransientError("owner key lookup timed out")
    ctx = _wire_callback(monkeypatch, decrypt_exc=transient)

    caplog.set_level(logging.DEBUG, logger=location_request.__name__)

    await _drive_callback(ctx)

    # SOLL (RED today): the transient type survives to the awaiting coroutine.
    assert isinstance(ctx.error, OwnerKeyLookupTransientError)

    # SOLL (RED today): no ERROR record for a transient; severity is DEBUG.
    error_records = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert not error_records, (
        "transient owner-key failure must not be logged at ERROR; "
        f"got {[r.getMessage() for r in error_records]}"
    )


async def test_owner_key_rederive_required_also_reaches_ctx_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (fail-safe vector): the transient subclass is surfaced via ctx.error too.

    ``_OwnerKeyRederiveRequired`` subclasses the transient base, so it must be caught
    by the same transient branch and land in ``ctx.error`` (today it is swallowed by
    the broad ``except Exception`` like its base, leaving ``ctx.error is None``).
    """
    transient = _OwnerKeyRederiveRequired("owner key must be re-derived")
    ctx = _wire_callback(monkeypatch, decrypt_exc=transient)

    await _drive_callback(ctx)

    assert isinstance(ctx.error, OwnerKeyLookupTransientError)
    assert isinstance(ctx.error, _OwnerKeyRederiveRequired)
