# custom_components/googlefindmy/__init__.py
"""Google Find My Device integration for Home Assistant.

Version: 2.6.3 — Unique-ID migration, storage refactor, lifecycle hardening & service split

Highlights
----------
- Entry-scoped TokenCache (HA Store backend) with migration from legacy secrets.json.
- Multi-entry safety: robust detection of multiple *active* entries -> Repair issue and early abort
  with deterministic tie-break to avoid "mutual abort".
- Deterministic default-entry choice: set only when exactly one active entry exists; clear default on conflicts.
- One-time migration that namespaces entity unique_ids by entry_id (idempotent, collision-aware);
  migration flag is set only on full success; collisions produce a Repair issue.
- Services are registered at integration level (async_setup) so they are always visible.
- **Service metadata moved to services.yaml; handlers split into services.py (clean SoC).**
- Clean lifecycle: refcounted shared FCM receiver; coordinator shutdown & cache flush on unload.
- Defensive logging: redact tokens in URLs; never log PII (no coordinates/secrets).

Notes
-----
This module aims to be self-documenting. All public functions include precise docstrings
(purpose, parameters, errors, security considerations). Keep comments/docstrings in English.

Compatibility
-------------
- Designed for HA 2025.5+.
- Future multi-account architecture: guard logic remains compatible; behavior is strict
  (single active entry) until the multi-account feature is officially enabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import time
from typing import Any, Iterable, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er, issue_registry as ir

# Token cache (entry-scoped HA Store-backed cache + registry/facade)
from .Auth.token_cache import (
    TokenCache,
    _register_instance,
    _set_default_entry_id,
    _unregister_instance,
    async_set_cached_value,
)
# Username key normalization
from .Auth.username_provider import username_string
# Shared FCM provider (HA-managed singleton)
from .Auth.fcm_receiver_ha import FcmReceiverHA
from .NovaApi.ExecuteAction.LocateTracker.location_request import (
    register_fcm_receiver_provider as loc_register_fcm_provider,
    unregister_fcm_receiver_provider as loc_unregister_fcm_provider,
)
from .api import (
    register_fcm_receiver_provider as api_register_fcm_provider,
    unregister_fcm_receiver_provider as api_unregister_fcm_provider,
)
from .const import (
    ATTR_DEVICE_IDS,
    ATTR_MODE,
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_SECRET_BUNDLE,
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MIN_POLL_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    MODE_MIGRATE,
    MODE_REBUILD,
    OPTION_KEYS,
    OPT_ALLOW_HISTORY_FALLBACK,
    OPT_DEVICE_POLL_DELAY,
    OPT_IGNORED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MIN_POLL_INTERVAL,
    OPT_OPTIONS_SCHEMA_VERSION,
    REBUILD_REGISTRY_MODES,
    SERVICE_LOCATE_DEVICE,
    SERVICE_LOCATE_EXTERNAL,
    SERVICE_PLAY_SOUND,
    SERVICE_REBUILD_REGISTRY,
    SERVICE_REFRESH_DEVICE_URLS,
    SERVICE_STOP_SOUND,
    coerce_ignored_mapping,
)
from .coordinator import GoogleFindMyCoordinator
from .map_view import GoogleFindMyMapRedirectView, GoogleFindMyMapView

# Eagerly import diagnostics to prevent blocking calls on-demand
from . import diagnostics  # noqa: F401

# Service registration has been moved to a dedicated module (clean separation of concerns)
from .services import async_register_services

# Optional feature: GoogleHomeFilter (guard import to avoid hard dependency)
try:
    from .google_home_filter import GoogleHomeFilter  # type: ignore
except Exception:  # pragma: no cover
    GoogleHomeFilter = None  # type: ignore

_LOGGER = logging.getLogger(__name__)

# Platforms provided by this integration
PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


# ---- Runtime typing helpers -------------------------------------------------
class RuntimeData:
    """Container for per-entry runtime structures.

    Attributes:
        coordinator: The entry's GoogleFindMyCoordinator instance.
    """

    def __init__(self, coordinator: GoogleFindMyCoordinator) -> None:
        self.coordinator = coordinator


MyConfigEntry = ConfigEntry


# --- BEGIN: Helpers for resolution and manual locate ---------------------------
def _resolve_canonical_from_any(hass: HomeAssistant, arg: str) -> Tuple[str, str]:
    """Resolve HA device_id/entity_id/canonical_id -> (canonical_id, friendly_name).

    Resolution order:
    1) If `arg` is a Home Assistant `device_id`: extract our (DOMAIN, identifier)
       from the device registry. Fails if not found/invalid.
    2) If `arg` is an `entity_id`: lookup entity; if it belongs to our DOMAIN
       and is linked to a device, extract the identifier from that device.
    3) Otherwise: treat `arg` as already-canonical Google ID.

    Raises:
        HomeAssistantError: if `arg` is a `device_id`/`entity_id` but cannot be mapped
            to a valid identifier of this integration.

    Security:
        Do not include secrets or coordinates in raised messages or logs.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    # 1) device_id
    dev = dev_reg.async_get(arg)
    if dev:
        for item in dev.identifiers:
            try:
                domain, ident = item  # expected 2-tuple
            except (TypeError, ValueError):
                continue
            if domain == DOMAIN and isinstance(ident, str) and ident:
                friendly = (dev.name_by_user or dev.name or ident).strip()
                return ident, friendly
        raise HomeAssistantError(f"Device '{arg}' has no valid {DOMAIN} identifier")

    # 2) entity_id
    if "." in arg:
        ent = ent_reg.async_get(arg)
        if ent and ent.platform == DOMAIN and ent.device_id:
            dev = dev_reg.async_get(ent.device_id)
            if dev:
                for item in dev.identifiers:
                    try:
                        domain, ident = item
                    except (TypeError, ValueError):
                        continue
                    if domain == DOMAIN and isinstance(ident, str) and ident:
                        friendly = (dev.name_by_user or dev.name or ident).strip()
                        return ident, friendly
            raise HomeAssistantError(
                f"Entity '{arg}' is not linked to a valid {DOMAIN} device"
            )

    # 3) fallback: assume canonical id already
    return arg, arg


