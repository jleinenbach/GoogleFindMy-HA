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
import contextvars
import functools
import json
import logging
import math
import random
import time
from collections.abc import (
    Awaitable,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    MutableMapping,
)
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

from aiohttp import ClientError
from cryptography.exceptions import InvalidTag
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.googlefindmy.Auth.firebase_messaging.fcmregister import (
    FcmRegisterHTTPError,
)
from custom_components.googlefindmy.exceptions import FatalRegistrationError
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    StaleOwnerKeyError,
    async_decrypt_location_response_locations,
)
from custom_components.googlefindmy.NovaApi.nova_request import (
    _CACHE_PROVIDER,
)
from custom_components.googlefindmy.ProtoDecoders import decoder as decoder_module

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


# Iter-12 (Codex follow-up): the short-run crash cap needs to know
# whether the FCM client ever reached ``FcmPushClientRunState.STARTED``
# during a run, even when the entire CREATED -> STARTED -> CRASH
# lifecycle happens between two monitor polls (``FCM_MONITOR_INTERVAL_S``
# >= 0.5s). Sampling alone is too coarse for the very poison-message
# loop the cap is meant to stop. A subclass with a ``__setattr__`` hook
# latches ``_has_been_started`` the moment the vendored library assigns
# ``run_state = STARTED`` -- independent of any polling cadence and
# without modifying the vendored library itself. See
# CA-DEFENSE-OBSERVABILITY-001.
if HAVE_FCM_PUSH_CLIENT and FcmPushClientRunState is not None:
    # Snapshot of the original class for runtime identity comparison.
    # Tests substitute the module-level ``FcmPushClient`` symbol via
    # monkeypatch (the documented seam) to inject test doubles; the
    # factory below honours that substitution by class identity, not by
    # subclass relation. See CA-SUBCLASS-IDENTITY-001.
    _OriginalFcmPushClient: type[Any] | None = FcmPushClient

    class _ObservableFcmPushClient(FcmPushClient[Any]):
        """FcmPushClient subclass with a run_state observer latch.

        See CA-DEFENSE-OBSERVABILITY-001.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Seed the latch before super().__init__ so any attribute
            # assignment performed by the parent's constructor still
            # routes through our ``__setattr__`` without raising.
            object.__setattr__(self, "_has_been_started", False)
            super().__init__(*args, **kwargs)

        def __setattr__(self, name: str, value: Any) -> None:
            super().__setattr__(name, value)
            if (
                name == "run_state"
                and FcmPushClientRunState is not None
                and value == FcmPushClientRunState.STARTED
            ):
                # ``object.__setattr__`` to avoid recursion through this hook.
                object.__setattr__(self, "_has_been_started", True)

    _ObservableFcmPushClientCls: type[Any] | None = _ObservableFcmPushClient
else:  # pragma: no cover - exercised only when the vendored library is absent
    _OriginalFcmPushClient = None
    _ObservableFcmPushClientCls = None


def _FcmPushClientFactory(*args: Any, **kwargs: Any) -> FcmPushClient[Any]:
    """Construct an FCM client at call time, honouring test substitutions.

    Production: prefers the ``_ObservableFcmPushClient`` subclass so the
    short-run crash cap can observe ``run_state -> STARTED`` transitions
    that happen between monitor polls (CA-DEFENSE-OBSERVABILITY-001).

    Tests: when the module-level ``FcmPushClient`` symbol has been
    monkeypatched away from the original class (the documented test
    seam), the factory instantiates the patched class DIRECTLY rather
    than the production subclass. This preserves ``isinstance`` identity
    for test doubles (CA-SUBCLASS-IDENTITY-001) instead of returning a
    subclass that diverges from the patched symbol.
    """

    current = globals().get("FcmPushClient")
    if (
        _ObservableFcmPushClientCls is not None
        and _OriginalFcmPushClient is not None
        and current is _OriginalFcmPushClient
    ):
        return cast(
            "FcmPushClient[Any]",
            _ObservableFcmPushClientCls(*args, **kwargs),
        )
    if current is None:  # pragma: no cover - vendored library missing
        raise RuntimeError("FcmPushClient is not available")
    return cast("FcmPushClient[Any]", current(*args, **kwargs))


type JSONDict = dict[str, Any]
type MutableJSONMapping = MutableMapping[str, Any]

_MAX_SUPERVISOR_BACKOFF_S = 120.0    # cap retry delay at 2 minutes (was 4096s)
_BACKOFF_WARNING_THRESHOLD_S = 64.0  # warn after ~6 failed attempts
_MAX_FATAL_AUTH_RETRIES = 3       # 401: max retries (60s, 120s, then give up)
_MAX_FATAL_ENDPOINT_RETRIES = 7   # 404: max retries (30s-300s, Google rotates endpoints)
# Defense 2: supervisor-level crash cap as a bulkhead around the inner-loop
# poison-message filter (``fcmpushclient._listen`` Defense 1+3). When the FCM
# client connects but loses the run-loop within ``_SHORT_RUN_THRESHOLD_S``
# repeatedly, a persistent poison condition is suspected and the supervisor
# stops looping to prevent unbounded retry pressure on Google + log spam.
_MAX_CONSECUTIVE_SHORT_RUNS = 10
_SHORT_RUN_THRESHOLD_S = 30.0  # strictly < 30s between pc.start() and STOPPED = short run
_HTTP_UNAUTHORIZED = 401
_HTTP_NOT_FOUND = 404


async def _call_in_executor[**P, T](
    func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    """Run ``func`` in a background thread with wide Python compatibility."""

    to_thread_obj = getattr(asyncio, "to_thread", None)
    if to_thread_obj is not None:
        to_thread = cast(Callable[..., Awaitable[T]], to_thread_obj)
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
        self.pcs: dict[str, FcmPushClient[Any]] = {}  # entry_id -> FcmPushClient
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
        self._default_entry_id: str | None = None

        # Debounce state (push path): keyed by (entry_id, device_id)
        self._pending: dict[tuple[str, str], JSONDict] = {}
        self._pending_targets: dict[tuple[str, str], set[str] | None] = {}
        self._flush_tasks: dict[tuple[str, str], asyncio.Task[None]] = {}
        self._debounce_ms: int = 250

        self._fatal_error: str | None = None
        self._fatal_errors: dict[str, str] = {}
        self._fatal_retry_counts: dict[str, int] = {}  # "entry:auth"|"entry:endpoint" -> count

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

        # Per-entry runtime health
        self._entry_last_activity_monotonic: dict[str, float] = {}
        self._activity_stale_after_s: float = float(FCM_IDLE_RESET_AFTER_S)
        self._entry_health: dict[str, bool] = {}
        self._entry_last_connected_wall: dict[str, float] = {}

        # Active background tasks for exception tracking (P0 fix)
        self._active_tasks: set[asyncio.Task[Any]] = set()

        # Per-entry nudge events: allows coordinator to wake up a sleeping supervisor
        self._retry_nudge_evts: dict[str, asyncio.Event] = {}

        # Defense 2 iter-8: persistent short-run cap latch.
        #
        # When the cap fires the entry is in a *poison-message crash loop*
        # that only a real healthy supervisor run (>= _SHORT_RUN_THRESHOLD_S)
        # can prove gone. A successful FCM re-registration alone is NOT a
        # proof: registration only contacts the GCM endpoint, it does not
        # show that the subsequent listener stays up long enough to drain
        # the poisoned notification.
        #
        # The latch is therefore set together with the user-visible repair
        # issue at cap-fire time and is the *only* thing that gates whether
        # ``_clear_fatal_error_for_entry`` (which gets called from every
        # registration-success path) is allowed to drop the fatal-error map
        # entry and the Repairs UI artefact. Without this latch, any reload
        # or credential update would clear the cap-related fatal state, the
        # supervisor would start a fresh 10-run burst, and the user would
        # see no visible fatal state in between (Codex defense-2 iter-8
        # second finding).
        self._short_run_cap_latched: set[str] = set()

    def _clear_fatal_error_for_entry(
        self,
        entry_id: str,
        *,
        reason: str | None = None,
        force: bool = False,
    ) -> None:
        """Clear any latched fatal registration error for a config entry.

        Also removes the Defense 2 short-run-crash-loop repair issue from the
        Home Assistant Issue Registry. The fatal-error map and the Issue
        Registry entry are a CREATE/DELETE pair: any recovery path that clears
        the in-memory latch must also delete the user-visible UI artefact, or
        the Repairs panel keeps a stale non-fixable error after the underlying
        condition is gone (e.g. user re-registers credentials and FCM starts
        successfully again).

        Defense 2 iter-8 (Codex second finding): a successful FCM
        re-registration is NOT a recovery proof for the short-run crash cap.
        While ``_short_run_cap_latched`` contains *entry_id* this method must
        protect the cap state (latched fatal message + Repairs UI artefact)
        from being silently cleared, so users keep seeing the fatal state
        until a real healthy supervisor run (>= _SHORT_RUN_THRESHOLD_S) drops
        the latch from the monitor loop. Hard-reset callers (entry unregister,
        test cleanup, integration teardown) must opt in with ``force=True``.

        Defense 2 iter-10 (Codex follow-up): the latch guard is intentionally
        narrow. It is a no-op ONLY when the currently stored fatal IS the
        cap-related message (``FCM short-run crash loop: ...``). Non-cap fatal
        errors (e.g. a terminal 401/404 from a subsequent re-registration that
        overwrote ``_fatal_errors[entry_id]``) must clear normally so a
        credentials recovery is not blocked by an unrelated latch. The cap
        latch, any cap-related fatal it still protects, and the Repairs UI
        artefact remain in place in that case; only the unrelated fatal is
        dropped.
        """

        cap_message_prefix = "FCM short-run crash loop:"

        if not force and entry_id in self._short_run_cap_latched:
            current_fatal = self._fatal_errors.get(entry_id)
            # No fatal stored, or the stored fatal IS the cap message:
            # preserve everything (latch, message, Repairs UI artefact).
            if current_fatal is None or current_fatal.startswith(cap_message_prefix):
                _LOGGER.debug(
                    "[entry=%s] Skipping fatal-error clear; short-run cap latch "
                    "active (reason=%s). Latch will be released by a healthy "
                    "supervisor run.",
                    entry_id,
                    reason or "unspecified",
                )
                return

            # Non-cap fatal stored while the cap latch is active. Clear the
            # unrelated fatal but KEEP the cap latch and Repairs UI artefact
            # intact, so the user-visible cap warning is not silently
            # invalidated by an unrelated recovery path.
            _LOGGER.debug(
                "[entry=%s] Cap latch active; clearing non-cap fatal "
                "(reason=%s) while preserving cap latch and repair issue",
                entry_id,
                reason or "unspecified",
            )
            self._fatal_errors.pop(entry_id, None)
            self._fatal_error = next(iter(self._fatal_errors.values()), None)
            return

        if force:
            self._short_run_cap_latched.discard(entry_id)

        removed = False
        if entry_id in self._fatal_errors:
            if reason:
                _LOGGER.debug(
                    "[entry=%s] Clearing latched FCM fatal error (%s)", entry_id, reason
                )
            else:
                _LOGGER.debug("[entry=%s] Clearing latched FCM fatal error", entry_id)

            self._fatal_errors.pop(entry_id, None)
            removed = True

        if removed:
            self._fatal_error = next(iter(self._fatal_errors.values()), None)

        if self._hass is not None:
            try:
                ir.async_delete_issue(
                    self._hass, DOMAIN, f"fcm_short_run_crash_loop_{entry_id}"
                )
            except Exception:  # noqa: BLE001 - best-effort UI cleanup
                _LOGGER.debug(
                    "[entry=%s] Failed to delete short-run repair issue",
                    entry_id,
                    exc_info=True,
                )

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

    # -------------------- Loop-safe dispatch (P0 fix) --------------------

    def _dispatch_to_hass_loop(
        self, coro: Coroutine[Any, Any, Any], *, label: str
    ) -> None:
        """Dispatch a coroutine into the HA event loop from any thread safely.

        This fixes P0 thread-safety issue: _on_notification may be called from
        a non-event-loop thread by the FCM client. Using asyncio.create_task
        directly would fail with 'no running event loop'.
        """
        try:
            loop = asyncio.get_running_loop()
            # Already in an event loop - schedule directly
            new_task: asyncio.Task[Any] = loop.create_task(
                coro,
                name=f"{DOMAIN}.{label}",
            )
            self._track_task(new_task, label=label)
            return
        except RuntimeError:
            # No running loop in this thread; schedule onto HA loop
            pass

        hass_loop = getattr(self._hass, "loop", None) if self._hass else None
        if hass_loop is None:
            _LOGGER.error("FCM notification dropped: Home Assistant loop not available")
            return

        def _schedule() -> None:
            scheduled_task: asyncio.Task[Any] = hass_loop.create_task(
                coro, name=f"{DOMAIN}.{label}"
            )
            self._track_task(scheduled_task, label=label)

        hass_loop.call_soon_threadsafe(_schedule)

    def _track_task(self, task: asyncio.Task[Any], *, label: str) -> None:
        """Ensure task exceptions are retrieved and logged.

        Prevents 'Task exception was never retrieved' warnings by attaching
        a done callback that logs any exceptions.
        """
        self._active_tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._active_tasks.discard(t)
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            except Exception:
                _LOGGER.exception(
                    "Unhandled exception retrieving task result (%s)", label
                )
                return
            if exc:
                _LOGGER.exception("Background task failed (%s)", label, exc_info=exc)

        task.add_done_callback(_done)

    @contextmanager
    def _scoped_cache_provider(
        self, provider: Callable[[], Any]
    ) -> Iterator[contextvars.Token[Callable[[], Any] | None] | None]:
        """Context manager for cache provider with guaranteed cleanup (P1-2 fix).

        Uses only contextvars (not global state) to prevent cross-contamination
        between concurrent async operations from different config entries.
        """
        token: contextvars.Token[Callable[[], Any] | None] | None = None
        try:
            token = _CACHE_PROVIDER.set(provider)
            yield token
        finally:
            if token is not None:
                try:
                    _CACHE_PROVIDER.reset(token)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Cache provider reset failed: %s", err)

    def _monotonic_from_wall_time(
        self, wall_time: float, monotonic_now: float
    ) -> float | None:
        """Convert a wall clock timestamp to the monotonic clock domain."""

        if wall_time <= 0:
            return None

        try:
            wall_now = time.time()
            offset = monotonic_now - wall_now
            return wall_time + offset
        except Exception:  # pragma: no cover - defensive conversion
            return None

    def _update_last_activity_for_entry(
        self, entry_id: str, pc: Any, monotonic_now: float
    ) -> float | None:
        """Refresh and return the last activity timestamp for an entry."""

        last_message_time = getattr(pc, "last_message_time", None)
        last_activity = None
        if isinstance(last_message_time, (int, float)):
            last_activity = self._monotonic_from_wall_time(
                float(last_message_time), monotonic_now
            )

        if last_activity is None:
            last_activity = self._entry_last_activity_monotonic.get(entry_id)

        if last_activity is not None:
            self._entry_last_activity_monotonic[entry_id] = last_activity

        return last_activity

    def get_health_snapshots(self) -> dict[str, dict[str, Any]]:
        """Return per-entry health snapshots for diagnostics/state reporting."""

        now = time.monotonic()
        stale_after = max(self._activity_stale_after_s, 0.0)

        snapshots: dict[str, dict[str, Any]] = {}
        for entry_id, pc in self.pcs.items():
            supervisor = self.supervisors.get(entry_id)
            supervisor_running = supervisor is not None and not supervisor.done()
            run_state = getattr(pc, "run_state", None)
            do_listen = bool(getattr(pc, "do_listen", False))

            last_activity = self._entry_last_activity_monotonic.get(entry_id)
            activity_age = None
            if last_activity is not None:
                activity_age = max(now - last_activity, 0.0)

            if FcmPushClientRunState is not None:
                client_ready = run_state == FcmPushClientRunState.STARTED and do_listen
            else:
                client_ready = do_listen

            fresh_activity = last_activity is not None and (
                stale_after == 0.0
                or (activity_age is not None and activity_age <= stale_after)
            )

            snapshots[entry_id] = {
                "supervisor_running": supervisor_running,
                "client_ready": client_ready,
                "run_state": getattr(run_state, "name", run_state),
                "do_listen": do_listen,
                "last_activity_monotonic": last_activity,
                "seconds_since_last_activity": activity_age,
                "activity_stale": not fresh_activity,
                "healthy": supervisor_running and client_ready and fresh_activity,
            }

        return snapshots

    def _update_entry_health(self, entry_id: str, healthy: bool) -> None:
        """Track entry health transitions and notify coordinators."""

        previous = self._entry_health.get(entry_id)
        if previous is healthy:
            return

        self._entry_health[entry_id] = healthy
        if healthy:
            self._entry_last_connected_wall[entry_id] = time.time()

        for coordinator in list(self.coordinators):
            entry = getattr(coordinator, "config_entry", None)
            if getattr(entry, "entry_id", None) != entry_id:
                continue

            try:
                update_listeners = getattr(coordinator, "async_update_listeners", None)
                if callable(update_listeners):
                    update_listeners()
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "[entry=%s] Coordinator listener notification failed: %s",
                    entry_id,
                    err,
                )

    # -------------------- Basic readiness (aggregate) --------------------

    @property
    def is_ready(self) -> bool:
        """True if at least one client is started and listening."""
        snapshots = self.get_health_snapshots()
        return any(snap.get("healthy") for snap in snapshots.values())

    ready = is_ready  # alias used by callers

    def nudge_retry(self, entry_id: str | None = None) -> bool:
        """Wake a sleeping supervisor so it retries FCM connection immediately.

        Called by the polling coordinator when it enters degraded mode and wants
        the supervisor to attempt reconnection sooner than the backoff timer.
        Returns True if a nudge event was set.
        """
        key = entry_id or self._default_entry_id
        if not key:
            # Nudge all entries
            nudged = False
            for evt in self._retry_nudge_evts.values():
                evt.set()
                nudged = True
            return nudged
        nudge_evt = self._retry_nudge_evts.get(key)
        if nudge_evt is not None:
            nudge_evt.set()
            return True
        return False

    def get_last_connected_wall_time(self, entry_id: str | None) -> float | None:
        """Return the last wall-clock timestamp when the entry marked healthy."""

        entry_key = entry_id if entry_id is not None else self._default_entry_id
        if entry_key is None:
            return None

        ts = self._entry_last_connected_wall.get(entry_key)
        return float(ts) if ts is not None else None

    # -------------------- Lifecycle --------------------

    async def _prime_cache_state(self, entry_id: str, cache: TokenCache) -> None:
        """Load entry-scoped credentials and routing tokens before startup."""

        try:
            creds_val = await cache.get("fcm_credentials")
            if isinstance(creds_val, str):
                creds_val = json.loads(creds_val)
            if isinstance(creds_val, MutableMapping):
                self.creds[entry_id] = creds_val
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "[entry=%s] Failed to load cached FCM credentials: %s", entry_id, err
            )

        try:
            tokens_val = await cache.get("fcm_routing_tokens")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "[entry=%s] Failed to load cached routing tokens: %s", entry_id, err
            )
            return

        if isinstance(tokens_val, (list, tuple, set)):
            tokens = {t for t in tokens_val if isinstance(t, str) and t}
            if tokens:
                entry_tokens = self._entry_to_tokens.setdefault(entry_id, set())
                entry_tokens.update(tokens)
                for token in tokens:
                    mapped_entries = self._token_to_entries.setdefault(token, set())
                    mapped_entries.add(entry_id)

    async def async_initialize(
        self, *, entry_id: str | None = None, cache: TokenCache | None = None
    ) -> bool:
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

        if entry_id and cache is None:
            try:
                from . import token_cache as token_cache_module  # noqa: PLC0415

                cache = token_cache_module.get_cache_for_entry(entry_id)
            except Exception:  # pragma: no cover - best-effort fallback
                cache = None

        if entry_id and cache is not None:
            self._ensure_cache_entry_id(cache, entry_id)
            self._entry_caches[entry_id] = cache
            await self._prime_cache_state(entry_id, cache)

        if entry_id is not None and self._default_entry_id is None:
            self._default_entry_id = entry_id

        _LOGGER.info("FCM receiver initialized (multi-client ready)")
        return True

    async def _ensure_client_for_entry(
        self, entry_id: str, cache: TokenCache | None
    ) -> FcmPushClient[Any] | None:
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
                android_cert_sha1="38918a453d07199354f8b19af05ec6562ced5788",
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
                pc = _FcmPushClientFactory(
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
            nudge_evt = self._retry_nudge_evts.setdefault(
                entry_id, asyncio.Event()
            )

            async def _nudge_sleep(seconds: float) -> bool:
                """Sleep up to *seconds*, returning True if nudged early."""
                nudge_evt.clear()
                try:
                    await asyncio.wait_for(nudge_evt.wait(), timeout=seconds)
                    return True  # nudged
                except TimeoutError:
                    return False

            backoff = 1.0
            # Defense 2: per-supervisor closure state for the short-run
            # crash cap. Kept closure-local (not on ``self``) so a
            # re-registration that cancels this supervisor and spawns a
            # new one cannot inherit a stale counter (CA-Audit B1).
            short_run_counter: int = 0
            entry_start: float = 0.0
            try:
                while not stop_evt.is_set():
                    pc = await self._ensure_client_for_entry(entry_id, cache)
                    if not pc:
                        nudged = await _nudge_sleep(backoff)
                        if nudged:
                            backoff = 1.0
                        else:
                            backoff = min(backoff * 2, _MAX_SUPERVISOR_BACKOFF_S)
                        continue

                    try:
                        ok_reg = await self._register_for_fcm_entry(entry_id)
                    except FatalRegistrationError as err:
                        message = str(err) or "FCM registration failed"
                        # Publish to ``_fatal_errors`` only once the retry
                        # budget is exhausted (see ``break`` paths below);
                        # otherwise the coordinator's 401-substring match
                        # in ``coordinator/helpers/update.py`` escalates
                        # to ``ConfigEntryAuthFailed`` before this loop
                        # gets a chance to renew tokens.
                        _LOGGER.error(
                            "[entry=%s] FCM registration failed: %s",
                            entry_id,
                            message,
                        )
                        try:
                            await pc.stop()
                        except Exception:
                            pass
                        finally:
                            async with self._lock:
                                self.pcs.pop(entry_id, None)

                        is_auth = getattr(err, "is_auth_error", False)
                        counter_key = (
                            f"{entry_id}:auth"
                            if is_auth
                            else f"{entry_id}:endpoint"
                        )
                        fatal_count = (
                            self._fatal_retry_counts.get(counter_key, 0) + 1
                        )
                        self._fatal_retry_counts[counter_key] = fatal_count

                        if is_auth:
                            # 401: Token renewal while preserving android_id
                            max_retries = _MAX_FATAL_AUTH_RETRIES
                            if fatal_count >= max_retries:
                                self._fatal_error = message
                                self._fatal_errors[entry_id] = message
                                _LOGGER.error(
                                    "[entry=%s] FCM auth failed %d "
                                    "consecutive times; giving up. "
                                    "Re-authentication may be required.",
                                    entry_id,
                                    fatal_count,
                                )
                                break

                            await self._invalidate_fcm_tokens(entry_id)

                            delay = 60.0 * fatal_count
                            _LOGGER.warning(
                                "[entry=%s] FCM auth error 401 (%d/%d); "
                                "renewing tokens (android_id preserved), "
                                "retry in %.0fs",
                                entry_id,
                                fatal_count,
                                max_retries,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue
                        else:
                            # 404: Endpoint rotation — just retry
                            max_retries = _MAX_FATAL_ENDPOINT_RETRIES
                            if fatal_count >= max_retries:
                                self._fatal_error = message
                                self._fatal_errors[entry_id] = message
                                _LOGGER.error(
                                    "[entry=%s] FCM endpoint 404 "
                                    "persisted for %d attempts; "
                                    "giving up.",
                                    entry_id,
                                    fatal_count,
                                )
                                break

                            delay = min(300.0, 30.0 * fatal_count)
                            _LOGGER.warning(
                                "[entry=%s] FCM endpoint 404 (%d/%d); "
                                "retrying in %.0fs (likely Google "
                                "endpoint rotation)",
                                entry_id,
                                fatal_count,
                                max_retries,
                                delay,
                            )
                            await asyncio.sleep(delay)
                            continue

                    if not ok_reg:
                        try:
                            await pc.stop()
                        except Exception:
                            pass
                        finally:
                            async with self._lock:
                                self.pcs.pop(entry_id, None)
                        delay = backoff + random.uniform(0.1, 0.3) * backoff  # nosec B311
                        if backoff >= _BACKOFF_WARNING_THRESHOLD_S:
                            _LOGGER.warning(
                                "[entry=%s] FCM registration still failing after multiple attempts (next retry in %.1fs). "
                                "Check Firewall (Port 5228) or Credentials.",
                                entry_id,
                                delay,
                            )
                            if self._hass:
                                ir.async_create_issue(
                                    self._hass,
                                    DOMAIN,
                                    f"fcm_stuck_{entry_id}",
                                    is_fixable=False,
                                    severity=ir.IssueSeverity.WARNING,
                                    translation_key="fcm_connection_stuck",
                                )
                        else:
                            _LOGGER.info(
                                "[entry=%s] Re-trying FCM registration in %.1fs",
                                entry_id,
                                delay,
                            )
                        nudged = await _nudge_sleep(delay)
                        if nudged:
                            backoff = 1.0
                        else:
                            backoff = min(backoff * 2, _MAX_SUPERVISOR_BACKOFF_S)
                        continue

                    # Telemetry (aggregate counters)
                    if self._hass:
                        ir.async_delete_issue(
                            self._hass, DOMAIN, f"fcm_stuck_{entry_id}"
                        )
                    self.last_start_monotonic = time.monotonic()
                    self.start_count += 1

                    try:
                        await pc.start()
                        _LOGGER.debug(
                            "[entry=%s] FCM client started; entering monitor loop",
                            entry_id,
                        )
                        self._update_last_activity_for_entry(
                            entry_id, pc, time.monotonic()
                        )
                        backoff = 1.0  # reset only after a successful start
                    except Exception as err:
                        _LOGGER.info(
                            "[entry=%s] FCM client failed to start: %s", entry_id, err
                        )

                    # Defense 2 snapshot: timestamp the entry into the
                    # monitor loop.
                    #
                    # Iter-11 (Codex follow-up): per-run latch tracking
                    # whether the client ever reached a connected/listening
                    # state. The cap is meant to detect a poison-message
                    # crash loop (client connects, decodes, dies, repeat),
                    # NOT ordinary connectivity outages where ``_listen()``
                    # exhausts its 5 connection retries (~15-20s) without
                    # ever reaching ``STARTED``. Without this gate, a normal
                    # MCS-unreachable / Google outage would cap the
                    # supervisor after 10 brief connection-failure runs and
                    # permanently disable FCM push until reload.
                    entry_start = time.monotonic()
                    became_started = False

                    while not stop_evt.is_set():
                        await asyncio.sleep(max(FCM_MONITOR_INTERVAL_S, 0.5))
                        state = getattr(pc, "run_state", None)
                        do_listen = getattr(pc, "do_listen", False)
                        monotonic_now = time.monotonic()
                        last_activity = self._update_last_activity_for_entry(
                            entry_id, pc, monotonic_now
                        )
                        stale_after = max(self._activity_stale_after_s, 0.0)
                        # Iter-11: track ``became_started`` symmetrically to
                        # the health probe (same fallback for libraries that
                        # do not expose a ``run_state`` enum).
                        if FcmPushClientRunState is None:
                            if do_listen:
                                became_started = True
                        elif state == FcmPushClientRunState.STARTED:
                            became_started = True
                        # Iter-12 (Codex follow-up): sampling-gap defense.
                        # When the full lifecycle ``CREATED -> STARTED ->
                        # CRASH`` happens between two monitor polls (>=0.5s),
                        # this loop reads STOPPED/!do_listen and ``became_started``
                        # would remain false. The vendored client's
                        # ``__setattr__`` hook latches ``_has_been_started``
                        # the moment the library assigns ``run_state =
                        # STARTED``, independent of polling cadence; mirror
                        # that observation here so the cap branch correctly
                        # classifies micro-window poison-message crashes.
                        became_started = became_started or bool(
                            getattr(pc, "_has_been_started", False)
                        )
                        healthy = (
                            (
                                FcmPushClientRunState is None
                                or state == FcmPushClientRunState.STARTED
                            )
                            and do_listen
                            and last_activity is not None
                            and (
                                stale_after == 0.0
                                or (monotonic_now - last_activity <= stale_after)
                            )
                        )
                        self._update_entry_health(entry_id, healthy)
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

                        if last_activity is None:
                            _LOGGER.info(
                                "[entry=%s] FCM client has no activity timestamp; scheduling restart",
                                entry_id,
                            )
                            break

                        if (
                            stale_after > 0.0
                            and monotonic_now - last_activity > stale_after
                        ):
                            _LOGGER.info(
                                "[entry=%s] FCM client activity stale (age=%.1fs); scheduling restart",
                                entry_id,
                                monotonic_now - last_activity,
                            )
                            break

                    # Defense 2: short-run crash cap. Counts consecutive
                    # monitor exits with ``run_duration <
                    # _SHORT_RUN_THRESHOLD_S`` THAT ALSO reached
                    # ``STARTED`` at least once during the run. After
                    # ``_MAX_CONSECUTIVE_SHORT_RUNS`` such runs, surface a
                    # non-fixable repair issue and stop the loop. The
                    # comparison is strictly ``<`` (a 30.0s run counts as
                    # healthy and resets the counter).
                    #
                    # Iter-11 (Codex follow-up): the ``became_started`` gate
                    # ensures the cap is specific to the poison-message
                    # scenario (client connects, decodes, dies, repeat) and
                    # excludes pre-start connection/login failures. A burst
                    # of MCS-unreachable failures (5 retries x ~3-4s each)
                    # would otherwise be counted as short-run crashes and
                    # permanently disable FCM push after one ordinary
                    # network outage. Pre-start failures leave the counter
                    # AND the latch UNTOUCHED so that (a) a poison-message
                    # cascade interleaved with outages still accumulates
                    # correctly, and (b) a long pre-start hang does not
                    # falsely clear a real cap latch (no healthy proof).
                    run_duration = time.monotonic() - entry_start
                    if not became_started:
                        # Pre-start failure (no connected/listening state
                        # observed during this run). Do NOT touch the
                        # short-run counter or the cap latch.
                        pass
                    elif run_duration < _SHORT_RUN_THRESHOLD_S:
                        short_run_counter += 1
                        # Test hook: expose the closure counter to stubs
                        # without leaking it onto ``self``.
                        observe = getattr(pc, "_observe_counter", None)
                        if callable(observe):
                            try:
                                observe(short_run_counter)
                            except Exception:  # noqa: BLE001
                                pass
                        if short_run_counter >= _MAX_CONSECUTIVE_SHORT_RUNS:
                            message = (
                                f"FCM short-run crash loop: "
                                f"{_MAX_CONSECUTIVE_SHORT_RUNS} consecutive "
                                f"runs ended within "
                                f"{_SHORT_RUN_THRESHOLD_S:.0f}s "
                                f"(persistent poison message suspected)"
                            )
                            _LOGGER.error("[entry=%s] %s", entry_id, message)
                            self._fatal_errors[entry_id] = message
                            # Defense 2 iter-8: latch the cap state. While
                            # this latch is set, ``_clear_fatal_error_for_entry``
                            # is a no-op (registration / credential-update paths
                            # cannot clear the fatal map or the Repairs issue
                            # before a real healthy supervisor run proves the
                            # poison message is gone).
                            self._short_run_cap_latched.add(entry_id)
                            if self._hass:
                                ir.async_create_issue(
                                    self._hass,
                                    DOMAIN,
                                    f"fcm_short_run_crash_loop_{entry_id}",
                                    is_fixable=False,
                                    severity=ir.IssueSeverity.ERROR,
                                    translation_key="fcm_short_run_crash_loop",
                                )
                            # Cleanup the client before breaking out so
                            # the loop exit doesn't leave a half-stopped pc.
                            try:
                                await pc.stop()
                            except Exception:  # noqa: BLE001
                                pass
                            finally:
                                async with self._lock:
                                    self.pcs.pop(entry_id, None)
                                self._update_entry_health(entry_id, False)
                            # Defense 2 iter-9 (Codex follow-up): the cap is
                            # a TERMINAL state for this supervisor. ``break``
                            # alone only exits the inner monitor loop and
                            # falls through to the per-iteration restart
                            # block (``if not stop_evt.is_set(): ...
                            # _nudge_sleep(delay)``), which would spawn a
                            # fresh FCM client and continue the retry
                            # pressure / log spam the cap is meant to stop.
                            # Set the stop event so the outer ``while not
                            # stop_evt.is_set()`` exits, and pop the entry
                            # from ``_stop_evts`` so a legitimate
                            # re-registration / reload (which constructs a
                            # fresh event via ``setdefault``) is not
                            # short-circuited by the now-consumed event.
                            stop_evt.set()
                            self._stop_evts.pop(entry_id, None)
                            break
                    else:
                        short_run_counter = 0  # healthy run resets the counter
                        observe = getattr(pc, "_observe_counter", None)
                        if callable(observe):
                            try:
                                observe(short_run_counter)
                            except Exception:  # noqa: BLE001
                                pass
                        # Mirror the counter reset in the Issue Registry: if
                        # the cap had previously fired on a prior supervisor
                        # incarnation, a healthy run is the user-visible
                        # signal that the condition is gone.
                        #
                        # Defense 2 iter-8: a healthy run is the ONLY proof
                        # the system has that the poison-message condition is
                        # gone, so this is also the only callsite that may
                        # drop the cap latch. After dropping the latch, the
                        # cap-related ``_fatal_errors`` entry is removed and
                        # the aggregate ``_fatal_error`` re-derived (so the
                        # System Health surface stops showing the stale
                        # message). The Repairs delete below stays inside
                        # ``self._hass is not None`` and intentionally also
                        # runs when no latch was set (cheap idempotent UI
                        # cleanup).
                        cap_was_latched = entry_id in self._short_run_cap_latched
                        if cap_was_latched:
                            self._short_run_cap_latched.discard(entry_id)
                            cap_message = self._fatal_errors.get(entry_id)
                            if cap_message and cap_message.startswith(
                                "FCM short-run crash loop:"
                            ):
                                self._fatal_errors.pop(entry_id, None)
                                self._fatal_error = next(
                                    iter(self._fatal_errors.values()), None
                                )
                                _LOGGER.info(
                                    "[entry=%s] FCM short-run cap latch released "
                                    "by healthy supervisor run",
                                    entry_id,
                                )
                        if self._hass is not None:
                            try:
                                ir.async_delete_issue(
                                    self._hass,
                                    DOMAIN,
                                    f"fcm_short_run_crash_loop_{entry_id}",
                                )
                            except Exception:  # noqa: BLE001
                                pass

                    # Cleanup before restart
                    try:
                        await pc.stop()
                    except Exception:
                        pass
                    finally:
                        async with self._lock:
                            self.pcs.pop(entry_id, None)
                        self._update_entry_health(entry_id, False)

                    if not stop_evt.is_set():
                        delay = backoff + random.uniform(0.1, 0.3) * backoff  # nosec B311
                        _LOGGER.info(
                            "[entry=%s] Restarting FCM client in %.1fs", entry_id, delay
                        )
                        nudged = await _nudge_sleep(delay)
                        if nudged:
                            backoff = 1.0
                        else:
                            backoff = min(backoff * 2, _MAX_SUPERVISOR_BACKOFF_S)
            except asyncio.CancelledError:
                _LOGGER.debug("[entry=%s] FCM supervisor cancelled", entry_id)
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("[entry=%s] FCM supervisor crashed: %s", entry_id, err)
            finally:
                _LOGGER.info("[entry=%s] FCM supervisor stopped", entry_id)
                self._update_entry_health(entry_id, False)
                self._retry_nudge_evts.pop(entry_id, None)

        task = asyncio.create_task(
            _supervisor(), name=f"{DOMAIN}.fcm_supervisor[{entry_id}]"
        )
        self.supervisors[entry_id] = task
        _LOGGER.info("Started FCM supervisor for entry %s", entry_id)

    async def _invalidate_fcm_tokens(self, entry_id: str) -> None:
        """Clear renewable FCM tokens while preserving GCM device identity.

        After this, the next registration attempt will use
        ``reregister_keeping_identity()`` to get fresh FCM tokens with
        the same android_id/security_token.
        """
        creds = self.creds.get(entry_id)
        if creds and isinstance(creds, dict):
            # Remove FCM-related parts, keep GCM device identity
            creds.pop("fcm", None)
            creds.pop("keys", None)
            gcm = creds.get("gcm")
            if isinstance(gcm, dict):
                gcm.pop("token", None)
                gcm.pop("app_id", None)

            # Persist partial credentials so restarts also trigger re-register
            entry_cache = self._entry_caches.get(entry_id)
            if entry_cache is not None:
                try:
                    await entry_cache.set("fcm_credentials", creds)
                except Exception:
                    pass

            _LOGGER.info(
                "[entry=%s] Invalidated FCM tokens; "
                "android_id/security_token preserved for re-registration",
                entry_id,
            )

    @staticmethod
    def _raise_if_fatal_client_error(
        entry_id: str, err: ClientError
    ) -> None:
        """Raise ``FatalRegistrationError`` for 401/404 client errors."""
        status_raw = getattr(err, "status", None)
        if status_raw is None:
            status_raw = getattr(err, "status_code", None)
        try:
            status_int = int(cast(int | str, status_raw))
        except (TypeError, ValueError):
            status_int = None

        if status_int == _HTTP_UNAUTHORIZED:
            raise FatalRegistrationError(
                f"FCM registration auth failed ({status_int}): "
                "token renewal needed",
                is_auth_error=True,
            ) from err

        if status_int == _HTTP_NOT_FOUND:
            raise FatalRegistrationError(
                f"FCM registration endpoint not found ({status_int}): "
                "may be transient",
                is_auth_error=False,
            ) from err

        _LOGGER.error(
            "[entry=%s] FCM registration client error (status=%s): %s",
            entry_id,
            status_raw,
            err,
        )

    @staticmethod
    def _raise_if_fatal_http_error(
        entry_id: str, err: FcmRegisterHTTPError
    ) -> None:
        """Map ``FcmRegisterHTTPError`` to ``FatalRegistrationError``.

        ``FcmRegisterHTTPError`` is raised inside the firebase_messaging helper
        functions (``fcm_install``, ``fcm_register``, ``fcm_refresh_install_token``,
        ``gcm_check_in``, ``gcm_register``) when an FCM/GCM endpoint returns 401
        or 404. Because those helpers do not propagate aiohttp ``ClientError``,
        the existing ``_raise_if_fatal_client_error`` path cannot see those
        statuses; this companion classifier closes that gap so the
        ``_register_for_fcm_entry`` retry budget (auth vs. endpoint) runs.
        """
        status_int = int(err.status)

        if status_int == _HTTP_UNAUTHORIZED:
            raise FatalRegistrationError(
                f"FCM registration auth failed ({status_int}): "
                "token renewal needed",
                is_auth_error=True,
            ) from err

        if status_int == _HTTP_NOT_FOUND:
            raise FatalRegistrationError(
                f"FCM registration endpoint not found ({status_int}): "
                "may be transient",
                is_auth_error=False,
            ) from err

        _LOGGER.error(
            "[entry=%s] FCM register fatal HTTP error (status=%s): %s",
            entry_id,
            status_int,
            err,
        )

    async def _register_for_fcm_entry(self, entry_id: str) -> bool:
        """Single registration attempt for a specific entry."""
        pc = self.pcs.get(entry_id)
        if not pc:
            return False
        try:
            creds = getattr(pc, "credentials", None)
            # If FCM tokens were invalidated (partial creds with only GCM
            # identity), re-register while preserving android_id.
            needs_reregister = (
                isinstance(creds, dict)
                and "gcm" in creds
                and "fcm" not in creds
            )
            if needs_reregister:
                _LOGGER.info(
                    "[entry=%s] Partial credentials detected; "
                    "re-registering with existing device identity",
                    entry_id,
                )
                token_or_creds = await pc.reregister_keeping_identity()
            else:
                token_or_creds = await pc.checkin_or_register()

            if token_or_creds:
                _LOGGER.info("[entry=%s] FCM registered successfully", entry_id)
                token = self.get_fcm_token(entry_id)
                if token:
                    self._update_token_routing(token, {entry_id})
                    await self._persist_routing_token(entry_id, token)
                self._clear_fatal_error_for_entry(
                    entry_id, reason="Registration succeeded"
                )
                # Reset fatal retry counters on success
                self._fatal_retry_counts.pop(f"{entry_id}:auth", None)
                self._fatal_retry_counts.pop(f"{entry_id}:endpoint", None)
                return True
            _LOGGER.warning("[entry=%s] FCM registration returned no token", entry_id)
            return False
        except FatalRegistrationError:
            raise
        except ClientError as err:
            self._raise_if_fatal_client_error(entry_id, err)
            return False
        except FcmRegisterHTTPError as err:
            # MUST precede the generic RuntimeError catch below, because
            # FcmRegisterHTTPError is a RuntimeError subclass. This branch
            # turns persistent 401/404 from the firebase_messaging helpers
            # into a FatalRegistrationError so the supervisor's auth/endpoint
            # retry budget (with _invalidate_fcm_tokens) runs.
            self._raise_if_fatal_http_error(entry_id, err)
            return False
        except (TimeoutError, RuntimeError, Exception) as err:  # noqa: BLE001
            # Transient errors (TimeoutError, RuntimeError from firebase_messaging)
            # are logged at info level; other exceptions at error level.
            if isinstance(err, (TimeoutError, RuntimeError)):
                _LOGGER.info(
                    "[entry=%s] FCM registration failed (transient): %s - will retry",
                    entry_id,
                    err,
                )
            else:
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
                self._dispatch_to_hass_loop(
                    cache.set("fcm_credentials", pending_creds),
                    label=f"set_pending_creds_{entry.entry_id}",
                )

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

                self._dispatch_to_hass_loop(
                    _flush_tokens(),
                    label=f"flush_pending_tokens_{entry.entry_id}",
                )

        # Mirror any known credentials to this entry cache
        try:
            creds = self.creds.get(entry.entry_id)
            if creds and cache is not None:
                self._dispatch_to_hass_loop(
                    cache.set("fcm_credentials", creds),
                    label=f"mirror_creds_{entry.entry_id}",
                )
        except Exception as err:
            _LOGGER.debug("Entry-scoped credentials persistence skipped: %s", err)

        # Update routing with any token we already have
        token = self.get_fcm_token(entry.entry_id)
        if token:
            self._update_token_routing(token, {entry.entry_id})
            self._dispatch_to_hass_loop(
                self._persist_routing_token(entry.entry_id, token),
                label=f"persist_routing_token_{entry.entry_id}",
            )

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

            self._dispatch_to_hass_loop(
                _load_tokens(),
                label=f"load_persisted_tokens_{entry.entry_id}",
            )

        # Start supervisor for this entry
        self._dispatch_to_hass_loop(
            self._start_supervisor_for_entry(entry.entry_id, cache),
            label=f"start_supervisor_{entry.entry_id}",
        )

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

        if entry_id is not None:
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
                # Hard-reset on unregister: the entry is going away, so the
                # short-run cap latch must be released together with the
                # fatal-error map and the Repairs issue (force=True).
                self._clear_fatal_error_for_entry(
                    entry_id, reason="Entry unregistered", force=True
                )

    # -------------------- Incoming notifications --------------------

    def _on_notification(
        self,
        entry_id: str,
        payload: Mapping[str, Any],
        persistent_id: str | None,
        context: Any | None,
    ) -> None:
        """Handle incoming FCM notification (sync callback from per-entry client).

        This callback may be invoked from any thread by the FCM client.
        We use _dispatch_to_hass_loop for thread-safe async dispatch (P0 fix).
        """
        _ = persistent_id  # maintained for signature compatibility
        _ = context

        # Thread-safe dispatch to HA event loop
        label = f"fcm-notification[{entry_id[:8] if entry_id else 'unknown'}]"
        self._dispatch_to_hass_loop(
            self._handle_notification_async(entry_id, payload),
            label=label,
        )

    async def _handle_notification_async(
        self, entry_id: str, payload: Mapping[str, Any]
    ) -> None:
        """Async handler executed in HA loop: parse, route, and schedule downstream work."""
        try:
            hex_string = self._extract_hex_payload(payload)
            if hex_string is None:
                return

            # P1 fix: offload protobuf parsing to executor
            canonic_id = await self._extract_canonic_id_async(hex_string)
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
                await self._run_callback_async(cb, canonic_id, hex_string)
                return

            # Log FCM pushes that have no registered callback (e.g. sound
            # confirmations, device status updates).  This fires only in
            # response to a user-initiated action (Play Sound button etc.)
            # so it does not create log spam during normal operation.
            _LOGGER.debug(
                "FCM push for %s has no registered callback "
                "(may be action confirmation): payload_len=%d, hex_prefix=%s",
                canonic_id[:8],
                len(hex_string),
                hex_string[:120] if hex_string else "(empty)",
            )

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

            await self._process_background_update(
                entry_id, canonic_id, hex_string, target_entries
            )

        except Exception:
            _LOGGER.exception("Failed to handle FCM notification safely")

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

        if entry_id is not None:
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
        """Extract canonical id via the decoder.

        Handles the schema difference between Android devices (nested in phoneInformation)
        and Spot/Tracker devices (direct canonicIds), as defined in DeviceUpdate.proto.
        """
        try:
            device_update = decoder_module.parse_device_update_protobuf(hex_response)
            if device_update.HasField("deviceMetadata"):
                info = device_update.deviceMetadata.identifierInformation

                # Aligns with ProtoDecoders.decoder.get_canonic_ids logic:
                # type == 1 → Android devices store IDs under phoneInformation.canonicIds.
                if info.type == 1:
                    ids = info.phoneInformation.canonicIds.canonicId
                else:
                    ids = info.canonicIds.canonicId

                if ids:
                    return ids[0].id
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to extract canonical id from FCM response: %s", err)
        return None

    async def _extract_canonic_id_async(self, hex_response: str) -> str | None:
        """Extract canonical ID via the decoder in an executor (P1-1 fix).

        Offloads CPU-bound protobuf parsing to avoid blocking the event loop.
        """
        return await _call_in_executor(
            self._extract_canonic_id_from_response, hex_response
        )

    async def _run_callback_async(
        self, callback: Callable[[str, str], None], canonic_id: str, hex_string: str
    ) -> None:
        """Run a user callback safely without unhandled task exceptions (P0-2 fix).

        Catches and logs exceptions to prevent 'Task exception was never retrieved'.
        """
        try:
            await _call_in_executor(callback, canonic_id, hex_string)
        except Exception:
            # Do NOT log the full payload - use length only for safety
            _LOGGER.exception(
                "FCM locate callback failed (canonic_id=%s, payload_len=%d)",
                canonic_id[:8] if canonic_id else "unknown",
                len(hex_string) if hex_string else 0,
            )

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
            location_data = await self._decode_background_location_async(
                entry_id, hex_string
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

    async def _decode_background_location_async(  # noqa: PLR0911
        self, entry_id: str, hex_string: str
    ) -> JSONDict:
        """Decode background location using protobuf decoders.

        P1 fixes applied:
        - Protobuf parsing offloaded to executor (non-blocking)
        - Scoped cache provider prevents cross-contamination between entries
        """
        try:
            # P1-1: Offload CPU-bound protobuf parsing to executor
            device_update = await _call_in_executor(
                decoder_module.parse_device_update_protobuf, hex_string
            )
            cache = self._entry_caches.get(entry_id)
            if cache is None:
                _LOGGER.error(
                    "No TokenCache available for entry %s during background decrypt",
                    entry_id,
                )
                return {}

            # P1-2: Use scoped context manager for proper cleanup
            with self._scoped_cache_provider(lambda: cache):
                try:
                    raw_locations = await async_decrypt_location_response_locations(
                        device_update, cache=cache
                    )
                except StaleOwnerKeyError:
                    _LOGGER.info(
                        "Background location update skipped (stale key) for entry %s",
                        entry_id,
                    )
                    return {}
                except DecryptionError as dec_err:
                    _LOGGER.warning(
                        "Background location decryption failed for entry %s: %s. "
                        "This may resolve after re-authentication or key refresh.",
                        entry_id,
                        dec_err,
                    )
                    return {}
                except InvalidTag:
                    _LOGGER.warning(
                        "Background location decryption auth failed (InvalidTag) "
                        "for entry %s. Cached identity key may be stale; "
                        "will retry on next update.",
                        entry_id,
                    )
                    return {}

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

            return dict(best_record) if best_record is not None else dict(locations[0])
        except Exception as err:  # noqa: BLE001
            _LOGGER.error(
                "Failed to decode background location data (%s): %s",
                type(err).__name__,
                err,
            )
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
            self._dispatch_to_hass_loop(
                self._persist_routing_token(entry_id, token),
                label=f"persist_routing_token_{entry_id}",
            )
        self._clear_fatal_error_for_entry(
            entry_id, reason="Credentials updated for entry"
        )

        self._dispatch_to_hass_loop(
            self._async_save_credentials_for_entry(entry_id),
            label=f"save_credentials_{entry_id}",
        )
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

    async def async_reregister_fcm(self, entry_id: str) -> bool:
        """Public API: invalidate FCM tokens and re-register for a specific entry.

        Preserves the GCM device identity (android_id/security_token) and
        obtains fresh GCM + FCM tokens via ``reregister_keeping_identity()``.

        Returns True if re-registration was initiated (supervisor restarted),
        False if the entry is not known to this receiver.
        """
        if entry_id not in self.pcs and entry_id not in self.creds:
            _LOGGER.warning(
                "[entry=%s] Cannot re-register FCM: entry not known to receiver",
                entry_id,
            )
            return False

        # 1. Invalidate FCM tokens (keeps GCM identity)
        await self._invalidate_fcm_tokens(entry_id)

        # 2. Stop the supervisor for this entry
        stop_evt = self._stop_evts.get(entry_id)
        if stop_evt is not None:
            stop_evt.set()
        task = self.supervisors.pop(entry_id, None)
        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass

        # 3. Reset the stop event BEFORE stopping the client to prevent
        #    a race where concurrent code sees the old (set) event.
        self._stop_evts[entry_id] = asyncio.Event()

        # 4. Stop the client so it can be re-created
        pc = self.pcs.pop(entry_id, None)
        if pc is not None:
            try:
                await asyncio.wait_for(pc.stop(), timeout=5.0)
            except (TimeoutError, ConnectionError, asyncio.CancelledError):
                pass
            except Exception:  # noqa: BLE001
                pass

        # 5. Restart the supervisor (will re-register with fresh tokens)
        cache = self._entry_caches.get(entry_id)
        await self._start_supervisor_for_entry(entry_id, cache)

        _LOGGER.info(
            "[entry=%s] FCM re-registration initiated; "
            "supervisor restarted with invalidated tokens",
            entry_id,
        )
        return True

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
            except ConnectionError as err:
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
        self._entry_last_activity_monotonic.pop(entry_id, None)
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
        Otherwise returns the token for the receiver's default entry when set,
        falling back to the first available token across clients.
        """
        target_entry = entry_id if entry_id is not None else self._default_entry_id

        if target_entry:
            creds = self.creds.get(target_entry)
            if isinstance(creds, dict):
                tok = (creds.get("fcm") or {}).get("registration", {}).get("token")
                if isinstance(tok, str) and tok:
                    return tok
            # Also try the current client's live creds if present
            pc = self.pcs.get(target_entry)
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

    def set_default_entry_id(self, entry_id: str | None) -> None:
        """Record the preferred entry_id for legacy token lookups."""

        if isinstance(entry_id, str):
            self._default_entry_id = entry_id
            return
        self._default_entry_id = None

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
            if candidate_entry is None:
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

            # Wait for the FCM client to be actively listening before sending
            # the location request.  Without this, the push notification from
            # Google may arrive before the MCS connection is established.
            pc = self.pcs.get(entry_id)
            if pc is not None:
                _MAX_READY_WAIT_S = 15.0
                _POLL_INTERVAL_S = 0.3
                waited = 0.0
                while waited < _MAX_READY_WAIT_S:
                    run_state = getattr(pc, "run_state", None)
                    if (
                        FcmPushClientRunState is not None
                        and run_state == FcmPushClientRunState.STARTED
                    ):
                        break
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    waited += _POLL_INTERVAL_S
                else:
                    _LOGGER.warning(
                        "[entry=%s] FCM client not in STARTED state after %.1fs; "
                        "location request may time out",
                        entry_id,
                        _MAX_READY_WAIT_S,
                    )

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
