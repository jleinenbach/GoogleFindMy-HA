# custom_components/googlefindmy/const.py
"""Constants for Google Find My Device integration.

All constants defined here are intended to be import-safe across the integration.
Keep comments and docstrings in English; user-facing strings belong in translations.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence

# --------------------------------------------------------------------------------------
# Core identifiers
# --------------------------------------------------------------------------------------
DOMAIN: str = "googlefindmy"
# Keep the integration version aligned across the project (match manifest.json)
INTEGRATION_VERSION: str = "1.6.0"

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
SUBENTRY_TYPE_SERVICE: str = "service"
SUBENTRY_TYPE_TRACKER: str = "tracker"


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
OPT_MIN_ACCURACY_THRESHOLD: str = "min_accuracy_threshold"
OPT_MOVEMENT_THRESHOLD: str = "movement_threshold"
OPT_ALLOW_HISTORY_FALLBACK: str = "allow_history_fallback"
OPT_ENABLE_STATS_ENTITIES: str = "enable_stats_entities"
OPT_GOOGLE_HOME_FILTER_ENABLED: str = "google_home_filter_enabled"
OPT_GOOGLE_HOME_FILTER_KEYWORDS: str = "google_home_filter_keywords"
OPT_MAP_VIEW_TOKEN_EXPIRATION: str = "map_view_token_expiration"
OPT_CONTRIBUTOR_MODE: str = "contributor_mode"
OPT_IGNORED_DEVICES: str = "ignored_devices"

# Canonical list of option keys supported by the integration (without tracked_devices)
OPTION_KEYS: tuple[str, ...] = (
    OPT_IGNORED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_POLL_INTERVAL,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MOVEMENT_THRESHOLD,
    OPT_ALLOW_HISTORY_FALLBACK,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_CONTRIBUTOR_MODE,
)

# Keys which may exist historically in entry.data and should be soft-copied to entry.options
MIGRATE_DATA_KEYS_TO_OPTIONS: tuple[str, ...] = OPTION_KEYS

# --------------------------------------------------------------------------------------
# Defaults (aligned with the current implementation; adjust carefully)
# --------------------------------------------------------------------------------------
UPDATE_INTERVAL: int = 60  # seconds; DataUpdateCoordinator "tick" (lightweight)

# Polling cadence
DEFAULT_LOCATION_POLL_INTERVAL: int = 300  # seconds; start a new polling cycle
DEFAULT_DEVICE_POLL_DELAY: int = 5  # seconds; inter-device delay within one cycle
DEFAULT_MIN_POLL_INTERVAL: int = 60  # seconds; hard lower bound between cycles

# Manual locate policy (button/service)
LOCATE_COOLDOWN_S: int = DEFAULT_MIN_POLL_INTERVAL
"""Cooldown window (seconds) applied after a manual locate trigger."""

# Quality/logic thresholds
DEFAULT_MIN_ACCURACY_THRESHOLD: int = 100  # meters; drop worse fixes (0 => disabled)
DEFAULT_MOVEMENT_THRESHOLD: int = 50  # meters; used for future movement gating
DEFAULT_ALLOW_HISTORY_FALLBACK: bool = False

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
    OPT_MIN_ACCURACY_THRESHOLD: DEFAULT_MIN_ACCURACY_THRESHOLD,
    OPT_MOVEMENT_THRESHOLD: DEFAULT_MOVEMENT_THRESHOLD,
    OPT_ALLOW_HISTORY_FALLBACK: DEFAULT_ALLOW_HISTORY_FALLBACK,
    OPT_ENABLE_STATS_ENTITIES: DEFAULT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED: DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS: DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_MAP_VIEW_TOKEN_EXPIRATION: DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_CONTRIBUTOR_MODE: DEFAULT_CONTRIBUTOR_MODE,
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
                    if isinstance(ignored_value, (int, float)):
                        ignored_at = int(ignored_value)
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
    OPT_MIN_ACCURACY_THRESHOLD: {
        "type": "int",
        "min": 25,
        "max": 500,
        "step": 1,
    },
    OPT_MOVEMENT_THRESHOLD: {
        "type": "int",
        "min": 10,
        "max": 200,
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
# Events & Repairs (auth status)
# --------------------------------------------------------------------------------------
# Events fired by the coordinator to allow user automations.
EVENT_AUTH_ERROR: str = f"{DOMAIN}.authentication_error"
EVENT_AUTH_OK: str = f"{DOMAIN}.authentication_ok"

# Translation key for the dedicated auth-status binary_sensor entity.
TRANSLATION_KEY_AUTH_STATUS: str = "nova_auth_status"

# Issue key used for Repairs (translations use the same key).
ISSUE_AUTH_EXPIRED_KEY: str = "auth_expired"


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
    "SUBENTRY_TYPE_SERVICE",
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
    "OPT_MIN_ACCURACY_THRESHOLD",
    "OPT_MOVEMENT_THRESHOLD",
    "OPT_ALLOW_HISTORY_FALLBACK",
    "OPT_ENABLE_STATS_ENTITIES",
    "OPT_GOOGLE_HOME_FILTER_ENABLED",
    "OPT_GOOGLE_HOME_FILTER_KEYWORDS",
    "OPT_MAP_VIEW_TOKEN_EXPIRATION",
    "OPTION_KEYS",
    "MIGRATE_DATA_KEYS_TO_OPTIONS",
    "UPDATE_INTERVAL",
    "DEFAULT_LOCATION_POLL_INTERVAL",
    "DEFAULT_DEVICE_POLL_DELAY",
    "DEFAULT_MIN_POLL_INTERVAL",
    "LOCATE_COOLDOWN_S",
    "DEFAULT_MIN_ACCURACY_THRESHOLD",
    "DEFAULT_MOVEMENT_THRESHOLD",
    "DEFAULT_ALLOW_HISTORY_FALLBACK",
    "DEFAULT_ENABLE_STATS_ENTITIES",
    "DEFAULT_GOOGLE_HOME_FILTER_ENABLED",
    "DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS",
    "GOOGLE_HOME_SPAM_THRESHOLD_MINUTES",
    "DEFAULT_MAP_VIEW_TOKEN_EXPIRATION",
    "DEFAULT_OPTIONS",
    "CONFIG_FIELDS",
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
    "NOVA_API_USER_AGENT",
    "FCM_CLIENT_HEARTBEAT_INTERVAL_S",
    "FCM_SERVER_HEARTBEAT_INTERVAL_S",
    "FCM_IDLE_RESET_AFTER_S",
    "FCM_CONNECTION_RETRY_COUNT",
    "FCM_MONITOR_INTERVAL_S",
    "FCM_ABORT_ON_SEQ_ERROR_COUNT",
    "EVENT_AUTH_ERROR",
    "EVENT_AUTH_OK",
    "TRANSLATION_KEY_AUTH_STATUS",
    "ISSUE_AUTH_EXPIRED_KEY",
    "issue_id_for",
    "STORAGE_KEY",
    "STORAGE_VERSION",
    "coerce_ignored_mapping",
    "ignored_choices_for_ui",
    "WEEK_SECONDS",
    "map_token_secret_seed",
    "map_token_hex_digest",
]