async def async_handle_manual_locate(
    hass: HomeAssistant, coordinator: GoogleFindMyCoordinator, arg: str
) -> None:
    """Handle manual locate button: resolve target, dispatch, and log correctly.

    Behavior:
        - Resolve any incoming identifier (`device_id`, `entity_id`, or canonical).
        - On success: dispatch the request to the coordinator and log an info line.
        - On failure: raise HomeAssistantError and mirror a redacted error record
          into the coordinator diagnostics buffer (if present).

    This function should be called by your button entity handler.
    """
    try:
        canonical_id, friendly = _resolve_canonical_from_any(hass, arg)
        await coordinator.async_locate_device(canonical_id)
        _LOGGER.info("Successfully submitted manual locate for %s", friendly)
    except HomeAssistantError as err:
        # Redacted, bounded diagnostics record
        if getattr(coordinator, "_diag", None):
            coordinator._diag.add_error(  # type: ignore[attr-defined]
                code="manual_locate_resolution_failed",
                context={
                    "device_id": "",  # unknown (arg may not be a device_id)
                    "arg": str(arg)[:64],  # redact length
                    "reason": str(err)[:160],
                },
            )
        _LOGGER.error("Locate failed for '%s': %s", arg, err)
        raise


# --- END: Helpers for resolution and manual locate -----------------------------


def _redact_url_token(url: str) -> str:
    """Return URL with any 'token' query parameter value redacted for safe logging."""
    try:
        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        redacted: list[tuple[str, str]] = []
        for k, v in q:
            if k.lower() == "token" and v:
                red_v = (v[:2] + "…" + v[-2:]) if len(v) > 4 else "****"
                redacted.append((k, red_v))
            else:
                redacted.append((k, v))
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(redacted, doseq=True),
                parts.fragment,
            )
        )
    except Exception:  # pragma: no cover
        return url


def _is_active_entry(entry: ConfigEntry) -> bool:
    """Return True if the entry is considered *active* for guard logic.

    We treat only well-defined 'working' states as active to avoid drift:
    - LOADED: entry is fully operational
    - SETUP_IN_PROGRESS / SETUP_RETRY: entry is being (re)initialized
    All other states are considered non-active.

    Rationale:
        An explicit allow-list is resilient to future/legacy state matrices and
        avoids misclassifying entries during transient states.
    """
    if entry.disabled_by:
        return False
    return entry.state in {
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.SETUP_RETRY,
    }


def _primary_active_entry(entries: list[ConfigEntry]) -> Optional[ConfigEntry]:
    """Pick a deterministic 'primary' active entry to avoid mutual aborts.

    Tie-break rule (stable, minimalistic):
        1) Prefer entries that are LOADED over all others.
        2) Otherwise, pick the lexicographically smallest entry_id.

    Returns:
        The chosen ConfigEntry, or None if no active entries exist.
    """
    active = [e for e in entries if _is_active_entry(e)]
    if not active:
        return None
    # Prefer LOADED
    loaded = [e for e in active if e.state == ConfigEntryState.LOADED]
    pool = loaded or active
    return sorted(pool, key=lambda e: e.entry_id)[0]


def _clear_default_entry_id() -> None:
    """Best-effort: clear the default entry in the TokenCache registry.

    TokenCache may or may not accept None; fall back to empty string.
    """
    try:
        _set_default_entry_id(None)  # type: ignore[arg-type]
    except Exception:
        try:
            _set_default_entry_id("")  # type: ignore[arg-type]
        except Exception:
            # Last resort: do nothing; downstream reads must handle missing default.
            pass


