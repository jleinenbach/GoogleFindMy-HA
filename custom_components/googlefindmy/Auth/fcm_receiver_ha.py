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

Multi-account preparation (entry-scoped)
----------------------------------------
* Credentials are **written to each entry's TokenCache** when available; global
  cache falls back only if no coordinator is registered yet (cold start).
* FCM token/endpoint → entry routing scaffold:
  - Map the active registration **token → set(entry_id)**.
  - On incoming push, prefer routing to the subset of coordinators whose entry_id
    is mapped from the push envelope's target token; fall back to all on ambiguity.
* Backwards compatible: with a single active entry, behavior is unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import random
import time
from typing import Any, Callable, Optional, Set

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
        * Persist credential updates to the TokenCache (entry-scoped when possible).

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
        self.message_sender_id = "289722593072"  # numeric Sender ID (project number)

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

        # ---------------- Multi-account routing scaffold -------------
        # Map FCM token -> set of entry_ids that currently use this token.
        # Note: With a shared receiver instance there is typically one token.
        self._token_to_entries: dict[str, Set[str]] = {}

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

    async def async_initialize(self) -> bool:
        """Initialize receiver and underlying push client (idempotent).

        Returns:
            True if the receiver is ready to start; False on a non-fatal failure.

        Persistence strategy:
            * If no coordinator is registered yet (cold start), read credentials from
              the legacy/global async cache (back-compat).
            * As soon as coordinators register, credentials are **written** to each
              entry's TokenCache and subsequent updates keep them in sync.
        """
        # Load FCM credentials from the async TokenCache (string or dict supported).
        # Cold-start path: use legacy/global cache; when a coordinator registers we will
        # mirror credentials into the entry-scoped caches.
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
            messaging_sender_id=self.message_sender_id,  # numeric sender id required
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
        if token:
            self._update_token_routing(token)  # keep routing table in sync
        else:
            _LOGGER.warning("FCM credentials/token not available after registration")
        return token

    async def async_unregister_for_location_updates(self, device_id: str) -> None:
        """Remove a per-request callback for a device."""
        self.location_update_callbacks.pop(device_id, None)
        _LOGGER.debug("Unregistered FCM callback for device: %s", device_id)

    # -------------------- Coordinator wiring (sync by contract) --------------------

    def register_coordinator(self, coordinator: Any) -> None:
        """Register a coordinator to receive background location updates.

        Side effects:
            * Adds coordinator to the fan-out list.
            * If credentials/token are available, mirror them into this entry's TokenCache
              and update the token→entry routing table.
        """
        if coordinator not in self.coordinators:
            self.coordinators.append(coordinator)
            _LOGGER.debug("Coordinator registered (total=%d)", len(self.coordinators))

        # Best-effort: persist current credentials to this entry's cache
        try:
            cache = getattr(coordinator, "cache", None)
            if cache is not None and self.credentials is not None:
                asyncio.create_task(cache.set("fcm_credentials", self.credentials))
        except Exception as err:
            _LOGGER.debug("Entry-scoped credentials persistence skipped: %s", err)

        # Update token routing table with the new coordinator's entry_id if token exists
        token = self.get_fcm_token()
        if token:
            self._update_token_routing(token)

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
            token_or_creds = await self.pc.checkin_or_register()
            if token_or_creds:
                # The client typically returns a credentials dict; keep previous log format concise.
                _LOGGER.info("FCM registered successfully")
                # Ensure our in-memory credentials are current (client will also call _on_credentials_updated)
                # but in case the client doesn't, try to read token now and update routing.
                token = self.get_fcm_token()
                if token:
                    self._update_token_routing(token)
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

            # Determine most-specific coordinator subset by push target token (if present)
            token = self._extract_push_token(obj)
            target_coordinators = self._coordinators_for_token(token)

            # Direct per-request callback bypasses fan-out when available
            cb = self.location_update_callbacks.get(canonic_id)
            if cb:
                asyncio.create_task(self._run_callback_async(cb, canonic_id, hex_string))
                return

            # Check if any chosen coordinator would process this device (ignore-aware).
            any_tracked = False
            for coordinator in target_coordinators:
                if self._is_tracked(coordinator, canonic_id):
                    any_tracked = True
                else:
                    _LOGGER.debug("Skipping FCM update for ignored device %s", canonic_id[:8])

            if not any_tracked:
                _LOGGER.debug(
                    "No registered coordinator will process %s (token=%s); dropping FCM update",
                    canonic_id[:8],
                    (token[:8] + "…") if token else "n/a",
                )
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

    @staticmethod
    def _extract_push_token(envelope: dict[str, Any]) -> Optional[str]:
        """Extract the push target token from the envelope (best-effort).

        The FCM library typically includes a `to` field with the registration token.
        We also check for common alternates to be robust across library versions.
        """
        try:
            token = envelope.get("to") or envelope.get("token")
            if not token and isinstance(envelope.get("message"), dict):
                token = envelope["message"].get("token")
            if isinstance(token, str) and token:
                return token
        except Exception:
            pass
        return None

    def _coordinators_for_token(self, token: Optional[str]) -> list[Any]:
        """Return the best coordinator subset for a given push token.

        Strategy:
            - If `token` is known in `_token_to_entries`, select only coordinators
              whose `config_entry.entry_id` is listed.
            - Otherwise, fall back to **all** registered coordinators (single-entry-safe).
        """
        if not token:
            return self.coordinators.copy()
        entry_ids = self._token_to_entries.get(token)
        if not entry_ids:
            return self.coordinators.copy()
        res: list[Any] = []
        for c in self.coordinators:
            try:
                entry = getattr(c, "config_entry", None)
                if entry is not None and entry.entry_id in entry_ids:
                    res.append(c)
            except Exception:
                # defensive: include coordinator to avoid dropping data
                res.append(c)
        return res or self.coordinators.copy()

    def _update_token_routing(self, token: str) -> None:
        """Update the token→entry mapping using currently registered coordinators.

        This keeps routing precise even if multiple entries are active in the future.
        """
        try:
            if not isinstance(token, str) or not token:
                return
            entry_ids: Set[str] = set()
            for c in self.coordinators:
                entry = getattr(c, "config_entry", None)
                if entry is not None and getattr(entry, "entry_id", None):
                    entry_ids.add(entry.entry_id)
            if entry_ids:
                prev = self._token_to_entries.get(token)
                self._token_to_entries[token] = entry_ids
                if prev != entry_ids:
                    _LOGGER.debug("Updated FCM token routing: token=%s… -> %s", token[:8], ",".join(sorted(entry_ids)))
        except Exception as err:
            _LOGGER.debug("Token routing update skipped: %s", err)

    async def _run_callback_async(
        self, callback: Callable[[str, str], None], canonic_id: str, hex_string: str
    ) -> None:
        """Run a potentially blocking callback in a thread."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, callback, canonic_id, hex_string)

    # -------------------- Push-path decode → debounce → flush --------------------

    async def _process_background_update(self, canonic_id: str, hex_string: str) -> None:
        """Decode location, enqueue for debounce, and schedule a flush.

        This method performs CPU-bound protobuf decryption in a thread executor,
        enriches diagnostics, and then **does not** touch coordinator state
        directly. Instead it stores the latest payload in `_pending` and triggers
        a debounced flush for the corresponding device id.
        """
        try:
            location_data = await asyncio.get_event_loop().run_in_executor(
                None, self._decode_background_location, hex_string
            )
            if not location_data:
                _LOGGER.debug("No location data in background update for %s", canonic_id)
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

    def _decode_background_location(self, hex_string: str) -> dict:
        """Decode background location using our protobuf decoders (CPU-bound).

        Implementation detail:
        - Calls the **sync** facade `decrypt_location_response_locations(...)` from a worker
          thread via `run_in_executor` (see caller). This guarantees we never call the
          sync facade while a running event loop exists in the same thread (see its contract).
        - The decoder layer is responsible for:
            * fail-fast validation of coordinates,
            * attaching an internal `_report_hint` based on report type (if known),
            * returning a normalized payload dictionary suitable for HA entities.
        """
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

        # Update token routing from the fresh credentials if possible
        token = self.get_fcm_token()
        if token:
            self._update_token_routing(token)

        asyncio.create_task(self._async_save_credentials())
        _LOGGER.info("FCM credentials updated")

    async def _async_save_credentials(self) -> None:
        """Persist current credentials to entry-scoped TokenCaches (best-effort).

        Behavior:
            * If one or more coordinators are registered, write to **each** coordinator's
              entry-scoped cache (key: 'fcm_credentials').
            * If no coordinator is registered yet (cold start), fall back to the legacy
              async facade to avoid losing credentials across restarts.
        """
        # Persist to entry-scoped caches when possible
        wrote_any = False
        for coordinator in self.coordinators.copy():
            try:
                cache = getattr(coordinator, "cache", None)
                if cache is None:
                    continue
                await cache.set("fcm_credentials", self.credentials)
                wrote_any = True
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Failed to save FCM credentials to entry cache: %s", err)

        # Back-compat fallback if no entry cache was available yet
        if not wrote_any:
            try:
                await async_set_cached_value("fcm_credentials", self.credentials)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Failed to save FCM credentials (fallback): %s", err)

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
