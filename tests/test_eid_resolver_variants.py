# tests/test_eid_resolver_variants.py
"""Resolver coverage for explicit EID variants and time-base handling."""

from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    LOCK_TTL_SECONDS,
    MIN_RELATIVE_WINDOW_SIZE,
    EIDGenerationLock,
    GoogleFindMyEIDResolver,
    iter_rotation_windows,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    FHNA_COUNTER_MASK,
    MODERN_EID_LENGTH,
    ROTATION_PERIOD,
    EidVariant,
)


def _build_resolver(monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyEIDResolver:
    _ = monkeypatch
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=lambda coro: asyncio.create_task(coro),
        data={},
    )

    async def _async_noop(payload=None):
        return None

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
    resolver._lock_miss_counts = {}
    resolver._store = SimpleNamespace(async_load=_async_noop, async_save=_async_noop)
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    return resolver


def test_iter_rotation_windows_alignment_and_neighbors() -> None:
    """Rotation windows should align to boundaries and support neighbor expansion."""

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


def test_iter_rotation_windows_skips_negative_neighbors() -> None:
    """Neighbor expansion must never produce negative windows."""

    windows = iter_rotation_windows(
        target_time=0,
        rotation_period=ROTATION_PERIOD,
        window_range=(0,),
        include_neighbors=True,
    )
    assert 0 in windows
    assert ROTATION_PERIOD in windows
    assert all(ts >= 0 for ts in windows)


@pytest.mark.asyncio
async def test_refresh_cache_uses_relative_timebases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative bases must use elapsed time since the anchor."""

    resolver = _build_resolver(monkeypatch)
    base_now = 10_000
    pair_date_anchor = 100
    secrets_anchor = 250
    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\x05" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=pair_date_anchor,
        secrets_creation_date=secrets_anchor,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(time, "time", lambda: float(base_now))
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)

    await resolver._refresh_cache()

    def _collect_windows(label: str) -> list[int]:
        return [
            meta["rotation_timestamp"]
            for meta in resolver._lookup_metadata.values()
            if label in meta.get("timestamp_bases", {meta["timestamp_basis"]})
        ]

    pair_date_windows = _collect_windows("pair_date")
    secrets_windows = _collect_windows("secrets_creation_date")

    def _expected_range(anchor: int) -> tuple[int, int]:
        provisioning_counter = max(0, base_now - anchor)
        drift_seconds = provisioning_counter * 0.00005
        drift_windows = math.ceil(drift_seconds / ROTATION_PERIOD)
        max_window = math.ceil((24 * 60 * 60) / ROTATION_PERIOD)
        total_window = min(MIN_RELATIVE_WINDOW_SIZE + drift_windows, max_window)
        elapsed = base_now - anchor
        rotation_start = elapsed - (elapsed % ROTATION_PERIOD)
        window_delta = total_window * ROTATION_PERIOD
        return rotation_start - window_delta, rotation_start + window_delta

    pair_min, pair_max = _expected_range(pair_date_anchor)
    secrets_min, secrets_max = _expected_range(secrets_anchor)

    assert pair_date_windows
    assert secrets_windows
    assert all(pair_min <= ts <= pair_max for ts in pair_date_windows)
    assert all(secrets_min <= ts <= secrets_max for ts in secrets_windows)


@pytest.mark.asyncio
async def test_refresh_cache_populates_all_variants_and_bases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache refresh should cover both curves, truncation, and byte-order options."""

    resolver = _build_resolver(monkeypatch)
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\x01" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=ROTATION_PERIOD,
        secrets_creation_date=ROTATION_PERIOD * 2,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)

    await resolver._refresh_cache()

    variants = {meta["variant"] for meta in resolver._lookup_metadata.values()}
    assert variants.issuperset(
        {
            EidVariant.LEGACY_SECP160R1_X20_BE.value,
            EidVariant.MODERN_P256_X32_BE.value,
            EidVariant.MODERN_P256_X20_TRUNC_BE.value,
            EidVariant.MODERN_P256_X32_LE_SCALAR.value,
            EidVariant.MODERN_P256_X20_TRUNC_LE.value,
        }
    )

    bases = set[str]()
    for meta in resolver._lookup_metadata.values():
        bases.update(meta.get("timestamp_bases", {meta["timestamp_basis"]}))
    assert {"unix", "pair_date", "secrets_creation_date"}.issubset(bases)


