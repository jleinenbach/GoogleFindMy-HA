# tests/test_eid_resolver_lifecycle.py
"""Lifecycle management tests for the EID resolver (stop, cleanup)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.googlefindmy.eid_resolver import (
    ConfirmedSighting,
    EIDGenerationLock,
    EidVariant,
    GoogleFindMyEIDResolver,
)


def _fake_hass() -> SimpleNamespace:
    """Return a minimal hass stand-in.

    For sync tests, coroutines are closed to avoid 'never awaited' warnings.
    For async tests, use _fake_hass_async() instead.
    """

    def _close_coro(coro, name=None):
        """Close coroutine in sync context to avoid RuntimeWarning."""
        if hasattr(coro, "close"):
            coro.close()

    return SimpleNamespace(
        async_create_task=_close_coro,
        async_create_background_task=_close_coro,
    )


def _fake_hass_async() -> SimpleNamespace:
    """Return a minimal hass stand-in for async tests."""
    return SimpleNamespace(
        async_create_task=lambda coro, name=None: asyncio.create_task(coro),
        async_create_background_task=lambda coro, name=None: asyncio.create_task(coro),
    )


def _build_resolver() -> GoogleFindMyEIDResolver:
    """Build a resolver with pre-populated state for stop() testing."""
    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = _fake_hass()
    resolver._lookup = {b"test": MagicMock()}
    resolver._lookup_metadata = {b"test": {"variant": "test"}}
    resolver._locks = {
        "device-1": EIDGenerationLock(
            device_id="device-1",
            canonical_id="canonical-1",
            variant=EidVariant.MODERN_P256_X32_BE.value,
            advertisement_reversed=False,
            eid_length=32,
        )
    }
    resolver._persisted_locks = dict(resolver._locks)
    resolver._known_offsets = {}
    resolver._known_advertisement_reversed = {}
    resolver._known_timebases = {}
    resolver._decryption_status = {}
    resolver._last_lock_confirmation = {}
    resolver._confirmed_sightings = {}
    resolver._provisioning_warn_at = {}
    resolver._truncated_frame_log_at = {}
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._load_task = None
    return resolver


def test_stop_clears_all_caches() -> None:
    """stop() should clear lookup, metadata, locks, and persisted_locks."""
    resolver = _build_resolver()

    assert resolver._lookup
    assert resolver._lookup_metadata
    assert resolver._locks
    assert resolver._persisted_locks

    resolver.stop()

    assert resolver._lookup == {}
    assert resolver._lookup_metadata == {}
    assert resolver._locks == {}
    assert resolver._persisted_locks == {}


def test_stop_cancels_unsub_callbacks() -> None:
    """stop() should call unsub callbacks and set them to None."""
    resolver = _build_resolver()

    alignment_unsub = MagicMock()
    interval_unsub = MagicMock()
    resolver._unsub_alignment = alignment_unsub
    resolver._unsub_interval = interval_unsub

    resolver.stop()

    alignment_unsub.assert_called_once()
    interval_unsub.assert_called_once()
    assert resolver._unsub_alignment is None
    assert resolver._unsub_interval is None


def test_stop_handles_none_unsub_gracefully() -> None:
    """stop() should not crash when unsub callbacks are None."""
    resolver = _build_resolver()
    resolver._unsub_alignment = None
    resolver._unsub_interval = None

    # Should not raise
    resolver.stop()

    assert resolver._unsub_alignment is None
    assert resolver._unsub_interval is None


@pytest.mark.asyncio
async def test_stop_cancels_running_load_task() -> None:
    """stop() should cancel a running _load_task."""
    resolver = _build_resolver()

    # Create a long-running task
    async def slow_load() -> None:
        await asyncio.sleep(10)

    resolver._load_task = asyncio.create_task(slow_load())
    assert not resolver._load_task.done()

    resolver.stop()

    # Task should be cancelled
    assert resolver._load_task is None

    # Give the event loop a chance to process the cancellation
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_stop_handles_completed_load_task() -> None:
    """stop() should handle an already-completed _load_task."""
    resolver = _build_resolver()

    async def quick_load() -> None:
        return None

    task = asyncio.create_task(quick_load())
    await task  # Let it complete
    resolver._load_task = task

    assert resolver._load_task.done()

    # Should not raise
    resolver.stop()

    assert resolver._lookup == {}


def test_stop_handles_unsub_exception() -> None:
    """stop() should log but not raise if unsub callback fails."""
    resolver = _build_resolver()

    def failing_unsub() -> None:
        raise RuntimeError("unsub failed")

    resolver._unsub_alignment = failing_unsub
    resolver._unsub_interval = None

    # Should not raise
    resolver.stop()

    assert resolver._unsub_alignment is None
    assert resolver._lookup == {}


def test_cancel_callback_helper_handles_coroutine() -> None:
    """_cancel_callback should close coroutines properly."""

    async def sample_coro() -> None:
        await asyncio.sleep(1)

    coro = sample_coro()

    # Should not raise
    GoogleFindMyEIDResolver._cancel_callback(coro, "test coroutine")

    # Coroutine should be closed (calling close again is safe)
    coro.close()


def _lock_with_variant(variant: str) -> EIDGenerationLock:
    """Return a non-legacy lock (rotation timestamp set) with ``variant``."""

    return EIDGenerationLock(
        device_id="device-1",
        canonical_id="canonical-1",
        variant=variant,
        advertisement_reversed=False,
        eid_length=32,
        rotation_timestamp=1_700_000_000,
    )


def _identity() -> SimpleNamespace:
    """Return the identity fields read by ``_prepare_work_item``."""

    return SimpleNamespace(
        registry_id="device-1",
        canonical_id="canonical-1",
        identity_key=bytes(range(32)),
        config_entry_id="entry-1",
    )


# A variant name no release defines. ``spec_p256_x20_trunc_be`` served here
# until it became a real variant and the three tests below started to fail.
_UNKNOWN_VARIANT = "unknown_variant_for_tests"


def test_unknown_variant_fixture_is_not_a_variant() -> None:
    """The fixture must stay unknown, or the discard tests test nothing."""

    assert _UNKNOWN_VARIANT not in {variant.value for variant in EidVariant}


def test_prepare_work_item_discards_lock_with_unknown_variant() -> None:
    """A persisted lock whose variant this version does not know is discarded.

    Before AP5b the ``except ValueError`` branch mapped any unknown value to
    ``MODERN_P256_X32_BE``, so a lock written by a newer release (for example a
    reading that a later rollback removes) would silently pin the device to a
    different derivation. The lock must be dropped from memory and from the
    persisted set, and the work item must carry neither the lock nor a locked
    variant, so ``_compute_variants`` tries every known variant again.
    """

    resolver = _build_resolver()
    lock = _lock_with_variant(_UNKNOWN_VARIANT)
    resolver._locks = {"device-1": lock}
    resolver._persisted_locks = {"device-1": lock}

    item = resolver._prepare_work_item(_identity(), now_unix=1_700_000_100)

    assert item is not None
    assert item.lock is None
    assert item.locked_variant is None
    assert "device-1" not in resolver._locks
    assert "device-1" not in resolver._persisted_locks


def test_prepare_work_item_unknown_variant_logs_discard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The discard of an unknown-variant lock is logged at WARNING."""

    resolver = _build_resolver()
    lock = _lock_with_variant(_UNKNOWN_VARIANT)
    resolver._locks = {"device-1": lock}
    resolver._persisted_locks = {"device-1": lock}

    with caplog.at_level(
        "WARNING", logger="custom_components.googlefindmy.eid_resolver"
    ):
        resolver._prepare_work_item(_identity(), now_unix=1_700_000_100)

    discards = [r for r in caplog.records if "Force re-discovery" in r.getMessage()]
    assert len(discards) == 1
    assert discards[0].levelname == "WARNING"
    assert "unknown_variant=True" in discards[0].getMessage()


