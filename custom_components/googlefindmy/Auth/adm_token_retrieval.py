#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
ADM (Android Device Manager) token retrieval for the Google Find My Device integration.

This module exposes an async-first API to obtain (and cache) ADM tokens per user.
It uses the integration's TokenCache (HA Store-backed) for persistence and avoids
blocking the Home Assistant event loop by offloading any synchronous library calls
to an executor.

Key points
----------
- Multi-entry safe: all state is kept in the shared TokenCache.
- Async-first: `async_get_adm_token()` is the primary API.
- Legacy sync facade: `get_adm_token()` is provided for CLI/offline usage only
  and will raise a RuntimeError if called from within the HA event loop.

Cache keys
----------
- adm_token_<email>                  -> str : the ADM token
- adm_token_issued_at_<email>        -> float (epoch seconds)
- adm_probe_startup_left_<email>     -> int : bootstrap probe counter (optional)
- username                           -> str : canonical username (see username_provider)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from custom_components.googlefindmy.Auth.token_retrieval import request_token
from custom_components.googlefindmy.Auth.username_provider import (
    async_get_username,
    username_string,
)
from custom_components.googlefindmy.Auth.token_cache import (
    async_get_cached_value,
    async_set_cached_value,
)

_LOGGER = logging.getLogger(__name__)


async def _seed_username_in_cache(username: str) -> None:
    """Ensure the canonical username cache key is populated (idempotent)."""
    try:
        cached = await async_get_cached_value(username_string)
        if cached != username and isinstance(username, str) and username:
            await async_set_cached_value(username_string, username)
            _LOGGER.debug("Seeded username cache key '%s' with '%s'.", username_string, username)
    except Exception as exc:  # defensive: never fail token flow on seeding
        _LOGGER.debug("Username cache seeding skipped: %s", exc)


async def _generate_adm_token(username: str) -> str:
    """Generate a new ADM token for the given user using the sync helper in an executor.

    The underlying `request_token(...)` is synchronous; we run it in a thread
    to avoid blocking the HA event loop.

    Raises:
        RuntimeError: If the token exchange returns an empty/invalid result.
    """
    await _seed_username_in_cache(username)

    loop = asyncio.get_running_loop()

    def _blocking_exchange() -> str:
        return request_token(username, "android_device_manager")

    token: str = await loop.run_in_executor(None, _blocking_exchange)
    if not token:
        raise RuntimeError("request_token() returned an empty ADM token")
    return token


async def async_get_adm_token(
    username: Optional[str] = None,
    *,
    retries: int = 2,
    backoff: float = 1.0,
) -> str:
    """Return a cached ADM token or generate a new one (async-first API).

    Args:
        username: Optional explicit username (email). If not provided, the value
            is resolved from the username cache via `async_get_username()`.
        retries: Number of retry attempts after the initial try (total = retries + 1).
        backoff: Initial backoff in seconds; doubled for each retry (exponential).

    Returns:
        The ADM token as a string.

    Raises:
        RuntimeError: If username is missing/invalid or token generation ultimately fails.
        Exception: Propagates unexpected exceptions from the underlying token flow.
    """
    # Resolve username first
    user = username or await async_get_username()
    if not isinstance(user, str) or not user:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token.")

    cache_key = f"adm_token_{user}"

    # 1) Fast path: cache hit
    token = await async_get_cached_value(cache_key)
    if isinstance(token, str) and token:
        return token

    # 2) Miss -> bounded retries with async backoff
    last_exc: Optional[Exception] = None
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            tok = await _generate_adm_token(user)

            # Persist token & issued-at metadata for TTL policy users
            await async_set_cached_value(cache_key, tok)
            await async_set_cached_value(f"adm_token_issued_at_{user}", time.time())

            # Bootstrap probe counter for TTL calibration on fresh installs (best-effort)
            probe_key = f"adm_probe_startup_left_{user}"
            current_probe = await async_get_cached_value(probe_key)
            if current_probe is None:
                await async_set_cached_value(probe_key, 3)

            return tok

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts - 1:
                sleep_s = backoff * (2**attempt)
                _LOGGER.info(
                    "ADM token generation failed (attempt %s/%s): %s — retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    exc,
                    sleep_s,
                )
                await asyncio.sleep(sleep_s)
                continue
            _LOGGER.error("ADM token generation failed after %s attempts: %s", attempts, exc)

    # If we reach here, all attempts failed
    assert last_exc is not None
    raise last_exc


# --------------------- Legacy sync facade (CLI/offline only) ---------------------


def get_adm_token(
    username: Optional[str] = None,
    *,
    retries: int = 2,
    backoff: float = 1.0,
) -> str:
    """Legacy synchronous facade for CLI/offline contexts.

    IMPORTANT:
        Do NOT call this from within the Home Assistant event loop. It will raise
        a RuntimeError to prevent deadlocks. Within HA, always use `async_get_adm_token()`.

    Args:
        username: Optional explicit username/email.
        retries: Number of retry attempts after the initial try (total = retries + 1).
        backoff: Initial backoff in seconds; doubled for each retry.

    Returns:
        The ADM token as a string.

    Raises:
        RuntimeError: If called from within a running event loop.
        Exception: Any error propagated from the async implementation.
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
