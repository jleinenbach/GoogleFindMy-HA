"""Google Find My Device integration for Home Assistant.

Version: 2.0 - Location extraction from device list
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import DOMAIN, SERVICE_LOCATE_DEVICE, SERVICE_PLAY_SOUND, SERVICE_LOCATE_EXTERNAL, SERVICE_REFRESH_URLS
from .coordinator import GoogleFindMyCoordinator
from .Auth.token_cache import async_load_cache_from_file
from .map_view import GoogleFindMyMapView, GoogleFindMyMapRedirectView
from .google_home_filter import GoogleHomeFilter
from .storage import LastKnownStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.DEVICE_TRACKER, Platform.BUTTON, Platform.SENSOR, Platform.BINARY_SENSOR]


async def _async_save_secrets_data(secrets_data: dict) -> None:
    """Save complete secrets data to persistent cache asynchronously."""
    from .Auth.token_cache import async_set_cached_value
    from .Auth.username_provider import username_string
    import json
    
    enhanced_data = secrets_data.copy()
    
    google_email = secrets_data.get('username', secrets_data.get('Email'))
    if google_email:
        enhanced_data[username_string] = google_email
    
    for key, value in enhanced_data.items():
        try:
            if isinstance(value, (str, int, float)):
                await async_set_cached_value(key, str(value))
            elif key == 'fcm_credentials':
                await async_set_cached_value(key, json.dumps(value))
            else:
                await async_set_cached_value(key, json.dumps(value))
        except Exception as e:
            _LOGGER.warning(f"Failed to save {key} to persistent cache: {e}")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Google Find My Device from a config entry."""
    try:
        await async_load_cache_from_file()
        _LOGGER.debug("Cache preloaded successfully")
    except Exception as e:
        _LOGGER.warning(f"Failed to preload cache: {e}")
    
    tracked_devices = entry.data.get("tracked_devices", [])
    location_poll_interval = entry.data.get("location_poll_interval", 300)
    device_poll_delay = entry.data.get("device_poll_delay", 5)
    min_poll_interval = entry.data.get("min_poll_interval", 120)
    min_accuracy_threshold = entry.data.get("min_accuracy_threshold", 100)
    movement_threshold = entry.data.get("movement_threshold", 50)

    secrets_data = entry.data.get("secrets_data")
    if not secrets_data:
        _LOGGER.error("Secrets data not found in config entry")
        raise ConfigEntryNotReady("Secrets data not found")

    coordinator = GoogleFindMyCoordinator(
        hass,
        secrets_data=secrets_data,
        tracked_devices=tracked_devices,
        location_poll_interval=location_poll_interval,
        device_poll_delay=device_poll_delay,
        min_poll_interval=min_poll_interval,
        min_accuracy_threshold=min_accuracy_threshold
    )
    
    coordinator.google_home_filter = GoogleHomeFilter(hass, entry.data)
    _LOGGER.debug("Initialized Google Home filter")

    # Load persisted last known locations (no network calls at setup)
    coordinator._store = LastKnownStore(hass)
    try:
        coordinator.last_known_locations = await coordinator._store.async_load()
        _LOGGER.debug("Loaded last known locations for %d device(s)", len(coordinator.last_known_locations))
    except Exception as err:
        _LOGGER.debug("Failed to load last known locations: %s", err)

    if secrets_data:
        try:
            await _async_save_secrets_data(secrets_data)
            _LOGGER.debug("Saved complete secrets data to persistent cache")
        except Exception as e:
            _LOGGER.warning(f"Failed to save secrets data to persistent cache: {e}")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    
    hass.data[DOMAIN]["config_data"] = {
        "min_accuracy_threshold": min_accuracy_threshold,
        "movement_threshold": movement_threshold
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    try:
        map_view = GoogleFindMyMapView(hass)
        hass.http.register_view(map_view)
        _LOGGER.debug("Registered map view")

        redirect_view = GoogleFindMyMapRedirectView(hass)
        hass.http.register_view(redirect_view)
        _LOGGER.debug("Registered map redirect view")
    except Exception as e:
        _LOGGER.warning(f"Failed to register map views: {e}")

    await _async_register_services(hass, coordinator)

    entry.async_on_unload(entry.add_update_listener(async_update_entry))

    return True


async def async_update_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update a config entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    
    coordinator.tracked_devices = entry.data.get("tracked_devices", [])
    coordinator.location_poll_interval = entry.data.get("location_poll_interval", 300)
    coordinator.device_poll_delay = entry.data.get("device_poll_delay", 5)

    if hasattr(coordinator, 'google_home_filter'):
        coordinator.google_home_filter.update_config(entry.data)

    hass.data[DOMAIN]["config_data"] = {
        "min_accuracy_threshold": entry.data.get("min_accuracy_threshold", 100),
        "movement_threshold": entry.data.get("movement_threshold", 50)
    }
    
    coordinator._last_location_poll_time = time.time() - coordinator.location_poll_interval
    
    _LOGGER.info(f"Updated configuration: {len(coordinator.tracked_devices)} tracked devices, {coordinator.location_poll_interval}s poll interval")
    
    await coordinator.async_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def _async_register_services(hass: HomeAssistant, coordinator: GoogleFindMyCoordinator) -> None:
    """Register services for the integration."""
    
    async def async_locate_device_service(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        try:
            await coordinator.async_locate_device(device_id)
        except Exception as err:
            _LOGGER.error("Failed to locate device %s: %s", device_id, err)

    async def async_play_sound_service(call: ServiceCall) -> None:
        device_id = call.data["device_id"]
        try:
            await coordinator.async_play_sound(device_id)
        except Exception as err:
            _LOGGER.error("Failed to play sound on device %s: %s", device_id, err)

    async def async_locate_external_service(call: ServiceCall) -> None:
        """Handle external locate device service call - delegates to normal locate service."""
        device_id = call.data.get("device_id")
        device_name = call.data.get("device_name", device_id)

        _LOGGER.info(f"External location request for device: {device_name} ({device_id}) - delegating to normal locate")

        await async_locate_device_service(call)

    async def async_refresh_device_urls_service(call: ServiceCall) -> None:
        """Handle refresh device URLs service call."""
        try:
            from homeassistant.helpers import device_registry
            from homeassistant.helpers.network import get_url
            import socket

            dev_reg = device_registry.async_get(hass)

            base_url = None
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                s.close()

                port = 8123
                use_ssl = False

                if hasattr(hass, 'http') and hasattr(hass.http, 'server_port'):
                    port = hass.http.server_port or 8123
                    use_ssl = hasattr(hass.http, 'ssl_context') and hass.http.ssl_context is not None

                protocol = "https" if use_ssl else "http"
                base_url = f"{protocol}://{local_ip}:{port}"
                _LOGGER.info(f"Detected local URL for device refresh: {base_url}")

            except Exception as local_err:
                _LOGGER.debug(f"Local IP detection failed: {local_err}, trying HA network detection")
                base_url = get_url(hass, prefer_external=False, allow_cloud=False, allow_external=False, allow_internal=True)
                _LOGGER.info(f"Using HA internal URL for device refresh: {base_url}")

            if not base_url:
                _LOGGER.error("Could not determine base URL for device refresh")
                return

            import hashlib
            import time
            day = str(int(time.time() // 86400))
            ha_uuid = str(hass.data.get("core.uuid", "ha"))
            auth_token = hashlib.md5(f"{ha_uuid}:{day}".encode()).hexdigest()[:16]

            updated_count = 0
            for device in dev_reg.devices.values():
                if any(identifier[0] == DOMAIN for identifier in device.identifiers):
                    device_id = None
                    for identifier in device.identifiers:
                        if identifier[0] == DOMAIN:
                            device_id = identifier[1]
                            break

                    if device_id:
                        new_config_url = f"{base_url}/api/googlefindmy/redirect_map/{device_id}?token={auth_token}"

                        dev_reg.async_update_device(
                            device_id=device.id,
                            configuration_url=new_config_url
                        )
                        updated_count += 1
                        _LOGGER.info(f"Updated URL for device {device.name_by_user or device.name}: {new_config_url}")

            _LOGGER.info(f"Refreshed URLs for {updated_count} Google Find My devices")

        except Exception as err:
            _LOGGER.error("Failed to refresh device URLs: %s", err)

    # Register services
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOCATE_DEVICE,
        async_locate_device_service,
        schema=vol.Schema({
            vol.Required("device_id"): cv.string,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_PLAY_SOUND,
        async_play_sound_service,
        schema=vol.Schema({
            vol.Required("device_id"): cv.string,
        }),
    )
    
    hass.services.async_register(
        DOMAIN,
        SERVICE_LOCATE_EXTERNAL,
        async_locate_external_service,
        schema=vol.Schema({
            vol.Required("device_id"): cv.string,
            vol.Optional("device_name"): cv.string,
        }),
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_URLS,
        async_refresh_device_urls_service,
        schema=vol.Schema({}),
    )
