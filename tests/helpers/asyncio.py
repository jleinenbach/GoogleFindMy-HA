# tests/helpers/asyncio.py
"""Asyncio event loop helpers for the Google Find My test suite."""

from __future__ import annotations

import asyncio
from contextlib import suppress


def drain_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel and drain all pending tasks before closing the loop."""

    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
        with suppress(Exception, asyncio.CancelledError):
            loop.run_until_complete(task)

    loop.run_until_complete(asyncio.sleep(0))
    loop.close()
    asyncio.set_event_loop(None)