@pytest.mark.parametrize("variant", [v.value for v in EidVariant])
def test_prepare_work_item_keeps_lock_with_known_variant(variant: str) -> None:
    """Every known variant value loads unchanged and keeps the lock."""

    resolver = _build_resolver()
    lock = _lock_with_variant(variant)
    resolver._locks = {"device-1": lock}
    resolver._persisted_locks = {"device-1": lock}

    item = resolver._prepare_work_item(_identity(), now_unix=1_700_000_100)

    assert item is not None
    assert item.lock is lock
    assert item.locked_variant == EidVariant(variant)
    assert resolver._locks["device-1"] is lock
    assert resolver._persisted_locks["device-1"] is lock


def test_prepare_work_item_persists_unknown_variant_discard() -> None:
    """Discarding an unknown-variant lock schedules persistence.

    ``_async_save_locks`` writes ``_locks``; without a save the unchanged
    on-disk lock is reloaded at every start, and an offline device would
    re-enter the discard (and its WARNING) after each restart.
    """

    resolver = _build_resolver()
    lock = _lock_with_variant(_UNKNOWN_VARIANT)
    resolver._locks = {"device-1": lock}
    resolver._persisted_locks = {"device-1": lock}

    with patch.object(GoogleFindMyEIDResolver, "_schedule_lock_save") as save:
        resolver._prepare_work_item(_identity(), now_unix=1_700_000_100)

    save.assert_called_once_with()


