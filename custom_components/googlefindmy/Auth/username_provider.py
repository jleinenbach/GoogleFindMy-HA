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
- Legacy sync wrappers are provided for backward compatibility but will raise
  a RuntimeError if called from within the Home Assistant event loop to prevent
  deadlocks. They remain safe for use from worker threads only.

The underlying persistence is handled by the entry-scoped TokenCache (HA Store).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .token_cache import (
    async_get_cached_value,
    async_set_cached_value,
    # Legacy sync facades (safe only outside the event loop)
    get_cached_value as _legacy_get_cached_value,
    set_cached_value as _legacy_set_cached_value,
)

_LOGGER = logging.getLogger(__name__)

# Single well-known cache key for the Google account e-mail
username_string = "username"


async def async_get_username() -> Optional[str]:
    """Return the configured Google account e-mail from the async token cache.

    Returns:
        The username (e-mail) if present and a string, otherwise ``None``.
    """
    val = await async_get_cached_value(username_string)
    return str(val) if isinstance(val, str) else None


async def async_set_username(username: str) -> None:
    """Seed or update the username in the async token cache.

    Args:
        username: The Google account e-mail to persist.

    Raises:
        ValueError: If the provided username is empty or not a string.
    """
    if not isinstance(username, str) or not username:
        raise ValueError("Username must be a non-empty string.")
    await async_set_cached_value(username_string, username)


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

    username = _legacy_get_cached_value(username_string)
    if isinstance(username, str) and username:
        return username

    # Fail fast instead of returning a placeholder that would cause PERMISSION_DENIED later.
    _LOGGER.error(
        "No Google username configured in cache key '%s'. Please configure the account in the UI.",
        username_string,
    )
    raise RuntimeError(
        "Google username is not configured. Open the integration UI and set the account."
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
    if not isinstance(username, str) or not username:
        raise ValueError("Username must be a non-empty string.")

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

    _legacy_set_cached_value(username_string, username)


__all__ = [
    "username_string",
    "async_get_username",
    "async_set_username",
    "get_username",
    "set_username",
]
