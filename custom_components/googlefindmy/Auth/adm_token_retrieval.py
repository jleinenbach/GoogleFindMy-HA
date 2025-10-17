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
- The `async_get_adm_token_isolated` function, which is required by the config flow,
  has been retained to prevent import errors during setup.
- Blocking `gpsoauth` calls are executed in a thread executor to avoid blocking
  Home Assistant's event loop.

Security notes (logging):
- We never log tokens or raw auth responses. Error details are summarized (type/keys),
  and account emails are masked for privacy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import gpsoauth

# Prefer relative imports inside the package for robustness
from .token_retrieval import async_request_token
from .token_cache import (
    async_get_cached_value,
    async_get_cached_value_or_set,
    async_set_cached_value,
)
from .username_provider import async_get_username

_LOGGER = logging.getLogger(__name__)

# Constants for gpsoauth (kept for compatibility/reference)
_ANDROID_ID: int = 0x38918A453D071993
_CLIENT_SIG: str = "38918a453d07199354f8b19af05ec6562ced5788"
_APP_ID: str = "com.google.android.apps.adm"


# ---------------------------------------------------------------------------
# Helpers (privacy-friendly logging, normalization, brief error messages)
# ---------------------------------------------------------------------------

def _mask_email(email: str | None) -> str:
    """Return a privacy-friendly representation of an email for logs."""
    if not email or "@" not in email:
        return "<unknown>"
    local, domain = email.split("@", 1)
    if not local:
        return f"*@{domain}"
    masked_local = (local[0] + "***") if len(local) > 1 else "*"
    return f"{masked_local}@{domain}"

def _clip(s: str, limit: int = 200) -> str:
    """Clip long strings to a safe length for logs."""
    return s if len(s) <= limit else (s[: limit - 1] + "…")

def _summarize_response(obj: Any) -> str:
    """Summarize a gpsoauth response without leaking sensitive data."""
    if isinstance(obj, dict):
        # Only reveal keys; never values
        keys = ", ".join(sorted(obj.keys()))
        return f"dict(keys=[{keys}])"
    return f"{type(obj).__name__}"


# ---------------------------------------------------------------------------
# Core token generation (delegates to central token retriever)
# ---------------------------------------------------------------------------

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
    _LOGGER.debug("Generating new ADM token for account %s", _mask_email(username))
    # The underlying function will handle AAS acquisition/exchange as needed.
    return await async_request_token(username, "android_device_manager")


# ---------------------------------------------------------------------------
# Public APIs
# ---------------------------------------------------------------------------

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
    user = (username or await async_get_username() or "").strip().lower()
    if not user:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token.")

    cache_key = f"adm_token_{user}"

    async def _generator() -> str:
        return await _generate_adm_token(user)

    last_exc: Optional[Exception] = None
    attempts = max(1, retries + 1)

    for attempt in range(attempts):
        try:
            # Only generates if not cached
            token = await async_get_cached_value_or_set(cache_key, _generator)

            # Persist TTL metadata (best-effort)
            if not await async_get_cached_value(f"adm_token_issued_at_{user}"):
                await async_set_cached_value(f"adm_token_issued_at_{user}", time.time())
            if not await async_get_cached_value(f"adm_probe_startup_left_{user}"):
                await async_set_cached_value(f"adm_probe_startup_left_{user}", 3)

            return token

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts - 1:
                sleep_s = backoff * (2 ** attempt)
                _LOGGER.info(
                    "ADM token generation failed (attempt %d/%d) for %s: %s — retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    _mask_email(user),
                    _clip(str(exc)),
                    sleep_s,
                )
                # Clear potentially bad cache entry before retrying (best-effort)
                try:
                    await async_set_cached_value(cache_key, None)
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(sleep_s)
                continue

            _LOGGER.error(
                "ADM token generation failed after %d attempts for %s: %s",
                attempts,
                _mask_email(user),
                _clip(str(exc)),
            )

    assert last_exc is not None
    raise last_exc


