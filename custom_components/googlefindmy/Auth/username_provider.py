# custom_components/googlefindmy/Auth/username_provider.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Username provider for the Google Find My Device integration.

This module exposes a single well-known cache key (`username_string`) and a
minimal API to read/write the configured Google account e-mail.

Design:
- Async-first: `async_get_username` / `async_set_username` are the primary API.
- Entry scoped: callers **must** provide the `TokenCache` instance that belongs
  to their config entry. This enforces strict multi-account isolation.
- Legacy sync wrappers are retained for compatibility with out-of-tree tooling
  but will raise when the global cache facade is no longer available.

Persistence:
- The underlying persistence is handled by the entry-scoped TokenCache (HA
  Store). Global cache facades are no longer used inside the async code paths.
"""

from __future__ import annotations

import asyncio

from .token_cache import TokenCache

# Single well-known cache key for the Google account e-mail
username_string = "username"


async def async_get_username(*, cache: TokenCache) -> str | None:
    """Return the configured Google account e-mail.

    The username is read strictly from the provided entry-scoped TokenCache.

    Args:
        cache: Entry-scoped TokenCache instance.

    Returns:
        The username (e-mail) if present and a string, otherwise ``None``.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    try:
        val = await cache.get(username_string)
    except Exception:
        # Defensive: fall through to None on cache I/O errors
        val = None
    return str(val).strip().lower() if isinstance(val, str) and val else None


async def async_set_username(username: str, *, cache: TokenCache) -> None:
    """Seed or update the username in the token cache (entry-scoped when provided).

    Normalizes the e-mail to lower-case and trims whitespace before writing.

    Args:
        username: The Google account e-mail to persist.
        cache: Entry-scoped TokenCache instance.

    Raises:
        ValueError: If the provided username is empty/invalid.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    if not isinstance(username, str):
        raise ValueError("Username must be a string.")
    norm = username.strip().lower()
    if not norm or "@" not in norm:
        raise ValueError("Username must be a non-empty e-mail address.")

    await cache.set(username_string, norm)


# ----------------------- Legacy sync wrappers (compat) -----------------------

def get_username() -> str:
    """Legacy sync getter for the username.

    IMPORTANT:
        - Must NOT be called from inside the Home Assistant event loop.
        - Prefer `await async_get_username()` instead.

    Returns:
        The username (e-mail) string if present.

    Raises:
        RuntimeError: If called from the event loop (risk of deadlock) or if the
            username is missing in the cache (fail-fast to avoid later API errors).
    """
    # Prevent deadlocks: disallow sync access from the HA event loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop => safe to proceed with legacy sync facade.
        pass
    else:
        raise RuntimeError(
            "Sync get_username() called from within the event loop. "
            "Use `await async_get_username()` instead."
        )

    raise RuntimeError(
        "Legacy get_username() is no longer supported without providing the entry TokenCache. "
        "Use `await async_get_username(cache=...)` instead."
    )


def set_username(username: str) -> None:
    """Legacy sync setter for the username.

    IMPORTANT:
        - Must NOT be called from inside the Home Assistant event loop.
        - Prefer `await async_set_username(...)` instead.

    Args:
        username: The Google account e-mail to persist.

    Raises:
        RuntimeError: If called from the event loop (risk of deadlock).
        ValueError: If the provided username is invalid.
    """
    if not isinstance(username, str):
        raise ValueError("Username must be a string.")
    norm = username.strip().lower()
    if not norm or "@" not in norm:
        raise ValueError("Username must be a non-empty e-mail address.")

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop => safe to proceed with legacy sync facade.
        pass
    else:
        raise RuntimeError(
            "Sync set_username() called from within the event loop. "
            "Use `await async_set_username(...)` instead."
        )

    raise RuntimeError(
        "Legacy set_username() is no longer supported without providing the entry TokenCache. "
        "Use `await async_set_username(..., cache=...)` instead."
    )


__all__ = [
    "username_string",
    "async_get_username",
    "async_set_username",
    "get_username",
    "set_username",
]
