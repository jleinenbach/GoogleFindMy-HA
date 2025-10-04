"""Google Find My Device integration for Home Assistant.

Version: 2.0 - Location extraction from device list
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.start import async_when_started
import voluptuous as vol

from .const import (
    DOMAIN,
    SERVICE_LOCATE_DEVICE,
    SERVICE_PLAY_SOUND,
    SERVICE_LOCATE_EXTERNAL,
    SERVICE_REFRESH_URLS,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
)
from .coordinator import GoogleFindMyCoordinator
from .Auth.token_cache import async_load_cache_from_file
from .map_view import GoogleFindMyMapView, GoogleFindMyMapRedirectView

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


def _get_local_ip_sync() -> str:
    """Best-effort local IP detection (blocking, run in executor)."""
    import socket

    try:
        # UDP connect does not send packets; it just sets routing and local address
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return ""


def _redact_url_token(url: str) -> str:
    """Redact 'token' query param for logging."""
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        if "token" in query:
            query["token"] = "****"
            redacted = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
            return redacted
    except Exception:
        pass
    return url


async def _async_save_secrets_data(secrets_data: dict) -> None:
    """Persist complete secrets data to the integration cache (async, non-blocking)."""
    from .Auth.token_cache import async_set_cached_value
    from .Auth.username_provider import username_string

    enhanced_data = secrets_data.copy()

    # Derive and persist the username in a normalized way
    google_email = secrets_data.get("username", secrets_data.get("Email"))
    if google_email:
        enhanced_data[username_string] = google_email

    # Store all keys; complex values are serialized to JSON
    for key, value in enhanced_data.items():
        try:
            if isinstance(value, (str, int, float)):
                await async_set_cached_value(key, str(value))
            else:
                await async_set_cached_value(key, json.dumps(value))
        except Exception as err:
            _LOGGER.warning("Failed to save %s to persistent cache: %s", key, err)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Google Find My Device from a config entry."""
    # Preload cache early to reduce I/O during first refresh
    try:
        await async_load_cache_from_file()
        _LOGGER.debug("Cache preloaded successfully")
    except Exception as err:
        _LOGGER.warning("Failed to preload cache: %s", err)

    # Extract configuration (secrets.json is the source)
    tracked_devices = entry.data.get("tracked_devices", [])
    location_poll_interval = entry.data.get("location_poll_interval", 300)
    device_poll_delay = entry.data.get("device_poll_delay", 5)
    min_poll_interval = entry.data.get("min_poll_interval", 120)
    min_accuracy_threshold = entry.data.get("min_accuracy_threshold", 100)
    movement_threshold = entry.data.get("movement_threshold", 50)
    allow_history_fallback = entry.data.get("allow_history_fallback", False)

    # Obtain secrets bundle
    secrets_data = entry.data.get("secrets_data")
    if not secrets_data:
        _LOGGER.error("Secrets data not found in config entry")
        raise ConfigEntryNotReady("Secrets data not found")

    # Initialize coordinator (non-blocking; first refresh will be deferred)
    coordinator = GoogleFindMyCoordinator(
        hass,
        secrets_data=secrets_data,
        tracked_devices=tracked_devices,
        location_poll_interval=location_poll_interval,
        device_poll_delay=device_poll_delay,
        min_poll_interval=min_poll_interval,
        min_accuracy_threshold=min_accuracy_threshold,
        allow_history_fallback=allow_history_fallback,
    )

    # Optional: attach Google Home filter (kept as-is)
    from .google_home_filter import GoogleHomeFilter

    coordinator.google_home_filter = GoogleHomeFilter(hass, entry.data)
    _LOGGER.debug("Initialized Google Home filter")

    # Persist secrets asynchronously (non-blocking)
    if secrets_data:
        try:
            await _async_save_secrets_data(secrets_data)
            _LOGGER.debug("Saved complete secrets data to persistent cache")
        except Exception as err:
            _LOGGER.warning("Failed to save secrets data to persistent cache: %s", err)

    # Make coordinator available in hass.data immediately
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    # Also share config data for device tracker
    hass.data[DOMAIN]["config_data"] = {
        "min_accuracy_threshold": min_accuracy_threshold,
        "movement_threshold": movement_threshold,
    }

    # Register map views early; safe to do before first refresh
    try:
        map_view = GoogleFindMyMapView(hass)
        hass.http.register_view(map_view)
        _LOGGER.debug("Registered map view")

        redirect_view = GoogleFindMyMapRedirectView(hass)
        hass.http.register_view(redirect_view)
        _LOGGER.debug("Registered map redirect view")
    except Exception as err:
        _LOGGER.warning("Failed to register map views: %s", err)

    # Register services (available regardless of initial data availability)
    await _async_register_services(hass, coordinator)

    # Defer the first refresh until HA is fully started to reduce startup pressure.
    # IMPORTANT: Do NOT call async_config_entry_first_refresh() when the entry is already LOADED.
    # Use async_refresh() instead and check last_update_success. Then forward platforms.
    async def _do_first_refresh() -> None:
        """Perform the initial coordinator refresh and then set up platforms."""
        try:
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                _LOGGER.warning(
                    "Initial refresh did not succeed; platforms will still be set up. "
                    "Entities may start without data and recover on subsequent polls."
                )
        except Exception as err:
            _LOGGER.error(
                "Initial refresh raised an unexpected error; setting up platforms anyway: %s",
                err,
            )
        # Forward platform setups after attempting the first refresh
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Use HA helper to schedule the task when HA is started, or immediately if already running.
    # This avoids manual subscribe/unsubscribe race conditions with one-time listeners.
    async_when_started(hass, _do_first_refresh)

    # React to entry updates (options) and apply changes
    entry.async_on_unload(entry.add_update_listener(async_update_entry))

    return True


