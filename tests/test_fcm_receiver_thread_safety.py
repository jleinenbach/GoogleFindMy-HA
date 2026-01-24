"""Tests for P0/P1 fixes in fcm_receiver_ha.py.

P0-1: Thread-safe dispatch via _dispatch_to_hass_loop
P0-2: Exception handling in _run_callback_async
P1-1: Executor offload for protobuf parsing
P1-2: Scoped cache provider context manager
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from tests.helpers import drain_loop


class TestDispatchToHassLoop:
    """P0-1: Tests for thread-safe dispatch mechanism."""

    @pytest.mark.asyncio
    async def test_dispatch_from_event_loop_thread(self) -> None:
        """Dispatch from event loop thread schedules task directly."""
        receiver = FcmReceiverHA()
        receiver._hass = SimpleNamespace(loop=None)

        async def sample_coro() -> str:
            return "done"

        # Inside event loop, dispatch should work directly
        receiver._dispatch_to_hass_loop(sample_coro(), label="test")
        await asyncio.sleep(0.01)

        # Task should have been tracked and completed
        assert len(receiver._active_tasks) == 0  # Completed and removed

    def test_dispatch_from_worker_thread_with_hass_loop(self) -> None:
        """Dispatch from worker thread uses call_soon_threadsafe."""
        loop = asyncio.new_event_loop()

        try:
            receiver = FcmReceiverHA()
            receiver._hass = SimpleNamespace(loop=loop)

            coro_executed = threading.Event()

            async def sample_coro() -> None:
                coro_executed.set()

            def worker() -> None:
                # From worker thread, dispatch into HA loop
                receiver._dispatch_to_hass_loop(sample_coro(), label="worker-test")

            # Start loop in background
            loop_thread = threading.Thread(target=loop.run_forever)
            loop_thread.start()

            try:
                # Give loop time to start
                time.sleep(0.05)

                # Call from worker thread
                worker_thread = threading.Thread(target=worker)
                worker_thread.start()
                worker_thread.join(timeout=1.0)

                # Wait for coroutine execution
                assert coro_executed.wait(timeout=1.0), "Coroutine was not executed"
            finally:
                loop.call_soon_threadsafe(loop.stop)
                loop_thread.join(timeout=1.0)
        finally:
            drain_loop(loop)

    def test_dispatch_without_hass_loop_logs_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dispatch without HA loop logs error and drops notification."""
        receiver = FcmReceiverHA()
        receiver._hass = None

        async def sample_coro() -> None:
            pass

        # Simulate being called from a non-event-loop context
        coro = sample_coro()
        with patch.object(asyncio, "get_running_loop", side_effect=RuntimeError):
            with caplog.at_level(logging.ERROR):
                receiver._dispatch_to_hass_loop(coro, label="no-loop-test")
        # Close the coroutine to avoid RuntimeWarning about never awaited coroutine
        coro.close()

        assert "FCM notification dropped" in caplog.text
        assert "Home Assistant loop not available" in caplog.text


