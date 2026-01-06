"""Google FMDN Backend Uploader.

Handles uploads of encrypted location reports to Google's FMDN backend via Spot API.

ENDPOINT DISCOVERY STATUS:
The exact Google FMDN upload endpoint is being explored via multiple candidates.
All SpotService methods on spot-pa.googleapis.com returned UNIMPLEMENTED.
Now trying alternative servers and gRPC service paths.

Candidate approaches (in order):
1. SpotService on spot-pa.googleapis.com (all methods tried, all UNIMPLEMENTED)
2. Alternative gRPC servers: findmydevice.googleapis.com, nearbyfinder-pa.googleapis.com
3. Alternative gRPC service: google.internal.nearby.v1.FinderService

Note: Owner operations (CreateBleDevice, GetEidInfoForE2eeDevices) work on SpotService.
Finder operations (reporting sightings) may use a different service or server entirely.

See docs/FMDN_ENDPOINT_DISCOVERY.md for comprehensive research and discovery instructions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import cast

    from homeassistant.core import HomeAssistant

    from custom_components.googlefindmy.Auth.token_cache import TokenCache
else:
    from typing import cast

_LOGGER = logging.getLogger(__name__)

# Candidate gRPC method names (on various services/servers)
FMDN_METHOD_CANDIDATES = [
    "ReportDeviceSighting",
    "UploadDeviceSightings",
    "ContributeLocationReport",  # Based on FMDN spec "contribute" terminology
    "SubmitLocationReport",
    "ReportSighting",
    "UploadSighting",
    "ReportLocation",
    "UploadLocationReports",
]

# Alternative gRPC service paths to try
FMDN_SERVICE_CANDIDATES = [
    "google.internal.spot.v1.SpotService",  # Default (confirmed for owner ops)
    "google.internal.nearby.v1.FinderService",  # Potential finder-specific service
    "google.internal.fmdn.v1.FinderService",  # FMDN-specific
    "google.internal.findmydevice.v1.FinderService",  # FMD-specific
    "google.internal.findhub.v1.FindHubService",  # Find Hub (new name for FMDN)
    "google.internal.findhub.v1.ContributorService",  # Contributor-specific
]

# Alternative servers to try
FMDN_SERVER_CANDIDATES = [
    "spot-pa.googleapis.com",  # Default (confirmed for owner ops)
    "nearbyfinder-pa.googleapis.com",  # Nearby Finder
    "findmydevice.googleapis.com",  # Find My Device
    "find-my.googleapis.com",  # Find Hub (documented for lookup endpoint)
]

# Track which combination worked (persistent across calls)
_working_config: dict[str, str] | None = None

FMDN_UPLOAD_ENABLED = True  # Enabled - trying multiple candidates


async def async_upload_to_google_fmdn(
    hass: HomeAssistant,
    payload: bytes,
    truncated_eid_hex: str,
) -> bool:
    """Upload encrypted location report to Google FMDN backend.

    Args:
        hass: Home Assistant instance
        payload: Serialized LocationReportsUpload protobuf
        truncated_eid_hex: Truncated EID (first 10 bytes) for logging

    Returns:
        True if upload succeeded, False otherwise

    Raises:
        RuntimeError: If GoogleFindMy integration not available or no cache
        ValueError: If upload fails
    """
    if not FMDN_UPLOAD_ENABLED:
        _LOGGER.warning(
            "FMDN upload disabled (endpoint not yet confirmed). "
            "Payload ready: %d bytes for EID %s... "
            "Run endpoint discovery: docs/QUICK_START_SHIZUKU.md",
            len(payload),
            truncated_eid_hex[:8],
        )
        return False

    # Get cache from integration data (entry-scoped on 1.7.0-3)
    cache = await _get_cache_from_hass(hass)

    _LOGGER.info(
        "Uploading FMDN location report: %d bytes for EID %s...",
        len(payload),
        truncated_eid_hex[:8],
    )

    try:
        response = await _upload_via_spot_request(cache, payload)
        _LOGGER.info(
            "FMDN location report uploaded successfully for EID %s... (response: %d bytes)",
            truncated_eid_hex[:8],
            len(response),
        )
        _LOGGER.debug(
            "Upload success - EID=%s, payload=%d bytes, response=%d bytes",
            truncated_eid_hex,
            len(payload),
            len(response),
        )
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Failed to upload FMDN report: %s", err, exc_info=True)
        raise ValueError(f"Upload failed: {err}") from err


async def _get_cache_from_hass(hass: HomeAssistant) -> TokenCache:
    """Extract TokenCache from Home Assistant integration data.

    Args:
        hass: Home Assistant instance

    Returns:
        TokenCache instance for Spot API authentication

    Raises:
        RuntimeError: If GoogleFindMy integration not loaded or cache unavailable
    """
    # Get GoogleFindMy integration data
    googlefindmy_data = hass.data.get("googlefindmy")
    if not googlefindmy_data:
        raise RuntimeError("GoogleFindMy integration not loaded - cannot upload FMDN reports")

    # Get first entry's cache (FMDN Finder uses primary account)
    entries = googlefindmy_data.get("entries", {})
    if not entries:
        raise RuntimeError("No GoogleFindMy entries available for authentication")

    # Extract cache from first entry
    # RuntimeData is a dataclass with .token_cache attribute, not a dict
    first_entry = next(iter(entries.values()))
    cache = getattr(first_entry, "token_cache", None)

    if cache is None:
        # Fallback: try dict-style access for backwards compatibility
        if isinstance(first_entry, dict):
            cache = first_entry.get("cache")

    if cache is None:
        raise RuntimeError("TokenCache not available in GoogleFindMy entry data")

    # Import TokenCache for runtime type checking
    from custom_components.googlefindmy.Auth.token_cache import (  # noqa: PLC0415
        TokenCache,
    )

    return cast(TokenCache, cache)


async def _upload_via_spot_request(cache: TokenCache, payload: bytes) -> bytes:
    """Upload payload using gRPC over HTTP/2.

    Tries multiple combinations of:
    - Servers: spot-pa.googleapis.com, nearbyfinder-pa.googleapis.com, findmydevice.googleapis.com
    - Services: SpotService, FinderService, etc.
    - Methods: ReportDeviceSighting, UploadLocationReports, etc.

    Args:
        cache: Entry-scoped TokenCache for authentication
        payload: Serialized LocationReportsUpload protobuf

    Returns:
        Response bytes from server

    Raises:
        ValueError: If all combinations fail
    """
    global _working_config  # noqa: PLW0603

    # If we've found a working config, use it directly
    if _working_config is not None:
        _LOGGER.debug(
            "Sending FMDN upload via cached config: %s/%s on %s (%d bytes)",
            _working_config["service"],
            _working_config["method"],
            _working_config["server"],
            len(payload),
        )
        return await _try_grpc_upload(
            cache=cache,
            payload=payload,
            server=_working_config["server"],
            service=_working_config["service"],
            method=_working_config["method"],
        )

    # Try all combinations
    last_error: Exception | None = None
    tried_count = 0

    for server in FMDN_SERVER_CANDIDATES:
        for service in FMDN_SERVICE_CANDIDATES:
            for method in FMDN_METHOD_CANDIDATES:
                tried_count += 1
                _LOGGER.info(
                    "Trying FMDN upload [%d]: %s/%s on %s",
                    tried_count,
                    service.split(".")[-1],  # Short service name
                    method,
                    server,
                )

                try:
                    response = await _try_grpc_upload(
                        cache=cache,
                        payload=payload,
                        server=server,
                        service=service,
                        method=method,
                    )

                    # Success! Remember this config
                    _working_config = {
                        "server": server,
                        "service": service,
                        "method": method,
                    }
                    response_len = len(response) if response else 0
                    _LOGGER.info(
                        "FMDN upload SUCCESS via %s/%s on %s (response: %d bytes)",
                        service.split(".")[-1],
                        method,
                        server,
                        response_len,
                    )
                    return response

                except _UnimplementedError:
                    # This endpoint doesn't exist - try next
                    _LOGGER.debug(
                        "Endpoint %s/%s on %s returned UNIMPLEMENTED",
                        service.split(".")[-1],
                        method,
                        server,
                    )
                    continue

                except _ConnectionError as err:
                    # Server unreachable - skip remaining methods on this server
                    _LOGGER.debug(
                        "Server %s unreachable: %s - skipping",
                        server,
                        err,
                    )
                    last_error = err
                    break  # Skip to next server

                except Exception as err:  # noqa: BLE001
                    # Unknown error - log and continue
                    _LOGGER.debug(
                        "Endpoint %s/%s on %s failed: %s",
                        service.split(".")[-1],
                        method,
                        server,
                        err,
                    )
                    last_error = err
                    continue

    # All combinations failed
    _LOGGER.error(
        "All FMDN upload combinations failed. Tried %d combinations across %d servers.",
        tried_count,
        len(FMDN_SERVER_CANDIDATES),
    )
    raise ValueError(
        f"All FMDN upload endpoints returned UNIMPLEMENTED or failed. "
        f"Tried {tried_count} combinations. Last error: {last_error}"
    )


class _UnimplementedError(Exception):
    """Endpoint returned UNIMPLEMENTED."""


class _ConnectionError(Exception):  # noqa: A001
    """Server connection failed."""


async def _try_grpc_upload(
    *,
    cache: TokenCache,
    payload: bytes,
    server: str,
    service: str,
    method: str,
) -> bytes:
    """Try a single gRPC upload to a specific server/service/method.

    Args:
        cache: TokenCache for authentication
        payload: Protobuf payload
        server: gRPC server hostname
        service: Full service path (e.g., google.internal.spot.v1.SpotService)
        method: Method name (e.g., ReportDeviceSighting)

    Returns:
        Response bytes

    Raises:
        _UnimplementedError: If endpoint doesn't exist
        _ConnectionError: If server is unreachable
        Other exceptions: For auth or unexpected errors
    """
    from ..SpotApi.spot_grpc_transport import SpotGrpcTransport  # noqa: PLC0415
    from ..SpotApi.spot_request import (  # noqa: PLC0415
        SpotGrpcStatusError,
        _pick_auth_token_async,
    )

    try:
        import grpclib.exceptions as grpclib_exceptions  # noqa: PLC0415
        from grpclib.client import UnaryUnaryMethod  # noqa: PLC0415
    except ImportError as err:
        raise RuntimeError("grpclib not available") from err

    # Create transport for this server
    transport = SpotGrpcTransport(host=server)
    method_path = f"/{service}/{method}"

    try:
        # Get auth token
        token, _token_kind, _token_user = await _pick_auth_token_async(
            prefer_adm=False, cache=cache
        )

        # Get channel and make request
        channel = await transport.get_channel()
        grpc_method = UnaryUnaryMethod(channel, method_path, bytes, bytes)

        metadata = (("authorization", f"Bearer {token}"),)

        async with grpc_method.open(metadata=metadata, timeout=30.0) as stream:
            await stream.send_message(payload, end=True)
            reply = await stream.recv_message()

        return reply if reply else b""

    except grpclib_exceptions.GRPCError as err:
        status_name = getattr(err.status, "name", str(err.status))
        if status_name == "UNIMPLEMENTED":
            raise _UnimplementedError(f"{method_path}: UNIMPLEMENTED") from err
        raise SpotGrpcStatusError(f"gRPC error: {status_name}") from err

    except (OSError, ConnectionError, TimeoutError) as err:
        raise _ConnectionError(f"Connection to {server} failed: {err}") from err

    finally:
        # Clean up transport (properly close the channel)
        try:
            await transport.async_close()
        except Exception:  # noqa: BLE001, S110
            pass


# ============================================================================
# ENDPOINT DISCOVERY HELPERS (for development/debugging)
# ============================================================================


def get_working_config() -> dict[str, str] | None:
    """Return the config that worked (for diagnostics).

    Returns:
        Dict with server/service/method or None if not yet discovered
    """
    return _working_config


async def async_discover_fmdn_endpoints(hass: HomeAssistant) -> dict[str, str]:
    """Attempt to discover FMDN-related endpoints from existing API patterns.

    This is a debugging helper to analyze GoogleFindMy integration patterns
    and validate endpoint expectations.

    Returns:
        Dictionary of discovered endpoint patterns
    """
    discovered: dict[str, str] = {}

    # Add configured candidates
    discovered["servers"] = ", ".join(FMDN_SERVER_CANDIDATES)
    discovered["services"] = ", ".join(s.split(".")[-1] for s in FMDN_SERVICE_CANDIDATES)
    discovered["methods"] = ", ".join(FMDN_METHOD_CANDIDATES)
    discovered["total_combinations"] = str(
        len(FMDN_SERVER_CANDIDATES) * len(FMDN_SERVICE_CANDIDATES) * len(FMDN_METHOD_CANDIDATES)
    )

    if _working_config:
        discovered["working_server"] = _working_config["server"]
        discovered["working_service"] = _working_config["service"]
        discovered["working_method"] = _working_config["method"]
    else:
        discovered["working_config"] = "(not yet discovered)"

    return discovered


# ============================================================================
# ENDPOINT STATUS
# ============================================================================
"""
CURRENT STATUS:
- All SpotService methods on spot-pa.googleapis.com returned UNIMPLEMENTED
- Now trying multiple combinations:
  - Servers: spot-pa, nearbyfinder-pa, findmydevice, find-my (Find Hub)
  - Services: SpotService, FinderService, FindHubService, ContributorService
  - Methods: ReportDeviceSighting, ContributeLocationReport, etc.

Total combinations: 4 servers × 6 services × 8 methods = 192 attempts

The FMDN network (now "Find Hub") distinguishes between:
- OWNER operations: CreateBleDevice, GetEidInfoForE2eeDevices (confirmed on SpotService)
- FINDER/CONTRIBUTOR operations: Reporting sightings (endpoint unknown - trying alternatives)

See: docs/FMDN_ENDPOINT_DISCOVERY.md for network capture instructions
"""
