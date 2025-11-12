# custom_components/googlefindmy/Auth/fcm_receiver_ha.py
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

Push-path debouncing
--------------------
* Multiple FCM messages for the same device can arrive in bursts. To avoid churning the
  coordinator and entities, we **debounce per device** in this receiver:
    - `_pending[device_id]` holds the latest decoded payload.
    - `_schedule_flush(device_id)` (re)starts a short timer (default 250 ms).
    - `_flush(device_id)` fans the single coalesced payload out to **all** registered
      coordinators (per-coordinator Google Home filtering is applied here).
* The receiver remains thin: significance gating and cooldown application are the
  coordinator’s responsibility.

Runtime telemetry (for diagnostics)
-----------------------------------
* `last_start_monotonic`: monotonic timestamp just before the client (re)starts.
* `last_stop_monotonic`: monotonic timestamp at the end of `async_stop()`.
* `start_count`: number of times the client was started by the supervisor.
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
        OPT_IGNORED_DEVICES,  # for ignore fallback via options
    )
except Exception:  # pragma: no cover
    FCM_CLIENT_HEARTBEAT_INTERVAL_S = 20
    FCM_SERVER_HEARTBEAT_INTERVAL_S = 10
    FCM_IDLE_RESET_AFTER_S = 90.0
    FCM_CONNECTION_RETRY_COUNT = 5
    FCM_MONITOR_INTERVAL_S = 1.0
    FCM_ABORT_ON_SEQ_ERROR_COUNT = 3
    DOMAIN = "googlefindmy"
    OPT_IGNORED_DEVICES = "ignored_devices"

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
        * `async_stop()` gracefully shuts down the supervisor and client with a bounded timeout.
        * `request_stop()` can be used to signal stop without awaiting (safe for `async_on_unload`).
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

        # Cooperative stop signal for bounded shutdown
        self._stop_evt: asyncio.Event = asyncio.Event()

        # Firebase project configuration for Google Find My Device
        self.project_id = "google.com:api-project-289722593072"
        self.app_id = "1:289722593072:android:3cfcf5bc359f0308"
        self.api_key = "AIzaSyD_gko3P392v6how2H7UpdeXQ0v2HLettc"
        self.message_sender_id = "289722593072"

        # ---------------- Debounce state (push path) ----------------
        # Latest pending payload per device; flushed after short debounce.
        self._pending: dict[str, dict] = {}
        # Flush tasks per device (cancel/recreate on new updates).
        self._flush_tasks: dict[str, asyncio.Task] = {}
        # Debounce window in milliseconds (small enough to feel real-time).
        self._debounce_ms: int = 250

        # ---------------- Telemetry (diagnostics) -------------------
        # Set/updated by the supervisor loop and async_stop().
        self.last_start_monotonic: float = 0.0
        self.last_stop_monotonic: float = 0.0
        self.start_count: int = 0

    # -------------------- Convenience readiness --------------------

    @property
    def is_ready(self) -> bool:
        """Best-effort readiness signal for quick checks/diagnostics."""
        pc = self.pc
        if not (self._listening and pc):
            return False
        state = getattr(pc, "run_state", None)
        do_listen = getattr(pc, "do_listen", False)
        if FcmPushClientRunState is not None:
            return bool(state == FcmPushClientRunState.STARTED and do_listen)
        # Fallback if enum is unavailable
        return bool(do_listen)

    # Alias sometimes used by callers
    ready = is_ready

    # -------------------- Lifecycle --------------------

    async def async_initialize(self, entry_id: Optional[str] = None) -> bool:
        """Initialize receiver and underlying push client (idempotent).

        Args:
            entry_id: Optional config entry ID to use for cache access in multi-account setups.
                     If not provided, will attempt to use the first available entry.

        Returns:
            True if the receiver is ready to start; False on a non-fatal failure.
        """
        # Load FCM credentials from the async TokenCache (string or dict supported).
        # In multi-account mode, we need to get the specific TokenCache instance
        from custom_components.googlefindmy.Auth.token_cache import _INSTANCES

        try:
            if entry_id and entry_id in _INSTANCES:
                # Use the specified entry's cache
                cache = _INSTANCES[entry_id]
                creds: Any = await cache.get("fcm_credentials")
            elif _INSTANCES:
                # Use the first available entry's credentials
                first_entry_id = next(iter(_INSTANCES.keys()))
                cache = _INSTANCES[first_entry_id]
                creds = await cache.get("fcm_credentials")
                _LOGGER.debug("Multi-account mode: using FCM credentials from entry %s", first_entry_id)
            else:
                _LOGGER.error("No config entries available for FCM credentials")
                return False
        except Exception as err:
            _LOGGER.error("Failed to load FCM credentials: %s", err)
            return False
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
        self._stop_evt.clear()
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
            while self._listening and not self._stop_evt.is_set():
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

                # ---- Telemetry: mark a (re)start event before client.start() ----
                self.last_start_monotonic = time.monotonic()
                self.start_count += 1

                # Start client (non-blocking; it launches internal tasks) and monitor
                try:
                    await self.pc.start()
                    _LOGGER.debug("FCM client started; entering monitor loop")
                except Exception as err:
                    _LOGGER.info("FCM client failed to start: %s", err)

                # Reset backoff after a successful start
                backoff = 1.0

                # Monitor until client stops or supervisor asked to stop
                while self._listening and not self._stop_evt.is_set() and self.pc:
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

                if self._listening and not self._stop_evt.is_set():
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

            # Check if any coordinator would process this device (ignore-aware).
            any_tracked = False
            for coordinator in self.coordinators.copy():
                if self._is_tracked(coordinator, canonic_id):
                    any_tracked = True
                else:
                    _LOGGER.debug("Skipping FCM update for ignored device %s", canonic_id[:8])

            if not any_tracked:
                # None of the registered coordinators will accept this device → drop.
                _LOGGER.debug("No registered coordinator will process %s; dropping FCM update", canonic_id[:8])
                return

            # Decode + enqueue; per-coordinator filtering and cache updates happen on flush.
            asyncio.create_task(self._process_background_update(canonic_id, hex_string))

        except Exception as err:  # noqa: BLE001
            # Final guard to avoid crashing the receiver callback
            _LOGGER.error("Error processing FCM notification: %s", err)

    # -------------------- Helpers --------------------

    @staticmethod
    def _norm(dev_id: str) -> str:
        """Normalize a device id for equality checks."""
        return (dev_id or "").replace("-", "").lower()

    def _is_tracked(self, coordinator: Any, canonic_id: str) -> bool:
        """Return True if device is eligible for push processing.

        Semantics:
            * `tracked_devices` only limits polling, not push/discovery, to ensure
              newly added devices get live updates immediately.
            * Ignored devices are filtered out here to prevent any further processing.
        """
        # 1) Early exit for ignored devices, using the coordinator's API if available.
        # This is the primary mechanism to respect a user's choice to delete a device.
        try:
            is_ignored_fn = getattr(coordinator, "is_ignored", None)
            if callable(is_ignored_fn) and is_ignored_fn(canonic_id):
                return False
        except Exception:
            # defensive fallback continues below
            pass

        # 2) Fallback check directly against config entry options.
        # This ensures the ignore logic still works even if the coordinator
        # instance is not fully updated yet.
        try:
            entry = getattr(coordinator, "config_entry", None)
            if entry is not None:
                ignored = entry.options.get(OPT_IGNORED_DEVICES, [])
                if isinstance(ignored, list) and canonic_id in ignored:
                    return False
        except Exception:
            pass

        # 3) Default to processing the push update.
        return True

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

    # -------------------- Push-path decode → debounce → flush --------------------

    async def _process_background_update(self, canonic_id: str, hex_string: str) -> None:
        """Decode location, enqueue for debounce, and schedule a flush.

        This method performs CPU-bound protobuf decryption using the async API,
        which maintains cache context for multi-account support.
        Enriches diagnostics, and then **does not** touch coordinator state
        directly. Instead it stores the latest payload in `_pending` and triggers
        a debounced flush for the corresponding device id.
        """
        try:
            # Find all coordinators that track this device
            # In multi-account mode, multiple coordinators might track devices with similar IDs
            # We need to try them all until one successfully decrypts
            tracking_coordinators = [
                coord for coord in self.coordinators.copy()
                if self._is_tracked(coord, canonic_id)
            ]

            if not tracking_coordinators:
                _LOGGER.debug("No coordinator tracks device %s", canonic_id[:8])
                return

            location_data = None
            last_error = None

            # Try each coordinator until one succeeds
            for coordinator in tracking_coordinators:
                try:
                    location_data = await self._decode_background_location_async(hex_string, coordinator)
                    if location_data:
                        break  # Success!
                except Exception as coord_err:
                    last_error = coord_err
                    # Log at debug level to avoid spam in multi-account setups
                    _LOGGER.debug(
                        "Decrypt attempt failed for %s (trying next coordinator if available): %s",
                        canonic_id[:8],
                        str(coord_err)[:100]
                    )
                    continue  # Try next coordinator

            if not location_data:
                # Only log if we actually tried coordinators
                if last_error and len(tracking_coordinators) > 1:
                    _LOGGER.debug(
                        "Failed to decrypt FCM update for %s with %d coordinator(s)",
                        canonic_id[:8],
                        len(tracking_coordinators)
                    )
                elif last_error:
                    _LOGGER.error(
                        "Failed to decrypt FCM update for %s: %s",
                        canonic_id[:8],
                        str(last_error)[:200]
                    )
                return

            # Enrich with a wall-clock 'last_updated' for UX parity with poll path.
            payload = dict(location_data)
            payload.setdefault("last_updated", time.time())

            # Replace any older pending payload for this device with the newest one.
            self._pending[canonic_id] = payload
            self._schedule_flush(canonic_id)

        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error processing background update for %s: %s", canonic_id, err)

    def _schedule_flush(self, device_id: str) -> None:
        """(Re)schedule a short debounce before fanning out updates to coordinators.

        Implementation details:
        - Cancels any running flush timer for this device.
        - Starts a new timer task that waits `_debounce_ms` and then calls `_flush()`.
        - Uses asyncio.create_task because the receiver is managed on the HA loop.
        """
        # Cancel any existing scheduled flush for this device
        existing = self._flush_tasks.pop(device_id, None)
        if existing and not existing.done():
            existing.cancel()

        async def _delayed() -> None:
            try:
                await asyncio.sleep(self._debounce_ms / 1000.0)
                await self._flush(device_id)
            except asyncio.CancelledError:
                # Expected when new updates arrive within the debounce window.
                return
            except Exception as err:
                _LOGGER.error("Flush task for %s failed: %s", device_id, err)

        task = asyncio.create_task(_delayed(), name=f"{DOMAIN}.fcm_flush[{device_id[:8]}]")
        self._flush_tasks[device_id] = task

    async def _flush(self, device_id: str) -> None:
        """Flush the latest pending payload to **all** registered coordinators.

        Per-coordinator steps:
          1) Skip if coordinator would ignore the device.
          2) Apply that coordinator's Google Home filter (if available):
             - If `should_filter` → drop for this coordinator only.
             - If replacement attributes are provided → substitute coordinates and
               clear `semantic_name` (so HA Core's zone engine drives state).
          3) Call `update_device_cache(device_id, payload)` on the coordinator.
          4) Push a minimal snapshot via `push_updated([device_id])` when available,
             otherwise fall back to `async_request_refresh()`.

        Notes:
            * Significance gating and type-aware cooldowns are applied **inside**
              the coordinator (`update_device_cache`), not here.
            * We do not strip internal `_report_hint`; the coordinator will.
        """
        payload = self._pending.pop(device_id, None)
        # Remove the stored task (it is this method's responsibility to clear it).
        self._flush_tasks.pop(device_id, None)

        if not payload:
            return

        # Iterate over a copy to avoid mutation issues if a coordinator unregisters mid-flight.
        for coordinator in self.coordinators.copy():
            try:
                if not self._is_tracked(coordinator, device_id):
                    continue

                # Prepare a per-coordinator copy (filters may mutate the payload)
                coordinator_payload = dict(payload)

                # Apply Google Home filter per coordinator (if available)
                semantic_name = coordinator_payload.get("semantic_name")
                ghf = getattr(coordinator, "google_home_filter", None)
                if semantic_name and ghf is not None:
                    try:
                        should_filter, replacement_attrs = ghf.should_filter_detection(device_id, semantic_name)
                    except Exception as gf_err:
                        _LOGGER.debug("Google Home filter error for %s: %s", device_id[:8], gf_err)
                        should_filter, replacement_attrs = False, None

                    if should_filter:
                        _LOGGER.debug("Filtered Google Home detection for %s (push path)", device_id[:8])
                        # Skip this coordinator only
                        continue

                    if replacement_attrs:
                        # Substitute coordinates; derive accuracy from radius if available
                        if "latitude" in replacement_attrs and "longitude" in replacement_attrs:
                            coordinator_payload["latitude"] = replacement_attrs.get("latitude")
                            coordinator_payload["longitude"] = replacement_attrs.get("longitude")
                        if "radius" in replacement_attrs and replacement_attrs.get("radius") is not None:
                            coordinator_payload["accuracy"] = replacement_attrs.get("radius")
                        # Clear semantic_name so HA zone engine determines final state
                        coordinator_payload["semantic_name"] = None

                # Commit to coordinator cache via its public API
                update_cache = getattr(coordinator, "update_device_cache", None)
                if callable(update_cache):
                    update_cache(device_id, coordinator_payload)
                else:
                    # Transitional fallback for older coordinators (to be removed once all callers updated)
                    try:
                        coordinator._device_location_data[device_id] = coordinator_payload  # noqa: SLF001
                        _LOGGER.debug(
                            "Fallback: wrote to coordinator._device_location_data directly (consider upgrading coordinator)"
                        )
                        coordinator.increment_stat("background_updates")
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.error("Coordinator cache update failed for %s: %s", device_id, err)
                        continue

                # Crowd-sourced classification remains useful for stats visibility.
                if coordinator_payload.get("is_own_report") is False:
                    try:
                        coordinator.increment_stat("crowd_sourced_updates")
                    except Exception:
                        pass

                # Push entities immediately (no poll). Prefer dedicated push method if available.
                push = getattr(coordinator, "push_updated", None)
                if callable(push):
                    # Push a minimal snapshot to reduce UI churn and work.
                    push([device_id])
                else:
                    await coordinator.async_request_refresh()

            except Exception as err:
                _LOGGER.debug("Failed to fan-out push update for %s to one coordinator: %s", device_id[:8], err)

    # -------------------- Decode helper --------------------

    async def _decode_background_location_async(self, hex_string: str, coordinator: Any = None) -> dict:
        """Decode background location using async API (maintains cache context).

        Args:
            hex_string: The hex-encoded protobuf data
            coordinator: The coordinator instance that owns this device (provides cache context)

        Implementation detail:
        - Uses the **async** `async_decrypt_location_response_locations(...)` which
          maintains the cache context for multi-account support.
        - CPU-bound crypto work is offloaded to executor within the async API.
        - The decoder layer is responsible for:
            * fail-fast validation of coordinates,
            * attaching an internal `_report_hint` based on report type (if known),
            * returning a normalized payload dictionary suitable for HA entities.
        """
        try:
            from custom_components.googlefindmy.ProtoDecoders.decoder import parse_device_update_protobuf  # type: ignore
            from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (  # type: ignore
                async_decrypt_location_response_locations,
            )
            from custom_components.googlefindmy.NovaApi import nova_request

            # Parse in current thread (lightweight)
            device_update = parse_device_update_protobuf(hex_string)

            # Register cache provider for multi-account support
            if coordinator and hasattr(coordinator, '_cache'):
                cache = coordinator._cache
                nova_request.register_cache_provider(lambda: cache)
                try:
                    # Decrypt async (maintains cache context, offloads CPU work)
                    locations = await async_decrypt_location_response_locations(device_update) or []
                    return locations[0] if locations else {}
                finally:
                    nova_request.unregister_cache_provider()
            else:
                # Fallback for single-account or if coordinator not available
                locations = await async_decrypt_location_response_locations(device_update) or []
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
        """Persist current credentials to the async TokenCache.

        In multi-account mode, FCM credentials are device-level (not account-level),
        so we save them to all entries' caches.
        """
        try:
            # Try to save to all entries in multi-account mode
            from custom_components.googlefindmy.Auth.token_cache import _INSTANCES
            if len(_INSTANCES) > 1:
                # Multi-account mode: save to all entries by getting each cache instance
                for entry_id in _INSTANCES.keys():
                    try:
                        cache = _INSTANCES[entry_id]
                        await cache.set("fcm_credentials", self.credentials)
                    except Exception as err2:
                        _LOGGER.debug("Failed to save FCM credentials to entry %s: %s", entry_id, err2)
                _LOGGER.debug("Saved FCM credentials to %d entries", len(_INSTANCES))
            else:
                # Single account mode: use the facade function
                await async_set_cached_value("fcm_credentials", self.credentials)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to save FCM credentials: %s", err)

    def request_stop(self) -> None:
        """Signal a cooperative stop without awaiting.

        Notes:
            This is safe to call from `ConfigEntry.async_on_unload(...)` because it does
            not block. It merely flips internal flags and cancels the supervisor task;
            the caller can later await `async_stop()` during `async_unload_entry`.
        """
        if self._listening:
            self._listening = False
        self._stop_evt.set()
        if self._listen_task:
            self._listen_task.cancel()

    async def async_stop(self, timeout: float = 5.0) -> None:
        """Stop the supervisor and push client (graceful, bounded by `timeout` seconds)."""
        # Stop supervisor loop
        if self._listening:
            self._listening = False
        self._stop_evt.set()

        # Stop background supervisor task (bounded wait)
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await asyncio.wait_for(self._listen_task, timeout=timeout)
            except asyncio.TimeoutError:
                _LOGGER.warning("FCM supervisor did not stop within %.1fs; detaching", timeout)
            except asyncio.CancelledError:
                pass
            finally:
                self._listen_task = None

        # Stop push client (if still present) with bounded wait
        if self.pc:
            try:
                await asyncio.wait_for(self.pc.stop(), timeout=timeout)
            except asyncio.TimeoutError:
                _LOGGER.warning("FCM push client did not stop within %.1fs; detaching", timeout)
            except (ConnectionError, TimeoutError) as err:
                _LOGGER.debug("FCM push client stop network error: %s", err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("FCM push client stop unexpected error: %s", err)
            finally:
                self.pc = None

        # ---- Telemetry: mark last stop moment for diagnostics ----
        self.last_stop_monotonic = time.monotonic()

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