def test_prepare_work_item_known_variant_schedules_no_save() -> None:
    """A lock with a known variant and unchanged canonical ID is not re-saved."""

    resolver = _build_resolver()
    lock = _lock_with_variant(EidVariant.MODERN_P256_X32_BE.value)
    resolver._locks = {"device-1": lock}
    resolver._persisted_locks = {"device-1": lock}

    with patch.object(GoogleFindMyEIDResolver, "_schedule_lock_save") as save:
        resolver._prepare_work_item(_identity(), now_unix=1_700_000_100)

    save.assert_not_called()


def test_prepare_work_item_legacy_discard_schedules_no_save() -> None:
    """Characterize the legacy discard as found: it does not schedule a save.

    Only the unknown-variant discard persists. Changing the legacy branch is a
    separate follow-up (it also keeps ``locked_variant`` set).
    """

    resolver = _build_resolver()
    lock = _lock_with_variant(EidVariant.MODERN_P256_X32_BE.value)
    lock.rotation_timestamp = None
    resolver._locks = {"device-1": lock}
    resolver._persisted_locks = {"device-1": lock}

    with patch.object(GoogleFindMyEIDResolver, "_schedule_lock_save") as save:
        item = resolver._prepare_work_item(_identity(), now_unix=1_700_000_100)

    assert item is not None
    assert item.lock is None
    assert "device-1" not in resolver._locks
    save.assert_not_called()


# --- last_confirmed_sighting(): the observation the finder reports --------

_PERIOD = 1024
_T = 1_700_000_000
_LEGACY = EidVariant.LEGACY_SECP160R1_X20_BE


def _matched_resolver() -> GoogleFindMyEIDResolver:
    """Resolver with lookup EIDs for ``dev-m`` in windows 7 and 9.

    Marker ``0x73`` is also window 7, but advertised by two devices that
    share the tracker (``dev-m`` and ``dev-n``).
    """
    from tests.test_ble_battery_sensor import _make_resolver, _match

    resolver = _make_resolver()
    for marker, window, devices in (
        (0x71, 7, ("dev-m",)),
        (0x72, 9, ("dev-m",)),
        (0x73, 7, ("dev-m", "dev-n")),
    ):
        eid = bytes([marker]) * 20
        resolver._lookup[eid] = [_match(device) for device in devices]
        resolver._lookup_metadata[eid] = {
            "variant": _LEGACY.value,
            "rotation_timestamp": window * _PERIOD,
            "timestamp_basis": "pair_date",
        }
    return resolver


