# custom_components/googlefindmy/const.py
"""Constants for Google Find My Device integration.

All constants defined here are intended to be import-safe across the integration.
Keep comments and docstrings in English; user-facing strings belong in translations.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Mapping, Sequence
from typing import Final, Literal

# --------------------------------------------------------------------------------------
# Core identifiers
# --------------------------------------------------------------------------------------
DOMAIN: str = "googlefindmy"
# Shared hass.data key for the global EID resolver instance
DATA_EID_RESOLVER: Final[Literal["eid_resolver"]] = "eid_resolver"
# Latest config entry schema version handled by this integration.
CONFIG_ENTRY_VERSION: int = 2
# Integration version. MUST be bumped together with manifest.json "version" and
# pyproject.toml [tool.poetry] version on every release; the three are a coupled
# triple, kept in sync automatically by semantic-release (pyproject.toml
# [tool.semantic_release] version_variables). manifest.json is strict JSON
# (hassfest-validated) and cannot carry this cross-reference, so the manifest side is
# anchored in this directory's AGENTS.md ("Version bump touches three files").
# pyproject.toml carries the reverse reference in a TOML comment.
# NOTE: no ": str" annotation on purpose -- semantic-release's version_variables
# regex only matches `NAME = "x"`, not `NAME: str = "x"`. Re-adding the annotation
# would silently skip this file on the automated version bump.
INTEGRATION_VERSION = "1.7.15.2"

# --------------------------------------------------------------------------------------
# Shared textual constants
# --------------------------------------------------------------------------------------
# Use the Greek small letter mu instead of the deprecated micro sign.
MICRO: str = "\u03bc"

# --------------------------------------------------------------------------------------
# Service device metadata & helpers (to enforce consistent identifiers across platforms)
# --------------------------------------------------------------------------------------
SERVICE_DEVICE_NAME: str = "Google Find My Integration"
SERVICE_DEVICE_MODEL: str = "Find My Device Integration"
SERVICE_DEVICE_MANUFACTURER: str = "BSkando"
SERVICE_DEVICE_TRANSLATION_KEY: str = "google_find_hub_service"

# Identifier pattern for the per-entry "integration device"
SERVICE_DEVICE_IDENTIFIER_PREFIX: str = "integration_"
# Legacy, pre-namespaced identifier used by older releases (kept for migrations)
LEGACY_SERVICE_IDENTIFIER: str = "integration"

# --------------------------------------------------------------------------------------
# Entity registry subentry helpers
# --------------------------------------------------------------------------------------
SERVICE_SUBENTRY_KEY: str = "service"
TRACKER_SUBENTRY_KEY: str = "core_tracking"
# Config entry data key reserved for subentry identifiers. Hub/root entries
# should store ``None`` to avoid being misclassified as a subentry during
# discovery lookups.
DATA_SUBENTRY_KEY: str = "subentry_key"
SUBENTRY_TYPE_SERVICE: str = "service"
SUBENTRY_TYPE_HUB: str = "hub"
SUBENTRY_TYPE_TRACKER: str = "tracker"
SERVICE_SUBENTRY_TRANSLATION_KEY: str = SERVICE_SUBENTRY_KEY
TRACKER_SUBENTRY_TRANSLATION_KEY: str = TRACKER_SUBENTRY_KEY

# Canonical feature lists for config subentries (kept sorted and import-safe)
TRACKER_FEATURE_PLATFORMS: tuple[str, ...] = (
    "button",
    "device_tracker",
    "sensor",
)
SERVICE_FEATURE_PLATFORMS: tuple[str, ...] = (
    "binary_sensor",
    "button",
    "sensor",
)


def service_device_identifier(entry_id: str) -> tuple[str, str]:
    """Return the (domain, identifier) tuple for the per-entry 'service device'.

    Args:
        entry_id: Home Assistant config entry id.

    Returns:
        A tuple suitable for DeviceInfo.identifiers, e.g. ('googlefindmy', 'integration_<entry_id>')
    """
    return (DOMAIN, f"{SERVICE_DEVICE_IDENTIFIER_PREFIX}{entry_id}")


# --------------------------------------------------------------------------------------
# Configuration keys (data vs. options separation)
# NOTE: Keep keys stable to avoid migration churn across releases.
# --------------------------------------------------------------------------------------
# Data (immutable / credentials): stored in config_entry.data
CONF_OAUTH_TOKEN: str = "oauth_token"  # kept for backward compatibility
# NOTE: This is a TokenCache key (persistent, rotating credential stored in HA Store),
# NOT a config_entry.data key. Do not persist this inside entry.data.
DATA_AAS_TOKEN: str = "aas_token"  # AAS token (TokenCache key; not in entry.data)
CONF_GOOGLE_EMAIL: str = "google_email"  # helper key when individual tokens are used
DATA_SECRET_BUNDLE: str = "secrets_data"  # full GoogleFindMyTools secrets.json content
DATA_AUTH_METHOD: str = "auth_method"  # "secrets_json" | "individual_tokens"

# Options (user-changeable): stored in config_entry.options
# (tracked_devices removed in Step 2; device inclusion is managed via HA device enable/disable)
OPT_LOCATION_POLL_INTERVAL: str = "location_poll_interval"
OPT_DEVICE_POLL_DELAY: str = "device_poll_delay"
OPT_MIN_POLL_INTERVAL: str = "min_poll_interval"
OPT_ALLOW_HISTORY_FALLBACK: str = "allow_history_fallback"
OPT_ENABLE_STATS_ENTITIES: str = "enable_stats_entities"
OPT_GOOGLE_HOME_FILTER_ENABLED: str = "google_home_filter_enabled"
OPT_GOOGLE_HOME_FILTER_KEYWORDS: str = "google_home_filter_keywords"
OPT_MAP_VIEW_TOKEN_EXPIRATION: str = "map_view_token_expiration"
OPT_SEMANTIC_LOCATIONS: str = "semantic_locations"
OPT_CONTRIBUTOR_MODE: str = "contributor_mode"
OPT_IGNORED_DEVICES: str = "ignored_devices"
OPT_DELETE_CACHES_ON_REMOVE: str = "delete_caches_on_remove"
OPT_STALE_THRESHOLD: str = "stale_threshold"
OPT_SHOW_LOCATION_AGE: str = "show_location_age"
OPT_SPEED_GATE_ENABLED: str = "speed_gate_enabled"
OPT_ROUNDTRIP_CONFIRM: str = "roundtrip_confirm_enabled"
# Legacy option key - kept for reading old configurations, no longer used
OPT_STALE_THRESHOLD_ENABLED: str = "stale_threshold_enabled"

# Canonical list of option keys supported by the integration (without tracked_devices)
OPTION_KEYS: tuple[str, ...] = (
    OPT_IGNORED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_POLL_INTERVAL,
    OPT_ALLOW_HISTORY_FALLBACK,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_SEMANTIC_LOCATIONS,
    OPT_DELETE_CACHES_ON_REMOVE,
    OPT_CONTRIBUTOR_MODE,
    OPT_STALE_THRESHOLD,
    OPT_SHOW_LOCATION_AGE,
    OPT_SPEED_GATE_ENABLED,
    OPT_ROUNDTRIP_CONFIRM,
)

# Keys which may exist historically in entry.data and should be soft-copied to entry.options
MIGRATE_DATA_KEYS_TO_OPTIONS: tuple[str, ...] = OPTION_KEYS

# --------------------------------------------------------------------------------------
# Defaults (aligned with the current implementation; adjust carefully)
# --------------------------------------------------------------------------------------
UPDATE_INTERVAL: int = 60  # seconds; DataUpdateCoordinator "tick" (lightweight)
# Minimum spacing for full device list refreshes (seconds); keeps discovery cheap
# while allowing per-device polling to stay responsive.
DEVICE_LIST_POLL_INTERVAL: int = 300

# Polling cadence
DEFAULT_LOCATION_POLL_INTERVAL: int = 300  # seconds; start a new polling cycle
DEFAULT_DEVICE_POLL_DELAY: int = 5  # seconds; inter-device delay within one cycle
DEFAULT_MIN_POLL_INTERVAL: int = 60  # seconds; hard lower bound between cycles

# Manual locate policy (button/service)
LOCATE_COOLDOWN_S: int = DEFAULT_MIN_POLL_INTERVAL
"""Cooldown window (seconds) applied after a manual locate trigger."""

# Token regeneration policy (buttons)
TOKEN_REFRESH_COOLDOWN_S: int = 180
"""Cooldown window (seconds) between token regeneration requests (3 minutes).

