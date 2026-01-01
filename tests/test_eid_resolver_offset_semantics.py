# tests/test_eid_resolver_offset_semantics.py
"""Semantic offset handling for EID resolver timestamp bases."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    ROTATION_PERIOD,
    EidVariant,
)


@pytest.mark.asyncio
async def test_pair_date_offset_beats_drifted_unix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure a perfect relative offset survives a later unix drift collision."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=lambda coro: asyncio.create_task(coro),
        data={},
    )
    async def _async_noop(_payload=None):
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_advertisement_reversed = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._decryption_status = {}
    resolver._last_lock_confirmation = {}
    resolver._provisioning_warn_at = {}
    resolver._locks = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    base_now = ROTATION_PERIOD * 2
    pair_target = base_now - ROTATION_PERIOD
    unix_target = base_now
    pair_collision_ts = pair_target
    unix_collision_ts = unix_target + ROTATION_PERIOD
    collision_eid = b"\xAB" * LEGACY_EID_LENGTH

    identity = DeviceIdentity(
        registry_id="device-id",
        canonical_id="canonical-id",
        identity_key=b"\x01" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=base_now - pair_target,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(time, "time", lambda: float(base_now))
    monkeypatch.setattr(resolver_module, "ENABLE_ABSOLUTE_UNIX_BASIS", True)

    def _iter_rotation_windows(
        target_time: int,
        *,
        rotation_period: int,
        window_range,
        include_neighbors: bool,
    ) -> tuple[int, ...]:
        _ = (rotation_period, window_range, include_neighbors)
        if target_time == unix_target:
            return (unix_collision_ts,)
        if target_time == pair_target:
            return (pair_collision_ts,)
        return ()

    collision_pairs = {
        (EidVariant.LEGACY_SECP160R1_X20_BE, pair_collision_ts),
        (EidVariant.LEGACY_SECP160R1_X20_BE, unix_collision_ts),
    }

    def _generate_variant(
        self: GoogleFindMyEIDResolver,
        _key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        if (variant, time_counter) in collision_pairs:
            return collision_eid
        return time_counter.to_bytes(LEGACY_EID_LENGTH, byteorder="big", signed=False)

    monkeypatch.setattr(resolver_module, "iter_rotation_windows", _iter_rotation_windows)
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", _generate_variant)

    await resolver._refresh_cache()

    assert collision_eid in resolver._lookup
    match = resolver._lookup[collision_eid]
    metadata = resolver._lookup_metadata[collision_eid]

    assert match.time_offset == 0
    assert metadata["time_offset"] == 0
    assert metadata["timestamp_basis"] == "pair_date"
    assert {"pair_date", "unix"}.issubset(metadata["timestamp_bases"])
