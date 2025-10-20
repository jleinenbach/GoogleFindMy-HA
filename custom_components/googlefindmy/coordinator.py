# custom_components/googlefindmy/coordinator.py
"""Data coordinator for Google Find My Device (async-first, HA-friendly).

Discovery vs. polling semantics:
- Every coordinator tick fetches the lightweight **full** Google device list.
- Presence and name/capability caches are updated for all devices.
- The published snapshot (`self.data`) contains **all** devices (for dynamic entity creation).
- The sequential **polling cycle** polls **only devices that are enabled** in Home Assistant's
  Device Registry (devices with `disabled_by is None`) for **this** config entry. Devices explicitly
  ignored via options are filtered out as well. Devices without a Device Registry entry yet are
  included to allow initial discovery.

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

Authentication handling and HA best practices (Platinum standard):
- The **DataUpdateCoordinator** is the central place to detect auth failures.
- When the API raises `ConfigEntryAuthFailed`, we:
  1) Create (idempotently) a **Repairs issue** using Home Assistant's issue registry
     so the integration is marked with **"Reconfigure"** in the UI (system repairs).
  2) Fire a domain-scoped **event** so users can automate on top of the condition.
  3) Set an internal **flag** (`auth_error_active`) that the diagnostic binary_sensor
     can expose as `on` (see step 5.1-C, binary_sensor.py).
  4) Re-raise `ConfigEntryAuthFailed` from the coordinator so HA triggers the
     **Re-auth flow** defined in `config_flow.py` (platinum-standard behavior).
- As soon as any subsequent API call succeeds, we:
  1) Dismiss the Repairs issue,
  2) Fire a matching **OK event**,
  3) Clear the internal flag and push an update so the sensor flips back to `off`.

This module must not log secrets and must keep user-facing strings out of code; use translations instead.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Set, Tuple

from homeassistant.components.recorder import (
    get_instance as get_recorder,
    history as recorder_history,
)
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import (
    EVENT_DEVICE_REGISTRY_UPDATED,
)
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers import issue_registry as ir  # Repairs: modern "needs action" UI

from .api import GoogleFindMyAPI
from .const import (
    # Core / options
    DEFAULT_MIN_POLL_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    LOCATION_REQUEST_TIMEOUT_S,
    OPT_IGNORED_DEVICES,
    UPDATE_INTERVAL,
    INTEGRATION_VERSION,
    # Integration "service device" metadata
    SERVICE_DEVICE_NAME,
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_MANUFACTURER,
    service_device_identifier,
    # Helpers
    coerce_ignored_mapping,
    # Credential meta for Repairs placeholders
    CONF_GOOGLE_EMAIL,
    # Required symbols provided by const.py (5.1-A)
    EVENT_AUTH_ERROR,
    EVENT_AUTH_OK,
    ISSUE_AUTH_EXPIRED_KEY,
    issue_id_for,
)

# IMPORTANT: make Common_pb2 import **mandatory** (integration packaging must include it).
# This avoids silent type/name drift and keeps source labels stable.
from custom_components.googlefindmy.ProtoDecoders import Common_pb2  # type: ignore

_LOGGER = logging.getLogger(__name__)


# --- Lightweight cache protocol for entry-scoped persistence -----------------
class CacheProtocol(Protocol):
    """Minimal cache protocol used by the coordinator.

    Implementations are provided by the integration's token/cache layer.
    """

    async def async_get_cached_value(self, key: str) -> Any:
        """Return a cached value for a given key (or None)."""
        ...

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        """Persist a value under a given key (overwriting the previous one)."""
        ...


# --- Module constants (cooldowns & quorum) ---------------------------------
# Accept an empty device list only on the 2nd consecutive result (defers once)
_EMPTY_LIST_QUORUM = 2

# POPETS'25-informed throttling windows for crowdsourced reports
_COOLDOWN_MIN_IN_ALL_AREAS_S = 10 * 60  # 10 minutes
_COOLDOWN_MIN_HIGH_TRAFFIC_S = 5 * 60   # 5 minutes

# Guardrails for owner-driven locate cooldown
_COOLDOWN_OWNER_MIN_S = 60              # at least 1 minute
_COOLDOWN_OWNER_MAX_S = 15 * 60         # at most 15 minutes


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp value between lo and hi (inclusive)."""
    try:
        v = float(value)
        return max(float(lo), min(float(hi), v))
    except (TypeError, ValueError):
        return float(lo)


# --- BEGIN: Diagnostics buffer & helpers (top-level) ---------------------------
@dataclass
class DiagnosticsBuffer:
    """Bounded, deduplicated in-memory buffer for warnings and errors.

    This buffer is used to expose runtime findings via diagnostics.py.
    Sensitive data MUST NOT be stored here (no tokens, no coordinates).
    The buffer is intentionally small and deduplicated to avoid large dumps.
    """

    max_items: int = 200
    warnings: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    errors: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def _add(
        self, bucket: Dict[str, Dict[str, Any]], key: str, payload: Dict[str, Any]
    ) -> None:
        """Add payload once (dedup by key); bounded to max_items."""
        if key in bucket:
            return
        if len(bucket) >= self.max_items:
            return
        bucket[key] = payload

    def add_warning(self, code: str, context: Dict[str, Any]) -> None:
        """Record a warning with a semantic code and redacted context."""
        key = f"{code}:{context.get('device_id','?')}"
        self._add(self.warnings, key, context)

    def add_error(self, code: str, context: Dict[str, Any]) -> None:
        """Record an error with a semantic code and redacted context."""
        key = f"{code}:{context.get('device_id','?')}:{context.get('arg','')}"
        self._add(self.errors, key, context)

    def to_dict(self) -> Dict[str, Any]:
        """Return a minimal, redacted, diagnostics-friendly dict."""
        return {
            "summary": {"warnings": len(self.warnings), "errors": len(self.errors)},
            "warnings": list(self.warnings.values()),
            "errors": list(self.errors.values()),
        }


# --- Epoch normalization (ms→s tolerant) -----------------------------------
def _normalize_epoch_seconds(ts: Any) -> Optional[float]:
    """Return epoch seconds as float; accept str/int/float; convert ms→s if needed."""
    try:
        f = float(ts)
        if not math.isfinite(f):
            return None
        # Heuristic for milliseconds: 1_000_000_000_000 is ~Sep 2001 in ms
        if f > 1_000_000_000_000:
            f = f / 1000.0
        return f
    except (TypeError, ValueError):
        return None


# --- Decoder-row Normalization & Attribute Helpers -------------------------
_MISSING = object()


def _row_source_label(row: Dict[str, Any]) -> Tuple[int, str]:
    """
    Determine (rank, label) for the source of a report.
    Rank: 3=owner, 2=crowdsourced, 1=aggregated, 0=semantic/unknown
    Label: 'owner' | 'crowdsourced' | 'aggregated' | 'semantic/unknown'
    """
    is_own = bool(row.get("is_own_report"))
    status_code = row.get("status_code")
    try:
        status_code_int = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status_code_int = None

    raw_status = row.get("status")
    if isinstance(raw_status, str):
        status_name = raw_status.strip().lower()
    elif isinstance(raw_status, (int, float)) and Common_pb2:
        try:
            status_name = Common_pb2.Status.Name(int(raw_status)).lower()
        except Exception:
            status_name = str(int(raw_status))
    else:
        status_name = ""

    hint = str(row.get("_report_hint") or "").strip().lower()
    cs = getattr(Common_pb2, "CROWDSOURCED", _MISSING)
    ag = getattr(Common_pb2, "AGGREGATED", _MISSING)

    if is_own:
        return 3, "owner"
    if (
        (cs is not _MISSING and status_code_int == cs)
        or "crowdsourced" in status_name
        or "in_all_areas" in status_name
        or hint == "in_all_areas"
    ):
        return 2, "crowdsourced"
    if (
        (ag is not _MISSING and status_code_int == ag)
        or "aggregated" in status_name
        or "high_traffic" in status_name
        or hint == "high_traffic"
    ):
        return 1, "aggregated"
    return 0, "semantic/unknown"


def _sanitize_decoder_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """Enforce protocol invariants and prepare HA attributes.

    Notes:
        - Ensures timestamps are normalized and provides an ISO UTC mirror.
        - Derives a stable `source_label`/`source_rank` used for stats and UX.
        - Zero-accuracy semantic entries are coerced to None to avoid noise.
    """
    r = dict(row)
    rank, label = _row_source_label(r)

    if label == "semantic/unknown" and r.get("is_own_report"):
        r["is_own_report"] = False
    if label == "semantic/unknown" and r.get("accuracy") in (0, 0.0):
        r["accuracy"] = None

    ts = _normalize_epoch_seconds(r.get("last_seen"))
    r["last_seen"] = ts  # Store normalized float
    if ts:
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            r["last_seen_utc"] = dt.isoformat().replace("+00:00", "Z")
        except Exception:
            r["last_seen_utc"] = None
    else:
        r["last_seen_utc"] = None

    r["source_label"] = label
    r["source_rank"] = rank
    return r


