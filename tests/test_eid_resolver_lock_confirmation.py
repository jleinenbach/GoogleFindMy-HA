# tests/test_eid_resolver_lock_confirmation.py
"""Lock confirmation self-healing for the resolver."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.eid_resolver import (
    LOCK_CONFIRMATION_TTL_SECONDS,
    EIDGenerationLock,
    EIDMatch,
    EidVariant,
    GoogleFindMyEIDResolver,
)


def _fake_hass() -> SimpleNamespace:
    """Return a minimal hass stand-in."""

    return SimpleNamespace(async_create_task=lambda coro, name=None: asyncio.create_task(coro))


@pytest.mark.asyncio
async def test_lock_confirmation_updates_on_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG C: confirmation timestamps must update every time a lock hits."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._ensure_cache_defaults()

    device_id = "device-id"
    eid_bytes = b"\x99" * 20
    resolver._lookup[eid_bytes] = [EIDMatch(
        device_id=device_id,
        config_entry_id="entry-id",
        canonical_id="canonical-id",
        time_offset=0,
        is_reversed=False,
    )]
    resolver._lookup_metadata[eid_bytes] = {
        "timestamp_basis": "pair_date",
        "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
    }
    resolver._locks[device_id] = EIDGenerationLock(
        device_id=device_id,
        canonical_id="canonical-id",
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=len(eid_bytes),
        created_at=75,
    )
    resolver._last_lock_confirmation[device_id] = 50

    now = 1_000
    monkeypatch.setattr(time, "time", lambda: float(now))

    match = resolver.resolve_eid(eid_bytes)

    assert match is not None
    assert resolver._last_lock_confirmation[device_id] == now, (
        "BUG C: last_lock_confirmation was not updated on match. "
        "Update resolve_eid() to set _last_lock_confirmation[device_id] on EVERY successful match."
    )
    assert resolver._known_timebases[device_id] == "pair_date"


@pytest.mark.asyncio
async def test_purge_stale_locks_keeps_recently_confirmed_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recently confirmed lock must not be purged by confirmation TTL."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._ensure_cache_defaults()

    device_id = "device-id"
    eid_bytes = b"\x01" * 20
    resolver._lookup[eid_bytes] = [EIDMatch(
        device_id=device_id,
        config_entry_id="entry-id",
        canonical_id="canonical-id",
        time_offset=0,
        is_reversed=False,
    )]
    resolver._lookup_metadata[eid_bytes] = {
        "timestamp_basis": "pair_date",
        "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
    }
    resolver._locks[device_id] = EIDGenerationLock(
        device_id=device_id,
        canonical_id="canonical-id",
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=len(eid_bytes),
        rotation_timestamp=100,
        created_at=50,
    )
    initial_confirmation = 10
    resolver._last_lock_confirmation[device_id] = initial_confirmation

    now = 1_000
    monkeypatch.setattr(time, "time", lambda: float(now))
    match = resolver.resolve_eid(eid_bytes)
    assert match is not None

    purge_time = now + 120  # Well within the confirmation TTL window
    resolver._purge_stale_locks(now=purge_time)

    assert device_id in resolver._locks, (
        "FAIL: active lock was purged despite recent confirmation. "
        "_purge_stale_locks must respect _last_lock_confirmation timestamps."
    )


@pytest.mark.asyncio
async def test_stale_lock_removed_by_confirmation_ttl() -> None:
    """BUG C: stale locks must clear when they are no longer confirmed."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    async def _async_save(payload: object) -> None:
        scheduled.append("payload")

    def _record_task(coro: asyncio.Future, name: str | None = None) -> asyncio.Task:
        scheduled.append(name or "save")
        return asyncio.create_task(coro)

    resolver.hass = SimpleNamespace(
        async_create_task=_record_task, async_create_background_task=_record_task
    )
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._ensure_cache_defaults()
    resolver._store = SimpleNamespace(async_save=_async_save)

    device_id = "stale-device"
    eid_length = 20
    lock = EIDGenerationLock(
        device_id=device_id,
        canonical_id="canonical-id",
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=True,
        eid_length=eid_length,
        created_at=1,
    )
    resolver._locks[device_id] = lock
    resolver._persisted_locks[device_id] = lock
    resolver._known_offsets[(device_id, "pair_date")] = 4
    resolver._known_advertisement_reversed[device_id] = True
    resolver._known_timebases[device_id] = "pair_date"

    last_confirmation = 100
    resolver._last_lock_confirmation[device_id] = last_confirmation
    now = last_confirmation + LOCK_CONFIRMATION_TTL_SECONDS + 1

    scheduled: list[str] = []

    resolver._purge_stale_locks(now=now)
    await asyncio.sleep(0)

    assert device_id not in resolver._locks, (
        "BUG C: stale lock not removed by confirmation TTL. "
        "Add LOCK_CONFIRMATION_TTL_SECONDS logic to purge and remove related caches."
    )
    assert device_id not in resolver._persisted_locks
    assert not any(key[0] == device_id for key in resolver._known_offsets)
    assert device_id not in resolver._known_advertisement_reversed
    assert device_id not in resolver._known_timebases
    assert device_id not in resolver._last_lock_confirmation
    assert scheduled
