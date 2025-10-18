# custom_components/googlefindmy/Auth/adm_token_retrieval.py
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
- Single-flight protection: per-username lock prevents parallel token exchanges.
- Auth failure handling: `BadAuthentication` is **not** retried; we raise
  `ConfigEntryAuthFailed` to trigger Home Assistant's reauth flow.

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
from typing import Optional, Awaitable, Callable, Any, Dict

import gpsoauth
from homeassistant.exceptions import ConfigEntryAuthFailed

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
_ANDROID_ID: int = 0x38918A453D071993
_CLIENT_SIG: str = "38918a453d07199354f8b19af05ec6562ced5788"
_APP_ID: str = "com.google.android.apps.adm"

# Per-username single-flight locks to avoid parallel ADM exchanges
_singleflight_locks: dict[str, asyncio.Lock] = {}


def _lock_for(user: str) -> asyncio.Lock:
    """Return (and create if needed) a single-flight lock for the given user."""
    lock = _singleflight_locks.get(user)
    if lock is None:
        lock = _singleflight_locks[user] = asyncio.Lock()
    return lock


async def _seed_username_in_cache(username: str) -> None:
    """Ensure the canonical username cache key is populated (idempotent)."""
    try:
        cached = await async_get_cached_value(username_string)
        if cached != username and isinstance(username, str) and username:
            await async_set_cached_value(username_string, username)
            _LOGGER.debug("Seeded username cache key '%s' with '%s'.", username_string, username)
    except Exception as exc:  # defensive: never fail token flow on seeding
        _LOGGER.debug("Username cache seeding skipped: %s", exc)


def _extract_auth_token_from_gpsoauth_response(resp: Dict[str, Any]) -> str:
    """Extract an ADM scope token from gpsoauth response.

    The gpsoauth library/endpoint may return either 'Auth' (common for perform_oauth)
    or 'Token' (older shapes) for successful responses. If an auth error is indicated
    (e.g. {'Error': 'BadAuthentication'}), we escalate with ConfigEntryAuthFailed to
    ensure the integration triggers a proper reauth flow.
    """
    if not isinstance(resp, dict):
        raise RuntimeError(f"gpsoauth returned invalid response type: {type(resp).__name__}")

    # Explicit auth error from server → never retry, trigger reauth
    err = (resp.get("Error") or resp.get("error") or "").strip()
    if err.lower() == "badauthentication":
        raise ConfigEntryAuthFailed("BadAuthentication returned by gpsoauth")

    # Success keys (accept both)
    if "Auth" in resp and isinstance(resp["Auth"], str) and resp["Auth"]:
        return resp["Auth"]
    if "Token" in resp and isinstance(resp["Token"], str) and resp["Token"]:
        return resp["Token"]

    # Unknown/unsupported shape
    raise RuntimeError(f"gpsoauth.perform_oauth returned invalid response (no Auth/Token): {resp}")


async def _perform_oauth_with_aas(username: str) -> str:
    """Exchange AAS -> scope token (ADM) using gpsoauth in a thread executor."""
    # Get AAS token asynchronously (it handles its own blocking via executor)
    aas_token = await async_get_aas_token()

    def _run() -> str:
        """Synchronous part to be run in an executor."""
        resp = gpsoauth.perform_oauth(
            username,
            aas_token,
            _ANDROID_ID,
            service="oauth2:https://www.googleapis.com/auth/android_device_manager",
            app=_APP_ID,
            client_sig=_CLIENT_SIG,
        )
        return _extract_auth_token_from_gpsoauth_response(resp)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


# -------------------- Isolated / flow-local exchange helpers --------------------

async def _perform_oauth_with_provided_aas(username: str, aas_token: str) -> str:
    """Like _perform_oauth_with_aas, but uses a caller-provided AAS token (no globals)."""
    def _run() -> str:
        resp = gpsoauth.perform_oauth(
            username,
            aas_token,
            _ANDROID_ID,
            service="oauth2:https://www.googleapis.com/auth/android_device_manager",
            app=_APP_ID,
            client_sig=_CLIENT_SIG,
        )
        return _extract_auth_token_from_gpsoauth_response(resp)

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


