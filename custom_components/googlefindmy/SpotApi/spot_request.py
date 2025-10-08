# custom_components/googlefindmy/SpotApi/spot_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

import asyncio
import logging
from typing import Tuple

import httpx

from custom_components.googlefindmy.Auth.username_provider import (
    get_username,          # sync wrapper (CLI/dev)
    async_get_username,    # async-first (HA)
)
from custom_components.googlefindmy.SpotApi.grpc_parser import GrpcParser

# Sync helpers for CLI/dev usage (never call inside the HA event loop)
from custom_components.googlefindmy.Auth.spot_token_retrieval import get_spot_token  # sync API
from custom_components.googlefindmy.Auth.adm_token_retrieval import get_adm_token    # sync API

# Cache access (we use async variants in async path and sync in CLI path)
from custom_components.googlefindmy.Auth.token_cache import (
    get_cached_value,
    set_cached_value,
    async_get_cached_value,
    async_set_cached_value,
    # get_all_cached_values  # intentionally not used in async path to avoid full scans
)

_LOGGER = logging.getLogger(__name__)


def _beautify_text(resp) -> str:
    """Best-effort body-to-text for diagnostics (HTML/JSON error pages)."""
    try:
        from bs4 import BeautifulSoup  # lazy import, optional
        return BeautifulSoup(resp.text, "html.parser").get_text()
    except Exception:
        try:
            body = (resp.content or b"")[:256]
            return body.decode("utf-8", errors="ignore")
        except Exception:
            return ""


# --------------------------- Token selection (sync) ---------------------------

