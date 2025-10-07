"""API wrapper for Google Find My Device."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from aiohttp import ClientSession

from .Auth.token_cache import set_memory_cache
from .Auth.username_provider import username_string
from .NovaApi.ExecuteAction.LocateTracker.location_request import (
    get_location_data_for_device,
)
from .NovaApi.ExecuteAction.PlaySound.start_sound_request import (
    async_submit_start_sound_request,
)
from .NovaApi.ExecuteAction.PlaySound.stop_sound_request import (
    async_submit_stop_sound_request,
)
from .NovaApi.ListDevices.nbe_list_devices import (
    async_request_device_list,
    request_device_list,
)
from .ProtoDecoders.decoder import (
    get_canonic_ids,
    get_devices_with_location,
    parse_device_list_protobuf,
)

_LOGGER = logging.getLogger(__name__)


@runtime_checkable
class FcmReceiverProtocol(Protocol):
    """Lightweight protocol for the shared FCM receiver used by this module.

    Notes:
        - Only the minimal surface needed here is declared to keep coupling low.
        - The receiver is expected to be a long-lived, HA-managed singleton registered
          from __init__.py via the register_fcm_receiver_provider() function below.
    """

    def get_fcm_token(self) -> Optional[str]:
        ...


# Module-local FCM provider getter; installed by the integration at setup time.
_FCM_ReceiverGetter: Optional[Callable[[], FcmReceiverProtocol]] = None


def register_fcm_receiver_provider(getter: Callable[[], FcmReceiverProtocol]) -> None:
    """Register a getter that returns the shared FCM receiver (HA-managed)."""
    global _FCM_ReceiverGetter
    _FCM_ReceiverGetter = getter


def unregister_fcm_receiver_provider() -> None:
    """Unregister the FCM receiver provider (called on unload/reload)."""
    global _FCM_ReceiverGetter
    _FCM_ReceiverGetter = None


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

    # ---------------------------------------------------------------------
    # FCM helpers (via shared provider)
    # ---------------------------------------------------------------------
    def _get_fcm_token_for_action(self) -> Optional[str]:
        """Return a valid FCM token for action requests via the shared receiver.

        Notes:
            - Uses the provider installed by the integration (HA-managed singleton).
            - Returns None if the provider is missing or a token cannot be obtained.
        """
        if _FCM_ReceiverGetter is None:
            _LOGGER.error("Cannot obtain FCM token: no provider registered.")
            return None

        try:
            receiver = _FCM_ReceiverGetter()
        except Exception as err:
            _LOGGER.error("Cannot obtain FCM token: provider callable failed: %s", err)
            return None

        if receiver is None:
            _LOGGER.error("Cannot obtain FCM token: provider returned None.")
            return None

        try:
            token = receiver.get_fcm_token()
        except Exception as err:
            _LOGGER.error("Cannot obtain FCM token from shared receiver: %s", err)
            return None

        if not token or not isinstance(token, str) or len(token) < 10:
            _LOGGER.error("FCM token not available or invalid (via shared receiver).")
            return None
        return token

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
        """Thin sync wrapper around async_get_device_location for non-HA contexts."""
        try:
            return asyncio.run(self.async_get_device_location(device_id, device_name))
        except RuntimeError as loop_err:
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
    # Play/Stop Sound / Push readiness
    # ---------------------------------------------------------------------
    def is_push_ready(self) -> bool:
        """Return True if the push transport (FCM) is initialized and has a usable token."""
        # Single source of truth: if we can obtain a non-empty token, push is ready.
        return self._get_fcm_token_for_action() is not None

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

    # ---------- Play/Stop Sound (sync thin wrappers; for CLI/non-HA) ----------
    def play_sound(self, device_id: str) -> bool:
        """Thin sync wrapper around async_play_sound for non-HA contexts."""
        try:
            return asyncio.run(self.async_play_sound(device_id))
        except RuntimeError as loop_err:
            _LOGGER.error(
                "play_sound() was called from an active event loop. "
                "Use async_play_sound() instead. Error: %s",
                loop_err,
            )
            return False
        except Exception as err:
            _LOGGER.error("Failed to play sound on %s: %s", device_id, err)
            return False

    def stop_sound(self, device_id: str) -> bool:
        """Thin sync wrapper around async_stop_sound for non-HA contexts."""
        try:
            return asyncio.run(self.async_stop_sound(device_id))
        except RuntimeError as loop_err:
            _LOGGER.error(
                "stop_sound() was called from an active event loop. "
                "Use async_stop_sound() instead. Error: %s",
                loop_err,
            )
            return False
        except Exception as err:
            _LOGGER.error("Failed to stop sound on %s: %s", device_id, err)
            return False

    # ---------- Play/Stop Sound (async; HA-first) ----------
    async def async_play_sound(self, device_id: str) -> bool:
        """Send a 'Play Sound' command to a device (async path for HA)."""
        token = self._get_fcm_token_for_action()
        if not token:
            return False

        try:
            _LOGGER.info("Submitting Play Sound (async) for %s", device_id)
            # Delegate payload build + transport to the submitter; provide HA session.
            # NOTE: If Nova later requires an explicit username for action endpoints,
            # extend submitter signatures to accept and forward it consistently.
            result_hex = await async_submit_start_sound_request(
                device_id, token, session=self._session
            )
            ok = result_hex is not None
            if ok:
                _LOGGER.info("Play Sound (async) submitted successfully for %s", device_id)
            else:
                _LOGGER.error("Play Sound (async) submission failed for %s", device_id)
            return bool(ok)
        except Exception as err:
            _LOGGER.error("Failed to play sound (async) on %s: %s", device_id, err)
            return False

    async def async_stop_sound(self, device_id: str) -> bool:
        """Send a 'Stop Sound' command to a device (async path for HA)."""
        token = self._get_fcm_token_for_action()
        if not token:
            return False

        try:
            _LOGGER.info("Submitting Stop Sound (async) for %s", device_id)
            # NOTE: See comment in async_play_sound() about potential username forwarding.
            result_hex = await async_submit_stop_sound_request(
                device_id, token, session=self._session
            )
            ok = result_hex is not None
            if ok:
                _LOGGER.info("Stop Sound (async) submitted successfully for %s", device_id)
            else:
                _LOGGER.error("Stop Sound (async) submission failed for %s", device_id)
            return bool(ok)
        except Exception as err:
            _LOGGER.error("Failed to stop sound (async) on %s: %s", device_id, err)
            return False
