# custom_components/googlefindmy/api.py
"""API wrapper for Google Find My Device (async-first, HA-friendly)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, runtime_checkable

from aiohttp import ClientError, ClientSession
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

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
from .NovaApi.nova_request import NovaAuthError, NovaHTTPError, NovaRateLimitError
from .ProtoDecoders.decoder import (
    get_canonic_ids,
    get_devices_with_location,
    parse_device_list_protobuf,
)
from .const import CONF_OAUTH_TOKEN  # used by the ephemeral flow cache

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
    """Register a getter that returns the shared FCM receiver (HA-managed).

    The provider is a zero-arg callable that returns the current receiver instance.
    We keep this indirection to avoid importing heavy modules at import time and
    to stay resilient to reloads (the callable resolves the live object on access).

    Args:
        getter: A callable that returns the singleton FcmReceiverProtocol instance.
    """
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
    """Build a mapping canonical_id -> can_ring (where determinable).

    Args:
        parsed_device_list: The parsed protobuf message from the device list response.

    Returns:
        A dictionary mapping canonical device IDs to a boolean indicating if they can ring.
    """
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


# ---------------------- Ephemeral flow cache for Config Flow -----------------
class _EphemeralCache:
    """Tiny in-memory cache used only for short-lived validation in flows.

    It implements the CacheProtocol subset that the API needs. Values are kept
    in-memory only and never persisted to disk.
    """

    def __init__(self, *, oauth_token: Optional[str], email: Optional[str]) -> None:
        """Initialize the ephemeral cache with credentials.

        Args:
            oauth_token: The OAuth token.
            email: The user's Google email address.
        """
        self._data: Dict[str, Any] = {}
        if isinstance(email, str) and email:
            self._data[username_string] = email
        if isinstance(oauth_token, str) and oauth_token:
            self._data[CONF_OAUTH_TOKEN] = oauth_token

    async def async_get_cached_value(self, key: str) -> Any:
        """Get a value from the in-memory cache.

        Args:
            key: The key of the value to retrieve.

        Returns:
            The cached value, or None if not found.
        """
        return self._data.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        """Set a value in the in-memory cache.

        Args:
            key: The key of the value to set.
            value: The value to store. If None, the key is removed.
        """
        if value is None:
            self._data.pop(key, None)
        else:
            self._data[key] = value


# ----------------------------- API class ------------------------------------
class GoogleFindMyAPI:
    """Async-first API wrapper for Google Find My Device.

    This class provides a high-level interface to the underlying Google Find My
    Device services. It handles authentication, data parsing, and action execution
    (like locating a device or playing a sound) in an asynchronous manner suitable
    for Home Assistant.

    Notes:
        - For runtime use, credentials/metadata come from the entry-scoped cache (TokenCache).
        - For short-lived Config/Options flows, minimal credentials may be provided directly.
        - A HA-managed aiohttp session can be reused for all network calls.
        - Push actions depend on the shared FCM receiver provider.
    """

    def __init__(
        self,
        cache: Optional[CacheProtocol] = None,
        *,
        session: Optional[ClientSession] = None,
        oauth_token: Optional[str] = None,
        google_email: Optional[str] = None,
    ) -> None:
        """Initialize the API wrapper.

        Preferred:
            Pass a TokenCache-like object via `cache`.

        Flow-friendly:
            If `cache` is not provided, you may pass `oauth_token` and/or
            `google_email`. The API will construct an ephemeral in-memory cache
            that satisfies the lookups it performs (primarily the username).

        Args:
            cache: Entry-scoped TokenCache instance (recommended for runtime).
            session: HA-managed aiohttp ClientSession to reuse for network calls.
            oauth_token: Optional OAuth token (flow validation only).
            google_email: Optional Google account e-mail (flow validation only).
        """
        if cache is None and (oauth_token or google_email):
            cache = _EphemeralCache(oauth_token=oauth_token, email=google_email)
        if cache is None:
            # Runtime misuse: the coordinator should always pass a cache; flows should
            # at least pass email/token. Fail early to surface programming errors.
            raise TypeError(
                "GoogleFindMyAPI requires either `cache=` or minimal flow credentials "
                "(`oauth_token`/`google_email`)."
            )

        self._cache: CacheProtocol = cache
        self._session = session

        # Capability cache to avoid repeated network calls in capability checks.
        # Key: canonical device id, Value: can_ring (bool)
        self._device_capabilities: Dict[str, bool] = {}

    # ------------------------ Internal processing helpers ------------------------
    def _process_device_list_response(self, result_hex: str) -> List[Dict[str, Any]]:
        """Parse protobuf, update capability cache, and build basic device list.

        Args:
            result_hex: The hexadecimal string of the protobuf response.

        Returns:
            A list of dictionaries, each representing a device with its basic info.
        """
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
        """Augment basic device entries with common location fields set to None.

        Args:
            items: A list of basic device dictionaries.

        Returns:
            A new list of device dictionaries with added placeholder location fields.
        """
        extended: List[Dict[str, Any]] = []
        for base in items:
            dev = (
                {
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
            )
            extended.append(dev)
        return extended

    def _select_best_location(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """Pick the most relevant location record (latest last_seen if present).

        Prefer the entry with the highest 'last_seen' when available; otherwise, the first.

        Args:
            records: A list of location data dictionaries for a device.

        Returns:
            The single best location record dictionary.
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

        Returns:
            The FCM token as a string, or None if unavailable.
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

    def _peek_fcm_token_quietly(self) -> Optional[str]:
        """Best-effort token probe for readiness checks (no ERROR-level log spam).

        Returns:
            Token string when obtainable; otherwise None. All failures are logged at DEBUG.
        """
        if _FCM_ReceiverGetter is None:
            _LOGGER.debug("FCM readiness probe: no provider registered.")
            return None
        try:
            receiver = _FCM_ReceiverGetter()
        except Exception as err:
            _LOGGER.debug("FCM readiness probe: provider callable failed: %s", err)
            return None
        if receiver is None:
            _LOGGER.debug("FCM readiness probe: provider returned None.")
            return None
        try:
            token = receiver.get_fcm_token()
        except Exception as err:
            _LOGGER.debug("FCM readiness probe: get_fcm_token failed: %s", err)
            return None
        if not token or not isinstance(token, str) or len(token) < 10:
            _LOGGER.debug("FCM readiness probe: token missing or too short.")
            return None
        return token

    # ----------------------------- Device enumeration ----------------------------
    async def async_get_basic_device_list(
        self,
        username: Optional[str] = None,
        *,
        # Flow/local validation overrides (passed through to Nova):
        token: Optional[str] = None,
        cache_get: Optional[Callable[[str], Awaitable[Any]]] = None,
        cache_set: Optional[Callable[[str, Any], Awaitable[None]]] = None,
        refresh_override: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    ) -> List[Dict[str, Any]]:
        """Async variant of the lightweight device list used by HA flows/coordinator.

        This method fetches a list of devices associated with the Google account,
        including their names, IDs, and ringing capability.

        Args:
            username: The Google account email. If None, it will be retrieved from the cache.
            token: Optional auth token override to use for this call only.
            cache_get: Optional async getter for TTL/aux metadata (flow-local).
            cache_set: Optional async setter for TTL/aux metadata (flow-local).
            refresh_override: Optional async callable to refresh/obtain a token for this call.

        Returns:
            A list of minimal device dicts (id, name, optional can_ring).

        Raises:
            ConfigEntryAuthFailed: If authentication fails.
            UpdateFailed: If the API is rate-limited, returns a server error, or a network/other error occurs.
        """
        try:
            if not username:
                try:
                    username = await self._cache.async_get_cached_value(username_string)
                except Exception:
                    username = None

            # Prefer the HA-managed session if available.
            sess = self._session

            # Provide defaults for TTL metadata I/O if the caller didn't override.
            cg = cache_get or self._cache.async_get_cached_value  # type: ignore[attr-defined]
            cs = cache_set or self._cache.async_set_cached_value  # type: ignore[attr-defined]

            # Forward flow/local knobs to the Nova ListDevices helper. If the installed
            # helper is older and does not support these kwargs, gracefully fall back.
            try:
                result_hex = await async_request_device_list(
                    username,
                    session=sess,
                    token=token,
                    cache_get=cg,
                    cache_set=cs,
                    refresh_override=refresh_override,
                )
            except TypeError:
                # Older helper signature (no pass-through); best-effort fallback matrix.
                if sess is not None:
                    try:
                        result_hex = await async_request_device_list(username, session=sess)  # type: ignore[misc]
                    except TypeError:
                        result_hex = await async_request_device_list(username)  # type: ignore[misc]
                else:
                    result_hex = await async_request_device_list(username)  # type: ignore[misc]

            return self._process_device_list_response(result_hex)
        except asyncio.CancelledError:
            raise
        except NovaRateLimitError as err:
            _LOGGER.warning("Device list temporarily rate-limited: %s", err)
            raise UpdateFailed(str(err)) from err
        except NovaHTTPError as err:
            _LOGGER.warning("Device list temporarily unavailable (server error %s): %s", err.status, err)
            raise UpdateFailed(str(err)) from err
        except NovaAuthError as err:
            _LOGGER.error("Authentication failed while listing devices: %s", err)
            raise ConfigEntryAuthFailed(str(err)) from err
        except ClientError as err:
            # Minimal-invasive change: do not degrade to empty success; signal transient failure.
            _LOGGER.warning("Failed to get basic device list (async, network): %s", err)
            raise UpdateFailed(f"Network error fetching device list: {err}") from err
        except Exception as err:
            # Do not mask unexpected errors as an empty list; let the coordinator keep last good data.
            _LOGGER.error("Failed to get basic device list (async): %s", err)
            raise UpdateFailed(f"Unexpected error fetching device list: {err}") from err

    def get_basic_device_list(self) -> List[Dict[str, Any]]:
        """Thin sync wrapper around async_get_basic_device_list for non-HA contexts.

        Guard:
            If called inside a running event loop (e.g., HA), logs and returns [].

        Returns:
            A list of device dictionaries.
        """
        try:
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
        """Return devices with basic info only; no up-front location fetch (sync wrapper).

        Returns:
            A list of device dictionaries augmented with empty location fields.
        """
        base = self.get_basic_device_list()
        if base:
            _LOGGER.info("API v3.0: Returning %d devices (basic)", len(base))
        return self._extend_with_empty_location_fields(base)

    # --------------------------------- Location ----------------------------------
    async def async_get_device_location(
        self, device_id: str, device_name: str
    ) -> Dict[str, Any]:
        """Async, HA-compatible location request for a single device.

        This function requests the location for a specific device and selects the most
        relevant location record from the response.

        Args:
            device_id: The canonical ID of the device.
            device_name: The human-readable name of the device for logging.

        Returns:
            A dictionary containing the best available location data for the device.
            Returns an empty dictionary on failure.
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
        except RuntimeError as err:
            # Startup safety net: during cold boot, the FCM provider may not yet be registered.
            # Downgrade this expected transient to DEBUG and retry on the next cycle.
            if "FCM receiver provider has not been registered" in str(err):
                _LOGGER.debug(
                    "Startup race: FCM provider not ready for %s (%s). Will retry on next cycle.",
                    device_name,
                    device_id,
                )
                return {}
            _LOGGER.error(
                "Runtime error while getting async location for %s (%s): %s",
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
        """Thin sync wrapper around async_get_device_location for non-HA contexts.

        Args:
            device_id: The canonical ID of the device.
            device_name: The human-readable name of the device.

        Returns:
            A dictionary containing location data, or an empty dictionary on failure.
        """
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
        """Compatibility sync entrypoint for location (uses sync wrapper).

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A dictionary containing location data.
        """
        return self.get_device_location(device_id, device_id)

    # ------------------------ Play/Stop Sound / Push readiness -------------------
    def is_push_ready(self) -> bool:
        """Return True if the push transport (FCM) appears initialized and ready.

        Heuristics (no I/O, no blocking):
          1) Use receiver-level readiness flags when available (is_ready/ready).
          2) Inspect push client state on the receiver (pc.run_state == STARTED and pc.do_listen).
          3) Fall back to token presence via a quiet probe (no ERROR-level spam).

        This keeps API- and coordinator-level gating consistent while avoiding tight
        coupling to specific FCM client classes and enums.
        """
        # No provider registered?
        if _FCM_ReceiverGetter is None:
            return False

        # Resolve live receiver (may change across reloads)
        try:
            receiver = _FCM_ReceiverGetter()
        except Exception:
            return False
        if receiver is None:
            return False

        # 1) Receiver-level booleans
        for attr in ("is_ready", "ready"):
            val = getattr(receiver, attr, None)
            if isinstance(val, bool):
                return val

        # 2) Push client heuristic: tolerate enum or string for run_state
        pc = getattr(receiver, "pc", None)
        if pc is not None:
            state = getattr(pc, "run_state", None)
            state_name = getattr(state, "name", state)  # enum.name or raw
            if state_name == "STARTED" and bool(getattr(pc, "do_listen", False)):
                return True

        # 3) Quiet token probe as last resort
        return self._peek_fcm_token_quietly() is not None

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

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the device can play sound, False if not, or None if unknown.
        """
        if not self.is_push_ready():
            return False
        if device_id in self._device_capabilities:
            return bool(self._device_capabilities[device_id])
        return None

    # ---------- Play/Stop Sound (sync wrappers; for CLI/non-HA) ----------
    def play_sound(self, device_id: str) -> bool:
        """Thin sync wrapper around async_play_sound for non-HA contexts.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the command was sent successfully, False otherwise.
        """
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
        """Thin sync wrapper around async_stop_sound for non-HA contexts.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the command was sent successfully, False otherwise.
        """
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
        """Send a 'Play Sound' command to a device (async path for HA).

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the command was submitted successfully, False otherwise.
        """
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
        """Send a 'Stop Sound' command to a device (async path for HA).

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the command was submitted successfully, False otherwise.
        """
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
