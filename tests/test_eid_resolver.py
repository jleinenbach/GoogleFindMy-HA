# tests/test_eid_resolver.py
import asyncio
import logging
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import custom_components.googlefindmy.coordinator as coordinator_module
import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EID_LENGTH,
    LOCK_CUSTOM_FIELD,
    EIDMatch,
    GoogleFindMyEIDResolver,
    TimebaseLabel,
    _build_timebase_candidates,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    FHNA_K,
    FHNA_ROTATION_MASK,
    ROTATION_PERIOD,
    EidCandidate,
    K,
    build_table10_block,
    fhna_build_prf_input,
    get_masked_timestamp,
)


def _fixed_length_eid(key: bytes, timestamp: int) -> bytes:
    """Generate a deterministic 20-byte EID for test fixtures."""

    ts_bytes = timestamp.to_bytes(8, "big", signed=False)
    seed = key + ts_bytes
    return seed.ljust(EID_LENGTH, b"\x00")[:EID_LENGTH]


def _fixed_length_p256_eid(key: bytes, timestamp: int) -> bytes:
    ts_bytes = timestamp.to_bytes(8, "big", signed=False)
    seed = b"p256" + key + ts_bytes + key
    return seed.ljust(resolver_module.MODERN_EID_LENGTH, b"\x00")[
        : resolver_module.MODERN_EID_LENGTH
    ]


def _build_resolver() -> GoogleFindMyEIDResolver:
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._persisted_locks = {}
    resolver._provisioning_warn_at = {}
    resolver._last_lock_confirmation = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    return resolver


def test_fhna_prf_input_layout_matches_table_10() -> None:
    ts_u32 = 0x1234_5678

    prf_input = fhna_build_prf_input(ts_u32)

    assert len(prf_input) == 32
    assert prf_input[0:11] == b"\xff" * 11
    assert prf_input[11] == FHNA_K
    assert prf_input[16:27] == b"\x00" * 11
    assert prf_input[27] == FHNA_K

    aligned_ts = (ts_u32 & 0xFFFFFFFF) & ~((1 << FHNA_K) - 1)
    assert prf_input[12:16] == aligned_ts.to_bytes(4, "big")
    assert prf_input[28:32] == aligned_ts.to_bytes(4, "big")


def test_fhna_prf_input_masks_low_bits() -> None:
    base_ts = 0xABCDEF00
    offset_ts = base_ts | ((1 << FHNA_K) - 1)

    assert fhna_build_prf_input(base_ts) == fhna_build_prf_input(offset_ts)


def test_fhna_prf_input_masks_low_bits_within_rotation() -> None:
    base_ts = 0xABCDE000
    nearly_next_rotation = base_ts + (FHNA_ROTATION_MASK - 1)

    assert fhna_build_prf_input(base_ts) == fhna_build_prf_input(nearly_next_rotation)


def test_fhna_prf_input_matches_expected_layout_bytes() -> None:
    ts_u32 = 0x1F2E3D4C
    masked = (ts_u32 & 0xFFFFFFFF) & ~((1 << FHNA_K) - 1)

    expected = (
        b"\xff" * 11
        + bytes([FHNA_K])
        + masked.to_bytes(4, "big")
        + b"\x00" * 11
        + bytes([FHNA_K])
        + masked.to_bytes(4, "big")
    )

    assert fhna_build_prf_input(ts_u32) == expected


def test_build_table10_block_enforces_fhna_k() -> None:
    ts_u32 = 0x12345678

    assert build_table10_block(ts_u32, k=FHNA_K) == fhna_build_prf_input(ts_u32)
    with pytest.raises(ValueError):
        build_table10_block(ts_u32, k=FHNA_K + 1)


@pytest.mark.asyncio
async def test_refresh_cache_records_consistent_time_domains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rotation_timestamp and time_offset share the same domain."""

    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    anchor_now = 2_000
    monkeypatch.setattr(resolver_module.time, "time", lambda: anchor_now)

    identity = DeviceIdentity(
        registry_id="registry-time",  # type: ignore[arg-type]
        canonical_id="canon-time",  # type: ignore[arg-type]
        identity_key=b"\x11" * 20,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-time",  # type: ignore[arg-type]
        pair_date=500,
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: (
            EidCandidate(name="fhna_secp160r1_rx20", eid=b"\xaa" * EID_LENGTH),
        ),
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: None),
    )

    await resolver._refresh_cache()

    assert resolver._lookup_metadata
    for metadata in resolver._lookup_metadata.values():
        timebase = metadata.get("timebase")
        anchor_epoch = metadata.get("anchor_epoch")
        try:
            anchor_ts = int(anchor_epoch) if anchor_epoch is not None else None
        except (TypeError, ValueError):
            anchor_ts = None

        expected_reference = resolver_module._mask_u32(anchor_now)  # type: ignore[attr-defined]
        if timebase != TimebaseLabel.ABSOLUTE:
            assert anchor_ts is not None
            expected_reference = anchor_now - anchor_ts

        assert (
            metadata["rotation_timestamp"] - metadata["time_offset"]
            == expected_reference
        )
        assert metadata["masked_rotation_timestamp"] == resolver_module._mask_u32(
            metadata["rotation_timestamp"]
        )


@pytest.mark.asyncio
async def test_absolute_timebase_skips_deep_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    now = ROTATION_PERIOD * 50
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    identity = DeviceIdentity(
        registry_id="registry-absolute",  # type: ignore[arg-type]
        canonical_id="absolute-device",  # type: ignore[arg-type]
        identity_key=b"\x33" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-absolute",  # type: ignore[arg-type]
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: (
            EidCandidate(name="fhna_secp160r1_rx20", eid=b"\xaa" * EID_LENGTH),
        ),
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: None),
    )

    await resolver._refresh_cache()

    offsets = [
        metadata["time_offset"]
        for metadata in resolver._lookup_metadata.values()
        if metadata.get("timebase") == TimebaseLabel.ABSOLUTE
    ]

    assert offsets
    max_narrow_rotations = max(
        abs(value) for value in resolver_module.NARROW_SCAN_RANGE
    )
    assert max(abs(offset) for offset in offsets) <= ROTATION_PERIOD * (
        max_narrow_rotations + 1
    )


@pytest.mark.asyncio
async def test_relative_timebase_allows_deep_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolver_module, "REL_DEEP_SCAN_DENSE_RADIUS", 5)
    monkeypatch.setattr(resolver_module, "REL_DEEP_SCAN_MAX_DRIFT", 5)
    monkeypatch.setattr(resolver_module, "REL_DEEP_SCAN_SPARSE_STEP", 1)

    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    now = ROTATION_PERIOD * 75
    anchor_epoch = now - 10
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    identity = DeviceIdentity(
        registry_id="registry-rel",  # type: ignore[arg-type]
        canonical_id="rel-device",  # type: ignore[arg-type]
        identity_key=b"\x44" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-rel",  # type: ignore[arg-type]
        pair_date=anchor_epoch,
    )

    async def _fake_collect_rel(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect_rel,
    )
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda _identity_key, timestamp, **_kwargs: (
            EidCandidate(
                name="fhna_secp160r1_rx20",
                eid=timestamp.to_bytes(EID_LENGTH, "big", signed=False),
            ),
        ),
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: None),
    )

    await resolver._refresh_cache()

    max_narrow_rotations = max(
        abs(value) for value in resolver_module.NARROW_SCAN_RANGE
    )
    rel_offsets = {
        metadata["time_offset"]
        for metadata in resolver._lookup_metadata.values()
        if metadata.get("timebase") == TimebaseLabel.REL_PAIR
    }

    assert any(
        abs(offset) > max_narrow_rotations * ROTATION_PERIOD for offset in rel_offsets
    )


@pytest.mark.asyncio
async def test_relative_fallback_populates_pair_and_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REL timebases should populate even when window generation is skipped."""

    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    now = ROTATION_PERIOD * 42
    pair_date = now - 1234
    secrets_date = now - 2345

    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    identity = DeviceIdentity(
        registry_id="registry-rel-fallback",  # type: ignore[arg-type]
        canonical_id="fallback-device",  # type: ignore[arg-type]
        identity_key=b"\x44" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-rel-fallback",  # type: ignore[arg-type]
        pair_date=pair_date,
        secrets_creation_date=secrets_date,
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "iter_rotation_windows",
        lambda *_args, **_kwargs: tuple(),
    )
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: (
            EidCandidate(name="fhna_secp160r1_rx20", eid=b"\xab" * EID_LENGTH),
        ),
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: None),
    )

    await resolver._refresh_cache()

    expected_lengths = {EID_LENGTH, resolver_module.MODERN_EID_LENGTH}
    assert set(len(key) for key in resolver._lookup) <= expected_lengths
    assert set(len(key) for key in resolver._lookup_metadata) <= expected_lengths

    anchors: dict[str, int] = {}
    label_eids: dict[str, set[bytes]] = {
        TimebaseLabel.REL_PAIR: set(),
        TimebaseLabel.REL_SECRETS: set(),
    }

    for eid_key, metadata in resolver._lookup_metadata.items():
        if not isinstance(metadata, dict):
            continue
        for payload in metadata.get("timebases", []):
            if not isinstance(payload, dict):
                continue
            label = payload.get("timebase")
            if label in label_eids:
                label_eids[label].add(eid_key)
                anchor = payload.get("anchor_epoch")
                if anchor is not None:
                    anchors.setdefault(label, int(anchor))

    assert all(label_eids[label] for label in label_eids)
    assert anchors[TimebaseLabel.REL_PAIR] == pair_date
    assert anchors[TimebaseLabel.REL_SECRETS] == secrets_date

    for eids in label_eids.values():
        for eid_key in eids:
            assert eid_key in resolver._lookup
            assert eid_key[::-1] in resolver._lookup


