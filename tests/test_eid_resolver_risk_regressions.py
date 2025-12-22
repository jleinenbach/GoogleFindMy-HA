# tests/test_eid_resolver_risk_regressions.py
"""LOUD regression tests for subtle resolver risks (dedupe, offsets, confirmation, soft-gate)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    LOCK_CONFIRMATION_TTL_SECONDS,
    EIDGenerationLock,
    EIDMatch,
    EidVariant,
    GoogleFindMyEIDResolver,
    WindowCandidate,
    WindowSpec,
    WorkItem,
    _normalize_counter_candidate,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import ROTATION_PERIOD


def _resolver() -> GoogleFindMyEIDResolver:
    """Return a resolver with initialized caches and fake hass."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=lambda coro, name=None: asyncio.create_task(coro),
        async_create_background_task=lambda coro, name=None: asyncio.create_task(coro),
    )
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_advertisement_reversed = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._decryption_status = {}
    resolver._last_lock_confirmation = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._provisioning_warn_at = {}
    resolver._ensure_cache_defaults()
    return resolver


def _identity() -> DeviceIdentity:
    """Baseline device identity."""

    return DeviceIdentity(
        registry_id="device-id",
        canonical_id="canonical-id",
        identity_key=b"\xAA" * 16,
        config_entry_id="entry-id",
        manufacturer="",
        model="",
        pair_date=1_000,
        secrets_creation_date=2_000,
    )


def test_dedupe_prefers_best_metadata() -> None:
    """AC4/R1: dedupe must keep the best semantic_offset, not first wins."""

    resolver = _resolver()
    w_bad = WindowCandidate(
        timestamp=12345,
        semantic_offset=1024,
        time_basis="pair_date",
        candidate_value=11111,
    )
    w_good = WindowCandidate(
        timestamp=12345,
        semantic_offset=0,
        time_basis="pair_date",
        candidate_value=11111,
    )
    specs = [
        WindowSpec(
            time_basis="pair_date",
            candidate_value=11111,
            windows=(w_bad,),
        ),
        WindowSpec(
            time_basis="pair_date",
            candidate_value=11111,
            windows=(w_good,),
        ),
    ]

    deduped = resolver._dedupe_windows(specs)
    assert deduped and len(deduped[0].windows) == 1
    kept = deduped[0].windows[0]
    assert kept.semantic_offset == 0, (
        "FAIL (R1): _dedupe_windows kept the first duplicate instead of the best one. "
        "Fix: In `GoogleFindMyEIDResolver._dedupe_windows`, when duplicates share (basis, timestamp), "
        "select the candidate with the smallest abs(semantic_offset) and keep deterministic ordering "
        "so better match metadata is preserved."
    )


