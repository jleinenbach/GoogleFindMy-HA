# /custom_components/googlefindmy/const.py
"""Constants for Google Find My Device integration.

All constants defined here are intended to be import-safe across the integration.
Keep comments and docstrings in English; user-facing strings belong in translations.
"""

from __future__ import annotations

# --------------------------------------------------------------------------------------
# Core identifiers
# --------------------------------------------------------------------------------------
DOMAIN: str = "googlefindmy"
INTEGRATION_VERSION: str = "1.5.6-1"

# --------------------------------------------------------------------------------------
# Configuration keys (data vs. options separation)
# NOTE: Keep keys stable to avoid migration churn across releases.
# --------------------------------------------------------------------------------------
# Data (immutable / credentials): stored in config_entry.data
CONF_OAUTH_TOKEN: str = "oauth_token"          # kept for backward compatibility
CONF_GOOGLE_EMAIL: str = "google_email"        # helper key when individual tokens are used
DATA_SECRET_BUNDLE: str = "secrets_data"       # full GoogleFindMyTools secrets.json content
DATA_AUTH_METHOD: str = "auth_method"          # "secrets_json" | "individual_tokens"

# Options (user-changeable): stored in config_entry.options
OPT_TRACKED_DEVICES: str = "tracked_devices"
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

# Canonical list of option keys supported by the integration
OPTION_KEYS: tuple[str, ...] = (
    OPT_TRACKED_DEVICES,
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
)

# Keys which may exist historically in entry.data and should be soft-copied to entry.options
MIGRATE_DATA_KEYS_TO_OPTIONS: tuple[str, ...] = OPTION_KEYS

# --------------------------------------------------------------------------------------
# Defaults (aligned with the current implementation; adjust carefully)
# --------------------------------------------------------------------------------------
UPDATE_INTERVAL: int = 60  # seconds; DataUpdateCoordinator "tick" (lightweight)

# Polling cadence
DEFAULT_LOCATION_POLL_INTERVAL: int = 300  # seconds; start a new polling cycle
DEFAULT_DEVICE_POLL_DELAY: int = 5         # seconds; inter-device delay within one cycle
DEFAULT_MIN_POLL_INTERVAL: int = 60        # seconds; hard lower bound between cycles

# Manual locate policy (button/service)
LOCATE_COOLDOWN_S: int = DEFAULT_MIN_POLL_INTERVAL
"""Cooldown window (seconds) applied after a manual locate trigger."""

# Quality/logic thresholds
DEFAULT_MIN_ACCURACY_THRESHOLD: int = 100  # meters; drop worse fixes (0 => disabled)
DEFAULT_MOVEMENT_THRESHOLD: int = 50       # meters; used for future movement gating
DEFAULT_ALLOW_HISTORY_FALLBACK: bool = False

# Stats entities
DEFAULT_ENABLE_STATS_ENTITIES: bool = True

# Google Home filter
DEFAULT_GOOGLE_HOME_FILTER_ENABLED: bool = True
DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS: str = "nest,google,home,mini,hub,display,chromecast,speaker"
GOOGLE_HOME_SPAM_THRESHOLD_MINUTES: int = 15  # debounce for repeated detections

# Map View token behavior
# Default remains "no expiration" for backwards compatibility.
DEFAULT_MAP_VIEW_TOKEN_EXPIRATION: bool = False

# Aggregate defaults dictionary for option-first reading patterns
DEFAULT_OPTIONS: dict[str, object] = {
    OPT_TRACKED_DEVICES: [],
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
}

# --------------------------------------------------------------------------------------
# Services (align with services.yaml and translations)
# --------------------------------------------------------------------------------------
SERVICE_LOCATE_DEVICE: str = "locate_device"
SERVICE_PLAY_SOUND: str = "play_sound"
SERVICE_LOCATE_EXTERNAL: str = "locate_device_external"

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
# Storage (entry-scoped key prefix; each entry gets its own Store file)
# --------------------------------------------------------------------------------------
STORAGE_KEY: str = f"{DOMAIN}_secrets"
STORAGE_VERSION: int = 1

__all__ = [
    "DOMAIN",
    "INTEGRATION_VERSION",
    "CONF_OAUTH_TOKEN",
    "CONF_GOOGLE_EMAIL",
    "DATA_SECRET_BUNDLE",
    "DATA_AUTH_METHOD",
    "OPT_TRACKED_DEVICES",
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
    "SERVICE_LOCATE_DEVICE",
    "SERVICE_PLAY_SOUND",
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
    "STORAGE_KEY",
    "STORAGE_VERSION",
]
