# custom_components/googlefindmy/Auth/adm_token_retrieval.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
ADM (Android Device Manager) token retrieval for the Google Find My Device integration.

This module provides an async-first API to obtain an ADM (Android Device Manager)
token, which is required for all interactions with the Nova API (e.g., listing
devices, requesting locations).

Design & Fix for BadAuthentication:
- Async-first: `async_get_adm_token()` is the primary API.
- **PATCH**: This version reverts to the direct token retrieval method used in older,
  functional versions. It removes the dependency on `aas_token_retrieval.py` which
  introduced overly strict validations and caused `BadAuthentication` errors for
  configurations relying on a cached `aas_token`.
- It now directly calls `async_request_token` from `token_retrieval.py`, delegating
  the entire authentication chain to a single, specialized module. This simplifies
  the logic and restores compatibility.
- Blocking `gpsoauth` calls within the token retrieval process are executed in a
  thread executor by the underlying `async_request_token` function.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

# Import the direct, async token requester, restoring the old, functional architecture.
from custom_components.googlefindmy.Auth.token_retrieval import \
    async_request_token
from custom_components.googlefindmy.Auth.token_cache import (
    async_get_cached_value, async_get_cached_value_or_set,
    async_set_cached_value)
from custom_components.googlefindmy.Auth.username_provider import \
    async_get_username

_LOGGER = logging.getLogger(__name__)


async def _generate_adm_token(username: str) -> str:
    """
    Generate a new ADM token by directly calling the central token retriever.

    This function restores the simpler, more robust logic of older versions by
    delegating the entire token exchange process to `async_request_token`.

    Args:
        username: The Google account e-mail for the request context.

    Returns:
        The generated ADM token string.
    """
    _LOGGER.debug("Generating new ADM token for user %s", username)
    # Directly request the specific token needed. The underlying function will handle
    # the necessary AAS token acquisition and exchange.
    return await async_request_token(username, "android_device_manager")


async def async_get_adm_token(
    username: Optional[str] = None,
    *,
    retries: int = 2,
    backoff: float = 1.0,
) -> str:
    """
    Return a cached ADM token or generate a new one (async-first API).

    This is the main entry point for other modules to get a valid ADM token.

    Args:
        username: Optional explicit username. If None, it's resolved from cache.
        retries: Number of retry attempts on failure.
        backoff: Initial backoff delay in seconds for retries.

    Returns:
        The ADM token string.

    Raises:
        RuntimeError: If the username is invalid or token generation fails after all retries.
    """
    user = username or await async_get_username()
    if not isinstance(user, str) or not user:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token.")

    cache_key = f"adm_token_{user}"

    # Use get_cached_value_or_set to handle caching, generation, and retries atomically.
    # The generator lambda captures the username for the generation function.
    generator = lambda: _generate_adm_token(user)

    # Note: The retry logic is now implicitly handled by the robust get_or_set,
    # but we keep an explicit loop for logging and backoff control.
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            # The `get_cached_value_or_set` pattern ensures the generator is only
            # called if the value is not in the cache.
            token = await async_get_cached_value_or_set(cache_key, generator)

            # Persist TTL metadata upon successful generation
            if not await async_get_cached_value(f"adm_token_issued_at_{user}"):
                 await async_set_cached_value(f"adm_token_issued_at_{user}", time.time())
            if not await async_get_cached_value(f"adm_probe_startup_left_{user}"):
                await async_set_cached_value(f"adm_probe_startup_left_{user}", 3)

            return token
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                sleep_s = backoff * (2**attempt)
                _LOGGER.info(
                    "ADM token generation failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    retries + 1,
                    exc,
                    sleep_s,
                )
                # Clear potentially bad cache entry before retrying
                await async_set_cached_value(cache_key, None)
                await asyncio.sleep(sleep_s)
                continue
            _LOGGER.error("ADM token generation failed after %d attempts: %s", retries + 1, exc)

    assert last_exc is not None
    raise last_exc


# --------------------- Legacy sync facade (CLI/offline only) ---------------------

def get_adm_token(
    username: Optional[str] = None,
    *,
    retries: int = 2,
    backoff: float = 1.0,
) -> str:
    """
    Synchronous facade for CLI/offline usage; not allowed in the HA event loop.
    
    Raises:
        RuntimeError: If called from within a running event loop.
    """
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            raise RuntimeError(
                "Sync get_adm_token() called from the event loop. "
                "Use `await async_get_adm_token()` instead."
            )
    except RuntimeError:
        # No running loop -> allowed (CLI/offline usage)
        return asyncio.run(async_get_adm_token(username, retries=retries, backoff=backoff))