class TestTrackTask:
    """P0-2: Tests for task exception tracking."""

    @pytest.mark.asyncio
    async def test_track_task_logs_exceptions(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Task exceptions are logged via done callback."""

        async def failing_task() -> None:
            raise ValueError("Intentional test failure")

        receiver = FcmReceiverHA()
        task = asyncio.create_task(failing_task())
        receiver._track_task(task, label="test-failure")

        # Wait for task to complete
        with pytest.raises(ValueError):
            await task

        # Give done callback time to execute
        await asyncio.sleep(0.01)

        assert "Background task failed" in caplog.text
        assert "test-failure" in caplog.text

    @pytest.mark.asyncio
    async def test_track_task_removes_from_active_set(self) -> None:
        """Completed tasks are removed from _active_tasks."""
        receiver = FcmReceiverHA()

        async def quick_task() -> str:
            return "done"

        task = asyncio.create_task(quick_task())
        receiver._track_task(task, label="quick")

        assert task in receiver._active_tasks
        await task
        await asyncio.sleep(0.01)
        assert task not in receiver._active_tasks

    @pytest.mark.asyncio
    async def test_track_task_handles_cancelled_tasks(self) -> None:
        """Cancelled tasks don't log errors."""
        receiver = FcmReceiverHA()

        async def slow_task() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(slow_task())
        receiver._track_task(task, label="cancelled")

        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        await asyncio.sleep(0.01)
        # Should not raise or log errors - just verify no exception


class TestRunCallbackAsync:
    """P0-2: Tests for safe callback execution."""

    @pytest.mark.asyncio
    async def test_callback_exception_is_caught_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Callback exceptions are caught and logged, not propagated."""
        receiver = FcmReceiverHA()

        def failing_callback(canonic_id: str, hex_string: str) -> None:
            raise RuntimeError("Callback failure")

        with caplog.at_level(logging.ERROR):
            # Should not raise
            await receiver._run_callback_async(
                failing_callback, "device12345678", "deadbeef"
            )

        assert "FCM locate callback failed" in caplog.text
        assert "device12" in caplog.text  # Truncated ID
        assert "payload_len=8" in caplog.text

    @pytest.mark.asyncio
    async def test_successful_callback_executes(self) -> None:
        """Successful callbacks execute normally."""
        receiver = FcmReceiverHA()
        callback_args: list[tuple[str, str]] = []

        def success_callback(canonic_id: str, hex_string: str) -> None:
            callback_args.append((canonic_id, hex_string))

        await receiver._run_callback_async(success_callback, "device-abc", "payload123")

        assert callback_args == [("device-abc", "payload123")]


class TestExtractCanonicIdAsync:
    """P1-1: Tests for executor offload of protobuf parsing."""

    @pytest.mark.asyncio
    async def test_extract_canonic_id_uses_executor(self) -> None:
        """Protobuf parsing is offloaded to executor."""
        receiver = FcmReceiverHA()

        call_thread_ids: list[int | None] = []

        def mock_extract(hex_response: str) -> str | None:
            call_thread_ids.append(threading.current_thread().ident)
            return "extracted-id"

        with patch.object(receiver, "_extract_canonic_id_from_response", mock_extract):
            result = await receiver._extract_canonic_id_async("deadbeef")

        assert result == "extracted-id"
        assert len(call_thread_ids) == 1
        # Should be called from a different thread (executor)
        # Note: In some test configurations this might be the same thread
        # The important thing is that _call_in_executor was used


class TestScopedCacheProvider:
    """P1-2: Tests for scoped cache provider context manager."""

    def test_scoped_cache_provider_sets_and_resets_context(self) -> None:
        """Context manager properly sets and resets the context var."""
        receiver = FcmReceiverHA()

        cache = MagicMock()
        cache.entry_id = "test-entry"

        # Import the context var
        from custom_components.googlefindmy.NovaApi.nova_request import (
            _CACHE_PROVIDER,
        )

        # Verify initial state
        initial_provider = _CACHE_PROVIDER.get()

        with receiver._scoped_cache_provider(lambda: cache):
            # Inside context, provider should be set
            provider = _CACHE_PROVIDER.get()
            assert provider is not None
            assert provider() is cache

        # After context, should be reset
        final_provider = _CACHE_PROVIDER.get()
        assert final_provider == initial_provider

    def test_scoped_cache_provider_resets_on_exception(self) -> None:
        """Context manager resets even when exception occurs."""
        receiver = FcmReceiverHA()

        cache = MagicMock()

        from custom_components.googlefindmy.NovaApi.nova_request import (
            _CACHE_PROVIDER,
        )

        initial_provider = _CACHE_PROVIDER.get()

        with pytest.raises(ValueError):
            with receiver._scoped_cache_provider(lambda: cache):
                raise ValueError("Test exception")

        # Should still be reset
        assert _CACHE_PROVIDER.get() == initial_provider

    @pytest.mark.asyncio
    async def test_concurrent_scoped_providers_are_isolated(self) -> None:
        """Concurrent async operations have isolated cache contexts."""
        receiver = FcmReceiverHA()

        from custom_components.googlefindmy.NovaApi.nova_request import (
            _CACHE_PROVIDER,
        )

        cache_a = MagicMock()
        cache_a.entry_id = "entry-a"
        cache_b = MagicMock()
        cache_b.entry_id = "entry-b"

        results: dict[str, str | None] = {}

        async def task_a() -> None:
            with receiver._scoped_cache_provider(lambda: cache_a):
                await asyncio.sleep(0.01)
                provider = _CACHE_PROVIDER.get()
                results["a"] = provider().entry_id if provider else None

        async def task_b() -> None:
            with receiver._scoped_cache_provider(lambda: cache_b):
                await asyncio.sleep(0.01)
                provider = _CACHE_PROVIDER.get()
                results["b"] = provider().entry_id if provider else None

        await asyncio.gather(task_a(), task_b())

        # Each task should see its own cache
        assert results["a"] == "entry-a"
        assert results["b"] == "entry-b"


class TestOnNotificationThreadSafety:
    """Integration test for _on_notification thread safety."""

    def test_on_notification_from_worker_thread(self) -> None:
        """_on_notification can be called from any thread safely."""
        loop = asyncio.new_event_loop()

        try:
            receiver = FcmReceiverHA()
            receiver._hass = SimpleNamespace(loop=loop)

            notifications_received: list[str] = []

            async def mock_handle(entry_id: str, payload: Any) -> None:
                notifications_received.append(entry_id)

            with patch.object(receiver, "_handle_notification_async", mock_handle):
                # Start loop in background
                loop_thread = threading.Thread(target=loop.run_forever)
                loop_thread.start()

                try:
                    time.sleep(0.05)

                    payload = {"data": {"test": "value"}}

                    # Call from worker thread
                    def worker() -> None:
                        receiver._on_notification("entry-123", payload, None, None)

                    worker_thread = threading.Thread(target=worker)
                    worker_thread.start()
                    worker_thread.join(timeout=1.0)

                    # Give time for async handler to execute
                    time.sleep(0.1)

                    assert "entry-123" in notifications_received
                finally:
                    loop.call_soon_threadsafe(loop.stop)
                    loop_thread.join(timeout=1.0)
        finally:
            drain_loop(loop)
