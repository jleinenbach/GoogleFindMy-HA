"""Config flow for Google Find My Device integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .api import GoogleFindMyAPI

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema({
    vol.Required("secrets_json", description="Paste the complete contents of your secrets.json file"): str
})




class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Google Find My Device."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        """Create the options flow."""
        return OptionsFlowHandler(config_entry)
    
    def __init__(self):
        """Initialize the config flow."""
        self.auth_data = {}
        self.available_devices = []

    def _write_secrets_file(self, file_path: str, data: dict) -> None:
        """Write secrets data to file (sync operation for executor)."""
        import json
        import os

        # Ensure directory exists
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        with open(file_path, 'w') as f:
            json.dump(data, f, indent=2)
    
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step - secrets.json authentication."""
        errors = {}
        
        if user_input is not None:
            secrets_json = user_input.get("secrets_json")
            
            if secrets_json:
                try:
                    import json
                    secrets_data = json.loads(secrets_json)
                    
                    # Store auth data and move to device selection
                    self.auth_data = {
                        "secrets_data": secrets_data,
                        "auth_method": "secrets_json"
                    }
                    
                    return await self.async_step_device_selection()
                except json.JSONDecodeError:
                    errors["base"] = "invalid_json"
                except Exception:
                    errors["base"] = "invalid_token"
            else:
                errors["base"] = "invalid_token"
        
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "info": "Run GoogleFindMyTools on a machine with Chrome, then paste the contents of the generated Auth/secrets.json file here."
            }
        )



    async def async_step_device_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle device selection step."""
        errors = {}
        
        if user_input is not None:
            selected_devices = user_input.get("tracked_devices", [])
            location_poll_interval = user_input.get("location_poll_interval", 300)
            device_poll_delay = user_input.get("device_poll_delay", 5)
            
            # Create config entry with selected devices and polling settings
            final_data = self.auth_data.copy()
            final_data["tracked_devices"] = selected_devices
            final_data["location_poll_interval"] = location_poll_interval
            final_data["device_poll_delay"] = device_poll_delay

            # Write complete secrets.json to disk for file-based cache
            import os
            import json
            secrets_file_path = os.path.join(self.hass.config.config_dir, 'custom_components', 'googlefindmy', 'Auth',
  'secrets.json')

            # Enhance secrets data with username if needed
            enhanced_secrets = self.auth_data["secrets_data"].copy()
            from .Auth.username_provider import username_string
            google_email = enhanced_secrets.get('username', enhanced_secrets.get('Email'))
            if not google_email:
                for key in enhanced_secrets.keys():
                    if key.startswith('adm_token_') and '@' in key:
                        google_email = key.replace('adm_token_', '')
                        break
            if google_email:
                enhanced_secrets[username_string] = google_email

            await self.hass.async_add_executor_job(
                lambda: self._write_secrets_file(secrets_file_path, enhanced_secrets)
            )
            
            return self.async_create_entry(
                title="Google Find My Device",
                data=final_data,
            )
        
        # Get available devices for selection
        if not self.available_devices:
            try:
                api = GoogleFindMyAPI(secrets_data=self.auth_data.get("secrets_data"))
                
                # Get device list (just names, no location data yet)
                devices = await self.hass.async_add_executor_job(api.get_basic_device_list)
                self.available_devices = [(dev["name"], dev["id"]) for dev in devices]
                
            except Exception as e:
                _LOGGER.error("Failed to get device list: %s", e)
                errors["base"] = "cannot_connect"
        
        if errors:
            return self.async_show_form(
                step_id="device_selection",
                data_schema=vol.Schema({}),
                errors=errors,
            )
        
        # Create multi-select for devices
        device_options = {dev_id: dev_name for dev_name, dev_id in self.available_devices}
        
        device_schema = vol.Schema({
            vol.Optional("tracked_devices", default=list(device_options.keys())): vol.All(
                cv.multi_select(device_options),
                vol.Length(min=1)
            ),
            vol.Optional("location_poll_interval", default=300): vol.All(
                vol.Coerce(int),
                vol.Range(min=30, max=3600)
            ),
            vol.Optional("device_poll_delay", default=5): vol.All(
                vol.Coerce(int),
                vol.Range(min=1, max=60)
            )
        })
        
        return self.async_show_form(
            step_id="device_selection",
            data_schema=device_schema,
            description_placeholders={
                "info": "Select devices to track and configure polling settings:\n• Location poll interval: How often to start a new polling cycle (30-3600 seconds)\n• Device poll delay: Delay between individual device polls within a cycle (1-60 seconds)\n\nThe integration cycles through devices sequentially, requesting location data with the specified delay between each device."
            }
        )



class OptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Google Find My Device."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__()

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors = {}
        
        if user_input is not None:
            # Update the config entry with new values
            new_data = self.config_entry.data.copy()
            new_data.update({
                "tracked_devices": user_input.get("tracked_devices", []),
                "location_poll_interval": user_input.get("location_poll_interval", 300),
                "device_poll_delay": user_input.get("device_poll_delay", 5),
                "min_accuracy_threshold": user_input.get("min_accuracy_threshold", 100),
                "movement_threshold": user_input.get("movement_threshold", 50)
            })
            
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=new_data
            )
            
            return self.async_create_entry(title="", data={})

        # Get current settings
        current_tracked = self.config_entry.data.get("tracked_devices", [])
        current_interval = self.config_entry.data.get("location_poll_interval", 300)
        
        # Get available devices
        try:
            api = GoogleFindMyAPI(secrets_data=self.config_entry.data.get("secrets_data"))
            
            devices = await self.hass.async_add_executor_job(api.get_basic_device_list)
            device_options = {dev["id"]: dev["name"] for dev in devices}
            
        except Exception as e:
            _LOGGER.error("Failed to get device list for options: %s", e)
            errors["base"] = "cannot_connect"
            device_options = {}

        if errors:
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema({}),
                errors=errors,
            )

        options_schema = vol.Schema({
            vol.Optional("tracked_devices", default=current_tracked): vol.All(
                cv.multi_select(device_options),
                vol.Length(min=1)
            ),
            vol.Optional("location_poll_interval", default=current_interval): vol.All(
                vol.Coerce(int),
                vol.Range(min=30, max=3600)
            ),
            vol.Optional("device_poll_delay", default=self.config_entry.data.get("device_poll_delay", 5)): vol.All(
                vol.Coerce(int),
                vol.Range(min=1, max=60)
            ),
            vol.Optional("min_accuracy_threshold", default=self.config_entry.data.get("min_accuracy_threshold", 100)): vol.All(
                vol.Coerce(int),
                vol.Range(min=25, max=500)
            ),
            vol.Optional("movement_threshold", default=self.config_entry.data.get("movement_threshold", 50)): vol.All(
                vol.Coerce(int),
                vol.Range(min=10, max=200)
            )
        })

        return self.async_show_form(
            step_id="init",
            data_schema=options_schema,
            description_placeholders={
                "info": "Modify tracking settings:\n• Location poll interval: How often to poll for locations (30-3600 seconds). Device poll delay: Delay between device polls (1-60 seconds)\n• Min accuracy threshold: Ignore locations worse than this (25-500 meters)\n• Movement threshold: Minimum distance to consider real movement (10-200 meters)\n\nThese settings help reduce location bouncing and false away/home triggers."
            }
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