@pytest.mark.asyncio
async def test_registry_miss_preserves_cached_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    registry_id = "registry-lock-miss"
    cached_lock = resolver_module._TimebaseLock(  # type: ignore[attr-defined]
        TimebaseLabel.ABSOLUTE,
        anchor_epoch=None,
        rotation_timestamp=123,
        offset=42,
        variant="fhna_p256_truncated_rx20",
    )
    resolver._known_timebases[registry_id] = cached_lock
    resolver._known_offsets[registry_id] = cached_lock.offset
    resolver._known_endianness[registry_id] = False
    resolver._persisted_locks[registry_id] = {"label": cached_lock.label}

    identity = DeviceIdentity(
        registry_id=registry_id,  # type: ignore[arg-type]
        canonical_id="canonical-lock-miss",  # type: ignore[arg-type]
        identity_key=b"\x99" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-lock-miss",  # type: ignore[arg-type]
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: (
            EidCandidate(name="fhna_secp160r1_rx20", eid=b"\xaa" * EID_LENGTH),
        ),
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: None),
    )

    await resolver._refresh_cache()

    assert resolver._known_timebases.get(registry_id) == cached_lock
    assert resolver._known_offsets.get(registry_id) == cached_lock.offset
    assert resolver._known_endianness.get(registry_id) is False
    assert resolver._persisted_locks.get(registry_id) == {"label": cached_lock.label}


@pytest.mark.asyncio
async def test_registry_entry_without_lock_drops_cache(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    registry_id = "registry-lock-drop"
    resolver._known_timebases[registry_id] = resolver_module._TimebaseLock(  # type: ignore[attr-defined]
        TimebaseLabel.ABSOLUTE, anchor_epoch=None, rotation_timestamp=10, offset=5
    )
    resolver._known_offsets[registry_id] = 5
    resolver._known_endianness[registry_id] = True
    resolver._persisted_locks[registry_id] = {"label": TimebaseLabel.ABSOLUTE}

    identity = DeviceIdentity(
        registry_id=registry_id,  # type: ignore[arg-type]
        canonical_id="canonical-lock-drop",  # type: ignore[arg-type]
        identity_key=b"\x98" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-lock-drop",  # type: ignore[arg-type]
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: (
            EidCandidate(name="fhna_secp160r1_rx20", eid=b"\xaa" * EID_LENGTH),
        ),
    )

    entry = SimpleNamespace(custom_fields={})
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: entry),
    )

    with caplog.at_level(logging.DEBUG):
        await resolver._refresh_cache()

    # Keep in-memory hints, but disable persisted hard lock filtering.
    assert registry_id in resolver._known_timebases
    assert registry_id in resolver._known_offsets
    assert registry_id in resolver._known_endianness
    assert registry_id not in resolver._persisted_locks
    assert any(
        "Disabling hard lock" in message and registry_id in message
        for message in caplog.messages
    )


@pytest.mark.asyncio
async def test_registry_lock_update_overwrites_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    registry_id = "registry-lock-update"
    resolver._known_timebases[registry_id] = resolver_module._TimebaseLock(  # type: ignore[attr-defined]
        TimebaseLabel.ABSOLUTE, anchor_epoch=None, rotation_timestamp=10, offset=5
    )
    resolver._known_offsets[registry_id] = 5
    resolver._known_endianness[registry_id] = False
    resolver._persisted_locks[registry_id] = {"label": TimebaseLabel.ABSOLUTE}

    identity = DeviceIdentity(
        registry_id=registry_id,  # type: ignore[arg-type]
        canonical_id="canonical-lock-update",  # type: ignore[arg-type]
        identity_key=b"\x97" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-lock-update",  # type: ignore[arg-type]
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: (
            EidCandidate(name="fhna_secp160r1_rx20", eid=b"\xaa" * EID_LENGTH),
        ),
    )

    new_payload = {
        "label": TimebaseLabel.REL_PAIR,
        "rotation_timestamp": 44,
        "time_offset": 99,
        "anchor_epoch": 11,
        "variant": "fhna_p256_le_rx32",
    }
    entry = SimpleNamespace(custom_fields={LOCK_CUSTOM_FIELD: new_payload})
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: entry),
    )

    await resolver._refresh_cache()

    updated_lock = resolver._known_timebases[registry_id]
    assert updated_lock.label == TimebaseLabel.REL_PAIR
    assert updated_lock.offset == 99
    assert updated_lock.anchor_epoch == 11
    assert updated_lock.variant == "fhna_p256_le_rx32"
    assert resolver._known_endianness[registry_id] is False
    assert resolver._persisted_locks[registry_id] == new_payload


@pytest.mark.asyncio
async def test_relative_fallback_registers_multiple_variants_and_endianness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    now = ROTATION_PERIOD * 40
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    identity = DeviceIdentity(
        registry_id="registry-fallback-multi",  # type: ignore[arg-type]
        canonical_id="canonical-fallback",  # type: ignore[arg-type]
        identity_key=b"\x45" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-fallback",  # type: ignore[arg-type]
        pair_date=now - 100,
        secrets_creation_date=now - 200,
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "iter_rotation_windows",
        lambda *_args, **_kwargs: tuple(),
    )

    candidate_one = EidCandidate(name="fhna_variant_one", eid=b"\x01" * EID_LENGTH)
    candidate_two = EidCandidate(name="fhna_variant_two", eid=b"\x02" * EID_LENGTH)
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: (candidate_one, candidate_two),
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: None),
    )

    await resolver._refresh_cache()

    variants = {
        payload.get("variant")
        for metadata in resolver._lookup_metadata.values()
        for payload in metadata.get("timebases", [])
        if isinstance(metadata, dict)
        and isinstance(payload, dict)
        and payload.get("timebase")
        in {TimebaseLabel.REL_PAIR, TimebaseLabel.REL_SECRETS}
    }

    assert {candidate_one.name, candidate_two.name}.issubset(variants)

    for eid_candidate in (candidate_one, candidate_two):
        assert eid_candidate.eid in resolver._lookup
        assert eid_candidate.eid[::-1] in resolver._lookup


@pytest.mark.asyncio
async def test_relative_fallback_registers_le_variants(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fallback registration should not trim little-endian variants."""

    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    now = ROTATION_PERIOD * 50
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    identity = DeviceIdentity(
        registry_id="registry-fallback-variants",  # type: ignore[arg-type]
        canonical_id="canonical-fallback-variants",  # type: ignore[arg-type]
        identity_key=b"\x46" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-fallback-variants",  # type: ignore[arg-type]
        pair_date=now - 10,
        secrets_creation_date=now - 20,
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "iter_rotation_windows",
        lambda *_args, **_kwargs: tuple(),
    )

    candidates = [
        EidCandidate(name=f"fhna_variant_{idx}", eid=bytes([idx]) * EID_LENGTH)
        for idx in range(5)
    ]
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: tuple(candidates),
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: None),
    )

    await resolver._refresh_cache()

    registered = set(resolver._lookup)
    for candidate in candidates:
        assert candidate.eid in registered
        assert candidate.eid[::-1] in registered

    variants = {
        payload.get("variant")
        for metadata in resolver._lookup_metadata.values()
        for payload in metadata.get("timebases", [])
        if isinstance(metadata, dict)
        and isinstance(payload, dict)
        and payload.get("timebase")
        in {TimebaseLabel.REL_PAIR, TimebaseLabel.REL_SECRETS}
    }

    assert {candidate.name for candidate in candidates}.issubset(variants)


@pytest.mark.asyncio
async def test_relative_fallback_runs_under_absolute_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Relative fallback should populate even when an absolute lock filters scans."""

    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    now = ROTATION_PERIOD * 55
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    registry_id = "registry-absolute-lock"
    active_lock = resolver_module._TimebaseLock(  # type: ignore[attr-defined]
        label=TimebaseLabel.ABSOLUTE,
        anchor_epoch=None,
        rotation_timestamp=now - ROTATION_PERIOD,
        offset=ROTATION_PERIOD,
        variant="fhna_p256_truncated_rx20",
    )
    resolver._known_timebases[registry_id] = active_lock
    resolver._known_offsets[registry_id] = active_lock.offset
    resolver._known_endianness[registry_id] = False
    resolver._persisted_locks[registry_id] = {"label": active_lock.label}

    identity = DeviceIdentity(
        registry_id=registry_id,  # type: ignore[arg-type]
        canonical_id="canonical-absolute-lock",  # type: ignore[arg-type]
        identity_key=b"\x4a" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-absolute-lock",  # type: ignore[arg-type]
        pair_date=now - 300,
        secrets_creation_date=now - 600,
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )
    monkeypatch.setattr(
        resolver_module,
        "iter_rotation_windows",
        lambda *_args, **_kwargs: tuple(),
    )

    captured_timestamps: list[int] = []

    def _eid_from_timestamp(ts: int, *, record: bool = True) -> tuple[EidCandidate, ...]:
        if record:
            captured_timestamps.append(ts)
        ts_bytes = int(ts).to_bytes(8, "big", signed=False)
        base_seed = (b"ts:" + ts_bytes) * 4
        eid_20 = base_seed.ljust(EID_LENGTH, b"\x00")[:EID_LENGTH]
        eid_32 = (b"ts32:" + ts_bytes) * 6
        eid_32 = eid_32.ljust(resolver_module.MODERN_EID_LENGTH, b"\x00")[
            : resolver_module.MODERN_EID_LENGTH
        ]
        return (
            EidCandidate(name=f"ts20_{ts}", eid=eid_20),
            EidCandidate(name=f"ts32_{ts}", eid=eid_32),
        )

    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda _key, ts: _eid_from_timestamp(ts),
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: None),
    )

    base_candidates = resolver_module._build_timebase_candidates(identity, now_unix=now)
    rel_pair = next(
        candidate for candidate in base_candidates if candidate.label == TimebaseLabel.REL_PAIR
    )
    rel_secrets = next(
        candidate for candidate in base_candidates if candidate.label == TimebaseLabel.REL_SECRETS
    )
    expected_pair_rotation = rel_pair.reference_time - (
        rel_pair.reference_time % ROTATION_PERIOD
    )
    expected_secrets_rotation = rel_secrets.reference_time - (
        rel_secrets.reference_time % ROTATION_PERIOD
    )

    await resolver._refresh_cache()

    assert set(captured_timestamps) == {expected_pair_rotation, expected_secrets_rotation}

    expected_lengths = {EID_LENGTH, resolver_module.MODERN_EID_LENGTH}
    assert set(len(key) for key in resolver._lookup) <= expected_lengths
    assert set(len(key) for key in resolver._lookup_metadata) <= expected_lengths

    expected_eids = (
        _eid_from_timestamp(expected_pair_rotation, record=False)[0].eid,
        _eid_from_timestamp(expected_pair_rotation, record=False)[1].eid,
        _eid_from_timestamp(expected_secrets_rotation, record=False)[0].eid,
        _eid_from_timestamp(expected_secrets_rotation, record=False)[1].eid,
    )

    for eid_key in expected_eids:
        assert eid_key in resolver._lookup
        assert eid_key[::-1] in resolver._lookup
        assert eid_key in resolver._lookup_metadata
        assert eid_key[::-1] in resolver._lookup_metadata


