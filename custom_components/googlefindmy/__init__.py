"""Google Find My Device integration for Home Assistant.

Version: 2.2 - Finalized architecture with full encapsulation, public APIs, and robust error handling. 
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import socket

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.network import get_url

from .Auth.token_cache import async_load_cache_from_file
from .Auth.username_provider import username_string
from .const import (
    CONF_OAUTH_TOKEN,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DOMAIN,
    SERVICE_LOCATE_DEVICE,
    SERVICE_LOCATE_EXTERNAL,
    SERVICE_PLAY_SOUND,
    SERVICE_REFRESH_URLS,
)
from .coordinator import GoogleFindMyCoordinator
from .map_view import GoogleFindMyMapRedirectView, GoogleFindMyMapView

_LOGGER = logging.getLogger(__name__)

# Platforms provided by this integration
PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]

# Settings that belong to entry.options (never secrets). Single source of truth.
_OPTION_KEYS: tuple[str, ...] = (
    "tracked_devices",
    "location_poll_interval",
    "device_poll_delay",
    "min_poll_interval",
    "min_accuracy_threshold",
    "movement_threshold",
    "allow_history_fallback",
    "google_home_filter_enabled",
    "google_home_filter_keywords",
    "enable_stats_entities",
    "map_view_token_expiration",
)


def _redact_url_token(url: str) -> str:
    """Return URL with any 'token' query parameter value redacted for safe logging."""
    try:
        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        redacted = []
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
        # This is a last resort to prevent logging functions from crashing.
        return url


async def _async_save_secrets_data(secrets_data: dict) -> None:
    """Persist secrets to the integration's async token cache.

    Note:
        Only called for the secrets.json path. Complex values are serialized to JSON strings.
    """
    from .Auth.token_cache import async_set_cached_value

    enhanced_data = secrets_data.copy()

    # Normalize username key across old/new secrets variants
    google_email = secrets_data.get("username", secrets_data.get("Email"))
    if google_email:
        enhanced_data[username_string] = google_email

    for key, value in enhanced_data.items():
        try:
            if isinstance(value, (str, int, float)):
                await async_set_cached_value(key, str(value))
            else:
                await async_set_cached_value(key, json.dumps(value))
        except (OSError, TypeError) as err:
            _LOGGER.warning("Failed to save '%s' to persistent cache: %s", key, err)


async def _async_save_individual_credentials(oauth_token: str, google_email: str) -> None:
    """Persist individual credentials (oauth_token + email) to the token cache."""
    from .Auth.token_cache import async_set_cached_value

    try:
        await async_set_cached_value("oauth_token", oauth_token)
        await async_set_cached_value(username_string, google_email)
    except OSError as err:
        _LOGGER.warning("Failed to save individual credentials to cache: %s", err)


def _opt(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a configuration value, preferring options over data.

    This helper provides a backward-compatible way to access settings,
    ensuring that user-configured options always take precedence over
    the initial setup data.

    Args:
        entry: The config entry to read from.
        key: The configuration key to look up.
        default: The default value to return if the key is not found.

    Returns:
        The configuration value.
    """
    if key in entry.options:
        return entry.options.get(key, default)
    return entry.data.get(key, default)


def _effective_config(entry: ConfigEntry) -> dict[str, Any]:
    """Assemble a dict of non-secret runtime settings (options-first).

    Args:
        entry: The config entry to build the configuration from.

    Returns:
        A dictionary containing the merged configuration.
    """
    return {k: _opt(entry, k, None) for k in _OPTION_KEYS}


async def _async_soft_migrate_data_to_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Idempotently copy known settings from data -> options (never move secrets).

    Rationale:
        Older versions stored user-tweakable settings in entry.data. Modern HA expects
        mutable settings in entry.options. This preserves compatibility without
        breaking existing user setups by migrating them on the fly.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry to migrate.
    """
    new_options = dict(entry.options)
    changed = False
    for k in _OPTION_KEYS:
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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry (entities-first, options-first)."""

    # Load persisted token cache (best-effort)
    try:
        await async_load_cache_from_file()
        _LOGGER.debug("Token cache preloaded successfully")
    except OSError as err:
        _LOGGER.warning("Failed to preload token cache: %s", err)

    # Soft-migrate mutable settings from data -> options (never secrets)
    await _async_soft_migrate_data_to_options(hass, entry)

    # --- Credentials handling (secrets-only in data) ---
    secrets_data = entry.data.get("secrets_data")
    oauth_token = entry.data.get(CONF_OAUTH_TOKEN)
    google_email = entry.data.get("google_email")

    if secrets_data:
        await _async_save_secrets_data(secrets_data)
        _LOGGER.debug("Persisted secrets.json bundle to token cache")
    elif oauth_token and google_email:
        await _async_save_individual_credentials(oauth_token, google_email)
        _LOGGER.debug("Persisted individual credentials to token cache")
    else:
        _LOGGER.error("No credentials found in config entry (neither secrets_data nor oauth_token+google_email)")
        raise ConfigEntryNotReady("Credentials missing")

    # --- Build effective runtime settings (options-first) ---
    coordinator = GoogleFindMyCoordinator(
        hass,
        secrets_data=secrets_data,  # may be None with individual-credentials path; token cache is prepped
        tracked_devices=_opt(entry, "tracked_devices", []),
        location_poll_interval=_opt(entry, "location_poll_interval", 300),
        device_poll_delay=_opt(entry, "device_poll_delay", 5),
        min_poll_interval=_opt(entry, "min_poll_interval", 120),
        min_accuracy_threshold=_opt(entry, "min_accuracy_threshold", 100),
        allow_history_fallback=_opt(entry, "allow_history_fallback", False),
    )
    coordinator.config_entry = entry  # convenience for platforms

    # Optional: attach Google Home filter (options-first configuration)
    from .google_home_filter import GoogleHomeFilter

    coordinator.google_home_filter = GoogleHomeFilter(hass, _effective_config(entry))
    _LOGGER.debug("Initialized Google Home filter (options-first)")

    # Share coordinator in hass.data (for platforms & restore path)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Register map views early
    hass.http.register_view(GoogleFindMyMapView(hass))
    hass.http.register_view(GoogleFindMyMapRedirectView(hass))
    _LOGGER.debug("Registered map views")

    # Register services (available regardless of data freshness)
    await _async_register_services(hass, coordinator)

    # ----- ENTITIES-FIRST: forward platforms now so RestoreEntity can populate immediately -----
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Defer the first refresh until HA is fully started
    listener_active = False

    async def _do_first_refresh(_: Any) -> None:
        """Perform the initial coordinator refresh after HA has started."""
        nonlocal listener_active
        listener_active = False
        try:
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                _LOGGER.warning("Initial refresh failed; entities will recover on subsequent polls.")
        except Exception as err:
            _LOGGER.error("Initial refresh raised an unexpected error: %s", err, exc_info=True)

    if hass.state == CoreState.running:
        hass.async_create_task(_do_first_refresh(None))
    else:
        unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _do_first_refresh)
        listener_active = True

        def _safe_unsub() -> None:
            if listener_active:
                unsub()

        entry.async_on_unload(_safe_unsub)

    # React to entry updates (options) and apply changes
    entry.async_on_unload(entry.add_update_listener(async_update_entry))
    return True


