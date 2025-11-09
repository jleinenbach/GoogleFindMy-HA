#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""AAS token retrieval for the Google Find My Device integration.

This module provides an async-first API to obtain an Android AuthSub (AAS) token.
It exchanges an existing OAuth token for an AAS token using the `gpsoauth` library.
Blocking calls are executed in an executor to avoid blocking Home Assistant's event loop.

Design:
- Primary API: `async_get_aas_token()`, which uses the entry-scoped TokenCache.
- Cached retrieval via `async_get_cached_value_or_set` ensures we compute only once.
- Fallback: If no explicit OAuth token is present, reuse any cached `adm_token_*` value.
- Sync wrapper `get_aas_token()` is intentionally unsupported to prevent deadlocks.

Notes:
- The Android ID is unique per user (extracted from fcm_credentials or generated) and cached.
  This prevents Google from flagging the integration as suspicious due to shared credentials.
- The username is obtained from the cache via `username_provider`; if an ADM fallback
  is used, we also update the username accordingly.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, Optional

import gpsoauth

from .token_cache import (
    async_get_all_cached_values,
    async_get_cached_value,
    async_get_cached_value_or_set,
    async_set_cached_value,
)
from .username_provider import async_get_username, username_string
from ..const import CONF_OAUTH_TOKEN

_LOGGER = logging.getLogger(__name__)

# Fallback Android ID - prefer per-user ID (16-hex-digit integer).
_ANDROID_ID_FALLBACK: int = 0x38918A453D071993


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


async def _exchange_oauth_for_aas(username: str, oauth_token: str, android_id: int) -> Dict[str, Any]:
    """Run the blocking gpsoauth exchange in an executor.

    Args:
        username: Google account e-mail.
        oauth_token: OAuth token to exchange.
        android_id: The unique Android ID for this user.

    Returns:
        The raw dictionary response from gpsoauth containing at least a 'Token' key.

    Raises:
        RuntimeError: If the exchange fails or returns an invalid response.
    """
    def _run() -> Dict[str, Any]:
        # gpsoauth.exchange_token(username, oauth_token, android_id) is blocking.
        return gpsoauth.exchange_token(username, oauth_token, android_id)  # Use per-user Android ID

    loop = asyncio.get_running_loop()
    try:
        resp = await loop.run_in_executor(None, _run)
    except Exception as err:  # noqa: BLE001
        raise RuntimeError(f"gpsoauth exchange failed: {err}") from err

    if not isinstance(resp, dict) or not resp:
        raise RuntimeError("Invalid response from gpsoauth: empty or not a dict")
    if "Token" not in resp:
        raise RuntimeError(f"Missing 'Token' in gpsoauth response: {resp}")
    return resp


async def _generate_aas_token() -> str:
    """Generate an AAS token using the best available OAuth token and username.

    Strategy:
        1) Try the explicit OAuth token from the cache (`CONF_OAUTH_TOKEN`).
        2) If missing, scan for any `adm_token_*` key and reuse its value as an OAuth token.
           In that case, set `username` from the key suffix (after `adm_token_`).
        3) Exchange OAuth → AAS via gpsoauth in an executor.
        4) Update the cached username if gpsoauth returns an 'Email' field.

    Returns:
        The AAS token string.

    Raises:
        ValueError: If required inputs are missing.
        RuntimeError: If gpsoauth exchange fails or returns an invalid response.
    """
    # Start with the configured username if present.
    try:
        username: Optional[str] = await async_get_username()
    except Exception:  # noqa: BLE001
        # Cache not available during validation
        username = None

    # Prefer explicit OAuth token from cache.
    oauth_token: Optional[str] = None
    try:
        oauth_token = await async_get_cached_value(CONF_OAUTH_TOKEN)
    except Exception:  # noqa: BLE001
        # Cache not available during validation
        pass

    # Fallback: scan ADM tokens if no explicit OAuth token exists.
    if not oauth_token:
        try:
            all_cached = await async_get_all_cached_values()
        except Exception:  # noqa: BLE001
            # Cache not available during validation
            all_cached = {}
        for key, value in all_cached.items():
            if isinstance(key, str) and key.startswith("adm_token_") and isinstance(value, str) and value:
                # Reuse ADM token value as OAuth token.
                oauth_token = value
                extracted_username = key.replace("adm_token_", "", 1)
                if extracted_username and "@" in extracted_username:
                    username = extracted_username
                _LOGGER.info("Using existing ADM token from cache for OAuth exchange (user: %s).", username or "unknown")
                break

    if not oauth_token:
        raise ValueError("No OAuth token available; please configure the integration with a valid token.")
    if not username:
        raise ValueError("No username available; please ensure the account e-mail is configured.")

    # Get unique Android ID for this user
    android_id = await _get_or_generate_android_id(username)

    # Exchange OAuth → AAS (blocking call executed in executor).
    resp = await _exchange_oauth_for_aas(username, oauth_token, android_id)

    # Persist normalized email if gpsoauth returns it (keeps cache consistent).
    if isinstance(resp.get("Email"), str) and resp["Email"]:
        try:
            await async_set_cached_value(username_string, resp["Email"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to persist normalized username from gpsoauth: %s", err)

    return str(resp["Token"])


async def async_get_aas_token() -> str:
    """Return the cached AAS token or compute and cache it.

    Returns:
        The AAS token string.
    """
    # Safe cache access - handle multi-entry scenarios during validation
    try:
        return await async_get_cached_value_or_set("aas_token", _generate_aas_token)
    except Exception as e:  # noqa: BLE001
        # Cache not available during validation - generate directly without caching
        _LOGGER.debug("Cache not available for AAS token; generating directly: %s", e)
        return await _generate_aas_token()


# ----------------------- Legacy sync wrapper (unsupported) -----------------------

def get_aas_token() -> str:  # pragma: no cover - legacy path kept for compatibility messaging
    """Legacy sync API is intentionally unsupported to prevent event loop deadlocks.

    Raises:
        NotImplementedError: Always. Use `await async_get_aas_token()` instead.
    """
    raise NotImplementedError(
        "Use `await async_get_aas_token()` instead of the synchronous get_aas_token()."
    )
