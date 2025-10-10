"""Config flow for Google Find My Device (custom integration)."""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

# Defensive import of selector (older HA versions may not expose it)
try:
    from homeassistant.helpers.selector import selector
except Exception:  # noqa: BLE001
    selector = None  # type: ignore[assignment]

from .api import GoogleFindMyAPI
from .const import (
    # Core
    DOMAIN,
    # Data (credentials, immutable)
    CONF_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,  # kept for compatibility in translations; not stored anymore
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


def _extract_email_from_secrets(data: Dict[str, Any]) -> Optional[str]:
    """Best-effort extractor for the Google account email from secrets.json."""
    candidates = [
        "googleHomeUsername",
        CONF_GOOGLE_EMAIL,
        "google_email",
        "email",
        "username",
        "user",
    ]
    for key in candidates:
        val = data.get(key)
        if isinstance(val, str) and "@" in val:
            return val
    return None


def _extract_oauth_from_secrets(data: Dict[str, Any]) -> Optional[str]:
    """Best-effort extractor for an OAuth token from secrets.json."""
    candidates = [
        CONF_OAUTH_TOKEN,
        "oauthToken",
        "oauth_token",
        "OAuthToken",
        "oauth",
        "token",
        "access_token",
        "adm_token",
        "admToken",
        "Auth",  # sometimes present in gpsoauth responses
    ]
    for key in candidates:
        val = data.get(key)
        if isinstance(val, str) and len(val) > 10:
            return val
    return None


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
        """Create the options flow."""
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

                email = _extract_email_from_secrets(secrets_data) or ""
                oauth = _extract_oauth_from_secrets(secrets_data) or ""
                if not (email and oauth):
                    errors["base"] = "invalid_token"
                else:
                    # Store only minimal credentials in memory for the next step.
                    self._auth_data = {
                        DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                        CONF_OAUTH_TOKEN: oauth,
                        CONF_GOOGLE_EMAIL: email,
                        # Keep the bundle transiently so translations/placeholders work in this flow.
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
        """Build API instance for setup using minimal credentials."""
        # During initial setup there is no entry-scoped cache yet,
        # so we instantiate the API with minimal flow credentials.
        email = self._auth_data.get(CONF_GOOGLE_EMAIL)
        oauth = self._auth_data.get(CONF_OAUTH_TOKEN)

        if not (email and oauth):
            raise HomeAssistantError("Missing credentials in setup flow.")

        api = GoogleFindMyAPI(oauth_token=oauth, google_email=email)
        return api, email

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

        # Build a multi-select; keep cv.multi_select for wide compatibility
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
            # Data (credentials). Store only minimal credentials.
            data_payload: Dict[str, Any] = {
                DATA_AUTH_METHOD: self._auth_data.get(DATA_AUTH_METHOD),
                CONF_OAUTH_TOKEN: self._auth_data.get(CONF_OAUTH_TOKEN),
                CONF_GOOGLE_EMAIL: self._auth_data.get(CONF_GOOGLE_EMAIL),
            }

            # Options
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

            # Prefer modern HA that supports options at create time; fallback to data-only.
            try:
                return self.async_create_entry(
                    title="Google Find My Device",
                    data=data_payload,
                    options=options_payload,  # type: ignore[call-arg]
                )
            except TypeError:
                shadow = dict(data_payload)
                shadow.update(options_payload)
                return self.async_create_entry(title="Google Find My Device", data=shadow)

        return self.async_show_form(step_id="device_selection", data_schema=schema)

    # ---------- Reauth ----------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauthentication flow with context."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect new credentials for reauth and validate them."""
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
                    email = _extract_email_from_secrets(parsed)
                    oauth = _extract_oauth_from_secrets(parsed)
                    if not (email and oauth):
                        errors["base"] = "invalid_token"
                    else:
                        new_data = {DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS, CONF_OAUTH_TOKEN: oauth, CONF_GOOGLE_EMAIL: email}
                        api = GoogleFindMyAPI(oauth_token=oauth, google_email=email)
                        _ = await api.async_get_basic_device_list(email)
                else:
                    if not (oauth_token and google_email):
                        errors["base"] = "invalid_token"
                    else:
                        new_data = {
                            DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                            CONF_OAUTH_TOKEN: oauth_token,
                            CONF_GOOGLE_EMAIL: google_email,
                        }
                        api = GoogleFindMyAPI(oauth_token=oauth_token, google_email=google_email)
                        _ = await api.async_get_basic_device_list(google_email)

            except json.JSONDecodeError:
                errors["base"] = "invalid_json"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Reauth validation failed: %s", err)
                errors["base"] = "cannot_connect"

            if not errors:
                entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                assert entry is not None
                updated_data = dict(entry.data)
                updated_data.update(new_data)

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
    """Options flow to update non-secret settings and optionally refresh credentials."""

    # ---------- Menu entry ----------
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show a small menu: edit settings vs. update credentials."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "credentials"],
        )

    # ---------- Helpers to access live cache/API ----------
    def _get_entry_cache(self, entry: ConfigEntry):
        """Return the TokenCache for this entry if available."""
        data = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if isinstance(data, dict) and "cache" in data:
            return data["cache"]
        # Fallback: runtime_data may carry the cache
        return getattr(entry, "runtime_data", None)

    async def _async_build_api_from_entry(self, entry: ConfigEntry) -> GoogleFindMyAPI:
        """Construct API object from the live entry context (cache-first)."""
        cache = self._get_entry_cache(entry)
        if cache is not None:
            session = async_get_clientsession(self.hass)
            return GoogleFindMyAPI(cache=cache, session=session)

        # Last resort: try minimal credentials from entry.data to keep Options usable.
        oauth = entry.data.get(CONF_OAUTH_TOKEN)
        email = entry.data.get(CONF_GOOGLE_EMAIL)
        if oauth and email:
            return GoogleFindMyAPI(oauth_token=oauth, google_email=email)

        # If neither cache nor credentials are available, surface a clear warning.
        raise RuntimeError(
            "GoogleFindMyAPI requires either `cache=` or minimal flow credentials (`oauth_token`/`google_email`)."
        )

    # ---------- Settings (non-secret) ----------
    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Update non-secret options."""
        errors: Dict[str, str] = {}

        entry = self.config_entry
        opt = entry.options
        dat = entry.data

        # Current values with safe fallbacks
        current_tracked = opt.get(OPT_TRACKED_DEVICES, dat.get(OPT_TRACKED_DEVICES, []))
        current_interval = opt.get(OPT_LOCATION_POLL_INTERVAL, dat.get(OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL))
        current_delay = opt.get(OPT_DEVICE_POLL_DELAY, dat.get(OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY))
        current_min_acc = opt.get(OPT_MIN_ACCURACY_THRESHOLD, dat.get(OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD))
        current_move_thr = opt.get(OPT_MOVEMENT_THRESHOLD, dat.get(OPT_MOVEMENT_THRESHOLD, DEFAULT_MOVEMENT_THRESHOLD))
        current_gh_enabled = opt.get(OPT_GOOGLE_HOME_FILTER_ENABLED, dat.get(OPT_GOOGLE_HOME_FILTER_ENABLED, DEFAULT_GOOGLE_HOME_FILTER_ENABLED))
        current_gh_keywords = opt.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, dat.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS))
        current_stats = opt.get(OPT_ENABLE_STATS_ENTITIES, dat.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES))
        current_map_token_exp = opt.get(OPT_MAP_VIEW_TOKEN_EXPIRATION, dat.get(OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION))

        # Build device options (robust against temporary API failures)
        device_options: Dict[str, str] = {}
        try:
            api = await self._async_build_api_from_entry(entry)
            devices = await api.async_get_basic_device_list(entry.data.get(CONF_GOOGLE_EMAIL))
            device_options = {dev["id"]: dev["name"] for dev in devices}
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not fetch a fresh device list for options; using existing tracked devices as fallback. Error: %s",
                err,
            )

        # Ensure already-tracked IDs remain valid choices even if fetch failed
        for dev_id in current_tracked or []:
            device_options.setdefault(dev_id, dev_id)

        # Base schema without defaults; suggested values will be injected
        base_schema = vol.Schema(
            {
                vol.Optional(OPT_TRACKED_DEVICES): vol.All(cv.multi_select(device_options), vol.Length(min=0)),
                vol.Optional(OPT_LOCATION_POLL_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
                vol.Optional(OPT_DEVICE_POLL_DELAY): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
                vol.Optional(OPT_MIN_ACCURACY_THRESHOLD): vol.All(vol.Coerce(int), vol.Range(min=25, max=500)),
                vol.Optional(OPT_MOVEMENT_THRESHOLD): vol.All(vol.Coerce(int), vol.Range(min=10, max=200)),
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
                vol.Optional("new_secrets_json"): str,
                vol.Optional("new_oauth_token"): str,
                vol.Optional("new_google_email"): str,
            }
        )

        if user_input is not None:
            secrets_json = user_input.get("new_secrets_json")
            oauth_token = user_input.get("new_oauth_token")
            google_email = user_input.get("new_google_email")

            new_data: Dict[str, Any] = {}
            try:
                if secrets_json:
                    parsed = json.loads(secrets_json)
                    email = _extract_email_from_secrets(parsed)
                    oauth = _extract_oauth_from_secrets(parsed)
                    if not (email and oauth):
                        errors["base"] = "invalid_token"
                    else:
                        new_data = {DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS, CONF_OAUTH_TOKEN: oauth, CONF_GOOGLE_EMAIL: email}
                        api = GoogleFindMyAPI(oauth_token=oauth, google_email=email)
                        _ = await api.async_get_basic_device_list(email)  # validation
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

                    # Update credentials in data only
                    self.hass.config_entries.async_update_entry(entry, data=updated_data)
                    # Reload to apply immediately
                    self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
                    return self.async_abort(reason="reconfigure_successful")
            except json.JSONDecodeError:
                errors["base"] = "invalid_json"
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Credentials update failed: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(step_id="credentials", data_schema=schema, errors=errors)


# ---------- Custom exceptions ----------
class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
