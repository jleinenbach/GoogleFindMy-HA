"""Config flow for Google Find My Device (custom integration)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

# Import selectors defensively (older HA versions may not have all selectors available)
try:
    from homeassistant.helpers.selector import selector
except Exception:  # noqa: BLE001
    selector = None  # type: ignore[assignment]

from .api import GoogleFindMyAPI
from .const import (
    DOMAIN,
    # Data (credentials, immutable)
    CONF_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DATA_AUTH_METHOD,
    # Options (user-changeable)
    OPT_TRACKED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MOVEMENT_THRESHOLD,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    # Defaults
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MOVEMENT_THRESHOLD,
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    DEFAULT_ENABLE_STATS_ENTITIES,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------
# Auth method list
# ---------------------------
_AUTH_METHOD_SECRETS = "secrets_json"
_AUTH_METHOD_INDIVIDUAL = "individual_tokens"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("auth_method"): vol.In(
            {
                _AUTH_METHOD_SECRETS: "GoogleFindMyTools secrets.json",
                _AUTH_METHOD_INDIVIDUAL: "Manual token + email",
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
        vol.Required(CONF_GOOGLE_EMAIL, description="Google email address"): str,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow for Google Find My Device."""

    VERSION = 1

    def __init__(self) -> None:
        # Keep transient auth info between steps; never log the values.
        self._auth_data: Dict[str, Any] = {}
        self._available_devices: List[Tuple[str, str]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow.

        Note:
            Do not pass config_entry into the handler and do not assign
            self.config_entry manually. The base class provides it.
        """
        return OptionsFlowHandler()

    # ---------- User entry point ----------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Entry step: choose auth method."""
        if user_input is not None:
            method = user_input.get("auth_method")
            if method == _AUTH_METHOD_SECRETS:
                return await self.async_step_secrets_json()
            if method == _AUTH_METHOD_INDIVIDUAL:
                return await self.async_step_individual_tokens()

        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

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
                    DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                    DATA_SECRET_BUNDLE: secrets_data,
                }
                return await self.async_step_device_selection()
            except json.JSONDecodeError:
                errors["base"] = "invalid_json"
            except Exception:  # noqa: BLE001
                errors["base"] = "invalid_token"

        return self.async_show_form(step_id="secrets_json", data_schema=STEP_SECRETS_DATA_SCHEMA, errors=errors)

    # ---------- Manual tokens path ----------
    async def async_step_individual_tokens(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect manual OAuth token + email."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            oauth_token = user_input.get(CONF_OAUTH_TOKEN)
            google_email = user_input.get(CONF_GOOGLE_EMAIL)
            if oauth_token and google_email:
                self._auth_data = {
                    DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                    CONF_OAUTH_TOKEN: oauth_token,
                    CONF_GOOGLE_EMAIL: google_email,
                }
                return await self.async_step_device_selection()
            errors["base"] = "invalid_token"

        return self.async_show_form(step_id="individual_tokens", data_schema=STEP_INDIVIDUAL_DATA_SCHEMA, errors=errors)

    # ---------- Shared helper to create API from stored auth_data ----------
    async def _async_build_api_and_username(self) -> Tuple[GoogleFindMyAPI, Optional[str]]:
        """Build API instance and derive a username if available (async-safe)."""
        if self._auth_data.get(DATA_AUTH_METHOD) == _AUTH_METHOD_SECRETS:
            secrets_data = self._auth_data.get(DATA_SECRET_BUNDLE) or {}
            api = GoogleFindMyAPI(secrets_data=secrets_data)
            username = secrets_data.get("googleHomeUsername") or secrets_data.get("google_email")
            return api, username
        # Manual path
        api = GoogleFindMyAPI(
            oauth_token=self._auth_data.get(CONF_OAUTH_TOKEN),
            google_email=self._auth_data.get(CONF_GOOGLE_EMAIL),
        )
        return api, self._auth_data.get(CONF_GOOGLE_EMAIL)

    # ---------- Device selection ----------
    async def async_step_device_selection(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select tracked devices and set poll intervals (initial create)."""
        errors: Dict[str, str] = {}

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

        # If we could not fetch anything, show a form with just the error.
        if errors:
            return self.async_show_form(step_id="device_selection", data_schema=vol.Schema({}), errors=errors)

        # Build a multi-select; keep cv.multi_select for universal compatibility
        options_map = {dev_id: dev_name for (dev_name, dev_id) in self._available_devices}
        schema = vol.Schema(
            {
                vol.Optional(OPT_TRACKED_DEVICES, default=list(options_map.keys())): vol.All(
                    cv.multi_select(options_map), vol.Length(min=1)
                ),
                vol.Optional(OPT_LOCATION_POLL_INTERVAL, default=DEFAULT_LOCATION_POLL_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=60, max=3600)
                ),
                vol.Optional(OPT_DEVICE_POLL_DELAY, default=DEFAULT_DEVICE_POLL_DELAY): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=60)
                ),
                # Provide a sensible initial set of advanced defaults (hidden in strings for UX)
                vol.Optional(OPT_MIN_ACCURACY_THRESHOLD, default=DEFAULT_MIN_ACCURACY_THRESHOLD): vol.All(
                    vol.Coerce(int), vol.Range(min=25, max=500)
                ),
                vol.Optional(OPT_MOVEMENT_THRESHOLD, default=DEFAULT_MOVEMENT_THRESHOLD): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=200)
                ),
                vol.Optional(OPT_GOOGLE_HOME_FILTER_ENABLED, default=DEFAULT_GOOGLE_HOME_FILTER_ENABLED): bool,
                vol.Optional(OPT_GOOGLE_HOME_FILTER_KEYWORDS, default=DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS): str,
                vol.Optional(OPT_ENABLE_STATS_ENTITIES, default=DEFAULT_ENABLE_STATS_ENTITIES): bool,
                vol.Optional(OPT_MAP_VIEW_TOKEN_EXPIRATION, default=DEFAULT_MAP_VIEW_TOKEN_EXPIRATION): bool,
            }
        )

        if user_input is not None:
            # Create credentials payload for entry.data only
            data_payload: Dict[str, Any] = dict(self._auth_data)

            # Options (canonical place for non-secrets)
            options_payload: Dict[str, Any] = {
                OPT_TRACKED_DEVICES: user_input.get(OPT_TRACKED_DEVICES, []),
                OPT_LOCATION_POLL_INTERVAL: user_input.get(
                    OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL
                ),
                OPT_DEVICE_POLL_DELAY: user_input.get(OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY),
                OPT_MIN_ACCURACY_THRESHOLD: user_input.get(
                    OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD
                ),
                OPT_MOVEMENT_THRESHOLD: user_input.get(OPT_MOVEMENT_THRESHOLD, DEFAULT_MOVEMENT_THRESHOLD),
                OPT_GOOGLE_HOME_FILTER_ENABLED: user_input.get(
                    OPT_GOOGLE_HOME_FILTER_ENABLED, DEFAULT_GOOGLE_HOME_FILTER_ENABLED
                ),
                OPT_GOOGLE_HOME_FILTER_KEYWORDS: user_input.get(
                    OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS
                ),
                OPT_ENABLE_STATS_ENTITIES: user_input.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES),
                OPT_MAP_VIEW_TOKEN_EXPIRATION: user_input.get(
                    OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
                ),
            }

            # Prefer modern HA that supports options at create time; fallback to data-only + migration.
            try:
                return self.async_create_entry(
                    title="Google Find My Device",
                    data=data_payload,
                    options=options_payload,  # type: ignore[call-arg]
                )
            except TypeError:
                # Older HA: shadow-copy options into data for backward compatibility.
                shadow = dict(data_payload)
                shadow.update(options_payload)
                return self.async_create_entry(title="Google Find My Device", data=shadow)

        return self.async_show_form(step_id="device_selection", data_schema=schema)

    # ---------- Reauth (triggered by HA when credentials invalid) ----------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauthentication flow with context."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect new credentials for reauth and validate them."""
        errors: Dict[str, str] = {}

        # Always-empty optional fields (never reveal existing data)
        schema = vol.Schema(
            {
                vol.Optional("secrets_json"): str,
                vol.Optional(CONF_OAUTH_TOKEN): str,
                vol.Optional(CONF_GOOGLE_EMAIL): str,
            }
        )

        if user_input is not None:
            secrets_json = user_input.get("secrets_json")
            oauth_token = user_input.get(CONF_OAUTH_TOKEN)
            google_email = user_input.get(CONF_GOOGLE_EMAIL)

            new_data: Dict[str, Any] = {}
            try:
                if secrets_json:
                    parsed = json.loads(secrets_json)
                    new_data = {DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS, DATA_SECRET_BUNDLE: parsed}
                    api = GoogleFindMyAPI(secrets_data=parsed)
                    # Basic validation: try device list
                    _ = await api.async_get_basic_device_list(
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
                        DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                        CONF_OAUTH_TOKEN: oauth_token,
                        CONF_GOOGLE_EMAIL: google_email,
                    }
                    api = GoogleFindMyAPI(oauth_token=oauth_token, google_email=google_email)
                    _ = await api.async_get_basic_device_list(google_email)  # validation

            except json.JSONDecodeError:
                errors["base"] = "invalid_json"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Reauth validation failed: %s", err)
                errors["base"] = "cannot_connect"

            if not errors:
                # Use the entry_id from the flow context (HA best practice for reauth).
                entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                assert entry is not None
                updated_data = dict(entry.data)
                updated_data.update(new_data)

                # Update credentials (data only), reload, and abort with success reason.
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


class OptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Options flow to (a) update non-secret settings and (b) optionally refresh credentials.

    Notes:
        - Do not assign self.config_entry here; the base class provides it.
        - OptionsFlowWithReload will reload the integration when options change
          if the flow ends with async_create_entry(data=...).
        - Do not update the config entry inside the options flow; the base class handles reloads.
    """

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
        """Update non-secret options.

        Best practice:
            - Return async_create_entry(data=options) to commit options.
            - OptionsFlowWithReload will reload the integration automatically.
            - Do not call hass.config_entries.async_update_entry() from inside the flow.
        """
        errors: Dict[str, str] = {}

        entry = self.config_entry
        opt = entry.options
        dat = entry.data

        # Options-first with safe fallbacks
        current_tracked = opt.get(OPT_TRACKED_DEVICES, dat.get(OPT_TRACKED_DEVICES, []))
        current_interval = opt.get(OPT_LOCATION_POLL_INTERVAL, dat.get(OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL))
        current_delay = opt.get(OPT_DEVICE_POLL_DELAY, dat.get(OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY))
        current_min_acc = opt.get(OPT_MIN_ACCURACY_THRESHOLD, dat.get(OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD))
        current_move_thr = opt.get(OPT_MOVEMENT_THRESHOLD, dat.get(OPT_MOVEMENT_THRESHOLD, DEFAULT_MOVEMENT_THRESHOLD))
        current_gh_enabled = opt.get(
            OPT_GOOGLE_HOME_FILTER_ENABLED, dat.get(OPT_GOOGLE_HOME_FILTER_ENABLED, DEFAULT_GOOGLE_HOME_FILTER_ENABLED)
        )
        current_gh_keywords = opt.get(
            OPT_GOOGLE_HOME_FILTER_KEYWORDS, dat.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS)
        )
        current_stats = opt.get(OPT_ENABLE_STATS_ENTITIES, dat.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES))
        current_map_token_exp = opt.get(
            OPT_MAP_VIEW_TOKEN_EXPIRATION, dat.get(OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION)
        )

        # Build device list (best-effort; do not fail the form)
        device_options: Dict[str, str] = {}
        try:
            api = await self._async_build_api_from_entry(entry)
            devices = await api.async_get_basic_device_list(entry.data.get(CONF_GOOGLE_EMAIL))
            device_options = {dev["id"]: dev["name"] for dev in devices}
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to fetch device list for options: %s", err)

        # Define the base schema without defaults and inject suggested values dynamically.
        base_schema = vol.Schema(
            {
                vol.Optional(OPT_TRACKED_DEVICES): vol.All(
                    cv.multi_select(device_options), vol.Length(min=0)
                ),
                vol.Optional(OPT_LOCATION_POLL_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=60, max=3600)
                ),
                vol.Optional(OPT_DEVICE_POLL_DELAY): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=60)
                ),
                vol.Optional(OPT_MIN_ACCURACY_THRESHOLD): vol.All(
                    vol.Coerce(int), vol.Range(min=25, max=500)
                ),
                vol.Optional(OPT_MOVEMENT_THRESHOLD): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=200)
                ),
                vol.Optional(OPT_GOOGLE_HOME_FILTER_ENABLED): bool,
                vol.Optional(OPT_GOOGLE_HOME_FILTER_KEYWORDS): str,
                vol.Optional(OPT_ENABLE_STATS_ENTITIES): bool,
                vol.Optional(OPT_MAP_VIEW_TOKEN_EXPIRATION): bool,
            }
        )

        if user_input is not None:
            new_options = {
                OPT_TRACKED_DEVICES: user_input.get(OPT_TRACKED_DEVICES, current_tracked),
                OPT_LOCATION_POLL_INTERVAL: user_input.get(OPT_LOCATION_POLL_INTERVAL, current_interval),
                OPT_DEVICE_POLL_DELAY: user_input.get(OPT_DEVICE_POLL_DELAY, current_delay),
                OPT_MIN_ACCURACY_THRESHOLD: user_input.get(OPT_MIN_ACCURACY_THRESHOLD, current_min_acc),
                OPT_MOVEMENT_THRESHOLD: user_input.get(OPT_MOVEMENT_THRESHOLD, current_move_thr),
                OPT_GOOGLE_HOME_FILTER_ENABLED: user_input.get(OPT_GOOGLE_HOME_FILTER_ENABLED, current_gh_enabled),
                OPT_GOOGLE_HOME_FILTER_KEYWORDS: user_input.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, current_gh_keywords),
                OPT_ENABLE_STATS_ENTITIES: user_input.get(OPT_ENABLE_STATS_ENTITIES, current_stats),
                OPT_MAP_VIEW_TOKEN_EXPIRATION: user_input.get(OPT_MAP_VIEW_TOKEN_EXPIRATION, current_map_token_exp),
            }

            # Commit options and trigger automatic reload via OptionsFlowWithReload.
            return self.async_create_entry(title="", data=new_options)

        # Inject suggested/current values for a clean, static schema.
        suggested_values = {
            OPT_TRACKED_DEVICES: current_tracked,
            OPT_LOCATION_POLL_INTERVAL: current_interval,
            OPT_DEVICE_POLL_DELAY: current_delay,
            OPT_MIN_ACCURACY_THRESHOLD: current_min_acc,
            OPT_MOVEMENT_THRESHOLD: current_move_thr,
            OPT_GOOGLE_HOME_FILTER_ENABLED: current_gh_enabled,
            OPT_GOOGLE_HOME_FILTER_KEYWORDS: current_gh_keywords,
            OPT_ENABLE_STATS_ENTITIES: current_stats,
            OPT_MAP_VIEW_TOKEN_EXPIRATION: current_map_token_exp,
        }

        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(base_schema, suggested_values),
            errors=errors,
        )

    # ---------- Credentials update (always-empty fields) ----------
    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Allow refreshing credentials without exposing current values."""
        errors: Dict[str, str] = {}

        schema = vol.Schema(
            {
                vol.Optional("secrets_json"): str,
                vol.Optional(CONF_OAUTH_TOKEN): str,
                vol.Optional(CONF_GOOGLE_EMAIL): str,
            }
        )

        if user_input is not None:
            secrets_json = user_input.get("secrets_json")
            oauth_token = user_input.get(CONF_OAUTH_TOKEN)
            google_email = user_input.get(CONF_GOOGLE_EMAIL)

            new_data: Dict[str, Any] = {}
            try:
                if secrets_json:
                    parsed = json.loads(secrets_json)
                    new_data = {DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS, DATA_SECRET_BUNDLE: parsed}
                    api = GoogleFindMyAPI(secrets_data=parsed)
                    _ = await api.async_get_basic_device_list(
                        parsed.get("googleHomeUsername") or parsed.get("google_email")
                    )  # validation
                elif oauth_token and google_email:
                    new_data = {
                        DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                        CONF_OAUTH_TOKEN: oauth_token,
                        CONF_GOOGLE_EMAIL: google_email,
                    }
                    api = GoogleFindMyAPI(oauth_token=oauth_token, google_email=google_email)
                    _ = await api.async_get_basic_device_list(google_email)  # validation
                else:
                    errors["base"] = "choose_one"

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

        return self.async_show_form(step_id="credentials", data_schema=schema, errors=errors)

    # ---------- Internal helper ----------
    async def _async_build_api_from_entry(self, entry: ConfigEntry) -> GoogleFindMyAPI:
        """Construct API object from entry data (supports both auth methods)."""
        if entry.data.get(DATA_AUTH_METHOD) == _AUTH_METHOD_SECRETS:
            return GoogleFindMyAPI(secrets_data=entry.data.get(DATA_SECRET_BUNDLE))
        return GoogleFindMyAPI(
            oauth_token=entry.data.get(CONF_OAUTH_TOKEN),
            google_email=entry.data.get(CONF_GOOGLE_EMAIL),
        )


# ---------- Custom exceptions ----------
class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