async def async_update_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates."""
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Update coordinator knobs
    coordinator.tracked_devices = entry.data.get("tracked_devices", [])
    coordinator.location_poll_interval = entry.data.get("location_poll_interval", 300)
    coordinator.device_poll_delay = entry.data.get("device_poll_delay", 5)
    coordinator.min_poll_interval = entry.data.get("min_poll_interval", 120)
    coordinator._min_accuracy_threshold = entry.data.get("min_accuracy_threshold", 100)
    coordinator.allow_history_fallback = entry.data.get("allow_history_fallback", False)

    # Update Google Home filter configuration
    if hasattr(coordinator, "google_home_filter"):
        coordinator.google_home_filter.update_config(entry.data)

    # Share updated config for device tracker
    hass.data[DOMAIN]["config_data"] = {
        "min_accuracy_threshold": entry.data.get("min_accuracy_threshold", 100),
        "movement_threshold": entry.data.get("movement_threshold", 50),
    }

    # Reset monotonic baseline so the next cycle is due immediately
    try:
        effective_interval = max(
            coordinator.location_poll_interval, coordinator.min_poll_interval
        )
    except Exception:
        effective_interval = coordinator.location_poll_interval
    coordinator._last_poll_mono = time.monotonic() - float(effective_interval)

    _LOGGER.info(
        "Updated configuration: %d tracked devices, %ss poll interval",
        len(coordinator.tracked_devices),
        coordinator.location_poll_interval,
    )

    # Request an immediate refresh (non-blocking); do not call async_config_entry_first_refresh() here
    await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def _async_register_services(
    hass: HomeAssistant, coordinator: GoogleFindMyCoordinator
) -> None:
    """Register services for the integration."""

    async def async_locate_device_service(call: ServiceCall) -> None:
        """Handle locate device service call."""
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
        """Handle external locate device service call (delegates to locate)."""
        device_id = call.data.get("device_id")
        device_name = call.data.get("device_name", device_id)
        _LOGGER.info(
            "External location request for device: %s (%s) - delegating to normal locate",
            device_name,
            device_id,
        )
        await async_locate_device_service(call)

    async def async_refresh_device_urls_service(call: ServiceCall) -> None:
        """Handle refresh device URLs service call."""
        try:
            from homeassistant.helpers import device_registry
            from homeassistant.helpers.network import get_url
            import hashlib

            # Determine a base URL (prefer local) – local IP detection runs in executor
            local_ip = await hass.async_add_executor_job(_get_local_ip_sync)
            if local_ip:
                port = 8123
                use_ssl = False
                if hasattr(hass, "http") and hasattr(hass.http, "server_port"):
                    port = hass.http.server_port or 8123
                    use_ssl = hasattr(hass.http, "ssl_context") and (
                        hass.http.ssl_context is not None
                    )
                protocol = "https" if use_ssl else "http"
                base_url = f"{protocol}://{local_ip}:{port}"
                _LOGGER.info("Detected local URL for device refresh: %s", base_url)
            else:
                # Fallback to HA's internal URL resolution
                base_url = get_url(
                    hass,
                    prefer_external=False,
                    allow_cloud=False,
                    allow_external=False,
                    allow_internal=True,
                )
                _LOGGER.info("Using HA internal URL for device refresh: %s", base_url)

            if not base_url:
                _LOGGER.error("Could not determine base URL for device refresh")
                return

            # Auth token (with optional weekly rotation)
            ha_uuid = str(hass.data.get("core.uuid", "ha"))
            config_entries = hass.config_entries.async_entries(DOMAIN)
            token_expiration_enabled = DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            if config_entries:
                token_expiration_enabled = config_entries[0].data.get(
                    "map_view_token_expiration", DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
                )

            if token_expiration_enabled:
                week = str(int(time.time() // 604800))  # week-bucket
                auth_token = hashlib.md5(f"{ha_uuid}:{week}".encode()).hexdigest()[:16]
            else:
                auth_token = hashlib.md5(f"{ha_uuid}:static".encode()).hexdigest()[:16]

            # Update device configuration URLs in the device registry
            dev_reg = device_registry.async_get(hass)
            updated_count = 0
            for device in dev_reg.devices.values():
                if any(identifier[0] == DOMAIN for identifier in device.identifiers):
                    device_id = None
                    for identifier in device.identifiers:
                        if identifier[0] == DOMAIN:
                            device_id = identifier[1]
                            break
                    if device_id:
                        new_config_url = (
                            f"{base_url}/api/googlefindmy/redirect_map/{device_id}"
                            f"?token={auth_token}"
                        )
                        dev_reg.async_update_device(
                            device_id=device.id, configuration_url=new_config_url
                        )
                        updated_count += 1
                        # Log redacted URL (do not leak token)
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
        schema=vol.Schema(
            {vol.Required("device_id"): cv.string, vol.Optional("device_name"): cv.string}
        ),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_URLS,
        async_refresh_device_urls_service,
        schema=vol.Schema({}),
    )