This window is applied per entry and per token type, so the FCM and ADM
regeneration buttons are rate-limited independently (a refresh of one does
not disable the sibling button). See ``token_refresh._cooldown_key``.
"""

# EID resolver refresh trigger debounce policy
_EID_REFRESH_DEBOUNCE_S: float = 3.0
"""Debounce window (seconds) coalescing EID-resolver refresh *triggers*.

Several read-only call sites and one inline cache path each request a resolver
refresh whenever an active device set or identity changes. Under an FCM burst
these requests arrive back-to-back, and without coalescing each one spawns its
own ``async_create_task(resolver.async_refresh())``. This window collapses such
bursts to a single scheduled task.

The value (3.0 s, sensible band 2-5 s) is deliberately small relative to the
device poll delay (``device_poll_delay`` 5-15 s) and the location poll interval
(``location_poll_interval`` 300-600 s), so a legitimate change is still picked
up promptly while same-burst triggers within 3 s coalesce. The debounce only
delays *task creation*; the actual rebuild-vs-skip decision stays with the
AP-B1 skip guard inside the resolver, so a genuine change is never dropped.
"""

# Quality/logic thresholds
DEFAULT_ALLOW_HISTORY_FALLBACK: bool = False
DEFAULT_SEMANTIC_DETECTION_RADIUS: float = (
    50.0  # meters; soft floor for semantic locations
)

# GPS accuracy fallback for invalid/missing values.
# When accuracy is reported as 0.0m, negative, NaN, or Inf, it indicates missing
# or corrupted data (0.0m GPS accuracy is physically impossible - real GPS: 3-50m).
# This fallback represents a conservative estimate based on:
#   - Bluetooth range: ~40-80m (tracker could be anywhere within BLE range)
#   - GPS error margin: ~10-30m (finder device uncertainty)
# Using 50m prevents the "Fusion Lock-in" effect where 0.0m gets extreme weight
# in weighted averaging (1/0² vs 1/20² = infinite vs 0.0025), while still being
# easily overridden by real GPS fixes.
DEFAULT_FALLBACK_ACCURACY_M: float = 50.0

# Location timestamp acceptance window
MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S: float = 24 * 60 * 60  # 24 hours

# Stats entities
DEFAULT_ENABLE_STATS_ENTITIES: bool = True

# Google Home filter
DEFAULT_GOOGLE_HOME_FILTER_ENABLED: bool = True
DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS: str = (
    "nest,google,home,mini,hub,display,chromecast,speaker"
)
GOOGLE_HOME_SPAM_THRESHOLD_MINUTES: int = 15  # debounce for repeated detections

# Map View token behavior
# Default remains "no expiration" for backwards compatibility.
DEFAULT_MAP_VIEW_TOKEN_EXPIRATION: bool = False

DEFAULT_DELETE_CACHES_ON_REMOVE: bool = True

# Stale threshold: After this many seconds without a location update,
# the tracker state becomes "unknown". This is always enabled.
# Users who need the last known location can use the "Last Location" entity.
#
# Situation (empirically observed, HA history July 2026):
# A stationary device at home (smartphone with a dark display, or a Bluetooth
# tag scanned only by the household's own dark-display phones) reports through
# the FMDN network roughly every ~30 minutes. Every spurious "unknown" flip in
# the field carried last_seen ~1800-1835 s old, i.e. exactly at the previous
# 1800 s threshold: the threshold sat directly on the natural home reporting
# cadence, so any minor delay tipped an otherwise-present device into "stale"
# and blanked its coordinates until the next report arrived.
#
# The earlier 1800 s were calibrated to ACTIVE FMDN tracker-scan statistics
# (median ~3.4 min, p95 ~8 min, p99 ~14 min) that hold when many foreign
# phones continuously scan a tracker. They do NOT hold for a device sitting at
# home: a dozing smartphone reports its own position only occasionally, and a
# tag at home is seen only by the few (also dozing) household phones. A shorter
# poll does not help - Google returns the last reported fix for a dozing
# device, so last_seen only advances when the device itself reports.
#
# Rationale for the default: 3900 s (65 minutes) ~= 2x the
# observed ~30 min home cadence plus margin to tolerate one missed report
# cycle, which removes the flapping for both smartphones and tags while still
# flagging a genuinely absent device within ~1 h. The threshold stays
# user-configurable (min 300 s, max 86400 s); only the default was mistuned.
#
# Note: EID rotation (1024s) is NOT relevant here.
# Default: 3900 seconds (65 minutes) - ~2x the observed home reporting cadence
# Minimum: 300 seconds (5 minutes) - allows ~2-3 active-scan update cycles
DEFAULT_STALE_THRESHOLD: int = 3900
DEFAULT_SHOW_LOCATION_AGE: bool = True

DEFAULT_SPEED_GATE_ENABLED: bool = True
# Kinematic plausibility cap (m/s). ~1440 km/h: above the record jetstream
# airliner ground speed (~369 m/s, BA Feb-2020) and far above cruise/ICE/car,
# so even a strong-tailwind flight passes; blocks the physically impossible
# FMDN crowd-report "teleport" (Discussion #177). Own-report GPS fixes bypass
# the gate entirely (crowd-awareness), so this cap only bounds crowd reports.
DEFAULT_MAX_PLAUSIBLE_SPEED_MPS: float = 400.0

# Round-trip escape hatch (Q2-A, Discussion #177 F-CODEX-4). When the speed gate
# accepts a wide jump A->B (device seen far away), it remembers A as a return
# anchor. A later crowd fix that the forward gate would otherwise reject as a
# "teleport" is accepted instead if it lands back near A within the TTL, which
# recovers a legitimate return trip without stranding the tracker. Anchor is set
# only from a reliable A (never an estimated/accuracy-less fix), one-shot, RAM-only.
DEFAULT_ROUNDTRIP_CONFIRM: bool = True
# Anchor lifetime (s). 250 km/400 m/s general recovery ~10.4 min < 15 min, so the
# window bounds A<->B ping-pong from stale echo reports without rejecting a real
# slow move as a round trip.
ROUND_TRIP_TTL_S: int = 900
# Fixed return radius (m). Deliberately NOT radius_sum (which inflates to ~400 m on
# a double fallback and couples tolerance to unreliable crowd accuracy). Value equals
# the established BT-tracker fallback DEFAULT_ACCURACY_FALLBACK_M (200.0); kept as an
# own constant (not an alias) so it stays one-line adjustable.
ROUND_TRIP_ANCHOR_RADIUS_M: float = 200.0

CONTRIBUTOR_MODE_HIGH_TRAFFIC: str = "high_traffic"
CONTRIBUTOR_MODE_IN_ALL_AREAS: str = "in_all_areas"
DEFAULT_CONTRIBUTOR_MODE: str = CONTRIBUTOR_MODE_IN_ALL_AREAS

CACHE_KEY_CONTRIBUTOR_MODE: str = "nova_contributor_mode"
CACHE_KEY_LAST_MODE_SWITCH: str = "nova_last_network_mode_switch"

# Aggregate defaults dictionary for option-first reading patterns
DEFAULT_OPTIONS: dict[str, object] = {
    # Store ignored devices as mapping {device_id: {"name": str, "aliases": [str], "ignored_at": int, "source": str}}
    # Backwards-compatible: old list[str] or dict[str,str] is auto-migrated on first write.
    OPT_IGNORED_DEVICES: {},
    OPT_LOCATION_POLL_INTERVAL: DEFAULT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY: DEFAULT_DEVICE_POLL_DELAY,
    OPT_MIN_POLL_INTERVAL: DEFAULT_MIN_POLL_INTERVAL,
    OPT_ALLOW_HISTORY_FALLBACK: DEFAULT_ALLOW_HISTORY_FALLBACK,
    OPT_ENABLE_STATS_ENTITIES: DEFAULT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED: DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS: DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_MAP_VIEW_TOKEN_EXPIRATION: DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_SEMANTIC_LOCATIONS: {},
    OPT_DELETE_CACHES_ON_REMOVE: DEFAULT_DELETE_CACHES_ON_REMOVE,
    OPT_CONTRIBUTOR_MODE: DEFAULT_CONTRIBUTOR_MODE,
    OPT_STALE_THRESHOLD: DEFAULT_STALE_THRESHOLD,
    OPT_SHOW_LOCATION_AGE: DEFAULT_SHOW_LOCATION_AGE,
    OPT_SPEED_GATE_ENABLED: DEFAULT_SPEED_GATE_ENABLED,
    OPT_ROUNDTRIP_CONFIRM: DEFAULT_ROUNDTRIP_CONFIRM,
}

# -------------------- Options schema versioning (lightweight) --------------------
# Used to mark that OPT_IGNORED_DEVICES is in "v2" (mapping with metadata).
OPT_OPTIONS_SCHEMA_VERSION = "options_schema_version"
_IGN_KEY_NAME = "name"
_IGN_KEY_ALIASES = "aliases"
_IGN_KEY_IGNORED_AT = "ignored_at"
_IGN_KEY_SOURCE = "source"


def _now_epoch() -> int:
    """Return current epoch timestamp as an integer."""
    return int(time.time())


IgnoredMetadata = dict[str, object]
IgnoredMapping = dict[str, IgnoredMetadata]


def _coerce_aliases(value: object) -> list[str]:
    """Return a list of string aliases from arbitrary input."""

    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [alias for alias in value if isinstance(alias, str)]
    return []


def _finite_int_or_none(value: object) -> int | None:
    """Convert a finite ``int``/``float`` to ``int``; return ``None`` otherwise.

    ``bool``, non-numeric types, and non-finite floats (``NaN``/``±inf``) all
    yield ``None``. This exists because ``int(float("nan"))`` raises
    ``ValueError`` and ``int(float("inf"))`` raises ``OverflowError`` -- either
    would defeat the drop-invalid, never-raise contract of the coercer below
    when a corrupt ``.storage`` edit or a direct-options writer stores such a
    value. Guarding on ``math.isfinite`` keeps that coercer total.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return int(value)
    return None


