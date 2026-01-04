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
    resolver._lookup[eid_bytes] = [EIDMatch(
        device_id="device-id",
        config_entry_id="entry-id",
        canonical_id="canonical-id",
        time_offset=0,
        is_reversed=False,
    )]
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


def test_resolve_eid_does_not_store_lock_tracking_as_known_timebasis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3/Task A: resolve_eid must sanitize anchor bases before persistence."""

    resolver = _resolver()
    eid = b"\x11" * 20
    resolver._lookup[eid] = [EIDMatch(
        device_id="dev",
        config_entry_id="entry",
        canonical_id="can",
        time_offset=7,
        is_reversed=False,
    )]
    resolver._lookup_metadata[eid] = {
        "timestamp_basis": "lock_tracking",
        "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
    }

    monkeypatch.setattr(time, "time", lambda: 1000.0)
    assert resolver.resolve_eid(eid) is not None

    assert resolver._known_timebases.get("dev") != "lock_tracking", (
        "FAIL: resolve_eid stored 'lock_tracking' into _known_timebases. "
        "Fix: sanitize metadata['timestamp_basis'] in resolve_eid() so only "
        "{'unix','pair_date','secrets_creation_date'} are persisted as anchor bases."
    )
    assert ("dev", "lock_tracking") not in resolver._known_offsets, (
        "FAIL: resolve_eid stored an offset under ('dev','lock_tracking'). "
        "Fix: only store known offsets when the basis is a valid anchor basis."
    )


def test_resolve_eid_missing_timestamp_basis_uses_previous_valid_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4/Task A: missing basis must fall back to a prior valid anchor basis."""

    resolver = _resolver()
    resolver._known_timebases["dev"] = "pair_date"

    eid = b"\x22" * 20
    resolver._lookup[eid] = [EIDMatch(
        device_id="dev",
        config_entry_id="entry",
        canonical_id="can",
        time_offset=3,
        is_reversed=False,
    )]
    resolver._lookup_metadata[eid] = {
        "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
    }

    monkeypatch.setattr(time, "time", lambda: 1000.0)
    assert resolver.resolve_eid(eid) is not None

    assert resolver._known_offsets.get(("dev", "pair_date")) == 3, (
        "FAIL: resolve_eid did not store basis-aware known_offset when timestamp_basis was missing. "
        "Fix: in resolve_eid(), if metadata basis is missing/invalid, fall back to a previously known "
        "valid anchor basis for that device (e.g., _known_timebases[device_id]) before storing offsets."
    )


