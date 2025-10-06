"""Constants for Google Find My Device integration."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Core identifiers
# ---------------------------------------------------------------------------
DOMAIN = "googlefindmy"
INTEGRATION_VERSION = "1.5.6-0"

# ---------------------------------------------------------------------------
# Configuration keys (data vs. options separation)
# ---------------------------------------------------------------------------
# Data (immutable / credentials): stored in config_entry.data
CONF_OAUTH_TOKEN = "oauth_token"          # kept for backward compatibility
CONF_GOOGLE_EMAIL = "google_email"        # helper key when individual tokens are used
DATA_SECRET_BUNDLE = "secrets_data"       # full GoogleFindMyTools secrets.json content
DATA_AUTH_METHOD = "auth_method"          # "secrets_json" | "individual_tokens"

# Options (user-changeable): stored in config_entry.options
OPT_TRACKED_DEVICES = "tracked_devices"
OPT_LOCATION_POLL_INTERVAL = "location_poll_interval"
OPT_DEVICE_POLL_DELAY = "device_poll_delay"
OPT_MIN_POLL_INTERVAL = "min_poll_interval"
OPT_MIN_ACCURACY_THRESHOLD = "min_accuracy_threshold"
OPT_MOVEMENT_THRESHOLD = "movement_threshold"
OPT_ALLOW_HISTORY_FALLBACK = "allow_history_fallback"
OPT_ENABLE_STATS_ENTITIES = "enable_stats_entities"
OPT_GOOGLE_HOME_FILTER_ENABLED = "google_home_filter_enabled"
OPT_GOOGLE_HOME_FILTER_KEYWORDS = "google_home_filter_keywords"
OPT_MAP_VIEW_TOKEN_EXPIRATION = "map_view_token_expiration"

# A canonical list of option keys the integration understands.
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

# For backward-compat migrations: these keys may exist in entry.data and
# should be SOFT-copied to entry.options at setup.
MIGRATE_DATA_KEYS_TO_OPTIONS: tuple[str, ...] = OPTION_KEYS

# ---------------------------------------------------------------------------
# Defaults (aligned with current implementation)
# ---------------------------------------------------------------------------
# Polling cadence
UPDATE_INTERVAL = 60  # seconds for HA DataUpdateCoordinator "tick" (lightweight)
DEFAULT_LOCATION_POLL_INTERVAL = 300  # seconds; start a new polling cycle
DEFAULT_DEVICE_POLL_DELAY = 5         # seconds; inter-device delay within one cycle
DEFAULT_MIN_POLL_INTERVAL = 1         # seconds; hard lower bound between cycles

# Quality/logic thresholds
DEFAULT_MIN_ACCURACY_THRESHOLD = 100  # meters; drop worse fixes (0 => disabled)
DEFAULT_MOVEMENT_THRESHOLD = 50       # meters; used for future movement gating
DEFAULT_ALLOW_HISTORY_FALLBACK = False

# Stats entities
DEFAULT_ENABLE_STATS_ENTITIES = True

# Google Home filter
DEFAULT_GOOGLE_HOME_FILTER_ENABLED = True
DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS = "nest,google,home,mini,hub,display,chromecast,speaker"
GOOGLE_HOME_SPAM_THRESHOLD_MINUTES = 15  # debounce for repeated detections

# Map View token behavior
# Default remains "no expiration" for backwards compatibility.
DEFAULT_MAP_VIEW_TOKEN_EXPIRATION = False

# Aggregate defaults dictionary for options-first reading patterns
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

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
SERVICE_LOCATE_DEVICE = "locate_device"
SERVICE_PLAY_SOUND = "play_sound"
SERVICE_LOCATE_EXTERNAL = "locate_device_external"
SERVICE_REFRESH_URLS = "refresh_device_urls"

# ---------------------------------------------------------------------------
# Optional request timeouts (consumers may adopt gradually)
# ---------------------------------------------------------------------------
# Using constants allows tuning without touching multiple call sites.
LOCATION_REQUEST_TIMEOUT_S = 30
