# custom_components/googlefindmy/SpotApi/spot_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

import asyncio
import datetime
import logging
import random

import httpx

from custom_components.googlefindmy.Auth.username_provider import async_get_username
from custom_components.googlefindmy.Auth.token_retrieval import InvalidAasTokenError
from custom_components.googlefindmy.SpotApi.grpc_parser import GrpcParser
from custom_components.googlefindmy.const import DATA_AAS_TOKEN

from custom_components.googlefindmy.Auth.spot_token_retrieval import (
    async_get_spot_token,
)
from custom_components.googlefindmy.Auth.adm_token_retrieval import (
    async_get_adm_token as async_get_adm_token_api,
)

from custom_components.googlefindmy.Auth.token_cache import (
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


def _compute_delay(attempt: int, retry_after: str | None) -> float:
    """Respect Retry-After header (seconds or HTTP-date), otherwise exponential backoff with jitter."""
    delay: float | None = None
    if retry_after:
        try:
            delay = float(retry_after)
        except ValueError:
            from email.utils import parsedate_to_datetime

            try:
                retry_dt = parsedate_to_datetime(retry_after)
                delay = max(
                    0.0,
                    (retry_dt - datetime.datetime.now(datetime.UTC)).total_seconds(),
                )
            except Exception:
                delay = None
    if delay is None:
        base = (_SPOT_BACKOFF_FACTOR ** (attempt - 1)) * _SPOT_INITIAL_BACKOFF_S
        delay = random.uniform(0.0, base)
    return min(delay, _SPOT_MAX_RETRY_AFTER_S)


# --------------------------- Token selection (async; HA) ---------------------------


async def _pick_auth_token_async(
    prefer_adm: bool = False,
    *,
    cache: TokenCache,
) -> tuple[str, str, str]:
    """
    Select a valid auth token (async). Prefer SPOT unless prefer_adm=True.

    Returns:
        (token, kind, token_owner_username)

    Async rules:
    - Use async username provider.
    - Prefer native async token retrieval.
    - Do NOT perform full-cache scans in the async path (avoid heavy ops).
    - All operations read/write via the provided entry-scoped cache.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    user = await async_get_username(cache=cache)
    if not user:
        raise RuntimeError("Username is not configured; cannot select auth token.")

    # Prefer SPOT unless explicitly preferring ADM
    if not prefer_adm:
        try:
            # Prefer async API; (optional) cache arg is supported by some impls
            tok = await async_get_spot_token(user, cache=cache)
            if tok:
                return tok, "spot", user
        except Exception as e:
            _LOGGER.debug(
                "Failed to get SPOT token for %s: %s; falling back to ADM", user, e
            )

    # Try ADM for the same user
    tok = await cache.get(f"adm_token_{user}")

    if not tok:
        try:
            tok = await async_get_adm_token_api(user, cache=cache)
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
            await cache.set(DATA_AAS_TOKEN, None)
        return

    # Fallback to global async facades
    if kind == "adm":
        await async_set_cached_value(f"adm_token_{username}", None)
    elif kind == "spot":
        await async_set_cached_value(f"spot_token_{username}", None)
        await async_set_cached_value(DATA_AAS_TOKEN, None)


async def _clear_aas_token_async(*, cache: TokenCache | None = None) -> None:
    """Clear the cached AAS token in the selected cache (entry-scoped when provided)."""

    if cache is not None:
        await cache.set(DATA_AAS_TOKEN, None)
        return

    await async_set_cached_value(DATA_AAS_TOKEN, None)


def spot_request(*_: object, **__: object) -> bytes:
    """Legacy synchronous interface removed in favor of async-only implementation."""

    raise RuntimeError(
        "Legacy sync spot_request() has been removed. Use async_spot_request(..., cache=...) instead."
    )


async def async_spot_request(
    api_scope: str,
    payload: bytes,
    *,
    cache: TokenCache,
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
    - Multi-account safe: token selection and invalidation always use the provided
      entry-scoped cache.

    Args:
        api_scope: Spot API method name (e.g., "GetEidInfoForE2eeDevices").
        payload: Serialized protobuf request.
        cache: Entry-scoped TokenCache used for username and token resolution.

    Returns:
        Raw protobuf payload (bytes).

    Raises:
        SpotTrailersOnlyError: on HTTP 200 without valid gRPC data (trailers-only).
        SpotAuthPermanentError: on persistent 401/403 or gRPC 16/7 after refresh attempt.
        SpotRateLimitError: on 429 after retries.
        SpotNetworkError / SpotHTTPError / SpotRequestFailedAfterRetries accordingly.
    """
    url = (
        "https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/"
        + api_scope
    )

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
    aas_reset_once = False

    async with httpx.AsyncClient(http2=True, timeout=30.0) as client:
        while True:
            attempt = retries_used + 1
            prefer_adm = (
                refreshed_once  # after first auth failure, prefer ADM path on retry
            )
            try:
                token, kind, token_user = await _pick_auth_token_async(
                    prefer_adm=prefer_adm,
                    cache=cache,
                )
            except InvalidAasTokenError as aas_err:
                if not aas_reset_once:
                    _LOGGER.warning(
                        "SPOT %s: cached AAS token rejected while selecting auth token; clearing and retrying once.",
                        api_scope,
                    )
                    await _clear_aas_token_async(cache=cache)
                    aas_reset_once = True
                    continue

                _LOGGER.error(
                    "SPOT %s: cached AAS token rejected after refresh; re-authentication required.",
                    api_scope,
                )
                raise SpotAuthPermanentError(
                    "AAS token invalid after refresh; re-authentication required."
                ) from aas_err

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
                        api_scope,
                        type(net_err).__name__,
                        delay,
                        attempt,
                        _SPOT_MAX_RETRIES,
                    )
                    retries_used += 1
                    await asyncio.sleep(delay)
                    continue
                raise SpotNetworkError(
                    f"Network error after retries: {type(net_err).__name__}"
                ) from net_err

            status = resp.status_code
            content = resp.content or b""
            clen = len(content)
            grpc_status = resp.headers.get("grpc-status")
            grpc_msg = resp.headers.get("grpc-message")
            ctype = resp.headers.get("Content-Type", "")
            _LOGGER.debug(
                "SPOT %s: HTTP %s, ctype=%s, len=%d", api_scope, status, ctype, clen
            )

            # Success: 200 + valid gRPC frame
            if status == 200 and clen >= 5 and content[0] in (0, 1):
                return GrpcParser.extract_grpc_payload(content)

            # Classify errors
            is_http_auth = status in (401, 403)
            is_grpc_auth = grpc_status in (
                "16",
                "7",
            )  # treat both as auth; allow one refresh chance
            is_rate_limit = (status == 429) or (
                grpc_status == "8"
            )  # RESOURCE_EXHAUSTED
            is_server_error = 500 <= status < 600
            # Treat all other gRPC non-OK as transient (UNKNOWN, INTERNAL, UNAVAILABLE, DEADLINE_EXCEEDED, …)
            is_grpc_non_ok = (
                grpc_status is not None and grpc_status != "0" and not is_grpc_auth
            )

            # 200 but trailers-only / invalid body
            if status == 200 and (
                clen == 0 or content[0] not in (0, 1) or is_grpc_non_ok
            ):
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
                        api_scope,
                        src,
                        kind,
                        token_user,
                    )
                    await _invalidate_token_async(kind, token_user, cache=cache)
                    aas_reset_once = True
                    refreshed_once = True
                    # immediate retry (do not consume backoff budget)
                    continue
                raise SpotAuthPermanentError(
                    f"Authentication failed after refresh attempt ({src})."
                )

            # Rate limiting → backoff with Retry-After
            if is_rate_limit:
                retry_after = resp.headers.get("Retry-After")
                if retries_used < _SPOT_MAX_RETRIES:
                    delay = _compute_delay(attempt, retry_after)
                    _LOGGER.warning(
                        "SPOT %s rate limited (%s). Retrying in %.2fs (attempt %d/%d)…",
                        api_scope,
                        "HTTP 429" if status == 429 else "gRPC 8",
                        delay,
                        attempt,
                        _SPOT_MAX_RETRIES,
                    )
                    retries_used += 1
                    await asyncio.sleep(delay)
                    continue
                raise SpotRateLimitError(
                    f"Rate limited after {_SPOT_MAX_RETRIES} attempts."
                )

            # Server / transient errors → backoff & retry
            if is_server_error or is_grpc_non_ok or status in (408,):
                if retries_used < _SPOT_MAX_RETRIES:
                    delay = _compute_delay(attempt, resp.headers.get("Retry-After"))
                    _LOGGER.warning(
                        "SPOT %s transient/server error (HTTP %s, gRPC %s). Retrying in %.2fs (attempt %d/%d)…",
                        api_scope,
                        status,
                        grpc_status,
                        delay,
                        attempt,
                        _SPOT_MAX_RETRIES,
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
