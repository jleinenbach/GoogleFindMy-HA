# custom_components/googlefindmy/__init__.py
"""Google Find My Device integration for Home Assistant.

Version: 2.5 — Storage refactor & lifecycle hardening
- Use entry-scoped TokenCache (HA Store backend) with migration from legacy secrets.json.
- Enforce multi-entry safety via registry; flush/close guarantees on stop/unload.
- Preserve existing services, views, FCM supervisor wiring, and coordinator lifecycle.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.network import get_url
# from homeassistant.helpers.instance_id import async_get as async_get_instance_id  # optional

# Token cache (new: entry-scoped HA Store-backed cache + registry/facade)
from .Auth.token_cache import (
    TokenCache,
    async_set_cached_value,
    _register_instance,
    _unregister_instance,
    _set_default_entry_id,
)

# Username key normalization
from .Auth.username_provider import username_string

from .const import (
    # Core
    DOMAIN,
    # Credentials/data keys
    CONF_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DATA_AUTH_METHOD,
    # Options keys & canonical list
    OPTION_KEYS,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_POLL_INTERVAL,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_ALLOW_HISTORY_FALLBACK,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_IGNORED_DEVICES,  # persist user's delete decision
    # Defaults
    DEFAULT_OPTIONS,
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_MIN_POLL_INTERVAL,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    # Services
    SERVICE_LOCATE_DEVICE,
    SERVICE_PLAY_SOUND,
    SERVICE_STOP_SOUND,
    SERVICE_LOCATE_EXTERNAL,
    SERVICE_REFRESH_DEVICE_URLS,
    SERVICE_REBUILD_REGISTRY,
    # Rebuild service schema constants
    ATTR_MODE,
    ATTR_DEVICE_IDS,
    MODE_REBUILD,
    MODE_MIGRATE,
    REBUILD_REGISTRY_MODES,
    OPT_OPTIONS_SCHEMA_VERSION,
    coerce_ignored_mapping,
)
from .coordinator import GoogleFindMyCoordinator
from .map_view import GoogleFindMyMapRedirectView, GoogleFindMyMapView

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
# Eagerly import diagnostics to prevent blocking calls on-demand
from . import diagnostics


_LOGGER = logging.getLogger(__name__)

# Platforms provided by this integration
PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


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
            (parts.scheme, parts.netloc, parts.path, urlencode(redacted, doseq=True), parts.fragment)
        )
    except Exception:  # pragma: no cover
        return url


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
            if isinstance(value, (str, int, float, bool)) or isinstance(value, (dict, list)):
                await async_set_cached_value(key, value)
            else:
                # Last-resort: try to JSON-encode unknown objects
                await async_set_cached_value(key, json.dumps(value))
        except (OSError, TypeError) as err:
            _LOGGER.warning("Failed to save '%s' to persistent cache: %s", key, err)


async def _async_save_individual_credentials(oauth_token: str, google_email: str) -> None:
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


async def _async_soft_migrate_data_to_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
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
            _LOGGER.info('Normalized %d device name(s) by removing legacy "Find My - " prefix', updated)
    except Exception as err:
        _LOGGER.debug("Device name normalization skipped due to: %s", err)


# --------------------------- Shared FCM provider ---------------------------

async def _async_acquire_shared_fcm(hass: HomeAssistant) -> FcmReceiverHA:
    """Get or create the shared FCM receiver for this HA instance.

    Behavior:
        - Creates and initializes the singleton if missing.
        - Registers provider callbacks for API and LocateTracker once.
        - Maintains a reference counter to support multiple entries.
    """
    bucket = hass.data.setdefault(DOMAIN, {})
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
    refcount = int(bucket.get("fcm_refcount", 0)) - 1
    refcount = max(refcount, 0)
    bucket["fcm_refcount"] = refcount
    _LOGGER.debug("FCM refcount -> %s", refcount)

    if refcount == 0:
        fcm: FcmReceiverHA | None = bucket.pop("fcm_receiver", None)

        # Unregister providers first (consumers will see provider=None immediately)
        try:
            loc_unregister_fcm_provider()
        except Exception:  # noqa: BLE001
            pass
        try:
            api_unregister_fcm_provider()
        except Exception:  # noqa: BLE001
            pass

        if fcm is not None:
            try:
                await fcm.async_stop()
                _LOGGER.info("Shared FCM receiver stopped")
            except Exception as err:
                _LOGGER.warning("Stopping FCM receiver failed: %s", err)


# ------------------------------ Setup / Unload -----------------------------

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry.

    Order of operations (important):
      1) Initialize and register TokenCache (includes legacy migration).
      2) Soft-migrate options, then acquire and wire the shared FCM provider.
      3) Seed token cache from entry data (secrets bundle or individual tokens).
      4) Build coordinator, register views/services, forward platforms.
      5) Schedule initial refresh after HA is fully started.

    Notes:
        * FCM acquisition/registration now happens through a single deterministic path.
          The startup barrier is set immediately after a successful acquire, avoiding
          duplicate provider registrations or refcount bumps.
    """
    # Detect cold start vs. reload (survives reloads within the same HA runtime)
    domain_bucket = hass.data.setdefault(DOMAIN, {})
    is_reload = bool(domain_bucket.get("initial_setup_complete", False))

    # 1) Token cache: create/register early (fail-fast if ambiguous multi-entry usage occurs later)
    legacy_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Auth", "secrets.json")
    cache = await TokenCache.create(hass, entry.entry_id, legacy_path=legacy_path)
    _register_instance(entry.entry_id, cache)
    _set_default_entry_id(entry.entry_id)

    # Ensure deferred writes are flushed on HA shutdown
    async def _flush_on_stop(event) -> None:
        """Flush deferred saves on Home Assistant stop."""
        try:
            await cache.flush()
        except Exception as err:
            _LOGGER.debug("Cache flush on stop raised: %s", err)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _flush_on_stop))

    # 2) Optional: register HA-managed aiohttp session for Nova API (defer import)
    try:
        from .NovaApi import nova_request as nova
        reg = getattr(nova, "register_hass", None)
        unreg = getattr(nova, "unregister_session_provider", None)
        if callable(reg):
            reg(hass)
            if callable(unreg):
                entry.async_on_unload(unreg)
        else:
            _LOGGER.debug("Nova API register_hass() not available; continuing with module defaults.")
    except Exception as err:  # Defensive: Nova module may not expose hooks in some builds
        _LOGGER.debug("Nova API session provider registration skipped: %s", err)

    # 3) Soft-migrate mutable settings from data -> options (never secrets)
    await _async_soft_migrate_data_to_options(hass, entry)

    # 4) Acquire shared FCM and create a startup barrier for the first poll cycle.
    fcm_ready_event = asyncio.Event()
    # Single acquisition path: acquire and register providers once, then set the barrier.
    fcm = await _async_acquire_shared_fcm(hass)
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

    # 5) Seed the token cache from entry data (one of the two paths must be present)
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

    # Optional: official instance ID (kept disabled for now)
    # instance_id = await async_get_instance_id(hass)

    # 6) Build effective runtime settings (options-first)
    coordinator = GoogleFindMyCoordinator(
        hass,
        cache=cache,
        # tracked_devices removed: device inclusion via HA device enable/disable.
        location_poll_interval=_opt(entry, OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL),
        device_poll_delay=_opt(entry, OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY),
        min_poll_interval=_opt(entry, OPT_MIN_POLL_INTERVAL, DEFAULT_MIN_POLL_INTERVAL),
        min_accuracy_threshold=_opt(entry, OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD),
        allow_history_fallback=_opt(
            entry, OPT_ALLOW_HISTORY_FALLBACK, DEFAULT_OPTIONS.get(OPT_ALLOW_HISTORY_FALLBACK, False)
        ),
    )
    coordinator.config_entry = entry  # convenience for platforms

    # Hand over the barrier without changing the coordinator's signature.
    # Event is already set after successful FCM acquisition to avoid startup races.
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

    # Optional: attach Google Home filter (options-first configuration)
    from .google_home_filter import GoogleHomeFilter

    coordinator.google_home_filter = GoogleHomeFilter(hass, _effective_config(entry))
    _LOGGER.debug("Initialized Google Home filter (options-first)")

    # Share coordinator in hass.data
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket[entry.entry_id] = coordinator

    # Register map views (idempotent across multi-entry)
    if not bucket.get("views_registered"):
        hass.http.register_view(GoogleFindMyMapView(hass))
        hass.http.register_view(GoogleFindMyMapRedirectView(hass))
        bucket["views_registered"] = True
        _LOGGER.debug("Registered map views")

    # Register services (available regardless of data freshness; idempotent)
    await _async_register_services(hass, coordinator)

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
                _LOGGER.info("Integration reloaded: forcing an immediate device scan window.")
                coordinator.force_poll_due()

            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                _LOGGER.warning("Initial refresh failed; entities will recover on subsequent polls.")
            await _async_normalize_device_names(hass)
        except Exception as err:
            _LOGGER.error("Initial refresh raised an unexpected error: %s", err, exc_info=True)

    if hass.state == CoreState.running:
        hass.async_create_task(_do_first_refresh(None), name="googlefindmy.initial_refresh")
    else:
        unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _do_first_refresh)
        listener_active = True

        def _safe_unsub() -> None:
            if listener_active:
                unsub()

        entry.async_on_unload(_safe_unsub)

    # Mark initial setup complete (used to distinguish cold start vs. reload)
    domain_bucket["initial_setup_complete"] = True

    # IMPORTANT: Do NOT add update listeners when using OptionsFlowWithReload.
    # Options changes will reload the entry automatically, rebuilding the coordinator.

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and its platforms.

    Notes:
        - FCM stop is *signaled* via the unload hook registered during setup.
          The awaited stop and refcount release are handled here to avoid long
          awaits inside `async_on_unload`.
        - TokenCache is explicitly closed here to flush and mark the cache closed.
    """
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Release shared FCM (decrement refcount and await bounded shutdown if it reaches zero)
    try:
        await _async_release_shared_fcm(hass)
    except Exception as err:
        _LOGGER.debug("FCM release during async_unload_entry raised: %s", err)

    # Unregister and close the TokenCache instance
    cache = _unregister_instance(entry.entry_id)
    if cache:
        try:
            await cache.close()
            _LOGGER.debug("TokenCache for entry '%s' has been flushed and closed.", entry.entry_id)
        except Exception as err:
            _LOGGER.warning("Closing TokenCache for entry '%s' failed: %s", entry.entry_id, err)

    if unload_ok:
        # Drop coordinator from hass.data
        hass.data.setdefault(DOMAIN, {}).pop(entry.entry_id, None)
        # Clear runtime_data to avoid holding references after unload.
        try:
            entry.runtime_data = None  # type: ignore[assignment]
        except Exception:
            # Defensive: older cores may not expose runtime_data; ignore cleanly.
            pass

    return unload_ok


# ------------------------------- Services ---------------------------------

def _get_local_ip_sync() -> str:
    """Best-effort local IP discovery via UDP connect (executor-only)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