def _see(
    resolver: GoogleFindMyEIDResolver,
    marker: int,
    at: int,
    *,
    monotonic: float | None = None,
) -> None:
    """Feed one sighting at wall time ``at``.

    ``monotonic`` is the advertisement time on the monotonic clock, as HA
    hands it over; by default it advances with ``at``.
    """
    from tests.test_ble_battery_sensor import _service_data_payload

    observed = float(at - _T + 50_000) if monotonic is None else monotonic
    # Delivered right when seen: _observation_clock then dates it at ``at``.
    with (
        patch("time.time", return_value=float(at)),
        patch("time.monotonic", return_value=observed),
    ):
        assert resolver.resolve_eid(
            _service_data_payload(bytes([marker]) * 20, 0), observed_at=observed
        )


def _window(resolver: GoogleFindMyEIDResolver, device: str = "dev-m") -> int | None:
    sighting = resolver.last_confirmed_sighting(device)
    return None if sighting is None else sighting.window_counter // _PERIOD


def test_sighting_records_variant_window_and_time() -> None:
    """A match records what the device advertised and when it was seen."""
    resolver = _matched_resolver()
    assert resolver.last_confirmed_sighting("dev-m") is None

    _see(resolver, 0x71, _T + 5)

    assert resolver.last_confirmed_sighting("dev-m") == ConfirmedSighting(
        variant=_LEGACY, window_counter=7 * _PERIOD, observed_at=_T + 5
    )


def test_sighting_aligns_the_window_timestamp() -> None:
    """``lock_tracking`` windows need not be aligned; the counter is."""
    resolver = _build_resolver()
    resolver._note_confirmed_sighting(
        "device-1",
        {"variant": _LEGACY.value, "rotation_timestamp": 7 * _PERIOD + 256},
        observed_at=_T,
    )

    sighting = resolver.last_confirmed_sighting("device-1")
    assert sighting is not None
    assert sighting.window_counter == 7 * _PERIOD


@pytest.mark.parametrize(
    "metadata",
    [
        {"variant": "no_such_variant", "rotation_timestamp": 7 * _PERIOD},
        {"variant": None, "rotation_timestamp": 7 * _PERIOD},
        {"variant": 7, "rotation_timestamp": 7 * _PERIOD},
        {"rotation_timestamp": 7 * _PERIOD},
        {"variant": "legacy_secp160r1_x20_be", "rotation_timestamp": True},
        {"variant": "legacy_secp160r1_x20_be", "rotation_timestamp": -_PERIOD},
        {"variant": "legacy_secp160r1_x20_be", "rotation_timestamp": 2**32},
        {"variant": "legacy_secp160r1_x20_be", "rotation_timestamp": 7.0 * _PERIOD},
        {"variant": "legacy_secp160r1_x20_be"},
    ],
    ids=[
        "unknown_variant",
        "variant_none",
        "variant_not_str",
        "variant_missing",
        "counter_bool",
        "counter_negative",
        "counter_beyond_u32",
        "counter_float",
        "counter_missing",
    ],
)
def test_sighting_with_invalid_metadata_records_nothing(
    metadata: dict[str, object],
) -> None:
    """Never raises, never records a sighting it cannot vouch for."""
    resolver = _build_resolver()
    resolver._note_confirmed_sighting("device-1", metadata, observed_at=_T)

    assert resolver.last_confirmed_sighting("device-1") is None


def test_sighting_accepts_the_largest_u32_counter() -> None:
    """The upper bound is inclusive: ``FHNA_COUNTER_MASK`` itself is valid."""
    resolver = _build_resolver()
    resolver._note_confirmed_sighting(
        "device-1",
        {"variant": _LEGACY.value, "rotation_timestamp": 2**32 - 1},
        observed_at=_T,
    )

    sighting = resolver.last_confirmed_sighting("device-1")
    assert sighting is not None
    assert sighting.window_counter == 2**32 - _PERIOD


def test_newer_window_replaces_the_sighting() -> None:
    resolver = _matched_resolver()
    _see(resolver, 0x71, _T)
    _see(resolver, 0x72, _T + 100)

    assert _window(resolver) == 9


