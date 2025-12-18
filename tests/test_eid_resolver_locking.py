"""Tests for EID resolver lock persistence and relative timebase generation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    CurveLabel,
    GoogleFindMyEIDResolver,
    LOCK_CUSTOM_FIELD,
    LOCK_SCHEMA_VERSION,
    MODERN_EID_LENGTH,
    P256Endian,
    TimebaseLabel,
    _DeviceLock,
    _build_timebase_candidates,
    _parse_persisted_lock,
    _serialize_lock,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import EidCandidate, ROTATION_PERIOD


class _FakeDeviceEntry:
    def __init__(self, device_id: str, custom_fields: Mapping[str, Any] | None = None) -> None:
        self.id = device_id
        self.custom_fields = dict(custom_fields or {})


class _FakeDeviceRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, _FakeDeviceEntry] = {}

    def add(self, device_id: str, *, custom_fields: Mapping[str, Any] | None = None) -> _FakeDeviceEntry:
        entry = _FakeDeviceEntry(device_id, custom_fields)
        self._entries[device_id] = entry
        return entry

    def async_get(self, device_id: str) -> _FakeDeviceEntry | None:
        return self._entries.get(device_id)

    def async_update_device(self, device_id: str, *, custom_fields: Mapping[str, Any] | None = None) -> _FakeDeviceEntry | None:
        entry = self._entries.get(device_id)
        if entry is None:
            return None
        entry.custom_fields = dict(custom_fields or {})
        return entry


def _install_fake_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _cached_candidates with deterministic candidates for unit testing."""
    import custom_components.googlefindmy.eid_resolver as eid_resolver

    def _fake_cached_candidates(identity_key: bytes, timestamp: int) -> tuple[EidCandidate, ...]:
        # Ensure all candidates are unique and timestamp-dependent.
        ts = int(timestamp) & 0xFF
        legacy = bytes([0x11]) * 19 + bytes([ts])
        p256_be = bytes([0x22]) * 31 + bytes([ts])
        p256_le = bytes([0x33]) * 31 + bytes([ts])
        return (
            EidCandidate(name="fhna_secp160r1_rx20", eid=legacy),
            EidCandidate(name="fhna_secp256r1_rx32", eid=p256_be),
            EidCandidate(name="fhna_p256_le_rx32", eid=p256_le),
        )

    _fake_cached_candidates.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(eid_resolver, "_cached_candidates", _fake_cached_candidates)


