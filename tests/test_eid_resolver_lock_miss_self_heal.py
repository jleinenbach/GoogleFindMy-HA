# tests/test_eid_resolver_lock_miss_self_heal.py
"""Lock miss counters drive early self-healing for resolver locks."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    LOCK_MISS_THRESHOLD,
    ROTATION_PERIOD,
    EIDGenerationLock,
    EIDMatch,
    EidVariant,
    GoogleFindMyEIDResolver,
)


def _fake_hass() -> SimpleNamespace:
    """Return a minimal hass stand-in."""

    return SimpleNamespace(
        async_create_task=lambda coro, name=None: asyncio.create_task(coro),
        async_create_background_task=lambda coro, name=None: asyncio.create_task(coro),
    )


@pytest.mark.asyncio
async def test_lock_miss_counter_triggers_self_heal(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG D: consecutive unconfirmed refresh cycles should clear a stale lock."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._ensure_cache_defaults()
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._lookup = {}
    resolver._lookup_metadata = {}

    scheduled: list[str] = []

    async def _async_save(payload: object) -> None:
        scheduled.append("payload")

    resolver._store = SimpleNamespace(async_save=_async_save)

    identity = DeviceIdentity(
        registry_id="device-id",
        canonical_id="canonical-id",
        identity_key=b"key-bytes",
        config_entry_id="entry-id",
        pair_date=1_000,
    )
    resolver._locks[identity.registry_id] = EIDGenerationLock(
        device_id=identity.registry_id,
        canonical_id=identity.canonical_id,
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=32,
        rotation_timestamp=5_000,
        created_at=1,
    )
    resolver._persisted_locks[identity.registry_id] = resolver._locks[identity.registry_id]
    resolver._last_lock_confirmation[identity.registry_id] = 0

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    def _generate_variant(
        _self: GoogleFindMyEIDResolver,
        key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        return f"eid-{variant.value}-{time_counter}".encode()

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", _generate_variant)

    current_time = 0

    def _tick_time() -> float:
        return float(current_time)

    monkeypatch.setattr(time, "time", _tick_time)

    for idx in range(LOCK_MISS_THRESHOLD):
        current_time = (idx + 1) * ROTATION_PERIOD
        await resolver._refresh_cache()
    await asyncio.sleep(0)

    assert identity.registry_id not in resolver._locks, (
        "BUG D: lock miss counter did not trigger self-healing. "
        "Implement _lock_miss_counts increments per locked device during _refresh_cache() and clear lock state after threshold."
    )
    assert identity.registry_id not in resolver._persisted_locks
    assert identity.registry_id not in resolver._lock_miss_counts
    assert identity.registry_id not in resolver._known_timebases
    assert scheduled


def test_miss_counter_resets_on_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """BUG D: a successful match must reset the miss counter."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}
    resolver._ensure_cache_defaults()
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None

    scheduled: list[str] = []

    async def _async_save(payload: object) -> None:
        scheduled.append("payload")

    resolver._store = SimpleNamespace(async_save=_async_save)

    device_id = "device-id"
    eid_bytes = b"\xAA" * 20
    resolver._lookup[eid_bytes] = EIDMatch(
        device_id=device_id,
        config_entry_id="entry-id",
        canonical_id="canonical-id",
        time_offset=0,
        is_reversed=False,
    )
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
        rotation_timestamp=5_000,
        created_at=1,
    )
    resolver._lock_miss_counts[device_id] = LOCK_MISS_THRESHOLD - 1

    now = 10_000
    monkeypatch.setattr(time, "time", lambda: float(now))

    match = resolver.resolve_eid(eid_bytes)

    assert match is not None
    assert resolver._lock_miss_counts[device_id] == 0, (
        "BUG D: miss counter was not reset on a successful match. "
        "Update resolve_eid() to reset _lock_miss_counts[device_id] when a match occurs."
    )
