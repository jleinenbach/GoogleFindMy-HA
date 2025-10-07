"""Home Assistant compatible FCM receiver for Google Find My Device."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from custom_components.googlefindmy.Auth.token_cache import (
    async_get_cached_value,
    async_load_cache_from_file,
    async_set_cached_value,
    get_cached_value,  # sync fallback for early boot
)

_LOGGER = logging.getLogger(__name__)


class FcmReceiverHA:
    """FCM Receiver integrated with Home Assistant's async lifecycle.

    Design:
      - No internal singleton: HA manages a single shared instance in hass.data.
      - Synchronous register/unregister methods to match async_on_unload contract.
      - Push-to-entities: prefer coordinator.push_updated() (if present) over refresh.
      - Encapsulation: never mutate coordinator private attrs when a public API exists.
    """

    def __init__(self) -> None:
        self.credentials: Optional[dict] = None
        # Per-request callbacks waiting for a specific device response
        self.location_update_callbacks: Dict[str, Callable[[str, str], None]] = {}
        # Coordinators eligible to receive background updates
        self.coordinators: List[Any] = []

        self.pc = None  # FcmPushClient instance
        self._listening: bool = False
        self._listen_task: Optional[asyncio.Task] = None

        # Firebase project configuration for Google Find My Device
        self.project_id = "google.com:api-project-289722593072"
        self.app_id = "1:289722593072:android:3cfcf5bc359f0308"
        self.api_key = "AIzaSyD_gko3P392v6how2H7UpdeXQ0v2HLettc"
        self.message_sender_id = "289722593072"

    # -------------------- Lifecycle --------------------

    async def async_initialize(self) -> bool:
        """Initialize receiver and underlying push client."""
        try:
            await async_load_cache_from_file()
        except OSError as err:
            _LOGGER.debug("Token cache preload failed (continuing): %s", err)

        creds: Any = await async_get_cached_value("fcm_credentials")
        if isinstance(creds, str):
            try:
                creds = json.loads(creds)
            except json.JSONDecodeError as e:
                _LOGGER.error("Failed to parse FCM credentials JSON: %s", e)
                return False
        self.credentials = creds if isinstance(creds, dict) else None

        # Lazy import to avoid heavy deps at import time
        try:
            from custom_components.googlefindmy.Auth.firebase_messaging import (  # type: ignore
                FcmPushClient,
                FcmRegisterConfig,
            )
        except ImportError as err:
            _LOGGER.error("Failed to import FCM client support: %s", err)
            return False

        fcm_config = FcmRegisterConfig(
            project_id=self.project_id,
            app_id=self.app_id,
            api_key=self.api_key,
            messaging_sender_id=self.message_sender_id,
            bundle_id="com.google.android.apps.adm",
        )

        try:
            self.pc = FcmPushClient(
                self._on_notification,
                fcm_config,
                self.credentials,
                self._on_credentials_updated,
            )
        except Exception as err:
            _LOGGER.error("Failed to construct FCM push client: %s", err)
            return False

        _LOGGER.info("FCM receiver initialized")
        return True

    async def async_register_for_location_updates(
        self, device_id: str, callback: Callable[[str, str], None]
    ) -> Optional[str]:
        """Register a per-request callback for a device and ensure listener is running."""
        self.location_update_callbacks[device_id] = callback
        _LOGGER.debug("Registered FCM callback for device: %s", device_id)

        if not self._listening:
            await self._start_listening()

        token = self.get_fcm_token()
        if not token:
            _LOGGER.warning("FCM credentials/token not available after registration")
        return token

    async def async_unregister_for_location_updates(self, device_id: str) -> None:
        """Remove a per-request callback for a device."""
        self.location_update_callbacks.pop(device_id, None)
        _LOGGER.debug("Unregistered FCM callback for device: %s", device_id)

    # -------------------- Coordinator wiring (sync by contract) --------------------

    def register_coordinator(self, coordinator: Any) -> None:
        """Register a coordinator to receive background location updates."""
        if coordinator not in self.coordinators:
            self.coordinators.append(coordinator)
            _LOGGER.debug("Coordinator registered (total=%d)", len(self.coordinators))

    def unregister_coordinator(self, coordinator: Any) -> None:
        """Unregister a coordinator (sync; safe for async_on_unload)."""
        try:
            self.coordinators.remove(coordinator)
            _LOGGER.debug("Coordinator unregistered (total=%d)", len(self.coordinators))
        except ValueError:
            pass  # already removed

    # -------------------- Internal listening --------------------

    async def _start_listening(self) -> None:
        if not self.pc:
            ok = await self.async_initialize()
            if not ok:
                return

        await self._register_for_fcm()

        # Start background listener task
        self._listen_task = asyncio.create_task(self._listen_for_messages(), name="googlefindmy.fcm_listener")
        self._listening = True
        _LOGGER.info("Started listening for FCM notifications")

    async def _register_for_fcm(self) -> None:
        if not self.pc:
            return

        for attempt in range(1, 4):
            try:
                token = await self.pc.checkin_or_register()
                if token:
                    _LOGGER.info("FCM registered (attempt %d), token: %s…", attempt, token[:20])
                    return
                _LOGGER.warning("FCM registration returned no token (attempt %d)", attempt)
            except (ConnectionError, TimeoutError) as err:
                _LOGGER.error("FCM registration network error (attempt %d): %s", attempt, err)
            except Exception as err:  # final guard
                _LOGGER.error("FCM registration unexpected error (attempt %d): %s", attempt, err)
            await asyncio.sleep(5)

    async def _listen_for_messages(self) -> None:
        try:
            if self.pc:
                await self.pc.start()
                _LOGGER.debug("FCM message listener started")
        except (ConnectionError, TimeoutError) as err:
            _LOGGER.error("FCM listen network error: %s", err)
        except Exception as err:
            _LOGGER.error("FCM listen unexpected error: %s", err)
        finally:
            self._listening = False

    # -------------------- Incoming notifications --------------------

    def _on_notification(self, obj: Dict[str, Any], notification, data_message) -> None:
        """Handle incoming FCM notification (sync callback from client)."""
        try:
            payload = (obj.get("data") or {}).get("com.google.android.apps.adm.FCM_PAYLOAD")
            if not payload:
                _LOGGER.debug("FCM notification without FMD payload")
                return

            # Base64 decode with padding
            pad = len(payload) % 4
            if pad:
                payload += "=" * (4 - pad)

            try:
                decoded = base64.b64decode(payload)
            except (binascii.Error, ValueError) as err:
                _LOGGER.error("FCM Base64 decode failed: %s", err)
                return

            hex_string = binascii.hexlify(decoded).decode("utf-8")
            _LOGGER.info("Received FCM location response: %d chars", len(hex_string))

            canonic_id = self._extract_canonic_id_from_response(hex_string)
            if not canonic_id:
                _LOGGER.debug("FCM response has no canonical id")
                return

            # Direct per-request callback?
            cb = self.location_update_callbacks.get(canonic_id)
            if cb:
                asyncio.create_task(self._run_callback_async(cb, canonic_id, hex_string))
                return

            # Background path via registered coordinators (iterate over a copy to avoid races)
            for coordinator in self.coordinators.copy():
                if self._is_tracked(coordinator, canonic_id):
                    name = getattr(coordinator, "_device_names", {}).get(canonic_id, canonic_id[:8])
                    _LOGGER.info("Processing background FCM update for %s", name)
                    asyncio.create_task(self._process_background_update(coordinator, canonic_id, hex_string))
                    return

            # Not matched to any waiting callback or coordinator
            if self.location_update_callbacks:
                waiting = [d[:8] + "…" for d in self.location_update_callbacks.keys()]
                _LOGGER.debug(
                    "FCM response for %s… not matched; currently waiting for: %s",
                    canonic_id[:8],
                    waiting,
                )
            else:
                _LOGGER.debug("FCM response for %s… (no registered coordinators or callbacks)", canonic_id[:8])

        except Exception as err:
            # Final guard to avoid crashing the receiver callback
            _LOGGER.error("Error processing FCM notification: %s", err)

    # -------------------- Helpers --------------------

    @staticmethod
    def _norm(dev_id: str) -> str:
        return (dev_id or "").replace("-", "").lower()

    def _is_tracked(self, coordinator: Any, canonic_id: str) -> bool:
        """True if device is tracked by coordinator.

        Semantics: empty tracked_devices => track ALL devices.
        """
        tracked = getattr(coordinator, "tracked_devices", []) or []
        if not tracked:
            return True
        nid = self._norm(canonic_id)
        return any(self._norm(did) == nid for did in tracked)

    def _extract_canonic_id_from_response(self, hex_response: str) -> Optional[str]:
        """Extract canonical id via the decoder."""
        try:
            from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf  # type: ignore

            device_update = parse_device_update_protobuf(hex_response)
            if device_update.HasField("deviceMetadata"):
                ids = device_update.deviceMetadata.identifierInformation.canonicIds.canonicId
                if ids:
                    return ids[0].id
        except Exception as err:
            _LOGGER.debug("Failed to extract canonical id from FCM response: %s", err)
        return None

    async def _run_callback_async(self, callback: Callable[[str, str], None], canonic_id: str, hex_string: str) -> None:
        """Run potentially blocking callback in a thread."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, callback, canonic_id, hex_string)

    async def _process_background_update(self, coordinator: Any, canonic_id: str, hex_string: str) -> None:
        """Decode and inject location into coordinator cache, then push-update entities."""
        try:
            location_data = await asyncio.get_event_loop().run_in_executor(
                None, self._decode_background_location, hex_string
            )
            if not location_data:
                _LOGGER.debug("No location data in background update for %s", canonic_id)
                return

            device_name = getattr(coordinator, "_device_names", {}).get(canonic_id, canonic_id[:8])

            # Optional Google Home filter
            semantic_name = location_data.get("semantic_name")
            ghf = getattr(coordinator, "google_home_filter", None)
            if semantic_name and ghf is not None:
                should_filter, replacement = ghf.should_filter_detection(canonic_id, semantic_name)
                if should_filter:
                    _LOGGER.debug("Filtered Google Home detection for %s", device_name)
                    return
                if replacement:
                    location_data = {**location_data, "semantic_name": replacement}
                    _LOGGER.info(
                        "Google Home filter: %s detected at '%s' -> using '%s'",
                        device_name,
                        semantic_name,
                        replacement,
                    )

            # De-duplicate by last_seen
            current_last_seen = location_data.get("last_seen")
            existing = getattr(coordinator, "_device_location_data", {}).get(canonic_id, {})
            if existing.get("last_seen") == current_last_seen:
                _LOGGER.debug("Duplicate background update for %s (last_seen=%s)", device_name, current_last_seen)
                coordinator.increment_stat("skipped_duplicates")
                return

            # Commit to coordinator cache via public API if available (encapsulation)
            slot = dict(location_data)
            slot["last_updated"] = time.time()

            update_cache = getattr(coordinator, "update_device_cache", None)
            if callable(update_cache):
                update_cache(canonic_id, slot)
            else:
                # Transitional fallback for older coordinators (to be removed once all callers updated)
                try:
                    coordinator._device_location_data[canonic_id] = slot  # noqa: SLF001
                    _LOGGER.debug(
                        "Fallback: wrote to coordinator._device_location_data directly (consider upgrading coordinator)"
                    )
                except Exception as err:
                    _LOGGER.error("Coordinator cache update failed for %s: %s", canonic_id, err)
                    return

            _LOGGER.info("Stored NEW background location update for %s (last_seen=%s)", device_name, current_last_seen)

            coordinator.increment_stat("background_updates")
            if location_data.get("is_own_report") is False:
                coordinator.increment_stat("crowd_sourced_updates")
                _LOGGER.info("Crowd-sourced update detected for %s via FCM", device_name)

            # Push entities immediately (no poll). Prefer dedicated push method if available.
            push = getattr(coordinator, "push_updated", None)
            if callable(push):
                push()
            else:
                await coordinator.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Error processing background update for %s: %s", canonic_id, err)

    def _decode_background_location(self, hex_string: str) -> dict:
        """Decode background location using our protobuf decoders (CPU-bound)."""
        try:
            from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf  # type: ignore
            from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (  # type: ignore
                decrypt_location_response_locations,
            )

            device_update = parse_device_update_protobuf(hex_string)
            locations = decrypt_location_response_locations(device_update) or []
            return locations[0] if locations else {}
        except Exception as err:
            _LOGGER.error("Failed to decode background location data: %s", err)
            return {}

    # -------------------- Credentials & stop --------------------

    def _on_credentials_updated(self, creds: Any) -> None:
        """Update in-memory creds and persist asynchronously."""
        normalized: Any = creds
        if isinstance(normalized, str):
            try:
                normalized = json.loads(normalized)
            except json.JSONDecodeError:
                _LOGGER.debug("FCM credentials arrived as non-JSON string; storing raw value.")
        self.credentials = normalized if isinstance(normalized, dict) else None
        asyncio.create_task(self._async_save_credentials())
        _LOGGER.info("FCM credentials updated")

    async def _async_save_credentials(self) -> None:
        try:
            await async_set_cached_value("fcm_credentials", self.credentials)
        except Exception as err:
            _LOGGER.error("Failed to save FCM credentials: %s", err)

    async def async_stop(self) -> None:
        """Stop the background listener and push client."""
        # Stop listener task
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            finally:
                self._listen_task = None
        self._listening = False

        # Stop push client
        if self.pc:
            try:
                await self.pc.stop()
            except (ConnectionError, TimeoutError) as err:
                _LOGGER.debug("FCM push client stop network error: %s", err)
            except Exception as err:
                _LOGGER.debug("FCM push client stop unexpected error: %s", err)
            finally:
                self.pc = None

        _LOGGER.info("FCM receiver stopped")

    # -------------------- Public token accessor --------------------

    def get_fcm_token(self) -> Optional[str]:
        """Return current FCM token if available (best-effort)."""
        creds = self.credentials
        if isinstance(creds, dict):
            token = (creds.get("fcm") or {}).get("registration", {}).get("token")
            if token:
                return token

        # Early-boot fallback: try sync cache
        try:
            cached: Any = get_cached_value("fcm_credentials")
            if isinstance(cached, str):
                cached = json.loads(cached)
            if isinstance(cached, dict):
                token = (cached.get("fcm") or {}).get("registration", {}).get("token")
                if token:
                    return token
        except json.JSONDecodeError:
            _LOGGER.debug("Cached FCM credentials are not valid JSON")
        except Exception:
            # final guard
            pass

        return None
