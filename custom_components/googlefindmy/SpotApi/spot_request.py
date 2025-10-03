#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import logging
import httpx

from custom_components.googlefindmy.Auth.spot_token_retrieval import get_spot_token
from custom_components.googlefindmy.Auth.username_provider import get_username
from custom_components.googlefindmy.SpotApi.grpc_parser import GrpcParser
from custom_components.googlefindmy.Auth.adm_token_retrieval import get_adm_token
from custom_components.googlefindmy.Auth.token_cache import set_cached_value, get_cached_value, get_all_cached_values

_LOGGER = logging.getLogger(__name__)


def _beautify_text(resp):
    """Best-effort body-to-text for diagnostics (HTML/JSON error pages)."""
    try:
        from bs4 import BeautifulSoup  # lazy import, optional
        return BeautifulSoup(resp.text, "html.parser").get_text()
    except Exception:
        return (resp.content or b"")[:256]


def _pick_auth_token():
    """Select a valid auth token. Prefer SPOT, fall back to ADM; return (token, kind, username)."""
    username = get_username()
    # Prefer SPOT token
    try:
        tok = get_spot_token(username)
        return tok, "spot", username
    except Exception as e:
        _LOGGER.debug("Failed to get SPOT token: %s; falling back to ADM", e)
        # Try cached ADM token first (per-user), then any cached adm_token_* (multi-account)
        tok = get_cached_value(f"adm_token_{username}")
        if not tok:
            for key, value in (get_all_cached_values() or {}).items():
                if key.startswith("adm_token_") and "@" in key:
                    tok = value
                    username = key.replace("adm_token_", "")
                    _LOGGER.debug("Using ADM token from cache for %s", username)
                    break
        if not tok:
            tok = get_adm_token(username)
        return tok, "adm", username


def _invalidate_token(kind: str, username: str):
    """Invalidate cached tokens to force re-auth on next call."""
    if kind == "adm":
        set_cached_value(f"adm_token_{username}", None)
    elif kind == "spot":
        # Drop AAS to force SPOT token refresh on next call (implementation-dependent)
        set_cached_value("aas_token", None)


def spot_request(api_scope: str, payload: bytes) -> bytes:
    """
    Perform a SPOT gRPC unary request over HTTP/2.

    ### Responsibilities of this function
    - Enforce HTTP/2 + TE: trailers (required by gRPC).
    - Send framed request (5-byte gRPC prefix).
    - Handle three server patterns:
      (1) 200 + data frame(s)  -> extract and return the uncompressed payload.
      (2) 200 + trailers-only  -> no DATA frames; read grpc-status/message and log appropriately.
      (3) Non-200 HTTP         -> log diagnostics and raise.
    - Keep return type stable for callers: bytes or empty bytes on trailers-only/invalid 200 bodies.
    """
    url = "https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/" + api_scope

    # Ensure HTTP/2 support is available (httpx[http2] -> h2)
    try:
        import h2  # noqa: F401
    except Exception as e:
        raise RuntimeError(
            "HTTP/2 support is required for SPOT gRPC. Please install the HTTP/2 extra: pip install 'httpx[http2]'"
        ) from e

    # Build framed payload early
    grpc_body = GrpcParser.construct_grpc(payload)

    # httpx is necessary because requests does not support the Te header
    # HTTP/2 + TE: trailers are required by gRPC
    # (httpx handles HTTP/2; trailers may appear as headers for 'trailers-only' responses)
    attempts = 0
    while attempts < 2:
        token, kind, username = _pick_auth_token()

        headers = {
            "User-Agent": "com.google.android.gms/244433022 grpc-java-cronet/1.69.0-SNAPSHOT",
            "Content-Type": "application/grpc",
            "Te": "trailers",  # required by gRPC over HTTP/2
            "Authorization": "Bearer " + token,
            "Grpc-Accept-Encoding": "gzip",
        }

        # --- HTTP/2 request (gRPC requires trailers over HTTP/2) ---
        with httpx.Client(http2=True, timeout=30.0) as client:
            resp = client.post(url, headers=headers, content=grpc_body)

        status = resp.status_code
        ctype = resp.headers.get("Content-Type")
        clen = len(resp.content or b"")
        _LOGGER.debug("SPOT %s: HTTP %s, ctype=%s, len=%d", api_scope, status, ctype, clen)

        # --- (1) Happy path: 200 + valid gRPC message frame ---
        if status == 200 and clen >= 5 and resp.content[0] in (0, 1):
            return GrpcParser.extract_grpc_payload(resp.content)

        # --- (2) Trailer-only / invalid-body handling (HTTP 200 without a usable frame) ---
        grpc_status = resp.headers.get("grpc-status")
        grpc_msg = resp.headers.get("grpc-message")

        if status == 200:
            # 2a) We received explicit gRPC status in trailers (no data frames)
            if grpc_status and grpc_status != "0":
                # Map well-known codes for clarity
                code_name = {"16": "UNAUTHENTICATED", "7": "PERMISSION_DENIED"}.get(grpc_status, "NON_OK")

                if grpc_status in ("16", "7"):
                    # AuthN/AuthZ failures must be logged as ERROR; retry once with token invalidation.
                    _LOGGER.error(
                        "SPOT %s trailers-only error: grpc-status=%s (%s), msg=%s",
                        api_scope, grpc_status, code_name, grpc_msg
                    )
                    if attempts == 0:
                        _invalidate_token(kind, username)
                        attempts += 1
                        continue
                else:
                    # Other non-OK gRPC statuses: warn and keep bytes contract.
                    _LOGGER.warning(
                        "SPOT %s trailers-only non-OK: grpc-status=%s (%s), msg=%s",
                        api_scope, grpc_status, code_name, grpc_msg
                    )
                return b""

            # 2b) No grpc-status, but body is empty or not a valid frame: treat as ambiguous trailers-only / protocol quirk.
            if (ctype or "").startswith("application/grpc") and clen == 0:
                # For critical RPCs (owner/e2ee info), an empty body prevents decryption -> log as ERROR.
                critical_methods = {"GetEidInfoForE2eeDevices"}
                if api_scope in critical_methods:
                    _LOGGER.error(
                        "SPOT %s: HTTP 200 with empty gRPC body (likely trailers-only or missing response). "
                        "This will prevent E2EE key retrieval and decryption.",
                        api_scope,
                    )
                else:
                    # If this ever still leads to a successful workflow upstream, it will be visible in later logs.
                    _LOGGER.warning(
                        "SPOT %s: HTTP 200 with empty gRPC body (possible trailers-only OK or missing response).",
                        api_scope,
                    )
                return b""

            # 2c) 200 but no usable frame; log a small snippet for diagnostics and keep bytes contract.
            snippet = (resp.content or b"")[:128]
            _LOGGER.debug("SPOT %s invalid 200 body (no frame). Snippet=%r", api_scope, snippet)
            return b""

        # --- (3) Non-200 HTTP responses (retry on common auth HTTP codes) ---
        if status in (401, 403) and attempts == 0:
            _LOGGER.debug("SPOT %s: %s, invalidating %s token and retrying", api_scope, status, kind)
            _invalidate_token(kind, username)
            attempts += 1
            continue

        # Other HTTP errors: include a brief body for debugging and raise.
        pretty = _beautify_text(resp)
        _LOGGER.debug("SPOT %s HTTP error body: %r", api_scope, pretty)
        raise RuntimeError(f"Spot API HTTP {status} for {api_scope}")

    raise RuntimeError("Spot request failed after retries")
