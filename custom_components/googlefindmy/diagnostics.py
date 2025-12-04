# custom_components/googlefindmy/diagnostics.py
"""Diagnostics helpers for the Google Find My Device integration.

Design goals (HA quality scale / Platinum-ready):
- Never leak secrets or personal data (tokens, emails, device IDs, coordinates, names).
- Provide enough structured, anonymized context to debug typical issues (polling, counts, timings).
- Prefer runtime_data (modern pattern) but gracefully fall back to hass.data for older setups.
- Keep redaction centralized and defensive (include common token/email keys even if we don't expose them now).

Privacy note:
- POPETS’25 (Böttger et al., 2025) highlights that EID-related artifacts and UT bits can be used
  for correlation/identification. We therefore **over-redact** such fields, even if we never place
  them into diagnostics directly. This is a defense-in-depth safeguard to keep future changes safe.

Additional privacy hardening (message bodies):
- Coordinator "recent errors" may contain a human-readable "where(...)" prefix that can embed device
  names. We therefore strip any parenthesized content from the prefix and avoid returning the free-form
  message body entirely. Only a coarse "where" tag, error type, and timestamp are exposed.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration

from .const import (
    CONF_GOOGLE_EMAIL,
    # secrets in entry.data (must never be exposed)
    CONF_OAUTH_TOKEN,
    # defaults for options (used to avoid hard-coded literals)
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_ENABLE_STATS_ENTITIES,
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MOVEMENT_THRESHOLD,
    DOMAIN,
    OPT_DEVICE_POLL_DELAY,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_IGNORED_DEVICES,
    # user-facing options (non-secret)
    OPT_LOCATION_POLL_INTERVAL,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MOVEMENT_THRESHOLD,
)
from .ha_typing import callback

# ---------------------------------------------------------------------------
# Compatibility placeholders
# ---------------------------------------------------------------------------


class GoogleFindMyCoordinator:  # pragma: no cover - patched in tests
    """Placeholder coordinator type for tests to monkeypatch."""


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


def _monotonic_to_wall_seconds(last_mono: float | None) -> float | None:
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


def _iso_utc(ts: float | None) -> str | None:
    """Render epoch seconds as ISO 8601 UTC string, or None."""
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except Exception:
        return None


def _safe_truncate(text: Any, limit: int = 160) -> str:
    """Return a short, non-sensitive representation of a value."""
    try:
        s = str(text)
    except Exception:
        return ""
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def _status_snapshot_to_dict(snapshot: Any) -> dict[str, Any] | None:
    """Serialize a StatusSnapshot dataclass (state/reason/changed_at)."""

    if snapshot is None:
        return None

    try:
        if is_dataclass(snapshot):
            data = asdict(snapshot)
        else:
            data = {
                "state": getattr(snapshot, "state", None),
                "reason": getattr(snapshot, "reason", None),
                "changed_at": getattr(snapshot, "changed_at", None),
            }
    except Exception:
        return None

    changed_at = data.get("changed_at")
    if not isinstance(changed_at, (int, float)):
        data["changed_at"] = None

    state = data.get("state")
    if state is not None and not isinstance(state, (str, int, float, bool)):
        data["state"] = str(state)

    reason = data.get("reason")
    if reason is not None and not isinstance(reason, str):
        data["reason"] = str(reason)

    return {
        "state": data.get("state"),
        "reason": data.get("reason"),
        "changed_at": data.get("changed_at"),
    }


def _sanitize_diag_entry(payload: Any) -> dict[str, Any]:
    """Return a diagnostics-friendly snapshot of a buffer entry."""
    if not isinstance(payload, dict):
        return {}

    sanitized: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str):
            # Skip unknown key types to avoid leaking repr() content.
            continue

        lowered_key = key.casefold()
        # Drop any name/title/label-like keys to avoid leaking human readable names.
        if "name" in lowered_key or "title" in lowered_key or "label" in lowered_key:
            continue

        if isinstance(value, (int, float, bool)) or value is None:
            sanitized[key] = value
        else:
            sanitized[key] = _safe_truncate(value)
    return sanitized


def _diagnostics_buffer_summary(raw: Any) -> dict[str, Any]:
    """Sanitize a diagnostics buffer payload for coordinator diagnostics."""
    if not isinstance(raw, dict):
        return {}

    summary: dict[str, Any] = {}

    raw_summary = raw.get("summary")
    if isinstance(raw_summary, dict):
        sanitized_summary: dict[str, Any] = {}
        for key, value in raw_summary.items():
            if isinstance(value, (int, float)):
                sanitized_summary[key] = value
            else:
                sanitized_summary[key] = _safe_truncate(value, 48)
        if sanitized_summary:
            summary["summary"] = sanitized_summary

    raw_warnings = raw.get("warnings")
    if isinstance(raw_warnings, list) and raw_warnings:
        summary["warnings_preview"] = [
            _sanitize_diag_entry(item) for item in raw_warnings[:5]
        ]

    raw_errors = raw.get("errors")
    if isinstance(raw_errors, list) and raw_errors:
        summary["errors_preview"] = [
            _sanitize_diag_entry(item) for item in raw_errors[:5]
        ]

    return summary


def _perf_durations(perf: dict[str, Any]) -> dict[str, Any]:
    """Compute stable setup durations (seconds) from monotonic stamps if present."""
    try:
        start = float(perf.get("setup_start_monotonic", 0) or 0)
        end = float(perf.get("setup_end_monotonic", 0) or 0)
        fcm = float(perf.get("fcm_acquired_monotonic", 0) or 0)
    except Exception:
        return {}

    out: dict[str, Any] = {}
    if start > 0 and end > 0 and end >= start:
        out["total_setup_duration_seconds"] = round(end - start, 3)
    if start > 0 and fcm >= start:
        out["fcm_acquisition_duration_seconds"] = round(fcm - start, 3)
    return out


def _concurrency_block(hass: HomeAssistant) -> dict[str, int]:
    """Return contention counters collected during setup/runtime."""
    bucket = hass.data.get(DOMAIN, {}) or {}
    return {
        "fcm_lock_contention_count": int(
            bucket.get("fcm_lock_contention_count", 0) or 0
        ),
        "services_lock_contention_count": int(
            bucket.get("services_lock_contention_count", 0) or 0
        ),
    }


def _fcm_receiver_state(hass: HomeAssistant) -> dict[str, Any] | None:
    """Summarize FCM receiver runtime health without leaking internals."""
    bucket = hass.data.get(DOMAIN, {}) or {}
    rcvr = bucket.get("fcm_receiver")
    if not rcvr:
        return None

    snapshots: dict[str, dict[str, Any]] = {}
    try:
        snapshots = rcvr.get_health_snapshots()
    except Exception:  # pragma: no cover - defensive guard
        snapshots = {}

    entries = []
    connected_entries: list[str] = []
    for entry_id, snap in snapshots.items():
        if snap.get("healthy"):
            connected_entries.append(entry_id)

        entries.append(
            {
                "entry_id": entry_id,
                "healthy": bool(snap.get("healthy")),
                "supervisor_running": bool(snap.get("supervisor_running")),
                "client_ready": bool(snap.get("client_ready")),
                "run_state": snap.get("run_state"),
                "do_listen": bool(snap.get("do_listen")),
                "last_activity_monotonic": snap.get("last_activity_monotonic"),
                "seconds_since_last_activity": snap.get(
                    "seconds_since_last_activity"
                ),
                "activity_stale": bool(snap.get("activity_stale")),
            }
        )

    def _get(attr: str, default: Any = None) -> Any:
        try:
            return getattr(rcvr, attr, default)
        except Exception:
            return default

    last_start = _get("last_start_monotonic", 0.0)
    seconds_since_last_start = None
    try:
        if isinstance(last_start, (int, float)) and last_start > 0:
            seconds_since_last_start = round(time.monotonic() - float(last_start), 2)
    except Exception:
        seconds_since_last_start = None

    return {
        "connected_entries": sorted(connected_entries),
        "entries": entries,
        "ref_count": int(bucket.get("fcm_refcount", 0) or 0),
        "start_count": int(_get("start_count", 0) or 0),
        "seconds_since_last_start": seconds_since_last_start,
        "activity_stale_after_seconds": _get("_activity_stale_after_s"),
    }


def _recent_errors_block(coordinator: Any) -> list[dict[str, Any]] | None:
    """Convert coordinator.recent_errors (deque) to a redacted list.

    Original intent:
        Return a bounded list of recent non-fatal errors for diagnostics.

    Correction / privacy hardening:
        Messages may include a 'where(...)' prefix that could embed device display names.
        We now extract only the coarse 'where' label (text before the first ':') and
        replace any parenthesized content with a generic placeholder '(*)'. The free-form
        message body is **not** included to avoid PII leakage.
    """
    try:
        recent = getattr(coordinator, "recent_errors", None)
    except Exception:
        recent = None
    if not recent:
        return None

    items: list[dict[str, Any]] = []
    # recent is expected to be a deque of (ts, type, msg)
    for row in list(recent):
        try:
            ts, etype, msg = row
        except Exception:
            # Be defensive with unknown tuple shapes
            ts, etype, msg = (None, None, None)

        # Extract a safe "where" tag (prefix up to ':') and scrub parentheses content.
        where = None
        try:
            prefix = str(msg or "").split(":", 1)[0].strip()
            where = re.sub(r"\([^)]*\)", "(*)", prefix)
        except Exception:
            where = None

        items.append(
            {
                "timestamp": _iso_utc(ts),
                "error_type": _safe_truncate(etype, 64),
                "where": _safe_truncate(where, 64),
            }
        )
    return items or None


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

    # --- Coordinator / runtime_data (typed container) ---
    coordinator: Any | None = None
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        candidate = getattr(runtime, "coordinator", runtime)
        if candidate is not None:
            coordinator = candidate

    # --- Build a compact, anonymized options snapshot (no raw strings that could contain PII) ---
    try:
        effective_config: dict[str, Any] = dict(entry.data)
    except Exception:
        effective_config = {}

    if isinstance(entry.options, Mapping):
        effective_config.update(entry.options)
    else:  # pragma: no cover - defensive fallback
        try:
            effective_config.update(dict(entry.options))
        except Exception:
            effective_config = dict(effective_config)

    keywords_count = _count_keywords(
        effective_config.get(
            OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS
        )
    )

    ignored_raw = effective_config.get(OPT_IGNORED_DEVICES) or {}

    effective_config_for_diag = dict(effective_config)

    # Coerce to handle legacy list[str] format gracefully
    if isinstance(ignored_raw, list):
        ignored_count = len(ignored_raw)
    elif isinstance(ignored_raw, dict):
        ignored_count = len(ignored_raw)
    else:
        ignored_count = 0

    if ignored_count:
        effective_config_for_diag[OPT_IGNORED_DEVICES] = [REDACTED] * ignored_count
    elif OPT_IGNORED_DEVICES in effective_config_for_diag:
        effective_config_for_diag[OPT_IGNORED_DEVICES] = []

    if keywords_count:
        effective_config_for_diag[OPT_GOOGLE_HOME_FILTER_KEYWORDS] = [
            REDACTED
        ] * keywords_count
    elif OPT_GOOGLE_HOME_FILTER_KEYWORDS in effective_config_for_diag:
        effective_config_for_diag[OPT_GOOGLE_HOME_FILTER_KEYWORDS] = []

    redacted_effective_config = async_redact_data(effective_config_for_diag, TO_REDACT)

    config_summary = {
        # Durations and numeric thresholds
        "location_poll_interval": _coerce_pos_int(
            effective_config.get(
                OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL
            ),
            DEFAULT_LOCATION_POLL_INTERVAL,
        ),
        "device_poll_delay": _coerce_pos_int(
            effective_config.get(OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY),
            DEFAULT_DEVICE_POLL_DELAY,
        ),
        "min_accuracy_threshold": _coerce_pos_int(
            effective_config.get(
                OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD
            ),
            DEFAULT_MIN_ACCURACY_THRESHOLD,
        ),
        "movement_threshold": _coerce_pos_int(
            effective_config.get(OPT_MOVEMENT_THRESHOLD, DEFAULT_MOVEMENT_THRESHOLD),
            DEFAULT_MOVEMENT_THRESHOLD,
        ),
        # Feature toggles
        "google_home_filter_enabled": bool(
            effective_config.get(
                OPT_GOOGLE_HOME_FILTER_ENABLED, DEFAULT_GOOGLE_HOME_FILTER_ENABLED
            )
        ),
        "enable_stats_entities": bool(
            effective_config.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES)
        ),
        # Token lifetime: store boolean value
        "map_view_token_expiration": bool(
            effective_config.get(
                OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            )
        ),
        # Counts only (never expose strings/IDs)
        "google_home_filter_keywords_count": keywords_count,
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
            cache_items_count = len(
                getattr(coordinator, "_device_location_data", {}) or {}
            )
        except (AttributeError, TypeError):
            cache_items_count = None

        try:
            last_poll_wall = _monotonic_to_wall_seconds(
                getattr(coordinator, "_last_poll_mono", None)
            )
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

        # Performance metrics (optional; only durations)
        perf_metrics = getattr(coordinator, "performance_metrics", {}) or {}
        setup_perf = _perf_durations(perf_metrics)

        # Recent, strictly redacted non-fatal errors (bounded)
        recent_errors = _recent_errors_block(coordinator)

        # Optional anonymous counters: enabled poll targets & present devices as seen last
        try:
            enabled_poll_targets_count = len(
                getattr(coordinator, "_enabled_poll_device_ids", set()) or set()
            )
        except (AttributeError, TypeError):
            enabled_poll_targets_count = None
        try:
            present_devices_seen_count = len(
                getattr(coordinator, "_present_device_ids", set()) or set()
            )
        except (AttributeError, TypeError):
            present_devices_seen_count = None

        coordinator_block = {
            "known_devices_count": known_devices_count,
            "cache_items_count": cache_items_count,
            "last_poll_wall_ts": last_poll_wall,  # seconds since epoch (UTC)
            "stats": stats,
            "enabled_poll_targets_count": enabled_poll_targets_count,
            "present_devices_seen_count": present_devices_seen_count,
            "api_status": _status_snapshot_to_dict(
                getattr(coordinator, "api_status", None)
            ),
            "fcm_status": _status_snapshot_to_dict(
                getattr(coordinator, "fcm_status", None)
            ),
        }
        if setup_perf:
            coordinator_block["setup_performance"] = setup_perf
        if recent_errors:
            coordinator_block["recent_errors"] = recent_errors

        diag_buffer = getattr(coordinator, "_diag", None)
        if diag_buffer is not None and hasattr(diag_buffer, "to_dict"):
            try:
                raw_diag = diag_buffer.to_dict()
            except Exception:
                raw_diag = None
            sanitized_diag = _diagnostics_buffer_summary(raw_diag)
            if sanitized_diag:
                coordinator_block["diagnostics_buffer"] = sanitized_diag

    # Concurrency & FCM receiver (global, not per-entry)
    concurrency = _concurrency_block(hass)
    fcm_state = _fcm_receiver_state(hass)

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
        "effective_config": redacted_effective_config,
        "registries": {
            "device": device_registry_counts,
            "entity": entity_registry_counts,
        },
        "concurrency": concurrency,
    }
    if coordinator_block:
        payload["coordinator"] = coordinator_block
    if fcm_state:
        payload["fcm_receiver_state"] = fcm_state

    # --- Final safety net: redact known secret-like keys anywhere in the payload ---
    # (We already avoided including secrets, but this keeps us safe against future extensions.)
    return async_redact_data(payload, TO_REDACT)
# Consistent placeholder used when redacting fields.
REDACTED = "**REDACTED**"

_T = TypeVar("_T")


@callback
def async_redact_data(data: _T, to_redact: Iterable[Any]) -> _T:
    """Redact sensitive keys from mappings or lists without importing HA's HTTP stack."""

    if not isinstance(data, (Mapping, list)):
        return data

    if isinstance(data, list):
        return cast(_T, [async_redact_data(item, to_redact) for item in data])

    redacted = dict(data)

    for key, value in list(redacted.items()):
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        if key in to_redact:
            redacted[key] = REDACTED
        elif isinstance(value, Mapping):
            redacted[key] = async_redact_data(value, to_redact)
        elif isinstance(value, list):
            redacted[key] = [async_redact_data(item, to_redact) for item in value]

    return cast(_T, redacted)
