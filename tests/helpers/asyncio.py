# tests/helpers/asyncio.py
"""Asyncio event loop helpers for the Google Find My test suite."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from contextlib import suppress

# Bound for joining the default executor. ``asyncio.run()`` allows 300 s; a
# test helper fails faster, so a stuck executor job surfaces as a warning and a
# lingering-thread failure instead of a hung CI job.
EXECUTOR_JOIN_TIMEOUT_S = 30.0


def drain_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel and drain all pending tasks, stop the default executor, close.

    ``loop.close()`` stops the default executor with ``wait=False``, so a
    worker thread started by ``run_in_executor`` or ``asyncio.to_thread`` can
    outlive the loop. The Home Assistant test plugin (``verify_cleanup``)
    counts every thread left after a test and fails the teardown, so the
    executor is shut down and joined first, as ``asyncio.run()`` does, but
    bounded by ``EXECUTOR_JOIN_TIMEOUT_S``.
    """

    if loop.is_closed():
        return

    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            loop.run_until_complete(task)

    loop.run_until_complete(asyncio.sleep(0))
    loop.run_until_complete(
        loop.shutdown_default_executor(timeout=EXECUTOR_JOIN_TIMEOUT_S)
    )
    loop.close()


def run_loop_until(
    loop: asyncio.AbstractEventLoop,
    predicate: Callable[[], bool],
    *,
    timeout: float = 5.0,
    description: str = "the condition",
) -> None:
    """Run ``loop`` in short steps until ``predicate()`` holds.

    Replaces a fixed ``loop.run_until_complete(asyncio.sleep(0.01))`` before an
    assertion: work that crosses an executor thread can outlast any short fixed
    sleep on a loaded runner. The helper waits for the condition itself;
    ``timeout`` is only the failure bound, after which it raises
    ``AssertionError`` naming ``description``.
    """

    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"timed out after {timeout}s waiting for {description}"
            )
        loop.run_until_complete(asyncio.sleep(0.01))