async def async_update_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates. Push new options into the coordinator and refresh."""
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Apply updated settings via public API (no private attribute access)
    coordinator.update_settings(
        tracked_devices=_opt(entry, "tracked_devices", []),
        location_poll_interval=_opt(entry, "location_poll_interval", 300),
        device_poll_delay=_opt(entry, "device_poll_delay", 5),
        min_poll_interval=_opt(entry, "min_poll_interval", 120),
        min_accuracy_threshold=_opt(entry, "min_accuracy_threshold", 100),
        allow_history_fallback=_opt(entry, "allow_history_fallback", False),
    )

    # Update Google Home filter configuration with merged options-over-data view
    if hasattr(coordinator, "google_home_filter"):
        coordinator.google_home_filter.update_config(_effective_config(entry))

    # Nudge scheduler: make next poll due immediately (no private access)
    coordinator.force_poll_due()

    _LOGGER.info(
        "Updated configuration: %d tracked device(s), poll=%ss, delay=%ss",
        len(coordinator.tracked_devices),
        coordinator.location_poll_interval,
        coordinator.device_poll_delay,
    )

    await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


def _get_local_ip_sync() -> str:
    """Best-effort local IP discovery via UDP connect (executor-only)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


async def _async_register_services(hass: HomeAssistant, coordinator: GoogleFindMyCoordinator) -> None:
    """Register services for the integration."""

    def _resolve_canonical_from_any(arg: str) -> tuple[str, str]:
        """Resolve any device identifier to (canonical_id, friendly_name).

        Tries to resolve, in order:
        1. A Home Assistant device_id.
        2. A Home Assistant entity_id.
        3. A raw canonical ID from this integration.

        Args:
            arg: The identifier to resolve.

        Returns:
            A tuple of (canonical_id, friendly_name).

        Raises:
            ValueError: If the identifier cannot be resolved to a device
                        known by this integration.
        """
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
        try:
            name = coordinator.get_device_display_name(arg) or arg
            # Verify this is a known device to prevent arbitrary calls
            if name != arg:
                return arg, name
        except (AttributeError, TypeError):
            # Coordinator or method not ready, or invalid arg type
            pass

        raise ValueError(f"Identifier '{arg}' could not be resolved to a known device")

    async def async_locate_device_service(call: ServiceCall) -> None:
        """Handle locate device service call."""
        raw = call.data["device_id"]
        try:
            canonical_id, friendly = _resolve_canonical_from_any(str(raw))
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
                _LOGGER.warning("Failed to play sound on %s (request may have been rejected by API)", friendly)
        except ValueError as err:
            _LOGGER.error("Failed to play sound: %s", err)
        except Exception as err:
            _LOGGER.error("Failed to play sound on '%s': %s", raw, err)

    async def async_locate_external_service(call: ServiceCall) -> None:
        """External locate device service (delegates to locate)."""
        raw = call.data["device_id"]
        provided_name = call.data.get("device_name")
        try:
            canonical_id, friendly = _resolve_canonical_from_any(str(raw))
            device_name = provided_name or friendly or canonical_id
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
            token_expiration_enabled = _opt(e0, "map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION)

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

    # Register services
    hass.services.async_register(
        DOMAIN, SERVICE_LOCATE_DEVICE, async_locate_device_service, schema=vol.Schema({vol.Required("device_id"): cv.string})
    )
    hass.services.async_register(
        DOMAIN, SERVICE_PLAY_SOUND, async_play_sound_service, schema=vol.Schema({vol.Required("device_id"): cv.string})
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOCATE_EXTERNAL,
        async_locate_external_service,
        schema=vol.Schema({vol.Required("device_id"): cv.string, vol.Optional("device_name"): cv.string}),
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_URLS, async_refresh_device_urls_service, schema=vol.Schema({}))
