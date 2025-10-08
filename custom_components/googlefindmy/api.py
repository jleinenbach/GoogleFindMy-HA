"""API wrapper for Google Find My Device (async-first, HA-friendly)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

from aiohttp import ClientError, ClientSession

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
from .NovaApi.ListDevices.nbe_list_devices import async_request_device_list
from .ProtoDecoders.decoder import (
    get_canonic_ids,
    get_devices_with_location,
    parse_device_list_protobuf,
)

_LOGGER = logging.getLogger(__name__)


# ----------------------------- Minimal protocols -----------------------------
@runtime_checkable
class FcmReceiverProtocol(Protocol):
    """Minimal protocol for the shared FCM receiver used by this module."""
    def get_fcm_token(self) -> Optional[str]: ...


@runtime_checkable
class CacheProtocol(Protocol):
    """Entry-scoped cache protocol (TokenCache instance)."""
    async def async_get_cached_value(self, key: str) -> Any: ...
    async def async_set_cached_value(self, key: str, value: Any) -> None: ...


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


# ----------------------------- Small helpers --------------------------------
def _infer_can_ring_slot(device: Dict[str, Any]) -> Optional[bool]:
    """Normalize a 'can ring' capability from various shapes; return None if unknown.
    
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
    except Exception:
        return None
    return None


def _build_can_ring_index(parsed_device_list: Any) -> Dict[str, bool]:
    """Build a mapping canonical_id -> can_ring (where determinable)."""
    index: Dict[str, bool] = {}
    try:
        devices = get_devices_with_location(parsed_device_list)
    except Exception:
        devices = []

    for d in devices or []:
        cid = d.get("canonicalId") or d.get("id") or d.get("device_id")
        if not cid:
            continue
        verdict = _infer_can_ring_slot(d)
        if isinstance(verdict, bool):
            index[str(cid)] = verdict
    return index