async def _async_save_secrets_data(secrets_data: dict) -> None:
    """Persist a legacy secrets.json bundle into the async token cache.

    Notes:
        - Only called for the legacy secrets.json path.
        - Store JSON-serializable values *as-is*. TokenCache validates and normalizes.
    """
    enhanced_data = dict(secrets_data)

    # Normalize username key across old/new secrets variants
    google_email = secrets_data.get("username", secrets_data.get("Email"))
    if google_email:
        enhanced_data[username_string] = google_email

    for key, value in enhanced_data.items():
        try:
            # Store primitives and JSON-safe structures directly
            if isinstance(value, (str, int, float, bool)) or isinstance(
                value, (dict, list)
            ):
                await async_set_cached_value(key, value)
            else:
                # Last-resort: try to JSON-encode unknown objects
                await async_set_cached_value(key, json.dumps(value))
        except (OSError, TypeError) as err:
            _LOGGER.warning("Failed to save '%s' to persistent cache: %s", key, err)


async def _async_save_individual_credentials(
    oauth_token: str, google_email: str
) -> None:
    """Persist individual credentials (oauth_token + email) to the token cache."""
    try:
        await async_set_cached_value(CONF_OAUTH_TOKEN, oauth_token)
        await async_set_cached_value(username_string, google_email)
    except OSError as err:
        _LOGGER.warning("Failed to save individual credentials to cache: %s", err)


def _opt(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a configuration value, preferring options over data."""
    if key in entry.options:
        return entry.options.get(key, default)
    return entry.data.get(key, default)


def _effective_config(entry: ConfigEntry) -> dict[str, Any]:
    """Assemble a dict of non-secret runtime settings (options-first)."""
    return {k: _opt(entry, k, None) for k in OPTION_KEYS}


async def _async_soft_migrate_data_to_options(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Idempotently copy known settings from data -> options (never move secrets)."""
    new_options = dict(entry.options)
    changed = False
    for k in OPTION_KEYS:
        if k not in new_options and k in entry.data:
            new_options[k] = entry.data[k]
            changed = True
    if changed:
        _LOGGER.info(
            "Soft-migrating %d option(s) from data to options for '%s'",
            len(new_options) - len(entry.options),
            entry.title,
        )
        hass.config_entries.async_update_entry(entry, options=new_options)


async def _async_normalize_device_names(hass: HomeAssistant) -> None:
    """One-time normalization: strip legacy 'Find My - ' prefix from device names."""
    try:
        dev_reg = dr.async_get(hass)
        updated = 0
        for device in list(dev_reg.devices.values()):
            if not any(domain == DOMAIN for domain, _ in device.identifiers):
                continue
            if device.name_by_user:
                continue  # user-chosen names stay untouched
            name = device.name or ""
            if name.startswith("Find My - "):
                new_name = name[len("Find My - ") :].strip()
                if new_name and new_name != name:
                    dev_reg.async_update_device(device_id=device.id, name=new_name)
                    updated += 1
        if updated:
            _LOGGER.info(
                'Normalized %d device name(s) by removing legacy "Find My - " prefix',
                updated,
            )
    except Exception as err:
        _LOGGER.debug("Device name normalization skipped due to: %s", err)


async def _async_create_uid_collision_issue(
    hass: HomeAssistant, entry: ConfigEntry, entity_ids: list[str]
) -> None:
    """Create a repair issue for unique_id collisions (batched; idempotent by key)."""
    try:
        # Truncate list for message brevity; diagnostics can hold the full list.
        preview = ", ".join(entity_ids[:8]) + ("…" if len(entity_ids) > 8 else "")
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"unique_id_collision_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unique_id_collision",
            translation_placeholders={
                "entry": entry.title or entry.entry_id,
                "count": str(len(entity_ids)),
                "entities": preview or "n/a",
            },
        )
    except Exception as err:
        _LOGGER.debug("Failed to create UID collision issue: %s", err)