def coerce_ignored_mapping(raw: object) -> tuple[IgnoredMapping, bool]:
    """Coerce various legacy shapes into v2 mapping.
    Accepted inputs:
      v0: list[str]                   -> ids only
      v1: dict[str, str]             -> id -> name
      v2: dict[str, dict[str, Any]]  -> id -> metadata
    Returns (mapping, changed_flag).
    """
    changed = False
    out: IgnoredMapping = {}
    if isinstance(raw, list):
        # v0 -> v2
        for dev_id in raw:
            if isinstance(dev_id, str):
                out[dev_id] = {
                    _IGN_KEY_NAME: dev_id,
                    _IGN_KEY_ALIASES: [],
                    _IGN_KEY_IGNORED_AT: _now_epoch(),
                    _IGN_KEY_SOURCE: "migrated_v0",
                }
        changed = bool(out)
    elif isinstance(raw, Mapping):
        # str->str ? (v1)
        mapping = raw
        if all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in mapping.items()
        ):
            changed = True
            for dev_id, name in mapping.items():
                out[dev_id] = {
                    _IGN_KEY_NAME: name,
                    _IGN_KEY_ALIASES: [],
                    _IGN_KEY_IGNORED_AT: _now_epoch(),
                    _IGN_KEY_SOURCE: "migrated_v1",
                }
        else:
            # assume v2-ish; normalize keys
            for dev_id, meta in mapping.items():
                if not isinstance(dev_id, str):
                    continue
                aliases: list[str] = []
                ignored_at: int = _now_epoch()
                source = "registry"
                name = dev_id
                if isinstance(meta, Mapping):
                    str_meta: Mapping[str, object] = {
                        key: value
                        for key, value in meta.items()
                        if isinstance(key, str)
                    }
                    name_value = str_meta.get(_IGN_KEY_NAME)
                    if isinstance(name_value, str) and name_value:
                        name = name_value
                    aliases = _coerce_aliases(str_meta.get(_IGN_KEY_ALIASES))
                    ignored_value = str_meta.get(_IGN_KEY_IGNORED_AT)
                    coerced_at = _finite_int_or_none(ignored_value)
                    if coerced_at is not None:
                        ignored_at = coerced_at
                    source_value = str_meta.get(_IGN_KEY_SOURCE)
                    if isinstance(source_value, str) and source_value:
                        source = source_value
                else:
                    name = str(meta)
                out[dev_id] = {
                    _IGN_KEY_NAME: name,
                    _IGN_KEY_ALIASES: aliases,
                    _IGN_KEY_IGNORED_AT: ignored_at,
                    _IGN_KEY_SOURCE: source,
                }
    else:
        out = {}
    return out, changed


