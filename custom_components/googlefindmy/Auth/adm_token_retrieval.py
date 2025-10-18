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
- Delegates token issuance to a central retriever: `token_retrieval.async_request_token`.
  A small alias→scope mapping guarantees that the service string is accepted even if
  the retriever expects the full OAuth2 scope.
- **Retry policy**: Transient network/library errors are retried with bounded backoff.
  Clear, non-recoverable auth errors (e.g., "BadAuthentication") are NOT retried.
  Additionally, HTTP-style signals such as 401/403 or "unauthorized"/"forbidden" in
  error messages are treated as non-retryable as well.
- Blocking `gpsoauth` calls (isolated flow) are executed in a thread executor to
  avoid blocking Home Assistant's event loop.

Security notes (logging):
- We never log tokens or raw auth responses. Error details are summarized (type/keys),
  and account emails are masked for privacy.

Entry-scoped behavior:
- When an entry-scoped `TokenCache` is provided to `async_get_adm_token(..., cache=...)`,
  we inject an **entry-scoped `aas_provider`** that resolves AAS via
  `async_get_aas_token(cache=cache)`. This prevents accidental fallbacks to any
  global AAS source and closes the end-to-end entry scoping for the ADM flow.

-------------------------------------------------------------------------------
Changelog (English)
-------------------------------------------------------------------------------
- Inject an entry-scoped `aas_provider` into ADM issuance when a `TokenCache` is
  supplied, preventing accidental fallback to global AAS tokens.
- Kept the public API unchanged; minimal internal refactor of `_generate_adm_token(...)`.
- Updated docstrings/comments and added a DEBUG log for observability.
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
    TokenCache,
    async_get_cached_value,
    async_get_cached_value_or_set,
    async_set_cached_value,
)
from .username_provider import async_get_username, username_string
from .aas_token_retrieval import async_get_aas_token  # entry-scoped AAS provider

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


def _clip(s: Any, limit: int = 200) -> str:
    """Clip long strings to a safe length for logs."""
    s = str(s)
    return s if len(s) <= limit else (s[: limit - 1] + "…")


def _summarize_response(obj: Any) -> str:
    """Summarize a gpsoauth response without leaking sensitive data."""
    if isinstance(obj, dict):
        keys = ", ".join(sorted(obj.keys()))
        return f"dict(keys=[{keys}])"
    return f"{type(obj).__name__}"


def _normalize_service(service: str) -> str:
    """Map known aliases to the expected OAuth2 scope (defensive)."""
    s = (service or "").strip().lower()
    if s in {"android_device_manager", "adm"}:
        return "oauth2:https://www.googleapis.com/auth/android_device_manager"
    # Fallback: allow callers to pass a full scope already
    return service


def _is_non_retryable_auth(err: Exception) -> bool:
    """Return True if the error indicates a non-recoverable auth problem."""
    text = _clip(err)
    # Typical shapes to consider non-retryable
    if "BadAuthentication" in text:
        return True
    low = text.lower()
    if "invalid_grant" in low:
        return True
    if "missing 'auth' in gpsoauth response" in text:
        # Most often wraps {"Error": "..."} from gpsoauth; treat as non-retryable
        return True
    # Treat obvious HTTP-style auth denials as non-retryable as well
    if "401" in low or "403" in low or "unauthorized" in low or "forbidden" in low:
        return True
    return False


async def _seed_username_in_cache(username: str, *, cache: TokenCache | None) -> None:
    """
    Ensure the canonical username cache key is populated (idempotent).

    When an entry-scoped `cache` is provided, only that cache is used. Otherwise,
    the legacy facades are used to preserve single-entry behavior.
    """
    try:
        if cache is not None:
            cached = await cache.get(username_string)
            if cached != username and isinstance(username, str) and username:
                await cache.set(username_string, username)
                _LOGGER.debug("Seeded username cache key '%s' with '%s' (entry-scoped).", username_string, username)
        else:
            cached = await async_get_cached_value(username_string)
            if cached != username and isinstance(username, str) and username:
                await async_set_cached_value(username_string, username)
                _LOGGER.debug("Seeded username cache key '%s' with '%s' (default cache).", username_string, username)
    except Exception as exc:  # Defensive: never fail token flow on seeding.
        _LOGGER.debug("Username cache seeding skipped: %s", _clip(exc))


# ---------------------------------------------------------------------------
# Core token generation (delegates to central token retriever)
# ---------------------------------------------------------------------------

async def _generate_adm_token(username: str, *, cache: TokenCache | None) -> str:
    """
    Generate a new ADM token by delegating to the central token retriever,
    injecting an **entry-scoped AAS provider** when `cache` is supplied.

    This keeps the logic simple and closes the end-to-end entry scoping:
    ADM <-OAuth(AAS from same cache)-> AAS.
    """
    _LOGGER.debug(
        "Generating new ADM token for account %s%s",
        _mask_email(username),
        " (entry-scoped AAS provider)" if cache is not None else "",
    )
    service = _normalize_service("android_device_manager")

    # Prefer an entry-scoped AAS provider when a cache is supplied; otherwise
    # fall back to the default provider inside async_request_token.
    aas_provider: Optional[Callable[[], Awaitable[str]]] = None
    if cache is not None:
        aas_provider = lambda: async_get_aas_token(cache=cache)

    return await async_request_token(username, service, aas_provider=aas_provider)


