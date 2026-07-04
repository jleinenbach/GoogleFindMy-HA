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
  backoff; clear auth failures (e.g., "BadAuthentication", "invalid_grant") are
  **not** retried. Non-retryable auth is matched structurally via ``error_kind``,
  not via broad HTTP substrings (401/403, "unauthorized"/"forbidden") in free-form
  text.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Mapping
from types import ModuleType
from typing import Any

from ..const import CONF_OAUTH_TOKEN, DATA_AAS_TOKEN
from .gpsoauth_loader import (
    GpsoauthModule,
    load_gpsoauth_exceptions,
    require_gpsoauth,
)
from .gpsoauth_loader import (
    gpsoauth as _gpsoauth_proxy,
)
from .token_cache import TokenCache
from .username_provider import username_string

_LOGGER = logging.getLogger(__name__)

gpsoauth_exceptions: ModuleType | None = load_gpsoauth_exceptions()
gpsoauth = _gpsoauth_proxy


def _gpsoauth() -> GpsoauthModule:
    """Import the optional gpsoauth module on demand."""

    return require_gpsoauth()


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


# Closed-set values that ``error_kind`` can carry from the gpsoauth exchange
# paths and that mark a definitively non-recoverable auth problem:
# ``auth_error`` (explicit gpsoauth ``AuthError`` surface) and the lowercased
# gpsoauth ``Error`` field values (``badauthentication``, ``invalid_grant``,
# ...). ``exchange_error`` is intentionally *not* listed: it is a catch-all
# wrapper for arbitrary executor failures (incl. transient network errors)
# and must remain retryable.
_NON_RETRYABLE_KINDS: frozenset[str] = frozenset(
    {
        "auth_error",
        "badauthentication",
        "invalid_grant",
    }
)

# Substrings that surface in ``_clip(err)`` for callers that raise
# ``RuntimeError`` without the ``error_kind`` attribute (legacy paths and the
# existing test suite). Only gpsoauth-specific vocabulary is matched here. The
# HTTP-generic tokens ("unauthorized" / "forbidden") are deliberately NOT
# listed: genuine HTTP auth denials are carried structurally (``error_kind``),
# and a free-text substring must not force a network-adjacent error to be
# treated as a permanent, non-retryable auth failure.
_NON_RETRYABLE_PATTERNS: tuple[str, ...] = (
    "badauthentication",
    "invalid_grant",
    "missing 'token' in gpsoauth response",
)


def _is_non_retryable_auth(err: Exception) -> bool:
    """Return True if the error indicates a non-recoverable auth problem.

    Prefers the structured ``error_kind`` attribute (set by the gpsoauth
    exchange paths in this module). Falls back to inspecting the clipped
    string representation for callers that raise ``RuntimeError`` without
    an attribute (e.g. HTTP-status surfaces in sibling modules and the
    existing test suite).
    """
    kind = getattr(err, "error_kind", "")
    if isinstance(kind, str) and kind.lower() in _NON_RETRYABLE_KINDS:
        return True
    text = _clip(err).lower()
    return any(pattern in text for pattern in _NON_RETRYABLE_PATTERNS)