def ignored_choices_for_ui(
    ignored_map: Mapping[str, Mapping[str, object]],
) -> dict[str, str]:
    """Build UI labels 'Name (id)' directly from the stored mapping."""
    return {
        dev_id: f"{str(meta.get(_IGN_KEY_NAME) or dev_id)} ({dev_id})"
        for dev_id, meta in ignored_map.items()
    }


# --------------------------------------------------------------------------------------
# CONFIG_FIELDS — server-side validation contract for config/options flows
# --------------------------------------------------------------------------------------
# Used by config_flow.py to apply strong validators (type/min/max/step) for known keys.
# Keep keys in sync with OPTION_KEYS and default ranges used in schemas.
CONFIG_FIELDS: dict[str, dict[str, object]] = {
    OPT_LOCATION_POLL_INTERVAL: {
        "type": "int",
        "min": 60,
        "max": 3600,
        "step": 1,
    },
    OPT_DEVICE_POLL_DELAY: {
        "type": "int",
        "min": 1,
        "max": 60,
        "step": 1,
    },
    OPT_MIN_POLL_INTERVAL: {
        "type": "int",
        "min": 30,
        "max": 3600,
        "step": 1,
    },
    OPT_ALLOW_HISTORY_FALLBACK: {
        "type": "bool",
    },
    OPT_ENABLE_STATS_ENTITIES: {
        "type": "bool",
    },
    OPT_GOOGLE_HOME_FILTER_ENABLED: {
        "type": "bool",
    },
    OPT_GOOGLE_HOME_FILTER_KEYWORDS: {
        "type": "str",
    },
    OPT_MAP_VIEW_TOKEN_EXPIRATION: {
        "type": "bool",
    },
    OPT_STALE_THRESHOLD: {
        "type": "int",
        "min": 300,  # 5 minutes - allows ~2-3 typical FMDN update cycles
        "max": 86400,  # max 24 hours
        "step": 60,
    },
    OPT_SPEED_GATE_ENABLED: {
        "type": "bool",
    },
    OPT_ROUNDTRIP_CONFIRM: {
        "type": "bool",
    },
    # OPT_IGNORED_DEVICES is intentionally omitted: it is managed by a dedicated
    # visibility flow and not edited as a raw field (list of ids).
}

