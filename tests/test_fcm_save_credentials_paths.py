# tests/test_fcm_save_credentials_paths.py
"""Where ``_async_save_credentials_for_entry`` stores a registration.

The receiver writes a renewed registration to the entry (best-effort) and to
the entry's token cache. Without a known cache it looks for one on a
coordinator of the same entry, and without either it keeps the registration
pending until a cache is attached. A failed cache write keeps it pending too,
so the next attach retries instead of losing it.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from tests.helpers.config_entries_stub import make_config_entry

ENTRY_ID = "entry-save"


class _Cache:
    """Token cache double whose ``set`` records or raises."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sets: list[tuple[str, Any]] = []

    async def set(self, name: str, value: Any) -> None:
        if self.error is not None:
            raise self.error
        self.sets.append((name, value))


def _coordinator(entry_id: str | None, cache: Any) -> Any:
    entry = None if entry_id is None else make_config_entry(entry_id=entry_id)
    return SimpleNamespace(config_entry=entry, cache=cache)


def _receiver(creds: Any) -> FcmReceiverHA:
    receiver = FcmReceiverHA()
    receiver.creds[ENTRY_ID] = creds
    return receiver


@pytest.mark.asyncio
async def test_a_failed_write_to_the_known_cache_keeps_the_registration_pending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The failure is logged without its text and the pending value stays."""

    creds = {"gcm": {"token": "t"}}
    receiver = _receiver(creds)
    receiver._entry_caches[ENTRY_ID] = _Cache(RuntimeError("cache-text"))  # type: ignore[assignment]
    receiver._pending_creds[ENTRY_ID] = creds

    with caplog.at_level(logging.DEBUG):
        await receiver._async_save_credentials_for_entry(ENTRY_ID)

    assert receiver._pending_creds[ENTRY_ID] is creds
    messages = [r.getMessage() for r in caplog.records]
    assert any(ENTRY_ID in m and "entry cache" in m for m in messages)
    assert not any("cache-text" in m for m in messages)


@pytest.mark.asyncio
async def test_the_cache_of_the_entrys_coordinator_is_found_and_kept() -> None:
    """Coordinators of other entries, or without entry or cache, are skipped."""

    creds = {"gcm": {"token": "t"}}
    receiver = _receiver(creds)
    found = _Cache()
    receiver.coordinators = [
        _coordinator(None, _Cache()),
        _coordinator("another-entry", _Cache()),
        _coordinator(ENTRY_ID, None),
        _coordinator(ENTRY_ID, found),
    ]
    receiver._pending_creds[ENTRY_ID] = creds

    await receiver._async_save_credentials_for_entry(ENTRY_ID)

    assert found.sets == [("fcm_credentials", creds)]
    assert receiver._entry_caches[ENTRY_ID] is found
    assert ENTRY_ID not in receiver._pending_creds


@pytest.mark.asyncio
async def test_a_failed_write_to_a_coordinator_cache_keeps_the_registration_pending(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The coordinator's cache is remembered even when the first write fails."""

    creds = {"gcm": {"token": "t"}}
    receiver = _receiver(creds)
    failing = _Cache(RuntimeError("cache-text"))
    receiver.coordinators = [_coordinator(ENTRY_ID, failing)]
    receiver._pending_creds[ENTRY_ID] = creds

    with caplog.at_level(logging.DEBUG):
        await receiver._async_save_credentials_for_entry(ENTRY_ID)

    assert receiver._entry_caches[ENTRY_ID] is failing
    assert receiver._pending_creds[ENTRY_ID] is creds
    assert not any("cache-text" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_without_a_cache_the_registration_waits_for_one() -> None:
    """A dict is kept as pending, the same object is not replaced, anything else is ``None``."""

    creds = {"gcm": {"token": "t"}}
    receiver = _receiver(creds)

    await receiver._async_save_credentials_for_entry(ENTRY_ID)
    assert receiver._pending_creds[ENTRY_ID] is creds

    # The same object is already pending; the slot is left as it is.
    await receiver._async_save_credentials_for_entry(ENTRY_ID)
    assert receiver._pending_creds[ENTRY_ID] is creds

    receiver.creds[ENTRY_ID] = "not-a-registration"  # type: ignore[assignment]
    await receiver._async_save_credentials_for_entry(ENTRY_ID)
    assert receiver._pending_creds[ENTRY_ID] is None
