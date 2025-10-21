# custom_components/googlefindmy/system_health.py
"""System health handlers for the Google Find My Device integration."""

from __future__ import annotations

import hashlib
from collections.abc import Collection
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

try:  # pragma: no cover - exercised in integration runtime, patched in tests
    from homeassistant.components import system_health as system_health_component
except ImportError:  # pragma: no cover - fallback for stripped test environments
    system_health_component = None  # type: ignore[assignment]

from .const import CONF_GOOGLE_EMAIL, DATA_SECRET_BUNDLE, DOMAIN, INTEGRATION_VERSION


def _normalize_email(value: str | None) -> str:
    """Return a normalized email address (lowercase, trimmed)."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _email_hash(entry: ConfigEntry) -> str | None:
    """Return a truncated SHA-256 hash for the account email (or None if absent)."""
    email = entry.data.get(CONF_GOOGLE_EMAIL)
    if isinstance(email, str) and email:
        normalized = _normalize_email(email)
    else:
        bundle = entry.data.get(DATA_SECRET_BUNDLE)
        normalized = ""
        if isinstance(bundle, dict):
            candidate = bundle.get("username") or bundle.get("Email")
            normalized = _normalize_email(candidate)
    if not normalized:
        return None
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def _safe_len(value: Any) -> int | None:
    """Return len(value) if it behaves like a collection, otherwise None."""
    if isinstance(value, Collection):
        try:
            return len(value)
        except Exception:  # pragma: no cover - defensive guard
            return None
    return None


def _safe_datetime(value: Any) -> str | None:
    """Return an ISO8601 timestamp for datetime values."""
    if isinstance(value, datetime):
        try:
            if value.tzinfo is not None:
                return value.isoformat()
            return value.replace(tzinfo=None).isoformat()
        except Exception:  # pragma: no cover - defensive guard
            return None
    return None


def _get_fcm_info(receiver: Any) -> dict[str, Any]:
    """Extract anonymized telemetry from the shared FCM receiver (if available)."""
    if receiver is None:
        return {"available": False, "is_ready": False}

    info: dict[str, Any] = {"available": True}

    ready_attr = getattr(receiver, "is_ready", getattr(receiver, "ready", None))
    if callable(ready_attr):
        try:
            ready_value = bool(ready_attr())
        except Exception:  # pragma: no cover - defensive
            ready_value = None
    else:
        ready_value = bool(ready_attr) if ready_attr is not None else None
    info["is_ready"] = ready_value

    for attr in ("start_count", "last_start_monotonic", "last_stop_monotonic"):
        value = getattr(receiver, attr, None)
        if isinstance(value, (int, float)):
            info[attr] = float(value)

    clients = getattr(receiver, "pcs", None)
    if isinstance(clients, dict):
        info["client_count"] = len(clients)
        info["client_ids"] = sorted(str(key) for key in clients.keys())

    return info


def _resolve_coordinator(entry: ConfigEntry, entries_bucket: dict[str, Any]) -> Any:
    """Resolve the coordinator instance for a given config entry."""
    runtime = entries_bucket.get(entry.entry_id)
    coordinator = None
    if runtime is not None:
        coordinator = getattr(runtime, "coordinator", runtime)
    if coordinator is None:
        runtime_data = getattr(entry, "runtime_data", None)
        if runtime_data is not None:
            coordinator = getattr(runtime_data, "coordinator", runtime_data)
    return coordinator


def _get_fcm_snapshot(coordinator: Any) -> dict[str, Any] | None:
    """Return a sanitized FCM status snapshot from the coordinator."""
    if coordinator is None:
        return None

    snapshot = getattr(coordinator, "fcm_status", None)
    state = getattr(snapshot, "state", None)
    reason = getattr(snapshot, "reason", None)
    changed_at = getattr(snapshot, "changed_at", None)

    if state is None and reason is None and changed_at is None:
        return None

    data: dict[str, Any] = {}
    if state is not None:
        data["state"] = state
    if reason is not None:
        data["reason"] = reason
    if isinstance(changed_at, (int, float)):
        data["changed_at"] = float(changed_at)
    return data


async def async_register(hass: HomeAssistant) -> None:
    """Register the system health info handler for this integration."""
    component = system_health_component
    if component is None:
        component = getattr(getattr(hass, "components", None), "system_health", None)
        if component is None or not hasattr(component, "async_register_info"):
            raise RuntimeError("system_health component not available")

    component.async_register_info(hass, DOMAIN, async_get_system_health_info)


def _entry_state(entry: ConfigEntry) -> str | None:
    """Return the config entry state as a plain string."""
    state = getattr(entry, "state", None)
    if state is None:
        return None
    if isinstance(state, str):
        return state
    return getattr(state, "value", str(state))


def _build_entry_payload(
    entry: ConfigEntry, coordinator: Any, *, include_stats: bool = True
) -> dict[str, Any]:
    """Construct a sanitized payload describing a single config entry."""
    payload: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "state": _entry_state(entry),
        "disabled_by": getattr(entry, "disabled_by", None),
    }

    email_hash = _email_hash(entry)
    if email_hash:
        payload["account_hash"] = email_hash

    devices_loaded = _safe_len(getattr(coordinator, "data", None))
    if devices_loaded is not None:
        payload["devices_loaded"] = devices_loaded

    last_success = _safe_datetime(
        getattr(coordinator, "last_update_success_time", None)
    )
    if last_success is None:
        alt_last = getattr(coordinator, "last_successful_update", None)
        payload_value = _safe_datetime(alt_last)
    else:
        payload_value = last_success
    if payload_value is not None:
        payload["last_successful_update"] = payload_value

    fcm_snapshot = _get_fcm_snapshot(coordinator)
    if fcm_snapshot is not None:
        payload["fcm_status"] = fcm_snapshot

    auth_active = getattr(coordinator, "is_auth_error_active", None)
    if isinstance(auth_active, bool):
        payload["auth_issue_active"] = auth_active

    if include_stats:
        stats = getattr(coordinator, "stats", None)
        if isinstance(stats, dict):
            payload["stats"] = {
                key: int(value)
                for key, value in stats.items()
                if isinstance(value, int)
            }

    return payload


async def async_get_system_health_info(hass: HomeAssistant) -> dict[str, Any]:
    """Return anonymized integration health diagnostics."""
    domain_bucket: dict[str, Any] = hass.data.get(DOMAIN, {})
    entries_bucket: dict[str, Any] = domain_bucket.get("entries", {}) or {}

    config_entries: list[ConfigEntry] = []
    manager = getattr(hass, "config_entries", None)
    if manager is not None and hasattr(manager, "async_entries"):
        try:
            config_entries = list(manager.async_entries(DOMAIN))
        except TypeError:
            config_entries = [
                entry
                for entry in manager.async_entries()
                if entry.domain == DOMAIN  # type: ignore[attr-defined]
            ]
        except Exception:  # pragma: no cover - defensive guard
            config_entries = []

    entries_payload = []
    for entry in config_entries:
        coordinator = _resolve_coordinator(entry, entries_bucket)
        entries_payload.append(_build_entry_payload(entry, coordinator))

    info: dict[str, Any] = {
        "integration_version": INTEGRATION_VERSION,
        "loaded_entries": len(entries_payload),
        "entries": entries_payload,
        "fcm": _get_fcm_info(domain_bucket.get("fcm_receiver")),
    }

    contention = domain_bucket.get("fcm_lock_contention_count")
    if isinstance(contention, int):
        info["fcm_lock_contention_count"] = contention

    return info