# --------------------------------------------------------------------------------------
# Services (align with services.yaml and translations)
# --------------------------------------------------------------------------------------
SERVICE_LOCATE_DEVICE: str = "locate_device"
SERVICE_PLAY_SOUND: str = "play_sound"
SERVICE_STOP_SOUND: str = "stop_sound"
SERVICE_LOCATE_EXTERNAL: str = "locate_external"

SERVICE_REFRESH_DEVICE_URLS: str = "refresh_device_urls"
# Optional compatibility alias (remove once all imports use SERVICE_REFRESH_DEVICE_URLS)
SERVICE_REFRESH_URLS: str = SERVICE_REFRESH_DEVICE_URLS

SERVICE_REBUILD_REGISTRY: str = "rebuild_registry"

# Optional attrs/modes for rebuild service
ATTR_MODE: str = "mode"
ATTR_DEVICE_IDS: str = "device_ids"
MODE_REBUILD: str = "rebuild"
MODE_MIGRATE: str = "migrate"
REBUILD_REGISTRY_MODES: tuple[str, str] = (MODE_REBUILD, MODE_MIGRATE)

# --------------------------------------------------------------------------------------
# Optional request timeouts (prefer central constants over scattered literals)
# --------------------------------------------------------------------------------------
LOCATION_REQUEST_TIMEOUT_S: int = 30

# Total budget for a single Nova HTTP round-trip. Single source of truth for the
# aiohttp ``ClientTimeout(total=...)`` used in NovaApi/nova_request.py. The outer
# poll guard below budgets against this value, so the two MUST move together;
# nova_request.py imports this constant instead of repeating the literal.
NOVA_REQUEST_TOTAL_TIMEOUT_S: int = 30

# Outer per-device poll guard. A single location request runs two *sequential*
# phases: first the Nova HTTP round-trip (capped at NOVA_REQUEST_TOTAL_TIMEOUT_S),
# then the FCM wait (capped at LOCATION_REQUEST_TIMEOUT_S). The outer guard must
# cover BOTH phases plus a small grace, otherwise a slow-but-successful HTTP call
# pushes the inner FCM wait past the guard and the outer wait_for raises a
# spurious TimeoutError before the inner request can return its clean empty
# result (Nygard: stagger nested timeout budgets, decreasing from outer to
# inner). The +5s grace absorbs scheduling/setup overhead between the phases.
# Note: this budgets a single HTTP attempt; a multi-retry backoff sequence inside
# nova_request can still exceed it, in which case the guard correctly intervenes.
POLL_DEVICE_OUTER_TIMEOUT_S: int = (
    NOVA_REQUEST_TOTAL_TIMEOUT_S + LOCATION_REQUEST_TIMEOUT_S + 5
)

# --------------------------------------------------------------------------------------
# HTTP headers / User-Agent (Nova API)
# --------------------------------------------------------------------------------------
NOVA_API_USER_AGENT: str = "fmd/20006320; gzip"
"""Canonical User-Agent for Nova API calls.

Used by `NovaApi/nova_request.py` for all upstream requests. Keep stable unless
there is a server-side change in expectations. Includes `gzip` to advertise
support for compressed responses.
"""

