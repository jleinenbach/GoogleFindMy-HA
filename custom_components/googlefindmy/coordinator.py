# custom_components/googlefindmy/coordinator.py
"""Data coordinator for Google Find My Device (async-first, HA-friendly).

Discovery vs. polling semantics:
- Every coordinator tick fetches the lightweight **full** Google device list.
- Presence and name/capability caches are updated for all devices.
- The published snapshot (`self.data`) contains **all** devices (for dynamic entity creation).
- The sequential **polling cycle** polls **only devices that are enabled** in Home Assistant's
  Device Registry (devices with `disabled_by is None`). Devices explicitly ignored via options
  are filtered out as well.

Google Home semantic locations (note):
- When the Google Home filter identifies a "Google Home-like" semantic location,
  we substitute **Home zone coordinates** (lat/lon[/radius]) instead of forcing
  a zone label. This lets HA Core's zone engine set the state to `home`, which
  aligns with best practices.

Thread-safety and quality goals:
- All state mutations and task creations occur on HA's event loop thread.
- Public methods that may be invoked from background threads marshal execution
  onto the loop using a single hop (no chained hops).
- Owner-driven locates introduce a server-side purge/cooldown window; we respect
  this via **per-device poll cooldowns** with **dynamic guardrails** (min/max bounds),
  without changing any external API or entity fields.

Implementation notes (server behaviour, POPETS'25):
- The network applies *type-specific throttling* to crowdsourced reports:
  "In All Areas" reports are effectively throttled for ~10 minutes,
  "High Traffic" reports for ~5 minutes. We respect this by applying
  per-device cooldowns derived from an internal `_report_hint` (set by the
  decrypt/parse layer) without changing public APIs or entity attributes.
- The well-known "~9h" rate limit discussed in the paper applies to *finder*
  devices contributing reports, not to the owner pulling locations. We **document**
  this here for maintainers but do **not** enforce it client-side.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Protocol, Set

from homeassistant.components.recorder import (
    get_instance as get_recorder,
    history as recorder_history,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
# HA session is provided by the integration and reused across I/O
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import ConfigEntryAuthFailed

from .api import GoogleFindMyAPI
from .const import (
    DOMAIN,
    UPDATE_INTERVAL,
    LOCATION_REQUEST_TIMEOUT_S,
    DEFAULT_MIN_POLL_INTERVAL,
    OPT_IGNORED_DEVICES,
    DEFAULT_OPTIONS,
    coerce_ignored_mapping,
)

_LOGGER = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Internal guardrails for dynamic cooldowns (module-local on purpose)
# Rationale: Only used by this coordinator; keep const.py focused on cross-cutting constants.
# Values are *bounds* that clamp dynamic guesses; they are not fixed thresholds.
# -------------------------------------------------------------------------
_COOLDOWN_OWNER_MIN_S = 300      # 5 min
_COOLDOWN_OWNER_MAX_S = 900      # 15 min

# Server-informed minimum cooldowns per report type (POPETS'25)
# We never go below these values; actual applied cooldown also respects the user poll interval.
_COOLDOWN_MIN_IN_ALL_AREAS_S = 600  # 10 min
_COOLDOWN_MIN_HIGH_TRAFFIC_S  = 300  # 5 min


def _clamp(val: float, lo: float, hi: float) -> float:
    """Return val clamped into [lo, hi]."""
    return max(lo, min(hi, val))


class CacheProtocol(Protocol):
    """Defines the interface for a cache that the coordinator can use.

    This protocol ensures that any cache object passed to the coordinator
    provides the necessary asynchronous methods for getting and setting values.
    """

    async def async_get_cached_value(self, key: str) -> Any: ...
    async def async_set_cached_value(self, key: str, value: Any) -> None: ...


# -------------------------------------------------------------------------
# Synchronous history helper (runs in Recorder executor)
# -------------------------------------------------------------------------
def _sync_get_last_gps_from_history(
    hass: HomeAssistant, entity_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch last state change with GPS coordinates via Recorder History (sync).

    This function is designed to be run in a worker thread (specifically the
    Home Assistant Recorder's executor) to avoid blocking the event loop with
    database queries.

    IMPORTANT:
    - Must run in a worker (Recorder executor). Do not call asyncio APIs here.
    - Keep queries minimal to avoid heavy DB load.

    Args:
        hass: The Home Assistant instance.
        entity_id: The entity ID of the device_tracker to query.

    Returns:
        A dictionary containing the last known location data, or None if not found.
    """
    try:
        # Minimal query: the last single change for this entity_id
        changes = recorder_history.get_last_state_changes(hass, 1, entity_id)
        samples = changes.get(entity_id, [])
        if not samples:
            return None

        last_state = samples[-1]
        attrs = getattr(last_state, "attributes", {}) or {}
        lat = attrs.get("latitude")
        lon = attrs.get("longitude")
        if lat is None or lon is None:
            return None

        return {
            "latitude": lat,
            "longitude": lon,
            "accuracy": attrs.get("gps_accuracy"),
            "last_seen": int(last_state.last_updated.timestamp()),
            "status": "Using historical data",
        }
    except Exception as err:
        _LOGGER.debug("History lookup failed for %s: %s", entity_id, err)
        return None


