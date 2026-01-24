# tests/test_eid_resolver_pipeline.py
"""Pipeline and invariants for the EID resolver refresh logic."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    CacheBuilder,
    EIDGenerationLock,
    GoogleFindMyEIDResolver,
    RotationParams,
    WindowCandidate,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
    EidVariant,
)


def _build_resolver(monkeypatch: pytest.MonkeyPatch) -> GoogleFindMyEIDResolver:
    """Return a resolver with lightweight Home Assistant stand-ins."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = SimpleNamespace(
        async_create_task=lambda coro,
        name=None: asyncio.get_running_loop().create_task(coro),
        data={},
    )
    resolver._store = SimpleNamespace(async_load=lambda: None, async_save=AsyncMock())
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._locks = {}
    resolver._persisted_locks = {}
    resolver._known_offsets = {}
    resolver._known_timebases = {}
    resolver._known_advertisement_reversed = {}
    resolver._last_lock_confirmation = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._decryption_status = {}
    resolver._provisioning_warn_at = {}
    monkeypatch.setattr(resolver_module.time, "time", lambda: 2_000.0)
    return resolver


@pytest.mark.asyncio
async def test_reset_device_offset_clears_state_and_schedules_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reset should drop per-device caches, persist, and refresh."""

    resolver = _build_resolver(monkeypatch)
    resolver._locks = {
        "reg-1": EIDGenerationLock(
            device_id="reg-1",
            canonical_id="canonical-1",
            variant=EidVariant.LEGACY_SECP160R1_X20_BE.value,
            advertisement_reversed=False,
            eid_length=LEGACY_EID_LENGTH,
        )
    }
    resolver._persisted_locks = dict(resolver._locks)
    resolver._known_offsets = {("reg-1", "pair_date"): -1}
    resolver._known_timebases = {"reg-1": "pair_date"}
    resolver._known_advertisement_reversed = {"reg-1": True}
    resolver._last_lock_confirmation = {"reg-1": 1_234}

    scheduled_tasks: list[asyncio.Task[None]] = []
    scheduled_names: list[str | None] = []
    refresh_calls: list[None] = []

    async def _refresh(self: GoogleFindMyEIDResolver) -> None:
        refresh_calls.append(None)

    def _create_task(
        coro: Coroutine[Any, Any, None], name: str | None = None
    ) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        scheduled_tasks.append(task)
        scheduled_names.append(name)
        return task

    resolver.hass.async_create_task = _create_task
    schedule_calls: list[None] = []
    monkeypatch.setattr(resolver.__class__, "async_refresh", _refresh)

    def _schedule_lock_save(self: GoogleFindMyEIDResolver) -> None:
        schedule_calls.append(None)

    monkeypatch.setattr(resolver.__class__, "_schedule_lock_save", _schedule_lock_save)

    resolver.reset_device_offset("reg-1")
    await asyncio.gather(*scheduled_tasks)

    assert resolver._locks == {}
    assert resolver._persisted_locks == {}
    assert resolver._known_offsets == {}
    assert resolver._known_timebases == {}
    assert resolver._known_advertisement_reversed == {}
    assert resolver._last_lock_confirmation == {}
    assert schedule_calls, "Lock persistence should be scheduled"
    assert refresh_calls, "Refresh should be scheduled after reset"
    assert scheduled_names == ["googlefindmy_eid_refresh_after_reset"]


def test_reset_device_offset_noop_when_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset should return early when the registry ID is absent."""

    resolver = _build_resolver(monkeypatch)
    resolver._locks = {"other": MagicMock()}
    resolver._persisted_locks = dict(resolver._locks)
    resolver._known_offsets = {("other", "pair_date"): 0}
    resolver._known_timebases = {"other": "pair_date"}
    resolver._known_advertisement_reversed = {"other": False}
    resolver._last_lock_confirmation = {"other": 1}

    refresh_calls: list[None] = []

    async def _refresh(self: GoogleFindMyEIDResolver) -> None:
        refresh_calls.append(None)

    schedule_calls: list[None] = []

    def _schedule_lock_save(self: GoogleFindMyEIDResolver) -> None:
        schedule_calls.append(None)

    # Mock async_create_task to close the coroutine (no event loop in sync test)
    def _mock_create_task(coro, name=None):
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(resolver.__class__, "async_refresh", _refresh)
    monkeypatch.setattr(resolver.__class__, "_schedule_lock_save", _schedule_lock_save)
    resolver.hass.async_create_task = _mock_create_task

    resolver.reset_device_offset("reg-1")

    assert resolver._locks == {"other": resolver._locks["other"]}
    assert resolver._persisted_locks == {"other": resolver._locks["other"]}
    assert resolver._known_offsets == {("other", "pair_date"): 0}
    assert resolver._known_timebases == {"other": "pair_date"}
    assert resolver._known_advertisement_reversed == {"other": False}
    assert resolver._last_lock_confirmation == {"other": 1}
    assert not schedule_calls
    assert not refresh_calls


