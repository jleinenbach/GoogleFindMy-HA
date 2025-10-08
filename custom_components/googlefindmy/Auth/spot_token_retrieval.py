# custom_components/googlefindmy/Auth/spot_token_retrieval.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Spot token retrieval (async-first, HA-friendly).

Primary API:
    - async_get_spot_token(username: Optional[str] = None) -> str

Design:
    - Async-first: obtains the Google username from the async username provider
      when not supplied, then uses the entry-scoped TokenCache to "get or set"
      a cached Spot token for that user.
    - Token generation prefers an async token retriever if present
      (`async_request_token`). If only a sync `request_token` is available,
      it is offloaded via `asyncio.to_thread` to avoid blocking the event loop.
    - A guarded sync wrapper (`get_spot_token`) is provided for CLI/tests and will
      raise if called from within a running event loop.

Caching:
    - Cache key: f"spot_token_{username}"

This module deliberately avoids heavy imports at module load. Token retrieval
functions are imported lazily inside helpers.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .username_provider import async_get_username
from .token_cache import async_get_cached_value_or_set

_LOGGER = logging.getLogger(__name__)


async def _async_generate_spot_token(username: str) -> str:
    """Generate a fresh Spot token for `username` without blocking the loop.

    Prefers an async retriever if available; falls back to running the sync
    retriever inside a worker thread.
    """
    try:
        # Prefer native async implementation if available.
        from .token_retrieval import async_request_token  # type: ignore[attr-defined]

        _LOGGER.debug("Using async_request_token for Spot token generation")
        token = await async_request_token(username, "spot", True)
        if not token:
            raise RuntimeError("async_request_token returned empty token")
        return token
    except ImportError:
        # No async entrypoint exported; fall back to sync retriever in a thread.
        _LOGGER.debug("async_request_token not available; falling back to sync retriever in a thread")
        from .token_retrieval import request_token  # sync path

        token = await asyncio.to_thread(request_token, username, "spot", True)
        if not token:
            raise RuntimeError("request_token returned empty token")
        return token


async def async_get_spot_token(username: Optional[str] = None) -> str:
    """Return a Spot token for the given user (async, cached).

    Behavior:
        - If `username` is None, resolve it via the async username provider.
        - Use the entry-scoped TokenCache to return a cached token when present.
        - Otherwise, generate a token and store it via the cache's async get-or-set.

    Raises:
        RuntimeError: if the username cannot be determined or token retrieval fails.
    """
    if not username:
        username = await async_get_username()
    if not isinstance(username, str) or not username:
        raise RuntimeError("Google username is not configured; cannot obtain Spot token")

    cache_key = f"spot_token_{username}"

    async def _generator() -> str:
        return await _async_generate_spot_token(username)

    token = await async_get_cached_value_or_set(cache_key, _generator)
    if not isinstance(token, str) or not token:
        raise RuntimeError("Spot token retrieval produced an invalid value")
    return token


# ----------------------- Legacy sync wrapper (CLI/tests) -----------------------

def get_spot_token(username: Optional[str] = None) -> str:
    """Sync wrapper for CLI/tests.

    IMPORTANT:
        - Must NOT be called from inside the Home Assistant event loop.
        - Prefer `await async_get_spot_token(...)` in all HA code paths.
    """
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            raise RuntimeError(
                "get_spot_token() was called inside the event loop. "
                "Use `await async_get_spot_token(...)` instead."
            )
    except RuntimeError:
        # No running loop -> safe to spin a private loop for CLI/tests.
        pass

    return asyncio.run(async_get_spot_token(username))


if __name__ == "__main__":
    # Simple CLI smoke test (requires cache + username to be initialized by the environment)
    try:
        print(get_spot_token())
    except Exception as exc:  # pragma: no cover
        _LOGGER.error("CLI Spot token retrieval failed: %s", exc)
        raise
