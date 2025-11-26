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
- Primary API: `async_get_aas_token(cache=..., retries=..., backoff=...)`.
  Callers **must** pass the entry-scoped TokenCache instance to guarantee
  multi-account isolation.
- Cached retrieval via the cache's `get_or_set` ensures we compute only once.
- Fallback: If no explicit OAuth token is present, reuse any `adm_token_*` value
  from the same cache (entry-scoped when provided).
- Sync wrapper `get_aas_token()` is intentionally unsupported to prevent deadlocks.

Notes:
- The Android ID is resolved per-user from cached FCM credentials or generated
  on demand to avoid reuse across accounts.
- The username is read from the cache via `username_provider`; if an ADM fallback
  is used, we also update the username accordingly (entry-scoped when `cache` is given).

Enhancements (defensive validation & retries):
- Some deployments accidentally persist non-OAuth values in the OAuth slot (e.g., an
  AAS token with prefix "aas_et…" or a JWT-like blob starting with "eyJ…").
  We **do not reuse** such values. Instead, we disqualify them for the OAuth→AAS
  exchange and fall back to the next available source. This avoids brittle shortcuts.
- Retry policy: transient transport/library errors retry with bounded exponential
  backoff; clear auth failures (e.g., "BadAuthentication", "invalid_grant", 401/403
  semantics like "unauthorized"/"forbidden") are **not** retried.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from types import ModuleType
from typing import Any

import gpsoauth

from ..const import CONF_OAUTH_TOKEN, DATA_AAS_TOKEN
from .token_cache import TokenCache
from .username_provider import username_string

_LOGGER = logging.getLogger(__name__)

gpsoauth_exceptions: ModuleType | None = None
try:  # pragma: no cover - defensive optional import layout
    from gpsoauth import exceptions as gpsoauth_exceptions
except Exception:  # noqa: BLE001
    gpsoauth_exceptions = None

_JWT_SEGMENT_MIN_COUNT = 2


# ---------------------------------------------------------------------------
# Helpers (privacy-friendly logging, validation, brief error messages)
# ---------------------------------------------------------------------------


def _clip(value: object, limit: int = 200) -> str:
    """Clip long strings to a safe length for logs."""
    s = str(value)
    return s if len(s) <= limit else (s[: limit - 1] + "…")


def _summarize_response(obj: Mapping[str, Any] | object) -> str:
    """Summarize a gpsoauth response without leaking sensitive data."""
    if isinstance(obj, Mapping):
        keys = ", ".join(sorted(map(str, obj.keys())))
        return f"dict(keys=[{keys}])"
    return f"{type(obj).__name__}"


def _mask_email_for_logs(email: str | None) -> str:
    """Return a privacy-friendly representation of an email for logs."""

    if not email or "@" not in email:
        return "<unknown>"
    local, domain = email.split("@", 1)
    if not local:
        return f"*@{domain}"
    masked_local = (local[0] + "***") if len(local) > 1 else "*"
    return f"{masked_local}@{domain}"


def _looks_like_jwt(token: str) -> bool:
    """Very lightweight check for JWT-like blobs (Base64URL x3, commonly 'eyJ' prefix).

    Note:
        We only use this to *disqualify* obviously wrong inputs for the OAuth→AAS
        exchange path. This is not a full JWT validator and intentionally avoids
        strict checks to keep the code robust and non-invasive.
    """
    return token.count(".") >= _JWT_SEGMENT_MIN_COUNT and token[:3] == "eyJ"


def _disqualifies_oauth_for_exchange(token: str) -> str | None:
    """Return a reason string if the value is clearly not suitable as an OAuth token.

    This function implements a negative filter. If it returns a non-empty string,
    callers must ignore the value for the OAuth→AAS exchange and use fallbacks.
    """
    if _looks_like_jwt(token):
        return "value looks like a JWT (possibly installation/ID token), not an OAuth token"
    return None


def _is_non_retryable_auth(err: Exception) -> bool:
    """Return True if the error indicates a non-recoverable auth problem."""
    text = _clip(err).lower()
    if "badauthentication" in text:
        return True
    if "invalid_grant" in text:
        return True
    if "unauthorized" in text or "forbidden" in text:
        return True
    # gpsoauth sometimes returns dicts with {"Error": ...}; these are wrapped in our error text
    if "missing 'token' in gpsoauth response" in text:
        return True
    return False


def _coerce_android_id(value: object, source: str) -> int | None:
    """Normalize cached or credential Android IDs into integers."""

    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except (TypeError, ValueError):
            _LOGGER.debug("android_id value from %s is not numeric", source)
            return None
    if value is not None:
        _LOGGER.debug("Unsupported android_id type from %s: %s", source, type(value))
    return None


