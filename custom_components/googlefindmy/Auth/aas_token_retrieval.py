# custom_components/googlefindmy/Auth/aas_token_retrieval.py
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
- The Android ID is a constant used by `gpsoauth` during the exchange.
- The username is obtained from the cache via `username_provider`; if an ADM fallback
  is used, we also update the username accordingly.

Enhancement (safe short-circuit):
- Some deployments store an already-exchanged AAS token in the OAuth slot. Empirically,
  such tokens start with "aas_et/" or "aas_et." (user-provided evidence). We detect
  this shape and short-circuit to return it directly, avoiding a redundant exchange.
"""

from __future__ import annotations

import asyncio
import logging
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

# Constant Android ID used for token exchange via gpsoauth (16-hex-digit integer).
_ANDROID_ID: int = 0x38918A453D071993


def _looks_like_aas(token: str) -> bool:
    """Heuristically identify an AAS token by its well-known prefix.

    Why a heuristic?
    ----------------
    There is no public, official spec for the AAS token wire format. However, in practice
    (and as provided by the user sample), AAS tokens commonly start with "aas_et/" or
    "aas_et." on Android. We conservatively check these prefixes to avoid false positives.

    Returns:
        True if the token looks like an AAS token; otherwise False.
    """
    return token.startswith("aas_et/") or token.startswith("aas_et.")


async def _exchange_oauth_for_aas(username: str, oauth_token: str) -> Dict[str, Any]:
    """Run the blocking gpsoauth exchange in an executor.

    Args:
        username: Google account e-mail.
        oauth_token: OAuth token to exchange.

    Returns:
        The raw dictionary response from gpsoauth containing at least a 'Token' key.

    Raises:
        RuntimeError: If the exchange fails or returns an invalid response.
    """

    def _run() -> Dict[str, Any]:
        # gpsoauth.exchange_token(username, oauth_token, android_id) is blocking.
        return gpsoauth.exchange_token(username, oauth_token, _ANDROID_ID)

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
           1a) SHORT-CIRCUIT: If that value already looks like an AAS token, return it as-is.
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
    username: Optional[str] = await async_get_username()

    # Prefer explicit OAuth token from cache.
    oauth_token: Optional[str] = await async_get_cached_value(CONF_OAUTH_TOKEN)

    # --- FIX/Enhancement: Accept pre-existing AAS tokens stored under the OAuth key. ---
    # Rationale:
    # * Some setups accidentally persist the AAS token into the OAuth slot.
    # * Attempting to "exchange" an AAS token again would fail or be redundant.
    # * If the value clearly matches an AAS token shape, we can directly reuse it.
    if oauth_token and _looks_like_aas(oauth_token):
        _LOGGER.debug(
            "Using pre-existing AAS token found in oauth_token cache (short-circuit)."
        )
        # NOTE: We intentionally skip username checks here because an AAS token is
        # already the final credential needed for downstream calls.
        return oauth_token

    # Fallback: scan ADM tokens if no explicit OAuth token exists.
    if not oauth_token:
        all_cached = await async_get_all_cached_values()
        for key, value in all_cached.items():
            if (
                isinstance(key, str)
                and key.startswith("adm_token_")
                and isinstance(value, str)
                and value
            ):
                # Reuse ADM token value as OAuth token.
                oauth_token = value
                extracted_username = key.replace("adm_token_", "", 1)
                if extracted_username and "@" in extracted_username:
                    username = extracted_username
                _LOGGER.info(
                    "Using existing ADM token from cache for OAuth exchange (user: %s).",
                    username or "unknown",
                )
                break

    if not oauth_token:
        raise ValueError(
            "No OAuth token available; please configure the integration with a valid token."
        )
    if not username:
        # We need a username only for the gpsoauth exchange path.
        raise ValueError(
            "No username available; please ensure the account e-mail is configured."
        )

    # Exchange OAuth → AAS (blocking call executed in executor).
    resp = await _exchange_oauth_for_aas(username, oauth_token)

    # Persist normalized email if gpsoauth returns it (keeps cache consistent).
    if isinstance(resp.get("Email"), str) and resp["Email"]:
        try:
            await async_set_cached_value(username_string, resp["Email"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to persist normalized username from gpsoauth: %s", err
            )

    return str(resp["Token"])


async def async_get_aas_token() -> str:
    """Return the cached AAS token or compute and cache it.

    Returns:
        The AAS token string.
    """
    # `async_get_cached_value_or_set` ensures single-flight computation:
    # the first caller computes and stores the value; subsequent callers reuse it.
    return await async_get_cached_value_or_set("aas_token", _generate_aas_token)


# ----------------------- Legacy sync wrapper (unsupported) -----------------------

def get_aas_token() -> str:  # pragma: no cover - legacy path kept for compatibility messaging
    """Legacy sync API is intentionally unsupported to prevent event loop deadlocks.

    Raises:
        NotImplementedError: Always. Use `await async_get_aas_token()` instead.
    """
    raise NotImplementedError(
        "Use `await async_get_aas_token()` instead of the synchronous get_aas_token()."
    )