@pytest.mark.asyncio
async def test_stale_lock_drops_after_stale_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Locks confirmed long ago should be cleared before rehydration."""

    resolver = _build_resolver()
    resolver.hass = SimpleNamespace()
    resolver._refresh_lock = asyncio.Lock()

    now = ROTATION_PERIOD * 60
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    registry_id = "registry-stale-lock"
    stale_lock = resolver_module._TimebaseLock(  # type: ignore[attr-defined]
        TimebaseLabel.ABSOLUTE,
        anchor_epoch=None,
        rotation_timestamp=now - ROTATION_PERIOD,
        offset=ROTATION_PERIOD,
        variant="fhna_p256_truncated_rx20",
    )

    resolver._known_timebases[registry_id] = stale_lock
    resolver._known_offsets[registry_id] = stale_lock.offset
    resolver._known_endianness[registry_id] = False
    resolver._persisted_locks[registry_id] = {"label": stale_lock.label}

    identity = DeviceIdentity(
        registry_id=registry_id,  # type: ignore[arg-type]
        canonical_id="canonical-stale",  # type: ignore[arg-type]
        identity_key=b"\x47" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-stale",  # type: ignore[arg-type]
    )

    async def _fake_collect(
        self: resolver_module.GoogleFindMyEIDResolver,
    ) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_collect_device_secrets",
        _fake_collect,
    )

    device_entry = SimpleNamespace(
        custom_fields={
            LOCK_CUSTOM_FIELD: {
                "label": stale_lock.label,
                "rotation_timestamp": stale_lock.rotation_timestamp,
                "time_offset": stale_lock.offset,
                "is_reversed": False,
                "confirmed_at": now - (resolver_module.LOCK_STALE_AFTER_SECONDS + 1),
            }
        }
    )
    monkeypatch.setattr(
        resolver_module.dr,
        "async_get",
        lambda _hass: SimpleNamespace(async_get=lambda _id: device_entry),
    )
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda *_args, **_kwargs: (
            EidCandidate(name="fhna_variant_one", eid=b"\x10" * EID_LENGTH),
        ),
    )

    await resolver._refresh_cache()

    assert registry_id not in resolver._known_timebases
    assert registry_id not in resolver._known_offsets
    assert registry_id not in resolver._known_endianness
    assert registry_id not in resolver._persisted_locks
    assert registry_id not in resolver._last_lock_confirmation
    assert LOCK_CUSTOM_FIELD not in device_entry.custom_fields


def test_resolve_updates_last_lock_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful resolutions should refresh the confirmation timestamp."""

    resolver = _build_resolver()
    now = 123456
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    registry_id = "registry-confirmation"
    eid_bytes = b"\x01" * EID_LENGTH
    resolver._lookup[eid_bytes] = EIDMatch(
        device_id=registry_id,
        config_entry_id="entry-confirmation",
        canonical_id="canonical-confirmation",
        time_offset=0,
        is_reversed=False,
    )
    resolver._lookup_metadata[eid_bytes] = {
        "time_offset": 0,
        "rotation_timestamp": now,
        "timebase": TimebaseLabel.ABSOLUTE,
    }

    resolver.hass = SimpleNamespace(
        async_create_task=lambda coro: asyncio.get_event_loop().create_task(coro)
    )
    monkeypatch.setattr(resolver_module.dr, "async_get", lambda _hass: None)

    match = resolver.resolve_eid(eid_bytes)

    assert match is not None
    assert resolver._last_lock_confirmation[registry_id] == now


def test_coerce_int_accepts_timestamp_like_mapping() -> None:
    timestamp_mapping = {"seconds": 1_000, "nanos": 500_000_000}
    timestamp_object = SimpleNamespace(seconds=2_000, nanos=250_000_000)

    assert resolver_module._coerce_int(timestamp_mapping) == 1_000
    assert resolver_module._coerce_int(timestamp_object) == 2_000
    assert resolver_module._coerce_int({"nanos": 123}) is None


def test_timebase_candidates_include_absolute_and_relative_anchor() -> None:
    now = 1_700_000_000
    secrets_anchor = now - 86_400
    pair_anchor = now - 200_000
    identity = DeviceIdentity(
        device_type="pixel",
        registry_id="registry-anchor",
        canonical_id="anchor-device",
        identity_key=b"\x01" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-anchor",
        pair_date=pair_anchor,
        secrets_creation_date=secrets_anchor,
        time_anchors_debug=None,
    )

    candidates = _build_timebase_candidates(
        identity,
        now_unix=now,
    )

    absolute = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.ABSOLUTE
    )
    rel_pair = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.REL_PAIR
    )
    rel_secrets = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.REL_SECRETS
    )

    assert absolute.reference_time == resolver_module._mask_u32(now)  # type: ignore[attr-defined]
    assert rel_secrets.reference_time == now - secrets_anchor
    assert rel_pair.reference_time == now - pair_anchor


def test_timebase_domain_separation() -> None:
    """Absolute and relative candidates should stay in their own time domains."""

    now = 1_760_000_000
    pair_date = now - 100_000
    identity = DeviceIdentity(
        device_type="pixel",
        registry_id="registry-domain",  # type: ignore[arg-type]
        canonical_id="domain-device",  # type: ignore[arg-type]
        identity_key=b"\x05" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-domain",  # type: ignore[arg-type]
        pair_date=pair_date,
        secrets_creation_date=None,
        time_anchors_debug=None,
    )

    candidates = _build_timebase_candidates(
        identity,
        now_unix=now,
    )

    absolute = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.ABSOLUTE
    )
    rel_pair = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.REL_PAIR
    )

    assert absolute.reference_time == resolver_module._mask_u32(now)  # type: ignore[attr-defined]
    assert rel_pair.reference_time == now - pair_date
    assert absolute.reference_time > 1_000_000_000
    assert rel_pair.reference_time < 1_000_000_000


def test_candidates_include_both_anchors_when_present() -> None:
    now = 1_800_000_000
    pair_date = now - 500_000
    secrets_creation_date = now - 50_000
    identity = DeviceIdentity(
        device_type="pixel",
        registry_id="registry-both-anchors",  # type: ignore[arg-type]
        canonical_id="both-anchors",  # type: ignore[arg-type]
        identity_key=b"\x06" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-both-anchors",  # type: ignore[arg-type]
        pair_date=pair_date,
        secrets_creation_date=secrets_creation_date,
        time_anchors_debug=None,
    )

    candidates = _build_timebase_candidates(
        identity,
        now_unix=now,
    )

    labels = {candidate.label for candidate in candidates}
    assert {
        TimebaseLabel.ABSOLUTE,
        TimebaseLabel.REL_PAIR,
        TimebaseLabel.REL_SECRETS,
    }.issubset(labels)

    rel_pair = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.REL_PAIR
    )
    rel_secrets = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.REL_SECRETS
    )

    assert rel_pair.reference_time == now - pair_date
    assert rel_secrets.reference_time == now - secrets_creation_date
    assert rel_pair.reference_time != rel_secrets.reference_time


def test_time_offset_alignment_uses_candidate_reference() -> None:
    now = 2_000_000
    anchor_epoch = now - 100
    identity = DeviceIdentity(
        device_type="pixel",
        registry_id="registry-offset",
        canonical_id="offset-device",
        identity_key=b"\x02" * EID_LENGTH,
        encrypted_identity_key=None,
        owner_key_version=None,
        config_entry_id="entry-offset",
        pair_date=anchor_epoch,
        secrets_creation_date=None,
        time_anchors_debug=None,
    )

    candidates = _build_timebase_candidates(
        identity,
        now_unix=now,
    )

    relative = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.REL_PAIR
    )
    rotation_timestamp = relative.reference_time - (
        relative.reference_time % ROTATION_PERIOD
    )
    time_offset = rotation_timestamp - relative.reference_time

    assert abs(time_offset) < ROTATION_PERIOD
    assert relative.reference_time == now - anchor_epoch


def test_p256_truncation_variants_enabled_and_deduplicated() -> None:
    identity_key = b"\x0c" * EID_LENGTH

    resolver_module._cached_candidates.cache_clear()
    candidates = resolver_module._cached_candidates(identity_key, 0)

    names = {candidate.name for candidate in candidates}
    unique_payloads = {candidate.eid for candidate in candidates}

    assert "fhna_p256_truncated_tail_rx20" in names
    assert "fhna_p256_le_rx32" in names
    assert "fhna_p256_le_truncated_rx20" in names
    assert "fhna_p256_le_truncated_tail_rx20" in names
    assert len(unique_payloads) == len(candidates)


class _StubDevice:
    def __init__(
        self,
        identifier: str,
        *,
        registry_id: str | None = None,
        custom_fields: dict | None = None,
        disabled: bool = False,
        identifiers: set[tuple[str, str]] | None = None,
    ) -> None:
        self.identifier = identifier
        self.id = registry_id or identifier
        self.custom_fields = custom_fields
        self.disabled_by = "user" if disabled else None
        self.identifiers = identifiers or {(DOMAIN, identifier)}


class _StubDeviceRegistry:
    def __init__(self, devices: list[_StubDevice]) -> None:
        self._devices = devices

    def async_entries_for_config_entry(self, _entry_id: str) -> list[_StubDevice]:
        return list(self._devices)

    def async_get(self, device_id: str) -> _StubDevice | None:
        return next(
            (device for device in self._devices if device.id == device_id), None
        )

    def async_get_device(
        self,
        *,
        identifiers: set[tuple[str, str]] | None = None,
        device_id: str | None = None,
    ) -> _StubDevice | None:
        if device_id:
            return self.async_get(device_id)

        if identifiers:
            for device in self._devices:
                device_identifiers = getattr(device, "identifiers", None)
                if device_identifiers and set(device_identifiers) & identifiers:
                    return device

        return None

    def async_update_device(
        self, device_id: str, *, custom_fields: dict | None = None
    ) -> _StubDevice | None:
        device = self.async_get(device_id)
        if device is None:
            return None
        if custom_fields is not None:
            device.custom_fields = custom_fields
        return device


