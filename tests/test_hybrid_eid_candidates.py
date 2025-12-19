# tests/test_hybrid_eid_candidates.py
"""Hybrid EID resolver coverage for legacy and modern derivations."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import eid_resolver as resolver_module
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EIDMatch,
    GoogleFindMyEIDResolver,
    iter_rotation_windows,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    K,
    build_table10_prf_input,
    generate_eid_candidates,
    get_masked_timestamp,
)


def test_table10_block_layout_masks_timestamp() -> None:
    """Table 10 block must embed the masked timestamp and sentinels."""

    timestamp = 0x12345678
    block = build_table10_prf_input(timestamp, k=K)
    masked = get_masked_timestamp(timestamp, K)

    assert len(block) == 32
    assert block[0:11] == b"\xff" * 11
    assert block[11] == K
    assert block[12:16] == masked
    assert block[16:27] == b"\x00" * 11
    assert block[27] == K
    assert block[28:32] == masked


def test_generate_eid_candidates_expected_variants() -> None:
    """Legacy and modern variants should be deterministic for a known fixture."""

    identity_key = bytes.fromhex(
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    )
    timestamp = 1_700_000_000

    candidates = generate_eid_candidates(identity_key, timestamp)
    variant_map = {candidate.name: candidate.eid for candidate in candidates}

    assert set(variant_map) == {
        "fhna_secp160r1_rx20",
        "fhna_secp256r1_rx32",
    }
    assert len(variant_map["fhna_secp160r1_rx20"]) == LEGACY_EID_LENGTH
    assert len(variant_map["fhna_secp256r1_rx32"]) == MODERN_EID_LENGTH


def test_iter_rotation_windows_alignment_and_neighbors() -> None:
    """Rotation windows should align to boundaries and include neighbors when requested."""

    target_time = 2050
    without_neighbors = iter_rotation_windows(
        target_time,
        rotation_period=1024,
        window_range=range(-1, 2),
        include_neighbors=False,
    )
    assert without_neighbors == (1024, 2048, 3072)

    with_neighbors = iter_rotation_windows(
        target_time,
        rotation_period=1024,
        window_range=range(0, 1),
        include_neighbors=True,
    )
    assert with_neighbors == (2048, 1024, 3072)


@pytest.mark.asyncio
async def test_refresh_cache_registers_current_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver cache refresh should register legacy and modern variants for the current window."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(async_create_task=lambda coro: asyncio.create_task(coro), data={})
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}
    async def _async_noop(payload=None):
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\x01" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _collect,
    )
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )

    fixed_time = 2048
    monkeypatch.setattr(time, "time", lambda: float(fixed_time))

    await resolver._refresh_cache()

    matches = list(resolver._lookup.values())
    assert matches
    assert all(isinstance(match, EIDMatch) for match in matches)


@pytest.mark.asyncio
async def test_resolve_eid_creates_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver should store a lock after the first successful match."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=lambda coro: asyncio.create_task(coro),
        data={},
    )
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}
    async def _async_noop(payload=None):
        return None

    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\x02" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _collect,
    )
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_normalize_identities",
        lambda self, identities, cache=None: identities,
    )
    monkeypatch.setattr(time, "time", lambda: 1024.0)

    await resolver._refresh_cache()

    eid = next(iter(resolver._lookup))
    payload = bytes([resolver_module.FMDN_FRAME_TYPE]) + eid

    match = resolver.resolve_eid(payload)

    assert match is not None
    assert resolver._locks[match.device_id].eid_length == LEGACY_EID_LENGTH
