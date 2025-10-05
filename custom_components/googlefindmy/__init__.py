"""Google Find My Device integration for Home Assistant.
Version: 2.2 - Encapsulation & options-first; remove legacy hass.data config_data
"""
from __future__ import annotations

import logging
import time
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import CoreState, HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .Auth.token_cache import async_load_cache_from_file
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
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    except Exception:
        # Last resort: return original (do not intentionally log secrets elsewhere).
        return url


async def _async_save_secrets_data(secrets_data: dict) -> None:
    """Persist secrets to the integration's async token cache.

    Note: Only called for the secrets.json path. Complex values are serialized to JSON strings.
    """
    from .Auth.token_cache import async_set_cached_value
    from .Auth.username_provider import username_string
    import json

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
        except Exception as err:
            _LOGGER.warning("Failed to save %s to persistent cache: %s", key, err)


async def _async_save_individual_credentials(oauth_token: str, google_email: str) -> None:
    """Persist individual credentials (oauth_token + email) to the token cache."""
    from .Auth.token_cache import async_set_cached_value
    from .Auth.username_provider import username_string

    try:
        await async_set_cached_value("oauth_token", oauth_token)
        await async_set_cached_value(username_string, google_email)
    except Exception as err:
        _LOGGER.warning("Failed to save individual credentials to cache: %s", err)