def test_resolve_eid_normalizes_variant_and_never_persists_invalid_lock_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC6/Task B: invalid metadata variant must not poison persisted locks."""

    resolver = _resolver()
    eid = b"\x33" * 20
    resolver._lookup[eid] = [EIDMatch(
        device_id="dev",
        config_entry_id="entry",
        canonical_id="can",
        time_offset=0,
        is_reversed=False,
    )]
    resolver._lookup_metadata[eid] = {
        "timestamp_basis": "pair_date",
        "variant": "0",  # intentionally invalid for EidVariant in many implementations
    }

    monkeypatch.setattr(time, "time", lambda: 1000.0)
    assert resolver.resolve_eid(eid) is not None

    lock = resolver._locks.get("dev")
    assert lock is not None
    try:
        EidVariant(lock.variant)
    except Exception as err:  # pragma: no cover - explicit assert helper
        raise AssertionError(
            "FAIL: resolve_eid persisted an invalid EIDGenerationLock.variant. "
            "Fix: normalize/validate metadata['variant'] in resolve_eid(); "
            "if it is not parseable as EidVariant, ignore it and infer from EID length. "
            f"Observed error: {err!r}"
        )


def test_purge_known_offsets_for_device_is_defensive_against_malformed_keys() -> None:
    """AC2/Task D: purge helper must tolerate malformed offset keys."""

    resolver = _resolver()
    resolver._known_offsets = {
        ("dev", "pair_date"): 1,
        "dev": 999,
        ("dev",): 888,
        ("other", "pair_date"): 2,
    }

    removed = resolver._purge_known_offsets_for_device("dev")
    assert removed is True
    assert ("dev", "pair_date") not in resolver._known_offsets
    assert ("other", "pair_date") in resolver._known_offsets


def test_dedupe_keeps_best_window_and_aligns_candidate_value() -> None:
    """AC5/Task C: dedupe must align spec candidate values with kept windows."""

    resolver = _resolver()

    w1 = WindowCandidate(
        timestamp=10,
        semantic_offset=5,
        time_basis="pair_date",
        candidate_value=100,
    )
    w2 = WindowCandidate(
        timestamp=10,
        semantic_offset=0,
        time_basis="pair_date",
        candidate_value=200,
    )

    specs = [
        WindowSpec(time_basis="pair_date", candidate_value=100, windows=(w1,)),
        WindowSpec(time_basis="pair_date", candidate_value=999, windows=(w2,)),
    ]

    out = resolver._dedupe_windows(specs)
    assert out and len(out[0].windows) == 1
    kept = out[0].windows[0]

    assert kept.semantic_offset == 0, (
        "FAIL: _dedupe_windows did not keep the best semantic_offset candidate. "
        "Fix: when duplicates share (basis,timestamp), choose the smallest abs(semantic_offset)."
    )
    assert out[0].candidate_value == kept.candidate_value, (
        "FAIL: _dedupe_windows produced WindowSpec.candidate_value that does not match the kept WindowCandidate. "
        "Fix: set basis_candidate_value[basis] from the kept WindowCandidate.candidate_value, not spec.candidate_value."
    )


def test_unix_hint_is_ignored_when_absolute_unix_disabled() -> None:
    """AC4: unix basis hint must not collapse windows when absolute unix scan is disabled."""

    resolver = _resolver()
    params = resolver._build_rotation_params()
    identity = _identity()

    resolver._known_timebases[identity.registry_id] = "unix"

    work_item = WorkItem(
        registry_id=identity.registry_id,
        config_entry_id=identity.config_entry_id,
        canonical_id=identity.canonical_id,
        key_bytes=b"\xCC" * 16,
        identity=identity,
        lock=None,
        locked_variant=None,
        rotation_ts=None,
        basis_hint="unix",
    )

    counter_windows, invalid_hint = resolver._compute_relative_windows(
        work_item,
        now_unix=10_000,
        params=params,
        min_relative_window=params.min_relative_window,
        allow_unix_basis=True,
    )

    assert counter_windows, (
        "FAIL: unix basis hint collapsed window generation when ENABLE_ABSOLUTE_UNIX_BASIS is False. "
        "Fix: exclude 'unix' from available bases when absolute unix scanning is disabled so the hint "
        "becomes invalid and pair_date/secrets_creation_date windows are generated."
    )
    assert invalid_hint is True, (
        "FAIL: unix basis hint should be treated as invalid when absolute unix scanning is disabled. "
        "Fix: remove unix from available_bases / counter_bases under ENABLE_ABSOLUTE_UNIX_BASIS=False."
    )


def test_resolve_eid_does_not_poison_anchor_offsets_with_lock_tracking_basis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock-tracking hits must not overwrite anchor-based known offsets/timebases."""

    resolver = _resolver()
    resolver._known_timebases["dev"] = "pair_date"
    resolver._known_offsets[("dev", "pair_date")] = 123

    eid = b"\x11" * 20
    resolver._lookup[eid] = [EIDMatch(
        device_id="dev",
        config_entry_id="entry",
        canonical_id="can",
        time_offset=7,
        is_reversed=False,
    )]
    resolver._lookup_metadata[eid] = {
        "timestamp_basis": "lock_tracking",
        "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
    }

    monkeypatch.setattr(time, "time", lambda: 1000.0)
    assert resolver.resolve_eid(eid) is not None

    assert resolver._known_timebases.get("dev") == "pair_date"
    assert ("dev", "lock_tracking") not in resolver._known_offsets
    assert resolver._known_offsets.get(("dev", "pair_date")) == 123, (
        "FAIL: lock-tracking hit overwrote anchor-based known offset. "
        "Fix: if timestamp_basis is explicitly invalid (e.g., lock_tracking), "
        "do not persist match.time_offset under any anchor basis."
    )


def test_normalize_counter_candidate_rejects_bool() -> None:
    """AC7: bool values must be rejected even though bool is a subclass of int."""

    assert _normalize_counter_candidate(True, basis="pair_date") is None, (
        "FAIL: _normalize_counter_candidate accepted True as valid counter. "
        "Fix: add explicit `isinstance(candidate_value, bool)` check."
    )
    assert _normalize_counter_candidate(False, basis="pair_date") is None, (
        "FAIL: _normalize_counter_candidate accepted False as valid counter. "
        "Fix: add explicit `isinstance(candidate_value, bool)` check."
    )

    # Valid integers should still work
    assert _normalize_counter_candidate(1000, basis="pair_date") == 1000
    # Zero is rejected to handle phones with pair_date=0 (no deviceRegistration)
    assert _normalize_counter_candidate(0, basis="pair_date") is None
