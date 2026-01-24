# tests/test_eid_resolver_tracking.py
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    LOCK_TRACKING_WINDOW_STEPS,
    ROTATION_PERIOD,
    EIDGenerationLock,
    EidVariant,
    GoogleFindMyEIDResolver,
)


@pytest.mark.asyncio
async def test_tracking_mode_predicts_next_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure locked devices use deterministic tracking windows."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=lambda coro, name=None: asyncio.create_task(coro),
        async_create_background_task=lambda coro, name=None: asyncio.create_task(coro),
    )
    resolver._locks = {}
    resolver._ensure_cache_defaults()
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None

    async def _noop_save(payload: object) -> None:
        return None

    resolver._store = SimpleNamespace(async_save=_noop_save)

    identity = DeviceIdentity(
        registry_id="device-id",
        canonical_id="canonical-id",
        identity_key=b"key-bytes",
        config_entry_id="entry-id",
    )
    resolver._locks[identity.registry_id] = EIDGenerationLock(
        device_id=identity.registry_id,
        canonical_id=identity.canonical_id,
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=32,
        rotation_timestamp=5_000,
        created_at=1_700_000_000,
    )
    current_time = 1_700_000_000 + (10 * ROTATION_PERIOD)
    resolver._last_lock_confirmation[identity.registry_id] = current_time

    generated_counters: list[int] = []

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    def _generate_variant(
        _self: GoogleFindMyEIDResolver,
        key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        generated_counters.append(time_counter)
        return f"eid-{variant.value}-{time_counter}".encode()

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", _generate_variant)
    monkeypatch.setattr(time, "time", lambda: float(current_time))

    await resolver._refresh_cache()

    expected_counters = {
        5_000 + ((10 + step) * ROTATION_PERIOD)
        for step in range(-LOCK_TRACKING_WINDOW_STEPS, LOCK_TRACKING_WINDOW_STEPS + 1)
    }
    assert set(generated_counters) == expected_counters
