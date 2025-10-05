"""Google Find My Device integration for Home Assistant.
Version: 2.1 - Entities-first bootstrap with deferred initial refresh
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall, CoreState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
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

# Platforms provided by this integration
PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


def _redact_url_token(url: str) -> str:
    """Return URL with any 'token' query parameter value redacted for safe logging.
    We never want to leak authentication/authorization tokens into logs or bug reports.
    This helper keeps the URL readable while masking the secret.
    """
    try:
        from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        redacted = []
        for k, v in q:
            if k.lower() == "token" and v:
                # Keep a tiny hint of length without exposing the secret
                red_v = (v[:2] + "…" + v[-2:]) if len(v) > 4 else "****"
                redacted.append((k, red_v))
            else:
                redacted.append((k, v))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(redacted, doseq=True), parts.fragment)
        )
    except Exception:
        # In worst case, fall back to original to avoid breaking logs (still try not to log raw tokens)
        return url


async def _async_save_secrets_data(secrets_data: dict) -> None:
    """Persist secrets data to the integration cache (async, non-blocking).
    All storage happens using the integration's async token_cache helpers.
    Complex values are serialized to JSON strings to avoid blocking I/O in the event loop.
    """
    from .Auth.token_cache import async_set_cached_value
    from .Auth.username_provider import username_string
    import json

    enhanced_data = secrets_data.copy()

    # Derive and persist the username in a normalized way (works for both old/new keys)
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
    """Set up Google Find My Device from a config entry (entities-first)."""

    # Preload cache early
    try:
        await async_load_cache_from_file()
        _LOGGER.debug("Cache preloaded successfully")
    except Exception as err:
        _LOGGER.warning("Failed to preload cache: %s", err)

    # Extract configuration (from entry.data)
    tracked_devices = entry.data.get("tracked_devices", [])
    location_poll_interval = entry.data.get("location_poll_interval", 300)
    device_poll_delay = entry.data.get("device_poll_delay", 5)
    min_poll_interval = entry.data.get("min_poll_interval", 120)
    min_accuracy_threshold = entry.data.get("min_accuracy_threshold", 100)
    movement_threshold = entry.data.get("movement_threshold", 50)
    allow_history_fallback = entry.data.get("allow_history_fallback", False)

    # Obtain secrets bundle (required)
    secrets_data = entry.data.get("secrets_data")
    if not secrets_data:
        _LOGGER.error("Secrets data not found in config entry")
        raise ConfigEntryNotReady("Secrets data not found")

    # Initialize coordinator (first refresh is deferred until HA is started)
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
    coordinator.config_entry = entry  # convenience for platforms

    # Optional: attach Google Home filter
    from .google_home_filter import GoogleHomeFilter

    coordinator.google_home_filter = GoogleHomeFilter(hass, entry.data)
    _LOGGER.debug("Initialized Google Home filter")

    # Persist secrets asynchronously (fire-and-forget)
    try:
        await _async_save_secrets_data(secrets_data)
        _LOGGER.debug("Saved complete secrets data to persistent cache")
    except Exception as err:
        _LOGGER.warning("Failed to save secrets data to persistent cache: %s", err)

    # Share coordinator & config in hass.data immediately (so platforms can restore)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    hass.data[DOMAIN]["config_data"] = {
        "min_accuracy_threshold": min_accuracy_threshold,
        "movement_threshold": movement_threshold,
    }

    # Register map views early
    try:
        hass.http.register_view(GoogleFindMyMapView(hass))
        hass.http.register_view(GoogleFindMyMapRedirectView(hass))
        _LOGGER.debug("Registered map views")
    except Exception as err:
        _LOGGER.warning("Failed to register map views: %s", err)

    # Register services (available regardless of data freshness)
    await _async_register_services(hass, coordinator)

    # --- ENTITIES-FIRST: forward platforms now so RestoreEntity can populate immediately ---
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Defer the first refresh until HA is fully started
    # ---------------- FIX: make one-time listener idempotent ----------------
    listener_active = False  # becomes True only if we actually register the one-time listener

    async def _do_first_refresh(_: Any) -> None:
        """Perform the initial coordinator refresh after HA has started."""
        nonlocal listener_active
        # Mark as consumed: prevents later unsub() on already-removed one-time listener
        listener_active = False
        try:
            await coordinator.async_refresh()
            if not coordinator.last_update_success:
                _LOGGER.warning(
                    "Initial refresh did not succeed; entities will recover on subsequent polls."
                )
        except Exception as err:
            _LOGGER.error("Initial refresh raised an unexpected error: %s", err)
    # -----------------------------------------------------------------------

    # ----- MINI-HARDENING START: wrap scheduling and listener registration -----
    if hass.state == CoreState.running:
        try:
            hass.async_create_task(_do_first_refresh(None))
        except Exception as err:
            _LOGGER.error("Failed to schedule initial refresh task: %s", err)
    else:
        try:
            unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _do_first_refresh)
            listener_active = True  # listener successfully registered and pending
        except Exception as err:
            _LOGGER.error("Failed to register initial refresh listener: %s", err)
        else:
            def _safe_unsub() -> None:
                # Only call unsub() if the one-time listener is still pending.
                if listener_active:
                    try:
                        unsub()
                    except Exception:
                        # Listener already removed or never registered; ignore
                        pass

            entry.async_on_unload(_safe_unsub)
    # ----- MINI-HARDENING END -----

    # React to entry updates (options) and apply changes
    entry.async_on_unload(entry.add_update_listener(async_update_entry))
    return True


async def async_update_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates.
    We push new options into the coordinator and trigger a refresh without blocking the loop.
    """
    coordinator: GoogleFindMyCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Update coordinator knobs
    coordinator.tracked_devices = entry.data.get("tracked_devices", [])
    coordinator.location_poll_interval = entry.data.get("location_poll_interval", 300)
    coordinator.device_poll_delay = entry.data.get("device_poll_delay", 5)
    coordinator.min_poll_interval = entry.data.get("min_poll_interval", 120)
    coordinator._min_accuracy_threshold = entry.data.get("min_accuracy_threshold", 100)  # noqa: SLF001
    coordinator.allow_history_fallback = entry.data.get("allow_history_fallback", False)

    # Update Google Home filter configuration
    if hasattr(coordinator, "google_home_filter"):
        coordinator.google_home_filter.update_config(entry.data)

    # Share updated config for platforms
    hass.data[DOMAIN]["config_data"] = {
        "min_accuracy_threshold": entry.data.get("min_accuracy_threshold", 100),
        "movement_threshold": entry.data.get("movement_threshold", 50),
    }

    # Reset scheduling baseline so the next cycle is due immediately
    try:
        effective_interval = max(coordinator.location_poll_interval, coordinator.min_poll_interval)
    except Exception:
        effective_interval = coordinator.location_poll_interval

    # Coordinator uses a monotonic timestamp for scheduling; subtract interval to force due
    coordinator._last_poll_mono = time.monotonic() - float(effective_interval)  # noqa: SLF001

    _LOGGER.info(
        "Updated configuration: %d tracked devices, %ss poll interval",
        len(coordinator.tracked_devices),
        coordinator.location_poll_interval,
    )

    # Trigger an immediate refresh (non-blocking)
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
        The device registry requires a valid **absolute HTTP(S) URL**. We therefore build a base URL once
        via `get_url(... prefer_external=True, allow_cloud=True, allow_external=True, allow_internal=True)`
        and **avoid** relative paths here. Browser navigation remains origin-agnostic thanks to the Redirect View
        (which issues a relative `Location`). Token is rotated weekly by default (configurable). All logs redact the token.
        """
        try:
            from homeassistant.helpers import device_registry
            from homeassistant.helpers.network import get_url
            import hashlib

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
                    e0.data.get(
                        "map_view_token_expiration",
                        DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
                    ),
                )

            if token_expiration_enabled:
                week = str(int(time.time() // 604800))  # current week bucket
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