@pytest.mark.asyncio
async def test_timebase_candidates_are_relative_only(hass: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The resolver must not generate ABSOLUTE (unix-time) candidates."""
    now_unix = 1_700_000_000
    identity = DeviceIdentity(
        registry_id="dev1",
        canonical_id="can1",
        identity_key=b"\x00" * 32,
        config_entry_id="entry1",
        pair_date=now_unix - 1000,
        secrets_creation_date=now_unix - 2000,
    )
    candidates = _build_timebase_candidates(identity, now_unix=now_unix)
    assert candidates, "Expected at least one relative timebase candidate"
    assert all(c.label != TimebaseLabel.ABSOLUTE for c in candidates)


def test_parse_persisted_lock_accepts_schema_v2_and_rejects_legacy() -> None:
    """Only schema v2 locks are accepted; legacy payloads must be rejected."""
    legacy = {"label": "ABSOLUTE", "rotation_timestamp": 123}
    assert _parse_persisted_lock(legacy) is None

    lock = _DeviceLock(
        schema=LOCK_SCHEMA_VERSION,
        timebase=TimebaseLabel.REL_SECRETS,
        rotation_shift=3,
        curve=CurveLabel.P256,
        p256_endian=P256Endian.LITTLE,
        eid_bytes_reversed=False,
        variant="fhna_p256_le_rx32",
        confirmed_at=1_700_000_000,
        hit_count=2,
    )
    payload = _serialize_lock(lock)
    parsed = _parse_persisted_lock(payload)
    assert parsed is not None
    assert parsed.schema == LOCK_SCHEMA_VERSION
    assert parsed.timebase == TimebaseLabel.REL_SECRETS
    assert parsed.rotation_shift == 3
    assert parsed.curve == CurveLabel.P256
    assert parsed.p256_endian == P256Endian.LITTLE
    assert parsed.variant == "fhna_p256_le_rx32"
    assert parsed.hit_count == 2


@pytest.mark.asyncio
async def test_refresh_cache_respects_persisted_lock_variant_filtering(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persisted lock must restrict variants to the locked curve/endian for that device."""
    import custom_components.googlefindmy.eid_resolver as eid_resolver

    _install_fake_candidates(monkeypatch)

    # Prevent background scheduling in tests.
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_start_alignment_timer", lambda self: None)

    fake_registry = _FakeDeviceRegistry()
    now_unix = 1_700_000_000
    anchor_epoch = now_unix - 10 * ROTATION_PERIOD

    persisted_lock = _DeviceLock(
        schema=LOCK_SCHEMA_VERSION,
        timebase=TimebaseLabel.REL_SECRETS,
        rotation_shift=5,
        curve=CurveLabel.P256,
        p256_endian=P256Endian.LITTLE,
        eid_bytes_reversed=False,
        variant="fhna_p256_le_rx32",
        confirmed_at=now_unix,
        hit_count=1,
    )
    fake_registry.add(
        "dev1",
        custom_fields={LOCK_CUSTOM_FIELD: _serialize_lock(persisted_lock)},
    )
    monkeypatch.setattr(eid_resolver.dr, "async_get", lambda _hass: fake_registry)

    resolver = GoogleFindMyEIDResolver(hass)
    identity = DeviceIdentity(
        registry_id="dev1",
        canonical_id="can1",
        identity_key=b"\x00" * 32,
        config_entry_id="entry1",
        secrets_creation_date=anchor_epoch,
        pair_date=anchor_epoch - ROTATION_PERIOD,
    )
    monkeypatch.setattr(resolver, "_collect_device_secrets", AsyncMock(return_value=[identity]))

    # Freeze wall clock.
    monkeypatch.setattr(eid_resolver.time, "time", lambda: now_unix)

    await resolver._refresh_cache()

    assert resolver._lookup, "Lookup must not be empty after refresh"
    assert resolver._lookup_metadata, "Metadata must be populated"

    # All variants must be LE P-256 because of the persisted lock filter.
    variants = {meta.get("variant") for meta in resolver._lookup_metadata.values()}
    assert "fhna_p256_le_rx32" in variants
    assert "fhna_secp256r1_rx32" not in variants
    assert all(meta.get("timebase") != TimebaseLabel.ABSOLUTE for meta in resolver._lookup_metadata.values())


@pytest.mark.asyncio
async def test_lock_is_created_only_on_hit(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolver must not create an in-memory lock until a real HIT is observed."""
    import custom_components.googlefindmy.eid_resolver as eid_resolver

    _install_fake_candidates(monkeypatch)
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_start_alignment_timer", lambda self: None)

    fake_registry = _FakeDeviceRegistry()
    fake_registry.add("dev1", custom_fields={})
    monkeypatch.setattr(eid_resolver.dr, "async_get", lambda _hass: fake_registry)

    now_unix = 1_700_000_000
    anchor_epoch = now_unix - 10 * ROTATION_PERIOD
    monkeypatch.setattr(eid_resolver.time, "time", lambda: now_unix)

    resolver = GoogleFindMyEIDResolver(hass)
    identity = DeviceIdentity(
        registry_id="dev1",
        canonical_id="can1",
        identity_key=b"\x00" * 32,
        config_entry_id="entry1",
        secrets_creation_date=anchor_epoch,
        pair_date=anchor_epoch - ROTATION_PERIOD,
    )
    monkeypatch.setattr(resolver, "_collect_device_secrets", AsyncMock(return_value=[identity]))

    await resolver._refresh_cache()

    assert "dev1" not in resolver._device_locks

    # Pick any cached EID and resolve it.
    any_eid = next(iter(resolver._lookup.keys()))
    match = resolver.resolve_eid(any_eid)
    assert match is not None
    assert match.device_id == "dev1"

    # Lock should be created after the HIT.
    assert "dev1" in resolver._device_locks
    lock = resolver._device_locks["dev1"]
    assert lock.timebase in (TimebaseLabel.REL_SECRETS, TimebaseLabel.REL_PAIR)
    assert lock.curve in (CurveLabel.LEGACY_SECP160R1, CurveLabel.P256)

    # The persisted payload must be schema v2 (if registry supports custom_fields).
    entry = fake_registry.async_get("dev1")
    assert entry is not None
    payload = entry.custom_fields.get(LOCK_CUSTOM_FIELD)
    if payload is not None:
        assert payload.get("schema") == LOCK_SCHEMA_VERSION
