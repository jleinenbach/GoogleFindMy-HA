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
# custom_components/googlefindmy/coordinator.py
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import warnings
from collections import deque
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    from homeassistant.core import Event

    from . import ConfigEntrySubentryDefinition, ConfigEntrySubEntryManager
    from .google_home_filter import GoogleHomeFilter as GoogleHomeFilterProtocol
else:  # pragma: no cover - typing fallback for runtime imports
    Event = Any
    GoogleHomeFilterProtocol = Any

from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.components.recorder import (
    get_instance as get_recorder,
)
from homeassistant.components.recorder import (
    history as recorder_history,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import (
    issue_registry as ir,
)  # Repairs: modern "needs action" UI
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import EVENT_DEVICE_REGISTRY_UPDATED
from homeassistant.helpers.entity_registry import RegistryEntry as EntityRegistryEntry
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import UpdateFailed

# IMPORTANT: make Common_pb2 import **mandatory** (integration packaging must include it).
# This avoids silent type/name drift and keeps source labels stable.
from custom_components.googlefindmy.ProtoDecoders import Common_pb2

from .api import GoogleFindMyAPI
from .const import (
    CACHE_KEY_CONTRIBUTOR_MODE,
    CACHE_KEY_LAST_MODE_SWITCH,
    # Credential meta for Repairs placeholders
    CONF_GOOGLE_EMAIL,
    CONTRIBUTOR_MODE_HIGH_TRAFFIC,
    CONTRIBUTOR_MODE_IN_ALL_AREAS,
    DEFAULT_CONTRIBUTOR_MODE,
    # Core / options
    DEFAULT_MIN_POLL_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    # Required symbols provided by const.py (5.1-A)
    EVENT_AUTH_ERROR,
    EVENT_AUTH_OK,
    INTEGRATION_VERSION,
    ISSUE_AUTH_EXPIRED_KEY,
    LOCATION_REQUEST_TIMEOUT_S,
    MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S,
    OPT_IGNORED_DEVICES,
    SERVICE_DEVICE_MANUFACTURER,
    # Integration "service device" metadata
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_TRANSLATION_KEY,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    UPDATE_INTERVAL,
    # Helpers
    coerce_ignored_mapping,
    issue_id_for,
    service_device_identifier,
)
from .ha_typing import DataUpdateCoordinator, callback

_LOGGER = logging.getLogger(__name__)


_DEFAULT_SUBENTRY_FEATURES: tuple[str, ...] = (
    "binary_sensor",
    "button",
    "device_tracker",
    "sensor",
)

_SERVICE_SUBENTRY_FEATURES: tuple[str, ...] = tuple(
    sorted(dict.fromkeys(SERVICE_FEATURE_PLATFORMS))
)
_TRACKER_SUBENTRY_FEATURES: tuple[str, ...] = tuple(
    sorted(dict.fromkeys(TRACKER_FEATURE_PLATFORMS))
)


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
_COOLDOWN_MIN_HIGH_TRAFFIC_S = 5 * 60  # 5 minutes

# Guardrails for owner-driven locate cooldown
_COOLDOWN_OWNER_MIN_S = 60  # at least 1 minute
_COOLDOWN_OWNER_MAX_S = 15 * 60  # at most 15 minutes

# Maximum delay before falling back to polling even when push is unavailable.
_FCM_FALLBACK_POLL_AFTER_S = 5 * 60

# Altitude adjustments smaller than 1 m are considered noise for significance checks.
_ALTITUDE_SIGNIFICANT_DELTA_M = 1.0


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
    warnings: dict[str, dict[str, Any]] = field(default_factory=dict)
    errors: dict[str, dict[str, Any]] = field(default_factory=dict)

    def _add(
        self, bucket: dict[str, dict[str, Any]], key: str, payload: dict[str, Any]
    ) -> None:
        """Add payload once (dedup by key); bounded to max_items."""
        if key in bucket:
            return
        if len(bucket) >= self.max_items:
            return
        bucket[key] = payload

    def add_warning(self, code: str, context: dict[str, Any]) -> None:
        """Record a warning with a semantic code and redacted context."""
        key = f"{code}:{context.get('device_id', '?')}"
        self._add(self.warnings, key, context)

    def add_error(self, code: str, context: dict[str, Any]) -> None:
        """Record an error with a semantic code and redacted context."""
        key = f"{code}:{context.get('device_id', '?')}:{context.get('arg', '')}"
        self._add(self.errors, key, context)

    def to_dict(self) -> dict[str, Any]:
        """Return a minimal, redacted, diagnostics-friendly dict."""
        return {
            "summary": {"warnings": len(self.warnings), "errors": len(self.errors)},
            "warnings": list(self.warnings.values()),
            "errors": list(self.errors.values()),
        }


# --- Subentry metadata ---------------------------------------------------------
@dataclass(slots=True, frozen=True)
class SubentryMetadata:
    """Lightweight view of a config-entry subentry relevant to platforms."""

    key: str
    config_subentry_id: str | None
    features: tuple[str, ...]
    title: str | None
    poll_intervals: Mapping[str, int]
    filters: Mapping[str, Any]
    feature_flags: Mapping[str, Any]
    visible_device_ids: tuple[str, ...]
    enabled_device_ids: tuple[str, ...]

    def stable_identifier(self) -> str:
        """Return the identifier to use when namespacing entities."""

        return self.config_subentry_id or self.key

    @property
    def subentry_id(self) -> str | None:
        """Backwards-compatible alias for the config subentry identifier."""

        return self.config_subentry_id


def _sanitize_subentry_identifier(candidate: Any) -> str | None:
    """Return a normalized subentry identifier or ``None`` when fabricated."""

    if not isinstance(candidate, str):
        return None

    normalized = candidate.strip()
    if not normalized:
        return None

    return normalized


# --- Epoch normalization (ms→s tolerant) -----------------------------------
def _normalize_epoch_seconds(ts: Any) -> float | None:
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


# NOTE: keep helper public for reuse in entities/system health snapshots.
def format_epoch_utc(value: Any) -> str | None:
    """Return an ISO 8601 UTC timestamp for epoch values (seconds or ms)."""

    ts = _normalize_epoch_seconds(value)
    if ts is None:
        return None
    try:
        dt = datetime.fromtimestamp(ts, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return dt.isoformat().replace("+00:00", "Z")


# --- Decoder-row Normalization & Attribute Helpers -------------------------
_MISSING = object()


def _row_source_label(row: dict[str, Any]) -> tuple[int, str]:
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


def _parse_last_seen_timestamp(value: Any) -> float | None:
    """Parse a last_seen candidate into epoch seconds."""

    ts = _normalize_epoch_seconds(value)
    if ts is not None:
        return ts
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _resolve_last_seen_from_attributes(
    attributes: Mapping[str, Any] | None, fallback: float | None
) -> float | None:
    """Prefer attribute-derived timestamps and fall back to provided default."""

    if not attributes:
        return fallback

    candidate: Any = attributes.get("last_seen")
    if candidate is None:
        candidate = attributes.get("last_seen_utc")

    ts = _parse_last_seen_timestamp(candidate)
    if ts is not None:
        return ts
    return fallback


def _sanitize_decoder_row(row: dict[str, Any]) -> dict[str, Any]:
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
    r["last_seen_utc"] = format_epoch_utc(ts)

    r["source_label"] = label
    r["source_rank"] = rank
    return r


def _as_ha_attributes(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Create a curated, stable attribute set for HA entities (recorder-friendly)."""
    if not row:
        return None
    r = _sanitize_decoder_row(row)

    def _cf(value: Any) -> float | None:
        try:
            candidate = float(value)
        except (TypeError, ValueError):
            return None
        return candidate if math.isfinite(candidate) else None

    lat = _cf(r.get("latitude"))
    lon = _cf(r.get("longitude"))
    acc = _cf(r.get("accuracy"))
    alt = _cf(r.get("altitude"))

    last_seen_iso = format_epoch_utc(r.get("last_seen"))
    last_seen_utc = r.get("last_seen_utc") or last_seen_iso

    out: dict[str, Any] = {
        "device_name": r.get("name"),
        "device_id": r.get("device_id") or r.get("id"),
        "status": r.get("status"),
        "semantic_name": r.get("semantic_name"),
        "battery_level": r.get("battery_level"),
        "last_seen": last_seen_iso,
        "last_seen_utc": last_seen_utc,
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
) -> dict[str, Any] | None:
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

        last_seen_ts = _resolve_last_seen_from_attributes(
            attrs, last_state.last_updated.timestamp()
        )
        return {
            "latitude": lat,
            "longitude": lon,
            "accuracy": attrs.get("gps_accuracy"),
            "last_seen": last_seen_ts,
            "status": "Using historical data",
        }
    except Exception as err:
        _LOGGER.debug("History lookup failed for %s: %s", entity_id, err)
        return None


@dataclass(frozen=True)
class StatusSnapshot:
    """Lightweight status descriptor shared with diagnostic entities/tests."""

    state: str
    reason: str | None = None
    changed_at: float | None = None


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


class GoogleFindMyCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
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
        contributor_mode: str = DEFAULT_CONTRIBUTOR_MODE,
        contributor_mode_switch_epoch: int | None = None,
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
            contributor_mode: Preferred Nova contributor mode ("high_traffic" or "in_all_areas").
            contributor_mode_switch_epoch: Epoch timestamp when the contributor mode last changed.
        """
        self.hass = hass
        self._cache = cache
        self.config_entry: ConfigEntry | None = getattr(self, "config_entry", None)

        # Try to ensure entry-scoped namespace on the cache early when possible.
        # This will be finalized in async_setup() once the ConfigEntry is bound.
        try:
            if (
                not getattr(self._cache, "entry_id", None)
                and self.config_entry is not None
            ):
                setattr(self._cache, "entry_id", self.config_entry.entry_id)
        except Exception:
            pass

        # Get the singleton aiohttp.ClientSession from Home Assistant and reuse it.
        self._session = async_get_clientsession(hass)

        normalized_mode = self._sanitize_contributor_mode(contributor_mode)
        if contributor_mode_switch_epoch is None or contributor_mode_switch_epoch <= 0:
            contributor_mode_switch_epoch = int(time.time())
        self._contributor_mode = normalized_mode
        self._contributor_mode_switch_epoch = int(contributor_mode_switch_epoch)

        self.api = GoogleFindMyAPI(
            cache=self._cache,
            session=self._session,
            contributor_mode=self._contributor_mode,
            contributor_mode_switch_epoch=self._contributor_mode_switch_epoch,
        )

        # Configuration (user options; updated via update_settings())
        self.location_poll_interval = int(location_poll_interval)
        self.device_poll_delay = int(device_poll_delay)
        self.min_poll_interval = int(
            min_poll_interval
        )  # hard lower bound between cycles
        self._min_accuracy_threshold = int(
            min_accuracy_threshold
        )  # quality filter (meters)
        self._movement_threshold = int(
            movement_threshold
        )  # meters; used by significance gate
        self.allow_history_fallback = bool(allow_history_fallback)

        # Initialize diagnostics buffer and one-shot warning guard for malformed IDs.
        self._diag = DiagnosticsBuffer(max_items=200)
        self._warned_bad_identifier_devices: set[str] = set()

        # Internal caches & bookkeeping
        self._device_location_data: dict[
            str, dict[str, Any]
        ] = {}  # device_id -> location dict
        self._device_names: dict[str, str] = {}  # device_id -> human name
        self._device_caps: dict[
            str, dict[str, Any]
        ] = {}  # device_id -> caps (e.g., {"can_ring": True})
        self._present_device_ids: set[str] = (
            set()
        )  # diagnostics-only set from latest non-empty list

        # Flag to separate initial discovery from later runtime additions.
        # After the first successful non-empty device list is processed, this becomes True.
        self._initial_discovery_done: bool = False

        # Presence smoothing (TTL):
        # - Per-device "last seen in full list" timestamp (monotonic)
        # - Cold-start marker: timestamp of last non-empty list (monotonic)
        # - Presence TTL in seconds (derived from poll interval, min 120s)
        self._present_last_seen: dict[str, float] = {}
        self._last_nonempty_wall: float = 0.0
        self._presence_ttl_s: int = 120

        # Minimal hardening state (empty-list quorum)
        self._last_device_list: list[dict[str, Any]] = []
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
        self._push_ready_memo: bool | None = None
        self._push_cooldown_until: float = 0.0

        # Manual locate gating (UX + server protection)
        self._locate_inflight: set[str] = set()  # device_id -> in-flight flag
        self._locate_cooldown_until: dict[str, float] = {}  # device_id -> mono deadline

        # Per-device poll cooldowns after owner/crowdsourced reports.
        self._device_poll_cooldown_until: dict[str, float] = {}

        # DR-driven poll targeting
        self._enabled_poll_device_ids: set[str] = set()
        self._devices_with_entry: set[str] = set()
        self._dr_unsub: Callable[[], None] | None = None
        # Subentry awareness (feature groups / platform scoping)
        self._subentry_metadata: dict[str, SubentryMetadata] = {}
        self._subentry_snapshots: dict[str, tuple[dict[str, Any], ...]] = {}
        self._feature_to_subentry: dict[str, str] = {}
        self._default_subentry_key_value: str = "core_tracking"
        self._subentry_manager: ConfigEntrySubEntryManager | None = None
        self._pending_subentry_repair: asyncio.Task[None] | None = None

        # Statistics (extend as needed)
        self.stats: dict[str, int] = {
            "background_updates": 0,  # FCM/push-driven updates + manual commits
            "polled_updates": 0,  # sequential poll-driven updates
            "crowd_sourced_updates": 0,  # number of crowdsourced updates observed
            "history_fallback_used": 0,  # times we had to fall back to Recorder history
            "timeouts": 0,  # request timeouts
            "invalid_coords": 0,  # coordinate validation failures
            "low_quality_dropped": 0,  # dropped due to accuracy worse than threshold
            "invalid_ts_drop_count": 0,  # invalid or stale (< existing) timestamps
            "future_ts_drop_count": 0,  # timestamps too far in the future
            "non_significant_dropped": 0,  # drops by significance gate
        }
        _LOGGER.debug("Initialized stats: %s", self.stats)

        # Granular status tracking (API polling vs. push transport)
        self._api_status_state: str = ApiStatus.UNKNOWN
        self._api_status_reason: str | None = None
        self._api_status_changed_at: float | None = None
        self._fcm_status_state: str = FcmStatus.UNKNOWN
        self._fcm_status_reason: str | None = None
        self._fcm_status_changed_at: float | None = None

        # Performance metrics (timestamps, durations) & recent errors (bounded)
        self.performance_metrics: dict[str, float] = {}
        self.recent_errors: deque[tuple[float, str, str]] = deque(maxlen=5)

        # Debounced stats persistence (avoid flushing on every increment)
        self._stats_save_task: asyncio.Task[None] | None = None
        self._stats_debounce_seconds: float = 5.0

        # Load persistent statistics asynchronously (name the task for better debugging)
        self.hass.async_create_task(
            self._async_load_stats(), name=f"{DOMAIN}.load_stats"
        )

        # Short-retry scheduling handle (coalesced)
        self._short_retry_cancel: Callable[[], None] | None = None

        # NEW: Authentication/repairs state
        self._auth_error_active: bool = False
        self._auth_error_since: float = 0.0
        self._auth_error_message: str | None = None

        # Reload guard: defer core subentry repairs once after reload-driven attach
        self._skip_repair_during_reload_refresh: bool = False
        self._reload_repair_skip_pending_release: bool = False

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )

    async def async_config_entry_first_refresh(self) -> None:
        """Run the first refresh, tolerating coordinators without the helper."""

        try:
            parent_first_refresh = super().async_config_entry_first_refresh
        except AttributeError:  # pragma: no cover - compatibility with older cores
            parent_first_refresh = None

        if parent_first_refresh is not None:
            await parent_first_refresh()
            return

        _LOGGER.debug(
            "[%s] Falling back to async_refresh for initial coordinator sync",
            self._entry_id() or "unknown",
        )
        await self.async_refresh()

    @property
    def cache(self) -> CacheProtocol:
        """Return the entry-scoped token cache backing this coordinator."""

        return self._cache

    def attach_subentry_manager(
        self, manager: ConfigEntrySubEntryManager, *, is_reload: bool = False
    ) -> None:
        """Attach the config entry subentry manager to the coordinator."""

        self._subentry_manager = manager
        self._skip_repair_during_reload_refresh = bool(is_reload)
        self._reload_repair_skip_pending_release = False
        if manager is None:
            return

        try:
            self._refresh_subentry_index(
                skip_manager_update=True, skip_repair=is_reload
            )
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "Initial subentry refresh failed during setup: %s",
                err,
            )
            return

        service_meta = self._subentry_metadata.get(SERVICE_SUBENTRY_KEY)
        if service_meta is not None and service_meta.config_subentry_id:
            try:
                self._ensure_service_device_exists()
            except Exception as err:  # noqa: BLE001 - defensive guard
                _LOGGER.debug(
                    "Service device ensure skipped during setup: %s",
                    err,
                )

    def _default_subentry_key(self) -> str:
        """Return the default subentry key used when no explicit mapping exists."""

        return self._default_subentry_key_value or "core_tracking"

    def _build_core_subentry_definitions(
        self,
    ) -> list[ConfigEntrySubentryDefinition]:
        """Return definitions for the core tracker/service subentries."""

        entry = self.config_entry or getattr(self, "entry", None)
        entry_id = getattr(entry, "entry_id", None) if entry is not None else None
        if entry is None or not isinstance(entry_id, str) or not entry_id:
            _LOGGER.debug(
                "Skipping core subentry repair: config entry unavailable (entry=%s)",
                entry,
            )
            return []

        try:
            from . import ConfigEntrySubentryDefinition  # local import to avoid cycles
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "Skipping core subentry repair: definition factory import failed (%s)",
                err,
            )
            return []

        runtime_data = getattr(entry, "runtime_data", None)
        fcm_receiver = getattr(runtime_data, "fcm_receiver", None)
        google_home_filter = getattr(runtime_data, "google_home_filter", None)

        fcm_push_enabled = fcm_receiver is not None
        has_google_home_filter = google_home_filter is not None
        entry_title = getattr(entry, "title", None) or "Google Find My"

        tracker_features = list(_TRACKER_SUBENTRY_FEATURES or TRACKER_FEATURE_PLATFORMS)
        service_features = list(_SERVICE_SUBENTRY_FEATURES or SERVICE_FEATURE_PLATFORMS)

        tracker_definition = ConfigEntrySubentryDefinition(
            key=TRACKER_SUBENTRY_KEY,
            title="Google Find My devices",
            data={
                "features": tracker_features,
                "fcm_push_enabled": fcm_push_enabled,
                "has_google_home_filter": has_google_home_filter,
                "entry_title": entry_title,
            },
            subentry_type=SUBENTRY_TYPE_TRACKER,
            unique_id=f"{entry_id}-{TRACKER_SUBENTRY_KEY}",
        )
        service_definition = ConfigEntrySubentryDefinition(
            key=SERVICE_SUBENTRY_KEY,
            title=entry_title,
            data={
                "features": service_features,
                "fcm_push_enabled": fcm_push_enabled,
                "has_google_home_filter": has_google_home_filter,
                "entry_title": entry_title,
            },
            subentry_type=SUBENTRY_TYPE_SERVICE,
            unique_id=f"{entry_id}-{SERVICE_SUBENTRY_KEY}",
        )

        return [tracker_definition, service_definition]

    def _schedule_core_subentry_repair(self, missing_keys: set[str]) -> None:
        """Schedule a repair task to recreate missing core subentries."""

        if not missing_keys:
            return

        manager = self._subentry_manager
        hass = getattr(self, "hass", None)
        if manager is None or hass is None:
            return

        pending = self._pending_subentry_repair
        if pending is not None and not pending.done():
            _LOGGER.debug(
                "Core subentry repair already running; deferring additional request (%s)",
                sorted(missing_keys),
            )
            return

        entry_id = self._entry_id() or "unknown"

        async def _repair() -> None:
            try:
                definitions = self._build_core_subentry_definitions()
                if not definitions:
                    _LOGGER.debug(
                        "Core subentry repair skipped for %s: definitions unavailable",
                        entry_id,
                    )
                    return

                _LOGGER.debug(
                    "Repairing missing subentries %s for entry %s",
                    sorted(missing_keys),
                    entry_id,
                )
                await manager.async_sync(definitions)
            except asyncio.CancelledError:  # pragma: no cover - task cancelled
                raise
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.warning(
                    "Core subentry repair failed for entry %s: %s",
                    entry_id,
                    err,
                )
                return
            finally:
                self._pending_subentry_repair = None

            self._ensure_service_device_exists()
            self._refresh_subentry_index()
            _LOGGER.debug(
                "Core subentry repair completed for entry %s", entry_id
            )

        task_name = f"{DOMAIN}.repair_core_subentries"
        create_task = getattr(hass, "async_create_task", None)
        if callable(create_task):
            task = create_task(_repair(), name=task_name)
        else:  # pragma: no cover - fallback for legacy stubs
            task = asyncio.create_task(_repair(), name=task_name)
        self._pending_subentry_repair = task

    def _refresh_subentry_index(
        self,
        visible_devices: Sequence[Mapping[str, Any]] | None = None,
        *,
        skip_manager_update: bool = False,
        skip_repair: bool = False,
    ) -> None:
        """Refresh internal subentry metadata caches."""

        reload_skip_active = bool(
            getattr(self, "_skip_repair_during_reload_refresh", False)
        )
        reload_skip_consumed = False
        if reload_skip_active and not skip_repair:
            skip_repair = True
            reload_skip_consumed = True
            self._reload_repair_skip_pending_release = True

        entry = self.config_entry

        entry_id = getattr(entry, "entry_id", None)
        entry_service_subentry_id = (
            _sanitize_subentry_identifier(getattr(entry, "service_subentry_id", None))
            if entry is not None
            else None
        )
        entry_tracker_subentry_id = (
            _sanitize_subentry_identifier(getattr(entry, "tracker_subentry_id", None))
            if entry is not None
            else None
        )

        raw_entries: list[tuple[str, str | None, dict[str, Any], str | None]] = []
        core_group_keys_present: set[str] = set()
        if entry and getattr(entry, "subentries", None):
            for subentry in entry.subentries.values():
                data = dict(getattr(subentry, "data", {}) or {})
                group_key = str(
                    data.get("group_key")
                    or getattr(subentry, "subentry_id", None)
                    or "core_tracking"
                )
                if group_key in (SERVICE_SUBENTRY_KEY, TRACKER_SUBENTRY_KEY):
                    core_group_keys_present.add(group_key)
                identifier = _sanitize_subentry_identifier(
                    getattr(subentry, "subentry_id", None)
                )
                if (
                    identifier is not None
                    and identifier.endswith("-provisional")
                ):
                    if (
                        group_key == SERVICE_SUBENTRY_KEY
                        and identifier != entry_service_subentry_id
                    ) or (
                        group_key == TRACKER_SUBENTRY_KEY
                        and identifier != entry_tracker_subentry_id
                    ):
                        identifier = None
                raw_entries.append(
                    (
                        group_key,
                        identifier,
                        data,
                        getattr(subentry, "title", None),
                    )
                )

        if entry is not None:
            missing_core_keys = {
                SERVICE_SUBENTRY_KEY,
                TRACKER_SUBENTRY_KEY,
            } - core_group_keys_present
        else:
            missing_core_keys = set()

        if not raw_entries:
            raw_entries.append(
                (
                    "core_tracking",
                    None,
                    {
                        "features": _DEFAULT_SUBENTRY_FEATURES,
                        "feature_flags": {},
                    },
                    getattr(entry, "title", None),
                )
            )

        ignored = self._get_ignored_set()
        device_index: dict[str, dict[str, Any]] = {}

        device_registry: dr.DeviceRegistry | None = None
        registry_lookup: Callable[[str], dr.DeviceEntry | None] | None = None
        hass_obj = getattr(self, "hass", None)
        if hass_obj is not None:
            try:
                device_registry = dr.async_get(hass_obj)
            except Exception:  # defensive: registry helpers may not be patched in tests
                device_registry = None
            else:
                candidate_lookup = getattr(device_registry, "async_get", None)
                if callable(candidate_lookup):
                    registry_lookup = candidate_lookup

        canonical_to_registry_id: dict[str, str] = {}
        registry_to_canonical: dict[str, str] = {}
        if device_registry is not None:
            candidate_entries: list[Any] = []
            raw_devices = getattr(device_registry, "devices", None)
            if isinstance(raw_devices, Mapping):
                candidate_entries.extend(raw_devices.values())
            else:
                registry_entries = getattr(device_registry, "_entries", None)
                if isinstance(registry_entries, Mapping):
                    candidate_entries.extend(registry_entries.values())

            if not candidate_entries:
                entry_id = self._entry_id()
                fetch_entries = getattr(dr, "async_entries_for_config_entry", None)
                if callable(fetch_entries) and entry_id:
                    try:
                        candidate_entries.extend(fetch_entries(device_registry, entry_id))
                    except Exception:  # defensive: stub mismatches / legacy HA versions
                        candidate_entries = []

            for device_entry in candidate_entries:
                try:
                    canonical = self._extract_our_identifier(device_entry)
                except Exception:  # defensive: tolerate stub deviations
                    canonical = None
                if not canonical:
                    continue
                device_id_attr = getattr(device_entry, "id", None)
                if isinstance(device_id_attr, str) and device_id_attr:
                    canonical_to_registry_id.setdefault(canonical, device_id_attr)
                    registry_to_canonical.setdefault(device_id_attr, canonical)

        def _register_device(candidate: Mapping[str, Any]) -> None:
            dev_id = candidate.get("id")
            if not isinstance(dev_id, str) or not dev_id:
                fallback_id = candidate.get("device_id")
                if isinstance(fallback_id, str) and fallback_id:
                    dev_id = fallback_id
                else:
                    return
            if dev_id in ignored:
                return
            name = (
                candidate.get("name")
                if isinstance(candidate.get("name"), str)
                else None
            )
            device_index.setdefault(dev_id, {"id": dev_id, "name": name})

        if visible_devices is not None:
            for dev in visible_devices:
                if isinstance(dev, Mapping):
                    _register_device(dev)
        else:
            for dev in self.data or []:
                if isinstance(dev, Mapping):
                    _register_device(dev)

        previous_visible: dict[str, tuple[str, ...]] = {
            key: meta.visible_device_ids
            for key, meta in self._subentry_metadata.items()
        }

        metadata: dict[str, SubentryMetadata] = {}
        feature_map: dict[str, str] = {}
        default_key: str | None = None
        manager_visible: dict[str, tuple[str, ...]] = {}

        def _current_poll_intervals() -> Mapping[str, int]:
            return MappingProxyType(
                {
                    "location": int(self.location_poll_interval),
                    "minimum": int(self.min_poll_interval),
                    "device": int(self.device_poll_delay),
                    "min_accuracy": int(self._min_accuracy_threshold),
                    "movement": int(self._movement_threshold),
                }
            )

        def _current_filters() -> Mapping[str, Any]:
            return MappingProxyType(
                {
                    "ignored_device_ids": tuple(sorted(ignored)),
                    "allow_history_fallback": bool(self.allow_history_fallback),
                }
            )

        for group_key, subentry_id, data, title in raw_entries:
            raw_features = data.get("features")
            if isinstance(raw_features, (list, tuple, set)):
                normalized_features = tuple(
                    sorted(
                        {
                            str(feature)
                            for feature in raw_features
                            if isinstance(feature, str)
                        }
                    )
                )
            else:
                normalized_features = _DEFAULT_SUBENTRY_FEATURES

            if group_key == SERVICE_SUBENTRY_KEY:
                features = _SERVICE_SUBENTRY_FEATURES or normalized_features
            elif group_key == TRACKER_SUBENTRY_KEY:
                features = _TRACKER_SUBENTRY_FEATURES or normalized_features
            else:
                features = normalized_features or _DEFAULT_SUBENTRY_FEATURES

            raw_flags = data.get("feature_flags")
            feature_flags: dict[str, Any]
            if isinstance(raw_flags, Mapping):
                feature_flags = {str(key): raw_flags[key] for key in raw_flags}
            else:
                feature_flags = dict[str, Any]()

            raw_allowed = data.get("visible_device_ids")
            normalized_allowed: set[str] | None = None
            if isinstance(raw_allowed, (list, tuple, set)):
                collected = {
                    str(item) for item in raw_allowed if isinstance(item, str) and item
                }
                if collected:
                    normalized_allowed = set(collected)
                    if registry_lookup is not None:
                        resolved: set[str] = set()
                        for candidate in collected:
                            try:
                                device_entry = registry_lookup(candidate)
                            except Exception:  # defensive against stub mismatches
                                device_entry = None
                            if device_entry is None:
                                continue
                            canonical = self._extract_our_identifier(device_entry)
                            if canonical:
                                resolved.add(canonical)
                        if resolved:
                            normalized_allowed.update(resolved)
                else:
                    normalized_allowed = None

            allow_filter = normalized_allowed

            if device_index:
                base_ids = [
                    dev_id
                    for dev_id in device_index
                    if allow_filter is None or dev_id in allow_filter
                ]
            else:
                base_ids = [
                    dev_id
                    for dev_id in previous_visible.get(group_key, ())
                    if allow_filter is None or dev_id in allow_filter
                ]

            visibility_candidates: list[str] = list(base_ids)
            if normalized_allowed:
                visibility_candidates.extend(normalized_allowed)

            visible_ids = tuple(sorted(dict.fromkeys(visibility_candidates)))
            if group_key != SERVICE_SUBENTRY_KEY and registry_to_canonical:
                canonicalized_ids: list[str] = []
                for dev_id in visible_ids:
                    canonicalized_ids.append(dev_id)
                    canonical_id = registry_to_canonical.get(dev_id)
                    if canonical_id and canonical_id != dev_id:
                        canonicalized_ids.append(canonical_id)
                visible_ids = tuple(
                    sorted(dict.fromkeys(canonicalized_ids))
                )

            if group_key == SERVICE_SUBENTRY_KEY:
                visible_ids = cast(tuple[str, ...], ())
                enabled_ids = cast(tuple[str, ...], ())
                manager_visible_ids = cast(tuple[str, ...], ())
            else:
                enabled_ids = tuple(
                    sorted(
                        dev_id
                        for dev_id in visible_ids
                        if dev_id in self._enabled_poll_device_ids
                    )
                )
                manager_visible_ids = tuple(
                    dict.fromkeys(
                        canonical_to_registry_id.get(dev_id, dev_id)
                        for dev_id in visible_ids
                    )
                )

            metadata[group_key] = SubentryMetadata(
                key=group_key,
                config_subentry_id=subentry_id,
                features=features,
                title=title,
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType(dict(feature_flags)),
                visible_device_ids=visible_ids,
                enabled_device_ids=enabled_ids,
            )

            if group_key != SERVICE_SUBENTRY_KEY:
                manager_visible[group_key] = manager_visible_ids

            for feature in features:
                feature_map.setdefault(feature, group_key)

            if default_key is None:
                default_key = group_key

        if SERVICE_SUBENTRY_KEY not in metadata:
            service_features = _SERVICE_SUBENTRY_FEATURES or _DEFAULT_SUBENTRY_FEATURES
            stable_service_id: str | None
            if isinstance(entry_id, str) and entry_id:
                stable_service_id = f"{entry_id}-{SERVICE_SUBENTRY_KEY}-subentry"
            else:
                stable_service_id = None
            metadata[SERVICE_SUBENTRY_KEY] = SubentryMetadata(
                key=SERVICE_SUBENTRY_KEY,
                config_subentry_id=stable_service_id,
                features=service_features,
                title=getattr(entry, "title", None),
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType({}),
                visible_device_ids=(),
                enabled_device_ids=(),
            )
            for feature in service_features:
                feature_map.setdefault(feature, SERVICE_SUBENTRY_KEY)

        if TRACKER_SUBENTRY_KEY not in metadata:
            tracker_features = _TRACKER_SUBENTRY_FEATURES or _DEFAULT_SUBENTRY_FEATURES
            previous_tracker_visible = previous_visible.get(TRACKER_SUBENTRY_KEY, ())
            stable_tracker_id: str | None
            if isinstance(entry_id, str) and entry_id:
                stable_tracker_id = f"{entry_id}-{TRACKER_SUBENTRY_KEY}-subentry"
            else:
                stable_tracker_id = None
            metadata[TRACKER_SUBENTRY_KEY] = SubentryMetadata(
                key=TRACKER_SUBENTRY_KEY,
                config_subentry_id=stable_tracker_id,
                features=tracker_features,
                title=getattr(entry, "title", None),
                poll_intervals=_current_poll_intervals(),
                filters=_current_filters(),
                feature_flags=MappingProxyType({}),
                visible_device_ids=previous_tracker_visible,
                enabled_device_ids=tuple(
                    dev_id
                    for dev_id in previous_tracker_visible
                    if dev_id in self._enabled_poll_device_ids
                ),
            )
            manager_visible[TRACKER_SUBENTRY_KEY] = tuple(
                dict.fromkeys(
                    canonical_to_registry_id.get(dev_id, dev_id)
                    for dev_id in previous_tracker_visible
                )
            )
            for feature in tracker_features:
                feature_map.setdefault(feature, TRACKER_SUBENTRY_KEY)

        self._subentry_metadata = metadata
        self._feature_to_subentry = feature_map
        if TRACKER_SUBENTRY_KEY in metadata:
            default_key = TRACKER_SUBENTRY_KEY
        elif default_key is None and metadata:
            default_key = next(iter(metadata))
        if default_key:
            self._default_subentry_key_value = default_key

        manager = self._subentry_manager
        if reload_skip_consumed:
            if visible_devices is not None or missing_core_keys:
                self._skip_repair_during_reload_refresh = False
                self._reload_repair_skip_pending_release = False
        elif (
            reload_skip_active
            and self._reload_repair_skip_pending_release
            and (visible_devices is not None or missing_core_keys)
        ):
            self._skip_repair_during_reload_refresh = False
            self._reload_repair_skip_pending_release = False

        if not skip_repair and missing_core_keys:
            self._schedule_core_subentry_repair(missing_core_keys)

        if manager and manager_visible and not skip_manager_update:
            for group_key, visible_ids in manager_visible.items():
                if group_key == SERVICE_SUBENTRY_KEY:
                    continue
                manager.update_visible_device_ids(group_key, visible_ids)
        # Ensure snapshot container has entries for all known keys
        for key in list(self._subentry_snapshots):
            if key not in metadata:
                self._subentry_snapshots.pop(key, None)
        for key in metadata:
            self._subentry_snapshots.setdefault(key, ())

    def _group_snapshot_by_subentry(
        self, snapshot: Sequence[Mapping[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """Return snapshot entries grouped by subentry key."""

        grouped: dict[str, list[dict[str, Any]]] = {
            key: [] for key in self._subentry_metadata
        }
        fallback_key = self._default_subentry_key()

        device_to_key: dict[str, str] = {}
        for key, meta in self._subentry_metadata.items():
            for dev_id in meta.visible_device_ids:
                device_to_key.setdefault(dev_id, key)

        for row in snapshot:
            if not isinstance(row, Mapping):
                continue
            dev_id_raw = row.get("device_id") or row.get("id")
            if not isinstance(dev_id_raw, str):
                continue
            target_key = device_to_key.get(dev_id_raw, fallback_key)
            grouped.setdefault(target_key, []).append(dict(row))

        for key in self._subentry_metadata:
            grouped.setdefault(key, [])

        return grouped

    def _store_subentry_snapshots(self, snapshot: Sequence[Mapping[str, Any]]) -> None:
        """Persist grouped snapshots for subentry-aware consumers."""

        grouped = self._group_snapshot_by_subentry(snapshot)
        self._subentry_snapshots = {
            key: tuple(entries) for key, entries in grouped.items()
        }

    def _resolve_subentry_key_for_feature(self, feature: str) -> str:
        """Return the subentry key for a platform feature without warnings."""

        return self._feature_to_subentry.get(feature, self._default_subentry_key())

    def get_subentry_key_for_feature(self, feature: str) -> str:
        """Return the subentry key responsible for a platform feature."""

        warnings.warn(
            "get_subentry_key_for_feature() is deprecated; pass the subentry key "
            "explicitly when constructing entities.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self._resolve_subentry_key_for_feature(feature)

    def get_subentry_metadata(
        self, *, key: str | None = None, feature: str | None = None
    ) -> SubentryMetadata | None:
        """Return metadata for a given subentry key or feature."""

        lookup_key = key
        if lookup_key is None and feature is not None:
            lookup_key = self._resolve_subentry_key_for_feature(feature)
        if lookup_key is None:
            return None
        return self._subentry_metadata.get(lookup_key)

    def stable_subentry_identifier(
        self, *, key: str | None = None, feature: str | None = None
    ) -> str:
        """Return the stable identifier string for a subentry."""

        meta = self.get_subentry_metadata(key=key, feature=feature)
        if meta is not None:
            return meta.stable_identifier()
        if key:
            return key
        if feature:
            return feature
        return self._default_subentry_key()

    def get_subentry_snapshot(
        self, key: str | None = None, *, feature: str | None = None
    ) -> list[dict[str, Any]]:
        """Return a copy of the current snapshot for a subentry."""

        lookup_key = key
        if lookup_key is None and feature is not None:
            lookup_key = self._resolve_subentry_key_for_feature(feature)
        if lookup_key is None:
            lookup_key = self._default_subentry_key()
        entries = self._subentry_snapshots.get(lookup_key)
        if not entries:
            return []
        return [dict(row) for row in entries]

    def is_device_visible_in_subentry(self, subentry_key: str, device_id: str) -> bool:
        """Return True if a device is visible within the subentry scope."""

        meta = self._subentry_metadata.get(subentry_key)
        if meta is None:
            return False
        return device_id in meta.visible_device_ids

    def get_device_location_data_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> dict[str, Any] | None:
        """Return location data for a device if it belongs to the subentry."""

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_device_location_data(device_id)

    def get_device_last_seen_for_subentry(
        self, subentry_key: str, device_id: str
    ) -> datetime | None:
        """Return last_seen for a device within the given subentry."""

        if not self.is_device_visible_in_subentry(subentry_key, device_id):
            return None
        return self.get_device_last_seen(device_id)

    @staticmethod
    def _sanitize_contributor_mode(mode: str | None) -> str:
        """Normalize the contributor mode to a supported value."""

        if isinstance(mode, str):
            normalized = mode.strip().lower()
            if normalized in (
                CONTRIBUTOR_MODE_HIGH_TRAFFIC,
                CONTRIBUTOR_MODE_IN_ALL_AREAS,
            ):
                return normalized
        return DEFAULT_CONTRIBUTOR_MODE

    def _async_persist_contributor_mode(self) -> None:
        """Persist the contributor mode preferences asynchronously."""

        async def _persist() -> None:
            try:
                await self._cache.async_set_cached_value(
                    CACHE_KEY_CONTRIBUTOR_MODE, self._contributor_mode
                )
                await self._cache.async_set_cached_value(
                    CACHE_KEY_LAST_MODE_SWITCH,
                    self._contributor_mode_switch_epoch,
                )
            except Exception as err:  # pragma: no cover - defensive persistence
                _LOGGER.debug("Failed to persist contributor mode state: %s", err)

        self.hass.async_create_task(
            _persist(), name=f"{DOMAIN}.persist_contributor_mode"
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
            entry = self.config_entry or getattr(self, "entry", None)
            if entry and not getattr(self._cache, "entry_id", None):
                setattr(self._cache, "entry_id", entry.entry_id)
        except Exception:
            pass

        # Make sure the service device exists early so end devices can link to it promptly.
        self._ensure_service_device_exists()

        # Initial index (works even if config_entry is not yet bound; will re-run on DR event)
        self._reindex_poll_targets_from_device_registry()
        if self._dr_unsub is None:
            self._dr_unsub = self.hass.bus.async_listen(
                EVENT_DEVICE_REGISTRY_UPDATED, self._handle_dr_event
            )

    def _get_google_home_filter(self) -> GoogleHomeFilterProtocol | None:
        """Return the Google Home filter associated with this coordinator."""

        entry = self.config_entry or getattr(self, "entry", None)
        runtime_data = getattr(entry, "runtime_data", None)
        google_home_filter = getattr(runtime_data, "google_home_filter", None)
        return cast("GoogleHomeFilterProtocol | None", google_home_filter)

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
    def _redact_text(self, value: str | None, max_len: int = 120) -> str:
        """Return a short, redacted string variant suitable for logs/diagnostics."""
        if not value:
            return ""
        s = str(value)
        return s if len(s) <= max_len else (s[:max_len] + "…")

    def _device_display_name(self, dev: dr.DeviceEntry, fallback: str) -> str:
        """Return the best human-friendly device name without sensitive data."""
        return (dev.name_by_user or dev.name or fallback or "").strip()

    def _entry_id(self) -> str | None:
        """Small helper to read the bound ConfigEntry ID (None at very early startup)."""
        entry = self.config_entry
        return getattr(entry, "entry_id", None)

    def _extract_our_identifier(self, device: dr.DeviceEntry) -> str | None:
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

    def _call_device_registry_api(
        self,
        call: Callable[..., Any],
        *,
        base_kwargs: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call a device registry API, handling keyword compatibility."""

        kwargs = dict(base_kwargs or {})
        if "config_subentry_id" in kwargs:
            replacement = self._device_registry_config_subentry_kwarg_name(call)
            if replacement is None:
                kwargs.pop("config_subentry_id")
            elif replacement != "config_subentry_id":
                kwargs[replacement] = kwargs.pop("config_subentry_id")

        try:
            return call(**kwargs)
        except TypeError as err:
            if not self._device_registry_kwargs_need_legacy_retry(err, kwargs):
                raise

            legacy_kwargs = self._device_registry_build_legacy_kwargs(kwargs)
            _LOGGER.debug(
                "Retrying device registry call %s with legacy keyword arguments after %s",
                getattr(call, "__qualname__", repr(call)),
                err,
            )
            return call(**legacy_kwargs)

    @staticmethod
    def _device_registry_kwargs_need_legacy_retry(
        err: TypeError, kwargs: Mapping[str, Any]
    ) -> bool:
        """Return True when ``kwargs`` must be rewritten for legacy cores."""

        err_str = str(err)
        if "add_config_entry_id" in kwargs and "add_config_entry_id" in err_str:
            return True
        if "add_config_subentry_id" in kwargs and "add_config_subentry_id" in err_str:
            return True
        if (
            "remove_config_subentry_id" in kwargs
            and "remove_config_subentry_id" in err_str
        ):
            return True
        return False

    @staticmethod
    def _device_registry_build_legacy_kwargs(
        kwargs: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Translate modern device-registry kwargs to their legacy names."""

        legacy_kwargs = dict(kwargs)
        if "add_config_entry_id" in legacy_kwargs:
            legacy_kwargs["config_entry_id"] = legacy_kwargs.pop("add_config_entry_id")
        if "add_config_subentry_id" in legacy_kwargs:
            legacy_kwargs["config_subentry_id"] = legacy_kwargs.pop(
                "add_config_subentry_id"
            )
        if "remove_config_subentry_id" in legacy_kwargs:
            legacy_kwargs.pop("remove_config_subentry_id")
        return legacy_kwargs

    def _device_registry_config_subentry_kwarg_name(
        self, call: Callable[..., Any]
    ) -> str | None:
        """Return the config-subentry kwarg name accepted by ``call``.

        Home Assistant 2025.11 renamed the ``async_update_device`` keyword from
        ``config_subentry_id`` to ``add_config_subentry_id``. Earlier versions still
        expect ``config_subentry_id``. This helper inspects the callable signature
        and returns the supported keyword, caching the result for reuse.
        """

        cache_attr = "_device_registry_config_subentry_kwarg_cache"
        cache_obj = getattr(self, cache_attr, None)
        cache: dict[Callable[..., Any], str | None]
        if isinstance(cache_obj, dict):
            cache = cast(dict[Callable[..., Any], str | None], cache_obj)
        else:
            cache = cast(dict[Callable[..., Any], str | None], {})
            setattr(self, cache_attr, cache)

        func = getattr(call, "__func__", call)
        if func in cache:
            return cache[func]

        try:
            signature = inspect.signature(call)
        except (TypeError, ValueError):  # pragma: no cover - defensive fallback
            kwarg_name: str | None = None
        else:
            parameters = signature.parameters
            if "config_subentry_id" in parameters:
                kwarg_name = "config_subentry_id"
            elif "add_config_subentry_id" in parameters:
                kwarg_name = "add_config_subentry_id"
            elif any(
                param.kind is inspect.Parameter.VAR_KEYWORD
                for param in parameters.values()
            ):
                kwarg_name = "config_subentry_id"
            else:
                kwarg_name = None

        cache[func] = kwarg_name
        return kwarg_name

    def _device_registry_allows_translation_update(self, dev_reg: Any) -> bool:
        """Return True if the registry accepts translation metadata during updates."""

        cached = getattr(self, "_device_registry_supports_translation_update", None)
        if isinstance(cached, bool):
            return cached

        update_helper = getattr(dev_reg, "async_update_device", None)
        supports_translation = False
        if callable(update_helper):
            try:
                signature = inspect.signature(update_helper)
            except (TypeError, ValueError):
                supports_translation = False
            else:
                params = signature.parameters
                supports_translation = "translation_key" in params and "translation_placeholders" in params

        setattr(self, "_device_registry_supports_translation_update", supports_translation)
        return supports_translation

    def _ensure_service_device_exists(self, entry: ConfigEntry | None = None) -> None:
        """Idempotently create/update the per-entry 'service device' in the device registry.

        This keeps diagnostic entities (e.g. polling/auth-status) grouped under a stable
        integration-level device. Safe to call multiple times.
        """
        # Resolve hass
        hass = getattr(self, "hass", None)
        if hass is None:
            return

        # Resolve ConfigEntry (works with either .entry or .config_entry on the coordinator)
        entry = entry or getattr(self, "entry", None) or self.config_entry
        if entry is None:
            _LOGGER.debug(
                "Service-device ensure skipped: ConfigEntry not available on coordinator."
            )
            return

        entry_id = getattr(entry, "entry_id", None)

        # Refresh subentry metadata to obtain the current service subentry context.
        try:
            self._refresh_subentry_index(
                skip_manager_update=True, skip_repair=True
            )
        except Exception:  # pragma: no cover - defensive guard
            pass

        service_meta = self._subentry_metadata.get(SERVICE_SUBENTRY_KEY)

        def _normalize_subentry_id(value: Any) -> str | None:
            return _sanitize_subentry_identifier(value)

        entry_service_subentry_id = _normalize_subentry_id(
            getattr(entry, "service_subentry_id", None)
        )

        entry_subentries = getattr(entry, "subentries", None)
        service_subentry_ids: set[str] = set()
        if isinstance(entry_subentries, Mapping):
            for subentry_id, subentry in entry_subentries.items():
                normalized_id = _normalize_subentry_id(subentry_id)
                if normalized_id is None:
                    continue
                if (
                    normalized_id.endswith("-provisional")
                    and normalized_id != entry_service_subentry_id
                ):
                    continue
                subentry_type = getattr(subentry, "subentry_type", None)
                group_key: Any = None
                data_obj = getattr(subentry, "data", None)
                if isinstance(data_obj, Mapping):
                    group_key = data_obj.get("group_key")
                if (
                    subentry_type == SUBENTRY_TYPE_SERVICE
                    or (
                        isinstance(group_key, str)
                        and group_key == SERVICE_SUBENTRY_KEY
                    )
                ):
                    service_subentry_ids.add(normalized_id)

        def _is_real_service_subentry(candidate: Any) -> str | None:
            """Return candidate when it matches a confirmed service subentry."""

            normalized_candidate = _normalize_subentry_id(candidate)
            if normalized_candidate is None:
                return None

            if entry_service_subentry_id is not None:
                if normalized_candidate != entry_service_subentry_id:
                    return None
                if (
                    service_subentry_ids
                    and normalized_candidate not in service_subentry_ids
                ):
                    return None
                return normalized_candidate

            if service_subentry_ids and normalized_candidate in service_subentry_ids:
                return normalized_candidate

            return None

        service_config_subentry_id = None
        meta_identifier: Any | None = None
        if service_meta is not None:
            meta_identifier = getattr(service_meta, "config_subentry_id", None)
        for candidate in (meta_identifier, entry_service_subentry_id):
            resolved = _is_real_service_subentry(candidate)
            if resolved is not None:
                service_config_subentry_id = resolved
                break

        service_subentry_identifier: tuple[str, str] | None = None
        if service_config_subentry_id is not None:
            service_subentry_identifier = (
                DOMAIN,
                f"{entry.entry_id}:{service_config_subentry_id}:service",
            )

        setattr(
            self,
            "_service_device_identifier",
            service_device_identifier(entry.entry_id),
        )

        previous_service_identifier_sentinel = object()
        previous_service_identifier = getattr(
            self,
            "_service_device_last_subentry_identifier",
            previous_service_identifier_sentinel,
        )

        # Fast-path: already ensured in this runtime and the service subentry
        # context has not changed.
        if (
            getattr(self, "_service_device_ready", False)
            and getattr(self, "_service_device_id", None)
            and previous_service_identifier is not previous_service_identifier_sentinel
            and service_subentry_identifier is not None
            and previous_service_identifier == service_subentry_identifier
        ):
            self._apply_pending_via_updates()
            return

        dev_reg = dr.async_get(hass)
        if not hasattr(dev_reg, "async_get_or_create") or not hasattr(
            dev_reg, "async_update_device"
        ):
            _LOGGER.debug(
                "Service-device ensure skipped: registry stub missing create/update APIs."
            )
            return
        identifiers: set[tuple[str, str]] = {
            service_device_identifier(entry.entry_id)
        }  # {(DOMAIN, f"integration_<entry_id>")}
        if service_subentry_identifier is not None:
            identifiers.add(service_subentry_identifier)

        def _service_entry_links(device: Any) -> set[str | None]:
            """Return the set of subentry identifiers linked to ``entry``."""

            if not entry_id:
                return set()

            mapping_obj = getattr(device, "config_entries_subentries", None)
            normalized: set[str | None] = set()
            if isinstance(mapping_obj, Mapping):
                raw_links = mapping_obj.get(entry_id)
                if isinstance(raw_links, str):
                    normalized.add(raw_links)
                elif isinstance(raw_links, Iterable) and not isinstance(
                    raw_links, (str, bytes)
                ):
                    for candidate in raw_links:
                        if isinstance(candidate, str):
                            normalized.add(candidate)
                        elif candidate is None:
                            normalized.add(None)
                elif raw_links is None and entry_id in mapping_obj:
                    normalized.add(None)

            if not normalized:
                fallback = getattr(device, "config_subentry_id", None)
                if isinstance(fallback, str):
                    normalized.add(fallback)
                elif fallback is None and isinstance(
                    getattr(device, "config_entries", None), Iterable
                ):
                    for candidate_entry_id in cast(
                        Iterable[Any], getattr(device, "config_entries", ())
                    ):
                        if isinstance(candidate_entry_id, str) and candidate_entry_id == entry_id:
                            normalized.add(None)
                            break

            return normalized

        def _service_has_service_link(device: Any) -> bool:
            if service_config_subentry_id is None:
                return False
            return service_config_subentry_id in _service_entry_links(device)

        def _service_has_hub_link(device: Any) -> bool:
            return None in _service_entry_links(device)

        def _detach_service_hub_link(device: Any) -> Any:
            update_call = getattr(dev_reg, "async_update_device", None)
            if not callable(update_call) or not entry_id:
                return device
            device_id = getattr(device, "id", None)
            if not isinstance(device_id, str) or not device_id:
                return device
            self._call_device_registry_api(
                update_call,
                base_kwargs={
                    "device_id": device_id,
                    "remove_config_entry_id": entry_id,
                    "remove_config_subentry_id": None,
                },
            )
            return _refresh_service_device_entry(device)

        get_device = getattr(dev_reg, "async_get_device", None)
        device = None
        if callable(get_device):
            try:
                device = get_device(identifiers=identifiers)
            except TypeError:
                device = None

        def _refresh_service_device_entry(candidate: Any) -> Any:
            """Return a fresh copy of the service device entry when possible."""

            if candidate is None:
                return None

            getter = getattr(dev_reg, "async_get", None)
            device_id = getattr(candidate, "id", None)
            if not callable(getter) or not isinstance(device_id, str) or not device_id:
                return candidate

            try:
                refreshed = getter(device_id)
            except TypeError:
                return candidate

            return candidate if refreshed is None else refreshed

        if (
            device is not None
            and service_config_subentry_id is not None
            and getattr(device, "config_subentry_id", None) != service_config_subentry_id
        ):
            device_id = getattr(device, "id", None)
            if isinstance(device_id, str) and device_id:
                _LOGGER.debug(
                    "[%s] Healing service device: correcting config_subentry_id from %s to %s",
                    entry.entry_id,
                    getattr(device, "config_subentry_id", None),
                    service_config_subentry_id,
                )
                healed = self._call_device_registry_api(
                    dev_reg.async_update_device,
                    base_kwargs={
                        "device_id": device_id,
                        "config_subentry_id": service_config_subentry_id,
                        "add_config_entry_id": entry.entry_id,
                    },
                )
                device = _refresh_service_device_entry(healed or device)
                if device is None:
                    _LOGGER.error(
                        "[%s] Failed to heal service device", entry.entry_id
                    )
                    raise HomeAssistantError("Failed to heal service device")
            else:
                _LOGGER.debug(
                    "[%s] Service device missing identifier; unable to heal config_subentry_id",
                    entry.entry_id,
                )

        existing_name: str | None = None
        existing_user_name: str | None = None
        has_user_name = False
        if device is not None:
            existing_name = getattr(device, "name", None)
            existing_user_name = getattr(device, "name_by_user", None)
            if isinstance(existing_user_name, str) and existing_user_name.strip():
                has_user_name = True

        entry_title = getattr(entry, "title", None)
        sanitized_entry_title = (
            entry_title.strip() if isinstance(entry_title, str) and entry_title.strip() else None
        )
        service_device_name = existing_name or sanitized_entry_title

        _LOGGER.debug(
            "Service device registry pre-ensure (entry=%s): name=%s, name_by_user=%s",
            entry.entry_id,
            self._redact_text(existing_name),
            self._redact_text(existing_user_name),
        )

        if device is None:
            create_kwargs: dict[str, Any] = {
                "config_entry_id": entry.entry_id,
                "identifiers": identifiers,
                "manufacturer": SERVICE_DEVICE_MANUFACTURER,
                "model": SERVICE_DEVICE_MODEL,
                "sw_version": INTEGRATION_VERSION,
                "entry_type": dr.DeviceEntryType.SERVICE,
                "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
            }
            if service_device_name:
                create_kwargs["name"] = service_device_name
            create_kwargs["translation_key"] = SERVICE_DEVICE_TRANSLATION_KEY
            create_kwargs["translation_placeholders"] = {}
            if service_config_subentry_id is not None:
                create_kwargs["config_subentry_id"] = service_config_subentry_id

            device = self._call_device_registry_api(
                dev_reg.async_get_or_create,
                base_kwargs=create_kwargs,
            )
            device = _refresh_service_device_entry(device)
            _LOGGER.debug(
                "Created Google Find My service device for entry %s (device_id=%s)",
                entry.entry_id,
                getattr(device, "id", None),
            )
        else:
            # Keep metadata fresh if it drifted (rare)
            raw_device_identifiers = getattr(device, "identifiers", set()) or set()
            device_identifiers = set(raw_device_identifiers)
            identifiers_to_apply = set(identifiers)
            extraneous_service_identifiers: set[tuple[Any, ...]] = set()
            for existing in list(device_identifiers):
                if (
                    isinstance(existing, tuple)
                    and len(existing) == 2
                    and existing[0] == DOMAIN
                    and isinstance(existing[1], str)
                    and existing[1].endswith(":service")
                    and existing not in identifiers_to_apply
                ):
                    extraneous_service_identifiers.add(existing)

            missing_identifiers = identifiers_to_apply - device_identifiers
            needs_identifier_sync = bool(missing_identifiers or extraneous_service_identifiers)
            current_service_links = {
                candidate
                for candidate in _service_entry_links(device)
                if isinstance(candidate, str)
            }

            dev_translation_key = getattr(device, "translation_key", None)
            dev_translation_placeholders = getattr(
                device, "translation_placeholders", None
            )
            dev_config_subentry_id = getattr(device, "config_subentry_id", None)
            should_remove_service_link = (
                service_config_subentry_id is None and bool(current_service_links)
            )
            should_add_hub_link = (
                service_config_subentry_id is None
                and not _service_has_hub_link(device)
                and bool(entry_id)
            )

            translation_refresh_required = (
                dev_translation_key != SERVICE_DEVICE_TRANSLATION_KEY
                or (dev_translation_placeholders or {}) != {}
            )
            translation_update_supported = (
                translation_refresh_required
                and self._device_registry_allows_translation_update(dev_reg)
            )

            needs_name_refresh = (
                service_device_name is not None
                and service_device_name != existing_name
                and not has_user_name
            )

            needs_update = (
                device.manufacturer != SERVICE_DEVICE_MANUFACTURER
                or device.model != SERVICE_DEVICE_MODEL
                or device.sw_version != INTEGRATION_VERSION
                or device.entry_type != dr.DeviceEntryType.SERVICE
                or dev_config_subentry_id != service_config_subentry_id
                or translation_refresh_required
                or needs_name_refresh
                or needs_identifier_sync
                or should_remove_service_link
                or should_add_hub_link
            )
            if needs_update:
                update_kwargs: dict[str, Any] = {
                    "device_id": device.id,
                    "manufacturer": SERVICE_DEVICE_MANUFACTURER,
                    "model": SERVICE_DEVICE_MODEL,
                    "sw_version": INTEGRATION_VERSION,
                    "entry_type": dr.DeviceEntryType.SERVICE,
                    "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
                }
                if service_config_subentry_id is not None:
                    update_kwargs["config_subentry_id"] = service_config_subentry_id
                if needs_identifier_sync:
                    new_identifiers = (
                        device_identifiers - extraneous_service_identifiers
                    ) | identifiers_to_apply
                    update_kwargs["new_identifiers"] = new_identifiers
                if entry_id and (
                    service_config_subentry_id is not None
                    or should_remove_service_link
                    or should_add_hub_link
                ):
                    update_kwargs["add_config_entry_id"] = entry.entry_id
                    if service_config_subentry_id is not None:
                        update_kwargs["add_config_subentry_id"] = (
                            service_config_subentry_id
                        )
                if should_remove_service_link and entry_id:
                    update_kwargs["remove_config_entry_id"] = entry.entry_id
                    removal_id: str | None = None
                    if current_service_links:
                        removal_id = next(iter(current_service_links))
                    elif isinstance(dev_config_subentry_id, str) and dev_config_subentry_id.strip():
                        removal_id = dev_config_subentry_id.strip()
                    update_kwargs["remove_config_subentry_id"] = removal_id
                if needs_name_refresh and service_device_name:
                    update_kwargs["name"] = service_device_name

                call_kwargs = dict(update_kwargs)
                if translation_update_supported:
                    call_kwargs["translation_key"] = SERVICE_DEVICE_TRANSLATION_KEY
                    call_kwargs["translation_placeholders"] = {}

                try:
                    self._call_device_registry_api(
                        dev_reg.async_update_device, base_kwargs=call_kwargs
                    )
                except TypeError as err:
                    if translation_update_supported:
                        setattr(
                            self,
                            "_device_registry_supports_translation_update",
                            False,
                        )
                        translation_update_supported = False
                        self._call_device_registry_api(
                            dev_reg.async_update_device, base_kwargs=update_kwargs
                        )
                    else:  # pragma: no cover - propagate unexpected contract errors
                        raise err
                device = _refresh_service_device_entry(device)
                if translation_refresh_required and not translation_update_supported:
                    translation_kwargs: dict[str, Any] = {
                        "config_entry_id": entry.entry_id,
                        "identifiers": identifiers,
                        "manufacturer": SERVICE_DEVICE_MANUFACTURER,
                        "model": SERVICE_DEVICE_MODEL,
                        "sw_version": INTEGRATION_VERSION,
                        "entry_type": dr.DeviceEntryType.SERVICE,
                        "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
                        "translation_key": SERVICE_DEVICE_TRANSLATION_KEY,
                        "translation_placeholders": {},
                    }
                    if service_config_subentry_id is not None:
                        translation_kwargs["config_subentry_id"] = (
                            service_config_subentry_id
                        )
                    if needs_name_refresh and service_device_name:
                        translation_kwargs["name"] = service_device_name
                    device = self._call_device_registry_api(
                        dev_reg.async_get_or_create,
                        base_kwargs=translation_kwargs,
                    )
                    device = _refresh_service_device_entry(device)
                    _LOGGER.debug(
                        "Backfilled service device translation metadata using get_or_create for entry %s",
                        entry.entry_id,
                    )
                _LOGGER.debug(
                    "Updated Google Find My service device metadata for entry %s",
                    entry.entry_id,
                )

        # Book-keeping for quick re-entrance
        self._service_device_ready = True
        self._service_device_id = getattr(device, "id", None)
        setattr(
            self,
            "_service_device_last_subentry_identifier",
            service_subentry_identifier,
        )
        setattr(
            self,
            "_service_device_last_config_subentry_id",
            service_config_subentry_id,
        )

        if device is not None:
            links = _service_entry_links(device)
            has_hub_link = None in links
            if has_hub_link and service_config_subentry_id is not None:
                _LOGGER.info(
                    "[%s] Removing redundant hub link from service device %s",
                    entry.entry_id,
                    getattr(device, "id", "<unknown>"),
                )
                device = _detach_service_hub_link(device)
                self._service_device_id = getattr(device, "id", None)

        if device is not None:
            _LOGGER.debug(
                "Service device registry post-ensure (entry=%s): name=%s, name_by_user=%s",
                entry.entry_id,
                self._redact_text(getattr(device, "name", None)),
                self._redact_text(getattr(device, "name_by_user", None)),
            )

        # Backfill any end devices that were created before the service device was known
        self._apply_pending_via_updates()

    def _find_tracker_entity_entry(
        self, device_id: str
    ) -> EntityRegistryEntry | None:
        """Return the registry entry for a tracker and migrate legacy unique IDs."""

        ent_reg = er.async_get(self.hass)
        entry_id = self._entry_id()
        device_label = self.get_device_display_name(device_id) or device_id

        entities_container = getattr(ent_reg, "entities", None)
        ent_registry_values: Sequence[Any] = ()
        if entities_container is not None:
            try:
                ent_registry_values = list(entities_container.values())
            except Exception:  # noqa: BLE001 - best-effort compatibility
                ent_registry_values = ()

        canonical_unique_id: str | None = None
        if entry_id:
            tracker_subentry_key = TRACKER_SUBENTRY_KEY
            tracker_meta: Any | None = None
            meta_getter = getattr(self, "get_subentry_metadata", None)
            if callable(meta_getter):
                try:
                    tracker_meta = meta_getter(feature="device_tracker")
                except TypeError:
                    tracker_meta = None
                except AttributeError:
                    tracker_meta = None
            if tracker_meta is not None:
                candidate_key = getattr(tracker_meta, "key", None)
                if isinstance(candidate_key, str) and candidate_key.strip():
                    tracker_subentry_key = candidate_key.strip()

            tracker_subentry_identifier: str | None = None
            identifier_getter = getattr(self, "stable_subentry_identifier", None)
            if callable(identifier_getter):
                try:
                    tracker_subentry_identifier = identifier_getter(
                        key=tracker_subentry_key,
                        feature="device_tracker",
                    )
                except TypeError:
                    tracker_subentry_identifier = identifier_getter(
                        key=tracker_subentry_key
                    )
                except Exception:  # noqa: BLE001 - defensive for legacy coordinators
                    tracker_subentry_identifier = None
            if not isinstance(tracker_subentry_identifier, str) or not tracker_subentry_identifier.strip():
                tracker_subentry_identifier = tracker_subentry_key

            parts: list[str] = []
            for part in (entry_id, tracker_subentry_identifier, device_id):
                if isinstance(part, str) and part:
                    stripped = part.strip()
                    if stripped:
                        parts.append(stripped)
            if parts:
                canonical_unique_id = ":".join(parts)

        def _get_entry_for_unique_id(
            unique_id: str,
        ) -> EntityRegistryEntry | None:
            """Return the registry entry for a given unique_id if it exists."""

            if not unique_id:
                return None

            try:
                entity_id = ent_reg.async_get_entity_id(
                    DEVICE_TRACKER_DOMAIN,
                    DOMAIN,
                    unique_id,
                )
            except TypeError:
                entity_id = None

            if not entity_id:
                return None

            entry: EntityRegistryEntry | None = None
            getter = getattr(ent_reg, "async_get", None)
            if callable(getter):
                try:
                    entry = getter(entity_id)
                except TypeError:
                    entry = None

            if entry is None and ent_registry_values:
                for candidate in ent_registry_values:
                    if getattr(candidate, "entity_id", None) == entity_id:
                        entry = candidate
                        break

            if entry is None:
                entry = SimpleNamespace(
                    entity_id=entity_id,
                    unique_id=unique_id,
                    domain=DEVICE_TRACKER_DOMAIN,
                    platform=DOMAIN,
                    config_entry_id=entry_id,
                )

            return cast("EntityRegistryEntry", entry)

        if canonical_unique_id:
            entry = _get_entry_for_unique_id(canonical_unique_id)
            if entry is not None:
                _LOGGER.debug(
                    "Tracker registry matched canonical unique_id=%s for device '%s' (entity_id=%s)",
                    canonical_unique_id,
                    device_label,
                    entry.entity_id,
                )
                return entry

        candidate_unique_ids: list[str] = []
        if entry_id:
            candidate_unique_ids.append(f"{entry_id}:{device_id}")
            candidate_unique_ids.append(f"{DOMAIN}_{entry_id}_{device_id}")
        candidate_unique_ids.append(f"{DOMAIN}_{device_id}")

        for unique_id in candidate_unique_ids:
            entry = _get_entry_for_unique_id(unique_id)
            if entry is None:
                continue

            if canonical_unique_id and entry.unique_id != canonical_unique_id:
                _LOGGER.info(
                    "Migrating tracker entity %s for device '%s' from legacy unique_id=%s to canonical unique_id=%s",
                    entry.entity_id,
                    device_label,
                    entry.unique_id,
                    canonical_unique_id,
                )
                try:
                    update_entity = getattr(ent_reg, "async_update_entity", None)
                    if callable(update_entity):
                        update_entity(
                            entry.entity_id,
                            new_unique_id=canonical_unique_id,
                        )
                        migrated = _get_entry_for_unique_id(canonical_unique_id)
                        if migrated is not None:
                            return migrated
                    else:
                        _LOGGER.debug(
                            "Entity registry for entry %s lacks async_update_entity; skipping canonical migration",
                            entry_id,
                        )
                        return entry
                except ValueError as err:
                    _LOGGER.error(
                        "Failed to migrate tracker entity %s to canonical unique_id=%s: %s",
                        entry.entity_id,
                        canonical_unique_id,
                        err,
                    )
                    return entry

            return entry

        for entry in ent_registry_values:
            if entry.domain != DEVICE_TRACKER_DOMAIN or entry.platform != DOMAIN:
                continue
            unique_id = getattr(entry, "unique_id", "")
            if not isinstance(unique_id, str) or device_id not in unique_id:
                continue
            if entry.config_entry_id and entry.config_entry_id != entry_id:
                continue

            if canonical_unique_id and unique_id != canonical_unique_id:
                _LOGGER.info(
                    "Migrating tracker entity %s for device '%s' from heuristic unique_id=%s to canonical unique_id=%s",
                    entry.entity_id,
                    device_label,
                    unique_id,
                    canonical_unique_id,
                )
                try:
                    update_entity = getattr(ent_reg, "async_update_entity", None)
                    if callable(update_entity):
                        update_entity(
                            entry.entity_id,
                            new_unique_id=canonical_unique_id,
                        )
                        migrated = _get_entry_for_unique_id(canonical_unique_id)
                        if migrated is not None:
                            return migrated
                    else:
                        _LOGGER.debug(
                            "Entity registry for entry %s lacks async_update_entity; skipping canonical migration",
                            entry_id,
                        )
                        return entry
                except ValueError as err:
                    _LOGGER.error(
                        "Failed to migrate heuristic tracker entity %s to canonical unique_id=%s: %s",
                        entry.entity_id,
                        canonical_unique_id,
                        err,
                    )
                    return entry

            _LOGGER.debug(
                "Tracker registry fallback matched entity_id=%s (unique_id=%s) for device '%s'",
                entry.entity_id,
                unique_id,
                device_label,
            )
            return entry

        _LOGGER.debug(
            "No entity registry entry for device '%s'; checked unique_id formats %s (canonical=%s)",
            device_label,
            candidate_unique_ids,
            canonical_unique_id,
        )
        return None

    def find_tracker_entity_entry(self, device_id: str) -> EntityRegistryEntry | None:
        """Public wrapper to expose tracker entity lookup to platforms."""

        return self._find_tracker_entity_entry(device_id)

    # Optional back-compat alias (some callers may use the public-style name)
    ensure_service_device_exists = _ensure_service_device_exists

    def _ensure_device_name_cache(self) -> dict[str, str]:
        """Return the lazily initialized device-name cache."""

        cache = getattr(self, "_device_names", None)
        if cache is None:
            cache = {}
            setattr(self, "_device_names", cache)
        return cache

    def _apply_pending_via_updates(self) -> None:
        """Deprecated no-op retained for backward compatibility."""

        # Tracker devices no longer link to the service device via ``via_device``.
        # Keep the method defined to avoid AttributeError in case third-party
        # callers relied on the old behavior, but return immediately.
        return

    def _sync_owner_index(self, devices: list[dict[str, Any]] | None) -> None:
        """Sync hass.data owner index for this entry (FCM fallback support)."""

        hass = getattr(self, "hass", None)
        entry_id = self._entry_id()
        if hass is None or not entry_id:
            return

        try:
            bucket = hass.data.setdefault(DOMAIN, {})
            owner_index: dict[str, str] = bucket.setdefault("device_owner_index", {})
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "[entry=%s] Owner-index sync skipped: %s",
                entry_id,
                err,
            )
            return

        seen: set[str] = set()
        for device in devices or []:
            canonical = (
                device.get("canonicalId")
                or device.get("canonical_id")
                or device.get("id")
                or device.get("device_id")
            )
            if canonical is None:
                continue
            if not isinstance(canonical, str):
                canonical = str(canonical)
            canonical = canonical.strip()
            if not canonical:
                continue
            owner_index[canonical] = entry_id
            seen.add(canonical)

        if owner_index:
            stale = [
                cid
                for cid, eid in list(owner_index.items())
                if eid == entry_id and cid not in seen
            ]
            for cid in stale:
                owner_index.pop(cid, None)
            if stale:
                _LOGGER.debug(
                    "[entry=%s] Pruned %d stale owner-index entries",
                    entry_id,
                    len(stale),
                )

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

        present: set[str] = set()
        enabled: set[str] = set()

        # Map device_id -> has_enabled_tracker_entity
        has_enabled_tracker: dict[str, bool] = {}
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

        # Update subentry metadata since enabled/present sets may affect visibility
        self._refresh_subentry_index()

        _LOGGER.debug(
            "Reindexed targets for entry %s: %d present / %d enabled (entities-driven)",
            entry_id,
            len(present),
            len(enabled),
        )

    # --- NEW: Create/refresh DR entries for end devices (entry-scoped) -----
    def _ensure_registry_for_devices(
        self, devices: list[dict[str, Any]], ignored: set[str]
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
        - Prefer linking devices to the service anchor via the identifier-based
          `via_device` kwarg when supported. Older cores fall back to `via_device_id`
          once the service device has been created.

        Returns:
            Count of devices that were created or updated.
        """
        entry = self.config_entry or getattr(self, "entry", None)
        entry_id = getattr(entry, "entry_id", None) if entry is not None else None
        if not entry_id:
            return 0

        entry_type: str | None = None
        if entry is not None:
            for container in (getattr(entry, "data", None), getattr(entry, "options", None)):
                if isinstance(container, Mapping):
                    marker = container.get("subentry_type")
                    if isinstance(marker, str):
                        entry_type = marker
                        break
            if entry_type is None and isinstance(getattr(entry, "data", None), Mapping):
                fallback_marker = cast(Mapping[str, Any], entry.data).get("type")
                if not isinstance(fallback_marker, str):
                    fallback_marker = cast(Mapping[str, Any], entry.data).get("entry_type")
                if isinstance(fallback_marker, str):
                    entry_type = fallback_marker

        if entry_type in {SUBENTRY_TYPE_HUB, "hub"}:
            _LOGGER.debug(
                "Skipping Device Registry ensure for hub entry %s; subentries manage device links.",
                entry_id,
            )
            return 0

        try:
            self._refresh_subentry_index(devices)
        except Exception:  # pragma: no cover - defensive guard
            pass

        tracker_meta = self._subentry_metadata.get(TRACKER_SUBENTRY_KEY)

        def _normalize_tracker_subentry_id(value: Any) -> str | None:
            return _sanitize_subentry_identifier(value)

        entry_tracker_subentry_id = _normalize_tracker_subentry_id(
            getattr(entry, "tracker_subentry_id", None)
        )

        entry_subentries = getattr(entry, "subentries", None)
        tracker_subentry_ids: set[str] = set()
        if isinstance(entry_subentries, Mapping):
            for subentry_id, subentry in entry_subentries.items():
                normalized_id = _normalize_tracker_subentry_id(subentry_id)
                if normalized_id is None:
                    continue
                if (
                    normalized_id.endswith("-provisional")
                    and normalized_id != entry_tracker_subentry_id
                ):
                    continue
                subentry_type = getattr(subentry, "subentry_type", None)
                group_key: Any = None
                data_obj = getattr(subentry, "data", None)
                if isinstance(data_obj, Mapping):
                    group_key = data_obj.get("group_key")
                if (
                    subentry_type == SUBENTRY_TYPE_TRACKER
                    or (
                        isinstance(group_key, str)
                        and group_key == TRACKER_SUBENTRY_KEY
                    )
                ):
                    tracker_subentry_ids.add(normalized_id)

        def _resolve_tracker_subentry(candidate: Any) -> str | None:
            normalized_candidate = _normalize_tracker_subentry_id(candidate)
            if normalized_candidate is None:
                return None

            if entry_tracker_subentry_id is not None:
                if normalized_candidate != entry_tracker_subentry_id:
                    return None
                if (
                    tracker_subentry_ids
                    and normalized_candidate not in tracker_subentry_ids
                ):
                    return None
                return normalized_candidate

            if tracker_subentry_ids and normalized_candidate in tracker_subentry_ids:
                return normalized_candidate

            return None

        tracker_config_subentry_id = None
        tracker_meta_identifier: Any | None = None
        if tracker_meta is not None:
            tracker_meta_identifier = getattr(
                tracker_meta, "config_subentry_id", None
            )
        for candidate in (tracker_meta_identifier, entry_tracker_subentry_id):
            resolved_tracker = _resolve_tracker_subentry(candidate)
            if resolved_tracker is not None:
                tracker_config_subentry_id = resolved_tracker
                break

        parent_identifier = service_device_identifier(entry_id)
        setattr(self, "_service_device_identifier", parent_identifier)

        dev_reg = dr.async_get(self.hass)
        async_get_or_create = getattr(dev_reg, "async_get_or_create", None)
        if not callable(async_get_or_create):
            _LOGGER.debug(
                "Skipping Device Registry ensure: registry stub missing async_get_or_create."
            )
            return 0
        update_device = getattr(dev_reg, "async_update_device", None)
        get_device = getattr(dev_reg, "async_get_device", None)
        get_device_by_id = getattr(dev_reg, "async_get", None)
        created_or_updated = 0

        def _subentry_links(device: Any) -> set[str | None]:
            """Return tracker subentry links associated with ``entry_id``."""

            if not entry_id:
                return set()
            mapping_obj = getattr(device, "config_entries_subentries", None)
            if isinstance(mapping_obj, Mapping):
                raw_links = mapping_obj.get(entry_id)
                if isinstance(raw_links, Collection) and not isinstance(
                    raw_links, (str, bytes, Mapping)
                ):
                    typed_links: set[str | None] = set()
                    for item in raw_links:
                        if item is None:
                            typed_links.add(None)
                        elif isinstance(item, str):
                            typed_links.add(item)
                    return typed_links
                if raw_links is None:
                    return set()
            fallback = getattr(device, "config_subentry_id", None)
            if isinstance(fallback, str):
                return {fallback}
            if fallback is not None:
                _LOGGER.debug(
                    "Skipping unexpected config_subentry_id type for device %s: %r",
                    getattr(device, "id", "unknown"),
                    fallback,
                )
            if fallback is None and getattr(device, "config_entries", None):
                return {None}
            return set()

        def _has_tracker_link(device: Any) -> bool:
            if tracker_config_subentry_id is None:
                return False
            return tracker_config_subentry_id in _subentry_links(device)

        def _has_hub_link(device: Any) -> bool:
            return None in _subentry_links(device)

        def _remove_hub_link(device: Any) -> Any:
            if (
                not callable(update_device)
                or not entry_id
                or tracker_config_subentry_id is None
            ):
                return device
            device_id = getattr(device, "id", "")
            if not device_id:
                return device
            self._call_device_registry_api(
                update_device,
                base_kwargs={
                    "device_id": device_id,
                    "remove_config_entry_id": entry_id,
                    "remove_config_subentry_id": None,
                },
            )
            return _refresh_device_entry(device_id, device)

        def _update_device_with_kwargs(kwargs: dict[str, Any]) -> None:
            if not callable(update_device):
                return
            self._call_device_registry_api(
                update_device,
                base_kwargs=dict(kwargs),
            )

        def _refresh_device_entry(device_id: str, fallback: Any) -> Any:
            if not callable(get_device_by_id) or not device_id:
                return fallback
            try:
                refreshed = get_device_by_id(device_id)
            except TypeError:
                return fallback
            return fallback if refreshed is None else refreshed

        def _heal_tracker_device_subentry(
            device: Any, *, device_label: str, device_id_hint: str | None
        ) -> tuple[Any, bool]:
            if (
                device is None
                or tracker_config_subentry_id is None
                or not callable(update_device)
            ):
                return device, False
            device_id = getattr(device, "id", None)
            if not isinstance(device_id, str) or not device_id:
                device_id = device_id_hint or None
            if not isinstance(device_id, str) or not device_id:
                return device, False
            current_subentry_id = getattr(device, "config_subentry_id", None)
            if current_subentry_id == tracker_config_subentry_id:
                return device, False
            _LOGGER.debug(
                "[%s] Healing device '%s': correcting config_subentry_id from %s to %s",
                entry_id,
                device_label,
                current_subentry_id,
                tracker_config_subentry_id,
            )
            updated = self._call_device_registry_api(
                update_device,
                base_kwargs={
                    "device_id": device_id,
                    "config_subentry_id": tracker_config_subentry_id,
                    "add_config_entry_id": entry_id,
                },
            )
            healed_device = _refresh_device_entry(device_id, updated or device)
            if healed_device is None:
                _LOGGER.error(
                    "[%s] Failed to heal device %s", entry_id, device_label
                )
                return device, False
            return healed_device, True

        for d in devices:
            dev_id = d.get("id")
            if not isinstance(dev_id, str) or dev_id in ignored:
                continue

            raw_label = (d.get("name") or "").strip()
            device_label = raw_label or dev_id or "<unknown>"

            # Build identifiers
            ns_ident = (DOMAIN, f"{entry_id}:{dev_id}")
            legacy_ident = (DOMAIN, dev_id)

            # Preferred: device already known by namespaced identifier?
            dev = None
            if callable(get_device):
                try:
                    dev = get_device(identifiers={ns_ident})
                except TypeError:
                    dev = None
            if dev is None:
                # Legacy present?
                legacy_dev = None
                if callable(get_device):
                    try:
                        legacy_dev = get_device(identifiers={legacy_ident})
                    except TypeError:
                        legacy_dev = None
                if legacy_dev is not None:
                    # If legacy device belongs to THIS entry, migrate by adding namespaced ident.
                    if entry_id in legacy_dev.config_entries:
                        new_idents = set(legacy_dev.identifiers)
                        new_idents.add(ns_ident)
                        needs_identifiers = new_idents != legacy_dev.identifiers
                        needs_config_subentry = (
                            tracker_config_subentry_id is not None
                            and not _has_tracker_link(legacy_dev)
                        )
                        raw_name = (d.get("name") or "").strip()
                        use_name = (
                            raw_name
                            if raw_name and raw_name != "Google Find My Device"
                            else None
                        )
                        needs_name = (
                            bool(use_name)
                            and not getattr(legacy_dev, "name_by_user", None)
                            and getattr(legacy_dev, "name", None) != use_name
                        )
                        needs_parent_clear = (
                            getattr(legacy_dev, "via_device_id", None) is not None
                        )
                        if (
                            needs_identifiers
                            or needs_config_subentry
                            or needs_name
                            or needs_parent_clear
                        ):
                            update_kwargs: dict[str, Any] = {
                                "device_id": legacy_dev.id,
                            }
                            if needs_config_subentry:
                                update_kwargs["add_config_entry_id"] = entry_id
                                update_kwargs["add_config_subentry_id"] = (
                                    tracker_config_subentry_id
                                )
                            if needs_identifiers:
                                update_kwargs["new_identifiers"] = new_idents
                            if needs_name:
                                update_kwargs["name"] = use_name
                            if needs_parent_clear:
                                update_kwargs["via_device_id"] = None
                            legacy_id = getattr(legacy_dev, "id", None)
                            _update_device_with_kwargs(update_kwargs)
                            legacy_dev = _refresh_device_entry(
                                legacy_id or "",
                                legacy_dev,
                            )
                            if (
                                tracker_config_subentry_id is not None
                                and _has_tracker_link(legacy_dev)
                                and _has_hub_link(legacy_dev)
                            ):
                                legacy_dev = _remove_hub_link(legacy_dev)
                        created_or_updated += 1
                        dev = legacy_dev
                    else:
                        # Belongs to another entry → create a new device with namespaced ident (no merge).
                        dev = None

            # Create if still missing
            if dev is None:
                # Only set a real label; never write placeholders on cold boot
                use_name = (
                    raw_label
                    if raw_label and raw_label != "Google Find My Device"
                    else None
                )

                create_kwargs: dict[str, Any] = {
                    "config_entry_id": entry_id,
                    "identifiers": {ns_ident},
                    "manufacturer": "Google",
                    "model": "Find My Device",
                    "name": use_name,
                }

                dev = self._call_device_registry_api(
                    async_get_or_create,
                    base_kwargs=create_kwargs,
                )
                dev, _ = _heal_tracker_device_subentry(
                    dev,
                    device_label=device_label,
                    device_id_hint=dev_id,
                )
                if (
                    tracker_config_subentry_id is not None
                    and dev is not None
                    and _has_tracker_link(dev)
                    and _has_hub_link(dev)
                ):
                    dev = _remove_hub_link(dev)
                created_or_updated += 1
            else:
                dev, _ = _heal_tracker_device_subentry(
                    dev,
                    device_label=device_label,
                    device_id_hint=dev_id,
                )
                # Keep name fresh if not user-overridden and a new upstream label is available
                use_name = (
                    raw_label
                    if raw_label and raw_label != "Google Find My Device"
                    else None
                )

                device_id = getattr(dev, "id", "")
                update_existing_kwargs: dict[str, Any] = {"device_id": device_id}
                name_needs_update = (
                    bool(use_name)
                    and not getattr(dev, "name_by_user", None)
                    and dev.name != use_name
                )
                if name_needs_update:
                    update_existing_kwargs["name"] = use_name

                needs_config_subentry_update = (
                    tracker_config_subentry_id is not None
                    and not _has_tracker_link(dev)
                )

                needs_parent_clear = (
                    getattr(dev, "via_device_id", None) is not None
                )

                if needs_parent_clear:
                    update_existing_kwargs["via_device_id"] = None

                needs_update = (
                    name_needs_update
                    or needs_config_subentry_update
                    or needs_parent_clear
                )

                if needs_update and callable(update_device) and device_id:
                    if needs_config_subentry_update:
                        update_existing_kwargs["add_config_entry_id"] = entry_id
                        update_existing_kwargs["add_config_subentry_id"] = (
                            tracker_config_subentry_id
                        )
                    elif (
                        tracker_config_subentry_id is not None
                        and _has_tracker_link(dev)
                        and _has_hub_link(dev)
                    ):
                        update_existing_kwargs["remove_config_entry_id"] = entry_id
                        update_existing_kwargs["remove_config_subentry_id"] = None
                    _update_device_with_kwargs(update_existing_kwargs)
                    dev = _refresh_device_entry(device_id or "", dev)
                    if (
                        tracker_config_subentry_id is not None
                        and _has_tracker_link(dev)
                        and _has_hub_link(dev)
                    ):
                        dev = _remove_hub_link(dev)
                    created_or_updated += 1

                elif (
                    tracker_config_subentry_id is not None
                    and callable(update_device)
                    and device_id
                    and _has_tracker_link(dev)
                    and _has_hub_link(dev)
                ):
                    dev = _remove_hub_link(dev)
                    created_or_updated += 1

        self._apply_pending_via_updates()
        return created_or_updated

    # --- NEW: Repairs + Auth state helpers ---------------------------------
    def _get_account_email(self) -> str:
        """Return the configured Google account email for this entry (empty if unknown)."""
        entry = self.config_entry
        if entry is not None:
            email_value = entry.data.get(CONF_GOOGLE_EMAIL)
            if isinstance(email_value, str):
                return email_value
        return ""

    def _create_auth_issue(self) -> None:
        """Create (idempotent) a Repairs issue for an authentication problem.

        Uses:
            - domain: `googlefindmy`
            - issue_id: stable per-entry (via `issue_id_for(entry_id)`)
            - translation_key: `ISSUE_AUTH_EXPIRED_KEY` (localizable title/description)
            - placeholders: `email` (shown in repairs UI)
        """
        entry = self.config_entry
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

    def _dismiss_auth_issue(self) -> bool:
        """Dismiss (idempotently) the Repairs issue if present.

        Returns True when an issue existed and was removed, False otherwise.
        """

        entry = self.config_entry
        if not entry:
            return False

        issue_id = issue_id_for(entry.entry_id)

        issue_present = False
        try:
            registry = ir.async_get(self.hass)
        except Exception:  # pragma: no cover - defensive fallback
            registry = None

        if registry and hasattr(registry, "async_get_issue"):
            try:
                issue_present = registry.async_get_issue(DOMAIN, issue_id) is not None
            except Exception:  # pragma: no cover - defensive fallback
                issue_present = False

        try:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        except Exception:
            # Deleting a non-existent issue is fine; keep silent.
            return False

        return issue_present

    def _set_auth_state(self, *, failed: bool, reason: str | None = None) -> None:
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
            entry_id = self.config_entry.entry_id if self.config_entry else ""
            self.hass.bus.async_fire(
                EVENT_AUTH_ERROR,
                {
                    "entry_id": entry_id,
                    "email": self._get_account_email(),
                    "message": self._auth_error_message,
                },
            )
            # Notify listeners (binary_sensor etc.)
            try:
                self.async_set_updated_data(self.data)
            except Exception:
                pass
        elif not failed:
            issue_dismissed = self._dismiss_auth_issue()
            state_changed = False

            if self._auth_error_active:
                self._auth_error_active = False
                self._auth_error_message = None
                state_changed = True

            if issue_dismissed or state_changed:
                entry_id = self.config_entry.entry_id if self.config_entry else ""
                self.hass.bus.async_fire(
                    EVENT_AUTH_OK,
                    {
                        "entry_id": entry_id,
                        "email": self._get_account_email(),
                    },
                )
                try:
                    self.async_set_updated_data(self.data)
                except Exception:
                    pass

    @property
    def auth_error_active(self) -> bool:
        """Expose the current "auth failed" condition for diagnostic entities (binary_sensor)."""
        return self._auth_error_active

    def _set_api_status(self, status: str, *, reason: str | None = None) -> None:
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

    def _set_fcm_status(self, status: str, *, reason: str | None = None) -> None:
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

    # Former `_async_start_reauth_flow` helper removed: rely on HA's automatic
    # reauth trigger when `ConfigEntryAuthFailed` bubbles up.

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

    def _run_on_hass_loop(
        self, func: Callable[..., None], *args: Any, **kwargs: Any
    ) -> None:
        """Schedule a plain callable to run on the HA loop thread ASAP.

        Note:
        - This is intentionally **fire-and-forget**; `call_soon_threadsafe` does not
          return the callable's result to the caller. Only use with functions that
          **return None** and are safe to run on the HA loop.
        """
        self.hass.loop.call_soon_threadsafe(func, *args, **kwargs)

    def _dispatch_async_request_refresh(
        self, *, task_name: str, log_context: str
    ) -> None:
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

            def _cb(_now: datetime) -> None:
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
    async def _handle_dr_event(self, _event: Event) -> None:
        """Handle Device Registry changes by rebuilding poll targets (rare)."""
        self._reindex_poll_targets_from_device_registry()
        # After changes, request a refresh so the next tick uses the new target sets.
        self._dispatch_async_request_refresh(
            task_name=f"{DOMAIN}.dr_event_refresh",
            log_context="device registry event",
        )

    # ---------------------------- Cooldown helpers (server-aware) -----------
    def _compute_type_cooldown_seconds(self, report_hint: str | None) -> int:
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
        self, device_id: str, report_hint: str | None
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
        payload: dict[str, Any],
        *,
        device_label: str | None = None,
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
    def _get_ignored_set(self) -> set[str]:
        """Return the set of device IDs the user chose to ignore (options-first).

        Notes:
            - Uses config_entry.options if available; falls back to an attribute
              'ignored_devices' when set through update_settings().
            - Intentionally simple equality (no normalization) to avoid surprises.
        """
        try:
            entry = self.config_entry
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
            return {x for x in raw_attr if isinstance(x, str)}
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
        self, exc: Exception, *, where: str = "", device: str | None = None
    ) -> None:
        """Public helper to record non-fatal errors with minimal context."""
        prefix = where or "coordinator"
        if device:
            prefix += f"({device})"
        err_type = type(exc).__name__
        self._append_recent_error(err_type, f"{prefix}: {exc}")

    # Safe getters for durations based on keys that __init__.py may set.
    def get_metric(self, key: str) -> float | None:
        val = self.performance_metrics.get(key)
        return float(val) if isinstance(val, (int, float)) else None

    def _get_duration(self, start_key: str, end_key: str) -> float | None:
        start = self.get_metric(start_key)
        end = self.get_metric(end_key)
        if start is None or end is None:
            return None
        try:
            return max(0.0, float(end) - float(start))
        except Exception:
            return None

    def get_setup_duration_seconds(self) -> float | None:
        """Duration between 'setup_start_monotonic' and 'setup_end_monotonic'."""
        return self._get_duration("setup_start_monotonic", "setup_end_monotonic")

    def get_fcm_acquire_duration_seconds(self) -> float | None:
        """Duration between 'setup_start_monotonic' and 'fcm_acquired_monotonic'."""
        start = self.get_metric("setup_start_monotonic")
        fcm = self.get_metric("fcm_acquired_monotonic")
        if start is None or fcm is None:
            return None
        try:
            return max(0.0, float(fcm) - float(start))
        except Exception:
            return None

    def get_last_poll_duration_seconds(self) -> float | None:
        """Duration of the most recent sequential polling cycle (if recorded)."""
        return self._get_duration("last_poll_start_mono", "last_poll_end_mono")

    def get_recent_errors(self) -> list[dict[str, Any]]:
        """Return a JSON-friendly copy of recent error triples."""
        out: list[dict[str, Any]] = []
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
                entry_id = self.config_entry.entry_id if self.config_entry else None
                token = fcm.get_fcm_token(entry_id)
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

    async def _async_update_data(self) -> list[dict[str, Any]]:
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
                    _LOGGER.debug(
                        "First run: waiting for FCM provider to become ready..."
                    )
                    try:
                        await asyncio.wait_for(fcm_evt.wait(), timeout=15.0)
                        _LOGGER.debug("FCM provider is ready; proceeding.")
                    except TimeoutError:
                        _LOGGER.warning(
                            "FCM provider not ready after 15s; proceeding anyway."
                        )
                self._startup_complete = True

            if self._is_fcm_ready_soft():
                self._set_fcm_status(FcmStatus.CONNECTED)
            elif self._fcm_last_stage < 2:
                self._set_fcm_status(
                    FcmStatus.DEGRADED,
                    reason="Push transport not ready; continuing with cached data",
                )

            # 1) Always fetch the lightweight FULL device list using native async API
            payload = await self.api.async_get_basic_device_list()

            # Success path: if we were in an auth error state, clear it now.
            self._set_auth_state(failed=False)
            self._set_api_status(ApiStatus.OK)

            # Normalize payloads that may arrive as mappings or sequences.
            devices_source: Any
            if isinstance(payload, Mapping):
                devices_source = payload.get("devices")
            else:
                devices_source = payload

            if devices_source is None:
                device_candidates: list[Any] = []
            elif isinstance(devices_source, Mapping):
                device_candidates = [devices_source]
            elif isinstance(devices_source, Sequence) and not isinstance(
                devices_source, (str, bytes, bytearray)
            ):
                device_candidates = list(devices_source)
            else:
                device_candidates = []

            filtered_devices: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for item in device_candidates:
                if not isinstance(item, Mapping):
                    continue
                normalized = dict(item)
                dev_id_value = normalized.get("id")
                if not isinstance(dev_id_value, str) or not dev_id_value.strip():
                    _LOGGER.debug(
                        "Skipping device without valid id (keys=%s)",
                        list(normalized)[:6],
                    )
                    continue
                dev_id = dev_id_value.strip()
                normalized["id"] = dev_id
                if dev_id in seen_ids:
                    # Duplicate policy: first-wins (skip subsequent duplicates).
                    _LOGGER.debug("Skipping duplicate device entry for id=%s", dev_id)
                    continue
                seen_ids.add(dev_id)
                filtered_devices.append(normalized)

            # Minimal hardening against false empties (keep prior behaviour)
            if not filtered_devices:
                self._empty_list_streak += 1
                if (
                    self._empty_list_streak < _EMPTY_LIST_QUORUM
                    and self._last_device_list
                ):
                    # Defer clearing once; keep previous view stable.
                    _LOGGER.debug(
                        "Successful empty device list received (%d/%d). Deferring clear until quorum is met.",
                        self._empty_list_streak,
                        _EMPTY_LIST_QUORUM,
                    )
                    filtered_devices = list(self._last_device_list)
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
                self._last_device_list = list(filtered_devices)

            # Presence TTL derives from the effective poll cadence
            effective_interval = max(
                self.location_poll_interval, self.min_poll_interval
            )
            self._presence_ttl_s = max(2 * effective_interval, 120)
            now_mono = time.monotonic()

            # Cold-start guard: if the very first seen list is empty, treat it as transient
            if not filtered_devices and self._last_nonempty_wall == 0.0:
                raise UpdateFailed(
                    "Cold start: empty device list; treating as transient."
                )

            # Maintain owner index for FCM fallback routing (entry-scoped).
            self._sync_owner_index(filtered_devices)

            ignored = self._get_ignored_set()

            # Record presence timestamps from the full list (unfiltered by ignore)
            if filtered_devices:
                for dev in filtered_devices:
                    dev_id = dev["id"]
                    self._present_last_seen[dev_id] = now_mono
                # Keep a diagnostics-only set mirroring the latest non-empty list
                self._present_device_ids = {
                    dev["id"] for dev in filtered_devices
                }
                self._last_nonempty_wall = now_mono
            # If the list is empty, leave _present_last_seen untouched; TTL will decide availability.

            # 2) Update internal name/capability caches for ALL devices
            name_cache = self._ensure_device_name_cache()
            for dev in filtered_devices:
                dev_id = dev["id"]
                raw_name = dev.get("name")
                if isinstance(raw_name, str) and raw_name.strip():
                    name_cache[dev_id] = raw_name
                elif dev_id not in name_cache:
                    name_cache[dev_id] = dev_id

                # Normalize and cache the "can ring" capability
                if "can_ring" in dev:
                    can_ring = bool(dev.get("can_ring"))
                    slot = self._device_caps.setdefault(dev_id, {})
                    slot["can_ring"] = can_ring

            # 2.5) Ensure Device Registry entries exist (service device + end-devices, namespaced)
            self._ensure_service_device_exists()
            created = self._ensure_registry_for_devices(filtered_devices, ignored)
            if created:
                _LOGGER.debug(
                    "Device Registry ensured/updated for %d device(s).", created
                )

            # 3) Decide whether to trigger a poll cycle (monotonic clock)
            # Build list of devices to POLL:
            # Poll devices that have at least one enabled DR entry for this config entry;
            # if a device has no DR entry yet, include it to allow initial discovery.
            devices_to_poll: list[dict[str, Any]] = []
            for dev in filtered_devices:
                dev_id = dev["id"]
                if dev_id in ignored:
                    continue
                if (
                    dev_id in self._enabled_poll_device_ids
                    or dev_id not in self._devices_with_entry
                ):
                    devices_to_poll.append(dev)

            # Apply per-device poll cooldowns
            if self._device_poll_cooldown_until and devices_to_poll:
                devices_to_poll = [
                    d
                    for d in devices_to_poll
                    if now_mono >= self._device_poll_cooldown_until.get(d["id"], 0.0)
                ]

            due = (now_mono - self._last_poll_mono) >= effective_interval
            if due and not self._is_polling and devices_to_poll:
                force_poll = False
                fcm_ready = self._is_fcm_ready_soft()
                if not fcm_ready:
                    # No baseline jump; schedule a short retry and escalate politely.
                    self._note_fcm_deferral(now_mono)
                    defer_started = self._fcm_defer_started_mono or 0.0
                    if defer_started:
                        elapsed = now_mono - defer_started
                        if elapsed >= _FCM_FALLBACK_POLL_AFTER_S:
                            force_poll = True
                        else:
                            self._schedule_short_retry(
                                min(5.0, effective_interval / 2.0)
                            )
                    else:
                        self._schedule_short_retry(
                            min(5.0, effective_interval / 2.0)
                        )
                elif self._fcm_defer_started_mono:
                    self._clear_fcm_deferral()

                if fcm_ready or force_poll:
                    if force_poll:
                        _LOGGER.warning(
                            "Push transport unavailable for %ds; forcing poll cycle.",
                            _FCM_FALLBACK_POLL_AFTER_S,
                        )
                    _LOGGER.debug(
                        "Scheduling background polling cycle (devices=%d, interval=%ds)",
                        len(devices_to_poll),
                        effective_interval,
                    )
                    self.hass.async_create_task(
                        self._async_start_poll_cycle(
                            devices_to_poll, force=force_poll
                        ),
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
            visible_devices = [
                dev for dev in filtered_devices if dev["id"] not in ignored
            ]
            for dev in visible_devices:
                dev_id = dev["id"]
                cached_name = name_cache.get(dev_id)
                name = dev.get("name")
                if cached_name and (not isinstance(name, str) or not name.strip()):
                    dev["name"] = cached_name
            self._refresh_subentry_index(visible_devices)
            snapshot = await self._async_build_device_snapshot_with_fallbacks(
                visible_devices
            )
            self._store_subentry_snapshots(snapshot)

            # 4.5) Close the initial discovery window once we have a non-empty full list
            if not self._initial_discovery_done and filtered_devices:
                self._initial_discovery_done = True
                _LOGGER.info(
                    "Initial discovery window closed; newly discovered devices will be created disabled by default."
                )

            _LOGGER.debug(
                "Returning %d device entries; next poll in ~%ds",
                len(snapshot),
                int(
                    max(
                        0,
                        effective_interval - (time.monotonic() - self._last_poll_mono),
                    )
                ),
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
            raise auth_exc
        except UpdateFailed as update_err:
            # Let pre-wrapped UpdateFailed bubble as-is after updating status
            self._set_api_status(
                ApiStatus.ERROR,
                reason=self._short_error_message(update_err),
            )
            raise
        except Exception as err:
            # Record and raise as UpdateFailed per coordinator contract
            self.note_error(err, where="_async_update_data")
            message = self._short_error_message(err)
            self._set_api_status(ApiStatus.ERROR, reason=message)
            _LOGGER.exception("Unexpected error during coordinator update")
            raise UpdateFailed(f"{type(err).__name__}: {err}") from err

    # ---------------------------- Polling Cycle -----------------------------
    async def _async_start_poll_cycle(
        self, devices: list[dict[str, Any]], *, force: bool = False
    ) -> None:
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
            # if readiness regressed between scheduling and execution, skip cleanly
            # unless we are explicitly forcing the cycle after a prolonged outage.
            if not self._is_fcm_ready_soft():
                if not force:
                    # No baseline jump; schedule a short retry and keep escalation ticking.
                    self._note_fcm_deferral(time.monotonic())
                    self._schedule_short_retry(5.0)
                    return
                _LOGGER.warning(
                    "Starting poll cycle without push transport; continuing in degraded mode."
                )
            elif self._fcm_defer_started_mono:
                # If we were deferring previously, clear the escalation timeline.
                self._clear_fcm_deferral()

            self._is_polling = True
            self.safe_update_metric("last_poll_start_mono", time.monotonic())
            _LOGGER.debug("Starting sequential poll of %d devices", len(devices))

            google_home_filter = self._get_google_home_filter()

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
                            _LOGGER.warning(
                                "No location data returned for %s", dev_name
                            )
                            continue

                        # --- Apply Google Home filter (keep parity with FCM push path) ---
                        # Consume coordinate substitution from the filter when needed.
                        semantic_name = location.get("semantic_name")
                        if semantic_name and google_home_filter is not None:
                            try:
                                (
                                    should_filter,
                                    replacement_attrs,
                                ) = google_home_filter.should_filter_detection(
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
                                        location["latitude"] = replacement_attrs.get(
                                            "latitude"
                                        )
                                        location["longitude"] = replacement_attrs.get(
                                            "longitude"
                                        )
                                    if (
                                        "radius" in replacement_attrs
                                        and replacement_attrs.get("radius") is not None
                                    ):
                                        location["accuracy"] = replacement_attrs.get(
                                            "radius"
                                        )
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

                    except TimeoutError as terr:
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
                        _LOGGER.error(
                            "Failed to get location for %s: %s", dev_name, err
                        )
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
                    d
                    for d in (self._last_device_list or [])
                    if d.get("id") not in ignored
                ]
                end_snapshot = self._build_snapshot_from_cache(
                    visible_devices, wall_now=time.time()
                )
                self.async_set_updated_data(end_snapshot)

    # ---------------------------- Snapshot helpers --------------------------
    def _build_base_snapshot_entry(self, device_dict: dict[str, Any]) -> dict[str, Any]:
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

    def _update_entry_from_cache(self, entry: dict[str, Any], wall_now: float) -> bool:
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
        self, devices: list[dict[str, Any]], wall_now: float
    ) -> list[dict[str, Any]]:
        """Build a lightweight snapshot using only the in-memory cache.

        This never touches HA state or the database; it is safe in background tasks.

        Args:
            devices: A list of device dictionaries to include in the snapshot.
            wall_now: The current wall-clock time as a float timestamp.

        Returns:
            A list of device state dictionaries built from the cache.
        """
        snapshot: list[dict[str, Any]] = []
        for dev in devices:
            entry = self._build_base_snapshot_entry(dev)
            # If cache has info, update status accordingly; otherwise keep default status.
            self._update_entry_from_cache(entry, wall_now)
            snapshot.append(entry)
        return snapshot

    async def _async_build_device_snapshot_with_fallbacks(
        self, devices: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Build a snapshot using cache, HA state and (optionally) history fallback.

        Args:
            devices: A list of device dictionaries to build the snapshot for.

        Returns:
            A complete list of device state dictionaries with fallbacks applied.
        """
        snapshot: list[dict[str, Any]] = []
        wall_now = time.time()

        for dev in devices:
            entry = self._build_base_snapshot_entry(dev)

            # Prefer cached result
            if self._update_entry_from_cache(entry, wall_now):
                entry = _sanitize_decoder_row(entry)
                snapshot.append(entry)
                continue

            # No cache -> Registry + State (cheap, non-blocking)
            dev_id = entry["device_id"]
            registry_entry = self._find_tracker_entity_entry(dev_id)
            if registry_entry is None:
                _LOGGER.debug(
                    "Skipping state/history fallback for '%s' because tracker cache entry is unavailable.",
                    entry["name"],
                )
                snapshot.append(entry)
                continue

            entity_id = registry_entry.entity_id
            if not entity_id:
                _LOGGER.debug(
                    "Entity registry entry missing entity_id for tracker '%s' (unique_id=%s)",
                    entry["name"],
                    registry_entry.unique_id,
                )
                snapshot.append(entry)
                continue

            state = self.hass.states.get(entity_id)
            if state:
                lat = state.attributes.get("latitude")
                lon = state.attributes.get("longitude")
                acc = state.attributes.get("gps_accuracy")
                last_seen_ts = _resolve_last_seen_from_attributes(
                    state.attributes, state.last_updated.timestamp()
                )
                if lat is not None and lon is not None:
                    entry.update(
                        {
                            "latitude": lat,
                            "longitude": lon,
                            "accuracy": acc,
                            "last_seen": last_seen_ts,
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
            await self._cache.async_set_cached_value(
                "integration_stats", self.stats.copy()
            )
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
            create_task = getattr(
                self.hass, "async_create_background_task", self.hass.async_create_task
            )
            self._stats_save_task = create_task(
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
            try:
                self.async_update_listeners()
            except Exception as err:
                _LOGGER.debug("Stats listener notification failed: %s", err)
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
    def get_device_location_data(self, device_id: str) -> dict[str, Any] | None:
        """Return current cached location dict for a device (or None).

        Args:
            device_id: The canonical ID of the device.

        Returns:
            A dictionary of location data or None if not found.
        """
        return self._device_location_data.get(device_id)

    def prime_device_location_cache(self, device_id: str, data: dict[str, Any]) -> None:
        """Seed/update the location cache for a device with (lat/lon/accuracy).

        Args:
            device_id: The canonical ID of the device.
            data: A dictionary containing latitude, longitude, and accuracy.
        """
        slot = self._device_location_data.get(device_id, {})
        slot.update(
            {
                k: v
                for k, v in data.items()
                if k in ("latitude", "longitude", "accuracy")
            }
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

    def update_device_cache(
        self, device_id: str, location_data: dict[str, Any]
    ) -> None:
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
            _LOGGER.debug(
                "Ignored cache update for %s: payload is not a dict", device_id
            )
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

        # Preserve recency/coordinate fidelity before committing to cache.
        slot = self._merge_with_existing_cache_row(device_id, slot)

        name_cache = self._ensure_device_name_cache()

        # Keep human-friendly name mapping up-to-date if provided alongside
        name = slot.get("name")
        if isinstance(name, str) and name:
            name_cache[device_id] = name

        self._device_location_data[device_id] = slot
        # Increment background updates to account for push/manual commits.
        self.increment_stat("background_updates")

    def _merge_with_existing_cache_row(
        self, device_id: str, incoming: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge accepted payloads with cached rows while respecting recency."""

        existing = self._device_location_data.get(device_id)
        if not existing:
            return incoming

        merged = dict(existing)
        merged.update(incoming)

        existing_seen = _normalize_epoch_seconds(existing.get("last_seen"))
        incoming_seen = _normalize_epoch_seconds(incoming.get("last_seen"))

        # Keep monotonic last_seen timestamps when payloads arrive without a newer value.
        if existing_seen is not None and (
            incoming_seen is None or incoming_seen < existing_seen
        ):
            merged["last_seen"] = existing.get("last_seen")
            if existing.get("last_seen_utc") is not None:
                merged["last_seen_utc"] = existing.get("last_seen_utc")
        elif (
            incoming_seen is not None
            and existing_seen is not None
            and incoming_seen == existing_seen
            and merged.get("last_seen_utc") is None
            and existing.get("last_seen_utc") is not None
        ):
            merged["last_seen_utc"] = existing.get("last_seen_utc")

        for coord_field in ("latitude", "longitude", "accuracy", "altitude"):
            if (
                merged.get(coord_field) is None
                and existing.get(coord_field) is not None
            ):
                merged[coord_field] = existing.get(coord_field)

        return merged

    # ---------------------------- Significance / gating ----------------------
    def _haversine_distance(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> float:
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

    def _is_significant_update(self, device_id: str, new_data: dict[str, Any]) -> bool:
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
            `new_seen` is in the far past (< 2000) or beyond the accepted future drift
            tolerance.
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
            if n_seen_norm > time.time() + MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S:
                self.increment_stat("future_ts_drop_count")
                return False

        e_seen_norm = _normalize_epoch_seconds(existing.get("last_seen"))

        # Stale-guard: reuse invalid_ts_drop_count for "older than existing"
        if (
            n_seen_norm is not None
            and e_seen_norm is not None
            and n_seen_norm < e_seen_norm
        ):
            self.increment_stat("invalid_ts_drop_count")
            return False

        if (
            n_seen_norm is not None
            and e_seen_norm is not None
            and n_seen_norm > e_seen_norm
        ):
            return True

        if (
            n_seen_norm is not None
            and e_seen_norm is not None
            and n_seen_norm == e_seen_norm
        ):
            n_lat, n_lon = new_data.get("latitude"), new_data.get("longitude")
            e_lat, e_lon = existing.get("latitude"), existing.get("longitude")
            if all(isinstance(v, (int, float)) for v in (n_lat, n_lon, e_lat, e_lon)):
                try:
                    e_lat_f = float(cast(float, e_lat))
                    e_lon_f = float(cast(float, e_lon))
                    n_lat_f = float(cast(float, n_lat))
                    n_lon_f = float(cast(float, n_lon))
                    dist = self._haversine_distance(e_lat_f, e_lon_f, n_lat_f, n_lon_f)
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

            n_alt = new_data.get("altitude")
            e_alt = existing.get("altitude")
            if n_alt is None or e_alt is None:
                if n_alt != e_alt:
                    return True
            else:
                try:
                    n_alt_f = float(n_alt)
                    e_alt_f = float(e_alt)
                except (TypeError, ValueError):
                    if n_alt != e_alt:
                        return True
                else:
                    if not (math.isfinite(n_alt_f) and math.isfinite(e_alt_f)):
                        if n_alt_f != e_alt_f:
                            return True
                    elif abs(n_alt_f - e_alt_f) >= _ALTITUDE_SIGNIFICANT_DELTA_M:
                        return True

        # Source or qualitative changes can still be valuable.
        if new_data.get("is_own_report") != existing.get("is_own_report"):
            return True
        if new_data.get("status") != existing.get("status"):
            return True
        if new_data.get("semantic_name") != existing.get("semantic_name"):
            return True

        return False

    def get_device_last_seen(self, device_id: str) -> datetime | None:
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
            return datetime.fromtimestamp(float(ts), tz=UTC)
        except Exception:
            return None

    def get_device_display_name(self, device_id: str) -> str | None:
        """Return the human-readable device name if known.

        Args:
            device_id: The canonical ID of the device.

        Returns:
            The display name as a string, or None.
        """
        return self._ensure_device_name_cache().get(device_id)

    def get_device_name_map(self) -> dict[str, str]:
        """Return a shallow copy of the internal device-id -> name mapping.

        Returns:
            A dictionary mapping device IDs to their names.
        """
        return dict(self._ensure_device_name_cache())

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

    def get_absent_device_ids(self) -> list[str]:
        """Return ids known by name/cache that are **expired** under the presence TTL.

        Useful for diagnostics. This does not imply automatic removal.
        """
        now_mono = time.monotonic()

        def expired(dev_id: str) -> bool:
            ts = self._present_last_seen.get(dev_id, 0.0)
            return (not ts) or ((now_mono - float(ts)) > float(self._presence_ttl_s))

        name_cache = self._ensure_device_name_cache()
        known = set(name_cache) | set(self._device_location_data)
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
        self._ensure_device_name_cache().pop(device_id, None)
        self._device_caps.pop(device_id, None)
        self._locate_inflight.discard(device_id)
        self._locate_cooldown_until.pop(device_id, None)
        self._device_poll_cooldown_until.pop(device_id, None)
        self._present_device_ids.discard(device_id)
        self._present_last_seen.pop(device_id, None)
        # Rebuild the cached snapshot without the purged device
        current_snapshot: list[dict[str, Any]] = []
        for row in list(self.data or []):
            if not isinstance(row, dict):
                continue
            if row.get("device_id") == device_id or row.get("id") == device_id:
                continue
            current_snapshot.append(dict(row))

        devices_stub = [
            {"id": entry.get("device_id") or entry.get("id"), "name": entry.get("name")}
            for entry in current_snapshot
            if isinstance(entry.get("device_id") or entry.get("id"), str)
        ]
        self._refresh_subentry_index(devices_stub)
        self._store_subentry_snapshots(current_snapshot)
        self.async_set_updated_data(current_snapshot)

    # ---------------------------- Push updates ------------------------------
    def push_updated(
        self,
        device_ids: list[str] | None = None,
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
            self._run_on_hass_loop(
                self.push_updated, device_ids, reset_baseline=reset_baseline
            )
            return

        wall_now = time.time()
        self._set_fcm_status(FcmStatus.CONNECTED)
        if reset_baseline:
            self._last_poll_mono = time.monotonic()  # optional: reset poll timer

        # Choose device ids for the snapshot
        name_cache = self._ensure_device_name_cache()

        if device_ids:
            ids = device_ids
        else:
            # union of all known names and cached locations
            ids = list({*name_cache.keys(), *self._device_location_data.keys()})

        # Apply ignore filter first to avoid touching presence for ignored devices.
        ignored = self._get_ignored_set()
        ids = [d for d in ids if d not in ignored]

        # Touch presence timestamps for pushed devices (keeps presence stable)
        now_mono = time.monotonic()
        for dev_id in ids:
            self._present_last_seen[dev_id] = now_mono

        # Build "devices" stubs from id->name mapping
        devices_stub: list[dict[str, Any]] = []
        for dev_id in ids:
            cached_name = name_cache.get(dev_id)
            if isinstance(cached_name, str) and cached_name.strip():
                name = cached_name if cached_name != dev_id else "Google Find My Device"
            else:
                name = "Google Find My Device"
            devices_stub.append({"id": dev_id, "name": name})

        snapshot = self._build_snapshot_from_cache(devices_stub, wall_now=wall_now)
        self._refresh_subentry_index(devices_stub)
        self._store_subentry_snapshots(snapshot)
        self.async_set_updated_data(snapshot)
        _LOGGER.debug(
            "Pushed snapshot for %d device(s) via push_updated()", len(snapshot)
        )

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
                _LOGGER.debug(
                    "Push readiness: cooldown active -> treating as not ready"
                )
            self._push_ready_memo = False
            return False

        ready: bool | None = None
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
        _LOGGER.debug(
            "Entering push cooldown for %ss after transport failure", cooldown_s
        )
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
            _LOGGER.debug(
                "can_play_sound(%s) -> %s (from capability can_ring)", device_id, res
            )
            return res

        # 2) Short-circuit if push transport is not ready.
        ready = self._api_push_ready()
        if ready is False:
            # Respect explicit cooldowns triggered after recent failures, but do not
            # hide the action solely because push transport appears disconnected.
            if time.monotonic() < self._push_cooldown_until:
                _LOGGER.debug(
                    "can_play_sound(%s) -> False (push cooldown active)", device_id
                )
                return False
            _LOGGER.debug(
                "can_play_sound(%s): push not ready, keeping entity available", device_id
            )

        # 3) Optimistic final decision based on whether we know the device.
        name_cache = self._ensure_device_name_cache()
        is_known = (device_id in name_cache or device_id in self._device_location_data)
        if is_known:
            _LOGGER.debug(
                "can_play_sound(%s) -> True (optimistic; known device, push_ready=%s)",
                device_id,
                ready,
            )
            return True

        _LOGGER.debug(
            "can_play_sound(%s) -> True (optimistic final fallback)", device_id
        )
        return True

    # ---------------------------- Public control / Locate gating ------------
    def can_request_location(self, device_id: str) -> bool:
        """Return True if a manual 'Locate now' request is currently allowed.

        Gate conditions:
          - device not ignored,
          - no sequential polling in progress,
          - no in-flight locate for the device,
          - per-device cooldown (lower-bounded by DEFAULT_MIN_POLL_INTERVAL) not active.
        Push readiness is checked lazily when submitting the request so the UI
        can stay responsive while the transport recovers.
        """
        # Block manual locate for ignored devices.
        if self.is_ignored(device_id):
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
        ignored_devices: list[str] | None = None,
        location_poll_interval: int | None = None,
        device_poll_delay: int | None = None,
        min_poll_interval: int | None = None,
        min_accuracy_threshold: int | None = None,
        movement_threshold: int | None = None,
        allow_history_fallback: bool | None = None,
        contributor_mode: str | None = None,
        contributor_mode_switch_epoch: int | None = None,
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
            contributor_mode: Updated contributor mode ("high_traffic" or "in_all_areas").
            contributor_mode_switch_epoch: Epoch timestamp when the mode last changed.
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
                _LOGGER.warning(
                    "Ignoring invalid device_poll_delay=%r", device_poll_delay
                )

        if min_poll_interval is not None:
            try:
                self.min_poll_interval = max(1, int(min_poll_interval))
            except (TypeError, ValueError):
                _LOGGER.warning(
                    "Ignoring invalid min_poll_interval=%r", min_poll_interval
                )

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
                _LOGGER.warning(
                    "Ignoring invalid movement_threshold=%r", movement_threshold
                )

        if allow_history_fallback is not None:
            self.allow_history_fallback = bool(allow_history_fallback)

        if contributor_mode is not None:
            normalized_mode = self._sanitize_contributor_mode(contributor_mode)
            if (
                contributor_mode_switch_epoch is None
                or contributor_mode_switch_epoch <= 0
            ):
                contributor_mode_switch_epoch = int(time.time())
            epoch = int(contributor_mode_switch_epoch)
            if (
                normalized_mode != self._contributor_mode
                or epoch != self._contributor_mode_switch_epoch
            ):
                self._contributor_mode = normalized_mode
                self._contributor_mode_switch_epoch = epoch
                self.api.set_contributor_mode(
                    normalized_mode, switch_epoch=self._contributor_mode_switch_epoch
                )
                self._async_persist_contributor_mode()

        # Settings adjustments may change per-subentry views
        self._refresh_subentry_index()

    def force_poll_due(self) -> None:
        """Force the next poll to be due immediately (no private access required externally)."""
        effective_interval = max(self.location_poll_interval, self.min_poll_interval)
        # Move the baseline back so that (now - _last_poll_mono) >= effective_interval
        self._last_poll_mono = time.monotonic() - float(effective_interval)

    # ---------------------------- Passthrough API ---------------------------
    async def async_locate_device(self, device_id: str) -> dict[str, Any]:
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
                "Manual locate for %s is currently disabled (in-flight, cooldown, or polling).",
                name,
            )
            return {}

        if not self._api_push_ready():
            _LOGGER.warning(
                "Manual locate for %s is currently disabled (push transport not ready).",
                name,
            )
            return {}

        # Enter in-flight and set a lower-bound cooldown window
        self._locate_inflight.add(device_id)
        self._locate_cooldown_until[device_id] = time.monotonic() + float(
            DEFAULT_MIN_POLL_INTERVAL
        )
        self.async_set_updated_data(self.data)

        google_home_filter = self._get_google_home_filter()

        try:
            location_data = await self.api.async_get_device_location(device_id, name)

            # Success path: clear any auth error state
            self._set_auth_state(failed=False)

            if not location_data:
                return {}

            # --- Parity with polling path: Google Home semantic spam filter --------
            # Consume coordinate substitution from the filter when needed.
            semantic_name = location_data.get("semantic_name")
            if semantic_name and google_home_filter is not None:
                try:
                    (should_filter, replacement_attrs) = (
                        google_home_filter.should_filter_detection(
                            device_id, semantic_name
                        )
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
                        if (
                            "latitude" in replacement_attrs
                            and "longitude" in replacement_attrs
                        ):
                            location_data["latitude"] = replacement_attrs.get(
                                "latitude"
                            )
                            location_data["longitude"] = replacement_attrs.get(
                                "longitude"
                            )
                        if (
                            "radius" in replacement_attrs
                            and replacement_attrs.get("radius") is not None
                        ):
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
                    location_data["status"] = (
                        "Semantic location; preserving previous coordinates"
                    )

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
            dynamic_guess = max(
                float(DEFAULT_MIN_POLL_INTERVAL), float(self.location_poll_interval)
            )
            owner_cooldown = _clamp(
                dynamic_guess, _COOLDOWN_OWNER_MIN_S, _COOLDOWN_OWNER_MAX_S
            )
            now_mono = time.monotonic()
            # Extend (not overwrite) any type-aware cooldown applied above
            existing_deadline = self._device_poll_cooldown_until.get(device_id, 0.0)
            owner_deadline = now_mono + owner_cooldown
            self._device_poll_cooldown_until[device_id] = max(
                existing_deadline, owner_deadline
            )
            self._locate_cooldown_until[device_id] = max(
                self._locate_cooldown_until.get(device_id, 0.0), owner_deadline
            )

            # Touch presence for the device (a fresh interaction implies it exists)
            self._present_last_seen[device_id] = now_mono

            self.push_updated([device_id])
            return location_data or {}
        except ConfigEntryAuthFailed as auth_exc:
            # Mark error and request a refresh; no need to re-raise here for manual action.
            self._set_auth_state(
                failed=True, reason=f"Auth failed during manual locate: {auth_exc}"
            )
            try:
                await self.async_request_refresh()
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
            _LOGGER.debug(
                "Suppressing play_sound call for %s: capability/push not ready",
                device_id,
            )
            return False
        try:
            ok = await self.api.async_play_sound(device_id)
            if not ok:
                self._note_push_transport_problem()
            # Success implies credentials worked
            self._set_auth_state(failed=False)
            return bool(ok)
        except ConfigEntryAuthFailed as auth_exc:
            self._set_auth_state(
                failed=True, reason=f"Auth failed during play_sound: {auth_exc}"
            )
            try:
                await self.async_request_refresh()
            except Exception:
                pass
            return False
        except Exception as err:
            _LOGGER.debug(
                "async_play_sound raised for %s: %s; entering cooldown", device_id, err
            )
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
            _LOGGER.debug(
                "Suppressing stop_sound call for %s: push not ready", device_id
            )
            return False
        try:
            ok = await self.api.async_stop_sound(device_id)
            if not ok:
                self._note_push_transport_problem()
            # Success implies credentials worked
            self._set_auth_state(failed=False)
            return bool(ok)
        except ConfigEntryAuthFailed as auth_exc:
            self._set_auth_state(
                failed=True, reason=f"Auth failed during stop_sound: {auth_exc}"
            )
            try:
                await self.async_request_refresh()
            except Exception:
                pass
            return False
        except Exception as err:
            _LOGGER.debug(
                "async_stop_sound raised for %s: %s; entering cooldown", device_id, err
            )
            self.note_error(err, where="async_stop_sound", device=device_id)
            self._note_push_transport_problem()
            return False