# --- Functions required by config_flow.py (isolated, no global cache touch) ---

async def _perform_oauth_with_provided_aas(username: str, aas_token: str) -> str:
    """
    Perform the OAuth exchange with a provided AAS token (used for isolated validation).

    Args:
        username: The Google account e-mail.
        aas_token: The AAS token to exchange.

    Returns:
        The resulting ADM token.

    Raises:
        RuntimeError: If the OAuth response is invalid or missing the expected fields.
    """
    def _run() -> str:
        resp = gpsoauth.perform_oauth(
            username,
            aas_token,
            _ANDROID_ID,
            service="oauth2:https://www.googleapis.com/auth/android_device_manager",
            app=_APP_ID,
            client_sig=_CLIENT_SIG,
        )
        if not isinstance(resp, dict):
            # Never include the raw `resp` in logs/errors
            raise RuntimeError(f"gpsoauth.perform_oauth returned non-dict response ({type(resp).__name__})")
        if "Auth" not in resp:
            # Typical error shape: {"Error": "BadAuthentication"} (do not print full dict)
            err = resp.get("Error", "unknown")
            raise RuntimeError(f"Missing 'Auth' in gpsoauth response (error={err})")
        return resp["Auth"]

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as exc:  # noqa: BLE001
        # Summarize without leaking sensitive data
        _LOGGER.debug(
            "perform_oauth failed for %s: %s",
            _mask_email(username),
            _clip(str(exc)),
        )
        raise


async def async_get_adm_token_isolated(
    username: str,
    *,
    aas_token: Optional[str] = None,
    secrets_bundle: Optional[dict[str, Any]] = None,
    cache_get: Optional[Callable[[str], Awaitable[Any]]] = None,
    cache_set: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    retries: int = 1,
    backoff: float = 1.0,
) -> str:
    """
    Perform a *real* AAS→ADM exchange **without touching the global cache**.
    This function is required by the config_flow for credential validation.

    Args:
        username: The Google account e-mail.
        aas_token: An explicit AAS token to use for the exchange.
        secrets_bundle: A dictionary (e.g., from secrets.json) to find an `aas_token` in.
        cache_get: Optional async getter for a flow-local cache.
        cache_set: Optional async setter for a flow-local cache.
        retries: Number of retries on failure.
        backoff: Initial backoff delay for retries.

    Returns:
        The generated ADM token.

    Raises:
        RuntimeError: If no AAS token is provided or the exchange fails.
    """
    user = (username or "").strip().lower()
    if not user:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token (isolated).")

    src_aas = (aas_token or "").strip()
    if not src_aas and isinstance(secrets_bundle, dict):
        candidate = secrets_bundle.get("aas_token")
        if isinstance(candidate, str) and candidate.strip():
            src_aas = candidate.strip()

    if not src_aas:
        raise RuntimeError("Isolated ADM exchange requires an AAS token.")

    last_exc: Optional[Exception] = None
    attempts = max(1, retries + 1)

    for attempt in range(attempts):
        try:
            tok = await _perform_oauth_with_provided_aas(user, src_aas)
            if cache_set is not None:
                # Best-effort metadata write (flow-local)
                try:
                    await cache_set(f"adm_token_{user}", tok)
                    await cache_set(f"adm_token_issued_at_{user}", time.time())
                except Exception as meta_exc:  # noqa: BLE001
                    _LOGGER.debug("Isolated TTL metadata write skipped: %s", _clip(str(meta_exc)))
            return tok

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts - 1:
                sleep_s = backoff * (2 ** attempt)
                _LOGGER.info(
                    "Isolated ADM exchange failed (attempt %d/%d) for %s: %s — retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    _mask_email(user),
                    _clip(str(exc)),
                    sleep_s,
                )
                await asyncio.sleep(sleep_s)
                continue

            _LOGGER.error(
                "Isolated ADM exchange failed after %d attempts for %s: %s",
                attempts,
                _mask_email(user),
                _clip(str(exc)),
            )

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
