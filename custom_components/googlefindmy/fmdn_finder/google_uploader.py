"""Google FMDN Backend Uploader.

Handles uploads of encrypted location reports to Google's FMDN backend via Spot API.

ENDPOINT DISCOVERY STATUS:
The exact Google FMDN upload endpoint is being explored via multiple candidates.
Initial candidate `UploadLocationReports` returned UNIMPLEMENTED - trying alternatives.

Candidate endpoints (SpotService pattern):
- ReportDeviceSighting - Finder reports seeing a device
- SubmitLocationReport - Alternative naming
- UploadSighting - Simplified variant
- ReportLocation - Generic location report

Note: Owner operations (CreateBleDevice, GetEidInfoForE2eeDevices) work on SpotService.
Finder operations (reporting sightings) may use a different service or endpoint.

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

# Candidate endpoints to try (in priority order based on FMDN terminology)
# The FMDN spec calls these operations "sightings" when a finder reports seeing a device
FMDN_UPLOAD_CANDIDATES = [
    "ReportDeviceSighting",  # FMDN terminology - finder "sights" a device
    "UploadDeviceSightings",  # Plural variant
    "SubmitLocationReport",  # Alternative naming
    "ReportSighting",  # Simplified
    "UploadSighting",  # Simplified upload
    "ReportLocation",  # Generic
    "UploadLocationReports",  # Original guess (returned UNIMPLEMENTED)
]

# Track which endpoint worked (persistent across calls)
_working_endpoint: str | None = None

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
    """Upload payload using Spot API (gRPC over HTTP/2).

    Implementation follows the pattern of existing Spot API calls on 1.7.0-3:
    - Uses async_spot_request with cache parameter (required on 1.7.0-3)
    - Tries multiple candidate endpoints until one works
    - Authentication via SPOT/ADM token (automatic refresh)
    - HTTP/2 with grpclib transport

    Args:
        cache: Entry-scoped TokenCache for authentication
        payload: Serialized LocationReportsUpload protobuf

    Returns:
        Response bytes from server

    Raises:
        ValueError: If all endpoints fail
    """
    global _working_endpoint  # noqa: PLW0603

    from ..SpotApi.spot_request import (  # noqa: PLC0415
        SpotGrpcStatusError,
        async_spot_request,
    )

    # If we've found a working endpoint, use it directly
    if _working_endpoint is not None:
        _LOGGER.debug(
            "Sending FMDN upload via cached endpoint SpotService/%s (%d bytes)",
            _working_endpoint,
            len(payload),
        )
        response = await async_spot_request(
            api_scope=_working_endpoint,
            payload=payload,
            cache=cache,
        )
        return response if response else b""

    # Try each candidate endpoint
    last_error: Exception | None = None
    for endpoint in FMDN_UPLOAD_CANDIDATES:
        _LOGGER.info(
            "Trying FMDN upload endpoint: SpotService/%s (%d bytes)",
            endpoint,
            len(payload),
        )

        try:
            response = await async_spot_request(
                api_scope=endpoint,
                payload=payload,
                cache=cache,
            )

            # Success! Remember this endpoint
            _working_endpoint = endpoint
            response_len = len(response) if response else 0
            _LOGGER.info(
                "FMDN upload SUCCESS via SpotService/%s (response: %d bytes)",
                endpoint,
                response_len,
            )
            return response if response else b""

        except SpotGrpcStatusError as err:
            # UNIMPLEMENTED means this endpoint doesn't exist - try next
            err_str = str(err).upper()
            if "UNIMPLEMENTED" in err_str:
                _LOGGER.debug(
                    "Endpoint SpotService/%s returned UNIMPLEMENTED - trying next",
                    endpoint,
                )
                last_error = err
                continue
            # Other gRPC errors (auth, network) - don't try more endpoints
            _LOGGER.warning(
                "Endpoint SpotService/%s failed with gRPC error: %s",
                endpoint,
                err,
            )
            raise

        except Exception as err:  # noqa: BLE001
            # Unknown error - log and try next endpoint
            _LOGGER.debug(
                "Endpoint SpotService/%s failed: %s - trying next",
                endpoint,
                err,
            )
            last_error = err
            continue

    # All endpoints failed
    _LOGGER.error(
        "All FMDN upload endpoints failed. Tried: %s",
        ", ".join(FMDN_UPLOAD_CANDIDATES),
    )
    raise ValueError(
        f"All FMDN upload endpoints returned UNIMPLEMENTED. "
        f"The FMDN Finder upload API may not be available on SpotService. "
        f"Last error: {last_error}"
    )


# ============================================================================
# ENDPOINT DISCOVERY HELPERS (for development/debugging)
# ============================================================================


def get_working_endpoint() -> str | None:
    """Return the endpoint that worked (for diagnostics).

    Returns:
        The working endpoint name or None if not yet discovered
    """
    return _working_endpoint


async def async_discover_fmdn_endpoints(hass: HomeAssistant) -> dict[str, str]:
    """Attempt to discover FMDN-related endpoints from existing API patterns.

    This is a debugging helper to analyze GoogleFindMy integration patterns
    and validate endpoint expectations.

    Returns:
        Dictionary of discovered endpoint patterns
    """
    discovered: dict[str, str] = {}

    # Check SpotApi patterns
    try:
        from ..SpotApi import spot_request  # noqa: PLC0415

        for attr in dir(spot_request):
            if "ENDPOINT" in attr.upper() or "URL" in attr.upper() or "METHOD" in attr.upper():
                value = getattr(spot_request, attr, None)
                if isinstance(value, str):
                    discovered[f"spot_request.{attr}"] = value
                    _LOGGER.debug("Discovered Spot API pattern: %s = %s", attr, value)

    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to introspect spot_request: %s", err)

    # Add known confirmed patterns
    discovered["confirmed_pattern"] = "google.internal.spot.v1.SpotService/{MethodName}"
    discovered["confirmed_examples"] = "CreateBleDevice, GetEidInfoForE2eeDevices, UploadPrecomputedPublicKeyIds"
    discovered["candidate_fmdn_methods"] = ", ".join(FMDN_UPLOAD_CANDIDATES)
    discovered["working_endpoint"] = _working_endpoint or "(not yet discovered)"

    return discovered


# ============================================================================
# ENDPOINT STATUS
# ============================================================================
"""
CURRENT STATUS:
- Initial candidate `UploadLocationReports` returned UNIMPLEMENTED
- Now trying multiple candidates based on FMDN terminology:
  - ReportDeviceSighting (FMDN spec uses "sighting" terminology)
  - UploadDeviceSightings
  - SubmitLocationReport
  - ReportSighting
  - UploadSighting
  - ReportLocation

ALTERNATIVE APPROACHES (if all SpotService methods fail):
1. Different gRPC service (not SpotService)
2. REST API instead of gRPC
3. Different server (findmydevice.googleapis.com instead of spot-pa.googleapis.com)

The FMDN network distinguishes between:
- OWNER operations: CreateBleDevice, GetEidInfoForE2eeDevices (confirmed on SpotService)
- FINDER operations: Reporting sightings (endpoint unknown)

Finder operations may use a completely different API surface.

See: docs/FMDN_ENDPOINT_DISCOVERY.md for network capture instructions
"""
