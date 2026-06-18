# custom_components/googlefindmy/shared_helpers.py
"""Shared utility functions for the Google Find My Device integration.

This module centralizes pure helper functions used by multiple platform modules
(``sensor.py``, ``binary_sensor.py``, ``diagnostics.py``, ``system_health.py``)
without importing the coordinator or other heavy modules.

The module is intentionally lightweight—it may only depend on the standard
library, ``homeassistant.config_entries``, and ``.const``—so that it can be
safely imported from any module in the integration without triggering circular
import chains.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry


def subentry_type(subentry: Any | None) -> str | None:
    """Return the declared subentry type for dispatcher filtering.

    Shared helper for sensor and binary_sensor platforms to avoid duplicating
    subentry introspection logic.
    """
    if subentry is None or isinstance(subentry, str):
        return None

    declared_type = getattr(subentry, "subentry_type", None)
    if isinstance(declared_type, str):
        return declared_type

    data = getattr(subentry, "data", None)
    if isinstance(data, Mapping):
        fallback_type = data.get("subentry_type") or data.get("type")
        if isinstance(fallback_type, str):
            return fallback_type
    return None


def known_ids_for_subentry_type(entry: ConfigEntry, expected_type: str) -> set[str]:
    """Return known subentry IDs matching the expected type.

    Consolidates identical logic previously duplicated in sensor.py and
    binary_sensor.py.
    """
    ids: set[str] = set()

    subentries = getattr(entry, "subentries", None)
    if isinstance(subentries, Mapping):
        for sub in subentries.values():
            if subentry_type(sub) == expected_type:
                candidate = getattr(sub, "subentry_id", None) or getattr(
                    sub, "entry_id", None
                )
                if isinstance(candidate, str) and candidate:
                    ids.add(candidate)

    runtime_data = getattr(entry, "runtime_data", None)
    subentry_manager = getattr(runtime_data, "subentry_manager", None)
    managed_subentries = getattr(subentry_manager, "managed_subentries", None)
    if isinstance(managed_subentries, Mapping):
        for sub in managed_subentries.values():
            if subentry_type(sub) == expected_type:
                candidate = getattr(sub, "subentry_id", None) or getattr(
                    sub, "entry_id", None
                )
                if isinstance(candidate, str) and candidate:
                    ids.add(candidate)

    return ids


def sanitize_state_text(text: Any, limit: int = 160) -> str:
    """Sanitize a state text value by stripping potential PII and truncating.

    Removes parenthesized content that might contain device names or email
    addresses, then truncates to ``limit`` characters.  This mirrors the
    privacy hardening applied in ``diagnostics.py`` so that binary sensor
    attributes never leak more data than the diagnostics JSON download.
    """
    try:
        s = str(text)
    except Exception:
        return ""
    # Strip parenthesized content to avoid PII leakage (e.g. device names, emails)
    s = re.sub(r"\([^)]*\)", "(*)", s)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)] + "…"


def safe_fcm_health_snapshots(receiver: Any) -> dict[str, dict[str, Any]]:
    """Safely extract FCM health snapshots from the receiver.

    Shared by ``diagnostics.py`` and ``system_health.py`` to avoid
    duplicating the defensive snapshot retrieval logic.
    """
    if not receiver:
        return {}
    try:
        result: dict[str, dict[str, Any]] = cast(
            dict[str, dict[str, Any]], receiver.get_health_snapshots()
        )
        return result
    except Exception:  # pragma: no cover - defensive guard
        return {}


# Credential keys whose values are base64 / base64url / hex / JWT tokens, or the
# numeric GCM identity material (``android_id`` / ``security_token``, both parsed
# via ``int(...)`` on the FCM login path). Those alphabets never contain a
# legitimate space, so ALL whitespace (including interior characters introduced
# by a line-wrapped copy/paste) is stripped from their values. Every other key
# keeps interior whitespace and is only trimmed at the edges, so values that may
# legitimately contain spaces (display names, email addresses) are never
# corrupted.
_SECRET_WHITELIST_KEYS: frozenset[str] = frozenset(
    {
        "owner_key",
        "shared_key",
        "aas_token",
        "oauth_token",
        "oauthToken",
        "OAuthToken",
        "access_token",
        "token",
        "adm_token",
        "admToken",
        "Auth",
        # GCM device identity under ``fcm_credentials.gcm`` -- numeric values fed
        # to ``int(android_id)`` / ``int(security_token)`` during push
        # registration; an interior space from a wrapped paste breaks login.
        "android_id",
        "security_token",
    }
)


def _normalize_secret_value(key: str, value: str) -> str:
    """Normalize one string value from a secrets bundle.

    Whitelisted credential keys have *all* whitespace removed; any other key is
    only stripped of leading/trailing whitespace. See ``_SECRET_WHITELIST_KEYS``
    for the security rationale behind the asymmetry.
    """
    if key in _SECRET_WHITELIST_KEYS:
        return "".join(value.split())
    return value.strip()


def normalize_secrets_bundle(data: Any) -> Any:
    """Return a whitespace-normalized deep copy of a secrets.json bundle.

    Copy/paste of ``secrets.json`` into the config flow can inject stray
    whitespace into credential values (a single space in ``owner_key`` breaks
    AES-GCM decryption). This walks the bundle recursively and normalizes every
    string value via :func:`_normalize_secret_value`, descending into nested
    dicts (e.g. ``fcm_credentials``) and lists.

    The function is idempotent and never mutates its input, so it is safe to
    apply to ``MappingProxyType``-wrapped structures. Non-string scalars are
    returned unchanged.
    """
    if isinstance(data, Mapping):
        return {
            key: (
                _normalize_secret_value(key, value)
                if isinstance(value, str)
                else normalize_secrets_bundle(value)
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [normalize_secrets_bundle(item) for item in data]
    return data


def normalize_fcm_entry_snapshot(entry_id: str, snap: dict[str, Any]) -> dict[str, Any]:
    """Normalize a single FCM health snapshot entry.

    Returns a dict with the common fields used by both ``diagnostics.py``
    and ``system_health.py``.  Callers can extend with additional fields.
    """
    return {
        "entry_id": entry_id,
        "healthy": bool(snap.get("healthy")),
        "run_state": snap.get("run_state"),
        "seconds_since_last_activity": snap.get("seconds_since_last_activity"),
        "activity_stale": bool(snap.get("activity_stale")),
    }
