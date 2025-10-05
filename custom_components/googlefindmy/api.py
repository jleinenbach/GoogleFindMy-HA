"""API wrapper for Google Find My Device."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .Auth.token_cache import set_memory_cache
from .Auth.username_provider import username_string
from .NovaApi.ListDevices.nbe_list_devices import (
    request_device_list,
    async_request_device_list,
)
from .NovaApi.ExecuteAction.LocateTracker.location_request import (
    get_location_data_for_device,
)
from .NovaApi.ExecuteAction.PlaySound.start_sound_request import start_sound_request
from .NovaApi.nova_request import nova_request
from .NovaApi.scopes import NOVA_ACTION_API_SCOPE
from .ProtoDecoders.decoder import (
    parse_device_list_protobuf,
    get_canonic_ids,
    get_devices_with_location,
)

_LOGGER = logging.getLogger(__name__)


def _infer_can_ring_slot(device: Dict[str, Any]) -> Optional[bool]:
    """Best-effort normalization of a 'can ring' capability from various shapes.

    We try multiple layouts because upstream protobuf decoders may evolve:
    - device["can_ring"] -> bool
    - device["canRing"] -> bool
    - device["capabilities"] -> list[str] / dict[str,bool]
    Returns:
        True/False when we could infer a verdict, otherwise None.
    """
    try:
        if "can_ring" in device:
            return bool(device.get("can_ring"))
        if "canRing" in device:
            return bool(device.get("canRing"))

        caps = device.get("capabilities")
        if isinstance(caps, (list, set, tuple)):
            lowered = {str(x).lower() for x in caps}
            return ("ring" in lowered) or ("play_sound" in lowered)
        if isinstance(caps, dict):
            lowered = {str(k).lower(): v for k, v in caps.items()}
            return bool(lowered.get("ring")) or bool(lowered.get("play_sound"))
    except Exception:  # defensive: never raise from a heuristic
        return None
    return None


def _build_can_ring_index(parsed_device_list: Any) -> Dict[str, bool]:
    """Build a mapping canonical_id -> can_ring (where determinable)."""
    index: Dict[str, bool] = {}
    try:
        devices = get_devices_with_location(parsed_device_list)  # may include caps even without location
    except Exception:
        devices = []

    for d in devices or []:
        # Common id fields: canonicalId / id / deviceId
        cid = d.get("canonicalId") or d.get("id") or d.get("device_id")
        if not cid:
            continue
        verdict = _infer_can_ring_slot(d)
        if isinstance(verdict, bool):
            index[str(cid)] = verdict
    return index


class GoogleFindMyAPI:
    """API wrapper for Google Find My Device."""

    def __init__(
        self,
        oauth_token: Optional[str] = None,
        google_email: Optional[str] = None,
        secrets_data: Optional[dict] = None,
    ) -> None:
        """Initialize the API wrapper.

        The API reads credentials from our in-memory token cache. We seed that cache from either:
        - A full secrets bundle (GoogleFindMyTools), or
        - Individual OAuth token + Google email.
        """
        self.secrets_data = secrets_data
        self.oauth_token = oauth_token
        self.google_email = google_email

        if secrets_data:
            self._initialize_from_secrets(secrets_data)
        else:
            # Individual tokens path: set a minimal in-memory cache
            cache_data = {
                "oauth_token": oauth_token,
                username_string: google_email,
            }
            set_memory_cache(cache_data)

    # ---------------------------------------------------------------------
    # Init helpers
    # ---------------------------------------------------------------------
    def _initialize_from_secrets(self, secrets: dict) -> None:
        """Initialize from secrets.json (GoogleFindMyTools) into memory cache.

        NOTE: No blocking I/O here; we only seed memory. Persistent persistence
        is handled elsewhere asynchronously by the integration.
        """
        enhanced = dict(secrets)
        self.google_email = secrets.get("username", secrets.get("Email"))
        if self.google_email:
            enhanced[username_string] = self.google_email

        set_memory_cache(enhanced)
        _LOGGER.debug("Seeded in-memory token cache with %d keys from secrets bundle", len(enhanced))

    # ---------------------------------------------------------------------
    # Device enumeration
    # ---------------------------------------------------------------------
    def get_basic_device_list(self) -> List[Dict[str, Any]]:
        """Return a lightweight list of devices (id/name), optionally with 'can_ring'."""
        try:
            result_hex = request_device_list()
            parsed = parse_device_list_protobuf(result_hex)
            canonic_ids = get_canonic_ids(parsed)
            # Try to enrich with capability flags
            can_ring_index = _build_can_ring_index(parsed)

            devices: List[Dict[str, Any]] = []
            for device_name, canonic_id in canonic_ids:
                item = {
                    "name": device_name,
                    "id": canonic_id,
                    "device_id": canonic_id,
                }
                if canonic_id in can_ring_index:
                    item["can_ring"] = bool(can_ring_index[canonic_id])
                devices.append(item)
            return devices
        except Exception as err:
            _LOGGER.debug("Failed to get basic device list: %s", err)
            raise

    async def async_get_basic_device_list(self, username: Optional[str] = None) -> List[Dict[str, Any]]:
        """Async variant of the lightweight device list, used by config/options flow."""
        try:
            if not username and self.secrets_data:
                username = self.secrets_data.get("googleHomeUsername") or self.secrets_data.get("google_email")

            result_hex = await async_request_device_list(username)
            parsed = parse_device_list_protobuf(result_hex)
            canonic_ids = get_canonic_ids(parsed)
            can_ring_index = _build_can_ring_index(parsed)

            devices: List[Dict[str, Any]] = []
            for device_name, canonic_id in canonic_ids:
                item = {
                    "name": device_name,
                    "id": canonic_id,
                    "device_id": canonic_id,
                }
                if canonic_id in can_ring_index:
                    item["can_ring"] = bool(can_ring_index[canonic_id])
                devices.append(item)
            return devices
        except Exception as err:
            _LOGGER.error("Failed to get basic device list (async): %s", err)
            # Graceful degradation (empty list keeps UI responsive)
            return []

    def get_devices(self) -> List[Dict[str, Any]]:
        """Return devices with basic info only (no up-front location fetch)."""
        try:
            _LOGGER.info("API v3.0: Enumerating devices (basic info only)")
            result_hex = request_device_list()
            parsed = parse_device_list_protobuf(result_hex)
            canonic_ids = get_canonic_ids(parsed)
            can_ring_index = _build_can_ring_index(parsed)

            devices: List[Dict[str, Any]] = []
            for device_name, canonic_id in canonic_ids:
                device_info = {
                    "name": device_name,
                    "id": canonic_id,
                    "device_id": canonic_id,
                    "latitude": None,
                    "longitude": None,
                    "altitude": None,
                    "accuracy": None,
                    "last_seen": None,
                    "status": "No location data (requires individual request)",
                    "is_own_report": None,
                    "semantic_name": None,
                    "battery_level": None,
                }
                if canonic_id in can_ring_index:
                    device_info["can_ring"] = bool(can_ring_index[canonic_id])
                devices.append(device_info)

            _LOGGER.info("API v3.0: Returning %d devices (basic)", len(devices))
            return devices
        except Exception as err:
            _LOGGER.debug("Failed to get devices: %s", err)
            raise

    # ---------------------------------------------------------------------
    # Location
    # ---------------------------------------------------------------------
    def get_device_location(self, device_id: str, device_name: str) -> Dict[str, Any]:
        """Sync wrapper for a per-device async location request.

        IMPORTANT: This method is intended to be executed in a worker thread
        (via hass.async_add_executor_job). It must not run in the main loop.
        """
        try:
            _LOGGER.info("API v3.0: Requesting location for %s (%s)", device_name, device_id)
            # Execute coroutine in this thread to avoid 'never awaited'
            return asyncio.run(self.async_get_device_location(device_id, device_name))
        except RuntimeError as loop_err:
            # Called from an active event loop (incorrect usage)
            _LOGGER.error(
                "get_device_location() was called from an active event loop. "
                "Use async_get_device_location() instead. Error: %s",
                loop_err,
            )
            return {}
        except Exception as err:
            _LOGGER.debug("Failed to get location for %s (%s): %s", device_name, device_id, err)
            return {}

    async def async_get_device_location(self, device_id: str, device_name: str) -> Dict[str, Any]:
        """Async, HA-compatible location request for a single device."""
        try:
            _LOGGER.info("API v3.0 Async: Requesting location for %s (%s)", device_name, device_id)
            location_data = await get_location_data_for_device(device_id, device_name)
            if location_data and len(location_data) > 0:
                _LOGGER.info("API v3.0 Async: Received %d location record(s) for %s", len(location_data), device_name)
                return location_data[0]
            _LOGGER.warning("API v3.0 Async: No location data for %s", device_name)
            return {}
        except Exception as err:
            _LOGGER.debug("Failed to get async location for %s (%s): %s", device_name, device_id, err)
            return {}

    def locate_device(self, device_id: str) -> Dict[str, Any]:
        """Compatibility sync entrypoint for location (uses sync wrapper)."""
        try:
            # Without a quick name map here, reuse device_id as a neutral name
            return self.get_device_location(device_id, device_id)
        except Exception as err:
            _LOGGER.debug("Failed to locate device %s: %s", device_id, err)
            raise

    # ---------------------------------------------------------------------
    # Play Sound / Push readiness
    # ---------------------------------------------------------------------
    def is_push_ready(self) -> bool:
        """Return True if the push transport (FCM) is initialized and has a usable token."""
        try:
            from .Auth.fcm_receiver_ha import FcmReceiverHA  # lazy import
        except Exception as err:
            _LOGGER.debug("FCM receiver import failed: %s", err)
            return False

        try:
            fcm = FcmReceiverHA()
            if not getattr(fcm, "credentials", None):
                return False
            token = fcm.get_fcm_token()
            if not token or not isinstance(token, str) or len(token) < 10:
                return False
            return True
        except Exception as err:
            _LOGGER.debug("FCM readiness check failed: %s", err)
            return False

    @property
    def push_ready(self) -> bool:
        """Back-compat property variant of is_push_ready()."""
        return self.is_push_ready()

    def can_play_sound(self, device_id: str) -> Optional[bool]:
        """Return a verdict whether 'Play Sound' is supported for this device.

        Strategy:
        - If push is not ready -> False.
        - Try to infer device capability from a fresh device list -> True/False when known.
        - If we cannot tell -> return None (let the caller decide optimistically).
        """
        # Quick gate on push readiness
        if not self.is_push_ready():
            return False

        # Best-effort capability probe (lightweight list; may still be network-bound)
        try:
            result_hex = request_device_list()
            parsed = parse_device_list_protobuf(result_hex)
            cap_index = _build_can_ring_index(parsed)
            if device_id in cap_index:
                return bool(cap_index[device_id])
        except Exception as err:
            _LOGGER.debug("Capability probe for device %s failed: %s", device_id, err)

        # Unknown -> let Coordinator fall back optimistically
        return None

    def play_sound(self, device_id: str) -> bool:
        """Send a 'Play Sound' command to a device.

        Returns:
            True on successful submission to the Google Action API (HTTP 200),
            False otherwise.
        """
        try:
            from .Auth.fcm_receiver_ha import FcmReceiverHA  # lazy import
        except Exception as err:
            _LOGGER.error("Cannot play sound: FCM receiver import failed: %s", err)
            return False

        try:
            fcm_receiver = FcmReceiverHA()
            if not fcm_receiver.credentials:
                _LOGGER.error("Cannot play sound: FCM receiver credentials missing")
                return False

            fcm_token = fcm_receiver.get_fcm_token()
            if not fcm_token:
                _LOGGER.error("Cannot play sound: FCM token is not available")
                return False

            # Do NOT log token contents; emit only coarse diagnostics.
            _LOGGER.info("Sending Play Sound to %s (token length=%s)", device_id, len(fcm_token))

            # Build payload and send
            hex_payload = start_sound_request(device_id, fcm_token)
            _LOGGER.debug("Sound request payload length: %s chars", len(hex_payload))

            result = nova_request(NOVA_ACTION_API_SCOPE, hex_payload)

            # Success case: nova_request returns empty string (HTTP 200, no body).
            ok = (result is not None)
            if ok:
                _LOGGER.info("Play Sound submitted successfully for %s", device_id)
            else:
                _LOGGER.error("Play Sound failed for %s (nova_request returned None)", device_id)
            return bool(ok)

        except Exception as err:
            _LOGGER.error("Failed to play sound on %s: %s", device_id, err)
            return False
