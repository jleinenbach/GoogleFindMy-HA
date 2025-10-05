"""Config flow for Google Find My Device (custom integration)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME  # generic keys if needed
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

# Import selectors defensively (older HA versions may not have all selectors available)
try:
    from homeassistant.helpers.selector import (
        selector,
    )
except Exception:  # noqa: BLE001
    selector = None  # type: ignore[assignment]

from .api import GoogleFindMyAPI
from .const import (
    DOMAIN,
    CONF_OAUTH_TOKEN,
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------
# Field names (non-secret)
# ---------------------------
CONF_TRACKED_DEVICES = "tracked_devices"
CONF_LOCATION_POLL_INTERVAL = "location_poll_interval"
CONF_DEVICE_POLL_DELAY = "device_poll_delay"
CONF_MIN_ACCURACY = "min_accuracy_threshold"
CONF_MOVEMENT_THRESHOLD = "movement_threshold"
CONF_GH_FILTER_ENABLED = "google_home_filter_enabled"
CONF_GH_FILTER_KEYWORDS = "google_home_filter_keywords"
CONF_ENABLE_STATS = "enable_stats_entities"
CONF_MAP_TOKEN_EXP = "map_view_token_expiration"

# ---------------------------
# Auth methods & schemas
# ---------------------------
STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("auth_method"): vol.In(
            {
                "secrets_json": "GoogleFindMyTools secrets.json",
                # Keep the 2nd path hidden in UI for now if not used:
                # "individual_tokens": "Manual token + email"
            }
        )
    }
)

STEP_SECRETS_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(
            "secrets_json",
            description="Paste the complete contents of your secrets.json file",
        ): str
    }
)

STEP_INDIVIDUAL_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OAUTH_TOKEN, description="OAuth token"): str,
        vol.Required("google_email", description="Google email address"): str,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow for Google Find My Device."""

    VERSION = 1

    def __init__(self) -> None:
        self._auth_data: Dict[str, Any] = {}
        self._available_devices: List[Tuple[str, str]] = []

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        return OptionsFlowHandler(config_entry)

    # ---------- User entry point ----------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Entry step: choose auth method."""
        if user_input is not None:
            if user_input.get("auth_method") == "secrets_json":
                return await self.async_step_secrets_json()
            # elif user_input.get("auth_method") == "individual_tokens":
            #     return await self.async_step_individual_tokens()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            description_placeholders={
                "info": "Authenticate using GoogleFindMyTools secrets.json generated on a machine with Chrome."
            },
        )

    # ---------- Secrets.json path ----------
    async def async_step_secrets_json(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect and validate secrets.json content."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            raw = user_input.get("secrets_json", "")
            try:
                secrets_data = json.loads(raw)
                # Store raw secrets payload in memory to construct API and fetch devices next
                self._auth_data = {
                    "auth_method": "secrets_json",
                    "secrets_data": secrets_data,
                }
                return await self.async_step_device_selection()
            except json.JSONDecodeError:
                errors["base"] = "invalid_json"
            except Exception:  # noqa: BLE001
                errors["base"] = "invalid_token"

        return self.async_show_form(
            step_id="secrets_json",
            data_schema=STEP_SECRETS_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "info": "Paste the content of Auth/secrets.json produced by GoogleFindMyTools."
            },
        )

    # ---------- Manual tokens path (kept for completeness; optional in UI) ----------
    async def async_step_individual_tokens(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect manual OAuth token + email."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            oauth_token = user_input.get(CONF_OAUTH_TOKEN)
            google_email = user_input.get("google_email")
            if oauth_token and google_email:
                self._auth_data = {
                    "auth_method": "individual_tokens",
                    CONF_OAUTH_TOKEN: oauth_token,
                    "google_email": google_email,
                }
                return await self.async_step_device_selection()
            errors["base"] = "invalid_token"

        return self.async_show_form(
            step_id="individual_tokens",
            data_schema=STEP_INDIVIDUAL_DATA_SCHEMA,
            errors=errors,
            description_placeholders={"info": "Enter OAuth token + Google email."},
        )

    # ---------- Shared helper to create API from stored auth_data ----------
    async def _async_build_api_and_username(self) -> Tuple[GoogleFindMyAPI, Optional[str]]:
        """Build API instance and derive a username if available (async-safe)."""
        if self._auth_data.get("auth_method") == "secrets_json":
            secrets_data = self._auth_data.get("secrets_data") or {}
            api = GoogleFindMyAPI(secrets_data=secrets_data)
            username = secrets_data.get("googleHomeUsername") or secrets_data.get("google_email")
            return api, username
        # Manual path
        api = GoogleFindMyAPI(
            oauth_token=self._auth_data.get(CONF_OAUTH_TOKEN),
            google_email=self._auth_data.get("google_email"),
        )
        return api, self._auth_data.get("google_email")

    # ---------- Device selection ----------
    async def async_step_device_selection(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select tracked devices and set poll intervals (initial create)."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            # NOTE: For backward-compat, we still store initial non-secret settings in data.
            # A future migration can move them to options; OptionsFlow already uses options.
            final_data = dict(self._auth_data)
            final_data[CONF_TRACKED_DEVICES] = user_input.get(CONF_TRACKED_DEVICES, [])
            final_data[CONF_LOCATION_POLL_INTERVAL] = user_input.get(CONF_LOCATION_POLL_INTERVAL, 300)
            final_data[CONF_DEVICE_POLL_DELAY] = user_input.get(CONF_DEVICE_POLL_DELAY, 5)

            return self.async_create_entry(
                title="Google Find My Device",
                data=final_data,
            )

        # Populate device choices once
        if not self._available_devices:
            try:
                api, username = await self._async_build_api_and_username()
                devices = await api.async_get_basic_device_list(username)
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    # store as (name, id)
                    self._available_devices = [(d["name"], d["id"]) for d in devices]
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Failed to fetch devices during setup: %s", err)
                errors["base"] = "cannot_connect"

        if errors:
            return self.async_show_form(step_id="device_selection", data_schema=vol.Schema({}), errors=errors)

        # Build a multi-select; keep cv.multi_select for universal compatibility
        options_map = {dev_id: dev_name for (dev_name, dev_id) in self._available_devices}
        schema = vol.Schema(
            {
                vol.Optional(CONF_TRACKED_DEVICES, default=list(options_map.keys())): vol.All(
                    cv.multi_select(options_map), vol.Length(min=1)
                ),
                vol.Optional(CONF_LOCATION_POLL_INTERVAL, default=300): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                vol.Optional(CONF_DEVICE_POLL_DELAY, default=5): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
            }
        )
        return self.async_show_form(step_id="device_selection", data_schema=schema)

    # ---------- Reauth (triggered by HA when credentials invalid) ----------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauthentication flow with context."""
        # Provide context why user is here
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect new credentials for reauth and validate them."""
        errors: Dict[str, str] = {}

        # Accept either new secrets.json or new token+email; do not display stored values
        fields = {
            vol.Optional("secrets_json"): str,
            vol.Optional(CONF_OAUTH_TOKEN): str,
            vol.Optional("google_email"): str,
        }
        schema = vol.Schema(fields)

        if user_input is not None:
            secrets_json = user_input.get("secrets_json")
            oauth_token = user_input.get(CONF_OAUTH_TOKEN)
            google_email = user_input.get("google_email")

            new_data: Dict[str, Any] = {}
            try:
                if secrets_json:
                    parsed = json.loads(secrets_json)
                    new_data = {"auth_method": "secrets_json", "secrets_data": parsed}
                    api = GoogleFindMyAPI(secrets_data=parsed)
                    # validate by listing devices
                    devices = await api.async_get_basic_device_list(
                        parsed.get("googleHomeUsername") or parsed.get("google_email")
                    )
                else:
                    if not (oauth_token and google_email):
                        errors["base"] = "invalid_token"
                        return self.async_show_form(
                            step_id="reauth_confirm",
                            data_schema=schema,
                            errors=errors,
                            description_placeholders={
                                "reason": "Your credentials are invalid or expired. Provide new ones."
                            },
                        )
                    new_data = {
                        "auth_method": "individual_tokens",
                        CONF_OAUTH_TOKEN: oauth_token,
                        "google_email": google_email,
                    }
                    api = GoogleFindMyAPI(oauth_token=oauth_token, google_email=google_email)
                    devices = await api.async_get_basic_device_list(google_email)

                if devices is None:
                    errors["base"] = "cannot_connect"
                elif devices == []:
                    # Consider empty list as valid but warn via logs
                    _LOGGER.warning("Reauth validation succeeded but returned zero devices")
                else:
                    _LOGGER.debug("Reauth validation returned %d device(s)", len(devices))
            except json.JSONDecodeError:
                errors["base"] = "invalid_json"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Reauth validation failed: %s", err)
                errors["base"] = "cannot_connect"

            if not errors:
                # Update entry data with new credentials only; DO NOT touch options here.
                assert self._get_active_entry() is not None
                entry = self._get_active_entry()
                updated_data = dict(entry.data)
                updated_data.update(new_data)

                # Use helper to update + reload + abort with success message
                return self.async_update_reload_and_abort(
                    entry=entry,
                    data=updated_data,
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "reason": "Your credentials are invalid or expired. Please provide new ones.",
            },
        )

    def _get_active_entry(self) -> Optional[ConfigEntry]:
        """Find the config entry associated with this flow (single-entry custom integration)."""
        for entry in self._async_current_entries():
            if entry.domain == DOMAIN:
                return entry
        return None


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Options flow to (a) update non-secret settings and (b) optionally refresh credentials."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self.config_entry = config_entry  # provided by parent in modern HA

    # ---------- Menu entry ----------
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show a small menu: edit settings vs. update credentials."""
        if selector:
            menu_schema = vol.Schema(
                {
                    vol.Required("action"): selector(
                        {
                            "select": {
                                "options": [
                                    {"value": "settings", "label": "Tracking & polling settings"},
                                    {"value": "credentials", "label": "Update credentials (hidden fields)"},
                                ]
                            }
                        }
                    )
                }
            )
        else:
            menu_schema = vol.Schema({vol.Required("action"): vol.In(["settings", "credentials"])})

        if user_input is not None:
            action = user_input["action"]
            if action == "settings":
                return await self.async_step_settings()
            if action == "credentials":
                return await self.async_step_credentials()
        return self.async_show_form(step_id="init", data_schema=menu_schema)

    # ---------- Settings (non-secret) ----------
    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Update non-secret options. Writes to options and temporarily mirrors to data for backward compatibility."""
        errors: Dict[str, str] = {}

        # Current values from options (prefer) with fallback to data for backward compatibility
        entry = self.config_entry
        opt = entry.options
        dat = entry.data

        current_tracked = opt.get(CONF_TRACKED_DEVICES, dat.get(CONF_TRACKED_DEVICES, []))
        current_interval = opt.get(CONF_LOCATION_POLL_INTERVAL, dat.get(CONF_LOCATION_POLL_INTERVAL, 300))
        current_delay = opt.get(CONF_DEVICE_POLL_DELAY, dat.get(CONF_DEVICE_POLL_DELAY, 5))
        current_min_acc = opt.get(CONF_MIN_ACCURACY, dat.get(CONF_MIN_ACCURACY, 100))
        current_move_thr = opt.get(CONF_MOVEMENT_THRESHOLD, dat.get(CONF_MOVEMENT_THRESHOLD, 50))
        current_gh_enabled = opt.get(CONF_GH_FILTER_ENABLED, dat.get(CONF_GH_FILTER_ENABLED, DEFAULT_GOOGLE_HOME_FILTER_ENABLED))
        current_gh_keywords = opt.get(CONF_GH_FILTER_KEYWORDS, dat.get(CONF_GH_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS))
        current_stats = opt.get(CONF_ENABLE_STATS, dat.get(CONF_ENABLE_STATS, True))
        current_map_token_exp = opt.get(CONF_MAP_TOKEN_EXP, dat.get(CONF_MAP_TOKEN_EXP, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION))

        # Build device list (best effort)
        device_options: Dict[str, str] = {}
        try:
            api = await self._async_build_api_from_entry(entry)
            # Prefer async variant; if secrets path we may pass username=None for auto-detect
            devices = await api.async_get_basic_device_list(entry.data.get("google_email"))
            device_options = {dev["id"]: dev["name"] for dev in devices}
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to fetch device list for options: %s", err)
            # Keep empty; form still usable

        if user_input is not None:
            # Persist to options (canonical place) …
            new_options = {
                CONF_TRACKED_DEVICES: user_input.get(CONF_TRACKED_DEVICES, current_tracked),
                CONF_LOCATION_POLL_INTERVAL: user_input.get(CONF_LOCATION_POLL_INTERVAL, current_interval),
                CONF_DEVICE_POLL_DELAY: user_input.get(CONF_DEVICE_POLL_DELAY, current_delay),
                CONF_MIN_ACCURACY: user_input.get(CONF_MIN_ACCURACY, current_min_acc),
                CONF_MOVEMENT_THRESHOLD: user_input.get(CONF_MOVEMENT_THRESHOLD, current_move_thr),
                CONF_GH_FILTER_ENABLED: user_input.get(CONF_GH_FILTER_ENABLED, current_gh_enabled),
                CONF_GH_FILTER_KEYWORDS: user_input.get(CONF_GH_FILTER_KEYWORDS, current_gh_keywords),
                CONF_ENABLE_STATS: user_input.get(CONF_ENABLE_STATS, current_stats),
                CONF_MAP_TOKEN_EXP: user_input.get(CONF_MAP_TOKEN_EXP, current_map_token_exp),
            }

            # … and (temporarily) shadow-copy non-secrets into data for backward compatibility
            # IMPORTANT: Never mirror credentials.
            shadow_data = dict(entry.data)
            for k, v in new_options.items():
                shadow_data[k] = v

            self.hass.config_entries.async_update_entry(entry, options=new_options, data=shadow_data)
            # Show a small finish step to provide UX feedback
            return await self.async_step_finish(mode="settings")

        # Build schema (use cv.multi_select for wide compatibility)
        schema = vol.Schema(
            {
                vol.Optional(CONF_TRACKED_DEVICES, default=current_tracked): vol.All(
                    cv.multi_select(device_options), vol.Length(min=0)
                ),
                vol.Optional(CONF_LOCATION_POLL_INTERVAL, default=current_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=60, max=3600)
                ),
                vol.Optional(CONF_DEVICE_POLL_DELAY, default=current_delay): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=60)
                ),
                vol.Optional(CONF_MIN_ACCURACY, default=current_min_acc): vol.All(
                    vol.Coerce(int), vol.Range(min=25, max=500)
                ),
                vol.Optional(CONF_MOVEMENT_THRESHOLD, default=current_move_thr): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=200)
                ),
                vol.Optional(CONF_GH_FILTER_ENABLED, default=current_gh_enabled): bool,
                vol.Optional(CONF_GH_FILTER_KEYWORDS, default=current_gh_keywords): str,
                vol.Optional(CONF_ENABLE_STATS, default=current_stats): bool,
                vol.Optional(CONF_MAP_TOKEN_EXP, default=current_map_token_exp): bool,
            }
        )

        return self.async_show_form(
            step_id="settings",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "info": "Adjust tracking and polling behavior. Changes are applied on save."
            },
        )

    # ---------- Credentials update (hidden/optional; fields are always empty) ----------
    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Allow refreshing credentials without exposing current values."""
        errors: Dict[str, str] = {}

        # Always show empty optional fields
        schema = vol.Schema(
            {
                vol.Optional("secrets_json"): str,
                vol.Optional(CONF_OAUTH_TOKEN): str,
                vol.Optional("google_email"): str,
            }
        )

        if user_input is not None:
            secrets_json = user_input.get("secrets_json")
            oauth_token = user_input.get(CONF_OAUTH_TOKEN)
            google_email = user_input.get("google_email")

            new_data: Dict[str, Any] = {}
            try:
                if secrets_json:
                    parsed = json.loads(secrets_json)
                    new_data = {"auth_method": "secrets_json", "secrets_data": parsed}
                    api = GoogleFindMyAPI(secrets_data=parsed)
                    _ = await api.async_get_basic_device_list(
                        parsed.get("googleHomeUsername") or parsed.get("google_email")
                    )  # validation
                elif oauth_token and google_email:
                    new_data = {"auth_method": "individual_tokens", CONF_OAUTH_TOKEN: oauth_token, "google_email": google_email}
                    api = GoogleFindMyAPI(oauth_token=oauth_token, google_email=google_email)
                    _ = await api.async_get_basic_device_list(google_email)  # validation
                else:
                    errors["base"] = "invalid_token"

                if not errors:
                    entry = self.config_entry
                    updated_data = dict(entry.data)
                    updated_data.update(new_data)
                    # Update credentials in data only (never in options) and reload for consistency
                    return self.async_update_reload_and_abort(
                        entry=entry,
                        data=updated_data,
                        reason="reconfigure_successful",
                        reload_even_if_entry_is_unchanged=True,
                    )
            except json.JSONDecodeError:
                errors["base"] = "invalid_json"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Credentials update failed: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "info": "Update credentials. Leave fields empty to keep existing values. Nothing is shown back."
            },
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None, *, mode: str = "settings") -> FlowResult:
        """UX feedback step after saving; immediately completes."""
        # In a full i18n setup, this would use translation keys & description_placeholders.
        return self.async_create_entry(title="", data={"result": f"{mode}_saved"} )

    # ---------- Internal helper ----------
    async def _async_build_api_from_entry(self, entry: ConfigEntry) -> GoogleFindMyAPI:
        """Construct API object from entry data (supports both auth methods)."""
        if entry.data.get("auth_method") == "secrets_json":
            return GoogleFindMyAPI(secrets_data=entry.data.get("secrets_data"))
        return GoogleFindMyAPI(
            oauth_token=entry.data.get(CONF_OAUTH_TOKEN),
            google_email=entry.data.get("google_email"),
        )


# ---------- Custom exceptions ----------
class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