# --------------------------------------------------------------------------------------
# FCM socket tuning (used by Auth.firebase_messaging client)
# --------------------------------------------------------------------------------------
FCM_CLIENT_HEARTBEAT_INTERVAL_S: int = 20
FCM_SERVER_HEARTBEAT_INTERVAL_S: int = 10
FCM_IDLE_RESET_AFTER_S: float = 90.0
FCM_CONNECTION_RETRY_COUNT: int = 5
FCM_MONITOR_INTERVAL_S: int = 1
FCM_ABORT_ON_SEQ_ERROR_COUNT: int = 3

# --------------------------------------------------------------------------------------
# FCM data-starvation liveness watchdog (re-arming zombie self-heal)
# --------------------------------------------------------------------------------------
# A "zombie" FCM session is STARTED and keeps acking heartbeats (so the activity
# clock and readiness gate both look healthy), yet has stopped delivering
# ``push_received`` data messages despite locates being sent. The one-shot
# first-locate reconnect only covers a *young* session (age < _CHURN_WINDOW_S);
# a long-running session that goes silent is only healed by a manual reload
# today (empirically sometimes twice). The watchdog below detects this and
# forces a cooperative, backoff-limited reconnect that re-arms after each cycle.
#
# FCM_DATA_STARVATION_S: minimum gap (seconds) since the last real data delivery
# before a STARTED, heartbeating, locate-active session is treated as starved.
# Calibration (live DEBUG capture 2026-07-04, 2h23m healthy window): normal gaps
# between data messages reach ~349s (they grow with the observation window; only
# ~305s over a 71-min window), so the threshold must be a *generous* multiple of
# the poll cycle, well above 349s, never a value near it. 900s ≈ 3 default poll
# cycles (300s) and ≈ 30× the 30s locate timeout — conservatively large so the
# watchdog reacts later, never more aggressively (storm-safe by construction).
# The MCS session lifetime is ~2h, so 15-minute detection is fast enough.
FCM_DATA_STARVATION_S: int = 900

# FCM_ZOMBIE_CHECK_INTERVAL_S: how often the watchdog *evaluates* starvation.
# The supervisor monitor loop ticks every FCM_MONITOR_INTERVAL_S (1s); evaluating
# the predicate every tick is wasteful, so the watchdog is throttled to run at
# most once per this interval (30× rarer than the tick, far denser than the
# starvation window, so detection latency stays well under a minute).
FCM_ZOMBIE_CHECK_INTERVAL_S: int = 30

# FCM_ZOMBIE_RECONNECT_BACKOFF_BASE_S: first backoff window after the first
# watchdog reconnect. Subsequent windows double (60 → 120 → 240 → …) up to the
# cap below, so a session that stays starved is retried with growing patience.
FCM_ZOMBIE_RECONNECT_BACKOFF_BASE_S: int = 60

# FCM_ZOMBIE_RECONNECT_BACKOFF_CAP_S: upper bound of the exponential backoff
# (equals the starvation window: never retry faster than the detection cadence).
FCM_ZOMBIE_RECONNECT_BACKOFF_CAP_S: int = 900

# FCM_ZOMBIE_MAX_RECONNECTS: hard ceiling on watchdog reconnects per starvation
# episode. The count (and backoff) reset on the first successful data delivery
# after a reconnect. Together with the cap and the one-in-flight lock this bounds
# the worst case to a slow, finite series — never a reconnect storm / self-DoS.
FCM_ZOMBIE_MAX_RECONNECTS: int = 5

# --------------------------------------------------------------------------------------
# Feature Flags (compile-time toggles for optional functionality)
# --------------------------------------------------------------------------------------
# FMDN Finder: Upload location reports to Google for FMDN beacons detected by Bermuda.
# This feature is prepared but disabled by default. Set to True to enable.
# When disabled, no Bermuda listeners are registered and no logs are produced.
FEATURE_FMDN_FINDER_ENABLED: bool = False

# --------------------------------------------------------------------------------------
# Events & Repairs (auth status)
# --------------------------------------------------------------------------------------
# Events fired by the coordinator to allow user automations.
EVENT_AUTH_ERROR: str = f"{DOMAIN}.authentication_error"
EVENT_AUTH_OK: str = f"{DOMAIN}.authentication_ok"

# Translation key for the dedicated auth-status binary_sensor entity.
TRANSLATION_KEY_AUTH_STATUS: str = "nova_auth_status"

# Translation key for the dedicated encryption-key-status (ENUM) sensor entity.
TRANSLATION_KEY_ENCRYPTION_KEY_STATUS: str = "encryption_key_status"

# Issue key used for Repairs (translations use the same key).
ISSUE_AUTH_EXPIRED_KEY: str = "auth_expired"

# Issue/translation keys for common repair issues (keep aligned with translations/*.json).
ISSUE_MULTIPLE_CONFIG_ENTRIES: str = "multiple_config_entries"
TRANSLATION_KEY_CACHE_PURGED: str = "cache_purged"
TRANSLATION_KEY_UNIQUE_ID_COLLISION: str = "unique_id_collision"
TRANSLATION_KEY_DUPLICATE_ACCOUNT: str = "duplicate_account_entries"

