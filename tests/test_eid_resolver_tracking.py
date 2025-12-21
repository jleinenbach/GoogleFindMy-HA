# tests/test_eid_resolver_tracking.py
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
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
    resolver.hass = MagicMock()
    resolver._locks = {}
    resolver._ensure_cache_defaults()
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None

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
        rotation_timestamp=1_000,
    )

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
    monkeypatch.setattr(time, "time", lambda: 42_000.0)

    await resolver._refresh_cache()

    expected_counters = {
        1_000,
        1_000 + ROTATION_PERIOD,
        1_000 + (2 * ROTATION_PERIOD),
    }
    assert set(generated_counters) == expected_counters
