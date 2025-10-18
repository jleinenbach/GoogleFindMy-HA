# custom_components/googlefindmy/SpotApi/spot_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Tuple, Optional

import httpx

from custom_components.googlefindmy.Auth.username_provider import (
    get_username,          # sync wrapper (CLI/dev; deprecated for HA)
    async_get_username,    # async-first (HA)
)
from custom_components.googlefindmy.SpotApi.grpc_parser import GrpcParser

# Sync helpers for CLI/dev usage (never call inside the HA event loop)
from custom_components.googlefindmy.Auth.spot_token_retrieval import (
    get_spot_token,         # sync API (CLI-only)
    async_get_spot_token,   # async API (preferred in HA)
)
from custom_components.googlefindmy.Auth.adm_token_retrieval import (
    get_adm_token,                                 # sync API (CLI-only)
    async_get_adm_token as async_get_adm_token_api # async API (preferred in HA)
)

# Cache access (we use async variants in async path and sync in CLI path)
from custom_components.googlefindmy.Auth.token_cache import (
    get_cached_value,
    set_cached_value,
    async_get_cached_value,
    async_set_cached_value,
    # Optional: entry-scoped cache object (when available at call sites)
    TokenCache,
)

_LOGGER = logging.getLogger(__name__)

# --------------------------- Exceptions (SpotError hierarchy) ---------------------------

class SpotError(Exception):
    """Base exception for SPOT request errors."""

class SpotAuthPermanentError(SpotError):
    """Authentication/authorization failed even after a refresh attempt (re-auth likely required)."""

class SpotRateLimitError(SpotError):
    """Rate limited after retries."""

class SpotHTTPError(SpotError):
    """Non-auth HTTP error (4xx/5xx) after retries."""

class SpotNetworkError(SpotError):
    """Network/transport failure after retries."""

class SpotTrailersOnlyError(SpotError):
    """HTTP 200 but trailers-only / invalid gRPC body."""

class SpotRequestFailedAfterRetries(SpotError):
    """Generic failure after all retries for non-auth categories."""

# --------------------------- Retry/backoff helpers ---------------------------

_SPOT_MAX_RETRIES = 3
_SPOT_INITIAL_BACKOFF_S = 1.0
_SPOT_BACKOFF_FACTOR = 2.0
_SPOT_MAX_RETRY_AFTER_S = 60.0