def test_known_offset_does_not_cross_bases(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3/R2: known_offset must not leak between time bases even when plausible."""

    resolver = _resolver()
    params = resolver._build_rotation_params()
    identity = _identity()
    resolver._known_offsets[(identity.registry_id, "pair_date")] = 2 * ROTATION_PERIOD

    now_unix = 10_000
    work_item = WorkItem(
        registry_id=identity.registry_id,
        config_entry_id=identity.config_entry_id,
        canonical_id=identity.canonical_id,
        key_bytes=b"\xBB" * 16,
        identity=identity,
        lock=None,
        locked_variant=None,
        rotation_ts=None,
        basis_hint=None,
    )

    counter_windows, invalid_hint = resolver._compute_relative_windows(
        work_item,
        now_unix=now_unix,
        params=params,
        min_relative_window=params.min_relative_window,
        allow_unix_basis=True,
    )
    assert invalid_hint is False
    basis_map = {spec.time_basis: spec for spec in counter_windows}
    secrets_spec = basis_map.get("secrets_creation_date")
    assert secrets_spec is not None

    base_candidate = now_unix - identity.secrets_creation_date
    expected_normalized = _normalize_counter_candidate(base_candidate, basis="secrets_creation_date")

    assert secrets_spec.candidate_value == expected_normalized, (
        "FAIL (R2): known_offset was applied across time bases (pair_date -> secrets_creation_date). "
        "Fix: Track offsets with basis provenance (for example, `_known_offsets[(device_id, basis)]`) "
        "and only apply the offset for the current basis inside `_compute_relative_windows`."
    )


def test_confirmation_refresh_and_purge_stability(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1/R3: confirmation must refresh on HIT and prevent premature purge."""

    resolver = _resolver()
    eid_bytes = b"\x99" * 20
    resolver._lookup[eid_bytes] = EIDMatch(
        device_id="device-id",
        config_entry_id="entry-id",
        canonical_id="canonical-id",
        time_offset=0,
        is_reversed=False,
    )
    resolver._lookup_metadata[eid_bytes] = {
        "timestamp_basis": "pair_date",
        "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
    }
    resolver._locks["device-id"] = EIDGenerationLock(
        device_id="device-id",
        canonical_id="canonical-id",
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=len(eid_bytes),
        created_at=0,
    )
    resolver._last_lock_confirmation["device-id"] = 1

    now = 1_000
    monkeypatch.setattr(time, "time", lambda: float(now))

    match = resolver.resolve_eid(eid_bytes)
    assert match is not None
    assert resolver._last_lock_confirmation["device-id"] == now, (
        "FAIL (AC1): resolve_eid did not refresh last_lock_confirmation on HIT. "
        "Fix: In `resolve_eid` match-found path, set `_last_lock_confirmation[device_id] = int(time.time())` "
        "before returning so active locks are not purged."
    )

    resolver._purge_stale_locks(now=now + LOCK_CONFIRMATION_TTL_SECONDS // 2)
    assert "device-id" in resolver._locks, (
        "FAIL (AC1): active lock was purged despite recent confirmation. "
        "Ensure `_purge_stale_locks` respects updated `_last_lock_confirmation` timestamps."
    )


def test_soft_gate_keeps_discovery_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5/R4: soft-gate must keep discovery windows after dedupe with deterministic ordering."""

    resolver = _resolver()
    params = resolver._build_rotation_params()
    identity = _identity()
    work_item = WorkItem(
        registry_id=identity.registry_id,
        config_entry_id=identity.config_entry_id,
        canonical_id=identity.canonical_id,
        key_bytes=b"\xCC" * 16,
        identity=identity,
        lock=None,
        locked_variant=None,
        rotation_ts=None,
        basis_hint=None,
    )

    lock_windows = [
        WindowSpec(
            time_basis="lock_tracking",
            candidate_value=1000,
            windows=(
                WindowCandidate(
                    timestamp=5000,
                    semantic_offset=0,
                    time_basis="lock_tracking",
                    candidate_value=1000,
                ),
            ),
        )
    ]
    discovery_windows = [
        WindowSpec(
            time_basis="pair_date",
            candidate_value=2000,
            windows=(
                WindowCandidate(
                    timestamp=5000,
                    semantic_offset=1,
                    time_basis="pair_date",
                    candidate_value=2000,
                ),
            ),
        )
    ]

    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_compute_lock_windows",
        lambda _self, *_args, **_: lock_windows,
    )
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_compute_relative_windows",
        lambda _self, *_args, **_: (discovery_windows, False),
    )

    merged, invalid_hint = resolver._compute_time_windows(
        work_item,
        now_unix=10_000,
        params=params,
    )

    assert invalid_hint is False
    bases = [spec.time_basis for spec in merged]
    assert bases[:2] == ["lock_tracking", "pair_date"], (
        "FAIL (AC5): soft-gate/dedupe removed or reordered discovery windows. "
        "Ensure `_compute_time_windows` keeps lock_tracking first and still includes discovery windows "
        "after deduplication so self-healing remains deterministic."
    )