async def _async_migrate_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """One-time migration to namespace entity unique_ids by config entry id.

    Policy:
        - Only migrate entities that belong to *this* entry (entity.config_entry_id match).
        - Old pattern:  'googlefindmy_<rest>'
        - New pattern:  'googlefindmy_<entry_id>_<rest>'
        - Skip if already namespaced. Idempotent and collision-aware.
        - Also migrate the service device identifier from 'integration' -> f'integration_{entry.entry_id}'
          when applicable (best-effort).

    Flagging rule:
        - Set options['unique_id_migrated']=True only if ALL candidates that need migration
          (old-prefix entities for this entry) were successfully migrated (no collisions).
        - If any collisions occurred, create a Repair issue and DO NOT set the flag;
          migration will be retried on next load after user action.
    """
    if entry.options.get("unique_id_migrated") is True:
        return

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    prefix = f"{DOMAIN}_"
    namespaced_prefix = f"{DOMAIN}_{entry.entry_id}_"

    total_candidates = 0
    migrated = 0
    skipped_already_scoped = 0
    skipped_nonprefix = 0
    collisions: list[str] = []

    # Entities
    for ent in list(ent_reg.entities.values()):
        try:
            if ent.platform != DOMAIN or ent.config_entry_id != entry.entry_id:
                continue
            uid = ent.unique_id or ""
            if uid.startswith(namespaced_prefix):
                skipped_already_scoped += 1
                continue
            if not uid.startswith(prefix):
                skipped_nonprefix += 1
                continue

            total_candidates += 1
            new_uid = namespaced_prefix + uid[len(prefix) :]

            # Collision check: if any entity already uses new_uid, skip and record.
            existing_eid = ent_reg.async_get_entity_id(ent.domain, ent.platform, new_uid)
            if existing_eid:
                _LOGGER.warning(
                    "Unique-ID migration skipped (collision): %s -> %s (existing=%s)",
                    uid,
                    new_uid,
                    existing_eid,
                )
                collisions.append(ent.entity_id)
                continue

            # Perform migration (HA handles index rebuild)
            ent_reg.async_update_entity(ent.entity_id, new_unique_id=new_uid)
            migrated += 1
        except Exception as err:
            _LOGGER.debug("Unique ID migration failed for %s: %s", ent.entity_id, err)

    # Service device identifier
    try:
        for device in list(dev_reg.devices.values()):
            if entry.entry_id not in device.config_entries:
                continue
            if (DOMAIN, "integration") in device.identifiers:
                new_identifiers = set(device.identifiers)
                new_identifiers.remove((DOMAIN, "integration"))
                new_identifiers.add((DOMAIN, f"integration_{entry.entry_id}"))
                dev_reg.async_update_device(
                    device_id=device.id, new_identifiers=new_identifiers
                )
                _LOGGER.info(
                    "Migrated integration service device identifier for entry '%s'",
                    entry.entry_id,
                )
    except Exception as err:
        _LOGGER.debug("Service device identifier migration skipped: %s", err)

    # Finalize flag / issue
    if collisions:
        await _async_create_uid_collision_issue(hass, entry, collisions)
        _LOGGER.warning(
            "Unique-ID migration incomplete for '%s': migrated=%d / total_needed=%d, collisions=%d",
            entry.title,
            migrated,
            total_candidates,
            len(collisions),
        )
        # Do NOT set the migration flag; we want to retry after user resolves collisions.
    else:
        new_opts = dict(entry.options)
        new_opts["unique_id_migrated"] = True
        if new_opts != entry.options:
            hass.config_entries.async_update_entry(entry, options=new_opts)
        if total_candidates or migrated:
            _LOGGER.info(
                "Unique-ID migration complete for '%s': migrated=%d, already_scoped=%d, nonprefix=%d",
                entry.title,
                migrated,
                skipped_already_scoped,
                skipped_nonprefix,
            )


# --------------------------- Shared FCM provider ---------------------------


async def _async_acquire_shared_fcm(hass: HomeAssistant) -> FcmReceiverHA:
    """Get or create the shared FCM receiver for this HA instance.

    Behavior:
        - Creates and initializes the singleton if missing.
        - Registers provider callbacks for API and LocateTracker once.
        - Maintains a reference counter to support multiple entries.

    Security:
        The FCM receiver holds no long-term secrets here; do not log PII or full tokens.
    """
    bucket = hass.data.setdefault(DOMAIN, {})
    fcm_lock = bucket.setdefault("fcm_lock", asyncio.Lock())
    # Contention monitoring: increment if someone else holds the lock right now
    if fcm_lock.locked():
        bucket["fcm_lock_contention_count"] = (
            int(bucket.get("fcm_lock_contention_count", 0)) + 1
        )
    async with fcm_lock:
        refcount = int(bucket.get("fcm_refcount", 0))
        fcm: FcmReceiverHA | None = bucket.get("fcm_receiver")

        if fcm is None:
            fcm = FcmReceiverHA()
            _LOGGER.debug("Initializing shared FCM receiver...")
            ok = await fcm.async_initialize()
            if not ok:
                raise ConfigEntryNotReady("Failed to initialize FCM receiver")
            bucket["fcm_receiver"] = fcm
            _LOGGER.info("Shared FCM receiver initialized")

            # Register provider for both consumer modules (exactly once on first acquire)
            loc_register_fcm_provider(lambda: hass.data[DOMAIN].get("fcm_receiver"))
            api_register_fcm_provider(lambda: hass.data[DOMAIN].get("fcm_receiver"))

        bucket["fcm_refcount"] = refcount + 1
        _LOGGER.debug("FCM refcount -> %s", bucket["fcm_refcount"])
        return fcm