def _compute_delay(attempt: int, retry_after: Optional[str]) -> float:
    """Respect Retry-After header (seconds or HTTP-date), otherwise exponential backoff with jitter."""
    delay: Optional[float] = None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:
                retry_dt = parsedate_to_datetime(retry_after)
                delay = max(0.0, (retry_dt - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
            except Exception:
                delay = None
    if delay is None:
        base = (_SPOT_BACKOFF_FACTOR ** (attempt - 1)) * _SPOT_INITIAL_BACKOFF_S
        delay = random.uniform(0.0, base)
    return min(delay, _SPOT_MAX_RETRY_AFTER_S)

# --------------------------- Token selection (sync; CLI only) ---------------------------

def _pick_auth_token(prefer_adm: bool = False) -> Tuple[str, str, str]:
    """
    Select a valid auth token (sync). Prefer SPOT unless prefer_adm=True.

    DEPRECATED for HA use. CLI/Testing ONLY. Uses global state and may exhibit cross-account behavior.

    Returns:
        (token, kind, token_owner_username)

    NOTE (auth routing):
    - Try SPOT first for the current user (unless prefer_adm=True).
    - Fallback to ADM for the same user.
    - As a last resort, scan cached ADM tokens from other users (sync path only).
    """
    original_username = get_username()

    if not prefer_adm:
        try:
            tok = get_spot_token(original_username)
            return tok, "spot", original_username
        except Exception as e:
            _LOGGER.debug(
                "Failed to get SPOT token for %s: %s; falling back to ADM",
                original_username, e
            )

    # Try ADM for the same user first (deterministic)
    tok = get_cached_value(f"adm_token_{original_username}")
    if not tok:
        try:
            tok = get_adm_token(original_username)
        except Exception:
            tok = None
    if tok:
        return tok, "adm", original_username

    # Fallback: any cached ADM token (multi-account) — last resort (sync path only)
    try:
        from custom_components.googlefindmy.Auth.token_cache import get_all_cached_values  # optional legacy helper
        for key, value in (get_all_cached_values() or {}).items():
            if key.startswith("adm_token_") and "@" in key and value:
                fallback_username = key.replace("adm_token_", "")
                _LOGGER.debug("Using ADM token from cache for %s (fallback)", fallback_username)
                return value, "adm", fallback_username
    except Exception:
        pass

    # No token available for any route
    raise RuntimeError("No valid SPOT/ADM token available")

def _invalidate_token(kind: str, username: str) -> None:
    """
    Invalidate cached tokens (sync; CLI only).

    DEPRECATED for HA use. CLI/Testing ONLY. Uses global state and may exhibit cross-account behavior.
    """
    if kind == "adm":
        set_cached_value(f"adm_token_{username}", None)
    elif kind == "spot":
        set_cached_value(f"spot_token_{username}", None)
        set_cached_value("aas_token", None)

# --------------------------- Token selection (async; HA) ---------------------------

async def _pick_auth_token_async(
    prefer_adm: bool = False,
    *,
    cache: TokenCache | None = None,
) -> Tuple[str, str, str]:
    """
    Select a valid auth token (async). Prefer SPOT unless prefer_adm=True.

    Returns:
        (token, kind, token_owner_username)

    Async rules:
    - Use async username provider.
    - Prefer native async token retrieval.
    - Do NOT perform full-cache scans in the async path (avoid heavy ops).
    - If a `cache` is provided, pass it through where supported.
    """
    user = await async_get_username()
    if not user:
        raise RuntimeError("Username is not configured; cannot select auth token.")

    # Prefer SPOT unless explicitly preferring ADM
    if not prefer_adm:
        try:
            # Prefer async API; (optional) cache arg is supported by some impls
            try:
                tok = await async_get_spot_token(user)  # type: ignore[call-arg]
            except TypeError:
                # Older signature without username parameter
                tok = await async_get_spot_token()  # type: ignore[misc]
            if tok:
                return tok, "spot", user
        except Exception as e:
            _LOGGER.debug("Failed to get SPOT token for %s: %s; falling back to ADM", user, e)

    # Try ADM for the same user
    if cache is not None:
        tok = await cache.get(f"adm_token_{user}")
    else:
        tok = await async_get_cached_value(f"adm_token_{user}")

    if not tok:
        try:
            if cache is not None:
                tok = await async_get_adm_token_api(user, cache=cache)
            else:
                tok = await async_get_adm_token_api(user)
        except Exception:
            tok = None

    if tok:
        return tok, "adm", user

    # No cross-account fallback in async path (would require full-cache scans)
    raise RuntimeError("No valid SPOT/ADM token available for current user")

async def _invalidate_token_async(
    kind: str,
    username: str,
    *,
    cache: TokenCache | None = None,
) -> None:
    """Async invalidation of cached tokens (scoped to token owner's username; entry-scoped when `cache` provided)."""
    if cache is not None:
        if kind == "adm":
            await cache.set(f"adm_token_{username}", None)
        elif kind == "spot":
            await cache.set(f"spot_token_{username}", None)
            # AAS should be entry-scoped; invalidate in the same cache to force fresh chain
            await cache.set("aas_token", None)
        return

    # Fallback to global async facades
    if kind == "adm":
        await async_set_cached_value(f"adm_token_{username}", None)
    elif kind == "spot":
        await async_set_cached_value(f"spot_token_{username}", None)
        await async_set_cached_value("aas_token", None)

# ------------------------------ SYNC API (CLI/dev) ------------------------------

def spot_request(api_scope: str, payload: bytes) -> bytes:
    """
    Perform a SPOT gRPC unary request over HTTP/2 (synchronous).

    IMPORTANT:
        - DEPRECATED for HA use. CLI/Testing ONLY. Uses global state and may exhibit cross-account behavior.
        - Do NOT call this from within Home Assistant's event loop; use
          `await async_spot_request(...)` instead.

    Responsibilities
    ----------------
    - Enforce HTTP/2 + TE: trailers (required by gRPC).
    - Send framed request (5-byte gRPC prefix).
    - Handle three server patterns:
        (1) 200 + data frame(s)  -> extract and return the uncompressed payload.
        (2) 200 + trailers-only  -> no DATA frames; read grpc-status/message and log appropriately.
        (3) Non-200 HTTP         -> log diagnostics and raise.
    - Keep return type stable for callers: bytes or empty bytes on trailers-only/invalid 200 bodies.
    - On persistent AuthN/AuthZ failure (gRPC 16/7) after a retry, raise to avoid silent failure.
    """
    # Fail-fast if called in the running event loop
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            raise RuntimeError(
                "Sync spot_request() called from within the event loop. "
                "Use `await async_spot_request(...)` instead."
            )
    except RuntimeError:
        # No running loop -> OK for CLI usage
        pass

    url = "https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/" + api_scope

    # Ensure HTTP/2 support is available (httpx[http2] -> h2)
    try:
        import h2  # noqa: F401
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "HTTP/2 support is required for SPOT gRPC. Please install the HTTP/2 extra: pip install 'httpx[http2]'"
        ) from e

    grpc_body = GrpcParser.construct_grpc(payload)

    attempts = 0
    prefer_adm = False  # If first try with SPOT hits AuthN/AuthZ error, switch to ADM on retry.

    # Networking: reuse a single HTTP/2 client for both attempts (perf + connection reuse)
    with httpx.Client(http2=True, timeout=30.0) as client:
        while attempts < 2:
            token, kind, token_user = _pick_auth_token(prefer_adm=prefer_adm)

            headers = {
                "User-Agent": "com.google.android.gms/244433022 grpc-java-cronet/1.69.0-SNAPSHOT",
                "Content-Type": "application/grpc",
                "Te": "trailers",  # required by gRPC over HTTP/2
                "Authorization": "Bearer " + token,
                "Grpc-Accept-Encoding": "gzip",
            }

            resp = client.post(url, headers=headers, content=grpc_body)
            status = resp.status_code
            ctype = resp.headers.get("Content-Type")
            clen = len(resp.content or b"")
            _LOGGER.debug("SPOT %s: HTTP %s, ctype=%s, len=%d", api_scope, status, ctype, clen)

            # (1) Happy path: 200 + valid gRPC message frame
            if status == 200 and clen >= 5 and resp.content[0] in (0, 1):
                return GrpcParser.extract_grpc_payload(resp.content)

            # (2) Trailer-only / invalid-body handling (HTTP 200 without a usable frame)
            grpc_status = resp.headers.get("grpc-status")
            grpc_msg = resp.headers.get("grpc-message")

            if status == 200:
                # 2a) Explicit gRPC status in trailers (no data frames)
                if grpc_status and grpc_status != "0":
                    code_name = {"16": "UNAUTHENTICATED", "7": "PERMISSION_DENIED"}.get(grpc_status, "NON_OK")

                    if grpc_status in ("16", "7"):
                        _LOGGER.error(
                            "SPOT %s trailers-only error: grpc-status=%s (%s), msg=%s",
                            api_scope, grpc_status, code_name, grpc_msg
                        )
                        if attempts == 0:
                            _invalidate_token(kind, token_user)
                            attempts += 1
                            prefer_adm = (kind == "spot")
                            continue
                        raise RuntimeError(f"Spot API authentication failed after retry ({code_name})")

                    _LOGGER.warning(
                        "SPOT %s trailers-only non-OK: grpc-status=%s (%s), msg=%s",
                        api_scope, grpc_status, code_name, grpc_msg
                    )
                    return b""

                # 2b) No grpc-status, but body is empty or not a valid frame: ambiguous trailers-only/protocol quirk.
                if (ctype or "").startswith("application/grpc") and clen == 0:
                    critical_methods = {"GetEidInfoForE2eeDevices"}
                    if api_scope in critical_methods:
                        _LOGGER.error(
                            "SPOT %s: HTTP 200 with empty gRPC body (likely trailers-only or missing response). "
                            "This will prevent E2EE key retrieval and decryption.",
                            api_scope,
                        )
                    else:
                        _LOGGER.warning(
                            "SPOT %s: HTTP 200 with empty gRPC body (possible trailers-only OK or missing response).",
                            api_scope,
                        )
                    return b""

                snippet = (resp.content or b"")[:128]
                _LOGGER.debug("SPOT %s invalid 200 body (no frame). Snippet=%r", api_scope, snippet)
                return b""

            # (3) Non-200 HTTP responses (retry once on common auth HTTP codes)
            if status in (401, 403) and attempts == 0:
                _LOGGER.debug("SPOT %s: %s, invalidating %s token for %s and retrying",
                              api_scope, status, kind, token_user)
                _invalidate_token(kind, token_user)
                attempts += 1
                prefer_adm = (kind == "spot")
                continue

            # Other HTTP errors: include a brief body for debugging and raise.
            def _beautify_text(resp_obj) -> str:
                try:
                    from bs4 import BeautifulSoup  # lazy import, optional
                    return BeautifulSoup(resp_obj.text, "html.parser").get_text()
                except Exception:
                    try:
                        body = (resp_obj.content or b"")[:256]
                        return body.decode("utf-8", errors="ignore")
                    except Exception:
                        return ""

            pretty = _beautify_text(resp)
            _LOGGER.debug("SPOT %s HTTP error body: %r", api_scope, pretty)
            raise RuntimeError(f"Spot API HTTP {status} for {api_scope}")

    raise RuntimeError("Spot request failed after retries")

# ------------------------------ ASYNC API (HA path) ------------------------------

async def async_spot_request(
    api_scope: str,
    payload: bytes,
    *,
    cache: TokenCache | None = None,
) -> bytes:
    """
    Perform a SPOT gRPC unary request over HTTP/2 (async, preferred in HA).

    Responsibilities
    ----------------
    - Enforce HTTP/2 + TE: trailers (required by gRPC).
    - Send framed request (5-byte gRPC prefix).
    - Handle server patterns:
        (1) 200 + data frame(s)  -> extract and return the uncompressed payload.
        (2) 200 + trailers-only  -> raise SpotTrailersOnlyError.
        (3) Non-200 HTTP         -> classify and retry where appropriate.
    - Clear signaling via SpotError hierarchy.
    - Multi-account safe: when `cache` (entry-scoped) is supplied, token selection
      and invalidation use that cache; otherwise default async facades are used.

    Returns:
        Raw protobuf payload (bytes).

    Raises:
        SpotTrailersOnlyError: on HTTP 200 without valid gRPC data (trailers-only).
        SpotAuthPermanentError: on persistent 401/403 or gRPC 16/7 after refresh attempt.
        SpotRateLimitError: on 429 after retries.
        SpotNetworkError / SpotHTTPError / SpotRequestFailedAfterRetries accordingly.
    """
    url = "https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/" + api_scope

    # Ensure HTTP/2 support is available (httpx[http2] -> h2)
    try:
        import h2  # noqa: F401
    except Exception as e:  # pragma: no cover
        raise RuntimeError(
            "HTTP/2 support is required for SPOT gRPC. Please install the HTTP/2 extra: pip install 'httpx[http2]'"
        ) from e

    grpc_body = GrpcParser.construct_grpc(payload)

    refreshed_once = False
    retries_used = 0

    async with httpx.AsyncClient(http2=True, timeout=30.0) as client:
        while True:
            attempt = retries_used + 1
            prefer_adm = refreshed_once  # after first auth failure, prefer ADM path on retry

            token, kind, token_user = await _pick_auth_token_async(
                prefer_adm=prefer_adm,
                cache=cache,
            )

            headers = {
                "User-Agent": "com.google.android.gms/244433022 grpc-java-cronet/1.69.0-SNAPSHOT",
                "Content-Type": "application/grpc",
                "Te": "trailers",  # required by gRPC over HTTP/2
                "Authorization": "Bearer " + token,
                "Grpc-Accept-Encoding": "gzip",
            }

            try:
                resp = await client.post(url, headers=headers, content=grpc_body)
            except (httpx.TimeoutException, httpx.TransportError) as net_err:
                # Network/transport → backoff & retry up to limit
                if retries_used < _SPOT_MAX_RETRIES:
                    delay = _compute_delay(attempt, None)
                    _LOGGER.warning(
                        "SPOT %s network error (%s). Retrying in %.2fs (attempt %d/%d)…",
                        api_scope, type(net_err).__name__, delay, attempt, _SPOT_MAX_RETRIES
                    )
                    retries_used += 1
                    await asyncio.sleep(delay)
                    continue
                raise SpotNetworkError(f"Network error after retries: {type(net_err).__name__}") from net_err

            status = resp.status_code
            content = resp.content or b""
            clen = len(content)
            grpc_status = resp.headers.get("grpc-status")
            grpc_msg = resp.headers.get("grpc-message")
            ctype = resp.headers.get("Content-Type", "")
            _LOGGER.debug("SPOT %s: HTTP %s, ctype=%s, len=%d", api_scope, status, ctype, clen)

            # Success: 200 + valid gRPC frame
            if status == 200 and clen >= 5 and content[0] in (0, 1):
                return GrpcParser.extract_grpc_payload(content)

            # Classify errors
            is_http_auth = status in (401, 403)
            is_grpc_auth = grpc_status in ("16", "7")  # treat both as auth; allow one refresh chance
            is_rate_limit = (status == 429) or (grpc_status == "8")  # RESOURCE_EXHAUSTED
            is_server_error = 500 <= status < 600
            # Treat all other gRPC non-OK as transient (UNKNOWN, INTERNAL, UNAVAILABLE, DEADLINE_EXCEEDED, …)
            is_grpc_non_ok = grpc_status is not None and grpc_status != "0" and not is_grpc_auth

            # 200 but trailers-only / invalid body
            if status == 200 and (clen == 0 or content[0] not in (0, 1) or is_grpc_non_ok):
                # No data frame or explicit non-OK trailers → raise for callers to map
                raise SpotTrailersOnlyError(
                    f"Trailers-only or invalid body (grpc-status={grpc_status!r}, msg={grpc_msg!r})"
                )

            # Auth errors → one refresh attempt total
            if is_http_auth or is_grpc_auth:
                src = f"HTTP {status}" if is_http_auth else f"gRPC {grpc_status}"
                if not refreshed_once:
                    _LOGGER.info(
                        "SPOT %s auth error (%s); invalidating %s token for %s and retrying once…",
                        api_scope, src, kind, token_user,
                    )
                    await _invalidate_token_async(kind, token_user, cache=cache)
                    refreshed_once = True
                    # immediate retry (do not consume backoff budget)
                    continue
                raise SpotAuthPermanentError(f"Authentication failed after refresh attempt ({src}).")

            # Rate limiting → backoff with Retry-After
            if is_rate_limit:
                retry_after = resp.headers.get("Retry-After")
                if retries_used < _SPOT_MAX_RETRIES:
                    delay = _compute_delay(attempt, retry_after)
                    _LOGGER.warning(
                        "SPOT %s rate limited (%s). Retrying in %.2fs (attempt %d/%d)…",
                        api_scope,
                        "HTTP 429" if status == 429 else "gRPC 8",
                        delay, attempt, _SPOT_MAX_RETRIES,
                    )
                    retries_used += 1
                    await asyncio.sleep(delay)
                    continue
                raise SpotRateLimitError(f"Rate limited after {_SPOT_MAX_RETRIES} attempts.")

            # Server / transient errors → backoff & retry
            if is_server_error or is_grpc_non_ok or status in (408,):
                if retries_used < _SPOT_MAX_RETRIES:
                    delay = _compute_delay(attempt, resp.headers.get("Retry-After"))
                    _LOGGER.warning(
                        "SPOT %s transient/server error (HTTP %s, gRPC %s). Retrying in %.2fs (attempt %d/%d)…",
                        api_scope, status, grpc_status, delay, attempt, _SPOT_MAX_RETRIES
                    )
                    retries_used += 1
                    await asyncio.sleep(delay)
                    continue
                raise SpotRequestFailedAfterRetries(
                    f"Transient/server error (HTTP {status}, gRPC {grpc_status}) after retries."
                )

            # Unhandled client errors (4xx other than 401/403/429)
            if 400 <= status < 500:
                raise SpotHTTPError(f"Client error HTTP {status} (gRPC {grpc_status}).")

            # Fallback: treat as failure after retries
            raise SpotRequestFailedAfterRetries(
                f"Unhandled response (HTTP {status}, gRPC {grpc_status}); body_len={clen}"
            )
