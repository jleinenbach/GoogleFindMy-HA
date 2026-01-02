"""Google FMDN Backend Uploader.

Handles uploads of encrypted location reports to Google's FMDN backend via Spot API.

ENDPOINT DISCOVERY STATUS:
The exact Google FMDN upload endpoint needs confirmation via Shizuku/logcat analysis.

Expected endpoint (95% probability based on existing Spot API patterns):
- https://spot-pa.googleapis.com/google.internal.spot.v1.SpotService/UploadLocationReports

This matches the pattern of 3 confirmed working endpoints:
- CreateBleDevice
- GetEidInfoForE2eeDevices
- UploadPrecomputedPublicKeyIds

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

# TODO: Confirm via Shizuku/logcat endpoint discovery
# See: docs/QUICK_START_SHIZUKU.md for 15-minute discovery guide
# See: docs/FMDN_ENDPOINT_DISCOVERY.md for comprehensive analysis

FMDN_UPLOAD_ENDPOINT = "UploadLocationReports"  # gRPC method name (highest probability)
FMDN_UPLOAD_ENABLED = False  # Enable after endpoint confirmation


async def async_upload_to_google_fmdn(
    hass: HomeAssistant,
    payload: bytes,
    truncated_eid_hex: str,
) -> None:
    """Upload encrypted location report to Google FMDN backend.

    Args:
        hass: Home Assistant instance
        payload: Serialized LocationReportsUpload protobuf
        truncated_eid_hex: Truncated EID (first 10 bytes) for logging

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
        return

    # Get cache from integration data (entry-scoped on 1.7.0-3)
    cache = await _get_cache_from_hass(hass)

    _LOGGER.info(
        "Uploading FMDN location report: %d bytes for EID %s...",
        len(payload),
        truncated_eid_hex[:8],
    )

    try:
        await _upload_via_spot_request(cache, payload)
        _LOGGER.info("FMDN location report uploaded successfully")
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
    first_entry = next(iter(entries.values()))
    cache = first_entry.get("cache")

    if cache is None:
        raise RuntimeError("TokenCache not available in GoogleFindMy entry data")

    # Import TokenCache for runtime type checking
    from custom_components.googlefindmy.Auth.token_cache import TokenCache  # noqa: PLC0415

    return cast(TokenCache, cache)


async def _upload_via_spot_request(cache: TokenCache, payload: bytes) -> None:
    """Upload payload using Spot API (gRPC over HTTP/2).

    Implementation follows the pattern of existing Spot API calls on 1.7.0-3:
    - Uses async_spot_request with cache parameter (required on 1.7.0-3)
    - gRPC method: /google.internal.spot.v1.SpotService/UploadLocationReports
    - Authentication via SPOT/ADM token (automatic refresh)
    - HTTP/2 with grpclib transport

    Args:
        cache: Entry-scoped TokenCache for authentication
        payload: Serialized LocationReportsUpload protobuf

    Raises:
        NotImplementedError: Until endpoint is confirmed via Shizuku discovery
    """
    # IMPLEMENTATION NOTE FOR ENDPOINT CONFIRMATION:
    #
    # After Shizuku endpoint discovery confirms "UploadLocationReports", uncomment:
    #
    # from ..SpotApi.spot_request import async_spot_request
    #
    # response = await async_spot_request(
    #     api_scope=FMDN_UPLOAD_ENDPOINT,  # "UploadLocationReports"
    #     payload=payload,  # Already serialized LocationReportsUpload protobuf
    #     cache=cache,  # REQUIRED on 1.7.0-3 for entry-scoped auth
    # )
    #
    # Then set FMDN_UPLOAD_ENABLED = True above.
    #
    # The async_spot_request function will:
    # 1. Construct full gRPC path: /google.internal.spot.v1.SpotService/UploadLocationReports
    # 2. Use grpclib with HTTP/2 transport
    # 3. Auto-refresh SPOT/ADM tokens using the provided cache
    # 4. Retry transient failures (network, rate limit, etc.)

    raise NotImplementedError(
        "FMDN upload endpoint not yet confirmed. "
        "Run endpoint discovery via Shizuku (see docs/QUICK_START_SHIZUKU.md) "
        "or automated script (tools/analyze_fmdn_logs.py --live). "
        "Expected endpoint: google.internal.spot.v1.SpotService/UploadLocationReports "
        "Then uncomment implementation above and set FMDN_UPLOAD_ENABLED = True."
    )


# ============================================================================
# ENDPOINT DISCOVERY HELPERS (for development/debugging)
# ============================================================================


async def async_discover_fmdn_endpoints(hass: HomeAssistant) -> dict[str, str]:
    """Attempt to discover FMDN-related endpoints from existing API patterns.

    This is a debugging helper to analyze GoogleFindMy integration patterns
    and validate endpoint expectations.

    Returns:
        Dictionary of discovered endpoint patterns
    """
    discovered = {}

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
    discovered["expected_fmdn_method"] = "UploadLocationReports"

    return discovered


# ============================================================================
# SHIZUKU ENDPOINT DISCOVERY INSTRUCTIONS
# ============================================================================
"""
QUICK START (15-30 minutes):

1. Setup Shizuku wireless debugging
   - Follow: https://shizuku.rikka.app/guide/setup/
   - Enable Developer Options → Wireless Debugging

2. Run automated analysis:
   ```bash
   cd /path/to/GoogleFindMy-HA
   python3 tools/analyze_fmdn_logs.py --live
   ```

3. Trigger FMDN upload:
   - Walk near FMDN beacon (Pixel Buds, Moto Tag, ESP32 tracker)
   - Wait 2-5 minutes for Play Services background upload
   - Script will detect and report endpoint

4. Expected results:
   HIGH confidence: google.internal.spot.v1.SpotService/UploadLocationReports
   Alternative:     /v1/uploadLocationReports (REST-style)

5. After confirmation:
   - Uncomment implementation in _upload_via_spot_request()
   - Set FMDN_UPLOAD_ENABLED = True
   - Restart Home Assistant
   - Monitor logs: grep -i "fmdn" home-assistant.log

See comprehensive guide: docs/FMDN_ENDPOINT_DISCOVERY.md
See quick start: docs/QUICK_START_SHIZUKU.md
"""