async def async_get_adm_token_isolated(
    username: str,
    *,
    # One of the following must be provided to allow an isolated exchange:
    aas_token: Optional[str] = None,
    secrets_bundle: Optional[dict[str, Any]] = None,
    # Optional, flow-local TTL metadata storage (to keep validation fully isolated)
    cache_get: Optional[Callable[[str], Awaitable[Any]]] = None,
    cache_set: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    retries: int = 1,
    backoff: float = 1.0,
) -> str:
    """
    Perform a *real* AAS→ADM exchange **without touching the global cache**.

    Usage:
      - Provide `aas_token` directly, or include it in `secrets_bundle['aas_token']`.
      - Optionally pass `cache_get` / `cache_set` to record TTL metadata in a
        flow-local ephemeral cache (used by Nova TTL policy during validation).

    Returns:
      ADM token string.

    Raises:
      ConfigEntryAuthFailed if gpsoauth indicates BadAuthentication.
      RuntimeError if input is insufficient or exchange fails after retries.
    """
    if not isinstance(username, str) or not username:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token (isolated).")

    # Prefer explicit parameter; otherwise look into the provided secrets bundle.
    src_aas = aas_token
    if src_aas is None and isinstance(secrets_bundle, dict):
        candidate = secrets_bundle.get("aas_token")
        if isinstance(candidate, str) and candidate.strip():
            src_aas = candidate.strip()

    if not src_aas:
        raise RuntimeError(
            "Isolated ADM exchange requires an AAS token (pass `aas_token` or include `aas_token` in `secrets_bundle`)."
        )

    last_exc: Optional[Exception] = None
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            tok = await _perform_oauth_with_provided_aas(username, src_aas)

            # Best-effort: persist TTL metadata *only* via provided flow-local cache.
            if cache_set is not None:
                try:
                    await cache_set(f"adm_token_{username}", tok)
                    await cache_set(f"adm_token_issued_at_{username}", time.time())
                    needs_bootstrap = True
                    if cache_get is not None:
                        try:
                            existing = await cache_get(f"adm_probe_startup_left_{username}")
                            needs_bootstrap = not bool(existing)
                        except Exception:  # defensive
                            needs_bootstrap = True
                    if needs_bootstrap:
                        await cache_set(f"adm_probe_startup_left_{username}", 3)
                except Exception as meta_exc:  # never fail the exchange on metadata issues
                    _LOGGER.debug("Isolated TTL metadata write skipped: %s", meta_exc)

            return tok

        except ConfigEntryAuthFailed:
            # Never retry on explicit authentication failures
            _LOGGER.warning("Isolated ADM exchange failed due to BadAuthentication (username redacted).")
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < attempts - 1:
                sleep_s = backoff * (2**attempt)
                _LOGGER.info(
                    "Isolated ADM exchange failed (attempt %s/%s): %s — retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    exc.__class__.__name__,
                    sleep_s,
                )
                await asyncio.sleep(sleep_s)
                continue
            _LOGGER.error("Isolated ADM exchange failed after %s attempts: %s", attempts, exc)

    assert last_exc is not None
    raise last_exc


# --------------------- Standard async API (global cache) ---------------------

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
        ConfigEntryAuthFailed: If gpsoauth indicates an authentication failure.
        RuntimeError: If the username is invalid or token generation fails after all retries.
    """
    user = username or await async_get_username()
    if not isinstance(user, str) or not user:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token.")

    cache_key = f"adm_token_{user}"

    # Cache fast-path
    token = await async_get_cached_value(cache_key)
    if isinstance(token, str) and token:
        return token

    # Single-flight: prevent parallel exchanges for the same user
    lock = _lock_for(user)
    async with lock:
        # Double-check cache after acquiring the lock
        token = await async_get_cached_value(cache_key)
        if isinstance(token, str) and token:
            return token

        # Generate with bounded retries (auth failures are *not* retried)
        last_exc: Optional[Exception] = None
        attempts = max(1, retries + 1)
        for attempt in range(attempts):
            try:
                await _seed_username_in_cache(user)
                tok = await _perform_oauth_with_aas(user)

                # Persist token & issued-at metadata
                await async_set_cached_value(cache_key, tok)
                await async_set_cached_value(f"adm_token_issued_at_{user}", time.time())
                if not await async_get_cached_value(f"adm_probe_startup_left_{user}"):
                    await async_set_cached_value(f"adm_probe_startup_left_{user}", 3)
                return tok

            except ConfigEntryAuthFailed:
                # Immediate escalation: no more retries; HA will trigger reauth
                _LOGGER.error("ADM token generation aborted due to BadAuthentication (username redacted).")
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < attempts - 1:
                    sleep_s = backoff * (2**attempt)
                    _LOGGER.info(
                        "ADM token generation failed (attempt %s/%s): %s — retrying in %.1fs",
                        attempt + 1,
                        attempts,
                        exc.__class__.__name__,
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
