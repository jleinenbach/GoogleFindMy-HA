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
    lock = _lock_with_variant("spec_p256_x20_trunc_be")
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
    lock = _lock_with_variant("spec_p256_x20_trunc_be")
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
    lock = _lock_with_variant("spec_p256_x20_trunc_be")
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