def test_older_window_delivered_late_keeps_the_newer_one() -> None:
    """Bermuda dates an advertisement without scanner stamp "now".

    Such a late advertisement of window 7 is newer on the monotonic clock than
    the sighting of window 9 fifty seconds earlier, but carries the older
    window; the sighting stays on window 9.
    """
    resolver = _matched_resolver()
    _see(resolver, 0x72, _T)
    _see(resolver, 0x71, _T + 50)

    assert resolver.last_confirmed_sighting("dev-m") == ConfirmedSighting(
        variant=_LEGACY, window_counter=9 * _PERIOD, observed_at=_T
    )


@pytest.mark.parametrize(
    ("delay", "window"),
    [(_PERIOD, 9), (_PERIOD + 1, 7)],
    ids=["within_one_period", "after_one_period"],
)
def test_smaller_window_replaces_only_after_one_period(delay: int, window: int) -> None:
    """After more than one period a smaller counter is a restarted device."""
    resolver = _matched_resolver()
    _see(resolver, 0x72, _T)
    _see(resolver, 0x71, _T + delay)

    assert _window(resolver) == window


def test_same_window_takes_the_newer_sighting_time() -> None:
    resolver = _matched_resolver()
    _see(resolver, 0x71, _T)
    _see(resolver, 0x71, _T + 500)

    sighting = resolver.last_confirmed_sighting("dev-m")
    assert sighting is not None
    assert sighting.observed_at == _T + 500


def test_wall_clock_stepped_back_does_not_freeze_the_sighting() -> None:
    """Newer on the monotonic clock wins, even with an earlier wall time."""
    resolver = _matched_resolver()
    _see(resolver, 0x71, _T, monotonic=50_000.0)
    _see(resolver, 0x71, _T - 5_000, monotonic=50_010.0)

    sighting = resolver.last_confirmed_sighting("dev-m")
    assert sighting is not None
    assert sighting.observed_at == _T - 5_000


@pytest.mark.parametrize(
    ("replay_wall", "replay_monotonic"),
    [(_T + 100, 50_000.0), (_T + 100 - 2_000, 48_000.0)],
    ids=["same_wall_time", "more_than_a_period_older"],
)
def test_replayed_older_sighting_is_ignored(
    replay_wall: int, replay_monotonic: float
) -> None:
    """Older on the monotonic clock: a replay, not a new observation.

    A replay more than one period older carries a smaller counter at a wall
    time more than one period away; without the monotonic order it would look
    like a device that restarted its counter.
    """
    resolver = _matched_resolver()
    _see(resolver, 0x72, _T + 100, monotonic=50_100.0)
    _see(resolver, 0x71, replay_wall, monotonic=replay_monotonic)

    assert resolver.last_confirmed_sighting("dev-m") == ConfirmedSighting(
        variant=_LEGACY, window_counter=9 * _PERIOD, observed_at=_T + 100
    )


def test_shared_tracker_records_a_sighting_per_device() -> None:
    resolver = _matched_resolver()
    _see(resolver, 0x73, _T)

    assert _window(resolver, "dev-m") == 7
    assert _window(resolver, "dev-n") == 7


def test_heuristic_match_records_no_sighting() -> None:
    """Phones found by the heuristic path have no lookup window to report."""
    from tests.test_ble_battery_sensor import _match, _service_data_payload

    resolver = _matched_resolver()
    with (
        patch.object(
            GoogleFindMyEIDResolver, "_heuristic_resolve", return_value=_match("dev-m")
        ),
        patch("time.time", return_value=float(_T)),
    ):
        assert resolver.resolve_eid(_service_data_payload(b"\x7f" * 20, 0))

    assert resolver.last_confirmed_sighting("dev-m") is None


def test_sighting_follows_clear_reset_and_stop() -> None:
    """Clearing, resetting or stopping drops the sighting at once."""
    resolver = _matched_resolver()
    _see(resolver, 0x71, _T)
    resolver._clear_lock_state("dev-m")
    assert resolver.last_confirmed_sighting("dev-m") is None

    resolver = _matched_resolver()
    _see(resolver, 0x71, _T)
    resolver.reset_device_offset("dev-m")
    assert resolver.last_confirmed_sighting("dev-m") is None

    resolver = _matched_resolver()
    _see(resolver, 0x71, _T)
    resolver.stop()
    assert resolver.last_confirmed_sighting("dev-m") is None