class _StubHass:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or {}

    def async_create_task(
        self, coro: asyncio.Future, name: str | None = None
    ) -> asyncio.Task:
        return asyncio.create_task(coro, name=name)


@pytest.mark.asyncio
async def test_active_device_identities_prefer_registry_custom_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-1",
                registry_id="registry-1",
                custom_fields={"identity_key": "0f0e0d", "pairDate": 1_700_000_006},
            )
        ]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1")
    coordinator._enabled_poll_device_ids = {"dev-1"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.identity_key == bytes.fromhex("0f0e0d")
    assert identity.config_entry_id == "entry-1"
    assert identity.registry_id == "registry-1"
    assert identity.canonical_id == "dev-1"


@pytest.mark.asyncio
async def test_active_device_identities_fall_back_to_location_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [_StubDevice("dev-2", registry_id="registry-2", custom_fields={})]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-2")
    coordinator._enabled_poll_device_ids = {"dev-2"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {
        "dev-2": {"identityKey": "abcd", "pair_date": 1_700_000_007}
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.registry_id == "registry-2"
    assert identity.canonical_id == "dev-2"
    assert identity.identity_key == bytes.fromhex("abcd")
    assert identity.config_entry_id == "entry-2"


@pytest.mark.asyncio
async def test_active_device_identities_ignore_opt_out_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-3",
                registry_id="registry-3",
                custom_fields={"identity_key": "1234"},
            )
        ]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-3")
    coordinator._enabled_poll_device_ids = {"dev-3"}
    coordinator._get_ignored_set = lambda: {"dev-3"}
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}

    identities = coordinator.get_active_device_identities()

    assert identities == []


@pytest.mark.asyncio
async def test_active_device_identities_surface_registry_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-meta",
                registry_id="registry-meta",
                custom_fields={
                    "identity_key": "0f0e0d",
                    "pair_date": {"seconds": 1234, "nanos": 500_000_000},
                    "encrypted_user_secrets": {
                        "creationDate": {"seconds": 2468, "nanos": 0}
                    },
                    "timeAnchorsDebug": {"hint": "anchor"},
                },
            )
        ]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-meta")
    coordinator._enabled_poll_device_ids = {"dev-meta"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.pair_date == 1234
    assert identity.secrets_creation_date == 2468
    assert identity.time_anchors_debug == {"hint": "anchor"}


def test_build_timebase_candidates_with_time_anchor_hints() -> None:
    now = ROTATION_PERIOD * 8
    identity = DeviceIdentity(
        registry_id="registry-anchor",
        canonical_id="device-anchor",
        identity_key=b"\x01",
        time_anchors_debug={
            "anchors": [
                {"label": "REL_DEBUG_HINT", "anchor_epoch": now - ROTATION_PERIOD}
            ]
        },
    )

    candidates = _build_timebase_candidates(
        identity,
        now_unix=now,
    )

    labels = {candidate.label for candidate in candidates}
    assert "REL_DEBUG_HINT" in labels
    assert TimebaseLabel.ABSOLUTE in labels
    debug_candidate = next(
        candidate for candidate in candidates if candidate.label == "REL_DEBUG_HINT"
    )
    assert debug_candidate.anchor_epoch == now - ROTATION_PERIOD


def test_build_timebase_candidates_always_include_absolute_with_rel_anchor() -> None:
    now = ROTATION_PERIOD * 10
    pair_date = now - ROTATION_PERIOD
    identity = DeviceIdentity(
        registry_id="registry-with-pair",
        canonical_id="device-with-pair",
        identity_key=b"\x03",
        pair_date=pair_date,
    )

    candidates = _build_timebase_candidates(
        identity,
        now_unix=now,
    )

    labels = {candidate.label for candidate in candidates}
    assert TimebaseLabel.ABSOLUTE in labels
    assert TimebaseLabel.REL_PAIR in labels


def test_build_timebase_candidates_use_provisioning_for_absolute() -> None:
    now = 1_700_000_000
    anchor_epoch = now - 100_000
    identity = DeviceIdentity(
        registry_id="registry-absolute",
        canonical_id="device-absolute",
        identity_key=b"\x04",
        secrets_creation_date=anchor_epoch,
    )

    candidates = _build_timebase_candidates(
        identity,
        now_unix=now,
    )

    absolute_candidate = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.ABSOLUTE
    )
    relative_candidate = next(
        candidate
        for candidate in candidates
        if candidate.label == TimebaseLabel.REL_SECRETS
    )

    assert absolute_candidate.reference_time == resolver_module._mask_u32(now)  # type: ignore[attr-defined]
    assert relative_candidate.reference_time == now - anchor_epoch
    assert relative_candidate.anchor_epoch == anchor_epoch


def test_build_timebase_candidates_ignores_unparsed_anchor_list() -> None:
    now = ROTATION_PERIOD * 9
    identity = DeviceIdentity(
        registry_id="registry-anchor-list",
        canonical_id="device-anchor-list",
        identity_key=b"\x02",
        time_anchors_debug=[{"unexpected": True}],
    )

    candidates = _build_timebase_candidates(
        identity,
        now_unix=now,
    )

    labels = {candidate.label for candidate in candidates}
    assert labels == {TimebaseLabel.ABSOLUTE}