async def _async_release_shared_fcm(hass: HomeAssistant) -> None:
    """Decrease refcount; stop and unregister provider when it reaches zero."""
    bucket = hass.data.setdefault(DOMAIN, {})
    fcm_lock = bucket.setdefault("fcm_lock", asyncio.Lock())
    async with fcm_lock:
        refcount = int(bucket.get("fcm_refcount", 0)) - 1
        refcount = max(refcount, 0)
        bucket["fcm_refcount"] = refcount
        _LOGGER.debug("FCM refcount -> %s", refcount)

        if refcount != 0:
            return

        fcm: FcmReceiverHA | None = bucket.pop("fcm_receiver", None)

        # Unregister providers first (consumers will see provider=None immediately)
        try:
            loc_unregister_fcm_provider()
        except Exception:
            pass
        try:
            api_unregister_fcm_provider()
        except Exception:
            pass

        if fcm is not None:
            try:
                await fcm.async_stop()
                _LOGGER.info("Shared FCM receiver stopped")
            except Exception as err:
                _LOGGER.warning("Stopping FCM receiver failed: %s", err)


# ------------------------------ Setup / Unload -----------------------------


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration namespace and register global services.

    Rationale:
        Services must be registered from async_setup so they are always available,
        even if no config entry is loaded, which enables frontend validation of
        automations referencing these services.

    Implementation:
        - Register services once (idempotent) and keep their metadata in services.yaml.
        - Pass a small context dict to services.py to avoid circular imports.
    """
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket.setdefault("entries", {})  # entry_id -> RuntimeData

    # Use a lock + idempotent flag to avoid double registration on racey startups.
    services_lock = bucket.setdefault("services_lock", asyncio.Lock())
    async with services_lock:
        if not bucket.get("services_registered"):
            # Build a small context object to avoid import cycles and keep services.yaml the SSoT.
            svc_ctx = {
                "domain": DOMAIN,
                "resolve_canonical": _resolve_canonical_from_any,
                "is_active_entry": _is_active_entry,
                "primary_active_entry": _primary_active_entry,
                "opt": _opt,
                "default_map_view_token_expiration": DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
                "opt_map_view_token_expiration_key": OPT_MAP_VIEW_TOKEN_EXPIRATION,
                "redact_url_token": _redact_url_token,
                # Minimal fix: allow services.py to perform the real soft data->options migration when requested.
                "soft_migrate_entry": _async_soft_migrate_data_to_options,
            }
            await async_register_services(hass, svc_ctx)
            bucket["services_registered"] = True
            _LOGGER.debug("Registered %s services at integration level", DOMAIN)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    """Set up a config entry.

    Order of operations (important):
      1) Multi-entry guard: if more than one *active* entry exists, choose a deterministic
         'primary' entry and abort all others with a Repair issue (avoid mutual abort); clear default.
      2) Initialize and register TokenCache (includes legacy migration).
      3) Soft-migrate options and unique_ids; acquire and wire the shared FCM provider.
      4) Seed token cache from entry data (secrets bundle or individual tokens).
      5) Build coordinator, register views, forward platforms.
      6) Schedule initial refresh after HA is fully started.

    Notes:
        * FCM acquisition/registration happens through a single deterministic path.
        * Default-entry is only set if exactly one active entry exists (deterministic).
        * Guard is strict-by-design (single active entry) but remains compatible with
          a future multi-account architecture.
    """
    # --- Multi-entry guard (robust & early) -----------------------------------
    all_entries = hass.config_entries.async_entries(DOMAIN)
    # An active entry is one that is not disabled and is currently loaded or trying to load.
    active_entries = [e for e in all_entries if _is_active_entry(e)]

    if len(active_entries) > 1:
        # If there are multiple active entries, create a repair issue and fail setup for all of them.
        # This prevents race conditions and ensures a consistent state.
        ir.async_create_issue(
            hass,
            DOMAIN,
            "multiple_config_entries",
            is_fixable=False,  # User must manually delete the entries.
            severity=ir.IssueSeverity.ERROR,
            translation_key="multiple_config_entries",
            translation_placeholders={
                "entries": ", ".join([e.title or e.entry_id for e in active_entries])
            },
        )
        _LOGGER.error(
            "Multiple config entries found for %s. Integration setup aborted for entry '%s'. "
            "Please remove all but one entry from Settings > Devices & Services to resolve this issue.",
            DOMAIN,
            entry.entry_id,
        )
        return False
    else:
        # If the condition is resolved (only one entry left), remove the repair issue.
        ir.async_delete_issue(hass, DOMAIN, "multiple_config_entries")

    # Monotonic performance markers (captured even before coordinator exists)
    pm_setup_start = time.monotonic()

    # Detect cold start vs. reload (survives reloads within the same HA runtime)
    domain_bucket = hass.data.setdefault(DOMAIN, {})
    is_reload = bool(domain_bucket.get("initial_setup_complete", False))

    # 1) Token cache: create/register early (before any default is set)
    legacy_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "Auth", "secrets.json"
    )
    cache = await TokenCache.create(hass, entry.entry_id, legacy_path=legacy_path)
    _register_instance(entry.entry_id, cache)

    # Deterministic default-entry: only set when this is the single active entry.
    all_entries = hass.config_entries.async_entries(DOMAIN)
    active_entries = [e for e in all_entries if _is_active_entry(e)]
    if len(active_entries) == 1 and active_entries[0].entry_id == entry.entry_id:
        _set_default_entry_id(entry.entry_id)
    else:
        _LOGGER.debug(
            "Default entry not set due to %d active entries (deterministic guard).",
            len(active_entries),
        )

    # Ensure deferred writes are flushed on HA shutdown
    async def _flush_on_stop(event) -> None:
        """Flush deferred saves on Home Assistant stop."""
        try:
            await cache.flush()
        except Exception as err:
            _LOGGER.debug("Cache flush on stop raised: %s", err)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _flush_on_stop)
    )

    # Early, idempotent seeding of TokenCache from entry.data (authoritative SSOT)
    try:
        if CONF_OAUTH_TOKEN in entry.data:
            await async_set_cached_value(CONF_OAUTH_TOKEN, entry.data[CONF_OAUTH_TOKEN])
            _LOGGER.debug("Seeded oauth_token into TokenCache from entry.data")
        if CONF_GOOGLE_EMAIL in entry.data:
            await async_set_cached_value(username_string, entry.data[CONF_GOOGLE_EMAIL])
            _LOGGER.debug("Seeded google_email into TokenCache from entry.data")
    except Exception as err:
        _LOGGER.debug("Early TokenCache seeding from entry.data failed: %s", err)

    # Optional: register HA-managed aiohttp session for Nova API (defer import)
    try:
        from .NovaApi import nova_request as nova

        reg = getattr(nova, "register_hass", None)
        unreg = getattr(nova, "unregister_session_provider", None)
        if callable(reg):
            reg(hass)
            if callable(unreg):
                entry.async_on_unload(unreg)
        else:
            _LOGGER.debug(
                "Nova API register_hass() not available; continuing with module defaults."
            )
    except Exception as err:  # Defensive: Nova module may not expose hooks in some builds
        _LOGGER.debug("Nova API session provider registration skipped: %s", err)

    # Soft-migrate mutable settings from data -> options (never secrets) and unique_ids
    await _async_soft_migrate_data_to_options(hass, entry)
    await _async_migrate_unique_ids(hass, entry)

    # Acquire shared FCM and create a startup barrier for the first poll cycle.
    fcm_ready_event = asyncio.Event()
    fcm = await _async_acquire_shared_fcm(hass)
    pm_fcm_acquired = time.monotonic()
    fcm_ready_event.set()

    # NOTE (lifecycle): Do not await long-running shutdowns inside async_on_unload.
    # We only *signal* the FCM receiver to stop here (non-blocking). The awaited
    # stop and refcount release are handled in `async_unload_entry`.
    def _on_unload_signal_fcm() -> None:
        """Signal FCM receiver to stop without awaiting (safe for async_on_unload)."""
        try:
            fcm.request_stop()
        except Exception as err:  # Defensive: never break unload pipeline
            _LOGGER.debug("FCM stop signal during unload raised: %s", err)

    entry.async_on_unload(_on_unload_signal_fcm)

    # Credentials seed: legacy bundle OR individual oauth_token+email must be present
    secrets_data = entry.data.get(DATA_SECRET_BUNDLE)
    oauth_token = entry.data.get(CONF_OAUTH_TOKEN)
    google_email = entry.data.get(CONF_GOOGLE_EMAIL)

    if secrets_data:
        await _async_save_secrets_data(secrets_data)
        _LOGGER.debug("Persisted secrets.json bundle to token cache")
    elif oauth_token and google_email:
        await _async_save_individual_credentials(oauth_token, google_email)
        _LOGGER.debug("Persisted individual credentials to token cache")
    else:
        _LOGGER.error(
            "No credentials found in config entry (neither secrets_data nor oauth_token+google_email)"
        )
        raise ConfigEntryNotReady("Credentials missing")

    # Build effective runtime settings (options-first)
    coordinator = GoogleFindMyCoordinator(
        hass,
        cache=cache,
        location_poll_interval=_opt(
            entry, OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL
        ),
        device_poll_delay=_opt(entry, OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY),
        min_poll_interval=_opt(entry, OPT_MIN_POLL_INTERVAL, DEFAULT_MIN_POLL_INTERVAL),
        min_accuracy_threshold=_opt(
            entry, OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD
        ),
        allow_history_fallback=_opt(
            entry,
            OPT_ALLOW_HISTORY_FALLBACK,
            DEFAULT_OPTIONS.get(OPT_ALLOW_HISTORY_FALLBACK, False),
        ),
    )
    coordinator.config_entry = entry  # convenience for platforms

    # Performance metrics injection (coordinator-owned dictionary)
    try:
        perf = getattr(coordinator, "performance_metrics", None)
        if not isinstance(perf, dict):
            perf = {}
            setattr(coordinator, "performance_metrics", perf)
        perf["setup_start_monotonic"] = pm_setup_start
        perf["fcm_acquired_monotonic"] = pm_fcm_acquired
    except Exception as err:
        _LOGGER.debug("Failed to set performance metrics on coordinator: %s", err)

    # Hand over the barrier without changing the coordinator's signature.
    setattr(coordinator, "fcm_ready_event", fcm_ready_event)

    # Register the coordinator with the shared FCM receiver (clear synchronous contract).
    fcm.register_coordinator(coordinator)
    entry.async_on_unload(lambda: fcm.unregister_coordinator(coordinator))

    # Ensure FCM supervisor is running for background push updates (idempotent).
    try:
        await fcm._start_listening()  # noqa: SLF001
    except AttributeError:
        _LOGGER.debug(
            "FCM receiver has no _start_listening(); relying on on-demand start via per-request registration."
        )

    # Expose runtime object for modern consumers (diagnostics, repair, etc.). No secrets.
    entry.runtime_data = coordinator
    hass.data[DOMAIN].setdefault("entries", {})[entry.entry_id] = RuntimeData(
        coordinator=coordinator
    )

    # Optional: attach Google Home filter (options-first configuration)
    if GoogleHomeFilter:
        try:
            coordinator.google_home_filter = GoogleHomeFilter(hass, _effective_config(entry))  # type: ignore[call-arg]
            _LOGGER.debug("Initialized Google Home filter (options-first)")
        except Exception as err:
            _LOGGER.debug("GoogleHomeFilter attach skipped due to: %s", err)
    else:
        _LOGGER.debug("GoogleHomeFilter not available; continuing without it")

    # Share coordinator in hass.data (legacy location for other modules)
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket[entry.entry_id] = coordinator

    # IMPORTANT: register DR listener & perform initial DR index
    try:
        await coordinator.async_setup()
    except Exception as err:
        _LOGGER.warning(
            "Coordinator setup failed early; will recover on next refresh: %s", err
        )

    # Register map views (idempotent across multi-entry)
    if not bucket.get("views_registered"):
        hass.http.register_view(GoogleFindMyMapView(hass))
        hass.http.register_view(GoogleFindMyMapRedirectView(hass))
        bucket["views_registered"] = True
        _LOGGER.debug("Registered map views")

    # Forward platforms so RestoreEntity can populate immediately
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Defer the first refresh until HA is fully started
    listener_active = False

    async def _do_first_refresh(_: Any) -> None:
        """Perform the initial coordinator refresh after HA has started.

        On reloads (warm start), we force the next poll to be due immediately
        to pick up newly added devices without waiting a full interval. Cold
        starts keep the deferred baseline to reduce startup load.
        """
        nonlocal listener_active
        listener_active = False
        try:
            if is_reload:
                _LOGGER.info(
                    "Integration reloaded: forcing an immediate device scan window."
                )
                coordinator.force_poll_due()

            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                _LOGGER.warning(
                    "Initial refresh failed; entities will recover on subsequent polls."
                )
            await _async_normalize_device_names(hass)
        except Exception as err:
            _LOGGER.error(
                "Initial refresh raised an unexpected error: %s", err, exc_info=True
            )

    if hass.state == CoreState.running:
        hass.async_create_task(
            _do_first_refresh(None), name="googlefindmy.initial_refresh"
        )
    else:
        unsub = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _do_first_refresh
        )
        listener_active = True

        def _safe_unsub() -> None:
            if listener_active:
                unsub()

        entry.async_on_unload(_safe_unsub)

    # Mark initial setup complete (used to distinguish cold start vs. reload)
    domain_bucket["initial_setup_complete"] = True

    # Final performance marker
    try:
        perf = getattr(coordinator, "performance_metrics", None)
        if isinstance(perf, dict):
            perf["setup_end_monotonic"] = time.monotonic()
    except Exception as err:
        _LOGGER.debug("Failed to set setup_end_monotonic: %s", err)

    # IMPORTANT: Do NOT add update listeners when using OptionsFlowWithReload.
    # Options changes will reload the entry automatically, rebuilding the coordinator.

    return True


async def async_unload_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    """Unload a config entry.

    Notes:
        - FCM stop is *signaled* via the unload hook registered during setup.
          The awaited stop and refcount release are handled here to avoid long
          awaits inside `async_on_unload`.
        - TokenCache is explicitly closed here to flush and mark the cache closed.
    """
    # First, shut down coordinator lifecycle (unsubscribe DR listener, timers)
    try:
        coordinator: GoogleFindMyCoordinator | None = (
            hass.data.get(DOMAIN, {}).get(entry.entry_id)
            or getattr(entry, "runtime_data", None)
        )
        if coordinator:
            await coordinator.async_shutdown()
    except Exception as err:
        _LOGGER.debug("Coordinator async_shutdown raised during unload: %s", err)

    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        # Drop runtime container
        hass.data.setdefault(DOMAIN, {}).setdefault("entries", {}).pop(
            entry.entry_id, None
        )

        # Unregister and close the TokenCache instance
        cache = _unregister_instance(entry.entry_id)
        if cache:
            try:
                await cache.close()
                _LOGGER.debug(
                    "TokenCache for entry '%s' has been flushed and closed.",
                    entry.entry_id,
                )
            except Exception as err:
                _LOGGER.warning(
                    "Closing TokenCache for entry '%s' failed: %s", entry.entry_id, err
                )

    # Release shared FCM (decrement refcount and await bounded shutdown if it reaches zero)
    try:
        await _async_release_shared_fcm(hass)
    except Exception as err:
        _LOGGER.debug("FCM release during async_unload_entry raised: %s", err)

    # Clear legacy pointer
    if ok:
        hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
        try:
            entry.runtime_data = None  # type: ignore[assignment]
        except Exception:
            pass

    return ok


# ------------------- Device removal (HA "Delete device" hook) -------------------


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Handle 'Delete device' requests from Home Assistant.

    Semantics:
    - Only act on devices owned by this integration/entry (identifier domain matches and
      the device is linked to this config entry).
    - Purge in-memory caches for the device via the coordinator (keeps UI/state clean).
    - Add the id to `ignored_devices` to prevent automatic re-creation.
    - Return True to allow HA to remove the device record and its entities.
    - Never allow removing the integration's own "service" device (ident startswith 'integration').

    Security:
        Do not log full device identifiers together with user-chosen names in error paths.
        Keep logs non-PII and bounded.
    """
    # Only handle devices that belong to this config entry
    if entry.entry_id not in device_entry.config_entries:
        return False

    # Resolve our canonical device id from identifiers
    dev_id = next(
        (ident for (domain, ident) in device_entry.identifiers if domain == DOMAIN),
        None,
    )
    if not dev_id:
        return False

    # Block deletion of the integration "service" device
    if dev_id == "integration" or dev_id == f"integration_{entry.entry_id}":
        return False

    # Purge coordinator caches (best effort; does not trigger polling)
    try:
        coordinator: GoogleFindMyCoordinator | None = hass.data.get(DOMAIN, {}).get(
            entry.entry_id
        )
        if coordinator is not None:
            coordinator.purge_device(dev_id)
    except Exception as err:
        _LOGGER.debug("Coordinator purge failed for %s: %s", dev_id, err)

    # Persist user's delete decision: add to ignored_devices mapping (idempotent, lossless)
    try:
        opts = dict(entry.options)
        # Coerce legacy shapes (list / dict[str,str]) to v2 mapping
        current_raw = opts.get(
            OPT_IGNORED_DEVICES, DEFAULT_OPTIONS.get(OPT_IGNORED_DEVICES)
        )
        ignored_map, _migrated = coerce_ignored_mapping(current_raw)

        # Determine the best human name at deletion time
        name_to_store = device_entry.name_by_user or device_entry.name or dev_id

        meta = ignored_map.get(dev_id, {})
        prev_name = meta.get("name")
        aliases = list(meta.get("aliases") or [])
        if prev_name and prev_name != name_to_store and prev_name not in aliases:
            aliases.append(prev_name)  # keep history as alias

        ignored_map[dev_id] = {
            "name": name_to_store,
            "aliases": aliases,
            "ignored_at": int(time.time()),
            "source": "registry",
        }
        opts[OPT_IGNORED_DEVICES] = ignored_map
        opts[OPT_OPTIONS_SCHEMA_VERSION] = 2

        if opts != entry.options:
            hass.config_entries.async_update_entry(entry, options=opts)
            _LOGGER.info(
                "Marked device '%s' (%s) as ignored for entry '%s'",
                name_to_store,
                dev_id,
                entry.title,
            )
    except Exception as err:
        _LOGGER.debug("Persisting delete decision failed for %s: %s", dev_id, err)

    # Allow HA to delete the device (and its entities) if no other entry still references it
    return True


# ------------------------------- Misc helpers ---------------------------------


def _get_local_ip_sync() -> str:
    """Best-effort local IP discovery via UDP connect (executor-only)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""