def _pick_auth_token(prefer_adm: bool = False) -> Tuple[str, str, str]:
    """
    Select a valid auth token (sync). Prefer SPOT unless prefer_adm=True.

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
    """Invalidate cached tokens to force re-auth on next call (scoped to the *token owner's* username)."""
    if kind == "adm":
        set_cached_value(f"adm_token_{username}", None)
    elif kind == "spot":
        # IMPORTANT: also drop the cached SPOT access token itself
        set_cached_value(f"spot_token_{username}", None)
        # Drop AAS so that the SPOT flow regenerates from its root credentials if needed
        set_cached_value("aas_token", None)


# --------------------------- Token selection (async) ---------------------------

async def _pick_auth_token_async(prefer_adm: bool = False) -> Tuple[str, str, str]:
    """
    Select a valid auth token (async). Prefer SPOT unless prefer_adm=True.

    Returns:
        (token, kind, token_owner_username)

    Async rules:
    - Use async username provider.
    - Run blocking token retrieval (SPOT/ADM) in a worker thread.
    - Do NOT perform full-cache scans in the async path (avoid heavy ops).
    """
    user = await async_get_username()
    if not user:
        raise RuntimeError("Username is not configured; cannot select auth token.")

    # Prefer SPOT unless explicitly preferring ADM
    if not prefer_adm:
        try:
            tok = await asyncio.to_thread(get_spot_token, user)
            return tok, "spot", user
        except Exception as e:
            _LOGGER.debug("Failed to get SPOT token for %s: %s; falling back to ADM", user, e)

    # Try ADM for the same user
    tok = await async_get_cached_value(f"adm_token_{user}")
    if not tok:
        try:
            tok = await asyncio.to_thread(get_adm_token, user)
        except Exception:
            tok = None
    if tok:
        return tok, "adm", user

    # No cross-account fallback in async path (would require full-cache scans)
    raise RuntimeError("No valid SPOT/ADM token available for current user")


async def _invalidate_token_async(kind: str, username: str) -> None:
    """Async invalidation of cached tokens (scoped to token owner's username)."""
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
        Do NOT call this from within Home Assistant's event loop; use
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
            pretty = _beautify_text(resp)
            _LOGGER.debug("SPOT %s HTTP error body: %r", api_scope, pretty)
            raise RuntimeError(f"Spot API HTTP {status} for {api_scope}")

    raise RuntimeError("Spot request failed after retries")


# ------------------------------ ASYNC API (HA path) ------------------------------

async def async_spot_request(api_scope: str, payload: bytes) -> bytes:
    """
    Perform a SPOT gRPC unary request over HTTP/2 (async, preferred in HA).

    Responsibilities
    ----------------
    - Enforce HTTP/2 + TE: trailers (required by gRPC).
    - Send framed request (5-byte gRPC prefix).
    - Handle server patterns as in sync version (200/data, 200/trailers-only, non-200).
    - Keep return type stable for callers: bytes or empty bytes on trailers-only/invalid 200 bodies.
    - On persistent AuthN/AuthZ failure (gRPC 16/7) after a retry, raise to avoid silent failure.
    - Never block the event loop: blocking token retrieval runs in a worker thread.

    Returns:
        Raw protobuf payload (bytes), or b"" for trailers-only/invalid 200 bodies.
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

    attempts = 0
    prefer_adm = False  # If first try with SPOT hits AuthN/AuthZ error, switch to ADM on retry.

    async with httpx.AsyncClient(http2=True, timeout=30.0) as client:
        while attempts < 2:
            token, kind, token_user = await _pick_auth_token_async(prefer_adm=prefer_adm)

            headers = {
                "User-Agent": "com.google.android.gms/244433022 grpc-java-cronet/1.69.0-SNAPSHOT",
                "Content-Type": "application/grpc",
                "Te": "trailers",  # required by gRPC over HTTP/2
                "Authorization": "Bearer " + token,
                "Grpc-Accept-Encoding": "gzip",
            }

            try:
                resp = await client.post(url, headers=headers, content=grpc_body)
            except httpx.TimeoutException:
                # Timeouts: do not mutate token; let caller decide to retry upstream if needed.
                if attempts == 0:
                    _LOGGER.warning("SPOT %s: request timed out; retrying once…", api_scope)
                    attempts += 1
                    continue
                raise RuntimeError("SPOT request timed out after retry")
            except httpx.TransportError as e:
                # Network errors: retry once
                if attempts == 0:
                    _LOGGER.warning("SPOT %s: transport error (%s); retrying once…", api_scope, e)
                    attempts += 1
                    continue
                raise RuntimeError(f"SPOT transport error after retry: {e}")

            status = resp.status_code
            ctype = resp.headers.get("Content-Type")
            content = resp.content or b""
            clen = len(content)
            _LOGGER.debug("SPOT %s: HTTP %s, ctype=%s, len=%d", api_scope, status, ctype, clen)

            # (1) Happy path: 200 + valid gRPC message frame
            if status == 200 and clen >= 5 and content[0] in (0, 1):
                return GrpcParser.extract_grpc_payload(content)

            # (2) Trailer-only / invalid-body handling (HTTP 200 without a usable frame)
            grpc_status = resp.headers.get("grpc-status")
            grpc_msg = resp.headers.get("grpc-message")

            if status == 200:
                if grpc_status and grpc_status != "0":
                    code_name = {"16": "UNAUTHENTICATED", "7": "PERMISSION_DENIED"}.get(grpc_status, "NON_OK")

                    if grpc_status in ("16", "7"):
                        _LOGGER.error(
                            "SPOT %s trailers-only error: grpc-status=%s (%s), msg=%s",
                            api_scope, grpc_status, code_name, grpc_msg
                        )
                        if attempts == 0:
                            await _invalidate_token_async(kind, token_user)
                            attempts += 1
                            prefer_adm = (kind == "spot")
                            continue
                        raise RuntimeError(f"Spot API authentication failed after retry ({code_name})")

                    _LOGGER.warning(
                        "SPOT %s trailers-only non-OK: grpc-status=%s (%s), msg=%s",
                        api_scope, grpc_status, code_name, grpc_msg
                    )
                    return b""

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

                snippet = content[:128]
                _LOGGER.debug("SPOT %s invalid 200 body (no frame). Snippet=%r", api_scope, snippet)
                return b""

            # (3) Non-200 HTTP responses (retry once on common auth HTTP codes)
            if status in (401, 403) and attempts == 0:
                _LOGGER.debug(
                    "SPOT %s: %s, invalidating %s token for %s and retrying",
                    api_scope, status, kind, token_user
                )
                await _invalidate_token_async(kind, token_user)
                attempts += 1
                prefer_adm = (kind == "spot")
                continue

            # Other HTTP errors: include a brief body for debugging and raise.
            try:
                pretty = content.decode("utf-8", errors="ignore")
                try:
                    from bs4 import BeautifulSoup  # optional prettifier
                    pretty = BeautifulSoup(pretty, "html.parser").get_text()
                except Exception:
                    pass
            except Exception:
                pretty = ""
            _LOGGER.debug("SPOT %s HTTP error body: %r", api_scope, pretty)
            raise RuntimeError(f"Spot API HTTP {status} for {api_scope}")

    raise RuntimeError("Spot request failed after retries")