@pytest.mark.asyncio
async def test_refresh_pipeline_is_deterministic_and_invariant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refresh should be deterministic and leave lookup/metadata aligned."""

    resolver = _build_resolver(monkeypatch)
    identity = DeviceIdentity(
        registry_id="dev-1",
        canonical_id="can:1",
        identity_key=b"\x01" * 32,
        config_entry_id="entry-1",
        pair_date=1_500,
        secrets_creation_date=1_000,
    )

    monkeypatch.setattr(
        resolver.__class__,
        "_collect_device_secrets",
        AsyncMock(return_value=[identity]),
    )

    def _fake_params(self: GoogleFindMyEIDResolver) -> RotationParams:
        return RotationParams(
            rotation_period=resolver_module.ROTATION_PERIOD,
            min_unix_window=1,
            min_relative_window=1,
            max_window=2,
        )

    monkeypatch.setattr(resolver.__class__, "_build_rotation_params", _fake_params)
    monkeypatch.setattr(
        resolver.__class__,
        "_generate_variant",
        lambda self,
        key_bytes,
        *,
        time_counter,
        variant: f"{variant.value}-{time_counter}".encode(),
    )

    await resolver._refresh_cache()
    first_lookup = dict(resolver._lookup)
    first_metadata = dict(resolver._lookup_metadata)

    await resolver._refresh_cache()
    assert resolver._lookup == first_lookup
    assert resolver._lookup_metadata == first_metadata
    assert set(resolver._lookup.keys()) == set(resolver._lookup_metadata.keys())
    assert resolver._lookup, "refresh should populate the lookup cache"


def test_collision_policy_prefers_smallest_offset() -> None:
    """Collision resolution should retain the closest semantic offset and merge bases."""

    builder = CacheBuilder()
    window_far = WindowCandidate(
        timestamp=10,
        semantic_offset=5,
        time_basis="pair_date",
        candidate_value=5,
    )
    window_close = WindowCandidate(
        timestamp=12,
        semantic_offset=1,
        time_basis="secrets_creation_date",
        candidate_value=11,
    )

    match_far = resolver_module.EIDMatch(
        device_id="dev",
        config_entry_id="entry",
        canonical_id="can",
        time_offset=5,
        is_reversed=False,
    )
    match_close = resolver_module.EIDMatch(
        device_id="dev",
        config_entry_id="entry",
        canonical_id="can",
        time_offset=1,
        is_reversed=False,
    )

    builder.register_eid(
        b"eid",
        match=match_far,
        variant=EidVariant.LEGACY_SECP160R1_X20_BE,
        window=window_far,
        advertisement_reversed=False,
    )
    builder.register_eid(
        b"eid",
        match=match_close,
        variant=EidVariant.LEGACY_SECP160R1_X20_BE,
        window=window_close,
        advertisement_reversed=False,
    )

    lookup, metadata = builder.finalize()
    assert lookup[b"eid"][0].time_offset == 1  # Get first match from list
    assert metadata[b"eid"]["timestamp_bases"] == {"pair_date", "secrets_creation_date"}


def test_extract_candidates_handles_frames_and_truncated_payloads() -> None:
    """Candidate extraction should return expected slices without crashing."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    payload = (
        b"\x00" * 7
        + bytes([resolver_module.FMDN_FRAME_TYPE])
        + b"A" * LEGACY_EID_LENGTH
        + b"\x99"
    )
    candidates, observed_frame = resolver._extract_candidates(payload)

    assert observed_frame == resolver_module.FMDN_FRAME_TYPE
    assert candidates == [b"A" * LEGACY_EID_LENGTH]

    candidates, observed_frame = resolver._extract_candidates(b"\x01\x02")
    assert candidates == []
    assert observed_frame is None


@pytest.mark.asyncio
async def test_lock_save_scheduled_after_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolver matches should schedule lock persistence with consistent metadata."""

    resolver = _build_resolver(monkeypatch)
    payload = b"B" * LEGACY_EID_LENGTH
    resolver._lookup = {
        payload: [
            resolver_module.EIDMatch(
                device_id="dev-lock",
                config_entry_id="entry-lock",
                canonical_id="can-lock",
                time_offset=0,
                is_reversed=False,
            )
        ]
    }
    resolver._lookup_metadata = {
        payload: {
            "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
            "rotation_timestamp": 123,
            "timestamp_basis": "pair_date",
            "timestamp_bases": {"pair_date"},
            "time_offset": 0,
            "advertisement_reversed": False,
        }
    }

    async def _fake_async_save(self: GoogleFindMyEIDResolver) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver, "_async_save_locks", _fake_async_save
    )

    original_schedule = resolver._schedule_lock_save
    spy_schedule = MagicMock()

    def _schedule_lock_save_spy(self: GoogleFindMyEIDResolver) -> None:  # noqa: ANN001
        spy_schedule(self)
        return original_schedule()

    monkeypatch.setattr(
        resolver_module.GoogleFindMyEIDResolver,
        "_schedule_lock_save",
        _schedule_lock_save_spy,
    )

    match = resolver.resolve_eid(payload)
    await asyncio.sleep(0)

    assert match is not None
    spy_schedule.assert_called_once()
    assert resolver._locks["dev-lock"].time_basis == "pair_date"
    assert resolver._persisted_locks["dev-lock"].time_basis == "pair_date"