@pytest.mark.asyncio
async def test_refresh_cache_skips_negative_neighbor_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero-valued candidates should not generate negative neighbor windows."""

    resolver = _build_resolver(monkeypatch)
    identity = DeviceIdentity(
        registry_id="zeroed-id",
        canonical_id="canonical-id",
        identity_key=b"\x03" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=0,
        secrets_creation_date=0,
    )

    call_log: list[int] = []
    original_generate = GoogleFindMyEIDResolver._generate_variant

    def _guarded_generate(
        self: GoogleFindMyEIDResolver,
        key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        assert 0 <= time_counter <= FHNA_COUNTER_MASK
        call_log.append(time_counter)
        return original_generate(self, key_bytes, time_counter=time_counter, variant=variant)

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", _guarded_generate)

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(time, "time", lambda: float(ROTATION_PERIOD))

    await resolver._refresh_cache()

    assert call_log
    assert min(call_log) >= 0
    assert all(ts <= FHNA_COUNTER_MASK for ts in call_log)


@pytest.mark.asyncio
async def test_refresh_cache_converts_millisecond_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Millisecond timestamps should normalize to seconds when plausible."""

    resolver = _build_resolver(monkeypatch)
    ms_candidate = 1_700_000_000_000
    identity = DeviceIdentity(
        registry_id="ms-id",
        canonical_id="canonical-id",
        identity_key=b"\x04" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=None,
        secrets_creation_date=ms_candidate,
    )

    call_log: list[int] = []
    original_generate = GoogleFindMyEIDResolver._generate_variant

    def _guarded_generate(
        self: GoogleFindMyEIDResolver,
        key_bytes: bytes,
        *,
        time_counter: int,
        variant: EidVariant,
    ) -> bytes:
        assert 0 <= time_counter <= FHNA_COUNTER_MASK
        call_log.append(time_counter)
        return original_generate(self, key_bytes, time_counter=time_counter, variant=variant)

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", _guarded_generate)

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)

    await resolver._refresh_cache()

    assert call_log
    assert all(ts <= FHNA_COUNTER_MASK for ts in call_log)
    assert any(
        meta["timestamp_basis"] == "secrets_creation_date" and meta["rotation_timestamp"] <= FHNA_COUNTER_MASK
        for meta in resolver._lookup_metadata.values()
    )


@pytest.mark.asyncio
async def test_resolve_eid_persists_variant_and_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver hits should persist the chosen variant and advertisement format."""

    resolver = _build_resolver(monkeypatch)
    monkeypatch.setattr(
        "custom_components.googlefindmy.eid_resolver.ENABLE_ABSOLUTE_UNIX_BASIS",
        True,
    )
    identity = DeviceIdentity(
        registry_id="registry-id",
        canonical_id="canonical-id",
        identity_key=b"\x02" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry-id",
        fast_pair_model_id=None,
        pair_date=None,
        secrets_creation_date=None,
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    await resolver._refresh_cache()

    reversed_entry = next(
        (eid for eid, meta in resolver._lookup_metadata.items() if meta["advertisement_reversed"]),
        None,
    )
    assert reversed_entry is not None
    metadata = resolver._lookup_metadata[reversed_entry]

    match = resolver.resolve_eid(reversed_entry)
    assert match is not None

    lock = resolver._locks[match.device_id]
    assert lock.advertisement_reversed is True
    assert lock.variant == metadata["variant"]
    assert lock.frame_type is None or isinstance(lock.frame_type, int)


@pytest.mark.asyncio
async def test_stale_locks_are_purged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Locks older than the TTL should be removed on refresh."""

    resolver = _build_resolver(monkeypatch)
    stale_created = int(time.time()) - LOCK_TTL_SECONDS - 5
    resolver._locks["stale"] = EIDGenerationLock(
        device_id="stale",
        canonical_id="canonical",
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=MODERN_EID_LENGTH,
        frame_type=None,
        time_basis="unix",
        created_at=stale_created,
    )
    resolver._persisted_locks["stale"] = resolver._locks["stale"]

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return []

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    await resolver._refresh_cache()

    assert "stale" not in resolver._locks
    assert "stale" not in resolver._persisted_locks
