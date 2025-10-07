"""API wrapper for Google Find My Device."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from aiohttp import ClientSession

from .Auth.token_cache import set_memory_cache
from .Auth.username_provider import username_string
from .NovaApi.ExecuteAction.LocateTracker.location_request import (
    get_location_data_for_device,
)
from .NovaApi.ExecuteAction.PlaySound.start_sound_request import start_sound_request
from .NovaApi.ListDevices.nbe_list_devices import (
    async_request_device_list,
    request_device_list,
)
from .NovaApi.nova_request import nova_request, async_nova_request
from .NovaApi.scopes import NOVA_ACTION_API_SCOPE
from .ProtoDecoders.decoder import (
    get_canonic_ids,
    get_devices_with_location,
    parse_device_list_protobuf,
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
        # May include capabilities even without location
        devices = get_devices_with_location(parsed_device_list)
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
        session: Optional[ClientSession] = None,
    ) -> None:
        """Initialize the API wrapper.

        The API reads credentials from our in-memory token cache. We seed that cache from either:
        - A full secrets bundle (GoogleFindMyTools), or
        - Individual OAuth token + Google email.

        Args:
            oauth_token: The OAuth token for authentication.
            google_email: The user's Google email address.
            secrets_data: A dictionary containing the full secrets bundle.
            session: The aiohttp ClientSession to use for requests (HA-managed).
        """
        self.secrets_data = secrets_data
        self.oauth_token = oauth_token
        self.google_email = google_email
        self._session = session  # HA-managed aiohttp session for async calls

        # Capability cache to avoid repeated network calls in capability checks.
        # Key: canonical device id, Value: can_ring (bool)
        self._device_capabilities: Dict[str, bool] = {}

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
    # Internal compatibility helpers
    # ---------------------------------------------------------------------
    def _req_device_list(self) -> str:
        """Call request_device_list with session if supported (compat-safe)."""
        try:
            return request_device_list(session=self._session)
        except TypeError:
            _LOGGER.debug("Falling back to request_device_list without session")
            return request_device_list()

    async def _async_req_device_list(self, username: Optional[str] = None) -> str:
        """Call async_request_device_list with session if supported (compat-safe)."""
        try:
            return await async_request_device_list(username, session=self._session)
        except TypeError:
            _LOGGER.debug("Falling back to async_request_device_list without session")
            return await async_request_device_list(username)

    async def _async_get_location(self, device_id: str, device_name: str) -> list[dict[str, Any]]:
        """Call get_location_data_for_device with session if supported (compat-safe)."""
        try:
            return await get_location_data_for_device(
                device_id, device_name, session=self._session
            )
        except TypeError:
            _LOGGER.debug("Falling back to get_location_data_for_device without session")
            return await get_location_data_for_device(device_id, device_name)

    # ---------------------------------------------------------------------
    # Internal processing helpers (de-duplicate logic)
    # ---------------------------------------------------------------------
    def _process_device_list_response(self, result_hex: str) -> List[Dict[str, Any]]:
        """Parse the protobuf payload, update capability cache, and build basic device list."""
        parsed = parse_device_list_protobuf(result_hex)
        # Update internal capability cache
        cap_index = _build_can_ring_index(parsed)
        if cap_index:
            self._device_capabilities.update(cap_index)

        # Build lightweight list (id/name [+ optional can_ring])
        devices: List[Dict[str, Any]] = []
        for device_name, canonic_id in get_canonic_ids(parsed):
            item = {
                "name": device_name,
                "id": canonic_id,
                "device_id": canonic_id,
            }
            if canonic_id in self._device_capabilities:
                item["can_ring"] = bool(self._device_capabilities[canonic_id])
            devices.append(item)
        return devices

    def _extend_with_empty_location_fields(
        self, items: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Augment basic device entries with common location fields set to None."""
        extended: List[Dict[str, Any]] = []
        for base in items:
            dev = {
                **base,
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
            extended.append(dev)
        return extended

    def _select_best_location(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Select the most relevant location record.

        Rationale:
            Upstream returns a list; in current practice the first entry is the most recent.
            To be robust, prefer the entry with the highest 'last_seen' when available.
        """
        if not records:
            return {}
        # Prefer record with max last_seen if present; otherwise keep the first element.
        try:
            with_ts = [r for r in records if "last_seen" in r and r["last_seen"] is not None]
            if with_ts:
                return max(with_ts, key=lambda r: float(r["last_seen"]))
        except Exception:
            pass
        return records[0]

    def _get_fcm_token_for_action(self) -> Optional[str]:
        """Return a valid FCM token for action requests or None if unavailable.

        Uses lazy import to avoid heavy imports for users not using push actions.
        """
        try:
            from .Auth.fcm_receiver_ha import FcmReceiverHA  # lazy import
        except Exception as err:
            _LOGGER.error("Cannot obtain FCM token: FCM receiver import failed: %s", err)
            return None

        try:
            fcm_receiver = FcmReceiverHA()
            if not getattr(fcm_receiver, "credentials", None):
                _LOGGER.error("Cannot obtain FCM token: credentials missing")
                return None

            token = fcm_receiver.get_fcm_token()
            if not token or not isinstance(token, str) or len(token) < 10:
                _LOGGER.error("Cannot obtain FCM token: token not available or invalid")
                return None
            return token
        except Exception as err:
            _LOGGER.error("Cannot obtain FCM token: %s", err)
            return None

    # ---------------------------------------------------------------------
    # Init helpers
    # ---------------------------------------------------------------------
    def _initialize_from_secrets(self, secrets: dict) -> None:
        """Initialize from secrets.json (GoogleFindMyTools) into memory cache.

        NOTE: No blocking I/O here; we only seed memory. Persistent storage
        is handled elsewhere asynchronously by the integration.
        """
        enhanced = dict(secrets)
        self.google_email = secrets.get("username", secrets.get("Email"))
        if self.google_email:
            enhanced[username_string] = self.google_email

        set_memory_cache(enhanced)
        _LOGGER.debug(
            "Seeded in-memory token cache with %d keys from secrets bundle", len(enhanced)
        )

    # ---------------------------------------------------------------------
    # Device enumeration
    # ---------------------------------------------------------------------
    def get_basic_device_list(self) -> List[Dict[str, Any]]:
        """Return a lightweight list of devices (id/name) with optional 'can_ring'.

        Error handling:
            This sync variant mirrors the async variant and returns an EMPTY LIST on errors.
            Callers should treat an empty list as a transient failure and may retry later.
        """
        try:
            result_hex = self._req_device_list()
            return self._process_device_list_response(result_hex)
        except Exception as err:
            _LOGGER.error("Failed to get basic device list (sync): %s", err)
            return []

    async def async_get_basic_device_list(
        self, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Async variant of the lightweight device list, used by HA flows and coordinator.

        Error handling:
            Returns an EMPTY LIST on errors to keep the UI responsive (graceful degradation).
        """
        try:
            if not username and self.secrets_data:
                username = self.secrets_data.get("googleHomeUsername") or self.secrets_data.get(
                    "google_email"
                )
            result_hex = await self._async_req_device_list(username)
            return self._process_device_list_response(result_hex)
        except Exception as err:
            _LOGGER.error("Failed to get basic device list (async): %s", err)
            return []

    def get_devices(self) -> List[Dict[str, Any]]:
        """Return devices with basic info only; no up-front location fetch.

        Implementation detail:
            Reuses get_basic_device_list() and augments the entries with
            standard location fields set to None (DRY).
        """
        base = self.get_basic_device_list()
        # Keep info-level log identical to former behavior (optional)
        if base:
            _LOGGER.info("API v3.0: Returning %d devices (basic)", len(base))
        return self._extend_with_empty_location_fields(base)

    # ---------------------------------------------------------------------
    # Location
    # ---------------------------------------------------------------------
    def get_device_location(self, device_id: str, device_name: str) -> Dict[str, Any]:
        """Sync wrapper for a per-device async location request.

        IMPORTANT:
            This method is intended to be executed in a worker thread
            (via hass.async_add_executor_job). It must not run in the main loop.

        Error handling:
            Returns an EMPTY DICT on error. The caller should treat this as "no data".
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
            _LOGGER.error(
                "Failed to get location for %s (%s): %s", device_name, device_id, err
            )
            return {}

    async def async_get_device_location(
        self, device_id: str, device_name: str
    ) -> Dict[str, Any]:
        """Async, HA-compatible location request for a single device.

        Selection policy:
            Upstream returns a list of location records. We choose the record
            with the highest 'last_seen' if present; otherwise we use the first record.
        """
        try:
            _LOGGER.info(
                "API v3.0 Async: Requesting location for %s (%s)", device_name, device_id
            )
            records = await self._async_get_location(device_id, device_name)
            best = self._select_best_location(records)
            if best:
                _LOGGER.info(
                    "API v3.0 Async: Selected location record for %s (have %d total)",
                    device_name,
                    len(records),
                )
                return best
            _LOGGER.warning("API v3.0 Async: No location data for %s", device_name)
            return {}
        except Exception as err:
            _LOGGER.error(
                "Failed to get async location for %s (%s): %s",
                device_name,
                device_id,
                err,
            )
            return {}

    def locate_device(self, device_id: str) -> Dict[str, Any]:
        """Compatibility sync entrypoint for location (uses sync wrapper)."""
        return self.get_device_location(device_id, device_id)

    # ---------------------------------------------------------------------
    # Play Sound / Push readiness
    # ---------------------------------------------------------------------
    def is_push_ready(self) -> bool:
        """Return True if the push transport (FCM) is initialized and has a usable token."""
        try:
            # Use the same token retrieval logic used for actions, but without logs at ERROR level.
            # We keep this check lightweight and quiet by duplicating minimal logic.
            from .Auth.fcm_receiver_ha import FcmReceiverHA  # lazy import
            fcm = FcmReceiverHA()
            if not getattr(fcm, "credentials", None):
                return False
            token = fcm.get_fcm_token()
            if not token or not isinstance(token, str) or len(token) < 10:
                return False
            return True
        except Exception:
            return False

    @property
    def push_ready(self) -> bool:
        """Back-compat property variant of is_push_ready()."""
        return self.is_push_ready()

    def can_play_sound(self, device_id: str) -> Optional[bool]:
        """Return a verdict whether 'Play Sound' is supported for this device.

        Strategy:
            - If push is not ready -> False.
            - Check internal capability cache first (no network).
            - If capability is unknown -> return None (let the caller decide optimistically).

        Note:
            The capability cache is updated whenever (async_)get_basic_device_list() runs.
            This avoids triggering a network call from availability checks.
        """
        if not self.is_push_ready():
            return False

        if device_id in self._device_capabilities:
            return bool(self._device_capabilities[device_id])

        # Unknown -> let Coordinator fall back optimistically without network I/O
        return None

    def play_sound(self, device_id: str) -> bool:
        """Send a 'Play Sound' command to a device (sync legacy path).

        NOTE:
            Retained for non-HA/CLI use. Home Assistant calls async_play_sound().

        Returns:
            True on successful submission to the Google Action API (HTTP 200),
            False otherwise.
        """
        token = self._get_fcm_token_for_action()
        if not token:
            return False

        try:
            _LOGGER.info("Sending Play Sound to %s (sync path)", device_id)
            hex_payload = start_sound_request(device_id, token)
            _LOGGER.debug("Sound request payload length: %s chars", len(hex_payload))

            # Sync nova_request() uses 'requests'; no aiohttp session is passed.
            result = nova_request(NOVA_ACTION_API_SCOPE, hex_payload)
            ok = result is not None
            if ok:
                _LOGGER.info("Play Sound submitted successfully for %s", device_id)
            else:
                _LOGGER.error("Play Sound failed for %s (empty response)", device_id)
            return bool(ok)
        except Exception as err:
            _LOGGER.error("Failed to play sound on %s: %s", device_id, err)
            return False

    async def async_play_sound(self, device_id: str) -> bool:
        """Send a 'Play Sound' command to a device (async path for HA)."""
        token = self._get_fcm_token_for_action()
        if not token:
            return False

        try:
            _LOGGER.info("Sending Play Sound (async) to %s", device_id)
            hex_payload = start_sound_request(device_id, token)
            _LOGGER.debug("Sound request payload length (async): %s chars", len(hex_payload))

            # Use async Nova path with HA-managed session if available
            result_hex = await async_nova_request(
                NOVA_ACTION_API_SCOPE, hex_payload, username=self.google_email, session=self._session
            )
            ok = result_hex is not None
            if ok:
                _LOGGER.info("Play Sound (async) submitted successfully for %s", device_id)
            else:
                _LOGGER.error("Play Sound (async) failed for %s (empty response)", device_id)
            return bool(ok)
        except Exception as err:
            _LOGGER.error("Failed to play sound (async) on %s: %s", device_id, err)
            return False