def test_compute_provisioning_counter_uses_pair_date(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = ROTATION_PERIOD * 20
    pair_date = now - 100
    identity = DeviceIdentity(
        registry_id="registry-pair-anchor",
        canonical_id="device-pair-anchor",
        identity_key=b"\x07",
        pair_date=pair_date,
    )

    with caplog.at_level(logging.DEBUG):
        counter, anchor_label, anchor_epoch = (
            resolver_module._compute_provisioning_counter(identity, now=now)
        )

    assert counter == (now - pair_date) & 0xFFFFFFFF
    assert anchor_label == "pair_date"
    assert anchor_epoch == pair_date
    assert any("Type=pair_date" in record.message for record in caplog.records)


def test_compute_provisioning_counter_prefers_secrets_creation_date(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = ROTATION_PERIOD * 30
    pair_date = now - 400
    secrets_creation_date = now - 10
    identity = DeviceIdentity(
        registry_id="registry-secrets-anchor",
        canonical_id="device-secrets-anchor",
        identity_key=b"\x08",
        pair_date=pair_date,
        secrets_creation_date=secrets_creation_date,
    )

    with caplog.at_level(logging.DEBUG):
        counter, anchor_label, anchor_epoch = (
            resolver_module._compute_provisioning_counter(identity, now=now)
        )

    assert counter == (now - secrets_creation_date) & 0xFFFFFFFF
    assert anchor_label == "secrets_creation_date"
    assert anchor_epoch == secrets_creation_date
    assert any(
        "Type=secrets_creation_date" in record.message for record in caplog.records
    )


def test_compute_provisioning_counter_uses_newer_pair_date_when_secrets_stale(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = ROTATION_PERIOD * 31
    pair_date = now - 25
    secrets_creation_date = now - 100
    identity = DeviceIdentity(
        registry_id="registry-pair-newer",
        canonical_id="device-pair-newer",
        identity_key=b"\x09",
        pair_date=pair_date,
        secrets_creation_date=secrets_creation_date,
    )

    with caplog.at_level(logging.DEBUG):
        counter, anchor_label, anchor_epoch = (
            resolver_module._compute_provisioning_counter(identity, now=now)
        )

    assert counter == (now - pair_date) & 0xFFFFFFFF
    assert anchor_label == "pair_date"
    assert anchor_epoch == pair_date
    assert any("Type=pair_date" in record.message for record in caplog.records)


def test_compute_provisioning_counter_falls_back_to_unix_time(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = ROTATION_PERIOD * 40
    identity = DeviceIdentity(
        registry_id="registry-fallback",
        canonical_id="device-fallback",
        identity_key=b"\x0a",
    )

    with caplog.at_level(logging.DEBUG):
        counter, anchor_label, anchor_epoch = (
            resolver_module._compute_provisioning_counter(identity, now=now)
        )

    assert counter == now
    assert anchor_label == "unix_time"
    assert anchor_epoch == now
    assert any("Type=unix_time" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_active_device_identities_surface_cached_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [_StubDevice("dev-cache", registry_id="registry-cache", custom_fields={})]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-cache")
    coordinator._enabled_poll_device_ids = {"dev-cache"}
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {
        "dev-cache": {
            "identityKey": "abcd",
            "deviceRegistration": {"pairDate": {"seconds": 10}},
            "encrypted_user_secrets": {
                "creation_date": {"seconds": 20, "nanos": 999_000_000}
            },
            "time_anchors_debug": [1, 2, 3],
        }
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.pair_date == 10
    assert identity.secrets_creation_date == 20
    assert identity.time_anchors_debug == [1, 2, 3]


@pytest.mark.asyncio
async def test_active_device_identities_retain_cached_anchors_when_not_polled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [_StubDevice("dev-sleep", registry_id="registry-sleep", custom_fields={})]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-sleep")
    coordinator._enabled_poll_device_ids = set()
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._last_device_list = []
    coordinator._device_location_data = {
        "dev-sleep": {
            "identityKey": b"\xaa" * 32,
            "pairDate": {"seconds": 30},
            "encrypted_user_secrets": {"creationDate": {"seconds": 40, "nanos": 0}},
        }
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.pair_date == 30
    assert identity.secrets_creation_date == 40


@pytest.mark.asyncio
async def test_sleeping_devices_merge_cache_and_registry_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [_StubDevice("dev-sleep", registry_id="registry-sleep", custom_fields={})]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-sleep-merge")
    coordinator._enabled_poll_device_ids = set()
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {
        "registry-sleep": {
            "pair_date": {"seconds": 55, "nanos": 900_000_000},
            "secrets_creation_date": 66,
            "identityKey": "0abc",
        }
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.pair_date == 55
    assert identity.secrets_creation_date == 66


def test_persist_anchor_metadata_records_metadata_only_payload() -> None:
    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator._device_location_data = {}

    payload = {
        "pair_date": 100,
        "secrets_creation_date": 200,
        "metadata_only": True,
        "device_registration": {"pairDate": 100},
        "encrypted_user_secrets": {"creationDate": 200},
        "identity_key": b"\x01" * 32,
        "identity_key_candidates": [b"\x01" * 32],
        "encrypted_identity_key": b"\x02" * 32,
        "owner_key_version": 3,
    }

    coordinator._persist_anchor_metadata("dev-meta", payload, clear_metadata_only=False)

    stored = coordinator._device_location_data["dev-meta"]
    assert stored["pair_date"] == 100
    assert stored["secrets_creation_date"] == 200
    assert stored.get("metadata_only") is True
    assert stored["device_registration"] == {"pairDate": 100}
    assert stored["encrypted_user_secrets"] == {"creationDate": 200}
    assert stored["identity_key"] == b"\x01" * 32
    assert stored["identity_key_candidates"] == [b"\x01" * 32]
    assert stored["encrypted_identity_key"] == b"\x02" * 32
    assert stored["owner_key_version"] == 3


def test_persist_anchor_metadata_updates_device_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-anchor",
                registry_id="registry-anchor",
                custom_fields={"pair_date": 1, "identity_key": "00"},
            )
        ]
    )
    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator._device_location_data = {}

    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda _hass: registry)

    payload = {
        "pair_date": 2,
        "secrets_creation_date": 3,
        "identity_key": b"\x01" * 32,
    }

    coordinator._persist_anchor_metadata(
        "dev-anchor", payload, clear_metadata_only=False
    )

    updated_device = registry.async_get_device(
        identifiers={(DOMAIN, "dev-anchor")}
    )
    assert updated_device is not None
    assert updated_device.custom_fields == {
        "pair_date": 2,
        "secrets_creation_date": 3,
        "identity_key": (b"\x01" * 32).hex(),
    }


def test_persist_anchor_metadata_ignores_nanos_only_payload() -> None:
    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator._device_location_data = {
        "dev-meta": {"pair_date": 100, "secrets_creation_date": 200}
    }

    nanos_only_payload = {
        "pair_date": {"nanos": 500_000_000},
        "secrets_creation_date": {"nsec": 750_000_000},
    }

    coordinator._persist_anchor_metadata(
        "dev-meta", nanos_only_payload, clear_metadata_only=False
    )

    stored = coordinator._device_location_data["dev-meta"]
    assert stored["pair_date"] == 100
    assert stored["secrets_creation_date"] == 200

    fresh_coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    fresh_coordinator._device_location_data = {}

    fresh_coordinator._persist_anchor_metadata(
        "dev-new", nanos_only_payload, clear_metadata_only=False
    )

    assert fresh_coordinator._device_location_data == {}


def test_update_device_cache_preserves_anchor_metadata() -> None:
    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator._device_location_data = {
        "dev-meta": {
            "pair_date": 100,
            "secrets_creation_date": 200,
            "metadata_only": True,
            "last_seen": 1,
            "source_rank": 1,
            "status": "baseline",
            "identity_key": b"\x03" * 32,
            "identity_key_candidates": [b"\x03" * 32],
            "encrypted_identity_key": b"\x04" * 32,
            "encrypted_identity_key_candidates": [b"\x04" * 32],
            "owner_key_version": 7,
            "encrypted_user_secrets": {"creationDate": 111},
            "device_registration": {"pairDate": 100},
        }
    }
    coordinator._record_semantic_label = lambda *args, **kwargs: None
    coordinator._apply_semantic_mapping = lambda *args, **kwargs: None
    coordinator._apply_weighted_location_fusion = lambda *args, **kwargs: True
    coordinator._apply_report_type_cooldown = lambda *args, **kwargs: None
    coordinator._is_on_hass_loop = lambda: True
    coordinator._is_significant_update = lambda *args, **kwargs: True
    coordinator.increment_stat = lambda *args, **kwargs: None
    coordinator._normalize_identity_key = lambda value: value
    coordinator._ensure_device_name_cache = lambda: {}
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None

    coordinator.update_device_cache(
        "dev-meta",
        {
            "status": "updated",
            "last_seen": 2,
        },
    )

    updated = coordinator._device_location_data["dev-meta"]
    assert updated["pair_date"] == 100
    assert updated["secrets_creation_date"] == 200
    assert updated["metadata_only"] is True
    assert updated["identity_key"] == b"\x03" * 32
    assert updated["identity_key_candidates"] == [b"\x03" * 32]
    assert updated["encrypted_identity_key"] == b"\x04" * 32
    assert updated["encrypted_identity_key_candidates"] == [b"\x04" * 32]
    assert updated["owner_key_version"] == 7
    assert updated["encrypted_user_secrets"] == {"creationDate": 111}
    assert updated["device_registration"] == {"pairDate": 100}


def test_update_device_cache_clears_metadata_only_when_locations_arrive() -> None:
    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator._device_location_data = {
        "dev-meta": {
            "pair_date": 100,
            "metadata_only": True,
            "identity_key": b"\x05" * 32,
        }
    }
    coordinator._record_semantic_label = lambda *args, **kwargs: None
    coordinator._apply_semantic_mapping = lambda *args, **kwargs: None
    coordinator._apply_weighted_location_fusion = lambda *args, **kwargs: True
    coordinator._apply_report_type_cooldown = lambda *args, **kwargs: None
    coordinator._is_on_hass_loop = lambda: True
    coordinator._is_significant_update = lambda *args, **kwargs: True
    coordinator.increment_stat = lambda *args, **kwargs: None
    coordinator._normalize_identity_key = lambda value: value
    coordinator._ensure_device_name_cache = lambda: {}
    coordinator._run_on_hass_loop = lambda *args, **kwargs: None

    coordinator.update_device_cache(
        "dev-meta",
        {
            "latitude": 1.0,
            "longitude": 2.0,
            "status": "updated",
        },
    )

    updated = coordinator._device_location_data["dev-meta"]
    assert updated.get("metadata_only") is None
    assert updated["pair_date"] == 100
    assert updated["identity_key"] == b"\x05" * 32


def test_metadata_only_snapshot_preserved() -> None:
    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator._device_location_data = {
        "dev-meta": {
            "pair_date": 100,
            "metadata_only": True,
            "latitude": 12.0,
            "longitude": 34.0,
            "status": "Anchor metadata cached",
        }
    }
    coordinator.location_poll_interval = 30.0

    snapshot = coordinator._build_snapshot_from_cache(
        [{"id": "dev-meta", "name": "Meta"}], wall_now=0
    )

    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry["metadata_only"] is True
    assert entry["latitude"] == 12.0
    assert entry["longitude"] == 34.0
    assert entry["status"] == "Anchor metadata cached"


@pytest.mark.asyncio
async def test_resolve_eid_schedules_refresh_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _build_resolver()
    scheduled: list[asyncio.Task] = []
    refresh_calls: list[int] = []

    async def _fake_refresh(self: resolver_module.GoogleFindMyEIDResolver) -> None:
        refresh_calls.append(1)
        resolver._pending_refresh = False

    def _fake_create_task(coro: asyncio.Future) -> asyncio.Task:
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    resolver.hass = SimpleNamespace(async_create_task=_fake_create_task)
    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver, "async_refresh", _fake_refresh
    )
    resolver._lookup = {}

    first = resolver.resolve_eid(b"\x00" * EID_LENGTH)
    second = resolver.resolve_eid(b"\x01" * EID_LENGTH)

    assert first is None
    assert second is None
    assert resolver._pending_refresh is True
    assert len(scheduled) == 1

    await asyncio.gather(*scheduled)
    assert len(refresh_calls) == 1
    assert resolver._pending_refresh is False


@pytest.mark.asyncio
async def test_resolve_eid_parses_fhna_legacy_service_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _build_resolver()
    legacy_eid = b"L" * resolver_module.EID_LENGTH
    modern_eid = b"M" * resolver_module.MODERN_EID_LENGTH

    def _fake_cached_candidates(
        identity_key: bytes, timestamp: int
    ) -> tuple[EidCandidate, ...]:
        return (
            EidCandidate(name="fhna_secp160r1_rx20", eid=legacy_eid),
            EidCandidate(name="fhna_secp256r1_rx32", eid=modern_eid),
        )

    monkeypatch.setattr(resolver_module, "_cached_candidates", _fake_cached_candidates)

    match = EIDMatch("device-legacy", "entry-legacy", "canonical-legacy", 0, False)
    resolver._lookup[legacy_eid] = match

    payload = bytearray(29)
    payload[7] = 0x40
    payload[8:28] = legacy_eid
    payload[28] = 0xAA

    assert resolver.resolve_eid(bytes(payload)) == match


def test_resolve_eid_parses_fhna_modern_service_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = _build_resolver()
    legacy_eid = b"L" * resolver_module.EID_LENGTH
    modern_eid = b"M" * resolver_module.MODERN_EID_LENGTH

    def _fake_cached_candidates(
        identity_key: bytes, timestamp: int
    ) -> tuple[EidCandidate, ...]:
        return (
            EidCandidate(name="fhna_secp160r1_rx20", eid=legacy_eid),
            EidCandidate(name="fhna_secp256r1_rx32", eid=modern_eid),
        )

    monkeypatch.setattr(resolver_module, "_cached_candidates", _fake_cached_candidates)

    match = EIDMatch("device-modern", "entry-modern", "canonical-modern", 0, False)
    resolver._lookup[modern_eid] = match

    payload = bytearray(41)
    payload[7] = 0x41
    payload[8:40] = modern_eid
    payload[40] = 0xBB

    assert resolver.resolve_eid(bytes(payload)) == match


@pytest.mark.asyncio
async def test_provisioning_counters_used_for_timebases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 10_000
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return base_time

    def _recording_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    def _recording_generate_eid_p256(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_p256_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _recording_generate_eid)
    monkeypatch.setattr(
        resolver_module, "generate_eid_p256", _recording_generate_eid_p256
    )
    monkeypatch.setattr(
        resolver_module, "generate_eid_p256_le", _recording_generate_eid_p256
    )

    identity = DeviceIdentity(
        registry_id="registry-counter",
        canonical_id="device-counter",
        identity_key=b"\x0b" * resolver_module.EIK_LENGTH,
        config_entry_id="entry-counter",
        pair_date=base_time - 120,
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-counter": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass
    cache_clear = getattr(resolver_module._cached_candidates, "cache_clear", None)
    if callable(cache_clear):
        cache_clear()

    await resolver._refresh_cache()

    assert recorded_timestamps
    assert all(ts < 5_000_000 for ts in recorded_timestamps)


@pytest.mark.asyncio
async def test_resolver_refreshes_all_rotation_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 2050
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    identity = DeviceIdentity(
        registry_id="registry-4",
        canonical_id="device-1",
        identity_key=b"\x01\x02",
        config_entry_id="entry-4",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-4": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = _build_resolver()
    resolver.hass = hass

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    expected_windows = resolver_module.iter_rotation_windows(
        rotation_start,
        rotation_period=ROTATION_PERIOD,
        window_range=resolver_module.NARROW_SCAN_RANGE,
        include_neighbors=False,
        allow_negative=False,
    )
    assert set(expected_windows).issubset(set(recorded_timestamps))
    assert len(recorded_timestamps) >= len(expected_windows)

    expected_eid = _fixed_length_eid(identity.identity_key, rotation_start)
    match = resolver.resolve_eid(expected_eid)
    assert match is not None
    assert match.device_id == "registry-4"
    assert match.canonical_id == "device-1"
    assert expected_eid[::-1] in resolver._lookup
    assert len(resolver._lookup) >= len(expected_windows)
    assert resolver.get_resolved_eid(expected_eid) == "registry-4"
    assert resolver.resolve_eid(b"unknown") is None
    assert resolver.get_resolved_eid(b"unknown") is None


@pytest.mark.asyncio
async def test_time_offset_uses_candidate_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 1_700_000_000
    anchor_epoch = base_time - 86400

    monkeypatch.setattr(resolver_module.time, "time", lambda: base_time)
    monkeypatch.setattr(
        resolver_module,
        "_cached_candidates",
        lambda key, timestamp: (
            EidCandidate(name="test", eid=_fixed_length_eid(key, timestamp)),
        ),
    )

    identity = DeviceIdentity(
        registry_id="registry-offset",
        canonical_id="device-offset",
        identity_key=b"\x0b",
        config_entry_id="entry-offset",
        secrets_creation_date=anchor_epoch,
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-offset": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass

    await resolver._refresh_cache()

    offsets_by_label: dict[str, list[int]] = {}
    for metadata in resolver._lookup_metadata.values():
        history = metadata.get("timebases", [metadata])
        for entry in history:
            label = str(entry.get("timebase"))
            offsets_by_label.setdefault(label, []).append(entry.get("time_offset", 0))

    assert TimebaseLabel.ABSOLUTE in offsets_by_label
    assert TimebaseLabel.REL_SECRETS in offsets_by_label

    for label, offsets in offsets_by_label.items():
        assert any(abs(offset) < ROTATION_PERIOD for offset in offsets), label


@pytest.mark.asyncio
async def test_negative_windows_processed_when_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 100
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return base_time

    def _fake_cached_candidates(
        identity_key: bytes, timestamp: int
    ) -> tuple[EidCandidate, ...]:
        recorded_timestamps.append(timestamp)
        return (EidCandidate(name="legacy", eid=b"\x00" * EID_LENGTH),)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "_cached_candidates", _fake_cached_candidates)

    identity = DeviceIdentity(
        registry_id="registry-neg",
        canonical_id="device-neg",
        identity_key=b"\x01\x02",
        config_entry_id="entry-neg",
        pair_date=base_time + ROTATION_PERIOD,
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-neg": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = _build_resolver()
    resolver.hass = hass

    await resolver._refresh_cache()

    assert any(timestamp > 2**31 for timestamp in recorded_timestamps)


@pytest.mark.asyncio
async def test_resolver_aggregates_multiple_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 4096

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    identity_one = DeviceIdentity(
        registry_id="registry-1",
        canonical_id="can-1",
        identity_key=b"\x01",
        config_entry_id="entry-a",
    )
    identity_two = DeviceIdentity(
        registry_id="registry-2",
        canonical_id="can-2",
        identity_key=b"\x02",
        config_entry_id="entry-b",
    )

    coordinator_one = SimpleNamespace(
        get_active_device_identities=lambda: [identity_one]
    )
    coordinator_two = SimpleNamespace(
        get_active_device_identities=lambda: [identity_two]
    )
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {
                    "entry-a": SimpleNamespace(coordinator=coordinator_one),
                    "entry-b": SimpleNamespace(coordinator=coordinator_two),
                }
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    eid_one = _fixed_length_eid(identity_one.identity_key, rotation_start)
    eid_two = _fixed_length_eid(identity_two.identity_key, rotation_start)

    assert resolver.resolve_eid(eid_one).config_entry_id == "entry-a"
    assert resolver.resolve_eid(eid_two).config_entry_id == "entry-b"


@pytest.mark.asyncio
async def test_resolver_excludes_disabled_or_ignored_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "dev-1",
                registry_id="registry-1",
                custom_fields={"identity_key": "abcd"},
                disabled=True,
            ),
            _StubDevice(
                "dev-2",
                registry_id="registry-2",
                custom_fields={"identity_key": "dcba"},
            ),
        ]
    )
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-ignore")
    coordinator._enabled_poll_device_ids = {"dev-1", "dev-2"}
    coordinator._get_ignored_set = lambda: {"dev-2"}
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}

    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-ignore": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    assert resolver._lookup == {}


@pytest.mark.asyncio
async def test_concurrent_refresh_requests_are_serialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = 8192

    def _fake_time() -> int:
        return base_time

    async def _delayed_refresh(_: float) -> None:
        await asyncio.sleep(0)

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    # Defensive: keep refresh serialization stable even if future implementations
    # introduce awaits inside the cache rebuild. The current refresh path does not
    # sleep, but the shim ensures concurrent refreshes would still join correctly.
    monkeypatch.setattr(
        resolver_module.asyncio, "sleep", lambda delay: _delayed_refresh(delay)
    )

    identity = DeviceIdentity(
        registry_id="registry-lock",
        canonical_id="canonical-lock",
        identity_key=b"\x0a",
        config_entry_id="entry-lock",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-lock": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await asyncio.gather(
        resolver.async_refresh(), resolver.async_refresh(), resolver.async_refresh()
    )

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    expected_eid = _fixed_length_eid(identity.identity_key, rotation_start)
    assert expected_eid in resolver._lookup


@pytest.mark.asyncio
async def test_resolver_learns_offsets_and_endianness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 200
    current_time = base_time
    generated: list[int] = []

    def _fake_time() -> int:
        return current_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        generated.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    identity = DeviceIdentity(
        registry_id="registry-learn",
        canonical_id="canonical-learn",
        identity_key=b"\x0b",
        config_entry_id="entry-learn",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-learn": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    expected_windows = resolver_module.iter_rotation_windows(
        rotation_start,
        rotation_period=ROTATION_PERIOD,
        window_range=resolver_module.NARROW_SCAN_RANGE,
        include_neighbors=False,
        allow_negative=False,
    )
    assert set(expected_windows).issubset(set(generated))
    assert len(generated) >= len(expected_windows)
    target_timestamp = rotation_start + (ROTATION_PERIOD * 2)
    expected_eid = _fixed_length_eid(identity.identity_key, target_timestamp)
    assert expected_eid in resolver._lookup

    match = resolver.resolve_eid(expected_eid[::-1])
    assert match is not None
    assert match.is_reversed is True
    assert resolver._known_endianness[identity.registry_id] is True
    assert resolver._known_offsets[identity.registry_id] == target_timestamp - base_time

    generated.clear()
    resolver._lookup = {}
    current_time = base_time + ROTATION_PERIOD

    await resolver._refresh_cache()

    target_rotation = current_time + resolver._known_offsets[identity.registry_id]
    target_rotation -= target_rotation % ROTATION_PERIOD
    expected_windows = {
        target_rotation,
        max(0, target_rotation - ROTATION_PERIOD),
        target_rotation + ROTATION_PERIOD,
    }
    generated_set = set(generated)
    assert target_rotation in generated_set
    assert expected_windows.intersection(generated_set)
    candidate_count = len(
        resolver_module._cached_candidates(identity.identity_key, target_rotation)
    )
    assert len(resolver._lookup) >= candidate_count
    assert all(match.is_reversed for match in resolver._lookup.values())


@pytest.mark.asyncio
async def test_resolver_populates_modern_and_legacy_eids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 2

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        return (timestamp.to_bytes(8, "big") * 3)[:EID_LENGTH]

    def _fake_generate_eid_p256(key: bytes, timestamp: int) -> bytes:
        return (b"m" + timestamp.to_bytes(8, "big") * 4)[:32]

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    monkeypatch.setattr(resolver_module, "generate_eid_p256", _fake_generate_eid_p256)
    monkeypatch.setattr(
        resolver_module, "generate_eid_p256_le", _fake_generate_eid_p256
    )

    identity = DeviceIdentity(
        registry_id="registry-modern",
        canonical_id="canonical-modern",
        identity_key=b"\x01" * 32,
        config_entry_id="entry-modern",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-modern": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._known_offsets = {identity.registry_id: 0}
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    legacy_eid = _fake_generate_eid(identity.identity_key, rotation_start)
    modern_eid_full = _fake_generate_eid_p256(identity.identity_key, rotation_start)
    modern_eid_truncated = modern_eid_full[:EID_LENGTH]

    assert len(resolver._lookup) == 21
    assert resolver.resolve_eid(legacy_eid).device_id == identity.registry_id
    assert resolver.resolve_eid(modern_eid_full).device_id == identity.registry_id
    assert resolver.resolve_eid(modern_eid_truncated).device_id == identity.registry_id


@pytest.mark.asyncio
async def test_resolve_eid_logs_variant_and_reversal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base_time = ROTATION_PERIOD * 4

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        return (timestamp.to_bytes(8, "big") * 3)[:EID_LENGTH]

    def _fake_generate_eid_p256(key: bytes, timestamp: int) -> bytes:
        return (b"m" + timestamp.to_bytes(8, "big") * 4)[:32]

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    monkeypatch.setattr(resolver_module, "generate_eid_p256", _fake_generate_eid_p256)

    identity = DeviceIdentity(
        registry_id="registry-modern",  # noqa: S106 - test identifier
        canonical_id="canonical-modern",
        identity_key=b"\x02" * 32,
        pair_date=base_time - 90,
        config_entry_id="entry-modern",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-modern": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    caplog.set_level(logging.INFO)
    await resolver._refresh_cache()

    lookup_metadata = getattr(resolver, "_lookup_metadata", {})
    reversed_tail_entry = next(
        (
            (key, meta)
            for key, meta in lookup_metadata.items()
            if meta.get("variant") == "fhna_p256_truncated_tail_rx20"
            and meta.get("is_reversed") is True
            and meta.get("timebase") != TimebaseLabel.ABSOLUTE
        ),
        None,
    )
    assert reversed_tail_entry is not None

    eid_bytes, metadata = reversed_tail_entry
    assert metadata.get("timebase") in {
        TimebaseLabel.REL_PAIR,
        TimebaseLabel.REL_SECRETS,
    }

    match = resolver.resolve_eid(eid_bytes)

    assert match is not None
    assert match.is_reversed is True
    assert any(
        "variant=fhna_p256_truncated_tail_rx20" in record.message
        and "timebase" in record.message
        for record in caplog.records
    )


def test_resolve_eid_slices_fmdn_frame() -> None:
    resolver = _build_resolver()

    expected_eid = b"\x01" * EID_LENGTH
    match = EIDMatch("device-1", "entry-1", "canonical-1", 0, False)
    resolver._lookup[expected_eid] = match

    framed_payload = bytes([resolver_module.FMDN_FRAME_TYPE]) + expected_eid + b"\x99"

    result = resolver.resolve_eid(framed_payload)

    assert result == match


def test_resolve_eid_handles_fhna_service_data_frames() -> None:
    resolver = _build_resolver()

    legacy_eid = b"L" * EID_LENGTH
    modern_eid = b"M" * resolver_module.MODERN_EID_LENGTH

    legacy_match = EIDMatch("dev-legacy", "entry-legacy", "canon-legacy", 0, False)
    modern_match = EIDMatch("dev-modern", "entry-modern", "canon-modern", 0, False)
    resolver._lookup[legacy_eid] = legacy_match
    resolver._lookup[modern_eid] = modern_match

    legacy_payload = bytearray(29)
    legacy_payload[7] = resolver_module.FHNA_FRAME_TYPE_LEGACY
    legacy_payload[8:28] = legacy_eid
    legacy_payload[28] = 0xAA

    modern_payload = bytearray(45)
    modern_payload[7] = resolver_module.FHNA_FRAME_TYPE_MODERN
    modern_payload[8:40] = modern_eid
    modern_payload[40] = 0xBB
    modern_payload[44] = 0xCC

    assert resolver.resolve_eid(bytes(legacy_payload)) == legacy_match
    assert resolver.resolve_eid(bytes(modern_payload)) == modern_match


def test_resolve_eid_accepts_raw_20_byte_payloads() -> None:
    resolver = _build_resolver()

    expected_eid = b"\x02" * EID_LENGTH
    match = EIDMatch("device-2", "entry-2", "canonical-2", 0, False)
    resolver._lookup[expected_eid] = match

    result = resolver.resolve_eid(expected_eid)

    assert result == match


def test_resolve_eid_accepts_raw_32_byte_payloads() -> None:
    resolver = _build_resolver()

    expected_eid = b"\x03" * resolver_module.MODERN_EID_LENGTH
    match = EIDMatch("device-3", "entry-3", "canonical-3", 0, False)
    resolver._lookup[expected_eid] = match

    result = resolver.resolve_eid(expected_eid)

    assert result == match


def test_resolve_eid_rejects_unexpected_lengths(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _build_resolver()

    caplog.set_level("DEBUG")

    assert resolver.resolve_eid(b"\x40\x01") is None
    assert any("Unexpected EID length" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_aesgcm_identity_key_unwrap_prefers_valid_shared_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext_key = b"\xaa" * 32
    nonce = b"\x00" * 12
    shared_key = b"\x11" * 32
    wrong_owner_key = b"\x22" * 32

    envelope = nonce + AESGCM(shared_key).encrypt(nonce, plaintext_key, b"")

    resolver = _build_resolver()
    identity = DeviceIdentity(
        registry_id="registry-envelope",
        canonical_id="canonical-envelope",
        identity_key=None,
        encrypted_identity_key=envelope,
        config_entry_id="entry-envelope",
    )

    cache = SimpleNamespace()

    async def _fake_owner_key(cache: object) -> resolver_module.OwnerKeyInfo:
        return resolver_module.OwnerKeyInfo(wrong_owner_key, 1)

    async def _fake_shared_key(cache: object) -> bytes:
        return shared_key

    def _raise_invalid_tag(key: bytes, blob: bytes) -> bytes:
        raise InvalidTag("unwrap failed")

    monkeypatch.setattr(resolver_module, "async_get_owner_key", _fake_owner_key)
    monkeypatch.setattr(resolver_module, "async_get_shared_key", _fake_shared_key)
    monkeypatch.setattr(resolver_module, "decrypt_eik", _raise_invalid_tag)

    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result.key == plaintext_key
    assert result.metadata["status"] == "decrypted"
    assert result.metadata["mode"] == "aesgcm_envelope"
    assert result.metadata["key_source"] == "shared"
    assert result.metadata.get("key_sources") == ["owner", "shared"]


@pytest.mark.asyncio
async def test_aesgcm_unwrap_failure_falls_back_to_owner_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plaintext_key = b"\xbb" * 32
    bogus_envelope = b"\x00" * 60
    owner_key = b"\x33" * 32
    decrypt_calls = 0

    resolver = _build_resolver()
    identity = DeviceIdentity(
        registry_id="registry-fallback",
        canonical_id="canonical-fallback",
        identity_key=None,
        encrypted_identity_key=bogus_envelope,
        config_entry_id="entry-fallback",
    )

    cache = SimpleNamespace()

    async def _fake_owner_key(cache: object) -> resolver_module.OwnerKeyInfo:
        return resolver_module.OwnerKeyInfo(owner_key, 1)

    async def _fake_shared_key(cache: object) -> bytes:
        return owner_key

    def _fake_decrypt(key: bytes, blob: bytes) -> bytes:
        nonlocal decrypt_calls
        decrypt_calls += 1
        return plaintext_key

    def _raise_invalid_tag(
        self: resolver_module.AESGCM, *_: object, **__: object
    ) -> bytes:
        raise InvalidTag("unwrap failed")

    monkeypatch.setattr(resolver_module, "async_get_owner_key", _fake_owner_key)
    monkeypatch.setattr(resolver_module, "async_get_shared_key", _fake_shared_key)
    monkeypatch.setattr(resolver_module, "decrypt_eik", _fake_decrypt)
    monkeypatch.setattr(resolver_module.AESGCM, "decrypt", _raise_invalid_tag)

    result = await resolver._try_decrypt_identity_key(identity, cache=cache)

    assert result.key == plaintext_key
    assert result.metadata["status"] == "decrypted"
    assert result.metadata["mode"] == "owner_key"
    assert result.metadata["key_source"] == "owner"
    assert decrypt_calls == 1


@pytest.mark.asyncio
async def test_rel_pair_timebase_lock_reduces_scan_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 10
    current_time = base_time
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return current_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    def _fake_generate_eid_p256(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return (_fixed_length_eid(key, timestamp) + b"p256").ljust(32, b"p")

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    monkeypatch.setattr(resolver_module, "generate_eid_p256", _fake_generate_eid_p256)

    pair_date = base_time + 5
    identity = DeviceIdentity(
        registry_id="registry-lock",
        canonical_id="device-lock",
        identity_key=b"\x01\x02",
        config_entry_id="entry-lock",
        pair_date=pair_date,
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-lock": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    assert recorded_timestamps

    counter_eids = [
        (eid, meta)
        for eid, meta in resolver._lookup_metadata.items()
        if str(meta.get("timestamp_basis", "")).startswith("counter")
    ]
    assert counter_eids

    best_eid, best_meta = min(
        counter_eids,
        key=lambda item: abs(int(item[1].get("time_offset", 0))),
    )

    match = resolver.resolve_eid(best_eid)
    assert match is not None
    lock = resolver._known_timebases.get(identity.registry_id)
    assert lock is not None
    assert lock.label == str(best_meta.get("timebase"))
    anchor_epoch = best_meta.get("anchor_epoch")
    if anchor_epoch is not None:
        assert lock.anchor_epoch == anchor_epoch
    else:
        assert lock.anchor_epoch is None
    assert abs(lock.offset) < ROTATION_PERIOD

    recorded_timestamps.clear()
    current_time = base_time + ROTATION_PERIOD

    await resolver._refresh_cache()

    lock_rotation = resolver._known_timebases[identity.registry_id].rotation_timestamp
    expected_neighbors = {
        resolver_module._mask_u32(lock_rotation + (offset * ROTATION_PERIOD))
        for offset in range(0, 3)
    }
    rel_fallback_rotations = {
        candidate.reference_time - (candidate.reference_time % ROTATION_PERIOD)
        for candidate in resolver_module._build_timebase_candidates(identity, now_unix=current_time)
        if candidate.label in {TimebaseLabel.REL_PAIR, TimebaseLabel.REL_SECRETS}
    }
    rel_neighbor_rotations = set(rel_fallback_rotations)
    for rotation in rel_fallback_rotations:
        for neighbor in (rotation - ROTATION_PERIOD, rotation + ROTATION_PERIOD):
            if neighbor >= 0:
                rel_neighbor_rotations.add(neighbor)

    # Safety net: future-dated anchors can yield rotation window 0 when allow_negative=True.
    assert set(recorded_timestamps) <= expected_neighbors | rel_neighbor_rotations | {0}


@pytest.mark.asyncio
async def test_absolute_timebase_lock_narrows_deep_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 12
    current_time = base_time
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return current_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    def _fake_generate_eid_p256(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return (_fixed_length_eid(key, timestamp) + b"modern").ljust(32, b"m")

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    monkeypatch.setattr(resolver_module, "generate_eid_p256", _fake_generate_eid_p256)
    monkeypatch.setattr(resolver_module, "generate_eid_p256_le", _fake_generate_eid_p256)

    identity = DeviceIdentity(
        registry_id="registry-abs-lock",
        canonical_id="device-abs-lock",
        identity_key=b"\x05\x06",
        config_entry_id="entry-abs-lock",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-abs-lock": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    absolute_candidates = [
        (eid, meta)
        for eid, meta in resolver._lookup_metadata.items()
        if meta.get("timebase") == TimebaseLabel.ABSOLUTE
    ]
    assert absolute_candidates

    target_eid, target_metadata = max(
        absolute_candidates,
        key=lambda item: abs(int(item[1].get("time_offset", 0))),
    )

    match = resolver.resolve_eid(target_eid)
    assert match is not None

    lock = resolver._known_timebases.get(identity.registry_id)
    assert lock is not None
    assert lock.label == TimebaseLabel.ABSOLUTE
    assert lock.offset == int(target_metadata.get("time_offset"))
    assert lock.variant == target_metadata.get("variant")

    recorded_timestamps.clear()
    current_time = base_time + ROTATION_PERIOD

    await resolver._refresh_cache()

    known_offset = resolver._known_offsets[identity.registry_id]
    target_time = current_time + known_offset
    expected_neighbors = set(
        resolver_module.iter_rotation_windows(
            target_time,
            rotation_period=ROTATION_PERIOD,
            window_range=range(0, 1),
            include_neighbors=True,
            allow_negative=False,
        )
    )
    assert set(recorded_timestamps) <= expected_neighbors


@pytest.mark.asyncio
async def test_debug_dump_logs_all_variants(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    base_time = ROTATION_PERIOD * 6

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        return _fixed_length_eid(key, timestamp)

    def _fake_generate_eid_p256(key: bytes, timestamp: int) -> bytes:
        return (b"m" + timestamp.to_bytes(8, "big") * 4)[:32]

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)
    monkeypatch.setattr(resolver_module, "generate_eid_p256", _fake_generate_eid_p256)
    monkeypatch.setattr(
        resolver_module, "generate_eid_p256_le", _fake_generate_eid_p256
    )
    monkeypatch.setattr(resolver_module, "NARROW_SCAN_RANGE", range(0, 1))

    identity = DeviceIdentity(
        registry_id="registry-debug",
        canonical_id="device-d329",
        identity_key=b"\x01" * 32,
        config_entry_id="entry-debug",
        pair_date=base_time - ROTATION_PERIOD,
        secrets_creation_date=base_time - (2 * ROTATION_PERIOD),
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-debug": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = _build_resolver()
    resolver.hass = hass
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._decryption_status = {identity.canonical_id: {"status": "pending"}}

    with caplog.at_level(logging.DEBUG):
        await resolver._refresh_cache()

    debug_messages = [
        message for message in caplog.messages if message.startswith("DEBUG DUMP:")
    ]
    assert len(debug_messages) >= 3
    assert any("Variant=fhna_secp160r1_rx20" in message for message in debug_messages)
    assert any("Variant=fhna_secp256r1_rx32" in message for message in debug_messages)
    assert any("Variant=fhna_p256_le_rx32" in message for message in debug_messages)
    assert any("Timebase=REL_PAIR" in message for message in debug_messages)
    assert all("EID_PREFIX=" in message for message in debug_messages)


def test_generate_eid_rejects_negative_timestamp() -> None:
    """Resolver should surface invalid negative timestamps instead of coercing."""

    key = b"\x02" * 32

    with pytest.raises(ValueError):
        resolver_module._cached_candidates(key, -1)


def test_get_masked_timestamp_rejects_negative_input() -> None:
    with pytest.raises(ValueError):
        get_masked_timestamp(-1, K)


def test_get_masked_timestamp_warns_when_coercing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        masked = get_masked_timestamp(-1, K, strict=False)

    expected = ((-1 & 0xFFFFFFFF) & ((~((1 << K) - 1)) & 0xFFFFFFFF)).to_bytes(
        4, "big", signed=False
    )

    assert masked == expected
    assert "coercing via & 0xFFFFFFFF" in caplog.text


@pytest.mark.asyncio
async def test_restores_persisted_timebase_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 12
    current_time = base_time
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return current_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    lock_payload = {
        "label": TimebaseLabel.REL_PAIR,
        "anchor_epoch": base_time - ROTATION_PERIOD,
        "rotation_timestamp": base_time - ROTATION_PERIOD,
        "time_offset": -ROTATION_PERIOD,
        "is_reversed": True,
    }

    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "device-lock",
                registry_id="registry-lock",
                custom_fields={LOCK_CUSTOM_FIELD: lock_payload},
            )
        ]
    )
    monkeypatch.setattr(resolver_module.dr, "async_get", lambda hass: registry)

    identity = DeviceIdentity(
        registry_id="registry-lock",
        canonical_id="device-lock",
        identity_key=b"\x0a\x0b",
        config_entry_id="entry-lock",
    )
    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {DOMAIN: {"entries": {"entry-lock": SimpleNamespace(coordinator=coordinator)}}}
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._persisted_locks = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    lock = resolver._known_timebases.get(identity.registry_id)
    assert lock is not None
    assert lock.label == TimebaseLabel.REL_PAIR
    assert resolver._known_endianness[identity.registry_id] is True

    recorded_timestamps.clear()
    current_time = base_time + ROTATION_PERIOD
    await resolver._refresh_cache()

    rotation_start = current_time - (current_time % ROTATION_PERIOD)
    expected_neighbors = {
        rotation_start,
        max(0, rotation_start - ROTATION_PERIOD),
        max(0, rotation_start - (2 * ROTATION_PERIOD)),
        rotation_start + ROTATION_PERIOD,
    }
    assert set(recorded_timestamps) <= expected_neighbors


@pytest.mark.asyncio
async def test_persists_lock_state_on_match(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "device-persist", registry_id="registry-persist", custom_fields={}
            )
        ]
    )
    monkeypatch.setattr(resolver_module.dr, "async_get", lambda hass: registry)

    resolver = _build_resolver()
    resolver.hass = _StubHass({})
    now = 12_345
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    metadata = {
        "timebase": TimebaseLabel.REL_PAIR,
        "anchor_epoch": 123,
        "rotation_timestamp": 456,
        "time_offset": -5,
    }
    eid = b"\x00" * EID_LENGTH
    match = EIDMatch(
        device_id="registry-persist",
        config_entry_id="entry-persist",
        canonical_id="device-persist",
        time_offset=-5,
        is_reversed=False,
    )
    resolver._lookup = {eid: match}
    resolver._lookup_metadata = {eid: metadata}

    result = resolver.resolve_eid(eid)
    assert result == match

    await asyncio.sleep(0)

    stored = registry.async_get("registry-persist").custom_fields.get(LOCK_CUSTOM_FIELD)
    assert stored == {
        "label": TimebaseLabel.REL_PAIR,
        "anchor_epoch": 123,
        "rotation_timestamp": 456,
        "time_offset": -5,
        "is_reversed": False,
        "variant": None,
        "confirmed_at": now,
    }


@pytest.mark.asyncio
async def test_refresh_drops_stale_persisted_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = resolver_module.LOCK_STALE_AFTER_SECONDS + 100
    stale_confirmed = now - (resolver_module.LOCK_STALE_AFTER_SECONDS + 1)

    registry = _StubDeviceRegistry(
        [
            _StubDevice(
                "device-stale",
                registry_id="registry-stale",
                custom_fields={
                    LOCK_CUSTOM_FIELD: {
                        "label": TimebaseLabel.REL_PAIR,
                        "anchor_epoch": 100,
                        "rotation_timestamp": ROTATION_PERIOD,
                        "time_offset": -ROTATION_PERIOD,
                        "is_reversed": True,
                        "variant": None,
                        "confirmed_at": stale_confirmed,
                    }
                },
            )
        ]
    )
    monkeypatch.setattr(resolver_module.dr, "async_get", lambda hass: registry)
    monkeypatch.setattr(resolver_module.time, "time", lambda: now)

    resolver = _build_resolver()
    resolver._known_timebases["registry-stale"] = resolver_module._TimebaseLock(
        label=TimebaseLabel.REL_PAIR,
        anchor_epoch=100,
        rotation_timestamp=ROTATION_PERIOD,
        offset=-ROTATION_PERIOD,
    )
    resolver._known_offsets["registry-stale"] = -ROTATION_PERIOD
    resolver._known_endianness["registry-stale"] = True
    resolver._persisted_locks["registry-stale"] = {
        "label": TimebaseLabel.REL_PAIR,
        "anchor_epoch": 100,
        "rotation_timestamp": ROTATION_PERIOD,
        "time_offset": -ROTATION_PERIOD,
        "is_reversed": True,
        "variant": None,
        "confirmed_at": stale_confirmed,
    }
    resolver._last_lock_confirmation["registry-stale"] = stale_confirmed

    identity = DeviceIdentity(
        registry_id="registry-stale",
        canonical_id="device-stale",
        identity_key=b"\x01\x02",
        config_entry_id="entry-stale",
    )
    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    resolver.hass = _StubHass(
        {DOMAIN: {"entries": {"entry-stale": SimpleNamespace(coordinator=coordinator)}}}
    )

    await resolver._refresh_cache()

    assert "registry-stale" not in resolver._known_timebases
    assert "registry-stale" not in resolver._known_offsets
    assert "registry-stale" not in resolver._known_endianness
    assert "registry-stale" not in resolver._persisted_locks


@pytest.mark.asyncio
async def test_skips_deep_scan_when_key_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_time = ROTATION_PERIOD * 4
    recorded_timestamps: list[int] = []

    def _fake_time() -> int:
        return base_time

    def _fake_generate_eid(key: bytes, timestamp: int) -> bytes:
        recorded_timestamps.append(timestamp)
        return _fixed_length_eid(key, timestamp)

    monkeypatch.setattr(resolver_module.time, "time", _fake_time)
    monkeypatch.setattr(resolver_module, "generate_eid", _fake_generate_eid)

    identity = DeviceIdentity(
        registry_id="registry-wrapped",
        canonical_id="device-wrapped",
        identity_key=b"\x01\x02",
        config_entry_id="entry-wrapped",
    )

    coordinator = SimpleNamespace(get_active_device_identities=lambda: [identity])
    hass = _StubHass(
        {
            DOMAIN: {
                "entries": {"entry-wrapped": SimpleNamespace(coordinator=coordinator)}
            }
        }
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_endianness = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {identity.canonical_id: {"status": "wrapped_failed"}}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False

    await resolver._refresh_cache()

    assert recorded_timestamps
    rotation_start = base_time - (base_time % ROTATION_PERIOD)
    expected_min = max(
        0, rotation_start + (min(resolver_module.NARROW_SCAN_RANGE) * ROTATION_PERIOD)
    )
    expected_max = rotation_start + (
        max(resolver_module.NARROW_SCAN_RANGE) * ROTATION_PERIOD
    )
    assert min(recorded_timestamps) == expected_min
    assert max(recorded_timestamps) == expected_max
