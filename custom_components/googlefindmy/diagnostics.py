# custom_components/googlefindmy/diagnostics.py
"""Diagnostics for the Google Find My Device integration."""
from __future__ import annotations

import time
from typing import Any, Optional

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    OPT_TRACKED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MOVEMENT_THRESHOLD,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
)

# Nothing to redact in our current summary (we avoid secrets entirely),
# but keep the hook ready in case we add more fields later.
TO_REDACT: list[str] = []


def _monotonic_to_wall_seconds(last_mono: Optional[float]) -> Optional[float]:
    """Convert a stored monotonic timestamp to wall-clock seconds since epoch.

    NOTE:
    - Our coordinator currently tracks the last poll baseline in a monotonic clock.
      For diagnostics (best-effort), we infer the wall time using the current deltas.
    - If the internal field is absent, we return None gracefully.
    """
    if not isinstance(last_mono, (int, float)) or last_mono <= 0:
        return None
    now_wall = time.time()
    now_mono = time.monotonic()
    return max(0.0, now_wall - (now_mono - float(last_mono)))


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return anonymized diagnostics for a config entry.

    Best practice:
    - Never include secrets, location coordinates, device IDs, device names or emails.
    - Keep output useful for debugging: show options summary, stats, counts, timing.
    - Prefer entry.runtime_data if present; otherwise fall back to hass.data.
    """
    # Prefer runtime_data if the integration adopts it; otherwise fall back.
    coordinator = None
    runtime = getattr(entry, "runtime_data", None)
    if runtime:
        # Allow either a direct coordinator or a simple holder object.
        coordinator = getattr(runtime, "coordinator", runtime)
    if coordinator is None:
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    # Build a compact, anonymized config snapshot from options (no secrets).
    opt = entry.options
    config_summary = {
        "location_poll_interval": int(opt.get(OPT_LOCATION_POLL_INTERVAL, 300)),
        "device_poll_delay": int(opt.get(OPT_DEVICE_POLL_DELAY, 5)),
        "min_accuracy_threshold": int(opt.get(OPT_MIN_ACCURACY_THRESHOLD, 100)),
        "movement_threshold": int(opt.get(OPT_MOVEMENT_THRESHOLD, 50)),
        "google_home_filter_enabled": bool(opt.get(OPT_GOOGLE_HOME_FILTER_ENABLED, False)),
        # Do NOT include the actual keywords; only their count to avoid revealing user text
        "google_home_filter_keywords_count": len(
            [k.strip() for k in str(opt.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, "")).split(",") if k.strip()]
        ),
        "enable_stats_entities": bool(opt.get(OPT_ENABLE_STATS_ENTITIES, True)),
        "map_view_token_expiration": bool(opt.get(OPT_MAP_VIEW_TOKEN_EXPIRATION, True)),
        # tracked devices: only the count, never IDs
        "tracked_devices_count": len(opt.get(OPT_TRACKED_DEVICES, [])),
    }

    payload: dict[str, Any] = {
        "entry": {
            # sanitize entry meta; do not include unique_id, data, or title if it could be user-provided PII
            "entry_id": entry.entry_id,
            "version": entry.version,
            "domain": entry.domain,
        },
        "config": async_redact_data(config_summary, TO_REDACT),
    }

    # Enrich with coordinator info if available (all anonymized)
    if coordinator is not None:
        # counts – never include names or IDs
        try:
            known_devices_count = len(getattr(coordinator, "_device_names", {}) or {})
        except (AttributeError, TypeError):
            known_devices_count = None
        try:
            cache_items_count = len(getattr(coordinator, "_device_location_data", {}) or {})
        except (AttributeError, TypeError):
            cache_items_count = None

        # last successful poll (best-effort using monotonic baseline if present)
        last_poll_wall = None
        try:
            last_poll_wall = _monotonic_to_wall_seconds(getattr(coordinator, "_last_poll_mono", None))
        except (AttributeError, TypeError):
            last_poll_wall = None

        # stats are already anonymized counters
        try:
            stats = dict(getattr(coordinator, "stats", {}) or {})
        except (AttributeError, TypeError):
            stats = {}

        payload["coordinator"] = {
            "is_polling": bool(getattr(coordinator, "_is_polling", False)),
            "known_devices_count": known_devices_count,
            "cache_items_count": cache_items_count,
            "last_poll_wall_ts": last_poll_wall,  # seconds since epoch (UTC)
            "stats": stats,
        }

    return payload
