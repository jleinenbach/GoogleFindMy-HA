"""Google FMDN Backend Uploader.

Handles uploads of encrypted location reports to Google's FMDN (Find My Device Network)
backend. This module is part of the "Finder" functionality where devices contribute
location sightings to the crowdsourced network.

IMPORTANT: UPLOAD FUNCTIONALITY IS CURRENTLY DISABLED
============================================================================

The FMDN Finder upload endpoint requires **DroidGuard attestation** - a device
integrity verification system that only real Android devices can provide.

This limitation was confirmed by the GoogleFindMyTools maintainer:

    "uploading finder reports requires a valid DroidGuard report.
     If you want, you can try to trick DroidGuard, but I was not successful doing so."

    — Leon Böttger, https://github.com/leonboe1/GoogleFindMyTools/issues/19

TECHNICAL DETAILS
============================================================================

The FMDN network distinguishes between two types of operations:

1. OWNER Operations (WORKING):
   - Server: spot-pa.googleapis.com
   - Service: google.internal.spot.v1.SpotService
   - Methods: CreateBleDevice, GetEidInfoForE2eeDevices, UploadPrecomputedPublicKeyIds
   - Auth: Standard OAuth Bearer token
   - These work because they manage YOUR OWN devices

2. FINDER Operations (BLOCKED - requires DroidGuard):
   - Server: spot-pa.googleapis.com (most likely)
   - Service: google.internal.spot.v1.SpotService (high confidence)
   - Method: UploadLocationReports or similar
   - Auth: OAuth Bearer token + X-DroidGuard-Result header
   - These report sightings of OTHER PEOPLE'S devices

The DroidGuard attestation token proves that:
- The request originates from a genuine Android device
- Google Play Services is installed and unmodified
- The device passes SafetyNet/Play Integrity checks

Home Assistant cannot generate valid DroidGuard tokens because it is not an
Android device running Google Play Services.

RESEARCH CONDUCTED
============================================================================

We tested 192 endpoint combinations across:
- 4 servers: spot-pa, nearbyfinder-pa, findmydevice, find-my.googleapis.com
- 6 services: SpotService, FinderService, FindHubService, ContributorService
- 8 methods: ReportDeviceSighting, UploadLocationReports, ContributeLocationReport, etc.

All returned UNIMPLEMENTED (actually: rejected due to missing attestation).

FUTURE WORK
============================================================================

If someone successfully bypasses DroidGuard attestation, the upload code can be
re-enabled by:

1. Setting FMDN_UPLOAD_ENABLED = True
2. Adding the X-DroidGuard-Result header to _try_grpc_upload()
3. Implementing the DroidGuard token generation

See also:
- https://github.com/leonboe1/GoogleFindMyTools/issues/19
- https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn
- https://petsymposium.org/popets/2025/popets-2025-0147.pdf (FMDN security analysis)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.googlefindmy.Auth.token_cache import TokenCache

_LOGGER = logging.getLogger(__name__)

# ============================================================================
# CONFIGURATION
# ============================================================================

# Upload is DISABLED because it requires DroidGuard attestation.
# See module docstring and https://github.com/leonboe1/GoogleFindMyTools/issues/19
FMDN_UPLOAD_ENABLED = False

# Known endpoint configuration (high confidence based on research)
# These are kept for future use if DroidGuard attestation becomes available
FMDN_UPLOAD_SERVER = "spot-pa.googleapis.com"
FMDN_UPLOAD_SERVICE = "google.internal.spot.v1.SpotService"
FMDN_UPLOAD_METHOD = "UploadLocationReports"  # Most likely method name


# ============================================================================
# PUBLIC API
# ============================================================================


async def async_upload_to_google_fmdn(
    hass: HomeAssistant,
    payload: bytes,
    truncated_eid_hex: str,
) -> bool:
    """Upload encrypted location report to Google FMDN backend.

    IMPORTANT: This function is currently disabled because the FMDN Finder
    upload endpoint requires DroidGuard attestation that only real Android
    devices can provide.

    See: https://github.com/leonboe1/GoogleFindMyTools/issues/19

    Args:
        hass: Home Assistant instance
        payload: Serialized LocationReportsUpload protobuf
        truncated_eid_hex: Truncated EID (first 10 bytes) for logging

    Returns:
        True if upload succeeded, False if disabled or failed

    Raises:
        ValueError: If upload is enabled but fails
    """
    if not FMDN_UPLOAD_ENABLED:
        _LOGGER.debug(
            "FMDN Finder upload is disabled (requires DroidGuard attestation). "
            "Payload prepared: %d bytes for EID %s... "
            "See: https://github.com/leonboe1/GoogleFindMyTools/issues/19",
            len(payload),
            truncated_eid_hex[:8],
        )
        return False

    # If upload is enabled (future: DroidGuard bypass available)
    cache = await _get_cache_from_hass(hass)

    _LOGGER.info(
        "Uploading FMDN location report: %d bytes for EID %s...",
        len(payload),
        truncated_eid_hex[:8],
    )

    try:
        response = await _try_grpc_upload(
            cache=cache,
            payload=payload,
            server=FMDN_UPLOAD_SERVER,
            service=FMDN_UPLOAD_SERVICE,
            method=FMDN_UPLOAD_METHOD,
        )
        _LOGGER.info(
            "FMDN location report uploaded successfully for EID %s... (response: %d bytes)",
            truncated_eid_hex[:8],
            len(response),
        )
        return True
    except Exception as err:  # noqa: BLE001
        _LOGGER.error("Failed to upload FMDN report: %s", err, exc_info=True)
        raise ValueError(f"Upload failed: {err}") from err


def is_upload_enabled() -> bool:
    """Check if FMDN upload is currently enabled.

    Returns:
        True if upload is enabled, False otherwise
    """
    return FMDN_UPLOAD_ENABLED


def get_upload_status() -> dict[str, str]:
    """Get current upload configuration status for diagnostics.

    Returns:
        Dictionary with current configuration and status
    """
    return {
        "enabled": str(FMDN_UPLOAD_ENABLED),
        "server": FMDN_UPLOAD_SERVER,
        "service": FMDN_UPLOAD_SERVICE,
        "method": FMDN_UPLOAD_METHOD,
        "blocker": "DroidGuard attestation required",
        "reference": "https://github.com/leonboe1/GoogleFindMyTools/issues/19",
    }


# ============================================================================
# INTERNAL HELPERS
# ============================================================================


async def _get_cache_from_hass(hass: HomeAssistant) -> TokenCache:
    """Extract TokenCache from Home Assistant integration data.

    Args:
        hass: Home Assistant instance

    Returns:
        TokenCache instance for Spot API authentication

    Raises:
        RuntimeError: If GoogleFindMy integration not loaded or cache unavailable
    """
    from typing import cast  # noqa: PLC0415

    # Get GoogleFindMy integration data
    googlefindmy_data = hass.data.get("googlefindmy")
    if not googlefindmy_data:
        raise RuntimeError("GoogleFindMy integration not loaded")

    # Get first entry's cache (FMDN Finder uses primary account)
    entries = googlefindmy_data.get("entries", {})
    if not entries:
        raise RuntimeError("No GoogleFindMy entries available")

    # Extract cache from first entry (RuntimeData dataclass)
    first_entry = next(iter(entries.values()))
    cache = getattr(first_entry, "token_cache", None)

    if cache is None:
        # Fallback for dict-style access
        if isinstance(first_entry, dict):
            cache = first_entry.get("cache")

    if cache is None:
        raise RuntimeError("TokenCache not available in entry data")

    from custom_components.googlefindmy.Auth.token_cache import (  # noqa: PLC0415
        TokenCache,
    )

    return cast(TokenCache, cache)


async def _try_grpc_upload(
    *,
    cache: TokenCache,
    payload: bytes,
    server: str,
    service: str,
    method: str,
) -> bytes:
    """Attempt gRPC upload to the specified endpoint.

    NOTE: This will fail without DroidGuard attestation. The endpoint exists
    but requires the X-DroidGuard-Result header that only Android devices
    can provide.

    Args:
        cache: TokenCache for OAuth authentication
        payload: Serialized LocationReportsUpload protobuf
        server: gRPC server hostname (e.g., spot-pa.googleapis.com)
        service: Full service path (e.g., google.internal.spot.v1.SpotService)
        method: Method name (e.g., UploadLocationReports)

    Returns:
        Response bytes from server

    Raises:
        RuntimeError: If grpclib not available
        ValueError: If upload fails
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

    transport = SpotGrpcTransport(host=server)
    method_path = f"/{service}/{method}"

    try:
        # Get OAuth token (this works - it's the attestation that's missing)
        token, _token_kind, _token_user = await _pick_auth_token_async(
            prefer_adm=False, cache=cache
        )

        channel = await transport.get_channel()
        grpc_method = UnaryUnaryMethod(channel, method_path, bytes, bytes)

        # Standard metadata - missing DroidGuard attestation
        # To enable uploads, add: ("x-droidguard-result", droidguard_token)
        metadata = (("authorization", f"Bearer {token}"),)

        async with grpc_method.open(metadata=metadata, timeout=30.0) as stream:
            await stream.send_message(payload, end=True)
            reply = await stream.recv_message()

        return reply if reply else b""

    except grpclib_exceptions.GRPCError as err:
        status_name = getattr(err.status, "name", str(err.status))
        # UNIMPLEMENTED likely means missing attestation, not wrong endpoint
        raise SpotGrpcStatusError(
            f"gRPC error: {status_name} (likely missing DroidGuard attestation)"
        ) from err

    except (OSError, ConnectionError, TimeoutError) as err:
        raise ValueError(f"Connection to {server} failed: {err}") from err

    finally:
        try:
            await transport.async_close()
        except Exception:  # noqa: BLE001, S110
            pass