# Global Repairs issue raised when HACS has written new integration code to disk
# but the running process still executes the previously loaded version.
ISSUE_RESTART_REQUIRED_KEY: str = "restart_required"
TRANSLATION_KEY_RESTART_REQUIRED: str = "restart_required"


def issue_id_for(entry_id: str) -> str:
    """Return a stable Repairs issue_id for a given config entry.

    Pattern: 'auth_expired_<entry_id>'
    """
    return f"{ISSUE_AUTH_EXPIRED_KEY}_{entry_id}"


# --------------------------------------------------------------------------------------
# Storage (entry-scoped key prefix; each entry gets its own Store file)
# --------------------------------------------------------------------------------------
STORAGE_KEY: str = f"{DOMAIN}_secrets"
STORAGE_VERSION: int = 1

# --------------------------------------------------------------------------------------
# Map token helpers (centralized, import-safe)
# --------------------------------------------------------------------------------------
WEEK_SECONDS: int = 7 * 24 * 60 * 60


def map_token_secret_seed(
    ha_uuid: str,
    entry_id: str,
    expiration_enabled: bool,
    now: int | None = None,
) -> str:
    """Return the secret seed string used to derive map-view tokens.

    Callers should pass the seed to :func:`map_token_hex_digest` to obtain the
    user-facing token string used in map URLs and entity configuration links.

    Args:
        ha_uuid: Home Assistant instance UUID (e.g., `hass.data["core.uuid"]`).
        entry_id: Config entry id used to namespace tokens per entry.
        expiration_enabled: If True, use a weekly-rolling bucket; else static.
        now: Optional epoch seconds, useful for tests; defaults to current time.

    Returns:
        A deterministic seed string in the form:
            "<uuid>:<entry_id>:<week>"  (rolling)
        or  "<uuid>:<entry_id>:static" (static)
    """
    safe_uuid = ha_uuid or "ha"
    if expiration_enabled:
        if now is None:
            now = int(time.time())
        week = now // WEEK_SECONDS
        return f"{safe_uuid}:{entry_id}:{week}"
    return f"{safe_uuid}:{entry_id}:static"


