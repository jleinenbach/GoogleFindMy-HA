"""Home Assistant compatible FCM receiver for Google Find My Device.

This module provides an HA-integrated Firebase Cloud Messaging (FCM) receiver that:
- Runs fully async with a supervised background loop (start/monitor/restart).
- Persists credentials via the integration's TokenCache (HA Store-backed).
- Notifies registered request callbacks or pushes background updates to the coordinator.
- Avoids any synchronous cache access in the event loop.

Design notes
------------
* Lifecycle: A single shared receiver instance is managed in `hass.data[DOMAIN]`.
* No global singletons in this module; Home Assistant orchestrates creation/cleanup.
* The receiver never triggers UI or ChromeDriver flows; it only consumes credentials
  from the cache and updates them when the server requests re-registration.
* All potentially blocking work (protobuf decoding, user callbacks) runs in executors.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import random
import time
from typing import Any, Callable, Optional

from custom_components.googlefindmy.Auth.token_cache import (
    async_get_cached_value,
    async_set_cached_value,
)

# Integration-level tunables (safe fallbacks if missing)
try:
    from custom_components.googlefindmy.const import (  # type: ignore
        FCM_CLIENT_HEARTBEAT_INTERVAL_S,
        FCM_SERVER_HEARTBEAT_INTERVAL_S,
        FCM_IDLE_RESET_AFTER_S,
        FCM_CONNECTION_RETRY_COUNT,
        FCM_MONITOR_INTERVAL_S,
        FCM_ABORT_ON_SEQ_ERROR_COUNT,
        DOMAIN,
    )
except Exception:  # pragma: no cover
    FCM_CLIENT_HEARTBEAT_INTERVAL_S = 20
    FCM_SERVER_HEARTBEAT_INTERVAL_S = 10
    FCM_IDLE_RESET_AFTER_S = 90.0
    FCM_CONNECTION_RETRY_COUNT = 5
    FCM_MONITOR_INTERVAL_S = 1.0
    FCM_ABORT_ON_SEQ_ERROR_COUNT = 3
    DOMAIN = "googlefindmy"

# Optional import of worker run-state enum (for robust state checks)
try:
    from custom_components.googlefindmy.Auth.firebase_messaging import (  # type: ignore
        FcmPushClientRunState,
    )
except Exception:  # pragma: no cover
    FcmPushClientRunState = None  # type: ignore[misc,assignment]

_LOGGER = logging.getLogger(__name__)


class FcmReceiverHA:
    """FCM receiver integrated with Home Assistant's async lifecycle.

    Key responsibilities:
        * Initialize and supervise the FCM push client.
        * Handle per-request callbacks for specific devices.
        * Fan out background updates to a coordinator.
        * Persist credential updates to the TokenCache.

    Contract:
        * `async_initialize()` must be called before use.
        * `register_coordinator()` / `unregister_coordinator()` are synchronous by design
          to match HA's `async_on_unload` contract.
        * `async_stop()` gracefully shuts down the supervisor and client.
    """

    def __init__(self) -> None:
        self.credentials: Optional[dict] = None
        # Per-request callbacks waiting for a specific device response
        self.location_update_callbacks: dict[str, Callable[[str, str], None]] = {}
        # Coordinators eligible to receive background updates
        self.coordinators: list[Any] = []

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
        """Initialize receiver and underlying push client (idempotent).

        Returns:
            True if the receiver is ready to start; False on a non-fatal failure.
        """
        # Load FCM credentials from the async TokenCache (string or dict supported).
        creds: Any = await async_get_cached_value("fcm_credentials")
        if isinstance(creds, str):
            try:
                creds = json.loads(creds)
            except json.JSONDecodeError as exc:
                _LOGGER.error("Failed to parse FCM credentials JSON: %s", exc)
                return False
        self.credentials = creds if isinstance(creds, dict) else None

        # Lazy import to avoid heavy deps at import time
        try:
            from custom_components.googlefindmy.Auth.firebase_messaging import (  # type: ignore
                FcmPushClient,
                FcmRegisterConfig,
                FcmPushClientConfig,
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

        # Wire integration-level tunables into the client config
        client_cfg = FcmPushClientConfig(
            client_heartbeat_interval=int(FCM_CLIENT_HEARTBEAT_INTERVAL_S),
            server_heartbeat_interval=int(FCM_SERVER_HEARTBEAT_INTERVAL_S),
            idle_reset_after=float(FCM_IDLE_RESET_AFTER_S),
            connection_retry_count=int(FCM_CONNECTION_RETRY_COUNT),
            monitor_interval=float(FCM_MONITOR_INTERVAL_S),  # accepted by worker config
            abort_on_sequential_error_count=int(FCM_ABORT_ON_SEQ_ERROR_COUNT),
        )

        try:
            self.pc = FcmPushClient(
                self._on_notification,
                fcm_config,
                self.credentials,
                self._on_credentials_updated,
                config=client_cfg,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to construct FCM push client: %s", err)
            return False

        _LOGGER.info(
            "FCM receiver initialized (client_hb=%ss, server_hb=%ss, idle_reset=%ss, retries=%d)",
            client_cfg.client_heartbeat_interval,
            client_cfg.server_heartbeat_interval,
            client_cfg.idle_reset_after,
            client_cfg.connection_retry_count,
        )
        return True

    async def async_register_for_location_updates(
        self, device_id: str, callback: Callable[[str, str], None]
    ) -> Optional[str]:
        """Register a per-request callback for a device and ensure listener is running.

        Args:
            device_id: Canonical device identifier.
            callback: Sync callback to invoke with (canonic_id, hex_payload).

        Returns:
            The current FCM registration token if available, otherwise None.
        """
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

    # -------------------- Internal listening & supervision --------------------

    async def _start_listening(self) -> None:
        """Start supervisor loop if not already running."""
        if self._listening:
            return
        self._listening = True
        # Start background supervisor task
        self._listen_task = asyncio.create_task(
            self._supervisor_loop(), name="googlefindmy.fcm_supervisor"
        )
        _LOGGER.info("Started FCM supervisor")

    async def _register_for_fcm(self) -> bool:
        """Single registration attempt; let the supervisor handle retries/backoff."""
        if not self.pc:
            return False
        try:
            token = await self.pc.checkin_or_register()
            if token:
                _LOGGER.info("FCM registered, token: %s…", str(token)[:20])
                return True
            _LOGGER.warning("FCM registration returned no token")
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("FCM registration error: %s", err)
            return False

    async def _supervisor_loop(self) -> None:
        """Supervise FCM client: start, monitor, restart on fatal stop."""
        backoff = 1.0  # seconds, exponential with cap
        try:
            while self._listening:
                # (Re)initialize client if needed
                if not self.pc:
                    ok = await self.async_initialize()
                    if not ok:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60.0)
                        continue

                # Ensure we have credentials/token (single attempt)
                ok_reg = await self._register_for_fcm()
                if not ok_reg:
                    # Cleanup before retrying
                    if self.pc:
                        try:
                            await self.pc.stop()
                        except Exception:
                            pass
                        finally:
                            self.pc = None
                    delay = backoff + random.uniform(0.1, 0.3) * backoff
                    _LOGGER.info("Re-trying FCM registration in %.1fs", delay)
                    await asyncio.sleep(delay)
                    backoff = min(backoff * 2, 60.0)
                    continue

                # Start client (non-blocking; it launches internal tasks) and monitor
                try:
                    await self.pc.start()
                    _LOGGER.debug("FCM client started; entering monitor loop")
                except Exception as err:
                    _LOGGER.info("FCM client failed to start: %s", err)

                # Reset backoff after a successful start
                backoff = 1.0

                # Monitor until client stops or supervisor asked to stop
                while self._listening and self.pc:
                    await asyncio.sleep(max(FCM_MONITOR_INTERVAL_S, 0.5))
                    # Check run_state/do_listen heuristics
                    state = getattr(self.pc, "run_state", None)
                    do_listen = getattr(self.pc, "do_listen", False)

                    if state is None:
                        _LOGGER.info("FCM client state unknown; scheduling restart")
                        break

                    # Robust enum-based check (fallback to do_listen if enum absent)
                    if (
                        (FcmPushClientRunState is not None and state in (FcmPushClientRunState.STOPPING, FcmPushClientRunState.STOPPED))
                        or not do_listen
                    ):
                        _LOGGER.info("FCM client stopped; scheduling restart")
                        break

                # Cleanup before restart
                if self.pc:
                    try:
                        await self.pc.stop()
                    except Exception:
                        pass
                    finally:
                        # Recreate the client on next loop to clear any bad state
                        self.pc = None

                if self._listening:
                    # Backoff with light jitter
                    delay = backoff + random.uniform(0.1, 0.3) * backoff
                    _LOGGER.info("Restarting FCM client in %.1fs", delay)
                    await asyncio.sleep(delay)
                    backoff = min(backoff * 2, 60.0)

        except asyncio.CancelledError:
            _LOGGER.debug("FCM supervisor cancelled")
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("FCM supervisor crashed: %s", err)
        finally:
            self._listening = False
            _LOGGER.info("FCM supervisor stopped")

    # -------------------- Incoming notifications --------------------

    def _on_notification(self, obj: dict[str, Any], notification, data_message) -> None:
        """Handle incoming FCM notification (sync callback from client).

        Args:
            obj: Envelope object from the FCM client (expected to hold the data dict).
            notification: Unused; provided by the client.
            data_message: Unused; provided by the client.
        """
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

        except Exception as err:  # noqa: BLE001
            # Final guard to avoid crashing the receiver callback
            _LOGGER.error("Error processing FCM notification: %s", err)

    # -------------------- Helpers --------------------

    @staticmethod
    def _norm(dev_id: str) -> str:
        """Normalize a device id for equality checks."""
        return (dev_id or "").replace("-", "").lower()

    def _is_tracked(self, coordinator: Any, canonic_id: str) -> bool:
        """Return True if device is tracked by the coordinator.

        Semantics: empty tracked_devices => track ALL devices.
        """
        tracked = getattr(coordinator, "tracked_devices", []) or []
        if not tracked:
            return True
        nid = self._norm(canonic_id)
        return any(self._norm(did) == nid for did in tracked)

    def _extract_canonic_id_from_response(self, hex_response: str) -> Optional[str]:
        """Extract canonical id via the decoder.

        Args:
            hex_response: Hex-encoded protobuf payload.

        Returns:
            Canonical device id, or None if not present.
        """
        try:
            from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf  # type: ignore

            device_update = parse_device_update_protobuf(hex_response)
            if device_update.HasField("deviceMetadata"):
                ids = device_update.deviceMetadata.identifierInformation.canonicIds.canonicId
                if ids:
                    return ids[0].id
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to extract canonical id from FCM response: %s", err)
        return None

    async def _run_callback_async(
        self, callback: Callable[[str, str], None], canonic_id: str, hex_string: str
    ) -> None:
        """Run a potentially blocking callback in a thread."""
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
                except Exception as err:  # noqa: BLE001
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

        except Exception as err:  # noqa: BLE001
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
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to decode background location data: %s", err)
            return {}

    # -------------------- Credentials & stop --------------------

    def _on_credentials_updated(self, creds: Any) -> None:
        """Update in-memory creds and persist asynchronously.

        The FCM library calls this when registration produces new credentials or
        when the server demands re-registration.
        """
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
        """Persist current credentials to the async TokenCache."""
        try:
            await async_set_cached_value("fcm_credentials", self.credentials)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to save FCM credentials: %s", err)

    async def async_stop(self) -> None:
        """Stop the supervisor and push client (graceful)."""
        # Stop supervisor loop
        if self._listening:
            self._listening = False

        # Stop background supervisor task
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            finally:
                self._listen_task = None

        # Stop push client (if still present)
        if self.pc:
            try:
                await self.pc.stop()
            except (ConnectionError, TimeoutError) as err:
                _LOGGER.debug("FCM push client stop network error: %s", err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("FCM push client stop unexpected error: %s", err)
            finally:
                self.pc = None

        _LOGGER.info("FCM receiver stopped")

    # -------------------- Public token accessor --------------------

    def get_fcm_token(self) -> Optional[str]:
        """Return current FCM token if available (best-effort).

        Notes:
            This accessor is synchronous by contract (used in mixed sync/async call sites).
            It does NOT read from the TokenCache synchronously to avoid event-loop deadlocks.
            Callers should rely on `async_initialize()` having loaded credentials from the cache.
        """
        creds = self.credentials
        if isinstance(creds, dict):
            token = (creds.get("fcm") or {}).get("registration", {}).get("token")
            if token:
                return token
        return None
