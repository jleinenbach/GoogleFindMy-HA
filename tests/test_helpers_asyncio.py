# tests/test_helpers_asyncio.py
"""Tests for the event-loop helpers in ``tests/helpers/asyncio.py``."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.helpers import drain_loop, run_loop_until


def test_drain_loop_joins_default_executor_workers() -> None:
    """No worker of the loop's default executor survives ``drain_loop``.

    ``loop.close()`` alone stops the default executor with ``wait=False``, so a
    worker thread can outlive the loop. The Home Assistant test plugin
    (``verify_cleanup``) counts every thread left after a test and fails the
    teardown, which made ``test_unregister_prunes_token_routing`` flaky under
    load (thread ``asyncio_0``).

    The executor below releases its blocked job only from a waiting shutdown.
    A ``drain_loop`` that shuts down with ``wait=False`` therefore returns
    while the worker is still blocked, without any timing assumption.
    """
    release = threading.Event()
    started = threading.Event()
    workers: list[threading.Thread] = []

    class _ReleasingExecutor(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            if wait:
                release.set()
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    def _job() -> None:
        workers.append(threading.current_thread())
        started.set()
        release.wait()

    # Build a plain loop on purpose: older releases of the Home Assistant test
    # plugin install ``HassEventLoopPolicy``, whose loops refuse
    # ``set_default_executor`` outside a running ``hass``
    # ("Frame helper not set up").
    loop = asyncio.SelectorEventLoop()
    executor = _ReleasingExecutor(max_workers=1)
    loop.set_default_executor(executor)
    try:
        loop.run_in_executor(None, _job)
        assert started.wait(timeout=10), "executor job never started"

        drain_loop(loop)
        worker_alive_after_drain = workers[0].is_alive()
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=10)
        if not loop.is_closed():
            loop.close()

    assert loop.is_closed()
    assert not worker_alive_after_drain


def test_run_loop_until_returns_once_the_predicate_holds() -> None:
    """The helper keeps the loop running until scheduled work has happened."""
    loop = asyncio.new_event_loop()
    done: list[bool] = []
    try:
        loop.call_later(0.05, done.append, True)
        run_loop_until(loop, lambda: bool(done), description="the callback")
        assert done == [True]
    finally:
        drain_loop(loop)


def test_run_loop_until_fails_with_the_description_after_the_timeout() -> None:
    """A predicate that never holds fails loudly instead of hanging."""
    loop = asyncio.new_event_loop()
    try:
        with pytest.raises(AssertionError, match="waiting for the impossible"):
            run_loop_until(
                loop, lambda: False, timeout=0.05, description="the impossible"
            )
    finally:
        drain_loop(loop)
