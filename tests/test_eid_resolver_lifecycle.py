# tests/test_eid_resolver_lifecycle.py
"""Lifecycle management tests for the EID resolver (stop, cleanup)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.eid_resolver import (
    EIDGenerationLock,
    EidVariant,
    GoogleFindMyEIDResolver,
)


def _fake_hass() -> SimpleNamespace:
    """Return a minimal hass stand-in."""
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
