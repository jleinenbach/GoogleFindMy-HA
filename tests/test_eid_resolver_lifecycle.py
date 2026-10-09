# tests/test_eid_resolver_lifecycle.py
"""Lifecycle management tests for the EID resolver (stop, cleanup)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.googlefindmy.eid_resolver import (
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


# --- locked_variant_value(): the finder's view of a lock -------------------


@pytest.mark.asyncio
async def test_locked_variant_value_reads_locks_loaded_from_storage() -> None:
    """A lock restored by ``_async_load_locks`` is reported by its variant."""
    resolver = _build_resolver()
    resolver._locks = {}
    resolver._persisted_locks = {}
    stored = EIDGenerationLock(
        device_id="device-2",
        canonical_id="canonical-2",
        variant=EidVariant.SPEC_P256_X32_BE.value,
        advertisement_reversed=False,
        eid_length=32,
    ).to_dict()

    async def _load() -> list[dict[str, object]]:
        return [stored]

    resolver._store = SimpleNamespace(async_load=_load)
    await resolver._async_load_locks()

    assert resolver._persisted_locks == {}
    assert resolver.locked_variant_value("device-2") == "spec_p256_x32_be"
    assert resolver.locked_variant_value("device-1") is None


def test_locked_variant_value_returns_an_unknown_value_unchanged() -> None:
    """The stored string is returned as is; the caller decides what it means."""
    resolver = _build_resolver()
    resolver._locks["device-1"].variant = "no_such_variant"

    assert resolver.locked_variant_value("device-1") == "no_such_variant"


def test_locked_variant_value_follows_clear_and_stop() -> None:
    """Clearing one lock or stopping the resolver takes effect at once."""
    resolver = _build_resolver()
    assert resolver.locked_variant_value("device-1") == "modern_p256_x32_be"

    resolver._clear_lock_state("device-1")
    assert resolver.locked_variant_value("device-1") is None

    resolver = _build_resolver()
    resolver.stop()
    assert resolver.locked_variant_value("device-1") is None


# --- encryption_counter: newest match, then the lock projection ------------

_PERIOD = 1024


def _matched_resolver() -> GoogleFindMyEIDResolver:
    """Resolver with two lookup EIDs for ``dev-m`` in windows 7 and 9."""
    from tests.test_ble_battery_sensor import _make_resolver, _match

    resolver = _make_resolver()
    for marker, window in ((0x71, 7), (0x72, 9)):
        eid = bytes([marker]) * 20
        resolver._lookup[eid] = [_match("dev-m")]
        resolver._lookup_metadata[eid] = {
            "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
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

    observed = float(at - 1_700_000_000 + 50_000) if monotonic is None else monotonic
    # Delivered right when seen: _observation_clock then dates it at ``at``.
    with (
        patch("time.time", return_value=float(at)),
        patch("time.monotonic", return_value=observed),
    ):
        assert resolver.resolve_eid(
            _service_data_payload(bytes([marker]) * 20, 0), observed_at=observed
        )


def test_encryption_counter_is_none_without_match_or_timed_lock() -> None:
    """No match and a lock without rotation timestamp give no counter."""
    resolver = _build_resolver()  # its lock has no rotation_timestamp

    assert resolver.encryption_counter("device-1", now=1_700_000_000) is None
    assert resolver.encryption_counter("unknown", now=1_700_000_000) is None


def test_encryption_counter_projects_the_lock_by_whole_periods() -> None:
    """Without a match: rotation timestamp plus whole periods since creation."""
    resolver = _build_resolver()
    lock = resolver._locks["device-1"]
    lock.rotation_timestamp = 40 * _PERIOD
    lock.created_at = 1_700_000_000

    assert resolver.encryption_counter(
        "device-1", now=1_700_000_000 + 2 * _PERIOD + 5
    ) == (42 * _PERIOD, "lock")
    # A clock behind the lock creation does not move the counter backwards.
    assert resolver.encryption_counter("device-1", now=1_699_000_000) == (
        40 * _PERIOD,
        "lock",
    )


def test_encryption_counter_follows_the_newest_match_not_the_lock() -> None:
    """The lock keeps its first window; the counter follows later matches."""
    resolver = _matched_resolver()
    _see(resolver, 0x71, 1_700_000_000)
    _see(resolver, 0x72, 1_700_000_100)

    assert resolver._locks["dev-m"].rotation_timestamp == 7 * _PERIOD
    assert resolver.encryption_counter("dev-m", now=1_700_000_100) == (
        9 * _PERIOD,
        "last_match",
    )
    assert resolver.encryption_counter("dev-m", now=1_700_000_100 + 3 * _PERIOD) == (
        12 * _PERIOD,
        "last_match",
    )


def test_encryption_counter_advances_from_the_first_sighting_of_a_window() -> None:
    """A later sighting of the same window does not delay the rollover.

    Seen 3 s and 900 s into window 7: one period after the first sighting the
    counter is window 8, not 900 s later.
    """
    resolver = _matched_resolver()
    _see(resolver, 0x71, 1_700_000_003)
    _see(resolver, 0x71, 1_700_000_900)

    assert resolver.encryption_counter("dev-m", now=1_700_000_003 + _PERIOD + 10) == (
        8 * _PERIOD,
        "last_match",
    )


@pytest.mark.parametrize(
    ("second_wall", "second_monotonic"),
    [
        (1_700_005_000, 50_010.0),  # wall clock stepped forward
        (1_700_007_203, 57_200.0),  # device clock stood still for two hours
    ],
    ids=["wall_clock_forward", "device_clock_stood_still"],
)
def test_encryption_counter_stays_on_a_window_seen_again(
    second_wall: int, second_monotonic: float
) -> None:
    """A sighting projects to the window it shows.

    Window 7 is seen again a period or more after its first sighting; the
    counter at that moment is window 7, not a window the device has not
    reached.
    """
    resolver = _matched_resolver()
    _see(resolver, 0x71, 1_700_000_003, monotonic=50_000.0)
    _see(resolver, 0x71, second_wall, monotonic=second_monotonic)

    assert resolver.encryption_counter("dev-m", now=second_wall) == (
        7 * _PERIOD,
        "last_match",
    )


def test_encryption_counter_restarts_the_window_after_a_backward_wall_step() -> None:
    """After a backward step the window is timed from the new sighting.

    Without that, the counter would stay on window 7 for the size of the step
    beyond one period.
    """
    resolver = _matched_resolver()
    _see(resolver, 0x71, 1_700_000_003, monotonic=50_000.0)
    _see(resolver, 0x71, 1_699_995_000, monotonic=50_010.0)

    assert resolver.encryption_counter("dev-m", now=1_699_995_000 + _PERIOD + 5) == (
        8 * _PERIOD,
        "last_match",
    )


def test_encryption_counter_ignores_an_older_replayed_match() -> None:
    """A replay, older on the monotonic clock, keeps the newer window.

    The replayed sighting carries an earlier time on the monotonic clock; that
    clock orders the sightings within this process.
    """
    resolver = _matched_resolver()
    _see(resolver, 0x72, 1_700_000_100, monotonic=50_100.0)
    _see(resolver, 0x71, 1_700_000_100, monotonic=50_000.0)  # replayed, older

    assert resolver.encryption_counter("dev-m", now=1_700_000_100) == (
        9 * _PERIOD,
        "last_match",
    )


def test_encryption_counter_follows_a_newer_match_after_a_wall_clock_step() -> None:
    """Within this process, a wall clock stepped backwards does not freeze it.

    The second sighting is newer on the monotonic clock but carries an
    earlier wall time; it replaces the first, as it does for the lock. The
    first sighting after a restart is still ordered against the lock's wall
    stamp, as for drift_offset and last_seen_at.
    """
    resolver = _matched_resolver()
    _see(resolver, 0x71, 1_700_000_000, monotonic=50_000.0)
    _see(resolver, 0x72, 1_699_995_000, monotonic=50_100.0)

    assert resolver._locks["dev-m"].last_seen_at == 1_699_995_000
    assert resolver.encryption_counter("dev-m", now=1_699_995_000) == (
        9 * _PERIOD,
        "last_match",
    )


def test_encryption_counter_follows_clear_and_stop() -> None:
    """Clearing a device or stopping the resolver drops the remembered match."""
    resolver = _matched_resolver()
    _see(resolver, 0x71, 1_700_000_000)
    resolver._locks.clear()
    assert resolver.encryption_counter("dev-m", now=1_700_000_000) is not None

    resolver._clear_lock_state("dev-m")
    assert resolver.encryption_counter("dev-m", now=1_700_000_000) is None

    resolver = _matched_resolver()
    _see(resolver, 0x71, 1_700_000_000)
    resolver.stop()
    assert resolver.encryption_counter("dev-m", now=1_700_000_000) is None