# ----------------------------- API class ------------------------------------
class GoogleFindMyAPI:
    """Async-first API wrapper for Google Find My Device.

    Notes:
        - Reads credentials/metadata via the injected entry-scoped cache (TokenCache).
        - Reuses a HA-managed aiohttp session when provided.
        - Push actions depend on the shared FCM receiver provider.
    """

    def __init__(
        self,
        cache: CacheProtocol,
        *,
        session: Optional[ClientSession] = None,
    ) -> None:
        """Initialize the API wrapper.

        Args:
            cache: Entry-scoped TokenCache instance (DI).
            session: HA-managed aiohttp ClientSession to reuse for network calls.
        """
        self._cache = cache
        self._session = session

        # Capability cache to avoid repeated network calls in capability checks.
        # Key: canonical device id, Value: can_ring (bool)
        self._device_capabilities: Dict[str, bool] = {}

    # ------------------------ Internal processing helpers ------------------------
    def _process_device_list_response(self, result_hex: str) -> List[Dict[str, Any]]:
        """Parse protobuf, update capability cache, and build basic device list."""
        parsed = parse_device_list_protobuf(result_hex)
        cap_index = _build_can_ring_index(parsed)
        if cap_index:
            self._device_capabilities.update(cap_index)

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
        """Pick the most relevant location record (latest last_seen if present).

        Prefer the entry with the highest 'last_seen' when available; otherwise, the first.
        """
        if not records:
            return {}
        try:
            with_ts = [r for r in records if r.get("last_seen") is not None]
            if with_ts:
                return max(with_ts, key=lambda r: float(r["last_seen"]))
        except Exception:
            pass
        return records[0]

    # ------------------------ FCM helper (via provider) --------------------------
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

    # ----------------------------- Device enumeration ----------------------------
    async def async_get_basic_device_list(
        self, username: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Async variant of the lightweight device list used by HA flows/coordinator.

        Returns:
            A list of minimal device dicts (id, name, optional can_ring).
            Returns an empty list on errors (graceful degradation).

        Behavior:
            - Resolves username from the entry-scoped cache if not provided.
            - Reuses the injected aiohttp session if supported by the caller.
        """
        try:
            if not username:
                try:
                    username = await self._cache.async_get_cached_value(username_string)
                except Exception:
                    username = None

            result_hex = await async_request_device_list(username, session=self._session)
            return self._process_device_list_response(result_hex)
        except ClientError as err:
            _LOGGER.error("Failed to get basic device list (async, network): %s", err)
            return []
        except Exception as err:
            _LOGGER.error("Failed to get basic device list (async): %s", err)
            return []

    def get_basic_device_list(self) -> List[Dict[str, Any]]:
        """Thin sync wrapper around async_get_basic_device_list for non-HA contexts.

        Guard:
            If called inside a running event loop (e.g., HA), logs and returns [].
        """
        try:
            # Avoid asyncio.run inside running loop (common in HA); guard defensively.
            loop = asyncio.get_event_loop()
            if loop.is_running():
                _LOGGER.error(
                    "get_basic_device_list() called inside an active event loop; use async_get_basic_device_list()."
                )
                return []
            return loop.run_until_complete(self.async_get_basic_device_list())
        except Exception as err:
            _LOGGER.error("Failed to get basic device list (sync): %s", err)
            return []

    def get_devices(self) -> List[Dict[str, Any]]:
        """Return devices with basic info only; no up-front location fetch (sync wrapper)."""
        base = self.get_basic_device_list()
        if base:
            _LOGGER.info("API v3.0: Returning %d devices (basic)", len(base))
        return self._extend_with_empty_location_fields(base)

    # --------------------------------- Location ----------------------------------
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
            records = await get_location_data_for_device(
                device_id, device_name, session=self._session
            )
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
        except ClientError as err:
            _LOGGER.error(
                "Network error while getting async location for %s (%s): %s",
                device_name,
                device_id,
                err,
            )
            return {}
        except Exception as err:
            _LOGGER.error(
                "Failed to get async location for %s (%s): %s",
                device_name,
                device_id,
                err,
            )
            return {}

    def get_device_location(self, device_id: str, device_name: str) -> Dict[str, Any]:
        """Thin sync wrapper around async_get_device_location for non-HA contexts."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                _LOGGER.error(
                    "get_device_location() called inside an active event loop; use async_get_device_location()."
                )
                return {}
            return loop.run_until_complete(self.async_get_device_location(device_id, device_name))
        except Exception as err:
            _LOGGER.error(
                "Failed to get location for %s (%s): %s", device_name, device_id, err
            )
            return {}

    def locate_device(self, device_id: str) -> Dict[str, Any]:
        """Compatibility sync entrypoint for location (uses sync wrapper)."""
        return self.get_device_location(device_id, device_id)

    # ------------------------ Play/Stop Sound / Push readiness -------------------
    def is_push_ready(self) -> bool:
        """Return True if the push transport (FCM) is initialized and has a usable token."""
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
        """
        if not self.is_push_ready():
            return False
        if device_id in self._device_capabilities:
            return bool(self._device_capabilities[device_id])
        return None

    # ---------- Play/Stop Sound (sync wrappers; for CLI/non-HA) ----------
    def play_sound(self, device_id: str) -> bool:
        """Thin sync wrapper around async_play_sound for non-HA contexts."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                _LOGGER.error(
                    "play_sound() called inside an active event loop; use async_play_sound()."
                )
                return False
            return loop.run_until_complete(self.async_play_sound(device_id))
        except Exception as err:
            _LOGGER.error("Failed to play sound on %s: %s", device_id, err)
            return False

    def stop_sound(self, device_id: str) -> bool:
        """Thin sync wrapper around async_stop_sound for non-HA contexts."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                _LOGGER.error(
                    "stop_sound() called inside an active event loop; use async_stop_sound()."
                )
                return False
            return loop.run_until_complete(self.async_stop_sound(device_id))
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
        except ClientError as err:
            _LOGGER.error("Network error while playing sound on %s: %s", device_id, err)
            return False
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
            result_hex = await async_submit_stop_sound_request(
                device_id, token, session=self._session
            )
            ok = result_hex is not None
            if ok:
                _LOGGER.info("Stop Sound (async) submitted successfully for %s", device_id)
            else:
                _LOGGER.error("Stop Sound (async) submission failed for %s", device_id)
            return bool(ok)
        except ClientError as err:
            _LOGGER.error("Network error while stopping sound on %s: %s", device_id, err)
            return False
        except Exception as err:
            _LOGGER.error("Failed to stop sound (async) on %s: %s", device_id, err)
            return False
