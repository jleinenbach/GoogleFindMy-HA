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
    try:
        from bs4 import BeautifulSoup  # lazy import, optional
        return BeautifulSoup(resp.text, "html.parser").get_text()
    except Exception:
        return (resp.content or b"")[:256]


def _pick_auth_token():
    """Return (token, kind, username). kind ∈ {'spot','adm'}."""
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
    """Clear cached token so that next call reauthenticates."""
    if kind == "adm":
        set_cached_value(f"adm_token_{username}", None)
    elif kind == "spot":
        # Drop AAS to force SPOT token refresh on next call (implementation-dependent)
        set_cached_value("aas_token", None)


def spot_request(api_scope: str, payload: bytes) -> bytes:
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
    # (httpx handles HTTP/2; trailers may appear as headers for "trailers-only" responses)
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

        # Enforce HTTP/2
        with httpx.Client(http2=True, timeout=30.0) as client:
            resp = client.post(url, headers=headers, content=grpc_body)

        status = resp.status_code
        ctype = resp.headers.get("Content-Type")
        clen = len(resp.content or b"")
        _LOGGER.debug("SPOT %s: HTTP %s, ctype=%s, len=%d", api_scope, status, ctype, clen)

        # Happy path: 200 + valid frame
        if status == 200 and clen >= 5 and resp.content[0] in (0, 1):
            return GrpcParser.extract_grpc_payload(resp.content)

        # Trailer-only handling (HTTP/2)
        grpc_status = resp.headers.get("grpc-status")
        grpc_msg = resp.headers.get("grpc-message")
        if status == 200:
            if grpc_status and grpc_status != "0":
                _LOGGER.debug("SPOT %s trailers-only error: grpc-status=%s, msg=%s",
                              api_scope, grpc_status, grpc_msg)
                # UNAUTHENTICATED=16 or PERMISSION_DENIED=7 → refresh token once
                if grpc_status in ("16", "7") and attempts == 0:
                    _invalidate_token(kind, username)
                    attempts += 1
                    continue
                raise RuntimeError(f"Spot gRPC error (trailers-only): status={grpc_status}, message={grpc_msg}")
            # 200 but no grpc-status → invalid body
            snippet = (resp.content or b"")[:128]
            raise ValueError(f"Invalid GRPC payload (200 without valid frame). Snippet={snippet!r}")

        # Non-200: auth retry once
        if status in (401, 403) and attempts == 0:
            _LOGGER.debug("SPOT %s: %s, invalidating %s token and retrying", api_scope, status, kind)
            _invalidate_token(kind, username)
            attempts += 1
            continue

        # Other HTTP errors
        pretty = _beautify_text(resp)
        _LOGGER.debug("SPOT %s HTTP error body: %r", api_scope, pretty)
        raise RuntimeError(f"Spot API HTTP {status} for {api_scope}")

    raise RuntimeError("Spot request failed after retries")
