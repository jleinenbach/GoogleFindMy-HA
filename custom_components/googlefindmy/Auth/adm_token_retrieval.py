#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
ADM (Android Device Manager) token retrieval for the Google Find My Device integration.

Async-first implementation:
- Uses async AAS token retrieval to avoid event-loop blocking.
- Runs gpsoauth.perform_oauth (blocking) inside an executor.
- Persists TTL metadata in the token cache (used by Nova TTL policies).

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
- android_id_<email>                 -> int : unique Android ID per user (extracted from fcm_credentials or generated)
- username                           -> str : canonical username (see username_provider)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Optional

import gpsoauth

from custom_components.googlefindmy.Auth.aas_token_retrieval import async_get_aas_token
from custom_components.googlefindmy.Auth.username_provider import (
    async_get_username,
    username_string,
)
from custom_components.googlefindmy.Auth.token_cache import (
    async_get_cached_value,
    async_set_cached_value,
)

_LOGGER = logging.getLogger(__name__)

# Constants for gpsoauth
_ANDROID_ID_FALLBACK: int = 0x38918A453D071993  # Fallback only - prefer per-user ID
_CLIENT_SIG: str = "38918a453d07199354f8b19af05ec6562ced5788"
_APP_ID: str = "com.google.android.apps.adm"


async def _seed_username_in_cache(username: str) -> None:
    """Ensure the canonical username cache key is populated (idempotent)."""
    try:
        cached = await async_get_cached_value(username_string)
        if cached != username and isinstance(username, str) and username:
            await async_set_cached_value(username_string, username)
            _LOGGER.debug("Seeded username cache key '%s' with '%s'.", username_string, username)
    except Exception as exc:  # defensive: never fail token flow on seeding
        _LOGGER.debug("Username cache seeding skipped: %s", exc)


async def _get_or_generate_android_id(username: str) -> int:
    """Get or generate a unique Android ID for this user.

    Strategy:
        1) Check cache for android_id_{username}
        2) If not found, try to extract from fcm_credentials.gcm.android_id
        3) If still not found, generate a random 64-bit Android ID
        4) Store in cache and return

    Args:
        username: The Google account email.

    Returns:
        A 64-bit Android ID (int) unique to this user.
    """
    cache_key = f"android_id_{username}"

    # Fast path: already cached
    try:
        cached_id = await async_get_cached_value(cache_key)
        if cached_id is not None:
            try:
                return int(cached_id)
            except (ValueError, TypeError):
                _LOGGER.warning("Cached android_id for %s is invalid; will regenerate.", username)
    except Exception as e:  # noqa: BLE001
        # Cache not available (multi-entry, validation, etc.) - continue to generation
        _LOGGER.debug("Cache not available for android_id lookup: %s. Will generate temporary Android ID.", e)

    # Try to extract from fcm_credentials
    try:
        fcm_creds = await async_get_cached_value("fcm_credentials")
        if isinstance(fcm_creds, dict):
            try:
                android_id = fcm_creds.get("gcm", {}).get("android_id")
                if android_id is not None:
                    android_id_int = int(android_id)
                    _LOGGER.info("Extracted android_id from fcm_credentials for user %s: %s", username, hex(android_id_int))
                    try:
                        await async_set_cached_value(cache_key, android_id_int)
                    except Exception:  # noqa: BLE001
                        # Can't cache during validation; that's OK
                        pass
                    return android_id_int
            except (ValueError, TypeError, KeyError) as e:
                _LOGGER.debug("Failed to extract android_id from fcm_credentials: %s", e)
    except Exception:  # noqa: BLE001
        # Cache not available; proceed to generation
        pass

    # Generate a new random Android ID (64-bit positive integer)
    # Use a secure random to avoid collisions
    new_id = random.randint(0x1000000000000000, 0xFFFFFFFFFFFFFFFF)
    _LOGGER.warning(
        "No android_id found for user %s; generated new random ID: %s. "
        "This may indicate the user needs to re-run GoogleFindMyTools to get fcm_credentials.",
        username,
        hex(new_id)
    )
    try:
        await async_set_cached_value(cache_key, new_id)
    except Exception:  # noqa: BLE001
        # Can't cache during config flow validation; the ID will be regenerated properly after entry creation
        _LOGGER.debug("Cannot cache android_id during validation; will cache after entry is created.")
    return new_id


async def _perform_oauth_with_aas(username: str, android_id: int) -> str:
    """Exchange AAS -> scope token (ADM) using gpsoauth in a thread executor.

    Args:
        username: The Google account email.
        android_id: The unique Android ID for this user.

    Returns:
        The ADM token string.
    """
    # Get AAS token asynchronously (it handles its own blocking via executor)
    aas_token = await async_get_aas_token()

    def _run() -> str:
        """Synchronous part to be run in an executor."""
        resp = gpsoauth.perform_oauth(
            username,
            aas_token,
            android_id,  # Use per-user Android ID instead of hardcoded value
            service="oauth2:https://www.googleapis.com/auth/android_device_manager",
            app=_APP_ID,
            client_sig=_CLIENT_SIG,
        )
        if not resp or "Auth" not in resp:
            raise RuntimeError(f"gpsoauth.perform_oauth returned invalid response: {resp}")
        return resp["Auth"]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


async def async_get_adm_token(
    username: Optional[str] = None,
    *,
    retries: int = 2,
    backoff: float = 1.0,
) -> str:
    """
    Return a cached ADM token or generate a new one (async-first API).

    Args:
        username: Optional explicit username. If None, it's resolved from cache.
        retries: Number of retry attempts on failure.
        backoff: Initial backoff delay in seconds for retries.

    Returns:
        The ADM token string.

    Raises:
        RuntimeError: If the username is invalid or token generation fails after all retries.
    """
    # Safe username retrieval - handle cache errors during validation
    if username:
        user = username
    else:
        try:
            user = await async_get_username()
        except Exception:  # noqa: BLE001
            # Cache not available during validation
            user = None

    if not isinstance(user, str) or not user:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token.")

    cache_key = f"adm_token_{user}"

    # Cache fast-path - handle cache errors during validation
    token = None
    try:
        token = await async_get_cached_value(cache_key)
    except Exception:  # noqa: BLE001
        # Cache not available during validation
        pass
    if isinstance(token, str) and token:
        return token

    # Generate with bounded retries
    last_exc: Optional[Exception] = None
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            await _seed_username_in_cache(user)

            # Get unique Android ID for this user
            android_id = await _get_or_generate_android_id(user)

            tok = await _perform_oauth_with_aas(user, android_id)

            # Persist token & issued-at metadata (safe during validation)
            try:
                await async_set_cached_value(cache_key, tok)
                await async_set_cached_value(f"adm_token_issued_at_{user}", time.time())
                probe_left = await async_get_cached_value(f"adm_probe_startup_left_{user}")
                if not probe_left:
                    await async_set_cached_value(f"adm_probe_startup_left_{user}", 3)
            except Exception:  # noqa: BLE001
                # Cache not available during validation - skip metadata persistence
                _LOGGER.debug("Cannot persist token metadata during validation; will persist after entry creation.")
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