def _coerce_android_id(value: object, source: str) -> int | None:
    """Normalize cached or credential Android IDs into integers."""

    if isinstance(value, bool):
        _LOGGER.debug("android_id value from %s is boolean; ignoring", source)
        return None

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
            _LOGGER.debug(
                "Skipping cache write for android_id derived from FCM bundle; persistence failed",
                exc_info=err,
            )
        return android_id

    if cached_android_id is not None:
        return cached_android_id

    android_id = secrets.randbelow(0xF000000000000000) + 0x1000000000000000
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
        return _gpsoauth().exchange_token(username, oauth_token, android_id)

    loop = asyncio.get_running_loop()

    _LOGGER.debug(
        "Calling gpsoauth.exchange_token.",
        extra={
            "user": _mask_email_for_logs(username),
            "token_length": len(oauth_token) if oauth_token else 0,
            "android_id_hex": f"0x{android_id:X}",
        },
    )

    try:
        resp = await loop.run_in_executor(None, _run)
    except Exception as err:  # noqa: BLE001
        if gpsoauth_exceptions and isinstance(err, gpsoauth_exceptions.AuthError):
            # Per Auth/AGENTS.md (lines 99-102, 133-135): keep raw exception
            # text out of the log message; surface a sanitized ``error_kind``
            # via ``extra`` and preserve traceback context via ``exc_info``.
            _LOGGER.warning(
                "gpsoauth authentication error.",
                extra={
                    "user": _mask_email_for_logs(username),
                    "error_kind": "auth_error",
                },
                exc_info=err,
            )
            new_err = RuntimeError("gpsoauth authentication failed (kind=auth_error)")
            new_err.error_kind = "auth_error"  # type: ignore[attr-defined]
            raise new_err from err
        # Some callers (and test doubles) raise a plain ``RuntimeError`` with a
        # known auth marker embedded in the message instead of an
        # ``AuthError``. Surface those as ``auth_error`` (non-retryable) so the
        # retry loop matches the explicit ``AuthError`` branch above; otherwise
        # fall back to the generic ``exchange_error`` (retryable) wrapper.
        clipped_lower = _clip(err).lower()
        if "badauthentication" in clipped_lower or "invalid_grant" in clipped_lower:
            wrapped_kind = "auth_error"
            wrapped_msg = "gpsoauth authentication failed (kind=auth_error)"
            log_level = _LOGGER.warning
        else:
            wrapped_kind = "exchange_error"
            wrapped_msg = "gpsoauth exchange failed (kind=exchange_error)"
            log_level = _LOGGER.error
        log_level(
            "gpsoauth exchange failed unexpectedly.",
            extra={
                "user": _mask_email_for_logs(username),
                "error_kind": wrapped_kind,
            },
            exc_info=err,
        )
        new_err = RuntimeError(wrapped_msg)
        new_err.error_kind = wrapped_kind  # type: ignore[attr-defined]
        raise new_err from err

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
        error_value = resp.get("Error", "") if isinstance(resp, dict) else ""
        error_details = resp.get("ErrorDetails", "") if isinstance(resp, dict) else ""
        resp_keys = list(resp.keys()) if isinstance(resp, dict) else "N/A"
        # Per Auth/AGENTS.md (lines 99-102, 133-135): keep raw gpsoauth
        # response bodies (esp. ``ErrorDetails``) out of the log message;
        # surface only sanitized flags/keys via ``extra``. The gpsoauth
        # ``Error`` field is a small closed set (e.g. ``NeedsBrowser``,
        # ``BadAuthentication``) and is exposed as ``error_kind``.
        _LOGGER.warning(
            "gpsoauth response missing token (user=%s, keys=%d)",
            _mask_email_for_logs(username),
            len(resp_keys) if isinstance(resp_keys, list) else 0,
            extra={
                "error_field_present": bool(error_value),
                "error_kind": (str(error_value)[:32] if error_value else None),
                "details_present": bool(error_details),
                "response_keys": resp_keys,
                "user": _mask_email_for_logs(username),
            },
        )
        error_kind = str(error_value)[:32] if error_value else "(none)"
        new_err = RuntimeError(
            f"Missing 'Token' in gpsoauth response (kind={error_kind})"
        )
        new_err.error_kind = error_kind  # type: ignore[attr-defined]
        raise new_err
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
            _LOGGER.debug("Failed to persist cached AAS token shortcut.", exc_info=err)
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
                    "Using existing ADM token from cache for OAuth exchange.",
                    extra={"user": _mask_email_for_logs(username)},
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

    android_id = await _get_or_generate_android_id(username, cache=cache)

    # 3) Exchange OAuth → AAS (blocking call executed in executor).
    try:
        resp = await _exchange_oauth_for_aas(username, oauth_token, android_id)
    except RuntimeError:
        if not oauth_token.startswith("aas_et/"):
            _LOGGER.error(
                "AAS token exchange failed with a likely-expired OAuth cookie. "
                "The Chrome OAuth cookie is single-use and cannot be reused. "
                "Please re-authenticate: "
                "python -m custom_components.googlefindmy --reauth",
            )
        raise

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
        - Issuance timestamp stored under `aas_token_issued_at_<username>` for TTL learning.

    Retry policy:
        - Non-retryable auth failures (e.g., "BadAuthentication", "invalid_grant")
          abort immediately, matched structurally via ``error_kind`` rather than via
          broad HTTP substrings ("unauthorized"/"forbidden") in free-form text.
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

    # Check if AAS token already exists (for TTL tracking)
    existing_token = await cache.get(DATA_AAS_TOKEN)
    was_cached = existing_token is not None and isinstance(existing_token, str)

    async def _gen_with_retries() -> str:
        _LOGGER.info("AAS token: generating new token via OAuth exchange.")
        last_exc: Exception | None = None
        attempts = max(1, retries + 1)
        max_retries = attempts - 1  # = retries parameter
        for attempt in range(attempts):
            try:
                token = await _generate_aas_token(cache=cache)
                _LOGGER.info("AAS token: successfully generated.")
                return token
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                retryable = not _is_non_retryable_auth(exc) and attempt < attempts - 1
                # retry_num: first attempt (attempt=0) doesn't count
                retry_num = attempt

                if not retryable:
                    if retry_num > 0:
                        _LOGGER.error(
                            "AAS token: generation failed (retry %d/%d). No more retries. "
                            "Error: %s",
                            retry_num,
                            max_retries,
                            exc,
                        )
                    else:
                        _LOGGER.error("AAS token: generation failed. Error: %s", exc)
                    break

                sleep_s = backoff * (2**attempt)
                if retry_num == 0:
                    _LOGGER.warning(
                        "AAS token: generation failed. Error: %s. Retrying...", exc
                    )
                else:
                    _LOGGER.warning(
                        "AAS token: generation failed (retry %d/%d). Error: %s. "
                        "Retrying in %.0fs...",
                        retry_num,
                        max_retries,
                        exc,
                        sleep_s,
                    )
                await asyncio.sleep(sleep_s)
        assert last_exc is not None
        raise last_exc

    token: str = await cache.get_or_set(DATA_AAS_TOKEN, _gen_with_retries)

    # Record AAS token issuance timestamp if a fresh token was generated
    # This enables TTL learning for proactive AAS refresh
    if not was_cached:
        username_val = await cache.get(username_string)
        if isinstance(username_val, str) and username_val:
            issued_key = f"aas_token_issued_at_{username_val.strip().lower()}"
            try:
                await cache.set(issued_key, time.time())
                _LOGGER.debug(
                    "Recorded AAS token issuance timestamp for TTL learning.",
                    extra={"user": _mask_email_for_logs(username_val)},
                )
            except Exception as err:  # noqa: BLE001 - defensive cache write
                _LOGGER.debug("Failed to record AAS issuance timestamp: %s", err)

    return token


# ----------------------- Legacy sync wrapper (CLI/offline only) -----------------------


def get_aas_token(*, cache: TokenCache) -> str:
    """Synchronous facade for CLI/offline usage; not allowed in the HA event loop.

    Raises:
        RuntimeError: If called from within a running event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(async_get_aas_token(cache=cache))
    raise RuntimeError(
        "Sync get_aas_token() called from the event loop. "
        "Use `await async_get_aas_token()` instead."
    )
