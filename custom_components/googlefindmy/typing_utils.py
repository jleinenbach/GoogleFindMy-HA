# custom_components/googlefindmy/typing_utils.py
"""Shared typing-focused helpers for the googlefindmy integration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

__all__ = ["run_in_executor"]

_T = TypeVar("_T")


def run_in_executor(func: Callable[..., _T], *args: Any) -> Awaitable[_T]:
    """Execute ``func`` in Home Assistant's default executor.

    The helper centralizes the `TypeVar`-preserving wrapper around
    :meth:`asyncio.loop.run_in_executor` so call sites across the
    integration can offload blocking functions without re-implementing
    the typing boilerplate.  Keeping the helper in this module ensures
    both runtime behavior and annotations stay consistent when reused by
    multiple packages (for example, KeyBackup and Spot API helpers).
    """

    loop = asyncio.get_running_loop()
    return loop.run_in_executor(None, func, *args)
