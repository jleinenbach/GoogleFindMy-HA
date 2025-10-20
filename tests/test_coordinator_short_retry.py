# tests/test_coordinator_short_retry.py
"""Regression tests for short-retry scheduling on the coordinator."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any, Callable, Coroutine
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.const import DOMAIN


class _DummyCache:
    """Minimal cache stub satisfying the coordinator constructor."""

    async def async_get_cached_value(self, _key: str) -> None:  # pragma: no cover - stub
        return None

    async def async_set_cached_value(self, _key: str, _value: Any) -> None:  # pragma: no cover - stub
        return None


class _DummyBus:
    """Provide an async_listen placeholder used by the coordinator."""

    def async_listen(self, *_args, **_kwargs) -> Callable[[], None]:  # pragma: no cover - stub
        return lambda: None


class _DummyHass:
    """Lightweight Home Assistant stub capturing created tasks."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.bus = _DummyBus()
        self.created: list[tuple[object, str | None]] = []

    def async_create_task(self, coro, *, name: str | None = None):  # noqa: D401 - stub signature
        self.created.append((coro, name))
        return self.loop.create_task(coro, name=name)


class _DummyAPI:
    """No-op API placeholder injected into the coordinator."""

    def __init__(self, *_args, **_kwargs) -> None:  # pragma: no cover - defensive
        pass

    def is_push_ready(self) -> bool:  # pragma: no cover - defensive
        return True


@pytest.fixture
def fresh_loop() -> asyncio.AbstractEventLoop:
    """Yield a fresh event loop for isolation in scheduler tests."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
            with contextlib.suppress(Exception):
                loop.run_until_complete(task)
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)


@pytest.fixture
def coordinator(monkeypatch: pytest.MonkeyPatch, fresh_loop: asyncio.AbstractEventLoop) -> GoogleFindMyCoordinator:
    """Instantiate a coordinator with patched dependencies for retry tests."""

    hass = _DummyHass(fresh_loop)

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyAPI",
        lambda *args, **kwargs: _DummyAPI(),
    )

    coord = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coord.async_set_updated_data = lambda _data: None

    fresh_loop.run_until_complete(asyncio.sleep(0))
    hass.created.clear()

    return coord


def test_short_retry_dispatches_refresh_task(
    coordinator: GoogleFindMyCoordinator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short retry callback schedules async_request_refresh as a task."""

    callbacks: dict[str, Any] = {}

    def _fake_async_call_later(_hass, _delay, callback):
        callbacks["cb"] = callback

        def _cancel() -> None:
            callbacks["cb"] = None

        callbacks["cancel"] = _cancel
        return _cancel

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.async_call_later", _fake_async_call_later
    )

    refresh_calls: list[str] = []

    async def _async_request_refresh() -> None:
        refresh_calls.append("called")

    coordinator.async_request_refresh = _async_request_refresh

    coordinator._schedule_short_retry(0.0)
    coordinator.hass.loop.run_until_complete(asyncio.sleep(0))

    cb = callbacks.get("cb")
    assert cb is not None

    cb(coordinator.hass.loop.time())
    coordinator.hass.loop.run_until_complete(asyncio.sleep(0))

    assert len(coordinator.hass.created) == 1
    coro, name = coordinator.hass.created[0]
    assert name == f"{DOMAIN}.short_retry_refresh"
    assert getattr(coro, "cr_code", None) is coordinator.async_request_refresh.__code__
    assert refresh_calls == ["called"]


def test_dispatch_async_request_refresh_marshals_to_loop_thread(
    coordinator: GoogleFindMyCoordinator,
) -> None:
    """Dispatch from a worker thread runs async_create_task on the loop thread."""

    loop = coordinator.hass.loop
    loop_thread_id = threading.get_ident()

    event = asyncio.Event()
    coroutine_threads: list[int] = []
    call_threads: list[int] = []
    call_count = 0

    async def _refresh_body() -> None:
        coroutine_threads.append(threading.get_ident())
        event.set()

    def _async_request_refresh() -> Coroutine[Any, Any, None]:
        nonlocal call_count
        call_count += 1
        return _refresh_body()

    coordinator.async_request_refresh = _async_request_refresh

    original_async_create_task = coordinator.hass.async_create_task

    def _wrapped_async_create_task(coro, *, name: str | None = None):
        call_threads.append(threading.get_ident())
        return original_async_create_task(coro, name=name)

    coordinator.hass.async_create_task = _wrapped_async_create_task

    def _worker() -> None:
        coordinator._dispatch_async_request_refresh(
            task_name=f"{DOMAIN}.thread_refresh",
            log_context="thread-test",
        )

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()

    loop.run_until_complete(asyncio.wait_for(event.wait(), timeout=1))
    loop.run_until_complete(asyncio.sleep(0))

    assert call_count == 1
    assert call_threads == [loop_thread_id]
    assert coroutine_threads == [loop_thread_id]
    assert coordinator.hass.created
    coro, name = coordinator.hass.created[-1]
    assert name == f"{DOMAIN}.thread_refresh"
    assert getattr(coro, "cr_code", None) is _refresh_body.__code__
