# tests/test_eid_resolver_variants.py
"""Resolver coverage for explicit EID variants and time-base handling."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    LOCK_TTL_SECONDS,
    EIDGenerationLock,
    GoogleFindMyEIDResolver,
    iter_rotation_windows,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    MODERN_EID_LENGTH,
    ROTATION_PERIOD,
    EidVariant,
)


def _build_resolver(monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyEIDResolver:
    _ = monkeypatch
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(async_create_task=lambda coro: asyncio.create_task(coro), data={})
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


@pytest.mark.asyncio
async def test_refresh_cache_populates_all_variants_and_bases(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cache refresh should cover both curves, truncation, and byte-order options."""

    resolver = _build_resolver(monkeypatch)
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
        }
    )

    bases = {meta["timestamp_basis"] for meta in resolver._lookup_metadata.values()}
    assert {"unix", "pair_date", "secrets_creation_date"}.issubset(bases)


@pytest.mark.asyncio
async def test_resolve_eid_persists_variant_and_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver hits should persist the chosen variant and advertisement format."""

    resolver = _build_resolver(monkeypatch)
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
