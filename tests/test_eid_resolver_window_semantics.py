# tests/test_eid_resolver_window_semantics.py
"""Window semantics and confirmation handling for the EID resolver."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EIDGenerationLock,
    EidVariant,
    GoogleFindMyEIDResolver,
    WindowCandidate,
    WindowSpec,
    WorkItem,
    _normalize_counter_candidate,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import ROTATION_PERIOD


def _close_coro(coro: object, name: object = None) -> None:
    """Close coroutine in sync context to avoid RuntimeWarning."""
    if hasattr(coro, "close"):
        coro.close()


def _resolver() -> GoogleFindMyEIDResolver:
    """Return a resolver with initialized caches and a fake hass."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=_close_coro,
        async_create_background_task=_close_coro,
    )
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._ensure_cache_defaults()
    return resolver


def _identity() -> DeviceIdentity:
    """Construct a baseline device identity."""

    return DeviceIdentity(
        registry_id="device-id",
        canonical_id="canonical-id",
        identity_key=b"\x01" * 16,
        config_entry_id="entry-id",
        manufacturer="",
        model="",
        pair_date=1_000,
        secrets_creation_date=1_000,
    )


def test_compute_lock_windows_uses_expected_counter_for_candidate_value() -> None:
    """Lock windows should carry the expected counter as their candidate value."""

    resolver = _resolver()
    params = resolver._build_rotation_params()
    identity = _identity()
    rotation_timestamp = 2_000
    lock = EIDGenerationLock(
        device_id=identity.registry_id,
        canonical_id=identity.canonical_id,
        variant=EidVariant.MODERN_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=20,
        rotation_timestamp=rotation_timestamp,
        created_at=100,
    )
    work_item = WorkItem(
        registry_id=identity.registry_id,
        config_entry_id=identity.config_entry_id,
        canonical_id=identity.canonical_id,
        key_bytes=b"\xaa" * 16,
        identity=identity,
        lock=lock,
        locked_variant=EidVariant.MODERN_P256_X32_BE,
        rotation_ts=rotation_timestamp,
        basis_hint=None,
    )

    now_unix = rotation_timestamp + (params.rotation_period * 3)
    windows = resolver._compute_lock_windows(
        work_item, now_unix=now_unix, params=params
    )
    assert windows

    expected_counter = (
        rotation_timestamp
        + ((now_unix - lock.created_at) // params.rotation_period)
        * params.rotation_period
    )
    assert windows[0].candidate_value == expected_counter, (
        "FAIL: lock WindowSpec.candidate_value must equal expected_counter (counter 'now')."
    )


def test_known_offset_out_of_range_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Implausible known offsets must not narrow the discovery window."""

    resolver = _resolver()
    params = resolver._build_rotation_params()
    identity = _identity()
    resolver._known_offsets[(identity.registry_id, "pair_date")] = -1_759_000_000

    now_unix = 10_000
    work_item = WorkItem(
        registry_id=identity.registry_id,
        config_entry_id=identity.config_entry_id,
        canonical_id=identity.canonical_id,
        key_bytes=b"\xbb" * 16,
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
    assert counter_windows

    pair_date_window = next(
        spec for spec in counter_windows if spec.time_basis == "pair_date"
    )
    base_candidate = now_unix - identity.pair_date
    normalized = _normalize_counter_candidate(base_candidate, basis="pair_date")

    assert pair_date_window.candidate_value == normalized, (
        "FAIL: implausible known_offset was applied; candidate_value must remain unshifted."
    )
    assert len(pair_date_window.windows) > 3, (
        "FAIL: implausible known_offset was applied, narrowing discovery and risking silent death. "
        "Bound known_offset before applying it."
    )


def test_time_windows_are_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Merged windows should deduplicate timestamps per basis."""

    resolver = _resolver()
    params = resolver._build_rotation_params()
    identity = _identity()
    work_item = WorkItem(
        registry_id=identity.registry_id,
        config_entry_id=identity.config_entry_id,
        canonical_id=identity.canonical_id,
        key_bytes=b"\xcc" * 16,
        identity=identity,
        lock=None,
        locked_variant=None,
        rotation_ts=None,
        basis_hint=None,
    )

    duplicate_ts = ROTATION_PERIOD * 5
    lock_windows = [
        WindowSpec(
            time_basis="pair_date",
            candidate_value=duplicate_ts,
            windows=(
                WindowCandidate(
                    timestamp=duplicate_ts,
                    semantic_offset=0,
                    time_basis="pair_date",
                    candidate_value=duplicate_ts,
                ),
            ),
        )
    ]
    relative_windows = [
        WindowSpec(
            time_basis="pair_date",
            candidate_value=duplicate_ts,
            windows=(
                WindowCandidate(
                    timestamp=duplicate_ts,
                    semantic_offset=0,
                    time_basis="pair_date",
                    candidate_value=duplicate_ts,
                ),
                WindowCandidate(
                    timestamp=duplicate_ts,
                    semantic_offset=0,
                    time_basis="pair_date",
                    candidate_value=duplicate_ts,
                ),
            ),
        )
    ]

    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_compute_lock_windows",
        lambda _self, *a, **k: lock_windows,
    )
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_compute_relative_windows",
        lambda _self, *a, **k: (relative_windows, False),
    )

    merged, invalid_hint = resolver._compute_time_windows(
        work_item,
        now_unix=10_000,
        params=params,
    )

    assert invalid_hint is False
    assert merged

    timestamps = [
        (spec.time_basis, window.timestamp)
        for spec in merged
        for window in spec.windows
    ]
    assert timestamps == [("pair_date", duplicate_ts)], (
        "FAIL: duplicate windows detected after merging lock and discovery windows. "
        "Deduplicate by (time_basis, timestamp) before variant generation."
    )
