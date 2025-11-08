#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

import gpsoauth

from custom_components.googlefindmy.Auth.aas_token_retrieval import get_aas_token, async_get_aas_token
from custom_components.googlefindmy.Auth.token_cache import (
    get_cached_value,
    set_cached_value,
    async_get_cached_value,
    async_set_cached_value,
)

_LOGGER = logging.getLogger(__name__)


def _get_or_generate_android_id_sync(username: str) -> int:
    """Get or generate a unique Android ID for this user (synchronous version).

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
        cached_id = get_cached_value(cache_key)
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
        fcm_creds = get_cached_value("fcm_credentials")
        if isinstance(fcm_creds, dict):
            try:
                android_id = fcm_creds.get("gcm", {}).get("android_id")
                if android_id is not None:
                    android_id_int = int(android_id)
                    _LOGGER.info("Extracted android_id from fcm_credentials for user %s: %s", username, hex(android_id_int))
                    try:
                        set_cached_value(cache_key, android_id_int)
                    except Exception:  # noqa: BLE001
                        # Can't cache; that's OK
                        pass
                    return android_id_int
            except (ValueError, TypeError, KeyError) as e:
                _LOGGER.debug("Failed to extract android_id from fcm_credentials: %s", e)
    except Exception:  # noqa: BLE001
        # Cache not available; proceed to generation
        pass

    # Generate a new random Android ID (64-bit positive integer)
    new_id = random.randint(0x1000000000000000, 0xFFFFFFFFFFFFFFFF)
    _LOGGER.warning(
        "No android_id found for user %s; generated new random ID: %s. "
        "This may indicate the user needs to re-run GoogleFindMyTools to get fcm_credentials.",
        username,
        hex(new_id)
    )
    try:
        set_cached_value(cache_key, new_id)
    except Exception:  # noqa: BLE001
        # Can't cache; the ID will be regenerated properly after entry creation
        _LOGGER.debug("Cannot cache android_id; will cache after entry is created.")
    return new_id


async def _get_or_generate_android_id_async(username: str) -> int:
    """Get or generate a unique Android ID for this user (async version).

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


def request_token(username: str, scope: str, play_services: bool = False) -> str:
    """Synchronous token request via gpsoauth (CLI/tests).

    WARNING: This is blocking. In Home Assistant use `await async_request_token(...)`
    or call this from an executor thread.
    """
    aas_token = get_aas_token()  # sync path (may block)

    # Get unique Android ID for this user
    android_id = _get_or_generate_android_id_sync(username)
    request_app = "com.google.android.gms" if play_services else "com.google.android.apps.adm"

    try:
        auth_response = gpsoauth.perform_oauth(
            username,
            aas_token,
            android_id,  # Use per-user Android ID
            service="oauth2:https://www.googleapis.com/auth/" + scope,
            app=request_app,
            client_sig="38918a453d07199354f8b19af05ec6562ced5788",
        )
        if not auth_response:
            raise ValueError("No response from gpsoauth.perform_oauth")

        if "Auth" not in auth_response:
            raise KeyError(f"'Auth' not found in response: {auth_response}")

        token = auth_response["Auth"]
        return token
    except Exception as e:
        raise RuntimeError(f"Failed to get auth token for scope '{scope}': {e}") from e


async def async_request_token(username: str, scope: str, play_services: bool = False) -> str:
    """Async token request via gpsoauth (HA-safe).

    Uses async token retrieval and runs gpsoauth in a thread pool to avoid blocking the event loop.
    """
    aas_token = await async_get_aas_token()  # async path

    # Get unique Android ID for this user
    android_id = await _get_or_generate_android_id_async(username)
    request_app = "com.google.android.gms" if play_services else "com.google.android.apps.adm"

    def _run() -> str:
        auth_response = gpsoauth.perform_oauth(
            username,
            aas_token,
            android_id,  # Use per-user Android ID
            service="oauth2:https://www.googleapis.com/auth/" + scope,
            app=request_app,
            client_sig="38918a453d07199354f8b19af05ec6562ced5788",
        )
        if not auth_response:
            raise ValueError("No response from gpsoauth.perform_oauth")

        if "Auth" not in auth_response:
            raise KeyError(f"'Auth' not found in response: {auth_response}")

        return auth_response["Auth"]

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        raise RuntimeError(f"Failed to get auth token for scope '{scope}': {e}") from e
