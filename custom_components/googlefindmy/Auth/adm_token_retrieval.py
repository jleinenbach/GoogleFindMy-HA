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
from .token_retrieval import (
    InvalidAasTokenError,
    async_request_token,
    _extract_android_id_from_credentials,
)
from .token_cache import TokenCache
from .username_provider import async_get_username, username_string
from .aas_token_retrieval import async_get_aas_token  # entry-scoped AAS provider
from ..const import DATA_AAS_TOKEN, DATA_AUTH_METHOD

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
    if isinstance(err, InvalidAasTokenError):
        return True
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


async def _seed_username_in_cache(username: str, *, cache: TokenCache) -> None:
    """Ensure the canonical username cache key is populated (idempotent)."""
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    try:
        cached = await cache.get(username_string)
        if cached != username and isinstance(username, str) and username:
            await cache.set(username_string, username)
            _LOGGER.debug(
                "Seeded username cache key '%s' with '%s' (entry-scoped).",
                username_string,
                username,
            )
    except Exception as exc:  # Defensive: never fail token flow on seeding.
        _LOGGER.debug("Username cache seeding skipped: %s", _clip(exc))


# ---------------------------------------------------------------------------
# Core token generation (delegates to central token retriever)
# ---------------------------------------------------------------------------

async def _generate_adm_token(username: str, *, cache: TokenCache) -> str:
    """
    Generate a new ADM token, honoring the original authentication method.

    When the integration was configured with individual OAuth tokens, an
    entry-scoped AAS provider is injected so that the cached OAuth credential
    can be exchanged for a fresh AAS token. Otherwise (secrets.json / AAS
    master token), the cached AAS token is reused directly to perform the
    AAS → ADM exchange.
    """
    _LOGGER.debug(
        "Generating new ADM token for account %s (entry scoped)",
        _mask_email(username),
    )
    service = _normalize_service("android_device_manager")

    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    auth_method = await cache.get(DATA_AUTH_METHOD)
    use_oauth_provider = auth_method == "individual_tokens"

    aas_token_direct: Optional[str] = None
    aas_provider: Optional[Callable[[], Awaitable[str]]] = None

    if use_oauth_provider:
        _LOGGER.debug("ADM token refresh path: using OAuth→AAS provider (individual tokens).")
        aas_provider = lambda: async_get_aas_token(cache=cache)  # noqa: E731
    else:
        _LOGGER.debug("ADM token refresh path: reusing cached AAS token (secrets.json / master).")
        aas_token_direct = await cache.get(DATA_AAS_TOKEN)
        if not isinstance(aas_token_direct, str) or not aas_token_direct:
            _LOGGER.warning(
                "Cached AAS token missing for %s during ADM refresh (method=%s); falling back to OAuth provider.",
                _mask_email(username),
                auth_method or "<unknown>",
            )
            aas_token_direct = None
            aas_provider = lambda: async_get_aas_token(cache=cache)  # noqa: E731

    return await async_request_token(
        username,
        service,
        cache=cache,
        aas_token=aas_token_direct,
        aas_provider=aas_provider,
    )


# ---------------------------------------------------------------------------
# Public APIs
# ---------------------------------------------------------------------------


async def _resolve_android_id_for_isolated_flow(
    *,
    secrets_bundle: Optional[dict[str, Any]],
    cache_get: Optional[Callable[[str], Awaitable[Any]]],
) -> int:
    """Resolve android_id for isolated exchanges using secrets or flow cache."""

    android_id: int | None = None

    if isinstance(secrets_bundle, dict):
        android_id = _extract_android_id_from_credentials(secrets_bundle.get("fcm_credentials"))

    if android_id is None and cache_get is not None:
        try:
            cached_fcm = await cache_get("fcm_credentials")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Isolated exchange: failed to read cached FCM credentials: %s", _clip(err))
        else:
            android_id = _extract_android_id_from_credentials(cached_fcm)

    if android_id is None:
        _LOGGER.warning(
            "FCM credentials missing android_id; falling back to static identifier. "
            "Generate fresh secrets.json if authentication fails."
        )
        android_id = _ANDROID_ID

    return android_id

async def async_get_adm_token(
    username: Optional[str] = None,
    *,
    retries: int = 2,
    backoff: float = 1.0,
    cache: TokenCache,
) -> str:
    """
    Return a cached ADM token or generate a new one (async-first API).

    This is the main entry point for other modules to get a valid ADM token.

    Args:
        username: Optional explicit username. If None, it's resolved from cache.
        retries: Number of retry attempts on failure (only for transient issues).
        backoff: Initial backoff delay in seconds for retries.
        cache: Entry-scoped TokenCache used for all reads/writes. Legacy global
            facades are no longer available.

    Returns:
        The ADM token string.

    Raises:
        RuntimeError: If the username is invalid or token generation fails after all retries.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    # Use the passed username if available; only fallback to provider when missing.
    user = (username or await async_get_username(cache=cache) or "").strip().lower()
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
            token = await cache.get_or_set(cache_key, _generator)

            # Persist TTL metadata (best-effort; entry-scoped if possible)
            issued_key = f"adm_token_issued_at_{user}"
            probe_key = f"adm_probe_startup_left_{user}"

            if not await cache.get(issued_key):
                await cache.set(issued_key, time.time())
            if not await cache.get(probe_key):
                await cache.set(probe_key, 3)

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
                await cache.set(cache_key, None)
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

async def _perform_oauth_with_provided_aas(
    username: str,
    aas_token: str,
    *,
    android_id: int = _ANDROID_ID,
) -> str:
    """
    Perform the OAuth exchange with a provided AAS token (used for isolated validation).

    Args:
        username: The Google account e-mail.
        aas_token: The AAS token to exchange.
        android_id: The device-specific Android ID used for the OAuth exchange.

    Returns:
        The resulting ADM token.

    Raises:
        RuntimeError: If the OAuth response is invalid or missing the expected fields.
    """
    def _run() -> str:
        resp = gpsoauth.perform_oauth(
            username,
            aas_token,
            android_id,
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

    android_id = await _resolve_android_id_for_isolated_flow(
        secrets_bundle=secrets_bundle,
        cache_get=cache_get,
    )

    for attempt in range(attempts):
        try:
            tok = await _perform_oauth_with_provided_aas(user, src_aas, android_id=android_id)

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