class GoogleFindMyCoordinator(DataUpdateCoordinator[List[Dict[str, Any]]]):
    """Coordinator that manages polling, cache, and push updates for Google Find My Device.

    Thread-safety & event loop rules (IMPORTANT):
    - All interactions that create HA tasks or publish state must occur on the HA event loop thread.
    - This class provides small helpers to "hop" from any background thread into the loop
      using `loop.call_soon_threadsafe(...)` before touching HA APIs.

    Pitfalls & mitigations (general guidance for future reviewers):
    - Pitfall 1 – return values with `call_soon_threadsafe`:
      `call_soon_threadsafe` does not propagate return values to the calling thread.
      **Mitigation:** All methods we marshal to the loop in this module (e.g. `increment_stat`,
      `update_device_cache`, `push_updated`, `purge_device`) are consciously `None`-returning.
    - Pitfall 2 – excessive thread hops:
      Unnecessary hops add overhead if used for micro-operations.
      **Mitigation:** We hop **once at the public method boundary**, then execute the
      complete logic on the HA loop (single-threaded, deterministic).
    - Pitfall 3 – complex external locks:
      Using extra `threading.Lock`s for shared state increases complexity and risk.
      **Mitigation:** We **serialize** state changes by marshalling to the **single-threaded**
      HA event loop – the loop itself is the synchronization primitive.
    """

    # ---------------------------- Lifecycle ---------------------------------
    def __init__(
        self,
        hass: HomeAssistant,
        cache: CacheProtocol,
        *,
        location_poll_interval: int = 300,
        device_poll_delay: int = 5,
        min_poll_interval: int = DEFAULT_MIN_POLL_INTERVAL,
        min_accuracy_threshold: int = 100,
        movement_threshold: int = 50,
        allow_history_fallback: bool = False,
    ) -> None:
        """Initialize the coordinator.

        This sets up the central data management for the integration, including
        API communication, state caching, and polling logic.

        Notes:
            - Credentials and related metadata are provided via the entry-scoped TokenCache.
            - The HA-managed aiohttp ClientSession is reused to avoid per-call pools.

        Args:
            hass: The Home Assistant instance.
            cache: An object implementing the CacheProtocol for persistent storage.
            location_poll_interval: The interval in seconds between polling cycles.
            device_poll_delay: The delay in seconds between polling individual devices.
            min_poll_interval: The minimum allowed interval between polling cycles.
            min_accuracy_threshold: The minimum GPS accuracy in meters to accept a location.
            movement_threshold: Movement delta in meters for significance gating (default 50 m).
            allow_history_fallback: Whether to fall back to Recorder history for location.
        """
        self.hass = hass
        self._cache = cache

        # Get the singleton aiohttp.ClientSession from Home Assistant and reuse it.
        self._session = async_get_clientsession(hass)
        self.api = GoogleFindMyAPI(cache=self._cache, session=self._session)

        # Configuration (user options; updated via update_settings())
        self.location_poll_interval = int(location_poll_interval)
        self.device_poll_delay = int(device_poll_delay)
        self.min_poll_interval = int(min_poll_interval)  # hard lower bound between cycles
        self._min_accuracy_threshold = int(min_accuracy_threshold)  # quality filter (meters)
        self._movement_threshold = int(movement_threshold)  # meters; used by significance gate
        self.allow_history_fallback = bool(allow_history_fallback)

        # Internal caches & bookkeeping
        self._device_location_data: Dict[str, Dict[str, Any]] = {}  # device_id -> location dict
        self._device_names: Dict[str, str] = {}  # device_id -> human name
        self._device_caps: Dict[str, Dict[str, Any]] = {}  # device_id -> caps (e.g., {"can_ring": True})
        self._present_device_ids: Set[str] = set()  # ids from latest full device list (unfiltered)

        # Polling state
        self._poll_lock = asyncio.Lock()
        self._is_polling = False
        self._startup_complete = False
        self._last_poll_mono: float = 0.0  # monotonic timestamp for scheduling
        # FCM deferral/escalation bookkeeping (to surface issues without log spam)
        # 0.0 means no active deferral window; stage marks last emitted severity.
        self._fcm_defer_started_mono: float = 0.0
        self._fcm_last_stage: int = 0  # 0=none, 1=warned, 2=errored

        # Push readiness memoization and cooldown after transport errors
        self._push_ready_memo: Optional[bool] = None
        self._push_cooldown_until: float = 0.0

        # Manual locate gating (UX + server protection)
        self._locate_inflight: Set[str] = set()                # device_id -> in-flight flag
        self._locate_cooldown_until: Dict[str, float] = {}     # device_id -> mono deadline

        # NEW: Per-device poll cooldowns after owner reports and type-hinted crowdsourced reports.
        # Only affects internal scheduling; no external API changes.
        self._device_poll_cooldown_until: Dict[str, float] = {}

        # Statistics (extend as needed)
        self.stats: Dict[str, int] = {
            "skipped_duplicates": 0,
            "background_updates": 0,  # FCM/push-driven updates + manual commits
            "polled_updates": 0,      # sequential poll-driven updates
            "crowd_sourced_updates": 0,
            "history_fallback_used": 0,
            "timeouts": 0,
            "invalid_coords": 0,
            "low_quality_dropped": 0,
            "non_significant_dropped": 0,
        }
        _LOGGER.debug("Initialized stats: %s", self.stats)

        # Debounced stats persistence (avoid flushing on every increment)
        self._stats_save_task: Optional[asyncio.Task] = None
        self._stats_debounce_seconds: float = 5.0

        # Load persistent statistics asynchronously (name the task for better debugging)
        self.hass.async_create_task(self._async_load_stats(), name=f"{DOMAIN}.load_stats")

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    # ---------------------------- Event loop helpers ------------------------
    def _is_on_hass_loop(self) -> bool:
        """Return True if currently executing on the HA event loop thread."""
        loop = self.hass.loop
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def _run_on_hass_loop(self, func, *args) -> None:
        """Schedule a plain callable to run on the HA loop thread ASAP.

        Note:
        - This is intentionally **fire-and-forget**; `call_soon_threadsafe` does not
          return the callable's result to the caller. Only use with functions that
          **return None** and are safe to run on the HA loop.
        """
        self.hass.loop.call_soon_threadsafe(func, *args)

    # ---------------------------- Cooldown helpers (server-aware) -----------
    def _compute_type_cooldown_seconds(self, report_hint: Optional[str]) -> int:
        """Return a server-aware cooldown duration in seconds for a crowdsourced report type.

        We derive cooldowns from POPETS'25 observations:
        - "in_all_areas": ~10 min throttle window (minimum).
        - "high_traffic": ~5 min throttle window (minimum).

        IMPORTANT:
        - To guarantee effect, the applied cooldown is **never shorter than** the
          configured `location_poll_interval`. This ensures at least one scheduled
          poll cycle is skipped in practice (see review note).
        """
        if not report_hint:
            return 0

        # Guarantee the cooldown always spans at least one poll interval
        effective_poll = max(1, int(self.location_poll_interval))
        if report_hint == "in_all_areas":
            base_cooldown = _COOLDOWN_MIN_IN_ALL_AREAS_S
        elif report_hint == "high_traffic":
            base_cooldown = _COOLDOWN_MIN_HIGH_TRAFFIC_S
        else:
            return 0

        return max(base_cooldown, effective_poll)

    def _apply_report_type_cooldown(self, device_id: str, report_hint: Optional[str]) -> None:
        """Apply a per-device **poll** cooldown based on the crowdsourced report type.

        - Does nothing for None/unknown hints.
        - Uses monotonic time, and **extends** any existing cooldown (takes the max).
        - Internal only; does not touch public APIs or entity attributes.
        """
        try:
            seconds = int(self._compute_type_cooldown_seconds(report_hint))
        except Exception:  # defensive
            seconds = 0
        if seconds <= 0:
            return

        now_mono = time.monotonic()
        new_deadline = now_mono + float(seconds)
        prev_deadline = self._device_poll_cooldown_until.get(device_id, 0.0)
        if new_deadline > prev_deadline:
            self._device_poll_cooldown_until[device_id] = new_deadline
            _LOGGER.debug(
                "Applied %ss poll cooldown for %s (hint='%s', poll_interval=%ss)",
                seconds,
                device_id,
                report_hint,
                self.location_poll_interval,
            )

    # ---------------------------- Coordinate normalization ------------------
    def _normalize_coords(
        self,
        payload: Dict[str, Any],
        *,
        device_label: Optional[str] = None,
        warn_on_invalid: bool = True,
    ) -> bool:
        """Validate and normalize latitude/longitude (and optionally accuracy).

        - Accepts numeric-like strings and converts them to floats.
        - Rejects NaN/Inf and out-of-range values.
        - Writes normalized floats back into `payload` when valid.
        - Normalizes `accuracy` to a finite float if present (best-effort).

        Returns:
            True if latitude/longitude are present and valid after normalization.
            False if coordinates are missing or invalid.

        Side effects:
            - Increments `invalid_coords` on invalid input.
            - Logs warnings for invalid data (unless warn_on_invalid=False).
        """
        lat = payload.get("latitude")
        lon = payload.get("longitude")
        if lat is None or lon is None:
            # Missing coordinates is not an error per se (semantic-only is valid).
            return False

        try:
            lat_f, lon_f = float(lat), float(lon)
        except (TypeError, ValueError):
            self.increment_stat("invalid_coords")
            if warn_on_invalid:
                _LOGGER.warning(
                    "Ignoring invalid (non-numeric) coordinates%s: lat=%r, lon=%r",
                    f" for {device_label}" if device_label else "",
                    lat,
                    lon,
                )
            return False

        if not (
            math.isfinite(lat_f)
            and math.isfinite(lon_f)
            and -90.0 <= lat_f <= 90.0
            and -180.0 <= lon_f <= 180.0
        ):
            self.increment_stat("invalid_coords")
            if warn_on_invalid:
                _LOGGER.warning(
                    "Ignoring out-of-range/invalid coordinates%s: lat=%s, lon=%s",
                    f" for {device_label}" if device_label else "",
                    lat,
                    lon,
                )
            return False

        # Write back normalized floats
        payload["latitude"] = lat_f
        payload["longitude"] = lon_f

        # Best-effort normalize accuracy (if present)
        acc = payload.get("accuracy")
        if acc is not None:
            try:
                acc_f = float(acc)
                if math.isfinite(acc_f):
                    payload["accuracy"] = acc_f
            except (TypeError, ValueError):
                # Accuracy can be absent or malformed; not critical enough for a warning.
                pass

        return True

    # ---------------------------- Ignore helpers ----------------------------
    def _get_ignored_set(self) -> Set[str]:
        """Return the set of device IDs the user chose to ignore (options-first).

        Notes:
            - Uses config_entry.options if available; falls back to an attribute
              'ignored_devices' when set through update_settings().
            - Intentionally simple equality (no normalization) to avoid surprises.
        """
        try:
            entry = getattr(self, "config_entry", None)
            if entry is not None:
                raw = entry.options.get(OPT_IGNORED_DEVICES, DEFAULT_OPTIONS.get(OPT_IGNORED_DEVICES, {}))
                # Accept list[str] (legacy) or mapping (current)
                mapping, _migrated = coerce_ignored_mapping(raw)
                if mapping:
                    return set(mapping.keys())
        except Exception:  # defensive
            pass
        raw_attr = getattr(self, "ignored_devices", None)
        if isinstance(raw_attr, list):
            return set(x for x in raw_attr if isinstance(x, str))
        return set()

    def is_ignored(self, device_id: str) -> bool:
        """Return True if the device is currently ignored by user choice."""
        return device_id in self._get_ignored_set()

    # Public read-only state for diagnostics/UI
    @property
    def is_polling(self) -> bool:
        """Expose current polling state (public read-only API).

        Returns:
            True if a polling cycle is currently in progress.
        """
        return self._is_polling

    # ---------------------------- HA Coordinator ----------------------------
    def _is_fcm_ready_soft(self) -> bool:
        """Best-effort check if the shared FCM receiver reports readiness.

        Notes:
            - Reads the HA-managed singleton from hass.data[DOMAIN]['fcm_receiver'].
            - Only inspects simple boolean properties (no network, no awaits).
            - Returns False if the receiver is missing or readiness cannot be determined.
        """
        try:
            bucket = self.hass.data.get(DOMAIN, {})
            fcm = bucket.get("fcm_receiver")
            if fcm is None:
                return False
            for attr in ("is_ready", "ready"):
                val = getattr(fcm, attr, None)
                if isinstance(val, bool):
                    return val
        except Exception:
            # Be defensive: any inspection error means "not ready".
            pass
        return False

    def _note_fcm_deferral(self, now_mono: float) -> None:
        """Advance a quiet escalation timeline while FCM is not ready.

        Emits at most:
            - one WARNING after ~60s
            - one ERROR   after ~300s
        Resets when readiness returns.
        """
        if self._fcm_defer_started_mono == 0.0:
            self._fcm_defer_started_mono = now_mono
            self._fcm_last_stage = 0
            return
        elapsed = now_mono - self._fcm_defer_started_mono
        if elapsed >= 60 and self._fcm_last_stage < 1:
            self._fcm_last_stage = 1
            _LOGGER.warning(
                "Polling deferred: FCM/push not ready 60s after (re)start. Polls and actions remain gated."
            )
        if elapsed >= 300 and self._fcm_last_stage < 2:
            self._fcm_last_stage = 2
            _LOGGER.error(
                "Polling still deferred: FCM/push not ready after 5 minutes. Check credentials/network."
            )

    def _clear_fcm_deferral(self) -> None:
        """Clear the escalation timeline once FCM becomes ready (log once)."""
        if self._fcm_defer_started_mono:
            _LOGGER.info("FCM/push is ready; resuming scheduled polling.")
        self._fcm_defer_started_mono = 0.0
        self._fcm_last_stage = 0

    async def _async_update_data(self) -> List[Dict[str, Any]]:
        """Provide cached device data; trigger background poll if due.

        Discovery semantics:
        - Always fetch the **full** lightweight device list (no executor).
        - Update presence and metadata caches for **all** devices.
        - The published snapshot (`self.data`) contains **all** devices (for dynamic entity creation).
        - The sequential **polling cycle** polls devices that are enabled in HA's Device Registry
          and not explicitly ignored in integration options.

        Returns:
            A list of dictionaries, where each dictionary represents a device's state.

        Raises:
            ConfigEntryAuthFailed: If authentication fails during device list fetching.
            UpdateFailed: For other transient or unexpected errors.
        """
        try:
            # 1) Always fetch the lightweight FULL device list using native async API
            all_devices = await self.api.async_get_basic_device_list()
            all_devices = all_devices or []
            ignored = self._get_ignored_set()

            # Record presence from the full list (unfiltered by ignore for accuracy).
            self._present_device_ids = {
                d["id"] for d in all_devices if isinstance(d.get("id"), str)
            }

            # 2) Update internal name/capability caches for ALL devices
            for dev in all_devices:
                dev_id = dev["id"]
                self._device_names[dev_id] = dev.get("name", dev_id)

                # Normalize and cache the "can ring" capability
                if "can_ring" in dev:
                    can_ring = bool(dev.get("can_ring"))
                    slot = self._device_caps.setdefault(dev_id, {})
                    slot["can_ring"] = can_ring

            # 3) Decide whether to trigger a poll cycle (monotonic clock)
            now_mono = time.monotonic()
            effective_interval = max(self.location_poll_interval, self.min_poll_interval)

            # Build list of devices to POLL:
            # - must not be ignored
            # - must be enabled in Device Registry (disabled_by is None)
            dev_reg = dr.async_get(self.hass)

            def _is_enabled_in_registry(dev_id: str) -> bool:
                device = dev_reg.async_get_device(identifiers={(DOMAIN, dev_id)})
                # If no registry entry yet, allow polling (entity creation path). If present and disabled, skip.
                return device is None or device.disabled_by is None

            devices_to_poll = [
                d for d in all_devices
                if d["id"] not in ignored and _is_enabled_in_registry(d["id"])
            ]

            # Apply per-device poll cooldowns (owner locate purge window + type-aware cooldowns)
            if self._device_poll_cooldown_until and devices_to_poll:
                filtered: List[Dict[str, Any]] = []
                skipped = 0
                for d in devices_to_poll:
                    until = self._device_poll_cooldown_until.get(d["id"], 0.0)
                    if until and now_mono < until:
                        skipped += 1
                        continue
                    filtered.append(d)
                if skipped:
                    _LOGGER.debug(
                        "Per-device poll cooldown active; skipping %d device(s) this cycle",
                        skipped,
                    )
                devices_to_poll = filtered

            if not self._startup_complete:
                fcm_evt = getattr(self, "fcm_ready_event", None)
                if isinstance(fcm_evt, asyncio.Event) and not fcm_evt.is_set():
                    _LOGGER.debug("First poll cycle is waiting for FCM provider to become ready...")
                    try:
                        await asyncio.wait_for(fcm_evt.wait(), timeout=15.0)
                        _LOGGER.debug("FCM provider is ready; proceeding with first poll cycle.")
                    except asyncio.TimeoutError:
                        _LOGGER.warning(
                            "FCM provider not ready after 15s; proceeding with first poll cycle anyway."
                        )
                
                self._startup_complete = True
                
                if devices_to_poll:
                    _LOGGER.debug("Triggering initial poll cycle immediately after startup.")
                    self.hass.async_create_task(
                        self._async_start_poll_cycle(devices_to_poll),
                        name=f"{DOMAIN}.initial_poll_cycle",
                    )
            else:
                due = (now_mono - self._last_poll_mono) >= effective_interval
                if due and not self._is_polling and devices_to_poll:
                    # Gate polling on FCM readiness to prevent startup/reload race.
                    if not self._is_fcm_ready_soft():
                        # Defer polling: move baseline forward to avoid tight loops and escalate politely.
                        self._last_poll_mono = now_mono
                        self._note_fcm_deferral(now_mono)
                        _LOGGER.debug("Deferring polling cycle: FCM/push not ready yet (reload gating).")
                    else:
                        # If we just recovered, clear the escalation state once.
                        if self._fcm_defer_started_mono:
                            self._clear_fcm_deferral()
                        _LOGGER.debug(
                            "Scheduling background polling cycle (devices=%d, interval=%ds)",
                            len(devices_to_poll),
                            effective_interval,
                        )
                        # Always create tasks from the HA loop thread
                        self.hass.async_create_task(
                            self._async_start_poll_cycle(devices_to_poll),
                            name=f"{DOMAIN}.poll_cycle",
                        )
                else:
                    _LOGGER.debug(
                        "Poll not due (elapsed=%.1fs/%ss) or already running=%s",
                        now_mono - self._last_poll_mono,
                        effective_interval,
                        self._is_polling,
                    )

            # 4) Build data snapshot for devices visible to the user (ignore-filter applied)
            visible_devices = [d for d in all_devices if d["id"] not in ignored]
            snapshot = await self._async_build_device_snapshot_with_fallbacks(visible_devices)
            _LOGGER.debug(
                "Returning %d device entries; next poll in ~%ds",
                len(snapshot),
                int(max(0, effective_interval - (time.monotonic() - self._last_poll_mono))),
            )
            return snapshot

        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed:
            # Surface up to HA to trigger re-auth flow; do not wrap into UpdateFailed
            raise
        except UpdateFailed:
            # Let pre-wrapped UpdateFailed bubble as-is
            raise
        except Exception as exc:
            # Coordinator contract: raise UpdateFailed on unexpected errors
            raise UpdateFailed(exc) from exc

    # ---------------------------- Polling Cycle -----------------------------
    async def _async_start_poll_cycle(self, devices: List[Dict[str, Any]]) -> None:
        """Run a full sequential polling cycle in a background task.

        This runs with a lock to avoid overlapping cycles, updates the
        internal cache, and pushes snapshots at start and end.

        Throttling awareness:
        - If a device returns a crowdsourced location with `_report_hint` equal to
          "in_all_areas" (~10 min throttle) or "high_traffic" (~5 min throttle),
          we apply a per-device cooldown so subsequent polls avoid the throttled window.
          (See POPETS'25 for measured behaviour.)
        - The cooldown is at least the server minimum and at least one user poll interval.

        Args:
            devices: A list of device dictionaries to poll.
        """
        if not devices:
            return

        async with self._poll_lock:
            if self._is_polling:
                return

            # Double-check FCM readiness inside the lock to avoid a narrow race:
            # if readiness regressed between scheduling and execution, skip cleanly.
            if not self._is_fcm_ready_soft():
                _LOGGER.debug("Skipping polling cycle: FCM/push not ready yet.")
                # Move baseline to now so we don't immediately reschedule; keep escalation ticking.
                self._last_poll_mono = time.monotonic()
                self._note_fcm_deferral(self._last_poll_mono)
                return
            else:
                # If we were deferring previously, clear the escalation timeline.
                if self._fcm_defer_started_mono:
                    self._clear_fcm_deferral()

            self._is_polling = True
            # Push a snapshot from cache to signal "polling" state to listeners
            start_snapshot = self._build_snapshot_from_cache(devices, wall_now=time.time())
            self.async_set_updated_data(start_snapshot)
            _LOGGER.info("Starting sequential poll of %d devices", len(devices))

            try:
                for idx, dev in enumerate(devices):
                    dev_id = dev["id"]
                    dev_name = dev.get("name", dev_id)
                    _LOGGER.info(
                        "Sequential poll: requesting location for %s (%d/%d)",
                        dev_name,
                        idx + 1,
                        len(devices),
                    )

                    try:
                        # Protect API awaitable with timeout
                        location = await asyncio.wait_for(
                            self.api.async_get_device_location(dev_id, dev_name),
                            timeout=LOCATION_REQUEST_TIMEOUT_S,
                        )

                        if not location:
                            _LOGGER.warning("No location data returned for %s", dev_name)
                            continue

                        # --- Apply Google Home filter (keep parity with FCM push path) ---
                        # Patch: consume coordinate substitution from the filter.
                        semantic_name = location.get("semantic_name")
                        if semantic_name and hasattr(self, "google_home_filter"):
                            try:
                                should_filter, replacement_attrs = self.google_home_filter.should_filter_detection(
                                    dev_id, semantic_name
                                )
                            except Exception as gf_err:
                                _LOGGER.debug(
                                    "Google Home filter error for %s: %s", dev_name, gf_err
                                )
                            else:
                                if should_filter:
                                    _LOGGER.debug(
                                        "Filtering out Google Home spam detection for %s", dev_name
                                    )
                                    continue
                                if replacement_attrs:
                                    _LOGGER.info(
                                        "Google Home filter: %s detected at '%s', substituting with Home coordinates",
                                        dev_name,
                                        semantic_name,
                                    )
                                    location = dict(location)
                                    # Update coordinates and derive accuracy from radius (if present).
                                    if "latitude" in replacement_attrs and "longitude" in replacement_attrs:
                                        location["latitude"] = replacement_attrs.get("latitude")
                                        location["longitude"] = replacement_attrs.get("longitude")
                                    if "radius" in replacement_attrs and replacement_attrs.get("radius") is not None:
                                        location["accuracy"] = replacement_attrs.get("radius")
                                    # Clear semantic name so HA Core's zone engine determines the final state.
                                    location["semantic_name"] = None
                        # ------------------------------------------------------------------

                        # If we only got a semantic location, preserve previous coordinates.
                        if (location.get("latitude") is None or location.get("longitude") is None) and location.get("semantic_name"):
                            prev = self._device_location_data.get(dev_id, {})
                            if prev:
                                location["latitude"] = prev.get("latitude")
                                location["longitude"] = prev.get("longitude")
                                location["accuracy"] = prev.get("accuracy")
                                location["status"] = (
                                    "Semantic location; preserving previous coordinates"
                                )

                        # Validate/normalize coordinates (and accuracy if present).
                        if not self._normalize_coords(location, device_label=dev_name):
                            if not location.get("semantic_name"):
                                _LOGGER.debug(
                                    "No location data (coordinates or semantic name) available for %s in this update.",
                                    dev_name,
                                )
                            # Nothing to commit/update in cache
                            # Strip any internal hint before dropping to avoid accidental exposure
                            location.pop("_report_hint", None)
                            continue

                        # Accuracy quality filter
                        acc = location.get("accuracy")
                        if (
                            isinstance(self._min_accuracy_threshold, int)
                            and self._min_accuracy_threshold > 0
                            and isinstance(acc, (int, float))
                            and acc > self._min_accuracy_threshold
                        ):
                            _LOGGER.debug(
                                "Dropping low-quality fix for %s (accuracy=%sm > %sm)",
                                dev_name,
                                acc,
                                self._min_accuracy_threshold,
                            )
                            self.increment_stat("low_quality_dropped")
                            # Strip any internal hint before dropping to avoid accidental exposure
                            location.pop("_report_hint", None)
                            continue

                        # De-duplicate replaced by significance gate
                        last_seen = location.get("last_seen", 0)
                        if not self._is_significant_update(dev_id, location):
                            _LOGGER.debug(
                                "Skipping non-significant update for %s (last_seen=%s)",
                                dev_name,
                                last_seen,
                            )
                            self.increment_stat("non_significant_dropped")
                            # Strip internal hint before dropping to avoid accidental exposure
                            location.pop("_report_hint", None)
                            continue

                        # Age diagnostics (informational)
                        wall_now = time.time()
                        if last_seen:
                            age_hours = max(0.0, (wall_now - float(last_seen)) / 3600.0)
                            if age_hours > 24:
                                _LOGGER.info(
                                    "Using old location data for %s (age=%.1fh)",
                                    dev_name,
                                    age_hours,
                                )
                            elif age_hours > 1:
                                _LOGGER.debug(
                                    "Using location data for %s (age=%.1fh)",
                                    dev_name,
                                    age_hours,
                                )

                        # Apply type-aware cooldowns based on internal hint (if any).
                        report_hint = location.get("_report_hint")
                        self._apply_report_type_cooldown(dev_id, report_hint)

                        # Ensure we don't leak the internal hint into public snapshots/entities.
                        location.pop("_report_hint", None)

                        # Commit to cache and bump statistics
                        location["last_updated"] = wall_now  # wall-clock for UX
                        self._device_location_data[dev_id] = location
                        self.increment_stat("polled_updates")

                    except asyncio.TimeoutError:
                        _LOGGER.info(
                            "Location request timed out for %s after %s seconds",
                            dev_name,
                            LOCATION_REQUEST_TIMEOUT_S,
                        )
                        self.increment_stat("timeouts")
                    except Exception as err:
                        _LOGGER.error("Failed to get location for %s: %s", dev_name, err)

                    # Inter-device delay (except after the last one)
                    if idx < len(devices) - 1 and self.device_poll_delay > 0:
                        await asyncio.sleep(self.device_poll_delay)

                _LOGGER.debug("Completed polling cycle for %d devices", len(devices))
            finally:
                # Update scheduling baseline and clear flag, then push end snapshot
                self._last_poll_mono = time.monotonic()
                self._is_polling = False
                end_snapshot = self._build_snapshot_from_cache(
                    devices, wall_now=time.time()
                )
                self.async_set_updated_data(end_snapshot)

    # ---------------------------- Snapshot helpers --------------------------
    def _build_base_snapshot_entry(self, device_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Create the base snapshot entry for a device (no cache lookups here).

        This centralizes the common fields to keep snapshot builders DRY.

        Args:
            device_dict: A dictionary containing basic device info (id, name).

        Returns:
            A dictionary with default fields for a device snapshot.
        """
        dev_id = device_dict["id"]
        dev_name = device_dict.get("name", dev_id)
        return {
            "name": dev_name,
            "id": dev_id,
            "device_id": dev_id,
            "latitude": None,
            "longitude": None,
            "altitude": None,
            "accuracy": None,
            "last_seen": None,
            "status": "Waiting for location poll",
            "is_own_report": None,
            "semantic_name": None,
            "battery_level": None,
        }

    def _update_entry_from_cache(self, entry: Dict[str, Any], wall_now: float) -> bool:
        """Update the given snapshot entry in place from the in-memory cache.

        Args:
            entry: The device snapshot entry to update.
            wall_now: The current wall-clock time as a float timestamp.

        Returns:
            True if the cache contained data for this device and the entry was updated, else False.
        """
        dev_id = entry["device_id"]
        cached = self._device_location_data.get(dev_id)
        if not cached:
            return False

        entry.update(cached)
        last_updated_ts = cached.get("last_updated", 0)
        age = max(0.0, wall_now - float(last_updated_ts))
        if age < self.location_poll_interval:
            entry["status"] = "Location data current"
        elif age < self.location_poll_interval * 2:
            entry["status"] = "Location data aging"
        else:
            entry["status"] = "Location data stale"
        return True

    def _build_snapshot_from_cache(
        self, devices: List[Dict[str, Any]], wall_now: float
    ) -> List[Dict[str, Any]]:
        """Build a lightweight snapshot using only the in-memory cache.

        This never touches HA state or the database; it is safe in background tasks.

        Args:
            devices: A list of device dictionaries to include in the snapshot.
            wall_now: The current wall-clock time as a float timestamp.

        Returns:
            A list of device state dictionaries built from the cache.
        """
        snapshot: List[Dict[str, Any]] = []
        for dev in devices:
            entry = self._build_base_snapshot_entry(dev)
            # If cache has info, update status accordingly; otherwise keep default status.
            self._update_entry_from_cache(entry, wall_now)
            snapshot.append(entry)
        return snapshot

    async def _async_build_device_snapshot_with_fallbacks(
        self, devices: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Build a snapshot using cache, HA state and (optionally) history fallback.

        Args:
            devices: A list of device dictionaries to build the snapshot for.

        Returns:
            A complete list of device state dictionaries with fallbacks applied.
        """
        snapshot: List[Dict[str, Any]] = []
        wall_now = time.time()
        ent_reg = er.async_get(self.hass)

        for dev in devices:
            entry = self._build_base_snapshot_entry(dev)

            # Prefer cached result
            if self._update_entry_from_cache(entry, wall_now):
                snapshot.append(entry)
                continue

            # No cache -> Registry + State (cheap, non-blocking)
            dev_id = entry["device_id"]
            unique_id = f"{DOMAIN}_{dev_id}"
            entity_id = ent_reg.async_get_entity_id("device_tracker", DOMAIN, unique_id)
            if not entity_id:
                _LOGGER.debug(
                    "No entity registry entry for device '%s' (unique_id=%s); skipping any fallback.",
                    entry["name"],
                    unique_id,
                )
                snapshot.append(entry)
                continue

            state = self.hass.states.get(entity_id)
            if state:
                lat = state.attributes.get("latitude")
                lon = state.attributes.get("longitude")
                acc = state.attributes.get("gps_accuracy")
                if lat is not None and lon is not None:
                    entry.update(
                        {
                            "latitude": lat,
                            "longitude": lon,
                            "accuracy": acc,
                            "last_seen": int(state.last_updated.timestamp()),
                            "status": "Using current state",
                        }
                    )
                    snapshot.append(entry)
                    continue

            # Optional history fallback
            if self.allow_history_fallback:
                _LOGGER.warning(
                    "No live state for %s (entity_id=%s); attempting history fallback via Recorder.",
                    entry["name"],
                    entity_id,
                )
                rec = get_recorder(self.hass)
                result = await rec.async_add_executor_job(
                    _sync_get_last_gps_from_history, self.hass, entity_id
                )
                if result:
                    entry.update(result)
                    self.increment_stat("history_fallback_used")
                else:
                    _LOGGER.warning(
                        "No historical GPS data found for %s (entity_id=%s). "
                        "Entity may be excluded from Recorder.",
                        entry["name"],
                        entity_id,
                    )

            snapshot.append(entry)

        return snapshot

    # ---------------------------- Stats persistence -------------------------
    async def _async_load_stats(self) -> None:
        """Load statistics from entry-scoped cache."""
        try:
            cached = await self._cache.async_get_cached_value("integration_stats")
            if cached and isinstance(cached, dict):
                for key in self.stats.keys():
                    if key in cached:
                        self.stats[key] = cached[key]
                _LOGGER.debug("Loaded statistics from cache: %s", self.stats)
        except Exception as err:
            _LOGGER.debug("Failed to load statistics from cache: %s", err)

    async def _async_save_stats(self) -> None:
        """Persist statistics to entry-scoped cache."""
        try:
            await self._cache.async_set_cached_value("integration_stats", self.stats.copy())
        except Exception as err:
            _LOGGER.debug("Failed to save statistics to cache: %s", err)

    async def _debounced_save_stats(self) -> None:
        """Debounce wrapper to coalesce frequent stat updates into a single write.

        This coroutine MUST run on the HA event loop. It is scheduled safely via
        `_schedule_stats_persist()` which ensures loop-thread execution.
        """
        try:
            await asyncio.sleep(self._stats_debounce_seconds)
            await self._async_save_stats()
        except asyncio.CancelledError:
            # Expected if a new increment arrives before the delay elapses; do nothing.
            return
        except Exception as err:
            _LOGGER.debug("Debounced stats save failed: %s", err)

    def _schedule_stats_persist(self) -> None:
        """(Re)schedule a debounced persistence task for statistics.

        Thread-safe: may be called from any thread. Ensures cancellation and creation
        of the debounced task happen on the HA loop.
        """
        def _do_schedule() -> None:
            # Cancel a pending writer, if any, and schedule a fresh one (loop-local).
            if self._stats_save_task and not self._stats_save_task.done():
                self._stats_save_task.cancel()
            self._stats_save_task = self.hass.loop.create_task(
                self._debounced_save_stats(), name=f"{DOMAIN}.save_stats_debounced"
            )

        if self._is_on_hass_loop():
            _do_schedule()
        else:
            self._run_on_hass_loop(_do_schedule)

    def _increment_stat_on_loop(self, stat_name: str) -> None:
        """Increment a statistic on the HA loop and schedule persistence."""
        if stat_name in self.stats:
            before = self.stats[stat_name]
            self.stats[stat_name] = before + 1
            _LOGGER.debug(
                "Incremented %s from %s to %s", stat_name, before, self.stats[stat_name]
            )
            self._schedule_stats_persist()
        else:
            _LOGGER.warning(
                "Tried to increment unknown stat '%s'; available=%s",
                stat_name,
                list(self.stats.keys() ),
            )

    def increment_stat(self, stat_name: str) -> None:
        """Increment a statistic counter (thread-safe).

        May be called from any thread. The actual mutation and scheduling are
        marshalled onto the HA event loop.

        Note on performance:
        - The "hop" to the loop occurs exactly once here (constant per call).
          We avoid repeated hops for inner micro-operations.
        """
        if self._is_on_hass_loop():
            self._increment_stat_on_loop(stat_name)
        else:
            self._run_on_hass_loop(self._increment_stat_on_loop, stat_name)

    # ---------------------------- Public platform API -----------------------
    def get_device_location_data(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Return current cached location dict for a device (or None).

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A dictionary of location data or None if not found.
        """
        return self._device_location_data.get(device_id)

    def prime_device_location_cache(self, device_id: str, data: Dict[str, Any]) -> None:
        """Seed/update the location cache for a device with (lat/lon/accuracy).

        Args:
            device_id: The canonical ID of the device.
            data: A dictionary containing latitude, longitude, and accuracy.
        """
        slot = self._device_location_data.get(device_id, {})
        slot.update(
            {k: v for k, v in data.items() if k in ("latitude", "longitude", "accuracy")}
        )
        # Do not set last_updated/last_seen here; this is only priming.
        self._device_location_data[device_id] = slot

    def seed_device_last_seen(self, device_id: str, ts_epoch: float) -> None:
        """Seed last_seen (epoch seconds) without overriding fresh data.

        Args:
            device_id: The canonical ID of the device.
            ts_epoch: The timestamp in epoch seconds.
        """
        slot = self._device_location_data.setdefault(device_id, {})
        slot.setdefault("last_seen", float(ts_epoch))

    def update_device_cache(self, device_id: str, location_data: Dict[str, Any]) -> None:
        """Public, encapsulated update of the internal location cache for one device (thread-safe).

        Used by the FCM receiver (push path) and by internal manual-commit call sites.
        Expects validated fields (decrypt layer performs fail-fast checks).

        Internal rules:
        - Applies type-aware **poll** cooldowns based on an internal `_report_hint` (if present).
        - Strips `_report_hint` from the cached payload to avoid exposing internal fields.
        - Runs significance gating to prevent redundant cache churn. The cooldown still applies
          even if the update is dropped as non-significant (server-friendly behaviour).

        Args:
            device_id: The canonical ID of the device.
            location_data: The new location data dictionary.
        """
        if not self._is_on_hass_loop():
            # Marshal entire update onto the HA loop to avoid cross-thread mutations.
            self._run_on_hass_loop(self.update_device_cache, device_id, location_data)
            return

        if not isinstance(location_data, dict):
            _LOGGER.debug("Ignored cache update for %s: payload is not a dict", device_id)
            return

        # Shallow copy to avoid caller-side mutation
        slot = dict(location_data)

        # Apply type-aware **poll** cooldowns (if decrypt layer provided a hint),
        # then drop the hint to keep internal-only.
        self._apply_report_type_cooldown(device_id, slot.get("_report_hint"))
        slot.pop("_report_hint", None)

        # Normalize coordinates (best-effort) if present; do not spam warnings here.
        # The push path may deliver semantic-only updates (no coordinates), which is valid.
        self._normalize_coords(slot, device_label=device_id, warn_on_invalid=False)

        # Significance gate (prevents redundant churn while still respecting cooldowns)
        if not self._is_significant_update(device_id, slot):
            self.increment_stat("non_significant_dropped")
            return

        # Ensure last_updated is present
        slot.setdefault("last_updated", time.time())

        # Keep human-friendly name mapping up-to-date if provided alongside
        name = slot.get("name")
        if isinstance(name, str) and name:
            self._device_names[device_id] = name

        self._device_location_data[device_id] = slot
        # Increment background updates to account for push/manual commits.
        self.increment_stat("background_updates")

    # ---------------------------- Significance / gating ----------------------
    def _haversine_distance(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Return distance in meters between two WGS84 coordinates.

        Implementation note:
            Kept lightweight and allocation-free; called per candidate update only.
        """
        from math import radians, sin, cos, sqrt, atan2

        R = 6371000.0  # Earth radius in meters
        lat1_r, lon1_r = radians(float(lat1)), radians(float(lon1))
        lat2_r, lon2_r = radians(float(lat2)), radians(float(lon2))
        dlat = lat2_r - lat1_r
        dlon = lon2_r - lon1_r
        a = sin(dlat / 2.0) ** 2 + cos(lat1_r) * cos(lat2_r) * sin(dlon / 2.0) ** 2
        c = 2.0 * atan2(sqrt(a), sqrt(1.0 - a))
        return R * c

    def _is_significant_update(self, device_id: str, new_data: Dict[str, Any]) -> bool:
        """Return True if the update carries meaningful new information.

        Criteria (coarse → fine):
          1) No previous data -> significant.
          2) Newer `last_seen` -> significant.
          3) Same `last_seen` but:
             3a) Position changed more than `self._movement_threshold` meters, or
             3b) Accuracy improved by ≥20% (smaller is better), or
             3c) Source/status changed (e.g., own → crowdsourced or semantic changes).

        Notes:
          * This replaces the old "same last_seen == duplicate" heuristic.
          * Movement threshold is user-configurable (options) and defaults to 50 m.
        """
        existing = self._device_location_data.get(device_id)
        if not existing:
            return True

        n_seen = new_data.get("last_seen")
        e_seen = existing.get("last_seen")
        try:
            if n_seen is not None and e_seen is not None and float(n_seen) > float(e_seen):
                return True
        except Exception:
            # If timestamps are non-numeric, fall through to other checks.
            pass

        # Same timestamp? Check for spatial delta and accuracy improvement.
        if n_seen == e_seen:
            n_lat, n_lon = new_data.get("latitude"), new_data.get("longitude")
            e_lat, e_lon = existing.get("latitude"), existing.get("longitude")
            if all(isinstance(v, (int, float)) for v in (n_lat, n_lon, e_lat, e_lon)):
                try:
                    dist = self._haversine_distance(e_lat, e_lon, n_lat, n_lon)
                    if dist > float(self._movement_threshold):
                        return True
                except Exception:
                    # Ignore distance errors and continue checks.
                    pass

            n_acc = new_data.get("accuracy")
            e_acc = existing.get("accuracy")
            if isinstance(n_acc, (int, float)) and isinstance(e_acc, (int, float)):
                try:
                    if float(n_acc) < float(e_acc) * 0.8:  # ≥20% better accuracy
                        return True
                except Exception:
                    pass

        # Source or semantic change can still be valuable.
        if new_data.get("is_own_report") != existing.get("is_own_report"):
            return True
        if new_data.get("semantic_name") != existing.get("semantic_name"):
            return True

        return False

    def get_device_last_seen(self, device_id: str) -> Optional[datetime]:
        """Return last_seen as timezone-aware datetime (UTC) if cached.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A timezone-aware datetime object or None.
        """
        ts = self._device_location_data.get(device_id, {}).get("last_seen")
        if ts is None:
            return None
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except Exception:
            return None

    def get_device_display_name(self, device_id: str) -> Optional[str]:
        """Return the human-readable device name if known.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            The display name as a string, or None.
        """
        return self._device_names.get(device_id)

    def get_device_name_map(self) -> Dict[str, str]:
        """Return a shallow copy of the internal device-id -> name mapping.

        Returns:
            A dictionary mapping device IDs to their names.
        """
        return dict(self._device_names)

    # ---------------------------- Presence & Purge API ----------------------------
    def is_device_present(self, device_id: str) -> bool:
        """Return True if the given device_id is present in the latest device list.

        Presence is derived from the most recent lightweight list returned by the API.
        """
        return device_id in self._present_device_ids

    def get_absent_device_ids(self) -> List[str]:
        """Return ids known by name/cache that are NOT present in the latest device list.

        Useful for diagnostics. This does not imply automatic removal.
        """
        known = set(self._device_names) | set(self._device_location_data)
        return sorted(list(known - set(self._present_device_ids)))

    def purge_device(self, device_id: str) -> None:
        """Remove all cached data and cooldown state for a device (thread-safe publish).

        Called from the config-entry device deletion flow. This does not trigger a poll,
        but it immediately publishes an updated snapshot so UI can refresh.
        """
        if not self._is_on_hass_loop():
            self._run_on_hass_loop(self.purge_device, device_id)
            return

        self._device_location_data.pop(device_id, None)
        self._device_names.pop(device_id, None)
        self._device_caps.pop(device_id, None)
        self._locate_inflight.discard(device_id)
        self._locate_cooldown_until.pop(device_id, None)
        self._device_poll_cooldown_until.pop(device_id, None)
        self._present_device_ids.discard(device_id)
        # Push a minimal update so listeners can refresh availability quickly
        self.async_set_updated_data(self.data)

    # ---------------------------- Push updates ------------------------------
    def push_updated(self, device_ids: Optional[List[str]] = None) -> None:
        """Publish a fresh snapshot to listeners after push (FCM) cache updates.

        Thread-safe: may be called from any thread. This method ensures all state
        publishing happens on the HA event loop.

        This **does not** trigger a poll. It:
        - Immediately pushes cache state to entities via `async_set_updated_data()`.
        - Resets the internal poll baseline to 'now' to prevent an immediate re-poll.
        - Optionally limits the snapshot to `device_ids`; otherwise includes all known devices.

        Args:
            device_ids: An optional list of device IDs to include in the update.
        """
        if not self._is_on_hass_loop():
            self._run_on_hass_loop(self.push_updated, device_ids)
            return

        wall_now = time.time()
        self._last_poll_mono = time.monotonic()  # reset poll timer

        # Choose device ids for the snapshot
        if device_ids:
            ids = device_ids
        else:
            # union of all known names and cached locations
            ids = list({*self._device_names.keys(), *self._device_location_data.keys()})

        # Apply ignore filter to prevent resurfacing ignored devices via push path.
        ignored = self._get_ignored_set()
        ids = [d for d in ids if d not in ignored]

        # Build "devices" stubs from id->name mapping
        devices_stub: List[Dict[str, Any]] = [
            {"id": dev_id, "name": self._device_names.get(dev_id, dev_id)} for dev_id in ids
        ]

        snapshot = self._build_snapshot_from_cache(devices_stub, wall_now=wall_now)
        self.async_set_updated_data(snapshot)
        _LOGGER.debug("Pushed snapshot for %d device(s) via push_updated()", len(snapshot))

    # ---------------------------- Play sound helpers ------------------------
    def _api_push_ready(self) -> bool:
        """Best-effort check whether push/FCM is initialized (backward compatible).

        Optimistic default: if we cannot determine readiness explicitly,
        return True so the UI stays usable; the API call will enforce reality.

        Returns:
            True if the push mechanism is believed to be ready.
        """
        # Short-circuit via cooldown window after a transport failure.
        now = time.monotonic()
        if now < self._push_cooldown_until:
            if self._push_ready_memo is not False:
                _LOGGER.debug("Push readiness: cooldown active -> treating as not ready")
            self._push_ready_memo = False
            return False

        ready: Optional[bool] = None
        try:
            fn = getattr(self.api, "is_push_ready", None)
            if callable(fn):
                ready = bool(fn())
            else:
                for attr in ("push_ready", "fcm_ready", "receiver_ready"):
                    val = getattr(self.api, attr, None)
                    if isinstance(val, bool):
                        ready = val
                        break
                if ready is None:
                    fcm = getattr(self.api, "fcm", None)
                    if fcm is not None:
                        for attr in ("is_ready", "ready"):
                            val = getattr(fcm, attr, None)
                            if isinstance(val, bool):
                                ready = val
                                break
        except Exception as err:
            _LOGGER.debug(
                "Push readiness check exception: %s (defaulting optimistic True)", err
            )
            ready = True

        if ready is None:
            ready = True  # optimistic default

        if ready != self._push_ready_memo:
            _LOGGER.debug("Push readiness changed: %s", ready)
            self._push_ready_memo = ready

        return ready

    def _note_push_transport_problem(self, cooldown_s: int = 90) -> None:
        """Enter a temporary cooldown after a push transport failure to avoid spamming.

        Args:
            cooldown_s: The duration of the cooldown in seconds.
        """
        self._push_cooldown_until = time.monotonic() + cooldown_s
        self._push_ready_memo = False
        _LOGGER.debug("Entering push cooldown for %ss after transport failure", cooldown_s)

    def can_play_sound(self, device_id: str) -> bool:
        """Return True if 'Play Sound' should be enabled for the device.

        **No network in availability path.**
        Strategy:
        - If capability is known from the lightweight device list -> use it (fast, cached).
        - If push readiness is explicitly False -> disable.
        - Otherwise -> optimistic True (known devices) to keep the UI usable.
          The actual action enforces reality and will start a cooldown on failure.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if playing a sound is likely possible.
        """
        # 1) Use cached capability when available (fast path, no network).
        caps = self._device_caps.get(device_id)
        if caps and isinstance(caps.get("can_ring"), bool):
            res = bool(caps["can_ring"])
            _LOGGER.debug("can_play_sound(%s) -> %s (from capability can_ring)", device_id, res)
            return res

        # 2) Short-circuit if push transport is not ready.
        ready = self._api_push_ready()
        if ready is False:
            _LOGGER.debug("can_play_sound(%s) -> False (push not ready)", device_id)
            return False

        # 3) Optimistic final decision based on whether we know the device.
        is_known = (
            device_id in self._device_names or device_id in self._device_location_data
        )
        if is_known:
            _LOGGER.debug(
                "can_play_sound(%s) -> True (optimistic; known device, push_ready=%s)",
                device_id,
                ready,
            )
            return True

        _LOGGER.debug("can_play_sound(%s) -> True (optimistic final fallback)", device_id)
        return True

    # ---------------------------- Public control / Locate gating ------------
    def can_request_location(self, device_id: str) -> bool:
        """Return True if a manual 'Locate now' request is currently allowed.

        Gate conditions:
          - push transport ready,
          - no sequential polling in progress,
          - no in-flight locate for the device,
          - per-device cooldown (lower-bounded by DEFAULT_MIN_POLL_INTERVAL) not active.
        """
        # Block manual locate for ignored devices.
        if self.is_ignored(device_id):
            return False
        if not self._api_push_ready():
            return False
        if self._is_polling:
            return False
        if device_id in self._locate_inflight:
            return False
        # Respect both manual-locate and poll cooldowns for the device
        now_mono = time.monotonic()
        until_manual = self._locate_cooldown_until.get(device_id, 0.0)
        if until_manual and now_mono < until_manual:
            return False
        until_poll = self._device_poll_cooldown_until.get(device_id, 0.0)
        if until_poll and now_mono < until_poll:
            return False
        return True

    def update_settings(
        self,
        *,
        ignored_devices: Optional[List[str]] = None,
        location_poll_interval: Optional[int] = None,
        device_poll_delay: Optional[int] = None,
        min_poll_interval: Optional[int] = None,
        min_accuracy_threshold: Optional[int] = None,
        movement_threshold: Optional[int] = None,
        allow_history_fallback: Optional[bool] = None,
    ) -> None:
        """Apply updated user settings provided by the config entry (options-first).

        This method deliberately enforces basic typing/limits to keep the coordinator sane
        regardless of where the values came from.

        Args:
            ignored_devices: A list of device IDs to hide from snapshots/polling.
            location_poll_interval: The interval in seconds for location polling.
            device_poll_delay: The delay in seconds between polling devices.
            min_poll_interval: The minimum polling interval in seconds.
            min_accuracy_threshold: The minimum accuracy in meters.
            movement_threshold: The spatial delta (meters) required to treat updates as significant.
            allow_history_fallback: Whether to allow falling back to Recorder history.
        """
        if ignored_devices is not None:
            # This attribute is only used as a fallback when config_entry is not available.
            self.ignored_devices = list(ignored_devices)

        if location_poll_interval is not None:
            try:
                self.location_poll_interval = max(1, int(location_poll_interval))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid location_poll_interval=%r", location_poll_interval
                )

        if device_poll_delay is not None:
            try:
                self.device_poll_delay = max(0, int(device_poll_delay))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid device_poll_delay=%r", device_poll_delay)

        if min_poll_interval is not None:
            try:
                self.min_poll_interval = max(1, int(min_poll_interval))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid min_poll_interval=%r", min_poll_interval)

        if min_accuracy_threshold is not None:
            try:
                self._min_accuracy_threshold = max(0, int(min_accuracy_threshold))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid min_accuracy_threshold=%r", min_accuracy_threshold
                )

        if movement_threshold is not None:
            try:
                self._movement_threshold = max(0, int(movement_threshold))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid movement_threshold=%r", movement_threshold)

        if allow_history_fallback is not None:
            self.allow_history_fallback = bool(allow_history_fallback)

    def force_poll_due(self) -> None:
        """Force the next poll to be due immediately (no private access required externally)."""
        effective_interval = max(self.location_poll_interval, self.min_poll_interval)
        # Move the baseline back so that (now - _last_poll_mono) >= effective_interval
        self._last_poll_mono = time.monotonic() - float(effective_interval)

    # ---------------------------- Passthrough API ---------------------------
    async def async_locate_device(self, device_id: str) -> Dict[str, Any]:
        """Locate a device using the native async API (no executor).

        UX & gating:
          - Reject immediately if `can_request_location()` is False.
          - Mark request as in-flight and (optimistically) start a cooldown that
            equals `DEFAULT_MIN_POLL_INTERVAL`. This disables repeated clicks.
          - On success: reset the polling baseline and set a **per-device cooldown**
            (owner-report purge window) by clamping a dynamic guess.
          - Always notify listeners via `async_set_updated_data(self.data)`.

        POPETS'25-informed behaviour:
          - If the returned payload carries an internal `_report_hint` of
            "in_all_areas" (~10 min throttle) or "high_traffic" (~5 min throttle),
            we additionally apply a type-aware cooldown (at least server minimum
            and at least one user poll interval). This stacks with the owner cooldown.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A dictionary containing the location data (empty dict on gating).

        Corrections:
            - Persist the received location data into the coordinator cache.
            - Mirror the Google Home spam filter used by the polling path.
            - Preserve previous coordinates for semantic-only locations.
            - Validate coordinates/accuracy and de-duplicate via `last_seen`.
            - Push a fresh snapshot via `push_updated([device_id])`.
        """
        name = self.get_device_display_name(device_id) or device_id

        if not self.can_request_location(device_id):
            _LOGGER.warning(
                "Manual locate for %s is currently disabled (in-flight, cooldown, push not ready, or polling).",
                name,
            )
            return {}

        # Enter in-flight and set a lower-bound cooldown window
        self._locate_inflight.add(device_id)
        self._locate_cooldown_until[device_id] = time.monotonic() + float(DEFAULT_MIN_POLL_INTERVAL)
        self.async_set_updated_data(self.data)

        try:
            location_data = await self.api.async_get_device_location(device_id, name)
            if not location_data:
                return {}

            # --- Parity with polling path: Google Home semantic spam filter --------
            # Patch: consume coordinate substitution from the filter.
            semantic_name = location_data.get("semantic_name")
            if semantic_name and hasattr(self, "google_home_filter"):
                try:
                    should_filter, replacement_attrs = self.google_home_filter.should_filter_detection(
                        device_id, semantic_name
                    )
                except Exception as gf_err:
                    _LOGGER.debug("Google Home filter error for %s: %s", name, gf_err)
                else:
                    if should_filter:
                        _LOGGER.debug("Filtering out Google Home spam detection for %s (manual locate)", name)
                        # Successful but filtered: reset baseline, clear cooldown, and refresh UI.
                        self._last_poll_mono = time.monotonic()
                        self._locate_cooldown_until.pop(device_id, None)
                        self.push_updated([device_id])
                        return {}
                    if replacement_attrs:
                        location_data = dict(location_data)
                        if "latitude" in replacement_attrs and "longitude" in replacement_attrs:
                            location_data["latitude"] = replacement_attrs.get("latitude")
                            location_data["longitude"] = replacement_attrs.get("longitude")
                        if "radius" in replacement_attrs and replacement_attrs.get("radius") is not None:
                            location_data["accuracy"] = replacement_attrs.get("radius")
                        # Clear semantic name so HA Core's zone engine determines the final state.
                        location_data["semantic_name"] = None
            # ----------------------------------------------------------------------

            # Preserve previous coordinates if only semantic location is provided.
            if (location_data.get("latitude") is None or location_data.get("longitude") is None) and location_data.get("semantic_name"):
                prev = self._device_location_data.get(device_id, {})
                if prev:
                    location_data.setdefault("latitude", prev.get("latitude"))
                    location_data.setdefault("longitude", prev.get("longitude"))
                    location_data.setdefault("accuracy", prev.get("accuracy"))
                    location_data["status"] = "Semantic location; preserving previous coordinates"

            # Validate/normalize coordinates (and accuracy if present).
            if not self._normalize_coords(location_data, device_label=name):
                if not location_data.get("semantic_name"):
                    _LOGGER.debug(
                        "No location data (coordinates or semantic name) available for %s in manual locate.",
                        name,
                    )
                return {}

            acc = location_data.get("accuracy")

            # Accuracy quality filter
            if (
                isinstance(self._min_accuracy_threshold, int)
                and self._min_accuracy_threshold > 0
                and isinstance(acc, (int, float))
                and float(acc) > float(self._min_accuracy_threshold)
            ):
                _LOGGER.debug(
                    "Dropping low-quality fix for %s (accuracy=%sm > %sm)", name, acc, self._min_accuracy_threshold
                )
                self.increment_stat("low_quality_dropped")
                return {}

            # De-duplicate replaced by significance gate
            existing_data = self._device_location_data.get(device_id, {})
            # Prepare a copy for gating/cooldown application
            slot = dict(location_data)
            slot.setdefault("last_updated", time.time())

            # Apply type-aware cooldowns based on internal hint (if any), then strip it.
            self._apply_report_type_cooldown(device_id, slot.get("_report_hint"))
            slot.pop("_report_hint", None)

            # Significance gate also for manual locate to avoid churn.
            if not self._is_significant_update(device_id, slot):
                self.increment_stat("non_significant_dropped")
                return {}

            # Commit to cache. `update_device_cache` ensures `last_updated` and stats.
            self.update_device_cache(device_id, slot)

            # Successful manual locate:
            # - reset poll baseline,
            # - set a per-device poll cooldown (owner purge window) using a dynamic guess
            #   clamped into guardrails,
            # - set the same cooldown for manual locate button to avoid spamming.
            self._last_poll_mono = time.monotonic()
            dynamic_guess = max(float(DEFAULT_MIN_POLL_INTERVAL), float(self.location_poll_interval))
            owner_cooldown = _clamp(dynamic_guess, _COOLDOWN_OWNER_MIN_S, _COOLDOWN_OWNER_MAX_S)
            now_mono = time.monotonic()
            # Extend (not overwrite) any type-aware cooldown applied above
            existing_deadline = self._device_poll_cooldown_until.get(device_id, 0.0)
            owner_deadline = now_mono + owner_cooldown
            self._device_poll_cooldown_until[device_id] = max(existing_deadline, owner_deadline)
            self._locate_cooldown_until[device_id] = max(
                self._locate_cooldown_until.get(device_id, 0.0), owner_deadline
            )

            self.push_updated([device_id])
            return location_data or {}
        except Exception as err:
            _LOGGER.error("Manual locate for %s failed: %s", name, err)
            raise
        finally:
            self._locate_inflight.discard(device_id)
            # Push an update so buttons/entities can refresh availability
            self.async_set_updated_data(self.data)

    async def async_play_sound(self, device_id: str) -> bool:
        """Play sound on a device using the native async API (no executor).

        Guard with can_play_sound(); on failure, start a short cooldown to avoid repeated errors.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the command was submitted successfully, False otherwise.
        """
        if not self.can_play_sound(device_id):
            _LOGGER.debug(
                "Suppressing play_sound call for %s: capability/push not ready",
                device_id,
            )
            return False
        try:
            ok = await self.api.async_play_sound(device_id)
            if not ok:
                self._note_push_transport_problem()
            return bool(ok)
        except Exception as err:
            _LOGGER.debug(
                "async_play_sound raised for %s: %s; entering cooldown", device_id, err
            )
            self._note_push_transport_problem()
            return False

    async def async_stop_sound(self, device_id: str) -> bool:
        """Stop sound on a device using the native async API (no executor).

        Args:
            device_id: The canonical ID of the device.

        Returns:
            True if the command was submitted successfully, False otherwise.
        """
        # Less strict than can_play_sound(): stopping is harmless but still requires push readiness.
        if not self._api_push_ready():
            _LOGGER.debug(
                "Suppressing stop_sound call for %s: push not ready",
                device_id,
            )
            return False
        try:
            ok = await self.api.async_stop_sound(device_id)
            if not ok:
                self._note_push_transport_problem()
            return bool(ok)
        except Exception as err:
            _LOGGER.debug(
                "async_stop_sound raised for %s: %s; entering cooldown", device_id, err
            )
            self._note_push_transport_problem()
            return False