def _opt(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Options-first read with data fallback for backward compatibility."""
    if key in entry.options:
        return entry.options.get(key, default)
    return entry.data.get(key, default)


def _effective_config(entry: ConfigEntry) -> dict[str, Any]:
    """Assemble a dict of non-secret runtime settings (options-first)."""
    return {k: _opt(entry, k, None) for k in _OPTION_KEYS}


async def _async_soft_migrate_data_to_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Idempotently copy known settings from data -> options (never move secrets).

    Rationale:
    Older versions stored user-tweakable settings in entry.data. Modern HA expects
    mutable settings in entry.options. This preserves compatibility without breaking existing setups.
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
    except Exception as err:
        _LOGGER.warning("Failed to preload token cache: %s", err)

    # Soft-migrate mutable settings from data -> options (never secrets)
    await _async_soft_migrate_data_to_options(hass, entry)

    # --- Credentials handling (secrets-only in data) ---
    secrets_data = entry.data.get("secrets_data")
    oauth_token = entry.data.get(CONF_OAUTH_TOKEN)
    google_email = entry.data.get("google_email")

    if secrets_data:
        try:
            await _async_save_secrets_data(secrets_data)
            _LOGGER.debug("Persisted secrets.json bundle to token cache")
        except Exception as err:
            _LOGGER.warning("Failed to persist secrets.json bundle: %s", err)
    elif oauth_token and google_email:
        try:
            await _async_save_individual_credentials(oauth_token, google_email)
            _LOGGER.debug("Persisted individual credentials to token cache")
        except Exception as err:
            _LOGGER.warning("Failed to persist individual credentials: %s", err)
    else:
        _LOGGER.error("No credentials found in config entry (neither secrets_data nor oauth_token+google_email)")
        raise ConfigEntryNotReady("Credentials missing")

    # --- Build effective runtime settings (options-first) ---
    tracked_devices = _opt(entry, "tracked_devices", [])
    location_poll_interval = _opt(entry, "location_poll_interval", 300)
    device_poll_delay = _opt(entry, "device_poll_delay", 5)
    min_poll_interval = _opt(entry, "min_poll_interval", 120)
    min_accuracy_threshold = _opt(entry, "min_accuracy_threshold", 100)
    movement_threshold = _opt(entry, "movement_threshold", 50)
    allow_history_fallback = _opt(entry, "allow_history_fallback", False)

    # Initialize coordinator (first refresh deferred until HA is started)
    coordinator = GoogleFindMyCoordinator(
        hass,
        secrets_data=secrets_data,  # may be None with individual-credentials path; token cache is prepped
        tracked_devices=tracked_devices,
        location_poll_interval=location_poll_interval,
        device_poll_delay=device_poll_delay,
        min_poll_interval=min_poll_interval,
        min_accuracy_threshold=min_accuracy_threshold,
        allow_history_fallback=allow_history_fallback,
    )
    coordinator.config_entry = entry  # convenience for platforms

    # Optional: attach Google Home filter (options-first configuration)
    from .google_home_filter import GoogleHomeFilter

    coordinator.google_home_filter = GoogleHomeFilter(hass, _effective_config(entry))
    _LOGGER.debug("Initialized Google Home filter (options-first)")

    # Share coordinator in hass.data (platforms & restore path)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    # Register map views early (absolute URL will be built by the service later)
    try:
        hass.http.register_view(GoogleFindMyMapView(hass))
        hass.http.register_view(GoogleFindMyMapRedirectView(hass))
        _LOGGER.debug("Registered map views")
    except Exception as err:
        _LOGGER.warning("Failed to register map views: %s", err)

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
            _LOGGER.error("Initial refresh raised an unexpected error: %s", err)

    if hass.state == CoreState.running:
        try:
            hass.async_create_task(_do_first_refresh(None))
        except Exception as err:
            _LOGGER.error("Failed to schedule initial refresh task: %s", err)
    else:
        try:
            unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _do_first_refresh)
            listener_active = True
        except Exception as err:
            _LOGGER.error("Failed to register initial refresh listener: %s", err)
        else:

            def _safe_unsub() -> None:
                if listener_active:
                    try:
                        unsub()
                    except Exception:
                        # Listener already removed or never registered; ignore
                        pass

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
    import socket

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


async def _async_register_services(
    hass: HomeAssistant, coordinator: GoogleFindMyCoordinator
) -> None:
    """Register services for the integration."""

    async def async_locate_device_service(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        try:
            await coordinator.async_locate_device(device_id)
        except Exception as err:
            _LOGGER.error("Failed to locate device %s: %s", device_id, err)

    async def async_play_sound_service(call: ServiceCall) -> None:
        """Handle play sound service call."""
        device_id = call.data["device_id"]
        try:
            await coordinator.async_play_sound(device_id)
        except Exception as err:
            _LOGGER.error("Failed to play sound on device %s: %s", device_id, err)

    async def async_locate_external_service(call: ServiceCall) -> None:
        """External locate device service (delegates to locate)."""
        device_id = call.data.get("device_id")
        device_name = call.data.get("device_name", device_id)
        _LOGGER.info(
            "External location request for device: %s (%s) - delegating to normal locate",
            device_name,
            device_id,
        )
        await async_locate_device_service(call)

    async def async_refresh_device_urls_service(call: ServiceCall) -> None:
        """Refresh configuration URLs for integration devices (absolute URL).

        The device registry expects an absolute HTTP(S) URL. We therefore determine a base URL once
        using get_url(... prefer_external=True, allow_cloud=True, allow_external=True, allow_internal=True)
        and then update all integration devices. Logs redact the token for safety.
        """
        try:
            import hashlib

            from homeassistant.helpers import device_registry
            from homeassistant.helpers.network import get_url

            base_url = get_url(
                hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
            if not base_url:
                _LOGGER.error("Could not determine base URL for device refresh")
                return

            # Token mode: options-first (consistent with platforms / map_view)
            ha_uuid = str(hass.data.get("core.uuid", "ha"))
            config_entries = hass.config_entries.async_entries(DOMAIN)
            token_expiration_enabled = DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            if config_entries:
                e0 = config_entries[0]
                token_expiration_enabled = e0.options.get(
                    "map_view_token_expiration",
                    e0.data.get("map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION),
                )

            if token_expiration_enabled:
                week = str(int(time.time() // 604800))  # weekly rotation bucket
                auth_token = hashlib.md5(f"{ha_uuid}:{week}".encode()).hexdigest()[:16]
            else:
                auth_token = hashlib.md5(f"{ha_uuid}:static".encode()).hexdigest()[:16]

            dev_reg = device_registry.async_get(hass)
            updated_count = 0
            for device in dev_reg.devices.values():
                if any(identifier[0] == DOMAIN for identifier in device.identifiers):
                    dev_id = None
                    for identifier in device.identifiers:
                        if identifier[0] == DOMAIN:
                            dev_id = identifier[1]
                            break
                    if dev_id:
                        new_config_url = f"{base_url}/api/googlefindmy/map/{dev_id}?token={auth_token}"
                        dev_reg.async_update_device(
                            device_id=device.id,
                            configuration_url=new_config_url,
                        )
                        updated_count += 1
                        _LOGGER.info(
                            "Updated URL for device %s: %s",
                            device.name_by_user or device.name,
                            _redact_url_token(new_config_url),
                        )

            _LOGGER.info("Refreshed URLs for %d Google Find My devices", updated_count)
        except Exception as err:
            _LOGGER.error("Failed to refresh device URLs: %s", err)

    # Register services
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
        SERVICE_LOCATE_EXTERNAL,
        async_locate_external_service,
        schema=vol.Schema({vol.Required("device_id"): cv.string, vol.Optional("device_name"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_URLS,
        async_refresh_device_urls_service,
        schema=vol.Schema({}),
    )