def _as_ha_attributes(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Create a curated, stable attribute set for HA entities (recorder-friendly)."""
    if not row:
        return None
    r = _sanitize_decoder_row(row)

    def _cf(v):
        try:
            f = float(v)
            return f if math.isfinite(f) else None
        except (TypeError, ValueError):
            return None

    lat = _cf(r.get("latitude"))
    lon = _cf(r.get("longitude"))
    acc = _cf(r.get("accuracy"))
    alt = _cf(r.get("altitude"))

    out: Dict[str, Any] = {
        "device_name": r.get("name"),
        "device_id": r.get("device_id") or r.get("id"),
        "status": r.get("status"),
        "semantic_name": r.get("semantic_name"),
        "battery_level": r.get("battery_level"),
        "last_seen": r.get("last_seen"),
        "last_seen_utc": r.get("last_seen_utc"),
        "source_label": r.get("source_label"),
        "source_rank": r.get("source_rank"),
        "is_own_report": r.get("is_own_report"),
    }
    if lat is not None and lon is not None:
        out["latitude"] = lat
        out["longitude"] = lon
    if acc is not None:
        out["accuracy_m"] = acc
    if alt is not None:
        out["altitude_m"] = alt
    return {k: v for k, v in out.items() if v is not None}


# -------------------------------------------------------------------------
# Synchronous history helper (runs in Recorder executor)
# -------------------------------------------------------------------------
def _sync_get_last_gps_from_history(
    hass: HomeAssistant, entity_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch the last state with GPS coordinates from Recorder History.

    IMPORTANT:
        This function is synchronous and performs database I/O. It MUST be
        run in a worker thread (e.g., the Recorder's executor) to avoid
        blocking the Home Assistant event loop.

    Args:
        hass: The Home Assistant instance.
        entity_id: The entity ID of the device_tracker to query.

    Returns:
        A dictionary containing the last known location data, or None if not found.
    """
    try:
        # Minimal query: the last single change for this entity_id
        # API expects an iterable of entity_ids.
        changes = recorder_history.get_last_state_changes(hass, 1, [entity_id])
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


@dataclass(frozen=True)
class StatusSnapshot:
    """Lightweight status descriptor shared with diagnostic entities/tests."""

    state: str
    reason: Optional[str] = None
    changed_at: Optional[float] = None


class ApiStatus:
    """String constants describing the coordinator's polling state."""

    UNKNOWN = "unknown"
    OK = "ok"
    ERROR = "error"
    REAUTH = "reauth_required"


class FcmStatus:
    """String constants representing push-transport health."""

    UNKNOWN = "unknown"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


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

        # Try to ensure entry-scoped namespace on the cache early when possible.
        # This will be finalized in async_setup() once the ConfigEntry is bound.
        try:
            if not getattr(self._cache, "entry_id", None) and getattr(self, "config_entry", None):
                setattr(self._cache, "entry_id", self.config_entry.entry_id)  # type: ignore[attr-defined]
        except Exception:
            pass

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

        # Initialize diagnostics buffer and one-shot warning guard for malformed IDs.
        self._diag = DiagnosticsBuffer(max_items=200)
        self._warned_bad_identifier_devices: set[str] = set()

        # Internal caches & bookkeeping
        self._device_location_data: Dict[str, Dict[str, Any]] = {}  # device_id -> location dict
        self._device_names: Dict[str, str] = {}  # device_id -> human name
        self._device_caps: Dict[str, Dict[str, Any]] = {}  # device_id -> caps (e.g., {"can_ring": True})
        self._present_device_ids: Set[str] = set()  # diagnostics-only set from latest non-empty list

        # Flag to separate initial discovery from later runtime additions.
        # After the first successful non-empty device list is processed, this becomes True.
        self._initial_discovery_done: bool = False

        # Presence smoothing (TTL):
        # - Per-device "last seen in full list" timestamp (monotonic)
        # - Cold-start marker: timestamp of last non-empty list (monotonic)
        # - Presence TTL in seconds (derived from poll interval, min 120s)
        self._present_last_seen: Dict[str, float] = {}
        self._last_nonempty_wall: float = 0.0
        self._presence_ttl_s: int = 120

        # Minimal hardening state (empty-list quorum)
        self._last_device_list: List[Dict[str, Any]] = []
        self._empty_list_streak: int = 0

        # Polling state
        self._poll_lock = asyncio.Lock()
        self._is_polling = False
        self._startup_complete = False
        self._last_poll_mono: float = 0.0  # monotonic timestamp for scheduling

        # Push readiness deferral/escalation bookkeeping
        self._fcm_defer_started_mono: float = 0.0
        self._fcm_last_stage: int = 0  # 0=none, 1=warned, 2=errored

        # Push readiness memoization and cooldown after transport errors
        self._push_ready_memo: Optional[bool] = None
        self._push_cooldown_until: float = 0.0

        # Manual locate gating (UX + server protection)
        self._locate_inflight: Set[str] = set()  # device_id -> in-flight flag
        self._locate_cooldown_until: Dict[str, float] = {}  # device_id -> mono deadline

        # Per-device poll cooldowns after owner/crowdsourced reports.
        self._device_poll_cooldown_until: Dict[str, float] = {}

        # DR-driven poll targeting
        self._enabled_poll_device_ids: Set[str] = set()
        self._devices_with_entry: Set[str] = set()
        self._dr_unsub: Optional[Callable] = None

        # Statistics (extend as needed)
        self.stats: Dict[str, int] = {
            "background_updates": 0,         # FCM/push-driven updates + manual commits
            "polled_updates": 0,             # sequential poll-driven updates
            "crowd_sourced_updates": 0,      # number of crowdsourced updates observed
            "history_fallback_used": 0,      # times we had to fall back to Recorder history
            "timeouts": 0,                   # request timeouts
            "invalid_coords": 0,             # coordinate validation failures
            "low_quality_dropped": 0,        # dropped due to accuracy worse than threshold
            "invalid_ts_drop_count": 0,      # invalid or stale (< existing) timestamps
            "future_ts_drop_count": 0,       # timestamps too far in the future
            "non_significant_dropped": 0,    # drops by significance gate
        }
        _LOGGER.debug("Initialized stats: %s", self.stats)

        # Granular status tracking (API polling vs. push transport)
        self._api_status_state: str = ApiStatus.UNKNOWN
        self._api_status_reason: Optional[str] = None
        self._api_status_changed_at: Optional[float] = None
        self._fcm_status_state: str = FcmStatus.UNKNOWN
        self._fcm_status_reason: Optional[str] = None
        self._fcm_status_changed_at: Optional[float] = None
        self._reauth_initiated: bool = False

        # Performance metrics (timestamps, durations) & recent errors (bounded)
        self.performance_metrics: Dict[str, float] = {}
        self.recent_errors = deque(maxlen=5)  # entries: (epoch_ts, error_type, short_message)

        # Debounced stats persistence (avoid flushing on every increment)
        self._stats_save_task: Optional[asyncio.Task] = None
        self._stats_debounce_seconds: float = 5.0

        # Load persistent statistics asynchronously (name the task for better debugging)
        self.hass.async_create_task(self._async_load_stats(), name=f"{DOMAIN}.load_stats")

        # Short-retry scheduling handle (coalesced)
        self._short_retry_cancel: Optional[Callable[[], None]] = None

        # NEW: Authentication/repairs state
        self._auth_error_active: bool = False
        self._auth_error_since: float = 0.0
        self._auth_error_message: Optional[str] = None

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def async_setup(self) -> None:
        """One-time async setup called from __init__.py (entry setup).

        - Loads stats (already scheduled in __init__, so this is idempotent).
        - Indexes poll targets from the Device Registry.
        - Subscribes to DR updates (unsubscribed in `async_shutdown()`).
        - Ensures the per-entry "service device" exists in the Device Registry.
        - Enforces entry-scoped namespace by attaching `entry_id` to the cache object.
        """
        # Ensure the cache carries our entry_id namespace for downstream Nova/API helpers.
        try:
            entry = getattr(self, "config_entry", None) or getattr(self, "entry", None)
            if entry and not getattr(self._cache, "entry_id", None):
                setattr(self._cache, "entry_id", entry.entry_id)
        except Exception:
            pass

        # Make sure the service device exists early to anchor end-devices via `via_device`.
        self._ensure_service_device_exists()

        # Initial index (works even if config_entry is not yet bound; will re-run on DR event)
        self._reindex_poll_targets_from_device_registry()
        if self._dr_unsub is None:
            self._dr_unsub = self.hass.bus.async_listen(
                EVENT_DEVICE_REGISTRY_UPDATED, self._handle_dr_event
            )

    async def async_shutdown(self) -> None:
        """Clean up listeners and timers on entry unload to avoid leaks."""
        # Unsubscribe DR listener
        if self._dr_unsub is not None:
            try:
                self._dr_unsub()
            except Exception:
                pass
            self._dr_unsub = None
        # Cancel short-retry callback if scheduled
        if self._short_retry_cancel is not None:
            try:
                self._short_retry_cancel()
            except Exception:
                pass
            finally:
                self._short_retry_cancel = None
        # Cancel pending debounced stats write
        if self._stats_save_task and not self._stats_save_task.done():
            self._stats_save_task.cancel()

    # --- BEGIN: Add/Replace inside Coordinator class ------------------------------
    def _redact_text(self, value: Optional[str], max_len: int = 120) -> str:
        """Return a short, redacted string variant suitable for logs/diagnostics."""
        if not value:
            return ""
        s = str(value)
        return s if len(s) <= max_len else (s[:max_len] + "…")

    def _device_display_name(self, dev: dr.DeviceEntry, fallback: str) -> str:
        """Return the best human-friendly device name without sensitive data."""
        return (dev.name_by_user or dev.name or fallback or "").strip()

    def _entry_id(self) -> Optional[str]:
        """Small helper to read the bound ConfigEntry ID (None at very early startup)."""
        entry = getattr(self, "config_entry", None)
        return getattr(entry, "entry_id", None)

    def _extract_our_identifier(self, device: dr.DeviceEntry) -> Optional[str]:
        """Return the first valid (DOMAIN, identifier) from a device, else None.

        Multi-account compatibility:
        - Since 2025.5+ we use **entry-scoped device identifiers** in the Device Registry
          to guarantee global uniqueness across multiple accounts:
              (DOMAIN, f\"{entry_id}:{device_id}\")
        - For backward compatibility we also recognize legacy identifiers:
              (DOMAIN, device_id)

        This helper:
        * Extracts our identifier
        * If it has the namespaced form, it returns the **raw device_id** part
          (the coordinator uses canonical device IDs internally).
        * If malformed tuples are encountered, it logs once and records a diagnostics warning.
        """
        entry_id = self._entry_id()
        for item in device.identifiers:
            try:
                domain, ident = item  # spec: 2-tuple (DOMAIN, identifier)
            except (TypeError, ValueError):
                if device.id not in self._warned_bad_identifier_devices:
                    self._warned_bad_identifier_devices.add(device.id)
                    _LOGGER.warning(
                        "Device %s has a non-conforming identifier: %r "
                        "(spec requires (DOMAIN, identifier) tuple); item ignored.",
                        device.id,
                        item,
                    )
                    self._diag.add_warning(
                        code="malformed_device_identifier",
                        context={
                            "device_id": device.id,
                            "device_name": self._device_display_name(device, ""),
                            "offending_item": self._redact_text(repr(item)),
                            "note": "Non-conforming identifier; ignored by parser.",
                        },
                    )
                continue

            if domain != DOMAIN or not isinstance(ident, str) or not ident:
                continue

            # Handle namespaced format "<entry_id>:<device_id>"
            if ":" in ident:
                if entry_id and ident.startswith(entry_id + ":"):
                    return ident.split(":", 1)[1]  # return canonical device_id
                # Identifier belongs to a different entry; ignore.
                continue
            # Legacy format -> accept as-is
            return ident
        return None

    def _ensure_service_device_exists(self, entry: 'ConfigEntry | None' = None) -> None:
        """Idempotently create/update the per-entry 'service device' in the device registry.

        This keeps diagnostic entities (e.g. polling/auth-status) grouped under a stable
        integration-level device. Safe to call multiple times.
        """
        # Resolve hass
        hass = getattr(self, "hass", None)
        if hass is None:
            return

        # Resolve ConfigEntry (works with either .entry or .config_entry on the coordinator)
        entry = entry or getattr(self, "entry", None) or getattr(self, "config_entry", None)
        if entry is None:
            _LOGGER.debug("Service-device ensure skipped: ConfigEntry not available on coordinator.")
            return

        # Fast-path: already ensured in this runtime
        if getattr(self, "_service_device_ready", False):
            return

        dev_reg = dr.async_get(hass)
        identifiers = {service_device_identifier(entry.entry_id)}  # {(DOMAIN, f"integration_<entry_id>")}

        device = dev_reg.async_get_device(identifiers=identifiers)
        if device is None:
            device = dev_reg.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers=identifiers,
                name=SERVICE_DEVICE_NAME,
                manufacturer=SERVICE_DEVICE_MANUFACTURER,
                model=SERVICE_DEVICE_MODEL,
                sw_version=INTEGRATION_VERSION,
                entry_type=dr.DeviceEntryType.SERVICE,
                configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
            )
            _LOGGER.debug("Created Google Find My service device for entry %s (device_id=%s)",
                          entry.entry_id, getattr(device, "id", None))
        else:
            # Keep metadata fresh if it drifted (rare)
            needs_update = (
                device.manufacturer != SERVICE_DEVICE_MANUFACTURER
                or device.model != SERVICE_DEVICE_MODEL
                or device.sw_version != INTEGRATION_VERSION
                or device.name != SERVICE_DEVICE_NAME
                or device.entry_type != dr.DeviceEntryType.SERVICE
            )
            if needs_update:
                dev_reg.async_update_device(
                    device_id=device.id,
                    manufacturer=SERVICE_DEVICE_MANUFACTURER,
                    model=SERVICE_DEVICE_MODEL,
                    sw_version=INTEGRATION_VERSION,
                    name=SERVICE_DEVICE_NAME,
                    entry_type=dr.DeviceEntryType.SERVICE,
                    configuration_url="https://github.com/BSkando/GoogleFindMy-HA",
                )
                _LOGGER.debug("Updated Google Find My service device metadata for entry %s", entry.entry_id)

        # Book-keeping for quick re-entrance
        self._service_device_ready = True
        self._service_device_id = getattr(device, "id", None)

    # Optional back-compat alias (some callers may use the public-style name)
    ensure_service_device_exists = _ensure_service_device_exists

    @callback
    def _reindex_poll_targets_from_device_registry(self) -> None:
        """Rebuild internal poll target sets from registries (fast, robust, diagnostics-aware).

        Semantics:
        - Consider ONLY devices that belong to THIS config entry (no global scan).
        - A device is "present" if we can extract a valid (DOMAIN, identifier).
        - A device is "enabled for polling" if there is at least one ENABLED
          `device_tracker` entity for our domain on that device AND the device
          itself is not disabled. This preserves the entities-driven polling
          selection and reduces UI churning.

        Multi-account safety:
        - Uses entry-scoped identifiers in the Device Registry:
              (DOMAIN, f"{entry_id}:{device_id}")
          and gracefully accepts legacy identifiers `(DOMAIN, device_id)`.
        """
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        entry_id = self._entry_id()

        if not entry_id:
            self._devices_with_entry = set()
            self._enabled_poll_device_ids = set()
            _LOGGER.debug("Skipping DR reindex: no config_entry bound yet")
            return

        # Limit to our integration's devices/entities: avoids interference & improves performance.
        devices_for_entry = dr.async_entries_for_config_entry(dev_reg, entry_id)
        entities_for_entry = er.async_entries_for_config_entry(ent_reg, entry_id)

        present: Set[str] = set()
        enabled: Set[str] = set()

        # Map device_id -> has_enabled_tracker_entity
        has_enabled_tracker: Dict[str, bool] = {}
        for ent in entities_for_entry:
            # We only care about our domain and enabled entities
            if ent.platform != DOMAIN or ent.disabled_by is not None:
                continue
            # Only trackers drive polling
            if ent.domain == "device_tracker" and ent.device_id:
                has_enabled_tracker[ent.device_id] = True

        for dev in devices_for_entry:
            ident = self._extract_our_identifier(dev)
            if not ident:
                continue
            present.add(ident)
            if dev.id in has_enabled_tracker and dev.disabled_by is None:
                enabled.add(ident)

        self._devices_with_entry = present
        self._enabled_poll_device_ids = enabled

        _LOGGER.debug(
            "Reindexed targets for entry %s: %d present / %d enabled (entities-driven)",
            entry_id,
            len(present),
            len(enabled),
        )

    # --- NEW: Create/refresh DR entries for end devices (entry-scoped) -----
    def _ensure_registry_for_devices(
        self, devices: List[Dict[str, Any]], ignored: Set[str]
    ) -> int:
        """Ensure end-device DR entries exist and link via the per-entry service device.

        Multi-account/compatibility rules:
        - **Primary identifier (namespaced):** (DOMAIN, f"{entry_id}:{device_id}")
          guarantees global uniqueness across config entries.
        - **Legacy identifier (non-namespaced):** (DOMAIN, device_id) recognized for
          existing installs. If a legacy device belongs to *this* entry, we migrate it
          by adding the new identifier (union) via `async_update_device`.
        - If a legacy device is associated with a *different* entry, we **do not merge**.
          We create a fresh device with the namespaced identifier to avoid cross-account
          collisions.

        Returns:
            Count of devices that were created or updated.
        """
        entry_id = self._entry_id()
        if not entry_id:
            return 0

        dev_reg = dr.async_get(self.hass)
        created_or_updated = 0

        for d in devices:
            dev_id = d.get("id")
            if not isinstance(dev_id, str) or dev_id in ignored:
                continue

            # Build identifiers
            ns_ident = (DOMAIN, f"{entry_id}:{dev_id}")
            legacy_ident = (DOMAIN, dev_id)

            # Preferred: device already known by namespaced identifier?
            dev = dev_reg.async_get_device(identifiers={ns_ident})
            if dev is None:
                # Legacy present?
                legacy_dev = dev_reg.async_get_device(identifiers={legacy_ident})
                if legacy_dev is not None:
                    # If legacy device belongs to THIS entry, migrate by adding namespaced ident.
                    if entry_id in legacy_dev.config_entries:
                        new_idents = set(legacy_dev.identifiers)
                        new_idents.add(ns_ident)
                        dev_reg.async_update_device(
                            device_id=legacy_dev.id,
                            new_identifiers=new_idents,
                        )
                        dev = legacy_dev
                        created_or_updated += 1
                    else:
                        # Belongs to another entry → create a new device with namespaced ident (no merge).
                        dev = None

            # Create if still missing
            if dev is None:
                # Only set a real label; never write placeholders on cold boot
                raw_name = (d.get("name") or "").strip()
                use_name = raw_name if raw_name and raw_name != "Google Find My Device" else None

                dev = dev_reg.async_get_or_create(
                    config_entry_id=entry_id,
                    identifiers={ns_ident},
                    manufacturer="Google",
                    model="Find My Device",
                    name=use_name,
                    via_device=getattr(self, "_service_device_id", None),
                )
                created_or_updated += 1
            else:
                # Keep name fresh if not user-overridden and a new upstream label is available
                raw_name = (d.get("name") or "").strip()
                use_name = raw_name if raw_name and raw_name != "Google Find My Device" else None
                if use_name and not dev.name_by_user and dev.name != use_name:
                    dev_reg.async_update_device(device_id=dev.id, name=use_name)
                    created_or_updated += 1  # count as an update for logging parity

        return created_or_updated

    # --- NEW: Repairs + Auth state helpers ---------------------------------
    def _get_account_email(self) -> str:
        """Return the configured Google account email for this entry (empty if unknown)."""
        entry = getattr(self, "config_entry", None)
        if entry and isinstance(entry.data.get(CONF_GOOGLE_EMAIL), str):
            return entry.data[CONF_GOOGLE_EMAIL]
        return ""

    def _create_auth_issue(self) -> None:
        """Create (idempotent) a Repairs issue for an authentication problem.

        Uses:
            - domain: `googlefindmy`
            - issue_id: stable per-entry (via `issue_id_for(entry_id)`)
            - translation_key: `ISSUE_AUTH_EXPIRED_KEY` (localizable title/description)
            - placeholders: `email` (shown in repairs UI)
        """
        entry = getattr(self, "config_entry", None)
        if not entry:
            return
        issue_id = issue_id_for(entry.entry_id)
        email = self._get_account_email() or "unknown"
        try:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_AUTH_EXPIRED_KEY,
                translation_placeholders={"email": email},
            )
        except Exception as err:
            _LOGGER.debug("Failed to create Repairs issue: %s", err)

    def _dismiss_auth_issue(self) -> None:
        """Dismiss (idempotently) the Repairs issue if present."""
        entry = getattr(self, "config_entry", None)
        if not entry:
            return
        try:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id_for(entry.entry_id))
        except Exception:
            # Deleting a non-existent issue is fine; keep silent.
            pass

    def _set_auth_state(self, *, failed: bool, reason: Optional[str] = None) -> None:
        """State machine for authentication error transitions.

        - When entering the "failed" state:
            * create a Repairs issue (idempotent)
            * fire a domain-scoped HA event (EVENT_AUTH_ERROR)
            * set `auth_error_active = True` and store a short message
            * push updated data so diagnostic sensors can flip to `on`
        - When entering the "ok" state from "failed":
            * dismiss the Repairs issue
            * fire an OK event (EVENT_AUTH_OK)
            * set `auth_error_active = False` and clear the message
            * push updated data so diagnostic sensors can flip back to `off`
        """
        if failed and not self._auth_error_active:
            self._auth_error_active = True
            self._auth_error_since = time.time()
            self._auth_error_message = (reason or "Authentication failed").strip()
            # Repairs + event
            self._create_auth_issue()
            self.hass.bus.async_fire(
                EVENT_AUTH_ERROR,
                {
                    "entry_id": getattr(self, "config_entry", None)
                    and getattr(self.config_entry, "entry_id", "")
                    or "",
                    "email": self._get_account_email(),
                    "message": self._auth_error_message,
                },
            )
            # Notify listeners (binary_sensor etc.)
            try:
                self.async_set_updated_data(self.data)
            except Exception:
                pass
        elif not failed and self._auth_error_active:
            self._auth_error_active = False
            self._auth_error_message = None
            self._dismiss_auth_issue()
            self.hass.bus.async_fire(
                EVENT_AUTH_OK,
                {
                    "entry_id": getattr(self, "config_entry", None)
                    and getattr(self.config_entry, "entry_id", "")
                    or "",
                    "email": self._get_account_email(),
                },
            )
            try:
                self.async_set_updated_data(self.data)
            except Exception:
                pass

        if not failed:
            # Allow future reauth flows once auth recovers.
            self._reauth_initiated = False

    @property
    def auth_error_active(self) -> bool:
        """Expose the current "auth failed" condition for diagnostic entities (binary_sensor)."""
        return self._auth_error_active

    def _set_api_status(self, status: str, *, reason: Optional[str] = None) -> None:
        """Update the API polling status and notify listeners if it changed."""

        if status == self._api_status_state and reason == self._api_status_reason:
            return

        self._api_status_state = status
        self._api_status_reason = reason
        self._api_status_changed_at = time.time()

        try:
            self.async_set_updated_data(self.data)
        except Exception:
            # Fallback for very early startup when listeners are not ready yet.
            pass

    def _set_fcm_status(self, status: str, *, reason: Optional[str] = None) -> None:
        """Update the push transport status while avoiding noisy churn."""

        if status == self._fcm_status_state and reason == self._fcm_status_reason:
            return

        self._fcm_status_state = status
        self._fcm_status_reason = reason
        self._fcm_status_changed_at = time.time()

        try:
            self.async_set_updated_data(self.data)
        except Exception:
            pass

    async def _async_start_reauth_flow(self) -> None:
        """Trigger Home Assistant's re-auth flow once per failure episode."""

        if self._reauth_initiated:
            return

        entry = getattr(self, "config_entry", None)
        if entry is None:
            return

        self._reauth_initiated = True

        # Prefer the ConfigEntry helper when available; fall back to the manager API.
        try:
            result = entry.async_start_reauth(self.hass)
        except AttributeError:
            result = None
        else:
            if asyncio.iscoroutine(result):
                try:
                    await result
                except Exception as err:
                    _LOGGER.debug("async_start_reauth(entry) failed: %s", err)
                    self._reauth_initiated = False
                return
            if result is not None:
                return

        hass_config_entries = getattr(self.hass, "config_entries", None)
        if hass_config_entries is None:
            _LOGGER.debug("Home Assistant config_entries manager unavailable; cannot start reauth")
            self._reauth_initiated = False
            return

        try:
            result = hass_config_entries.async_start_reauth(entry)
        except TypeError:
            try:
                result = hass_config_entries.async_start_reauth(entry.entry_id)
            except Exception as err:
                _LOGGER.debug("async_start_reauth(entry_id) failed: %s", err)
                self._reauth_initiated = False
                return
        except Exception as err:
            _LOGGER.debug("async_start_reauth fallback failed: %s", err)
            self._reauth_initiated = False
            return

        if asyncio.iscoroutine(result):
            try:
                await result
            except Exception as err:
                _LOGGER.debug("async_start_reauth coroutine failed: %s", err)
                self._reauth_initiated = False

    @property
    def api_status(self) -> StatusSnapshot:
        """Return a snapshot describing the current API polling health."""

        return StatusSnapshot(
            state=self._api_status_state,
            reason=self._api_status_reason,
            changed_at=self._api_status_changed_at,
        )

    @property
    def fcm_status(self) -> StatusSnapshot:
        """Return a snapshot describing the current push transport health."""

        return StatusSnapshot(
            state=self._fcm_status_state,
            reason=self._fcm_status_reason,
            changed_at=self._fcm_status_changed_at,
        )

    @property
    def is_fcm_connected(self) -> bool:
        """Convenience boolean for entities relying on push transport availability."""

        return self._fcm_status_state == FcmStatus.CONNECTED

    # --- END: Add/Replace inside Coordinator class --------------------------------

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

    def _dispatch_async_request_refresh(self, *, task_name: str, log_context: str) -> None:
        """Invoke ``async_request_refresh`` safely regardless of its implementation."""

        fn = getattr(self, "async_request_refresh", None)
        if not callable(fn):
            return

        def _invoke() -> None:
            try:
                result = fn()
                if inspect.isawaitable(result):
                    self.hass.async_create_task(result, name=task_name)
            except Exception as err:
                _LOGGER.debug(
                    "async_request_refresh dispatch failed (%s): %s", log_context, err
                )

        if self._is_on_hass_loop():
            _invoke()
        else:
            self._run_on_hass_loop(_invoke)

    def _schedule_short_retry(self, delay_s: float = 5.0) -> None:
        """Schedule a short, coalesced refresh instead of shifting the poll baseline.

        Rationale:
        - When FCM/push is not ready, we *do not* advance `_last_poll_mono`.
          Advancing the baseline hides readiness transitions and can put the
          scheduler to "sleep". Instead, we request a short follow-up refresh.

        Behavior:
        - Coalesces multiple calls by cancelling a pending callback first.
        - Always runs on the HA event loop.

        Args:
            delay_s: Delay in seconds before requesting a coordinator refresh.
        """

        def _do_schedule() -> None:
            # Cancel a pending short retry (coalesce)
            if self._short_retry_cancel is not None:
                try:
                    self._short_retry_cancel()
                except Exception:  # defensive
                    pass
                finally:
                    self._short_retry_cancel = None

            def _cb(_now) -> None:
                # Clear handle and request a refresh (non-blocking)
                self._short_retry_cancel = None
                self._dispatch_async_request_refresh(
                    task_name=f"{DOMAIN}.short_retry_refresh",
                    log_context="short retry",
                )

            self._short_retry_cancel = async_call_later(
                self.hass, max(0.0, float(delay_s)), _cb
            )

        if self._is_on_hass_loop():
            _do_schedule()
        else:
            self._run_on_hass_loop(_do_schedule)

    # ---------------------------- Device Registry helpers -------------------
    async def _handle_dr_event(self, _event) -> None:
        """Handle Device Registry changes by rebuilding poll targets (rare)."""
        self._reindex_poll_targets_from_device_registry()
        # After changes, request a refresh so the next tick uses the new target sets.
        self._dispatch_async_request_refresh(
            task_name=f"{DOMAIN}.dr_event_refresh",
            log_context="device registry event",
        )

    # ---------------------------- Cooldown helpers (server-aware) -----------
    def _compute_type_cooldown_seconds(self, report_hint: Optional[str]) -> int:
        """Return a server-aware cooldown duration in seconds for a crowdsourced report type.

        Derived from POPETS'25 observations:
        - "in_all_areas": ~10 min throttle window (minimum).
        - "high_traffic": ~5 min throttle window (minimum).

        IMPORTANT:
        - To guarantee effect, the applied cooldown is **never shorter than** the
          configured `location_poll_interval`. This ensures at least one scheduled
          poll cycle is skipped in practice.
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

    def _apply_report_type_cooldown(
        self, device_id: str, report_hint: Optional[str]
    ) -> None:
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
                raw = entry.options.get(
                    OPT_IGNORED_DEVICES, DEFAULT_OPTIONS.get(OPT_IGNORED_DEVICES, {})
                )
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

    # ---------------------------- Metrics & errors helpers ------------------
    def safe_update_metric(self, key: str, value: float) -> None:
        """Safely set a numeric performance metric (float-coerced)."""
        try:
            self.performance_metrics[str(key)] = float(value)
        except Exception:
            # Never raise from diagnostics helpers
            pass

    def _short_error_message(self, exc: Exception | str) -> str:
        """Return a compact, single-line error string (no PII redaction beyond truncation)."""
        msg = str(exc)
        # collapse newlines/whitespace
        msg = " ".join(msg.split())
        # bound message length
        if len(msg) > 180:
            msg = msg[:177] + "..."
        return msg

    def _append_recent_error(self, err_type: str, message: str) -> None:
        """Append a (timestamp, type, message) triple to the bounded deque."""
        try:
            self.recent_errors.append(
                (time.time(), err_type, self._short_error_message(message))
            )
        except Exception:
            pass

    def note_error(
        self, exc: Exception, *, where: str = "", device: Optional[str] = None
    ) -> None:
        """Public helper to record non-fatal errors with minimal context."""
        prefix = where or "coordinator"
        if device:
            prefix += f"({device})"
        err_type = type(exc).__name__
        self._append_recent_error(err_type, f"{prefix}: {exc}")

    # Safe getters for durations based on keys that __init__.py may set.
    def get_metric(self, key: str) -> Optional[float]:
        val = self.performance_metrics.get(key)
        return float(val) if isinstance(val, (int, float)) else None

    def _get_duration(self, start_key: str, end_key: str) -> Optional[float]:
        start = self.get_metric(start_key)
        end = self.get_metric(end_key)
        if start is None or end is None:
            return None
        try:
            return max(0.0, float(end) - float(start))
        except Exception:
            return None

    def get_setup_duration_seconds(self) -> Optional[float]:
        """Duration between 'setup_start_monotonic' and 'setup_end_monotonic'."""
        return self._get_duration("setup_start_monotonic", "setup_end_monotonic")

    def get_fcm_acquire_duration_seconds(self) -> Optional[float]:
        """Duration between 'setup_start_monotonic' and 'fcm_acquired_monotonic'."""
        start = self.get_metric("setup_start_monotonic")
        fcm = self.get_metric("fcm_acquired_monotonic")
        if start is None or fcm is None:
            return None
        try:
            return max(0.0, float(fcm) - float(start))
        except Exception:
            return None

    def get_last_poll_duration_seconds(self) -> Optional[float]:
        """Duration of the most recent sequential polling cycle (if recorded)."""
        return self._get_duration("last_poll_start_mono", "last_poll_end_mono")

    def get_recent_errors(self) -> List[Dict[str, Any]]:
        """Return a JSON-friendly copy of recent error triples."""
        out: List[Dict[str, Any]] = []
        for ts, et, msg in list(self.recent_errors):
            out.append({"timestamp": ts, "error_type": et, "message": msg})
        return out

    # ---------------------------- HA Coordinator ----------------------------
    def _is_fcm_ready_soft(self) -> bool:
        """Return True if push transport appears ready (no awaits, no I/O).

        Priority order:
          1) Ask API (single source of truth if available).
          2) Receiver-level booleans.
          3) Push-client heuristic (run_state + do_listen).
          4) Token presence as last resort.
        """
        try:
            # 1) API knowledge (preferred)
            try:
                fn = getattr(self.api, "is_push_ready", None)
                if callable(fn):
                    return bool(fn())
            except Exception:
                pass

            # 2) Receiver-level flags
            fcm = self.hass.data.get(DOMAIN, {}).get("fcm_receiver")
            if not fcm:
                return False
            for attr in ("is_ready", "ready"):
                val = getattr(fcm, attr, None)
                if isinstance(val, bool):
                    return val

            # 3) Heuristic: push client state (no enum import)
            pc = getattr(fcm, "pc", None)
            if pc is not None:
                state = getattr(pc, "run_state", None)
                state_name = getattr(state, "name", state)
                if state_name == "STARTED" and bool(getattr(pc, "do_listen", False)):
                    return True

            # 4) Token as last resort
            try:
                token = fcm.get_fcm_token()
                if isinstance(token, str) and len(token) >= 10:
                    return True
            except Exception:
                pass

            return False
        except Exception:
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
            self._set_fcm_status(
                FcmStatus.DEGRADED,
                reason="Push transport not ready; awaiting connection",
            )
            return
        elapsed = now_mono - self._fcm_defer_started_mono
        if elapsed >= 60 and self._fcm_last_stage < 1:
            self._fcm_last_stage = 1
            _LOGGER.warning(
                "Polling deferred: FCM/push not ready 60s after (re)start. Polls and actions remain gated."
            )
            self._set_fcm_status(
                FcmStatus.DEGRADED,
                reason="Push transport waiting for connection (60s elapsed)",
            )
        if elapsed >= 300 and self._fcm_last_stage < 2:
            self._fcm_last_stage = 2
            _LOGGER.error(
                "Polling still deferred: FCM/push not ready after 5 minutes. Check credentials/network."
            )
            self._set_fcm_status(
                FcmStatus.DISCONNECTED,
                reason="Push transport not connected after prolonged wait",
            )

    def _clear_fcm_deferral(self) -> None:
        """Clear the escalation timeline once FCM becomes ready (log once)."""
        if self._fcm_defer_started_mono:
            _LOGGER.info("FCM/push is ready; resuming scheduled polling.")
        self._fcm_defer_started_mono = 0.0
        self._fcm_last_stage = 0
        self._set_fcm_status(FcmStatus.CONNECTED)

    async def _async_update_data(self) -> List[Dict[str, Any]]:
        """Provide cached device data; trigger background poll if due.

        Discovery semantics:
        - Always fetch the **full** lightweight device list (no executor).
        - Update presence and metadata caches for **all** devices.
        - The published snapshot (`self.data`) contains **all** devices (for dynamic entity creation).
        - The sequential **polling cycle** polls devices that are enabled in HA's Device Registry
          **for this config entry** and not explicitly ignored in integration options.

        Returns:
            A list of dictionaries, where each dictionary represents a device's state.

        Raises:
            ConfigEntryAuthFailed: If authentication fails during device list fetching.
            UpdateFailed: For other transient or unexpected errors.
        """
        try:
            # One-time wait for FCM on first run.
            if not self._startup_complete:
                fcm_evt = getattr(self, "fcm_ready_event", None)
                if isinstance(fcm_evt, asyncio.Event) and not fcm_evt.is_set():
                    _LOGGER.debug("First run: waiting for FCM provider to become ready...")
                    try:
                        await asyncio.wait_for(fcm_evt.wait(), timeout=15.0)
                        _LOGGER.debug("FCM provider is ready; proceeding.")
                    except asyncio.TimeoutError:
                        _LOGGER.warning("FCM provider not ready after 15s; proceeding anyway.")
                self._startup_complete = True

            if self._is_fcm_ready_soft():
                self._set_fcm_status(FcmStatus.CONNECTED)
            elif self._fcm_last_stage < 2:
                self._set_fcm_status(
                    FcmStatus.DEGRADED,
                    reason="Push transport not ready; continuing with cached data",
                )

            # 1) Always fetch the lightweight FULL device list using native async API
            all_devices = await self.api.async_get_basic_device_list()

            # Success path: if we were in an auth error state, clear it now.
            self._set_auth_state(failed=False)
            self._set_api_status(ApiStatus.OK)

            all_devices = all_devices or []

            # Minimal hardening against false empties (keep prior behaviour)
            if not all_devices:
                self._empty_list_streak += 1
                if self._empty_list_streak < _EMPTY_LIST_QUORUM and self._last_device_list:
                    # Defer clearing once; keep previous view stable.
                    _LOGGER.debug(
                        "Successful empty device list received (%d/%d). Deferring clear until quorum is met.",
                        self._empty_list_streak,
                        _EMPTY_LIST_QUORUM,
                    )
                    all_devices = list(self._last_device_list)
                else:
                    _LOGGER.debug(
                        "Accepting empty device list after %d consecutive empties.",
                        self._empty_list_streak,
                    )
                    # Once accepted, forget any prior list so snapshot becomes empty below.
                    self._last_device_list = []
            else:
                # Non-empty result: reset streak and remember latest good list.
                self._empty_list_streak = 0
                self._last_device_list = list(all_devices)

            # Presence TTL derives from the effective poll cadence
            effective_interval = max(self.location_poll_interval, self.min_poll_interval)
            self._presence_ttl_s = max(2 * effective_interval, 120)
            now_mono = time.monotonic()

            # Cold-start guard: if the very first seen list is empty, treat it as transient
            if not all_devices and self._last_nonempty_wall == 0.0:
                raise UpdateFailed("Cold start: empty device list; treating as transient.")

            ignored = self._get_ignored_set()

            # Record presence timestamps from the full list (unfiltered by ignore)
            if all_devices:
                for d in all_devices:
                    dev_id = d.get("id")
                    if isinstance(dev_id, str):
                        self._present_last_seen[dev_id] = now_mono
                # Keep a diagnostics-only set mirroring the latest non-empty list
                self._present_device_ids = {d["id"] for d in all_devices if isinstance(d.get("id"), str)}
                self._last_nonempty_wall = now_mono
            # If the list is empty, leave _present_last_seen untouched; TTL will decide availability.

            # 2) Update internal name/capability caches for ALL devices
            for dev in all_devices:
                dev_id = dev["id"]
                self._device_names[dev_id] = dev.get("name", dev_id)

                # Normalize and cache the "can ring" capability
                if "can_ring" in dev:
                    can_ring = bool(dev.get("can_ring"))
                    slot = self._device_caps.setdefault(dev_id, {})
                    slot["can_ring"] = can_ring

            # 2.5) Ensure Device Registry entries exist (service device + end-devices, namespaced)
            created = self._ensure_registry_for_devices(all_devices, ignored)
            if created:
                _LOGGER.debug("Device Registry ensured/updated for %d device(s).", created)

            # 3) Decide whether to trigger a poll cycle (monotonic clock)
            # Build list of devices to POLL:
            # Poll devices that have at least one enabled DR entry for this config entry;
            # if a device has no DR entry yet, include it to allow initial discovery.
            devices_to_poll = [
                d
                for d in all_devices
                if (d["id"] not in ignored)
                and (
                    d["id"] in self._enabled_poll_device_ids
                    or d["id"] not in self._devices_with_entry
                )
            ]

            # Apply per-device poll cooldowns
            if self._device_poll_cooldown_until and devices_to_poll:
                devices_to_poll = [
                    d
                    for d in devices_to_poll
                    if now_mono >= self._device_poll_cooldown_until.get(d["id"], 0.0)
                ]

            due = (now_mono - self._last_poll_mono) >= effective_interval
            if due and not self._is_polling and devices_to_poll:
                if not self._is_fcm_ready_soft():
                    # No baseline jump; schedule a short retry and escalate politely.
                    self._note_fcm_deferral(now_mono)
                    self._schedule_short_retry(min(5.0, effective_interval / 2.0))
                else:
                    if self._fcm_defer_started_mono:
                        self._clear_fcm_deferral()
                    _LOGGER.debug(
                        "Scheduling background polling cycle (devices=%d, interval=%ds)",
                        len(devices_to_poll),
                        effective_interval,
                    )
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

            # 4.5) Close the initial discovery window once we have a non-empty full list
            if not self._initial_discovery_done and all_devices:
                self._initial_discovery_done = True
                _LOGGER.info(
                    "Initial discovery window closed; newly discovered devices will be created disabled by default."
                )

            _LOGGER.debug(
                "Returning %d device entries; next poll in ~%ds",
                len(snapshot),
                int(max(0, effective_interval - (time.monotonic() - self._last_poll_mono))),
            )
            return snapshot

        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed as auth_exc:
            # Surface up to HA to trigger re-auth flow; create Repairs issue & flag before bubbling up.
            reason = self._short_error_message(auth_exc)
            self._set_api_status(ApiStatus.REAUTH, reason=reason)
            self._set_auth_state(
                failed=True,
                reason=f"Auth failed while fetching device list: {reason}",
            )
            try:
                await self._async_start_reauth_flow()
            except Exception as reauth_err:  # pragma: no cover - defensive guard
                self._reauth_initiated = False
                _LOGGER.exception(
                    "Failed to initiate re-auth flow after auth failure: %s",
                    self._short_error_message(reauth_err),
                )
            raise auth_exc
        except UpdateFailed as update_err:
            # Let pre-wrapped UpdateFailed bubble as-is after updating status
            self._set_api_status(
                ApiStatus.ERROR,
                reason=self._short_error_message(update_err),
            )
            raise
        except Exception as exc:
            # Record and raise as UpdateFailed per coordinator contract
            self.note_error(exc, where="_async_update_data")
            message = self._short_error_message(exc)
            self._set_api_status(ApiStatus.ERROR, reason=message)
            raise UpdateFailed(
                f"Unexpected error during coordinator update: {message}"
            ) from exc

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
                # No baseline jump; schedule a short retry and keep escalation ticking.
                self._note_fcm_deferral(time.monotonic())
                self._schedule_short_retry(5.0)
                return
            else:
                # If we were deferring previously, clear the escalation timeline.
                if self._fcm_defer_started_mono:
                    self._clear_fcm_deferral()

            self._is_polling = True
            self.safe_update_metric("last_poll_start_mono", time.monotonic())
            _LOGGER.debug("Starting sequential poll of %d devices", len(devices))

            try:
                for idx, dev in enumerate(devices):
                    dev_id = dev["id"]
                    dev_name = dev.get("name", dev_id)
                    _LOGGER.debug(
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

                        # Success path: ensure any previous auth error is cleared
                        self._set_auth_state(failed=False)

                        if not location:
                            _LOGGER.warning("No location data returned for %s", dev_name)
                            continue

                        # --- Apply Google Home filter (keep parity with FCM push path) ---
                        # Consume coordinate substitution from the filter when needed.
                        semantic_name = location.get("semantic_name")
                        if semantic_name and hasattr(self, "google_home_filter"):
                            try:
                                (
                                    should_filter,
                                    replacement_attrs,
                                ) = self.google_home_filter.should_filter_detection(
                                    dev_id, semantic_name
                                )
                            except Exception as gf_err:
                                _LOGGER.debug(
                                    "Google Home filter error for %s: %s",
                                    dev_name,
                                    gf_err,
                                )
                            else:
                                if should_filter:
                                    _LOGGER.debug(
                                        "Filtering out Google Home spam detection for %s",
                                        dev_name,
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
                                    if (
                                        "latitude" in replacement_attrs
                                        and "longitude" in replacement_attrs
                                    ):
                                        location["latitude"] = replacement_attrs.get("latitude")
                                        location["longitude"] = replacement_attrs.get("longitude")
                                    if (
                                        "radius" in replacement_attrs
                                        and replacement_attrs.get("radius") is not None
                                    ):
                                        location["accuracy"] = replacement_attrs.get("radius")
                                    # Clear semantic name so HA Core's zone engine determines the final state.
                                    location["semantic_name"] = None
                        # ------------------------------------------------------------------

                        # If we only got a semantic location, preserve previous coordinates.
                        if (
                            location.get("latitude") is None
                            or location.get("longitude") is None
                        ) and location.get("semantic_name"):
                            prev = self._device_location_data.get(dev_id, {})
                            if prev:
                                location["latitude"] = prev.get("latitude")
                                location["longitude"] = prev.get("longitude")
                                location["accuracy"] = prev.get("accuracy")
                                location["status"] = "Semantic location; preserving previous coordinates"

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

                        # Sanitize invariants + enrich fields (label, utc-string)
                        location = _sanitize_decoder_row(location)

                        # Increment crowdsourced updates statistic (post-sanitization)
                        if location.get("source_label") == "crowdsourced":
                            self.increment_stat("crowd_sourced_updates")

                        # Significance gate (replaces naive duplicate check)
                        if not self._is_significant_update(dev_id, location):
                            _LOGGER.debug(
                                "Skipping non-significant update for %s (last_seen=%s)",
                                dev_name,
                                location.get("last_seen"),
                            )
                            self.increment_stat("non_significant_dropped")
                            # Strip internal hint before dropping to avoid accidental exposure
                            location.pop("_report_hint", None)
                            continue

                        # Age diagnostics (informational)
                        wall_now = time.time()
                        last_seen = location.get("last_seen", 0)
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

                        # Commit to cache and bump statistics
                        location["last_updated"] = wall_now  # wall-clock for UX
                        self._device_location_data[dev_id] = location
                        self.increment_stat("polled_updates")

                        # Immediate per-device update for more responsive UI during long poll cycles.
                        self.push_updated([dev_id])

                    except asyncio.TimeoutError as terr:
                        _LOGGER.info(
                            "Location request timed out for %s after %s seconds",
                            dev_name,
                            LOCATION_REQUEST_TIMEOUT_S,
                        )
                        self.increment_stat("timeouts")
                        self.note_error(terr, where="poll_timeout", device=dev_name)
                    except ConfigEntryAuthFailed as auth_exc:
                        # Mark auth failures to HA; abort remaining devices by re-raising.
                        self._set_auth_state(
                            failed=True,
                            reason=f"Auth failed during poll for {dev_name}: {auth_exc}",
                        )
                        raise
                    except Exception as err:
                        _LOGGER.error("Failed to get location for %s: %s", dev_name, err)
                        self.note_error(err, where="poll_exception", device=dev_name)

                    # Inter-device delay (except after the last one)
                    if idx < len(devices) - 1 and self.device_poll_delay > 0:
                        await asyncio.sleep(self.device_poll_delay)

                _LOGGER.debug("Completed polling cycle for %d devices", len(devices))
            finally:
                # Update scheduling baseline and clear flag, then push end snapshot
                self._last_poll_mono = time.monotonic()
                self._is_polling = False
                self.safe_update_metric("last_poll_end_mono", time.monotonic())
                # Publish a full, visible snapshot (not just polled devices)
                ignored = self._get_ignored_set()
                # Use the latest remembered full list; filter ignored
                visible_devices = [
                    d for d in (self._last_device_list or []) if d.get("id") not in ignored
                ]
                end_snapshot = self._build_snapshot_from_cache(
                    visible_devices, wall_now=time.time()
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

    def _build_snapshot_from_cache(self, devices: List[Dict[str, Any]], wall_now: float) -> List[Dict[str, Any]]:
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
                entry = _sanitize_decoder_row(entry)
                snapshot.append(entry)
                continue

            # No cache -> Registry + State (cheap, non-blocking)
            dev_id = entry["device_id"]
            # Support entry-scoped unique_ids (preferred) and legacy forms as fallback
            entry_id = self._entry_id()
            uid_candidates: List[str] = []
            if entry_id:
                uid_candidates.append(f"{entry_id}:{dev_id}")
                uid_candidates.append(f"{DOMAIN}_{entry_id}_{dev_id}")
            uid_candidates.append(f"{DOMAIN}_{dev_id}")  # legacy

            entity_id = None
            for uid in uid_candidates:
                entity_id = ent_reg.async_get_entity_id("device_tracker", DOMAIN, uid)
                if entity_id:
                    break
            if not entity_id:
                _LOGGER.debug(
                    "No entity registry entry for device '%s'; checked unique_id formats %s. "
                    "Skipping state/history fallback because tracker cache is unavailable.",
                    entry["name"],
                    uid_candidates,
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
                    entry = _sanitize_decoder_row(entry)
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

            entry = _sanitize_decoder_row(entry)
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
                self._debounced_save_stats(),
                name=f"{DOMAIN}.save_stats_debounced",
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
                "Incremented %s from %s to %s",
                stat_name,
                before,
                self.stats[stat_name],
            )
            self._schedule_stats_persist()
        else:
            _LOGGER.warning(
                "Tried to increment unknown stat '%s'; available=%s",
                stat_name,
                list(self.stats.keys()),
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
        slot.update({k: v for k, v in data.items() if k in ("latitude", "longitude", "accuracy")})
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

        # Sanitize invariants + enrich fields before gating
        slot = _sanitize_decoder_row(slot)

        # Increment crowdsourced stats for push/manual commits as well
        if slot.get("source_label") == "crowdsourced":
            self.increment_stat("crowd_sourced_updates")

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
        from math import atan2, cos, radians, sin, sqrt

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

        This function acts as the primary gate to prevent redundant or low-value
        updates from churning the state machine and recorder.

        Criteria (in order of evaluation):
          1. No previous data exists for the device.
          2. The `last_seen` timestamp of the new data is newer than the existing one.
          3. If timestamps are identical, an update is significant if:
             a) The position has moved more than `self._movement_threshold` meters, OR
             b) The accuracy has improved by at least 20% (smaller radius is better), OR
             c) The qualitative data has changed (source, status, or semantic name).

        Notes:
          * This replaces a simple "same last_seen == duplicate" heuristic with a more
            intelligent assessment of data quality.
          * Stale-guard: If a normalized `last_seen` is **older** than the existing one,
            we drop it and reuse `invalid_ts_drop_count` to track this case to avoid
            adding a new counter (see diagnostics & stats entity mapping).
          * It is assumed that a staleness guard has already discarded updates where
            `new_seen` is in the far past (< 2000) or far future (+10 min).
        """
        existing = self._device_location_data.get(device_id)
        if not existing:
            return True

        n_seen_norm = _normalize_epoch_seconds(new_data.get("last_seen"))
        # last_seen can be missing, do not drop, qualitative checks will still run
        if n_seen_norm is not None:
            if n_seen_norm < 946684800.0:  # < 2000-01-01
                self.increment_stat("invalid_ts_drop_count")
                return False
            if n_seen_norm > time.time() + 600.0:  # > +10min
                self.increment_stat("future_ts_drop_count")
                return False

        e_seen_norm = _normalize_epoch_seconds(existing.get("last_seen"))

        # Stale-guard: reuse invalid_ts_drop_count for "older than existing"
        if n_seen_norm is not None and e_seen_norm is not None and n_seen_norm < e_seen_norm:
            self.increment_stat("invalid_ts_drop_count")
            return False

        if n_seen_norm is not None and e_seen_norm is not None and n_seen_norm > e_seen_norm:
            return True

        # Same timestamp? Check for spatial delta and accuracy improvement.
        if n_seen_norm is not None and e_seen_norm is not None and n_seen_norm == e_seen_norm:
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
                    if float(n_acc) < float(e_acc) * 0.8:  # >=20% better accuracy
                        return True
                except Exception:
                    pass

        # Source or qualitative changes can still be valuable.
        if new_data.get("is_own_report") != existing.get("is_own_report"):
            return True
        if new_data.get("status") != existing.get("status"):
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
        """Return True if the given device_id is present (TTL-smoothed).

        Presence is determined by the last time the device appeared in the full list
        and a time-to-live (`_presence_ttl_s`). This avoids availability flips on
        transient empty lists.
        """
        ts = self._present_last_seen.get(device_id, 0.0)
        if not ts:
            return False
        return (time.monotonic() - float(ts)) <= float(self._presence_ttl_s)

    def get_absent_device_ids(self) -> List[str]:
        """Return ids known by name/cache that are **expired** under the presence TTL.

        Useful for diagnostics. This does not imply automatic removal.
        """
        now_mono = time.monotonic()

        def expired(dev_id: str) -> bool:
            ts = self._present_last_seen.get(dev_id, 0.0)
            return (not ts) or ((now_mono - float(ts)) > float(self._presence_ttl_s))

        known = set(self._device_names) | set(self._device_location_data)
        return sorted([d for d in known if expired(d)])

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
        self._present_last_seen.pop(device_id, None)
        # Push a minimal update so listeners can refresh availability quickly
        self.async_set_updated_data(self.data)

    # ---------------------------- Push updates ------------------------------
    def push_updated(
        self,
        device_ids: Optional[List[str]] = None,
        *,
        reset_baseline: bool = True,
    ) -> None:
        """Publish a fresh snapshot to listeners after push (FCM) cache updates.

        Thread-safe: may be called from any thread. This method ensures all state
        publishing happens on the HA event loop.

        This **does not** trigger a poll. It:
        - Immediately pushes cache state to entities via `async_set_updated_data()`.
        - Optionally resets the internal poll baseline to 'now' to prevent an immediate
          re-poll when push-driven updates arrive (`reset_baseline=True` by default).
        - Optionally limits the snapshot to `device_ids`; otherwise includes all known devices.

        Args:
            device_ids: An optional list of device IDs to include in the update.
            reset_baseline: If True (default), reset the scheduler baseline to now.
        """
        if not self._is_on_hass_loop():
            self._run_on_hass_loop(self.push_updated, device_ids, reset_baseline=reset_baseline)
            return

        wall_now = time.time()
        self._set_fcm_status(FcmStatus.CONNECTED)
        if reset_baseline:
            self._last_poll_mono = time.monotonic()  # optional: reset poll timer

        # Choose device ids for the snapshot
        if device_ids:
            ids = device_ids
        else:
            # union of all known names and cached locations
            ids = list({*self._device_names.keys(), *self._device_location_data.keys()})

        # Apply ignore filter first to avoid touching presence for ignored devices.
        ignored = self._get_ignored_set()
        ids = [d for d in ids if d not in ignored]

        # Touch presence timestamps for pushed devices (keeps presence stable)
        now_mono = time.monotonic()
        for dev_id in ids:
            self._present_last_seen[dev_id] = now_mono

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
            _LOGGER.debug("Push readiness check exception: %s (defaulting optimistic True)", err)
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
        self._set_fcm_status(
            FcmStatus.DEGRADED,
            reason=f"Push transport recovering from error (cooldown {cooldown_s}s)",
        )

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
        is_known = device_id in self._device_names or device_id in self._device_location_data
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
                _LOGGER.warning("Ignoring invalid location_poll_interval=%r", location_poll_interval)

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
                _LOGGER.warning("Ignoring invalid min_accuracy_threshold=%r", min_accuracy_threshold)

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
            - Validate coordinates/accuracy and apply significance gating.
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

            # Success path: clear any auth error state
            self._set_auth_state(failed=False)

            if not location_data:
                return {}

            # --- Parity with polling path: Google Home semantic spam filter --------
            # Consume coordinate substitution from the filter when needed.
            semantic_name = location_data.get("semantic_name")
            if semantic_name and hasattr(self, "google_home_filter"):
                try:
                    (should_filter, replacement_attrs) = self.google_home_filter.should_filter_detection(
                        device_id, semantic_name
                    )
                except Exception as gf_err:
                    _LOGGER.debug("Google Home filter error for %s: %s", name, gf_err)
                else:
                    if should_filter:
                        _LOGGER.debug(
                            "Filtering out Google Home spam detection for %s (manual locate)",
                            name,
                        )
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
            if (
                location_data.get("latitude") is None
                or location_data.get("longitude") is None
            ) and location_data.get("semantic_name"):
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
                    "Dropping low-quality fix for %s (accuracy=%sm > %sm)",
                    name,
                    acc,
                    self._min_accuracy_threshold,
                )
                self.increment_stat("low_quality_dropped")
                return {}

            # Prepare a copy for gating/cooldown application
            slot = dict(location_data)
            slot.setdefault("last_updated", time.time())

            # Apply type-aware cooldowns based on internal hint (if any), then strip it.
            self._apply_report_type_cooldown(device_id, slot.get("_report_hint"))
            slot.pop("_report_hint", None)

            # Sanitize invariants + derive labels before significance gating
            slot = _sanitize_decoder_row(slot)

            # Increment crowdsourced stats for manual locate as well (if applicable)
            if slot.get("source_label") == "crowdsourced":
                self.increment_stat("crowd_sourced_updates")

            # Significance gate also for manual locate to avoid churn.
            if not self._is_significant_update(device_id, slot):
                self.increment_stat("non_significant_dropped")
                return {}

            # Commit to cache (update_device_cache ensures last_updated and stats)
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

            # Touch presence for the device (a fresh interaction implies it exists)
            self._present_last_seen[device_id] = now_mono

            self.push_updated([device_id])
            return location_data or {}
        except ConfigEntryAuthFailed as auth_exc:
            # Mark error and request a refresh; no need to re-raise here for manual action.
            self._set_auth_state(failed=True, reason=f"Auth failed during manual locate: {auth_exc}")
            try:
                self.async_request_refresh()
            except Exception:
                pass
            return {}
        except Exception as err:
            short_err = self._short_error_message(err)
            _LOGGER.error("Manual locate for %s failed: %s", name, short_err)
            self.note_error(err, where="async_locate_device", device=name)
            raise HomeAssistantError(
                f"Manual locate for '{name}' failed due to an unexpected error. "
                "Check logs for details."
            ) from err
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
            _LOGGER.debug("Suppressing play_sound call for %s: capability/push not ready", device_id)
            return False
        try:
            ok = await self.api.async_play_sound(device_id)
            if not ok:
                self._note_push_transport_problem()
            # Success implies credentials worked
            self._set_auth_state(failed=False)
            return bool(ok)
        except ConfigEntryAuthFailed as auth_exc:
            self._set_auth_state(failed=True, reason=f"Auth failed during play_sound: {auth_exc}")
            try:
                self.async_request_refresh()
            except Exception:
                pass
            return False
        except Exception as err:
            _LOGGER.debug("async_play_sound raised for %s: %s; entering cooldown", device_id, err)
            self.note_error(err, where="async_play_sound", device=device_id)
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
            _LOGGER.debug("Suppressing stop_sound call for %s: push not ready", device_id)
            return False
        try:
            ok = await self.api.async_stop_sound(device_id)
            if not ok:
                self._note_push_transport_problem()
            # Success implies credentials worked
            self._set_auth_state(failed=False)
            return bool(ok)
        except ConfigEntryAuthFailed as auth_exc:
            self._set_auth_state(failed=True, reason=f"Auth failed during stop_sound: {auth_exc}")
            try:
                self.async_request_refresh()
            except Exception:
                pass
            return False
        except Exception as err:
            _LOGGER.debug("async_stop_sound raised for %s: %s; entering cooldown", device_id, err)
            self.note_error(err, where="async_stop_sound", device=device_id)
            self._note_push_transport_problem()
            return False