# ---------------------------------------------------------------------------
# Public APIs
# ---------------------------------------------------------------------------

async def async_get_adm_token(
    username: Optional[str] = None,
    *,
    retries: int = 2,
    backoff: float = 1.0,
    cache: TokenCache | None = None,
) -> str:
    """
    Return a cached ADM token or generate a new one (async-first API).

    This is the main entry point for other modules to get a valid ADM token.

    Args:
        username: Optional explicit username. If None, it's resolved from cache.
        retries: Number of retry attempts on failure (only for transient issues).
        backoff: Initial backoff delay in seconds for retries.
        cache: Optional entry-scoped TokenCache. If provided, **only this cache**
            is used for reads/writes. If None, legacy default-cache facades are used.

    Returns:
        The ADM token string.

    Raises:
        RuntimeError: If the username is invalid or token generation fails after all retries.
    """
    # Use the passed username if available; only fallback to provider when missing.
    user = (username or await async_get_username() or "").strip().lower()
    if not user:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token.")

    # Ensure username is present in the selected cache (idempotent).
    await _seed_username_in_cache(user, cache=cache)

    cache_key = f"adm_token_{user}"

    async def _generator() -> str:
        return await _generate_adm_token(user, cache=cache)

    last_exc: Optional[Exception] = None
    attempts = max(1, retries + 1)

    for attempt in range(attempts):
        try:
            # Only generates if not cached; avoids multiple token exchanges under load
            if cache is not None:
                token = await cache.get_or_set(cache_key, _generator)
            else:
                token = await async_get_cached_value_or_set(cache_key, _generator)

            # Persist TTL metadata (best-effort; entry-scoped if possible)
            issued_key = f"adm_token_issued_at_{user}"
            probe_key = f"adm_probe_startup_left_{user}"

            if cache is not None:
                if not await cache.get(issued_key):
                    await cache.set(issued_key, time.time())
                if not await cache.get(probe_key):
                    await cache.set(probe_key, 3)
            else:
                if not await async_get_cached_value(issued_key):
                    await async_set_cached_value(issued_key, time.time())
                if not await async_get_cached_value(probe_key):
                    await async_set_cached_value(probe_key, 3)

            return token

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            # Non-retryable? Log once and stop immediately.
            if _is_non_retryable_auth(exc) or attempt >= attempts - 1:
                _LOGGER.error(
                    "ADM token generation failed%s for %s: %s",
                    "" if attempt >= attempts - 1 else " (non-retryable)",
                    _mask_email(user),
                    _clip(exc),
                )
                break

            # Retryable path: clear any stale cache value and back off
            try:
                if cache is not None:
                    await cache.set(cache_key, None)
                else:
                    await async_set_cached_value(cache_key, None)
            except Exception:
                pass  # best-effort

            sleep_s = backoff * (2 ** attempt)
            _LOGGER.info(
                "ADM token generation failed (attempt %d/%d) for %s: %s — retrying in %.1fs",
                attempt + 1,
                attempts,
                _mask_email(user),
                _clip(exc),
                sleep_s,
            )
            await asyncio.sleep(sleep_s)

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
    This function is required by the config flow for credential validation.

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

            # Best-effort: persist TTL metadata via provided flow-local cache.
            if cache_set is not None:
                try:
                    await cache_set(f"adm_token_{user}", tok)

                    issued_key = f"adm_token_issued_at_{user}"
                    if cache_get is not None:
                        has_issued = await cache_get(issued_key)
                    else:
                        has_issued = None
                    if not has_issued:
                        await cache_set(issued_key, time.time())

                    # Restore bootstrap probe counter (regression fix #3)
                    probe_key = f"adm_probe_startup_left_{user}"
                    if cache_get is not None:
                        existing = await cache_get(probe_key)
                    else:
                        existing = None
                    if not existing:
                        await cache_set(probe_key, 3)
                except Exception as meta_exc:  # never fail the exchange on metadata issues
                    _LOGGER.debug("Isolated TTL metadata write skipped: %s", _clip(meta_exc))

            return tok

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if _is_non_retryable_auth(exc) or attempt >= attempts - 1:
                _LOGGER.error(
                    "Isolated ADM exchange failed%s for %s: %s",
                    "" if attempt >= attempts - 1 else " (non-retryable)",
                    _mask_email(user),
                    _clip(exc),
                )
                break
            sleep_s = backoff * (2 ** attempt)
            _LOGGER.info(
                "Isolated ADM exchange failed (attempt %d/%d) for %s: %s — retrying in %.1fs",
                attempt + 1,
                attempts,
                _mask_email(user),
                _clip(exc),
                sleep_s,
            )
            await asyncio.sleep(sleep_s)

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