async def _async_register_services(hass: HomeAssistant, coordinator: GoogleFindMyCoordinator) -> None:
    """Register services for the integration (idempotent per-HA instance)."""
    # Guard: register services only once per HA instance
    domain_bucket = hass.data.setdefault(DOMAIN, {})
    if domain_bucket.get("services_registered"):
        return

    def _resolve_canonical_from_any(arg: str) -> tuple[str, str]:
        """Resolve any device identifier to (canonical_id, friendly_name)."""
        # 1) Treat as HA device_id
        dev = dr.async_get(hass).async_get(arg)
        if dev:
            for domain, ident in dev.identifiers:
                if domain == DOMAIN:
                    name = dev.name_by_user or dev.name or ident
                    return ident, name

        # 2) Treat as entity_id
        if "." in arg:
            ent = er.async_get(hass).async_get(arg)
            if ent and ent.platform == DOMAIN and ent.device_id:
                dev = dr.async_get(hass).async_get(ent.device_id)
                if dev:
                    for domain, ident in dev.identifiers:
                        if domain == DOMAIN:
                            name = dev.name_by_user or dev.name or ident
                            return ident, name

        # 3) Fallback: assume arg is the canonical Google ID
        name = coordinator.get_device_display_name(arg) or arg
        if name != arg:
            return arg, name
        raise ValueError(f"Identifier '{arg}' could not be resolved to a known device")

    async def async_locate_device_service(call: ServiceCall) -> None:
        """Handle locate device service call."""
        raw = call.data["device_id"]
        try:
            canonical_id, friendly = _resolve_canonical_from_any(str(raw))
            # Early gating for better UX/logging (mirrors button/coordinator checks)
            if not coordinator.can_request_location(canonical_id):
                _LOGGER.warning(
                    "Locate request for %s (%s) ignored: not allowed right now (in-flight, cooldown, push not ready, or polling).",
                    friendly,
                    canonical_id,
                )
                return
            _LOGGER.info("Locate request for %s (%s)", friendly, canonical_id)
            await coordinator.async_locate_device(canonical_id)
        except ValueError as err:
            _LOGGER.error("Failed to locate device: %s", err)
        except Exception as err:  # Catch potential API errors from coordinator
            _LOGGER.error("Failed to locate device '%s': %s", raw, err)

    async def async_play_sound_service(call: ServiceCall) -> None:
        """Handle play sound service call."""
        raw = call.data["device_id"]
        try:
            canonical_id, friendly = _resolve_canonical_from_any(str(raw))
            _LOGGER.info("Play Sound request for %s (%s)", friendly, canonical_id)
            ok = await coordinator.async_play_sound(canonical_id)
            if not ok:
                _LOGGER.warning(
                    "Failed to play sound on %s (request may have been rejected by API)", friendly
                )
        except ValueError as err:
            _LOGGER.error("Failed to play sound: %s", err)
        except Exception as err:
            _LOGGER.error("Failed to play sound on '%s': %s", raw, err)

    async def async_stop_sound_service(call: ServiceCall) -> None:
        """Handle stop sound service call."""
        raw = call.data["device_id"]
        try:
            canonical_id, friendly = _resolve_canonical_from_any(str(raw))
            _LOGGER.info("Stop Sound request for %s (%s)", friendly, canonical_id)
            ok = await coordinator.async_stop_sound(canonical_id)
            if not ok:
                _LOGGER.warning(
                    "Failed to stop sound on %s (request may have been rejected by API)", friendly
                )
        except ValueError as err:
            _LOGGER.error("Failed to stop sound: %s", err)
        except Exception as err:
            _LOGGER.error("Failed to stop sound on '%s': %s", raw, err)

    async def async_locate_external_service(call: ServiceCall) -> None:
        """External locate device service (delegates to locate)."""
        raw = call.data["device_id"]
        provided_name = call.data.get("device_name")
        try:
            canonical_id, friendly = _resolve_canonical_from_any(str(raw))
            device_name = provided_name or friendly or canonical_id
            # Early gating for better UX/logging (mirrors button/coordinator checks)
            if not coordinator.can_request_location(canonical_id):
                _LOGGER.warning(
                    "External locate request for %s (%s) ignored: not allowed right now (in-flight, cooldown, push not ready, or polling).",
                    device_name,
                    canonical_id,
                )
                return
            _LOGGER.info(
                "External location request for %s (%s) - delegating to normal locate",
                device_name,
                canonical_id,
            )
            await coordinator.async_locate_device(canonical_id)
        except ValueError as err:
            _LOGGER.error("Failed to execute external locate: %s", err)
        except Exception as err:
            _LOGGER.error("Failed to execute external locate for '%s': %s", raw, err)

    async def async_refresh_device_urls_service(call: ServiceCall) -> None:
        """Refresh configuration URLs for integration devices (absolute URL)."""
        try:
            base_url = get_url(
                hass, prefer_external=True, allow_cloud=True, allow_external=True, allow_internal=True
            )
        except HomeAssistantError as err:
            _LOGGER.error("Could not determine base URL for device refresh: %s", err)
            return

        # Token mode: options-first
        ha_uuid = str(hass.data.get("core.uuid", "ha"))
        config_entries = hass.config_entries.async_entries(DOMAIN)
        token_expiration_enabled = DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
        if config_entries:
            e0 = config_entries[0]
            token_expiration_enabled = _opt(
                e0, OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            )

        if token_expiration_enabled:
            week = str(int(time.time() // 604800))  # weekly rotation bucket
            auth_token = hashlib.md5(f"{ha_uuid}:{week}".encode()).hexdigest()[:16]
        else:
            auth_token = hashlib.md5(f"{ha_uuid}:static".encode()).hexdigest()[:16]

        dev_reg = dr.async_get(hass)
        updated_count = 0
        for device in dev_reg.devices.values():
            if any(identifier[0] == DOMAIN for identifier in device.identifiers):
                dev_id = next((ident for domain, ident in device.identifiers if domain == DOMAIN), None)
                if dev_id:
                    new_config_url = f"{base_url}/api/googlefindmy/map/{dev_id}?token={auth_token}"
                    dev_reg.async_update_device(device_id=device.id, configuration_url=new_config_url)
                    updated_count += 1
                    _LOGGER.debug(
                        "Updated URL for device %s: %s",
                        device.name_by_user or device.name,
                        _redact_url_token(new_config_url),
                    )

        _LOGGER.info("Refreshed URLs for %d Google Find My devices", updated_count)

    async def async_rebuild_registry_service(call: ServiceCall) -> None:
        """Migrate soft settings or rebuild the registry (optionally scoped to device_ids).

        Logic:
            1. Determine target devices (all or a subset).
            2. Remove all entities associated with these devices.
            3. Remove orphaned devices (those with no entities left).
            4. Reload the config entries associated with the affected devices.
        """
        mode: str = str(call.data.get(ATTR_MODE, MODE_REBUILD)).lower()
        raw_ids = call.data.get(ATTR_DEVICE_IDS)

        if isinstance(raw_ids, str):
            target_device_ids = {raw_ids}
        elif isinstance(raw_ids, (list, tuple, set)):
            target_device_ids = {str(x) for x in raw_ids}
        else:
            target_device_ids = set()

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
        entries = hass.config_entries.async_entries(DOMAIN)

        _LOGGER.info(
            "googlefindmy.rebuild_registry requested: mode=%s, device_ids=%s",
            mode,
            "none" if not raw_ids else (raw_ids if isinstance(raw_ids, str) else f"{len(target_device_ids)} ids"),
        )

        if mode == MODE_MIGRATE:
            for entry in entries:
                try:
                    await _async_soft_migrate_data_to_options(hass, entry)
                except Exception as err:
                    _LOGGER.error("Soft-migrate failed for entry %s: %s", entry.entry_id, err)
            _LOGGER.info(
                "googlefindmy.rebuild_registry: soft-migrate completed for %d config entrie(s).",
                len(entries),
            )
            return

        if mode != MODE_REBUILD:
            _LOGGER.error(
                "Unsupported mode '%s' for rebuild_registry; use one of: %s",
                mode,
                ", ".join(REBUILD_REGISTRY_MODES),
            )
            return

        affected_entry_ids: set[str] = set()
        if target_device_ids:
            candidate_devices = set()
            for d in target_device_ids:
                dev = dev_reg.async_get(d)
                if dev is not None:
                    candidate_devices.add(dev.id)
                    affected_entry_ids.update(dev.config_entries)
        else:
            candidate_devices = set()
            for dev in dev_reg.devices.values():
                if any(domain == DOMAIN for domain, _ in dev.identifiers):
                    candidate_devices.add(dev.id)
                    affected_entry_ids.update(dev.config_entries)

        if not candidate_devices:
            _LOGGER.info("googlefindmy.rebuild_registry: no matching devices to rebuild.")
            return

        removed_entities = 0
        removed_devices = 0

        for ent in list(ent_reg.entities.values()):
            if ent.platform == DOMAIN and ent.device_id in candidate_devices:
                try:
                    ent_reg.async_remove(ent.entity_id)
                    removed_entities += 1
                except Exception as err:
                    _LOGGER.error("Failed to remove entity %s: %s", ent.entity_id, err)

        for dev_id in list(candidate_devices):
            dev = dev_reg.async_get(dev_id)
            if dev is None:
                continue
            has_entities = any(e.device_id == dev_id for e in ent_reg.entities.values())
            if not has_entities:
                try:
                    dev_reg.async_remove_device(dev_id)
                    removed_devices += 1
                except Exception as err:
                    _LOGGER.error("Failed to remove device %s: %s", dev_id, err)

        to_reload = [e for e in entries if e.entry_id in affected_entry_ids] or list(entries)
        for entry in to_reload:
            try:
                await hass.config_entries.async_reload(entry.entry_id)
            except Exception as err:
                _LOGGER.error("Reload failed for entry %s: %s", entry.entry_id, err)

        _LOGGER.info(
            "googlefindmy.rebuild_registry: rebuild finished: removed %d entit(y/ies), %d device(s), entries reloaded=%d",
            removed_entities,
            removed_devices,
            len(to_reload),
        )

    # Register all services for the integration.
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOCATE_DEVICE,
        async_locate_device_service,
        schema=vol.Schema({vol.Required("device_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY_SOUND,
        async_play_sound_service,
        schema=vol.Schema({vol.Required("device_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_STOP_SOUND,
        async_stop_sound_service,
        schema=vol.Schema({vol.Required("device_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOCATE_EXTERNAL,
        async_locate_external_service,
        schema=vol.Schema({vol.Required("device_id"): cv.string, vol.Optional("device_name"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_DEVICE_URLS,
        async_refresh_device_urls_service,
        schema=vol.Schema({}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD_REGISTRY,
        async_rebuild_registry_service,
        schema=vol.Schema(
            {
                vol.Optional(ATTR_MODE, default=MODE_REBUILD): vol.In(REBUILD_REGISTRY_MODES),
                vol.Optional(ATTR_DEVICE_IDS): vol.Any(cv.string, [cv.string]),
            }
        ),
    )

    domain_bucket = hass.data.setdefault(DOMAIN, {})
    domain_bucket["services_registered"] = True


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
    - Never allow removing the integration's own "service" device (ident = 'integration').
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
    if dev_id == "integration":
        return False

    # Purge coordinator caches (best effort; does not trigger polling)
    try:
        coordinator: GoogleFindMyCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator is not None:
            coordinator.purge_device(dev_id)
    except Exception as err:
        _LOGGER.debug("Coordinator purge failed for %s: %s", dev_id, err)

    # Persist user's delete decision: add to ignored_devices mapping (idempotent, lossless)
    try:
        opts = dict(entry.options)
        # Coerce legacy shapes (list / dict[str,str]) to v2 mapping
        current_raw = opts.get(OPT_IGNORED_DEVICES, DEFAULT_OPTIONS.get(OPT_IGNORED_DEVICES))
        ignored_map, migrated = coerce_ignored_mapping(current_raw)

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
            _LOGGER.info("Marked device '%s' (%s) as ignored for entry '%s'", name_to_store, dev_id, entry.title)
    except Exception as err:
        _LOGGER.debug("Persisting delete decision failed for %s: %s", dev_id, err)

    # Allow HA to delete the device (and its entities) if no other entry still references it
    return True