async def _get_or_generate_android_id(
    username: str, cache: TokenCache | None = None
) -> int:
    """Return a per-user Android ID from cache, FCM credentials, or a fresh value."""

    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    cache_key = f"android_id_{username}"
    cached_android_id = _coerce_android_id(await cache.get(cache_key), "cache")

    fcm_creds = await cache.get("fcm_credentials")
    android_id = None
    if isinstance(fcm_creds, Mapping):
        gcm_block = fcm_creds.get("gcm")
        if isinstance(gcm_block, Mapping):
            android_id = _coerce_android_id(
                gcm_block.get("android_id"), "FCM credentials"
            )

    if android_id is not None:
        try:
            await cache.set(cache_key, android_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to persist android_id from FCM credentials: %s", _clip(err))
        return android_id

    if cached_android_id is not None:
        return cached_android_id

    android_id = random.randint(0x1000000000000000, 0xFFFFFFFFFFFFFFFF)
    _LOGGER.warning(
        "Generated new android_id for %s; cache was missing a stored identifier.",
        _mask_email_for_logs(username),
    )
    try:
        await cache.set(cache_key, android_id)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to persist generated android_id: %s", _clip(err))
    return android_id


# ---------------------------------------------------------------------------
# Core exchange (executor offload)
# ---------------------------------------------------------------------------


async def _exchange_oauth_for_aas(
    username: str, oauth_token: str, android_id: int
) -> dict[str, Any]:
    """Run the blocking gpsoauth exchange in an executor.

    Args:
        username: Google account e-mail.
        oauth_token: OAuth token to exchange.
        android_id: Android ID tied to the FCM credentials for this account.

    Returns:
        The raw dictionary response from gpsoauth containing at least a 'Token' key.

    Raises:
        RuntimeError: If the exchange fails or returns an invalid response.
    """

    def _run() -> dict[str, Any]:
        # gpsoauth.exchange_token(username, oauth_token, android_id) is blocking.
        return gpsoauth.exchange_token(username, oauth_token, android_id)

    loop = asyncio.get_running_loop()

    _LOGGER.debug(
        "Calling gpsoauth.exchange_token with username=%s, oauth_token_prefix=%s, oauth_token_len=%d, android_id=0x%X",
        _mask_email_for_logs(username),
        oauth_token[:10] + "..." if oauth_token else "None",
        len(oauth_token) if oauth_token else 0,
        android_id,
    )

    try:
        resp = await loop.run_in_executor(None, _run)
    except Exception as err:  # noqa: BLE001
        if gpsoauth_exceptions and isinstance(err, gpsoauth_exceptions.AuthError):
            _LOGGER.warning(
                "gpsoauth authentication error for %s: %s",
                _mask_email_for_logs(username),
                _clip(err),
            )
            raise RuntimeError(f"gpsoauth authentication failed: {_clip(err)}") from err
        _LOGGER.error(
            "gpsoauth exchange failed unexpectedly for %s: %s",
            _mask_email_for_logs(username),
            _clip(err),
        )
        raise RuntimeError(f"gpsoauth exchange failed: {_clip(err)}") from err

    _LOGGER.debug(
        "gpsoauth exchange response received: type=%s, keys=%s",
        type(resp).__name__,
        list(resp.keys()) if isinstance(resp, dict) else "N/A",
    )

    if not isinstance(resp, dict) or not resp:
        raise RuntimeError(
            f"Invalid response from gpsoauth: {_summarize_response(resp)}"
        )
    if "Token" not in resp:
        error_details = None
        resp_keys: list[str] | str = "N/A"
        if isinstance(resp, dict):
            resp_keys = list(resp.keys())
            error_details = resp.get("ErrorDetails") or resp.get("Error")
        _LOGGER.warning(
            "gpsoauth response missing 'Token'. Error details (if any): %s. Response keys: %s",
            error_details,
            resp_keys,
        )
        raise RuntimeError("Missing 'Token' in gpsoauth response")
    return resp


# ---------------------------------------------------------------------------
# Token generation (entry-scoped when `cache` is provided)
# ---------------------------------------------------------------------------


async def _generate_aas_token(*, cache: TokenCache) -> str:  # noqa: PLR0912, PLR0915
    """Generate an AAS token using the best available OAuth token and username.

    Strategy:
        1) Try the explicit OAuth token from the cache (`CONF_OAUTH_TOKEN`).
           1a) If the value is *clearly not* an OAuth token (e.g., JWT), ignore it.
        2) If missing, scan for any `adm_token_*` key and reuse its value as an OAuth token.
           In that case, set `username` from the key suffix (after `adm_token_`).
        3) Exchange OAuth → AAS via gpsoauth in an executor.
        4) Update the cached username if gpsoauth returns an 'Email' field (entry-scoped when possible).

    Args:
        cache: Optional TokenCache instance for multi-account isolation.

    Returns:
        The AAS token string.

    Raises:
        ValueError: If required inputs are missing.
        RuntimeError: If gpsoauth exchange fails or returns an invalid response.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    # 0) Username (prefer entry cache when available)
    cached_user = await cache.get(username_string)
    username: str | None = str(cached_user) if isinstance(cached_user, str) else None

    # 1) Explicit OAuth token from cache
    oauth_val = await cache.get(CONF_OAUTH_TOKEN)
    oauth_token: str | None = str(oauth_val) if isinstance(oauth_val, str) else None

    # Defensive negative validation for OAuth slot
    if oauth_token:
        reason = _disqualifies_oauth_for_exchange(oauth_token)
        if reason:
            _LOGGER.warning("Ignoring value from '%s': %s.", CONF_OAUTH_TOKEN, reason)
            oauth_token = None  # Force fallback path

    # 2) Fallback: scan ADM tokens if no explicit OAuth token exists or it was disqualified
    if oauth_token and oauth_token.startswith("aas_et/"):
        if not username:
            raise ValueError(
                "No username available; please ensure the account e-mail is configured."
            )
        _LOGGER.debug(
            "Cached value for '%s' already looks like an AAS token; reusing without gpsoauth.exchange_token.",
            CONF_OAUTH_TOKEN,
        )
        try:
            await cache.set(DATA_AAS_TOKEN, oauth_token)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to persist cached AAS token shortcut: %s", _clip(err))
        return oauth_token

    if not oauth_token:
        all_cached = await cache.all()
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
                    (username or "unknown").split("@", 1)[0] + "@…",
                )
                break

        # Fallback 3: Try global cache for ADM tokens if entry cache had none (validation scenario)
        if not oauth_token and cache:
            try:
                all_cached_global = await async_get_all_cached_values()
                for key, value in all_cached_global.items():
                    if isinstance(key, str) and key.startswith("adm_token_") and isinstance(value, str) and value:
                        oauth_token = value
                        extracted_username = key.replace("adm_token_", "", 1)
                        if extracted_username and "@" in extracted_username:
                            username = extracted_username
                        _LOGGER.info("Using existing ADM token from global cache for OAuth exchange (user: %s).", username or "unknown")
                        break
            except Exception:  # noqa: BLE001
                pass

    if not oauth_token:
        raise ValueError(
            "No OAuth token available; please configure the integration with a valid token."
        )
    if not username:
        # We need a username only for the gpsoauth exchange path.
        raise ValueError(
            "No username available; please ensure the account e-mail is configured."
        )

    android_id = await _get_or_generate_android_id(username, cache=cache)

    # 3) Exchange OAuth → AAS (blocking call executed in executor).
    resp = await _exchange_oauth_for_aas(username, oauth_token, android_id)

    # 4) Persist normalized email if gpsoauth returns it (keeps cache consistent).
    if isinstance(resp.get("Email"), str) and resp["Email"]:
        try:
            await cache.set(username_string, resp["Email"])
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Failed to persist normalized username from gpsoauth: %s", _clip(err)
            )

    return str(resp["Token"])


# ---------------------------------------------------------------------------
# Public API (entry-scoped when `cache` is provided) with retries/backoff
# ---------------------------------------------------------------------------


async def async_get_aas_token(
    *,
    cache: TokenCache,
    retries: int = 2,
    backoff: float = 1.0,
) -> str:
    """Return the cached AAS token or compute and cache it.

    When an entry-scoped `cache` is provided, only that cache is used for reads/writes.
    Otherwise, the legacy facade operates on the default cache (single-entry setups).

    Persistence:
        - Stored under key `DATA_AAS_TOKEN` in the selected cache.

    Retry policy:
        - Non-retryable auth failures (e.g., "BadAuthentication", "invalid_grant",
          "unauthorized"/"forbidden") abort immediately.
        - Transient errors (network/timeouts/library) retry with exponential backoff.

    Args:
        cache: Entry-scoped TokenCache.
        retries: Number of retry attempts on transient failure.
        backoff: Initial backoff delay in seconds for retries.

    Returns:
        The AAS token string.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    async def _gen_with_retries() -> str:
        last_exc: Exception | None = None
        attempts = max(1, retries + 1)
        for attempt in range(attempts):
            try:
                return await _generate_aas_token(cache=cache)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _is_non_retryable_auth(exc) or attempt >= attempts - 1:
                    _LOGGER.error(
                        "AAS token generation failed%s: %s",
                        "" if attempt >= attempts - 1 else " (non-retryable)",
                        _clip(exc),
                    )
                    break
                sleep_s = backoff * (2**attempt)
                _LOGGER.info(
                    "AAS token generation failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    _clip(exc),
                    sleep_s,
                )
                await asyncio.sleep(sleep_s)
        assert last_exc is not None
        raise last_exc

    token: str = await cache.get_or_set(DATA_AAS_TOKEN, _gen_with_retries)
    return token


# ----------------------- Legacy sync wrapper (unsupported) -----------------------


def get_aas_token() -> str:  # pragma: no cover - legacy path kept for compatibility messaging
    """Legacy sync API is intentionally unsupported to prevent event loop deadlocks.

    Raises:
        NotImplementedError: Always. Use `await async_get_aas_token()` instead.
    """
    raise NotImplementedError(
        "Use `await async_get_aas_token(cache=...)` instead of the synchronous get_aas_token()."
    )
