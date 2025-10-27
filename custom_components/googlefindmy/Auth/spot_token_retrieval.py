# custom_components/googlefindmy/Auth/spot_token_retrieval.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Spot token retrieval (async-first, HA-friendly; entry-scoped capable).

Primary API:
    - async_get_spot_token(username: Optional[str] = None, *, cache: TokenCache,
                           aas_provider: Callable[[], Awaitable[str]] | None = None) -> str

Design:
    - Async-first: obtains the Google username from the username provider when not supplied.
      Callers **must** provide the entry-scoped TokenCache so that all reads/writes are
      performed against that cache, guaranteeing strict multi-account isolation.
    - Token generation prefers the async token retriever (`async_request_token`). We inject
      an AAS provider derived from the SAME cache (lambda: async_get_aas_token(cache=cache))
      when no custom provider is supplied. This ensures true end-to-end entry scoping.
Caching:
    - Cache key: f"spot_token_{username}" (stored in the selected cache).

Multi-account compatibility:
    - Passing the entry-scoped `cache` isolates tokens per config entry. Legacy global
      cache fallbacks have been removed to prevent cross-account leakage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Awaitable

from .username_provider import async_get_username
from .token_cache import TokenCache

_LOGGER = logging.getLogger(__name__)


async def _async_generate_spot_token(
    username: str,
    *,
    cache: TokenCache,
    aas_provider: Callable[[], Awaitable[str]] | None = None,
) -> str:
    """Generate a fresh Spot token for `username` without blocking the loop.

    Prefers an async retriever if available; falls back to running the sync
    retriever inside a worker thread.

    Notes:
        - The AAS provider is passed through so the OAuth exchange can resolve the
          AAS token from the *same* entry-scoped cache when provided upstream.
    """
    try:
        # Prefer native async implementation if available.
        from .token_retrieval import async_request_token

        _LOGGER.debug("Using async_request_token for Spot token generation")
        token = await async_request_token(
            username,
            "spot",
            True,  # play_services=True
            cache=cache,
            aas_provider=aas_provider,
        )
        if not token:
            raise RuntimeError("async_request_token returned empty token")
        return token
    except ImportError:
        # No async entrypoint exported; fall back to sync retriever in a thread.
        _LOGGER.debug(
            "async_request_token not available; falling back to sync retriever in a thread"
        )
        from .token_retrieval import request_token  # sync path

        if aas_provider is not None:
            aas_token_value = await aas_provider()
        else:
            from .aas_token_retrieval import (
                async_get_aas_token,
            )  # lazy import for fallback

            aas_token_value = await async_get_aas_token(cache=cache)

        token = await asyncio.to_thread(
            request_token,
            username,
            "spot",
            True,
            aas_token=aas_token_value,
            cache=cache,
        )
        if not token:
            raise RuntimeError("request_token returned empty token")
        return token


async def async_get_spot_token(
    username: str | None = None,
    *,
    cache: TokenCache,
    aas_provider: Callable[[], Awaitable[str]] | None = None,
) -> str:
    """Return a Spot token for the given user (async, cached; entry-scoped when `cache` is provided).

    Behavior:
        - If `username` is None, resolve it via the async username provider
          (entry-scoped when `cache` is given).
        - Use the selected cache to return a cached token when present
          (entry cache preferred; otherwise legacy default cache).
        - Otherwise, generate a token and store it via the cache's async get-or-set.
        - The OAuth exchange uses an AAS provider that resolves from the same cache.

    Raises:
        RuntimeError: if the username cannot be determined or token retrieval fails.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    # Resolve username (entry-scoped).
    if not username:
        username = await async_get_username(cache=cache)

    if not isinstance(username, str) or not username:
        raise RuntimeError(
            "Google username is not configured; cannot obtain Spot token"
        )

    cache_key = f"spot_token_{username}"

    # Build an AAS provider that uses the SAME cache if the caller didn't supply one.
    if aas_provider is None:

        async def _fallback_aas_provider() -> str:
            from .aas_token_retrieval import async_get_aas_token  # lazy import

            return await async_get_aas_token(cache=cache)

        aas_provider = _fallback_aas_provider

    async def _generator() -> str:
        return await _async_generate_spot_token(
            username,
            cache=cache,
            aas_provider=aas_provider,
        )

    return await cache.get_or_set(cache_key, _generator)
