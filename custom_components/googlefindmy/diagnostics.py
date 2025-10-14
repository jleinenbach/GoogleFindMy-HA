# custom_components/googlefindmy/diagnostics.py
"""Diagnostics for the Google Find My Device integration.

Design goals (HA quality scale / Platinum-ready):
- Never leak secrets or personal data (tokens, emails, device IDs, coordinates, names).
- Provide enough structured, anonymized context to debug typical issues (polling, counts, timings).
- Prefer runtime_data (modern pattern) but gracefully fall back to hass.data for older setups.
- Keep redaction centralized and defensive (include common token/email keys even if we don't expose them now).

Privacy note:
- POPETS’25 (Böttger et al., 2025) highlights that EID-related artifacts and UT bits can be used
  for correlation/identification. We therefore **over-redact** such fields, even if we never place
  them into diagnostics directly. This is a defense-in-depth safeguard to keep future changes safe.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    # user-facing options (non-secret)
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MOVEMENT_THRESHOLD,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_IGNORED_DEVICES,
    # secrets in entry.data (must never be exposed)
    CONF_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
)

# ---------------------------------------------------------------------------
# Redaction policy
# ---------------------------------------------------------------------------
# Keys to redact anywhere they appear in the diagnostics payload.
# Keep this list generous; it is safe to over-redact (defense-in-depth).
TO_REDACT: list[str] = [
    # Known integration secrets (entry.data)
    CONF_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
    # Common token/email/credential shapes
    "aas_token",
    "access_token",
    "refresh_token",
    "token",
    "security_token",
    "authorization",
    "cookie",
    "set-cookie",
    "app_id",
    "android_id",
    "fid",
    "email",
    "username",
    "user",
    "Auth",
    "secret",
    "private",
    "public",
    "p256dh",
    "auth",
    "endpoint",
    # Identity resolving / E2EE related (never expose!)
    "irk",
    "irk_hex",
    "identity_resolving_key",
    "identity_resolving_keys",
    "encrypted_identity_resolving_key",
    "encrypted_identity_resolving_keys",
    "identityResolvingKey",
    "identityResolvingKeys",
    "encryptedIdentityResolvingKey",
    "encryptedIdentityResolvingKeys",
    "eik",
    "eik_hex",
    "identity_key",
    "identity_keys",
    "encrypted_identity_key",
    "encrypted_identity_keys",
    "identityKey",
    "identityKeys",
    "encryptedIdentityKey",
    "encryptedIdentityKeys",
    "ownerKey",
    "ownerKeyVersion",
    # EID / UT artifacts (see POPETS’25; redact to avoid correlation)
    "eid",
    "eid_prefix",
    "eidPrefix",
    "truncated_eid",
    "truncatedEid",
    "ut",
    "ut_bits",
    "utBits",
    # Device identifiers (avoid leaking stable IDs)
    "device_id",
    "deviceId",
    "canonical_id",
    "canonicalId",
    "canonic_id",
    "canonicId",
    "canonicIds",
    # Location-related fields (we do not include them, but redact defensively)
    "latitude",
    "longitude",
    "altitude",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _monotonic_to_wall_seconds(last_mono: Optional[float]) -> Optional[float]:
    """Convert a stored monotonic timestamp to wall-clock seconds since epoch (UTC).

    We infer the wall time using the current monotonic delta; this is best-effort
    and intentionally avoids reading any precise location timestamps from entities.
    """
    if not isinstance(last_mono, (int, float)) or last_mono <= 0:
        return None
    now_wall = time.time()
    now_mono = time.monotonic()
    # Clamp at 0 to avoid negative values when clocks drift
    return max(0.0, now_wall - (now_mono - float(last_mono)))


def _count_keywords(value: Any) -> int:
    """Count comma-separated keywords without exposing their content."""
    if not value:
        return 0
    try:
        parts = [p.strip() for p in str(value).split(",")]
        return sum(1 for p in parts if p)
    except Exception:
        return 0


def _coerce_pos_int(value: Any, default: int) -> int:
    """Best-effort positive-int coercion for options (defensive)."""
    try:
        v = int(value)
        return v if v >= 0 else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Diagnostics entrypoint
# ---------------------------------------------------------------------------


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return anonymized diagnostics for a config entry.

    Best practices:
    - Do NOT include: coordinates, device IDs, device names, emails, tokens,
      unique_id, or any raw content from external services.
    - DO include: anonymized counters, booleans, timings, and versions.

    POPETS’25 context (documentation only):
    - Server-side throttling and purging behaviors inform our coordinator logic,
      but diagnostics remain strictly anonymized and redacted to avoid leakage of
      EID/UT artifacts or stable identifiers.
    """
    # --- Integration metadata (manifest) ---
    integration_meta: dict[str, Any] = {}
    try:
        integ = await async_get_integration(hass, DOMAIN)
        # Name and version from manifest; both are safe to expose
        integration_meta = {
            "name": integ.name,
            "version": str(integ.version),
        }
    except Exception:
        # Stay resilient if loader fails in custom environments
        integration_meta = {}

    # --- Coordinator / runtime_data (preferred) or hass.data fallback ---
    coordinator = None
    runtime = getattr(entry, "runtime_data", None)
    if runtime:
        # Allow either a direct coordinator or a holder object with attribute "coordinator"
        coordinator = getattr(runtime, "coordinator", runtime)
    if coordinator is None:
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)

    # --- Build a compact, anonymized options snapshot (no raw strings that could contain PII) ---
    opt = entry.options
    ignored_raw = opt.get(OPT_IGNORED_DEVICES) or entry.data.get(OPT_IGNORED_DEVICES) or {}
    
    # Coerce to handle legacy list[str] format gracefully
    if isinstance(ignored_raw, list):
        ignored_count = len(ignored_raw)
    elif isinstance(ignored_raw, dict):
        ignored_count = len(ignored_raw)
    else:
        ignored_count = 0

    config_summary = {
        # Durations and numeric thresholds
        "location_poll_interval": _coerce_pos_int(opt.get(OPT_LOCATION_POLL_INTERVAL, 300), 300),
        "device_poll_delay": _coerce_pos_int(opt.get(OPT_DEVICE_POLL_DELAY, 5), 5),
        "min_accuracy_threshold": _coerce_pos_int(opt.get(OPT_MIN_ACCURACY_THRESHOLD, 100), 100),
        "movement_threshold": _coerce_pos_int(opt.get(OPT_MOVEMENT_THRESHOLD, 50), 50),
        # Feature toggles
        "google_home_filter_enabled": bool(opt.get(OPT_GOOGLE_HOME_FILTER_ENABLED, False)),
        "enable_stats_entities": bool(opt.get(OPT_ENABLE_STATS_ENTITIES, True)),
        # Token lifetime: store boolean value
        "map_view_token_expiration": bool(opt.get(OPT_MAP_VIEW_TOKEN_EXPIRATION, False)),
        # Counts only (never expose strings/IDs)
        "google_home_filter_keywords_count": _count_keywords(opt.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS)),
        "ignored_devices_count": ignored_count,
    }

    # --- Device & entity registry counts (anonymized) ---
    device_registry_counts: dict[str, Any] = {}
    try:
        dev_reg = dr.async_get(hass)
        devices_for_entry = [
            d for d in dev_reg.devices.values() if entry.entry_id in d.config_entries
        ]
        device_registry_counts["devices_count"] = len(devices_for_entry)
    except Exception:
        device_registry_counts["devices_count"] = None

    entity_registry_counts: dict[str, Any] = {}
    try:
        ent_reg = er.async_get(hass)
        entities_for_entry = [
            e for e in ent_reg.entities.values() if e.config_entry_id == entry.entry_id
        ]
        entity_registry_counts["entities_count"] = len(entities_for_entry)
    except Exception:
        entity_registry_counts["entities_count"] = None

    # --- Coordinator-derived info (all anonymized/counted) ---
    coordinator_block: dict[str, Any] = {}
    if coordinator is not None:
        # Boolean flags and counters only; never expose maps with device IDs/names
        try:
            known_devices_count = len(getattr(coordinator, "_device_names", {}) or {})
        except (AttributeError, TypeError):
            known_devices_count = None

        try:
            cache_items_count = len(getattr(coordinator, "_device_location_data", {}) or {})
        except (AttributeError, TypeError):
            cache_items_count = None

        try:
            last_poll_wall = _monotonic_to_wall_seconds(getattr(coordinator, "_last_poll_mono", None))
        except (AttributeError, TypeError):
            last_poll_wall = None

        try:
            stats = dict(getattr(coordinator, "stats", {}) or {})
            # Stats should already be anonymized counters; still ensure only numbers
            for k, v in list(stats.items()):
                if not isinstance(v, (int, float)):
                    stats[k] = None
        except (AttributeError, TypeError):
            stats = {}

        coordinator_block = {
            "is_polling": bool(getattr(coordinator, "_is_polling", False)),
            "known_devices_count": known_devices_count,
            "cache_items_count": cache_items_count,
            "last_poll_wall_ts": last_poll_wall,  # seconds since epoch (UTC)
            "stats": stats,
        }

    # --- Assemble payload (without secrets) ---
    payload: dict[str, Any] = {
        "integration": integration_meta,
        "entry": {
            # Safe metadata only; DO NOT include entry.unique_id, entry.title, or entry.data (contains secrets)
            "entry_id": entry.entry_id,
            "version": entry.version,  # config-entry schema version (safe)
            "domain": entry.domain,
        },
        "config": config_summary,
        "registries": {
            "device": device_registry_counts,
            "entity": entity_registry_counts,
        },
    }
    if coordinator_block:
        payload["coordinator"] = coordinator_block

    # --- Final safety net: redact known secret-like keys anywhere in the payload ---
    # (We already avoided including secrets, but this keeps us safe against future extensions.)
    return async_redact_data(payload, TO_REDACT)
