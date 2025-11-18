# custom_components/googlefindmy/Auth/fcm_receiver_ha.py
"""Home Assistant compatible FCM receiver for Google Find My Device.

This module provides an HA-integrated Firebase Cloud Messaging (FCM) receiver that:
- Runs fully async with supervised background loops (one supervisor per config entry).
- Persists credentials via each entry's TokenCache (HA Store-backed).
- Notifies registered request callbacks or pushes background updates to the *right* coordinator(s).
- Avoids any synchronous cache access in the event loop.

Design notes
------------
* Lifecycle: A single shared receiver instance is managed in `hass.data[DOMAIN]`.
  Internally, this receiver manages **one FCM client per entry_id**.
* No global singletons outside this module; Home Assistant orchestrates creation/cleanup.
* The receiver never triggers UI/ChromeDriver flows; it only consumes credentials
  from caches and updates them when the server requests re-registration.
* All potentially blocking work (protobuf decoding, user callbacks) runs in executors.

Multi-account support (entry-scoped clients)
--------------------------------------------
* One client per entry: `self.pcs[entry_id]` and in-memory creds `self.creds[entry_id]`.
* One supervisor loop per entry: `self.supervisors[entry_id]` with the same
  backoff/heartbeat logic as before.
* Per-entry persistence: credentials are written to the entry's TokenCache
  (key: `fcm_credentials`); a routing set of tokens can be stored (key:
  `fcm_routing_tokens`) for resume-after-restart.
* Token → entry routing: incoming pushes are routed using the message `to` token
  (or registration endpoint token). Fallback: if no token mapping exists, all
  coordinators are considered. Optionally, an owner-index fallback can be used
  when a Home Assistant instance is attached (see `attach_hass`).

Precise fan-out (debounce with routing context)
-----------------------------------------------
* We debounce per **(entry_id, device_id)**:
    - `_pending[(entry_id, device_id)]` holds the latest decoded payload **plus** the
      routed target entry set.
    - `_schedule_flush(entry_id, device_id)` (re)starts a short timer (default 250 ms).
    - `_flush(entry_id, device_id)` fans the coalesced payload out only to coordinators
      for the routed entries (no broadcast).

Runtime telemetry (for diagnostics)
-----------------------------------
* Per-receiver metrics retained for compatibility:
  `last_start_monotonic`, `last_stop_monotonic`, `start_count` (aggregate view).
* Logs include routing details:
  `push_received(entry=<id>|unknown, device=..., fanout_targets=n, route=token|owner_index|client|fallback)`.

Retry/404 mitigation (unchanged behavior)
-----------------------------------------
* Registration keeps the existing fixes: numeric `messaging_sender_id`, `Android-GCM/1.5`
  UA in the underlying client, 404 toggle `/register ↔ /register3` (handled in client),
  bounded retries on `PHONE_REGISTRATION_ERROR`, and **no** retries on `BadAuthentication`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import json
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    decrypt_location_response_locations,
)
from custom_components.googlefindmy.ProtoDecoders.decoder import (
    parse_device_update_protobuf,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.googlefindmy.Auth.firebase_messaging._typing import (
        CredentialsUpdatedCallable,
    )
    from custom_components.googlefindmy.Auth.token_cache import TokenCache
    from custom_components.googlefindmy.google_home_filter import (
        GoogleHomeFilter as GoogleHomeFilterProtocol,
    )
else:
    GoogleHomeFilterProtocol = Any
    TokenCache = Any

# Integration-level tunables (safe fallbacks if missing)
try:
    from custom_components.googlefindmy.const import (
        DOMAIN,
        FCM_ABORT_ON_SEQ_ERROR_COUNT,
        FCM_CLIENT_HEARTBEAT_INTERVAL_S,
        FCM_CONNECTION_RETRY_COUNT,
        FCM_IDLE_RESET_AFTER_S,
        FCM_MONITOR_INTERVAL_S,
        FCM_SERVER_HEARTBEAT_INTERVAL_S,
        OPT_IGNORED_DEVICES,  # for ignore fallback via options
    )
except ImportError:  # pragma: no cover
    FCM_CLIENT_HEARTBEAT_INTERVAL_S = 20
    FCM_SERVER_HEARTBEAT_INTERVAL_S = 10
    FCM_IDLE_RESET_AFTER_S = 90.0
    FCM_CONNECTION_RETRY_COUNT = 5
    FCM_MONITOR_INTERVAL_S = 1
    FCM_ABORT_ON_SEQ_ERROR_COUNT = 3
    DOMAIN = "googlefindmy"
    OPT_IGNORED_DEVICES = "ignored_devices"

# Optional import of worker run-state enum (for robust state checks)
if TYPE_CHECKING:
    from custom_components.googlefindmy.Auth.firebase_messaging import (
        FcmPushClient,
        FcmPushClientConfig,
        FcmPushClientRunState,
        FcmRegisterConfig,
    )

    HAVE_FCM_PUSH_CLIENT = True
else:
    try:
        from custom_components.googlefindmy.Auth.firebase_messaging import (
            FcmPushClient,
            FcmPushClientConfig,
            FcmPushClientRunState,
            FcmRegisterConfig,
        )

        HAVE_FCM_PUSH_CLIENT = True
    except ImportError:  # pragma: no cover
        HAVE_FCM_PUSH_CLIENT = False
        FcmPushClientRunState = None
        FcmPushClient = cast("type[Any]", object)
        FcmRegisterConfig = cast("type[Any]", object)
        FcmPushClientConfig = cast("type[Any]", object)

_LOGGER = logging.getLogger(__name__)

type JSONDict = dict[str, Any]
type MutableJSONMapping = MutableMapping[str, Any]

_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _call_in_executor(
    func: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs
) -> _T:
    """Run ``func`` in a background thread with wide Python compatibility."""

    to_thread_obj = getattr(asyncio, "to_thread", None)
    if to_thread_obj is not None:
        to_thread = cast(Callable[..., Awaitable[_T]], to_thread_obj)
        return await to_thread(func, *args, **kwargs)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func, *args, **kwargs)
            return future.result()

    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))


def _coordinator_google_home_filter(
    coordinator: Any,
) -> GoogleHomeFilterProtocol | None:
    """Return the Google Home filter for a coordinator if present."""

    entry = getattr(coordinator, "config_entry", None) or getattr(
        coordinator, "entry", None
    )
    runtime_data = getattr(entry, "runtime_data", None)
    google_home_filter = getattr(runtime_data, "google_home_filter", None)
    return cast("GoogleHomeFilterProtocol | None", google_home_filter)


class FcmReceiverHA:
    """FCM receiver integrated with Home Assistant's async lifecycle (multi-client).

    Responsibilities:
        * Initialize and supervise a dedicated FCM client per config entry.
        * Handle per-request callbacks for specific devices.
        * Route background updates only to the owning entry's coordinator(s).
        * Persist credential updates to each entry's TokenCache.

    Contract:
        * Call `async_initialize()` once (idempotent).
        * `register_coordinator()` / `unregister_coordinator()` are synchronous
          to match HA's `async_on_unload` contract.
        * `async_stop()` gracefully shuts down **all** supervisors/clients.
        * `request_stop()` signals a stop without awaiting.
    """

    # -------------------- Construction & shared constants --------------------

    def __init__(self) -> None:
        # Optional handle to Home Assistant (for owner-index fallback). Set via attach_hass().
        self._hass: HomeAssistant | None = None

        # Per-entry in-memory credentials and clients
        self.creds: dict[
            str, MutableJSONMapping | None
        ] = {}  # entry_id -> credentials dict
        self.pcs: dict[str, FcmPushClient] = {}  # entry_id -> FcmPushClient
        self.supervisors: dict[
            str, asyncio.Task[None]
        ] = {}  # entry_id -> supervisor task
        self._stop_evts: dict[str, asyncio.Event] = {}  # entry_id -> stop event

        # Per-request callbacks awaiting device responses (entry-agnostic)
        self.location_update_callbacks: dict[str, Callable[[str, str], None]] = {}

        # Coordinators eligible to receive background updates
        self.coordinators: list[Any] = []

        # Routing tables
        self._token_to_entries: dict[str, set[str]] = {}  # token -> set(entry_id)
        self._entry_to_tokens: dict[str, set[str]] = {}  # entry_id -> set(token)

        # Entry-scoped TokenCache instances (for background decrypt path)
        self._entry_caches: dict[str, TokenCache] = {}
        self._pending_creds: dict[str, MutableJSONMapping | None] = {}
        self._pending_routing_tokens: dict[str, set[str]] = {}

        # Debounce state (push path): keyed by (entry_id, device_id)
        self._pending: dict[tuple[str, str], JSONDict] = {}
        self._pending_targets: dict[tuple[str, str], set[str] | None] = {}
        self._flush_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._debounce_ms: int = 250

        # Aggregate telemetry
        self.last_start_monotonic: float = 0.0
        self.last_stop_monotonic: float = 0.0
        self.start_count: int = 0

        # Firebase project configuration for Google Find My Device
        self.project_id = "google.com:api-project-289722593072"
        self.app_id = "1:289722593072:android:3cfcf5bc359f0308"
        self.api_key = "AIzaSyD_gko3P392v6how2H7UpdeXQ0v2HLettc"
        self.message_sender_id = "289722593072"  # numeric Sender ID (project number)

        # Config used by all clients
        self._client_cfg: FcmPushClientConfig | None = None  # initialized lazily

        # Guard against concurrent start/stop/register races
        self._lock = asyncio.Lock()

    @staticmethod
    def _ensure_cache_entry_id(cache: Any, entry_id: str) -> None:
        """Attach the entry_id to a cache instance when possible."""

        try:
            current = getattr(cache, "entry_id", None)
        except Exception:  # noqa: BLE001 - attribute access guard
            current = None

        if isinstance(current, str):
            normalized = current.strip()
            if normalized and normalized != entry_id:
                _LOGGER.warning(
                    "[entry=%s] TokenCache provided to FCM receiver has mismatched entry_id '%s'; overriding.",
                    entry_id,
                    normalized,
                )
                try:
                    setattr(cache, "entry_id", entry_id)
                except Exception as err:  # noqa: BLE001 - best-effort
                    _LOGGER.debug(
                        "[entry=%s] Failed to override cache entry_id: %s",
                        entry_id,
                        err,
                    )
            elif not normalized:
                try:
                    setattr(cache, "entry_id", entry_id)
                except Exception as err:  # noqa: BLE001 - best-effort
                    _LOGGER.debug(
                        "[entry=%s] Failed to attach entry_id to cache: %s",
                        entry_id,
                        err,
                    )
        else:
            try:
                setattr(cache, "entry_id", entry_id)
            except Exception as err:  # noqa: BLE001 - best-effort
                _LOGGER.debug(
                    "[entry=%s] Failed to tag cache with entry_id: %s", entry_id, err
                )

    # -------------------- Optional HA attach --------------------

    def attach_hass(self, hass: HomeAssistant) -> None:
        """Optionally attach Home Assistant for owner-index fallback routing."""
        self._hass = hass

    # -------------------- Basic readiness (aggregate) --------------------

    @property
    def is_ready(self) -> bool:
        """True if at least one client is started and listening."""
        for pc in self.pcs.values():
            state = getattr(pc, "run_state", None)
            do_listen = getattr(pc, "do_listen", False)
            if FcmPushClientRunState is not None:
                if state == FcmPushClientRunState.STARTED and do_listen:
                    return True
            elif do_listen:
                return True
        return False

    ready = is_ready  # alias used by callers

    # -------------------- Lifecycle --------------------

    async def async_initialize(self) -> bool:
        """Initialize receiver (idempotent). Defers client creation to coordinator registration."""
        # Prepare shared client config once
        if self._client_cfg is None and HAVE_FCM_PUSH_CLIENT:
            self._client_cfg = FcmPushClientConfig(
                client_heartbeat_interval=int(FCM_CLIENT_HEARTBEAT_INTERVAL_S),
                server_heartbeat_interval=int(FCM_SERVER_HEARTBEAT_INTERVAL_S),
                idle_reset_after=float(FCM_IDLE_RESET_AFTER_S),
                connection_retry_count=int(FCM_CONNECTION_RETRY_COUNT),
                monitor_interval=float(FCM_MONITOR_INTERVAL_S),
                abort_on_sequential_error_count=int(FCM_ABORT_ON_SEQ_ERROR_COUNT),
            )

        _LOGGER.info("FCM receiver initialized (multi-client ready)")
        return True

    async def _ensure_client_for_entry(
        self, entry_id: str, cache: TokenCache | None
    ) -> FcmPushClient | None:
        """Create or return the FCM client for the given entry (idempotent)."""
        if cache is not None:
            self._ensure_cache_entry_id(cache, entry_id)
        async with self._lock:
            if entry_id in self.pcs:
                return self.pcs[entry_id]

            # Load entry-scoped credentials if present
            creds = self.creds.get(entry_id)
            if creds is None:
                pending = self._pending_creds.get(entry_id)
                if isinstance(pending, dict):
                    creds = pending
            try:
                if cache is not None:
                    val = await cache.get("fcm_credentials")
                    if isinstance(val, str):
                        val = json.loads(val)
                    if isinstance(val, dict):
                        creds = val
                        self.creds[entry_id] = creds
                        self._pending_creds.pop(entry_id, None)
            except Exception as err:
                _LOGGER.debug(
                    "Failed to load entry-scoped FCM creds for %s: %s", entry_id, err
                )

            # Build register config (shared across entries)
            if not HAVE_FCM_PUSH_CLIENT:
                _LOGGER.error("FCM client support not available; cannot create client")
                return None

            fcm_config = FcmRegisterConfig(
                project_id=self.project_id,
                app_id=self.app_id,
                api_key=self.api_key,
                messaging_sender_id=self.message_sender_id,
                bundle_id="com.google.android.apps.adm",
            )

            # Per-entry credentials update callback
            def _on_creds_updated_entry(updated: MutableJSONMapping) -> None:
                self._on_credentials_updated_for_entry(entry_id, updated)

            http_client_session = None
            if self._hass is not None:
                http_client_session = async_get_clientsession(self._hass)

            try:
                credential_callback = cast(
                    "CredentialsUpdatedCallable[MutableJSONMapping]",
                    _on_creds_updated_entry,
                )
                pc = FcmPushClient(
                    lambda payload,
                    persistent_id,
                    context,
                    eid=entry_id: self._on_notification(
                        eid, payload, persistent_id, context
                    ),
                    fcm_config,
                    creds,
                    credential_callback,
                    http_client_session=http_client_session,
                    config=self._client_cfg,
                )
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "Failed to construct FCM client for %s: %s", entry_id, err
                )
                return None

            self.pcs[entry_id] = pc
            self.creds[entry_id] = creds if isinstance(creds, dict) else None
            return pc

    async def _start_supervisor_for_entry(  # noqa: PLR0915
        self, entry_id: str, cache: TokenCache | None
    ) -> None:
        """Start the supervisor loop for the given entry if not running."""
        if entry_id in self.supervisors and not self.supervisors[entry_id].done():
            return

        stop_evt = self._stop_evts.setdefault(entry_id, asyncio.Event())

        async def _supervisor() -> None:  # noqa: PLR0912, PLR0915
            backoff = 1.0
            try:
                while not stop_evt.is_set():
                    pc = await self._ensure_client_for_entry(entry_id, cache)
                    if not pc:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, 60.0)
                        continue

                    ok_reg = await self._register_for_fcm_entry(entry_id)
                    if not ok_reg:
                        try:
                            await pc.stop()
                        except Exception:
                            pass
                        finally:
                            async with self._lock:
                                self.pcs.pop(entry_id, None)
                        delay = backoff + random.uniform(0.1, 0.3) * backoff
                        _LOGGER.info(
                            "[entry=%s] Re-trying FCM registration in %.1fs",
                            entry_id,
                            delay,
                        )
                        await asyncio.sleep(delay)
                        backoff = min(backoff * 2, 60.0)
                        continue

                    # Telemetry (aggregate counters)
                    self.last_start_monotonic = time.monotonic()
                    self.start_count += 1

                    try:
                        await pc.start()
                        _LOGGER.debug(
                            "[entry=%s] FCM client started; entering monitor loop",
                            entry_id,
                        )
                    except Exception as err:
                        _LOGGER.info(
                            "[entry=%s] FCM client failed to start: %s", entry_id, err
                        )

                    backoff = 1.0  # reset after a successful start

                    while not stop_evt.is_set():
                        await asyncio.sleep(max(FCM_MONITOR_INTERVAL_S, 0.5))
                        state = getattr(pc, "run_state", None)
                        do_listen = getattr(pc, "do_listen", False)
                        if state is None:
                            _LOGGER.info(
                                "[entry=%s] FCM client state unknown; scheduling restart",
                                entry_id,
                            )
                            break
                        if (
                            FcmPushClientRunState is not None
                            and state
                            in (
                                FcmPushClientRunState.STOPPING,
                                FcmPushClientRunState.STOPPED,
                            )
                        ) or not do_listen:
                            _LOGGER.info(
                                "[entry=%s] FCM client stopped; scheduling restart",
                                entry_id,
                            )
                            break

                    # Cleanup before restart
                    try:
                        await pc.stop()
                    except Exception:
                        pass
                    finally:
                        async with self._lock:
                            self.pcs.pop(entry_id, None)

                    if not stop_evt.is_set():
                        delay = backoff + random.uniform(0.1, 0.3) * backoff
                        _LOGGER.info(
                            "[entry=%s] Restarting FCM client in %.1fs", entry_id, delay
                        )
                        await asyncio.sleep(delay)
                        backoff = min(backoff * 2, 60.0)
            except asyncio.CancelledError:
                _LOGGER.debug("[entry=%s] FCM supervisor cancelled", entry_id)
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("[entry=%s] FCM supervisor crashed: %s", entry_id, err)
            finally:
                _LOGGER.info("[entry=%s] FCM supervisor stopped", entry_id)

        task = asyncio.create_task(
            _supervisor(), name=f"{DOMAIN}.fcm_supervisor[{entry_id}]"
        )
        self.supervisors[entry_id] = task
        _LOGGER.info("Started FCM supervisor for entry %s", entry_id)

    async def _register_for_fcm_entry(self, entry_id: str) -> bool:
        """Single registration attempt for a specific entry."""
        pc = self.pcs.get(entry_id)
        if not pc:
            return False
        try:
            token_or_creds = await pc.checkin_or_register()
            if token_or_creds:
                _LOGGER.info("[entry=%s] FCM registered successfully", entry_id)
                token = self.get_fcm_token(entry_id)
                if token:
                    self._update_token_routing(token, {entry_id})
                    await self._persist_routing_token(entry_id, token)
                return True
            _LOGGER.warning("[entry=%s] FCM registration returned no token", entry_id)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("[entry=%s] FCM registration error: %s", entry_id, err)
            return False

    # Public entrypoint kept for back-compat (starts supervisors lazily if needed)
    async def _start_listening(self) -> None:
        """Ensure supervisors are running for all known coordinators' entries."""
        # Start a supervisor per entry present among registered coordinators
        for coordinator in self.coordinators.copy():
            entry = getattr(coordinator, "config_entry", None)
            cache = getattr(coordinator, "cache", None)
            if entry is None:
                continue
            await self._start_supervisor_for_entry(entry.entry_id, cache)

    # -------------------- Coordinator wiring --------------------

    def register_coordinator(self, coordinator: Any) -> None:
        """Register a coordinator for background updates and ensure its entry client runs.

        Side effects:
            * Adds coordinator to the fan-out list.
            * Mirrors current credentials into this entry's TokenCache (if available).
            * Starts (or ensures) a supervisor for the coordinator's entry.
            * Updates token→entry routing for any available token.
            * Loads previously persisted routing tokens (`fcm_routing_tokens`) and maps them to this entry.
        """
        if coordinator not in self.coordinators:
            self.coordinators.append(coordinator)
            _LOGGER.debug("Coordinator registered (total=%d)", len(self.coordinators))

        entry = getattr(coordinator, "config_entry", None)
        cache = getattr(coordinator, "cache", None)
        if entry is None:
            return

        self._entry_to_tokens.setdefault(entry.entry_id, set())

        if cache is not None:
            self._ensure_cache_entry_id(cache, entry.entry_id)
            self._entry_caches[entry.entry_id] = cache

            pending_creds = self._pending_creds.pop(entry.entry_id, None)
            if pending_creds is not None:
                asyncio.create_task(cache.set("fcm_credentials", pending_creds))

            pending_tokens = self._pending_routing_tokens.pop(entry.entry_id, set())

            if pending_tokens:
                self._entry_to_tokens.setdefault(entry.entry_id, set()).update(
                    pending_tokens
                )

                async def _flush_tokens() -> None:
                    try:
                        existing = await cache.get("fcm_routing_tokens")
                        tokens = set(existing or [])
                        tokens.update(pending_tokens)
                        await cache.set("fcm_routing_tokens", sorted(tokens))
                    except Exception as err:
                        _LOGGER.debug(
                            "[entry=%s] Failed to flush pending routing tokens: %s",
                            entry.entry_id,
                            err,
                        )

                asyncio.create_task(_flush_tokens())

        # Mirror any known credentials to this entry cache
        try:
            creds = self.creds.get(entry.entry_id)
            if creds and cache is not None:
                asyncio.create_task(cache.set("fcm_credentials", creds))
        except Exception as err:
            _LOGGER.debug("Entry-scoped credentials persistence skipped: %s", err)

        # Update routing with any token we already have
        token = self.get_fcm_token(entry.entry_id)
        if token:
            self._update_token_routing(token, {entry.entry_id})
            asyncio.create_task(self._persist_routing_token(entry.entry_id, token))

        # Load persisted routing tokens for this entry and map them as well
        if cache is not None:

            async def _load_tokens() -> None:
                try:
                    existing = await cache.get("fcm_routing_tokens")
                    if isinstance(existing, (list, tuple, set)):
                        for t in existing:
                            if isinstance(t, str) and t:
                                self._update_token_routing(t, {entry.entry_id})
                except Exception as err:
                    _LOGGER.debug(
                        "[entry=%s] Failed to load persisted routing tokens: %s",
                        entry.entry_id,
                        err,
                    )

            asyncio.create_task(_load_tokens())

        # Start supervisor for this entry
        asyncio.create_task(self._start_supervisor_for_entry(entry.entry_id, cache))

    def unregister_coordinator(self, coordinator: Any) -> None:
        """Unregister a coordinator (sync; safe for async_on_unload)."""
        entry = getattr(coordinator, "config_entry", None)
        entry_id: str | None = None
        if entry is not None:
            entry_id = getattr(entry, "entry_id", None)

        try:
            self.coordinators.remove(coordinator)
            _LOGGER.debug("Coordinator unregistered (total=%d)", len(self.coordinators))
        except ValueError:
            pass  # already removed

        if entry_id:
            replacement = None
            for other in self.coordinators:
                other_entry = getattr(other, "config_entry", None)
                other_cache = getattr(other, "cache", None)
                if (
                    other_entry is not None
                    and getattr(other_entry, "entry_id", None) == entry_id
                    and other_cache is not None
                ):
                    replacement = other_cache
                    break

            if replacement is not None:
                self._entry_caches[entry_id] = replacement
            else:
                self._entry_caches.pop(entry_id, None)
                self._purge_entry_tokens(entry_id)

    # -------------------- Incoming notifications --------------------

    def _on_notification(
        self,
        entry_id: str,
        payload: Mapping[str, Any],
        persistent_id: str | None,
        context: Any | None,
    ) -> None:
        """Handle incoming FCM notification (sync callback from per-entry client)."""
        _ = persistent_id  # maintained for signature compatibility
        _ = context
        try:
            hex_string = self._extract_hex_payload(payload)
            if hex_string is None:
                return

            canonic_id = self._extract_canonic_id_from_response(hex_string)
            if not canonic_id:
                _LOGGER.debug("FCM response has no canonical id")
                return

            token = self._extract_push_token(dict(payload))
            target_entries, route_src = self._route_target_entries(
                entry_id, canonic_id, token
            )
            target_coordinators = self._coordinators_for_entries(target_entries)

            cb = self.location_update_callbacks.get(canonic_id)
            if cb:
                self._log_push_received(canonic_id, target_entries, route_src, 1)
                asyncio.create_task(
                    self._run_callback_async(cb, canonic_id, hex_string)
                )
                return

            tracked = [
                c for c in target_coordinators if self._is_tracked(c, canonic_id)
            ]
            for coordinator in target_coordinators:
                if coordinator in tracked:
                    continue
                _LOGGER.debug(
                    "Skipping FCM update for ignored device %s", canonic_id[:8]
                )

            self._log_push_received(canonic_id, target_entries, route_src, len(tracked))

            if not tracked:
                _LOGGER.debug(
                    "No registered coordinator will process %s; dropping FCM update",
                    canonic_id[:8],
                )
                return

            asyncio.create_task(
                self._process_background_update(
                    entry_id, canonic_id, hex_string, target_entries
                )
            )

        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error processing FCM notification: %s", err)

    # -------------------- Routing helpers --------------------

    def _extract_hex_payload(self, payload: Mapping[str, Any]) -> str | None:
        """Return the decoded hex payload or None when absent."""
        payload_dict = (payload.get("data") or {}).get(
            "com.google.android.apps.adm.FCM_PAYLOAD"
        )
        if not payload_dict:
            _LOGGER.debug("FCM notification without FMD payload")
            return None

        pad = len(payload_dict) % 4
        if pad:
            payload_dict += "=" * (4 - pad)

        try:
            decoded = base64.b64decode(payload_dict)
        except (binascii.Error, ValueError) as err:
            _LOGGER.error("FCM Base64 decode failed: %s", err)
            return None

        return binascii.hexlify(decoded).decode("utf-8")

    def _route_target_entries(
        self, entry_id: str, canonic_id: str, token: str | None
    ) -> tuple[set[str] | None, str]:
        """Determine routing targets and the selected source."""
        if token and token in self._token_to_entries:
            return set(self._token_to_entries[token]), "token"

        if entry_id:
            return {entry_id}, "client"

        owner_entry = self._lookup_owner_entry(canonic_id)
        if owner_entry:
            return {owner_entry}, "owner_index"

        return None, "fallback"

    def _lookup_owner_entry(self, canonic_id: str) -> str | None:
        """Best-effort lookup of owner entry id from hass data."""
        if self._hass is None:
            return None
        owner_index = self._hass.data.get(DOMAIN, {}).get("device_owner_index", {})
        entry_id = owner_index.get(canonic_id)
        return entry_id if isinstance(entry_id, str) else None

    def _log_push_received(
        self,
        canonic_id: str,
        target_entries: set[str] | None,
        route_src: str,
        fanout_targets: int,
    ) -> None:
        """Log the push receipt with routing context."""
        _LOGGER.info(
            "push_received(entry=%s, device=%s, fanout_targets=%d, route=%s)",
            ",".join(sorted(target_entries)) if target_entries else "unknown",
            canonic_id[:8],
            fanout_targets,
            route_src,
        )

    @staticmethod
    def _extract_push_token(envelope: dict[str, Any]) -> str | None:
        """Extract the push target token from the envelope (best-effort)."""
        try:
            token = envelope.get("to") or envelope.get("token")
            if not token and isinstance(envelope.get("message"), dict):
                token = envelope["message"].get("token")
            if isinstance(token, str) and token:
                return token
        except Exception:
            pass
        return None

    def _coordinators_for_entries(self, entries: set[str] | None) -> list[Any]:
        """Return coordinators for the given entry set (or all if None)."""
        if entries is None:
            return self.coordinators.copy()
        if not entries:
            return []
        res: list[Any] = []
        for c in self.coordinators:
            try:
                entry = getattr(c, "config_entry", None)
                if entry is not None and entry.entry_id in entries:
                    res.append(c)
            except Exception:
                pass
        return res

    def _prepare_coordinator_payload(
        self, coordinator: Any, key: tuple[str, str], payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Apply coordinator-specific filtering and return payload or None if filtered."""
        coordinator_payload = dict(payload)
        semantic_name = coordinator_payload.get("semantic_name")
        ghf = _coordinator_google_home_filter(coordinator)
        if semantic_name and ghf is not None:
            try:
                should_filter, replacement_attrs = ghf.should_filter_detection(
                    key[1], semantic_name
                )
            except Exception as gf_err:
                _LOGGER.debug("Google Home filter error for %s: %s", key[1][:8], gf_err)
                should_filter, replacement_attrs = False, None

            if should_filter:
                _LOGGER.debug(
                    "Filtered Google Home detection for %s (push path)", key[1][:8]
                )
                return None

            if replacement_attrs:
                if "latitude" in replacement_attrs and "longitude" in replacement_attrs:
                    coordinator_payload["latitude"] = replacement_attrs.get("latitude")
                    coordinator_payload["longitude"] = replacement_attrs.get(
                        "longitude"
                    )
                radius = replacement_attrs.get("radius")
                if radius is not None:
                    coordinator_payload["accuracy"] = radius
                coordinator_payload["semantic_name"] = None

        return coordinator_payload

    def _write_coordinator_payload(
        self, coordinator: Any, device_id: str, payload: dict[str, Any]
    ) -> bool:
        """Persist the payload into coordinator caches."""
        update_cache = getattr(coordinator, "update_device_cache", None)
        if callable(update_cache):
            update_cache(device_id, payload)
            return True

        try:
            coordinator._device_location_data[device_id] = payload  # noqa: SLF001
            _LOGGER.debug(
                "Fallback: wrote to coordinator._device_location_data directly"
            )
            coordinator.increment_stat("background_updates")
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Coordinator cache update failed for %s: %s", device_id, err)
            return False
        return True

    def _record_crowd_source_stat(self, coordinator: Any) -> None:
        """Increment crowd-source stat when available."""
        try:
            coordinator.increment_stat("crowd_sourced_updates")
        except Exception:
            pass

    async def _notify_coordinator(self, coordinator: Any, device_id: str) -> None:
        """Notify coordinator about updated payload."""
        push = getattr(coordinator, "push_updated", None)
        if callable(push):
            push([device_id])
            return

        await coordinator.async_request_refresh()

    def _update_token_routing(self, token: str, entry_ids: set[str]) -> None:
        """Update the token→entry mapping."""
        try:
            if not isinstance(token, str) or not token:
                return
            prev = set(self._token_to_entries.get(token, set()))
            new_entries = {eid for eid in entry_ids if isinstance(eid, str) and eid}

            if new_entries:
                self._token_to_entries[token] = new_entries
            else:
                self._token_to_entries.pop(token, None)

            removed_entries = prev - new_entries
            for entry_id in removed_entries:
                tokens = self._entry_to_tokens.get(entry_id)
                if tokens is not None:
                    tokens.discard(token)
                    if not tokens:
                        self._entry_to_tokens.pop(entry_id, None)

            for entry_id in new_entries:
                self._entry_to_tokens.setdefault(entry_id, set()).add(token)

            if prev != new_entries:
                _LOGGER.debug(
                    "Updated FCM token routing: token=%s… -> %s",
                    token[:8],
                    ",".join(sorted(new_entries)) or "<none>",
                )
        except Exception as err:
            _LOGGER.debug("Token routing update skipped: %s", err)

    async def _persist_routing_token(self, entry_id: str, token: str) -> None:
        """Persist routing tokens per entry (best-effort, entry-scoped if cache available)."""
        if not isinstance(token, str) or not token:
            return

        self._entry_to_tokens.setdefault(entry_id, set()).add(token)

        cache = self._entry_caches.get(entry_id)
        if cache is not None:
            try:
                existing = await cache.get("fcm_routing_tokens")
                tokens = set(existing or [])
                tokens.add(token)
                await cache.set("fcm_routing_tokens", sorted(tokens))
            except Exception as err:
                _LOGGER.debug(
                    "Persisting routing token failed for %s: %s", entry_id, err
                )
            return

        # Resolve cache lazily via registered coordinators
        for coordinator in self.coordinators.copy():
            entry = getattr(coordinator, "config_entry", None)
            cache = getattr(coordinator, "cache", None)
            if entry is None or entry.entry_id != entry_id or cache is None:
                continue
            self._entry_caches[entry_id] = cache
            try:
                existing = await cache.get("fcm_routing_tokens")
                tokens = set(existing or [])
                tokens.add(token)
                await cache.set("fcm_routing_tokens", sorted(tokens))
            except Exception as err:
                _LOGGER.debug(
                    "Persisting routing token failed for %s: %s", entry_id, err
                )
            return

        pending = self._pending_routing_tokens.setdefault(entry_id, set())
        pending.add(token)

    # -------------------- Ignore / target helpers --------------------

    @staticmethod
    def _norm(dev_id: str) -> str:
        """Normalize a device id for equality checks."""
        return (dev_id or "").replace("-", "").lower()

    def _is_tracked(self, coordinator: Any, canonic_id: str) -> bool:
        """Return True if device is eligible for push processing."""
        try:
            is_ignored_fn = getattr(coordinator, "is_ignored", None)
            if callable(is_ignored_fn) and is_ignored_fn(canonic_id):
                return False
        except Exception:
            pass
        try:
            entry = getattr(coordinator, "config_entry", None)
            if entry is not None:
                ignored = entry.options.get(OPT_IGNORED_DEVICES, [])
                if isinstance(ignored, list) and canonic_id in ignored:
                    return False
        except Exception:
            pass
        return True

    def _extract_canonic_id_from_response(self, hex_response: str) -> str | None:
        """Extract canonical id via the decoder."""
        try:
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
        await _call_in_executor(callback, canonic_id, hex_string)

    # -------------------- Push-path decode → debounce → flush --------------------

    async def _process_background_update(
        self,
        entry_id: str,
        canonic_id: str,
        hex_string: str,
        target_entries: set[str] | None,
    ) -> None:
        """Decode location, enqueue for debounce, and schedule a flush (with routing context).

        The routing context (target entry set) is stored alongside the pending payload to
        enable precise fan-out in `_flush(...)`.
        """
        try:
            location_data = await _call_in_executor(
                self._decode_background_location, entry_id, hex_string
            )
            if not location_data:
                _LOGGER.debug(
                    "No location data in background update for %s", canonic_id
                )
                return

            payload: JSONDict = dict(location_data)
            payload.setdefault("last_updated", time.time())

            key = (
                next(iter(target_entries))
                if (target_entries and len(target_entries) == 1)
                else entry_id,
                canonic_id,
            )
            # Store the payload and the full routing target set (may be None for broadcast fallback)
            self._pending[key] = payload
            self._pending_targets[key] = set(target_entries) if target_entries else None

            self._schedule_flush(key)

        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Error processing background update for %s: %s", canonic_id, err
            )

    def _schedule_flush(self, key: tuple[str, str]) -> None:
        """(Re)schedule a short debounce before fanning out updates for (entry, device)."""
        existing = self._flush_tasks.pop(key, None)
        if existing and not existing.done():
            existing.cancel()

        async def _delayed() -> None:
            try:
                await asyncio.sleep(self._debounce_ms / 1000.0)
                await self._flush(key)
            except asyncio.CancelledError:
                return
            except Exception as err:
                _LOGGER.error("Flush task for %s/%s failed: %s", key[0], key[1], err)

        task = asyncio.create_task(
            _delayed(), name=f"{DOMAIN}.fcm_flush[{key[0]}:{key[1][:8]}]"
        )
        self._flush_tasks[key] = task

    async def _flush(self, key: tuple[str, str]) -> None:
        """Flush the latest pending payload to target coordinators only.

        Args:
            key: Tuple of (entry_id_hint, device_id). The exact target entries are taken
                 from `_pending_targets[key]`, which may be a set or None (broadcast fallback).
        """
        payload = self._pending.pop(key, None)
        entries = self._pending_targets.pop(key, None)
        self._flush_tasks.pop(key, None)
        if not payload:
            return

        target_coordinators = self._coordinators_for_entries(entries)

        for coordinator in target_coordinators:
            try:
                if not self._is_tracked(coordinator, key[1]):
                    continue

                coordinator_payload = self._prepare_coordinator_payload(
                    coordinator, key, payload
                )
                if coordinator_payload is None:
                    continue

                if not self._write_coordinator_payload(
                    coordinator, key[1], coordinator_payload
                ):
                    continue

                if coordinator_payload.get("is_own_report") is False:
                    self._record_crowd_source_stat(coordinator)

                await self._notify_coordinator(coordinator, key[1])

            except Exception as err:
                _LOGGER.debug(
                    "Failed to fan-out push update for %s to one coordinator: %s",
                    key[1][:8],
                    err,
                )

    # -------------------- Decode helper --------------------

    def _decode_background_location(self, entry_id: str, hex_string: str) -> JSONDict:
        """Decode background location using protobuf decoders (CPU-bound)."""
        try:
            device_update = parse_device_update_protobuf(hex_string)
            cache = self._entry_caches.get(entry_id)
            if cache is None:
                _LOGGER.error(
                    "No TokenCache available for entry %s during background decrypt",
                    entry_id,
                )
                return {}

            raw_locations = decrypt_location_response_locations(
                device_update, cache=cache
            )
            locations: list[JSONDict] = (
                raw_locations if raw_locations is not None else []
            )
            if not locations:
                return {}

            best_record: Mapping[str, Any] | None = None
            best_key: tuple[float, int, int] | None = None

            for record in locations:
                raw_last_seen = record.get("last_seen")
                if raw_last_seen is None:
                    continue
                try:
                    last_seen = float(raw_last_seen)
                except (TypeError, ValueError):
                    continue
                if math.isnan(last_seen):
                    continue

                has_coordinates = int(
                    record.get("latitude") is not None
                    and record.get("longitude") is not None
                )
                has_altitude = int(record.get("altitude") is not None)

                key = (last_seen, has_coordinates, has_altitude)
                if best_key is None or key > best_key:
                    best_record = record
                    best_key = key

            if best_record is not None:
                return dict(best_record)

            return dict(locations[0])
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to decode background location data: %s", err)
            return {}

    # -------------------- Credentials & stop --------------------

    def _on_credentials_updated_for_entry(self, entry_id: str, creds: Any) -> None:
        """Update in-memory creds for the entry and persist asynchronously."""
        normalized: Any = creds
        if isinstance(normalized, str):
            try:
                normalized = json.loads(normalized)
            except json.JSONDecodeError:
                _LOGGER.debug(
                    "[entry=%s] FCM credentials arrived as non-JSON string", entry_id
                )
        self.creds[entry_id] = normalized if isinstance(normalized, dict) else None

        # Update token routing from fresh creds if possible
        token = self.get_fcm_token(entry_id)
        if token:
            self._update_token_routing(token, {entry_id})
            asyncio.create_task(self._persist_routing_token(entry_id, token))

        asyncio.create_task(self._async_save_credentials_for_entry(entry_id))
        _LOGGER.info("[entry=%s] FCM credentials updated", entry_id)

    async def _async_save_credentials_for_entry(self, entry_id: str) -> None:
        """Persist current credentials to the entry's TokenCache (best-effort)."""
        creds = self.creds.get(entry_id)
        cache = self._entry_caches.get(entry_id)
        if cache is not None:
            try:
                await cache.set("fcm_credentials", creds)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "[entry=%s] Failed to save FCM credentials to entry cache: %s",
                    entry_id,
                    err,
                )
            else:
                self._pending_creds.pop(entry_id, None)
            return

        for coordinator in self.coordinators.copy():
            entry = getattr(coordinator, "config_entry", None)
            cache = getattr(coordinator, "cache", None)
            if entry is None or entry.entry_id != entry_id or cache is None:
                continue
            self._entry_caches[entry_id] = cache
            try:
                await cache.set("fcm_credentials", creds)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "[entry=%s] Failed to save FCM credentials to entry cache: %s",
                    entry_id,
                    err,
                )
            else:
                self._pending_creds.pop(entry_id, None)
            return

        # Defer until cache available
        if (
            entry_id not in self._pending_creds
            or creds is not self._pending_creds[entry_id]
        ):
            self._pending_creds[entry_id] = creds if isinstance(creds, dict) else None

    def request_stop(self) -> None:
        """Signal a cooperative stop for all supervisors without awaiting."""
        for eid, evt in self._stop_evts.items():
            evt.set()
            task = self.supervisors.get(eid)
            if task:
                task.cancel()

    async def async_stop(self, timeout: float = 5.0) -> None:
        """Stop all supervisors and clients (graceful, bounded)."""
        for eid, evt in self._stop_evts.items():
            evt.set()
        for eid, task in list(self.supervisors.items()):
            if task:
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=timeout)
                except TimeoutError:
                    _LOGGER.warning(
                        "[entry=%s] FCM supervisor did not stop within %.1fs; detaching",
                        eid,
                        timeout,
                    )
                except asyncio.CancelledError:
                    pass
        self.supervisors.clear()

        # Stop all clients
        for eid, pc in list(self.pcs.items()):
            try:
                await asyncio.wait_for(pc.stop(), timeout=timeout)
            except TimeoutError:
                _LOGGER.warning(
                    "[entry=%s] FCM client did not stop within %.1fs; detaching",
                    eid,
                    timeout,
                )
            except (ConnectionError, TimeoutError) as err:
                _LOGGER.debug("[entry=%s] FCM client stop network error: %s", eid, err)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "[entry=%s] FCM client stop unexpected error: %s", eid, err
                )
            finally:
                self.pcs.pop(eid, None)
                self._purge_entry_tokens(eid)

        self.last_stop_monotonic = time.monotonic()
        _LOGGER.info("FCM receiver stopped")

    def _purge_entry_tokens(self, entry_id: str) -> None:
        """Remove all routing references for a given entry."""
        tokens = self._entry_to_tokens.pop(entry_id, set())
        self._pending_routing_tokens.pop(entry_id, None)
        if not tokens:
            return
        for token in tokens:
            entries = self._token_to_entries.get(token)
            if entries is None:
                continue
            entries = set(entries)
            entries.discard(entry_id)
            if entries:
                self._token_to_entries[token] = entries
            else:
                self._token_to_entries.pop(token, None)

    # -------------------- Public token accessor --------------------

    def get_fcm_token(self, entry_id: str | None = None) -> str | None:
        """Return current FCM token (best-effort).

        If `entry_id` is provided, returns the token for that entry's client when available.
        Otherwise returns the first available token across clients (legacy behavior).
        """
        if entry_id:
            creds = self.creds.get(entry_id)
            if isinstance(creds, dict):
                tok = (creds.get("fcm") or {}).get("registration", {}).get("token")
                if isinstance(tok, str) and tok:
                    return tok
            # Also try the current client's live creds if present
            pc = self.pcs.get(entry_id)
            if pc:
                try:
                    c = getattr(pc, "credentials", None)
                    if isinstance(c, dict):
                        tok = (c.get("fcm") or {}).get("registration", {}).get("token")
                        if isinstance(tok, str) and tok:
                            return tok
                except Exception:
                    pass
        # Fallback: first available token across entries
        for c in self.creds.values():
            if isinstance(c, dict):
                tok = (c.get("fcm") or {}).get("registration", {}).get("token")
                if isinstance(tok, str) and tok:
                    return tok
        return None

    # -------------------- Manual locate registration --------------------

    def _select_manual_locate_entry(  # noqa: PLR0912
        self, canonic_id: str
    ) -> tuple[str | None, TokenCache | None]:
        """Choose the best entry/cache for manual locate registration."""
        fallback_entry: str | None = None
        fallback_cache: TokenCache | None = None
        display_entry: str | None = None
        display_cache: TokenCache | None = None

        for coordinator in self.coordinators.copy():
            entry = getattr(coordinator, "config_entry", None)
            candidate_entry = (
                getattr(entry, "entry_id", None) if entry is not None else None
            )
            if not candidate_entry:
                continue

            candidate_cache = self._entry_caches.get(candidate_entry)
            if candidate_cache is None:
                candidate_cache = getattr(coordinator, "cache", None) or getattr(
                    coordinator, "_cache", None
                )
                if candidate_cache is not None:
                    self._entry_caches[candidate_entry] = candidate_cache

            present = False
            present_fn = getattr(coordinator, "is_device_present", None)
            if callable(present_fn):
                try:
                    present = bool(present_fn(canonic_id))
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "[entry=%s] Manual locate presence check failed for %s: %s",
                        candidate_entry,
                        canonic_id[:8],
                        err,
                    )

            has_display = False
            if not present:
                name_fn = getattr(coordinator, "get_device_display_name", None)
                if callable(name_fn):
                    try:
                        has_display = bool(name_fn(canonic_id))
                    except Exception:
                        has_display = False

            if present:
                return candidate_entry, candidate_cache

            if has_display:
                display_entry = candidate_entry
                display_cache = candidate_cache

            if fallback_entry is None:
                fallback_entry = candidate_entry
                fallback_cache = candidate_cache

        if display_entry is not None:
            return display_entry, display_cache

        return fallback_entry, fallback_cache

    async def _ensure_token_for_entry(self, entry_id: str) -> str | None:
        """Request and return a token for the given entry when missing."""
        token = self.get_fcm_token(entry_id)
        if token:
            return token

        ok_reg = await self._register_for_fcm_entry(entry_id)
        if not ok_reg:
            return None

        return self.get_fcm_token(entry_id)

    async def async_register_for_location_updates(
        self, canonic_id: str, callback: Callable[[str, str], None]
    ) -> str | None:
        """Register a manual locate callback and ensure an entry token is available."""

        if not isinstance(canonic_id, str) or not canonic_id:
            _LOGGER.warning("Manual locate registration skipped: missing canonical id")
            return None
        if not callable(callback):
            _LOGGER.error(
                "Manual locate registration for %s rejected: callback is not callable",
                canonic_id[:8],
            )
            return None

        entry_id, cache = self._select_manual_locate_entry(canonic_id)
        if entry_id is None:
            _LOGGER.warning(
                "Manual locate registration skipped for %s: no coordinator available",
                canonic_id[:8],
            )
            return None

        self.location_update_callbacks[canonic_id] = callback

        token: str | None = None
        try:
            client = await self._ensure_client_for_entry(entry_id, cache)
            if client is None:
                _LOGGER.warning(
                    "[entry=%s] Manual locate registration failed: client unavailable",
                    entry_id,
                )
                return None

            await self._start_supervisor_for_entry(entry_id, cache)

            token = await self._ensure_token_for_entry(entry_id)

            if not token:
                _LOGGER.warning(
                    "[entry=%s] Manual locate registration failed: token unavailable",
                    entry_id,
                )
                return None

            self._update_token_routing(token, {entry_id})
            await self._persist_routing_token(entry_id, token)
            _LOGGER.info(
                "[entry=%s] Manual locate registration ready for %s",
                entry_id,
                canonic_id[:8],
            )
            return token
        finally:
            if not token:
                self.location_update_callbacks.pop(canonic_id, None)

    async def async_unregister_for_location_updates(self, canonic_id: str) -> None:
        """Remove a manual locate callback if registered."""

        if self.location_update_callbacks.pop(canonic_id, None) is not None:
            _LOGGER.debug(
                "Manual locate callback removed for %s",
                canonic_id[:8],
            )
