"""Hybrid EID resolver coverage for legacy and modern derivations."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import eid_resolver as resolver_module
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    GoogleFindMyEIDResolver,
    iter_rotation_windows,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    EidCandidate,
    K,
    build_table10_block,
    generate_eid_candidates,
    get_masked_timestamp,
)


def test_table10_block_layout_masks_timestamp() -> None:
    """Table 10 block must embed the masked timestamp and sentinels."""

    timestamp = 0x12345678
    block = build_table10_block(timestamp)
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
        "fhna_p256_truncated_rx20",
    }
    assert len(variant_map["fhna_secp160r1_rx20"]) == LEGACY_EID_LENGTH
    assert (
        variant_map["fhna_secp160r1_rx20"].hex()
        == "bccc42845790b5a2d9376edd6d66f8d15a6c7877"
    )
    assert len(variant_map["fhna_secp256r1_rx32"]) == 32
    assert (
        variant_map["fhna_secp256r1_rx32"].hex()
        == "f5a2f55527688d26c47043bde3f8888274a4eb9fcd6ff5fad3302f12dd47bf5e"
    )
    assert len(variant_map["fhna_p256_truncated_rx20"]) == LEGACY_EID_LENGTH
    assert (
        variant_map["fhna_p256_truncated_rx20"].hex()
        == "f5a2f55527688d26c47043bde3f8888274a4eb9f"
    )


def test_cached_candidates_registers_endianness_variants() -> None:
    """Both big- and little-endian P-256 variants should be cached."""

    identity_key = bytes.fromhex(
        "00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff"
    )
    cache_clear = getattr(
        resolver_module._cached_candidates, "cache_clear", None
    )
    if callable(cache_clear):
        cache_clear()

    candidates = resolver_module._cached_candidates(identity_key, 1_700_000_000)
    variant_names = {candidate.name for candidate in candidates}

    assert variant_names == {
        "fhna_secp160r1_rx20",
        "fhna_secp256r1_rx32",
        "fhna_p256_truncated_rx20",
        "fhna_p256_truncated_tail_rx20",
        "fhna_p256_le_rx32",
        "fhna_p256_le_truncated_rx20",
        "fhna_p256_le_truncated_tail_rx20",
    }


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
async def test_refresh_cache_registers_hybrid_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolver cache refresh should register all hybrid candidate variants."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace()
    resolver._lookup = {}
    resolver._known_offsets = {"registry-id": 0}
    resolver._known_endianness = {"registry-id": False}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

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

    call_order: list[int] = []

    def _fake_candidates(
        identity_key: bytes, timestamp: int
    ) -> tuple[EidCandidate, ...]:
        call_order.append(timestamp)
        return (
            EidCandidate(name="legacy", eid=b"A" * LEGACY_EID_LENGTH),
            EidCandidate(name="modern", eid=b"B" * 32),
            EidCandidate(name="modern_trunc", eid=b"C" * LEGACY_EID_LENGTH),
        )

    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver._cached_candidates",
        _fake_candidates,
    )

    fixed_time = 2048
    monkeypatch.setattr(time, "time", lambda: float(fixed_time))

    await resolver._refresh_cache()

    assert resolver._lookup[b"A" * LEGACY_EID_LENGTH].time_offset == 0
    assert resolver._lookup[b"B" * 32].time_offset == 0
    assert resolver._lookup[b"C" * LEGACY_EID_LENGTH].time_offset == 0
    assert call_order == [2048, 1024, 3072]


@pytest.mark.asyncio
async def test_unanchored_absolute_timebase_runs_deep_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unanchored identities should still receive deep scan coverage."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._decryption_status = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._provisioning_warn_at = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    identity = DeviceIdentity(
        registry_id="abs-id",
        canonical_id="abs-canonical",
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

    window_timestamps: list[int] = []

    def _fake_candidates(
        identity_key: bytes, timestamp: int
    ) -> tuple[EidCandidate, ...]:
        window_timestamps.append(timestamp)
        eid_bytes = timestamp.to_bytes(4, "big") * 5
        return (EidCandidate(name=f"eid-{timestamp}", eid=eid_bytes),)

    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        _fake_candidates,
    )

    fixed_time = 2048
    monkeypatch.setattr(time, "time", lambda: float(fixed_time))

    await resolver._refresh_cache()

    # Deep scan should expand far beyond the narrow +/-3 window range.
    assert len(window_timestamps) >= 90
    assert max(window_timestamps) >= (
        fixed_time
        + (
            resolver_module.REL_DEEP_SCAN_DENSE_RADIUS
            * resolver_module.ROTATION_PERIOD
        )
    )