def map_token_hex_digest(seed: str) -> str:
    """Return the 16-character hex token derived from a seed value.

    The helper generates a SHA-256 digest of ``seed`` and truncates it to the
    first 16 hexadecimal characters, ensuring a consistent token format across
    all components.
    """

    return hashlib.sha256(seed.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------------------
# Container login (one-click Docker login handoff)
# --------------------------------------------------------------------------------------
# Two distinct container ports keep the human-facing noVNC console and the
# machine-facing token endpoint separated. ``CONTAINER_NOVNC_PORT`` is only ever
# rendered as a clickable link/hint, while ``CONTAINER_TOKEN_PORT`` is the default
# for the loopback fetch that pulls the freshly minted ``secrets.json``.
CONTAINER_NOVNC_PORT: int = 7900
CONTAINER_TOKEN_PORT: int = 7901

# Total aiohttp timeout budget (seconds) for a single container fetch/ack call.
CONTAINER_FETCH_TIMEOUT: int = 30

# Minimum accepted length of the pairing nonce (``secrets.token_urlsafe(16)`` ->
# 22 url-safe characters == 128 bit of entropy); shorter values are rejected
# before any network round-trip.
CONTAINER_NONCE_MIN_LEN: int = 16

# Hard cap (bytes) on the container response body to bound memory and abuse; the
# freshly minted bundle is small, so a generous but finite ceiling is enough.
CONTAINER_MAX_RESPONSE_BYTES: int = 1024 * 1024

# Lockout threshold: after this many failed pairing attempts the token endpoint
# refuses further ``GET /secrets`` calls (bruteforce guard).
CONTAINER_NONCE_MAX_ATTEMPTS: int = 5

# Time-to-live (seconds) for the one-shot token endpoint. After this window the
# server deletes the bundle and shuts down even without an explicit ACK.
CONTAINER_TOKEN_TTL: int = 300

# Options key holding an optional list of *additional* ``secrets.json`` paths the
# discovery watcher observes on top of the default ``Auth/secrets.json``.
SECRETS_EXTRA_WATCH_PATHS: str = "secrets_extra_watch_paths"


__all__ = [
    "DOMAIN",
    "INTEGRATION_VERSION",
    "SERVICE_DEVICE_NAME",
    "SERVICE_DEVICE_MODEL",
    "SERVICE_DEVICE_MANUFACTURER",
    "SERVICE_DEVICE_TRANSLATION_KEY",
    "SERVICE_DEVICE_IDENTIFIER_PREFIX",
    "LEGACY_SERVICE_IDENTIFIER",
    "SERVICE_SUBENTRY_KEY",
    "TRACKER_SUBENTRY_KEY",
    "DATA_SUBENTRY_KEY",
    "SUBENTRY_TYPE_SERVICE",
    "SUBENTRY_TYPE_HUB",
    "SUBENTRY_TYPE_TRACKER",
    "service_device_identifier",
    "CONF_OAUTH_TOKEN",
    "DATA_AAS_TOKEN",
    "CONF_GOOGLE_EMAIL",
    "DATA_SECRET_BUNDLE",
    "DATA_AUTH_METHOD",
    "OPT_IGNORED_DEVICES",
    "OPT_LOCATION_POLL_INTERVAL",
    "OPT_DEVICE_POLL_DELAY",
    "OPT_MIN_POLL_INTERVAL",
    "OPT_ALLOW_HISTORY_FALLBACK",
    "OPT_ENABLE_STATS_ENTITIES",
    "OPT_GOOGLE_HOME_FILTER_ENABLED",
    "OPT_GOOGLE_HOME_FILTER_KEYWORDS",
    "OPT_MAP_VIEW_TOKEN_EXPIRATION",
    "OPTION_KEYS",
    "OPT_DELETE_CACHES_ON_REMOVE",
    "OPT_STALE_THRESHOLD",
    "OPT_SHOW_LOCATION_AGE",
    "OPT_SPEED_GATE_ENABLED",
    "OPT_ROUNDTRIP_CONFIRM",
    "OPT_STALE_THRESHOLD_ENABLED",
    "MIGRATE_DATA_KEYS_TO_OPTIONS",
    "UPDATE_INTERVAL",
    "DEFAULT_SPEED_GATE_ENABLED",
    "DEFAULT_MAX_PLAUSIBLE_SPEED_MPS",
    "DEFAULT_ROUNDTRIP_CONFIRM",
    "ROUND_TRIP_TTL_S",
    "ROUND_TRIP_ANCHOR_RADIUS_M",
    "DEFAULT_LOCATION_POLL_INTERVAL",
    "DEFAULT_DEVICE_POLL_DELAY",
    "DEFAULT_MIN_POLL_INTERVAL",
    "LOCATE_COOLDOWN_S",
    "DEFAULT_ALLOW_HISTORY_FALLBACK",
    "DEFAULT_ENABLE_STATS_ENTITIES",
    "DEFAULT_GOOGLE_HOME_FILTER_ENABLED",
    "DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS",
    "GOOGLE_HOME_SPAM_THRESHOLD_MINUTES",
    "DEFAULT_MAP_VIEW_TOKEN_EXPIRATION",
    "DEFAULT_DELETE_CACHES_ON_REMOVE",
    "DEFAULT_STALE_THRESHOLD",
    "DEFAULT_SHOW_LOCATION_AGE",
    "DEFAULT_OPTIONS",
    "CONFIG_FIELDS",
    "TOKEN_REFRESH_COOLDOWN_S",
    "_EID_REFRESH_DEBOUNCE_S",
    "SERVICE_LOCATE_DEVICE",
    "SERVICE_PLAY_SOUND",
    "SERVICE_STOP_SOUND",
    "SERVICE_LOCATE_EXTERNAL",
    "SERVICE_REFRESH_DEVICE_URLS",
    "SERVICE_REFRESH_URLS",
    "SERVICE_REBUILD_REGISTRY",
    "ATTR_MODE",
    "ATTR_DEVICE_IDS",
    "MODE_REBUILD",
    "MODE_MIGRATE",
    "REBUILD_REGISTRY_MODES",
    "LOCATION_REQUEST_TIMEOUT_S",
    "NOVA_REQUEST_TOTAL_TIMEOUT_S",
    "POLL_DEVICE_OUTER_TIMEOUT_S",
    "NOVA_API_USER_AGENT",
    "FCM_CLIENT_HEARTBEAT_INTERVAL_S",
    "FCM_SERVER_HEARTBEAT_INTERVAL_S",
    "FCM_IDLE_RESET_AFTER_S",
    "FCM_CONNECTION_RETRY_COUNT",
    "FCM_MONITOR_INTERVAL_S",
    "FCM_ABORT_ON_SEQ_ERROR_COUNT",
    "FCM_DATA_STARVATION_S",
    "FCM_ZOMBIE_CHECK_INTERVAL_S",
    "FCM_ZOMBIE_RECONNECT_BACKOFF_BASE_S",
    "FCM_ZOMBIE_RECONNECT_BACKOFF_CAP_S",
    "FCM_ZOMBIE_MAX_RECONNECTS",
    "EVENT_AUTH_ERROR",
    "EVENT_AUTH_OK",
    "TRANSLATION_KEY_AUTH_STATUS",
    "TRANSLATION_KEY_ENCRYPTION_KEY_STATUS",
    "ISSUE_AUTH_EXPIRED_KEY",
    "ISSUE_MULTIPLE_CONFIG_ENTRIES",
    "TRANSLATION_KEY_CACHE_PURGED",
    "TRANSLATION_KEY_UNIQUE_ID_COLLISION",
    "TRANSLATION_KEY_DUPLICATE_ACCOUNT",
    "ISSUE_RESTART_REQUIRED_KEY",
    "TRANSLATION_KEY_RESTART_REQUIRED",
    "issue_id_for",
    "STORAGE_KEY",
    "STORAGE_VERSION",
    "coerce_ignored_mapping",
    "ignored_choices_for_ui",
    "WEEK_SECONDS",
    "map_token_secret_seed",
    "map_token_hex_digest",
    "CONTAINER_NOVNC_PORT",
    "CONTAINER_TOKEN_PORT",
    "CONTAINER_FETCH_TIMEOUT",
    "CONTAINER_NONCE_MIN_LEN",
    "CONTAINER_MAX_RESPONSE_BYTES",
    "CONTAINER_NONCE_MAX_ATTEMPTS",
    "CONTAINER_TOKEN_TTL",
    "SECRETS_EXTRA_WATCH_PATHS",
]
