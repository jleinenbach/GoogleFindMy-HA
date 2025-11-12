# custom_components/googlefindmy/config_flow.py
"""Config flow for Google Find My Device (custom integration).

Invariants (why this looks the way it does):
- Authentication requires a full `secrets.json` file from GoogleFindMyTools.
- We distinguish syntax errors (`invalid_json`) from missing/invalid content
  (`invalid_token`) to give precise feedback.
- We use a multiline selector for `secrets_json` where available to reduce
  paste truncation issues.
- We set a unique config-entry identifier (`DOMAIN:email`) to prevent duplicate
  setups for the same Google account (quality-scale rule: unique-config-entry).
- We prefer `entry.runtime_data` over `hass.data` for runtime objects and avoid
  logging secrets.
- For `secrets.json`, we support multiple token sources and **fail over**:
  prefer `aas_token`, then `fcm_credentials.installation.token`, then legacy keys.

Change (Step 1): Remove legacy `tracked_devices` UI from both the initial setup
and the options flow, without altering authentication behaviour or the online
connection test. Device inclusion/exclusion will be handled by native HA device
enable/disable in later steps.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import device_registry as dr

# Defensive import of selector (older HA versions may not expose it)
try:  # pragma: no cover - import environment detail
    from homeassistant.helpers.selector import selector
except ImportError:  # noqa: F401 - broad env compatibility
    selector = None  # type: ignore[assignment]

from .api import GoogleFindMyAPI
from .Auth.token_cache import _register_instance, _unregister_instance, _set_default_entry_id
from .const import (
    # Core
    DOMAIN,
    # Data (credentials, immutable)
    CONF_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,  # kept for compatibility in translations; not stored anymore
    DATA_AUTH_METHOD,
    # Options (user-changeable)
    # OPT_TRACKED_DEVICES,  # (removed from UI in Step 1; left import commented for clarity)
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MOVEMENT_THRESHOLD,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_IGNORED_DEVICES,  # visibility management
    # Defaults
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MOVEMENT_THRESHOLD,
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    DEFAULT_ENABLE_STATS_ENTITIES,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DEFAULT_OPTIONS,
    OPT_OPTIONS_SCHEMA_VERSION,
    coerce_ignored_mapping,
    ignored_choices_for_ui,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------
# Validators (format/plausibility)
# ---------------------------
# RFC5322-ish but pragmatic email check (must have at least one dot in domain)
_EMAIL_RE = re.compile(
    r"^(?=.{3,254}$)[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$"
)
# Pragmatic token plausibility: no whitespace, at least 16 chars
_TOKEN_RE = re.compile(r"^\S{16,}$")


def _email_valid(value: str) -> bool:
    """Return True if value looks like a real email address."""
    return bool(_EMAIL_RE.match(value or ""))


def _token_plausible(value: str) -> bool:
    """Return True if value looks like a token (no spaces, long enough)."""
    return bool(_TOKEN_RE.match(value or ""))


# ---------------------------
# Auth method constants
# ---------------------------
_AUTH_METHOD_SECRETS = "secrets_json"

# Base schema for secrets.json step (fallback when selector is unavailable)
STEP_SECRETS_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(
            "secrets_json",
            description="Paste the complete contents of your secrets.json file",
        ): str
    }
)

# ---------------------------
# Extractors (email + tokens with preference & failover list)
# ---------------------------
def _extract_fcm_credentials_from_secrets(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract fcm_credentials from secrets.json if present.

    Returns:
        The fcm_credentials dict, or None if not found.
    """
    try:
        fcm_creds = data.get("fcm_credentials")
        if isinstance(fcm_creds, dict):
            return fcm_creds
    except Exception:  # noqa: BLE001
        pass
    return None


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

    # Nested fallback shapes (defensive)
    try:
        val = data["account"]["email"]
        if isinstance(val, str) and "@" in val:
            return val
    except Exception:
        pass
    return None


def _extract_oauth_candidates_from_secrets(data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return plausible tokens in order of preference from a secrets bundle.

    The order encodes our failover strategy:
    1) 'aas_token' (Account Authentication Service token) — preferred
    2) 'fcm_credentials.installation.token' — FCM installation JWT
    3) 'fcm_credentials.fcm.registration.token' — FCM registration token (rare)
    4) Legacy/generic flat keys such as 'oauth_token', 'access_token', etc.
    """
    cands: List[Tuple[str, str]] = []

    # 1) AAS token (preferred)
    t = data.get("aas_token")
    if isinstance(t, str) and _token_plausible(t):
        cands.append(("aas_token", t))

    # 2) FCM Installation JWT (preferred fallback)
    try:
        t2 = data["fcm_credentials"]["installation"]["token"]
        if isinstance(t2, str) and _token_plausible(t2):
            cands.append(("fcm_installation", t2))
    except Exception:
        pass

    # 3) FCM registration token (rarely useful for FMD; lowest priority but keep as last resort)
    try:
        t3 = data["fcm_credentials"]["fcm"]["registration"]["token"]
        if isinstance(t3, str) and _token_plausible(t3):
            cands.append(("fcm_registration", t3))
    except Exception:
        pass

    # 4) Legacy / generic flat keys (very last)
    for key in (
        CONF_OAUTH_TOKEN,
        "oauthToken",
        "OAuthToken",
        "token",
        "access_token",
        "adm_token",
        "admToken",
        "Auth",
    ):
        v = data.get(key)
        if isinstance(v, str) and _token_plausible(v):
            cands.append((key, v))

    return cands


def _extract_oauth_from_secrets(data: Dict[str, Any]) -> Optional[str]:
    """Return a single selected token from secrets.json using preferred order."""
    cands = _extract_oauth_candidates_from_secrets(data)
    if not cands:
        return None
    try:
        _LOGGER.debug("Choosing OAuth token from secrets source: %s", cands[0][0])
    except Exception:
        pass
    return cands[0][1]


async def async_pick_working_token(
    email: str,
    candidates: List[Tuple[str, str]],
    secrets_data: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[str, str]]:
    """Try tokens in order until one passes a minimal online validation.

    This performs a very lightweight API call (`async_get_basic_device_list`)
    to verify the token works for the given Google account.

    Args:
        email: Google account email.
        candidates: List of (source_name, token) tuples to try.
        secrets_data: Optional parsed secrets.json dict (for extracting android_id).

    Returns:
        Tuple of (source_name, token) if validation succeeds, None otherwise.
    """
    # Extract fcm_credentials from secrets if available (contains android_id)
    fcm_creds = None
    if isinstance(secrets_data, dict):
        fcm_creds = _extract_fcm_credentials_from_secrets(secrets_data)

    for source, token in candidates:
        try:
            # Pass token to correct parameter based on source type
            # If source is "aas_token", it's already an AAS token, not an OAuth token
            api = GoogleFindMyAPI(
                oauth_token=token if source != "aas_token" else None,
                google_email=email,
                fcm_credentials=fcm_creds,
                aas_token=token if source == "aas_token" else None,
            )

            # Temporarily register the ephemeral cache to allow token generation during validation
            # when multiple entries are active. Use a unique temporary entry_id.
            temp_entry_id = f"__validation_{id(api)}"
            _register_instance(temp_entry_id, api._cache)  # type: ignore[arg-type]
            _set_default_entry_id(temp_entry_id, force=True)

            try:
                await api.async_get_basic_device_list(email)
                _LOGGER.debug("Token from '%s' validated successfully.", source)
                return (source, token)
            finally:
                # Clean up temporary registration
                _unregister_instance(temp_entry_id)

        except Exception as err:  # noqa: BLE001 - network/auth errors
            _LOGGER.warning("Token from '%s' failed validation: %s", source, err)
            continue
    return None


# ---------------------------
# Shared interpreters (Either/Or choice handling)
# ---------------------------
def _interpret_credentials_choice(
    user_input: Dict[str, Any],
    *,
    secrets_field: str,
    token_field: str,
    email_field: str,
) -> Tuple[Optional[str], Optional[str], Optional[List[Tuple[str, str]]], Optional[str]]:
    """Interpret input into exactly-one-method choice.

    Returns:
        tuple(method, email, token_candidates, error_key)
          - method: "secrets" | "manual" | None
          - email: parsed/entered email (for manual or from secrets)
          - token_candidates: list of (source_name, token) in order of preference
          - error_key: translation key if an immediate input error is detected

    Notes:
        - Field-level errors are decided by the caller (e.g., to attach invalid_json to the
          secrets field specifically); this function only returns the canonical error key.
    """
    secrets_json = (user_input.get(secrets_field) or "").strip()
    oauth_token = (user_input.get(token_field) or "").strip()
    google_email = (user_input.get(email_field) or "").strip()

    has_secrets = bool(secrets_json)
    has_token = bool(oauth_token)
    has_email = bool(google_email)

    # Mixing methods is not allowed
    if has_secrets and (has_token or has_email):
        return None, None, None, "choose_one"

    # Neither provided
    if not has_secrets and not (has_token or has_email):
        return None, None, None, "choose_one"

    # Secrets path
    if has_secrets:
        try:
            parsed = json.loads(secrets_json)
            if not isinstance(parsed, dict):
                raise TypeError()
        except (json.JSONDecodeError, TypeError):
            return "secrets", None, None, "invalid_json"

        email = _extract_email_from_secrets(parsed) or ""
        cands = _extract_oauth_candidates_from_secrets(parsed)
        if not (_email_valid(email) and cands):
            return "secrets", None, None, "invalid_token"
        return "secrets", email, cands, None

    # Manual path (must have both)
    if not (has_token and has_email):
        # Partial manual input
        return None, None, None, "choose_one"

    if not (_email_valid(google_email) and _token_plausible(oauth_token)):
        return "manual", None, None, "invalid_token"

    return "manual", google_email, [("manual", oauth_token)], None


# ---------------------------
# Config flow
# ---------------------------
class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow for Google Find My Device."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize transient state for the flow."""
        self._auth_data: Dict[str, Any] = {}
        self._available_devices: List[Tuple[str, str]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        """Create the options flow (HA injects the config entry automatically)."""
        return OptionsFlowHandler()

    # ---------- User entry point ----------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Entry step: collect secrets.json."""
        return await self.async_step_secrets_json(user_input)

    # ---------- Secrets.json path ----------
    async def async_step_secrets_json(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect and validate secrets.json content (with online validation + failover).

        Invariant:
            This step expects a *full* secrets.json (valid JSON object) that contains both
            an email address and at least one plausible token candidate. We never log secrets.
        """
        errors: Dict[str, str] = {}

        # Use multiline text input for secrets.json to improve UX
        schema = STEP_SECRETS_DATA_SCHEMA
        if selector is not None:
            schema = vol.Schema({vol.Required("secrets_json"): selector({"text": {"multiline": True}})})

        if user_input is not None:
            method, email, cands, err = _interpret_credentials_choice(
                user_input, secrets_field="secrets_json", token_field=CONF_OAUTH_TOKEN, email_field=CONF_GOOGLE_EMAIL
            )
            if err:
                # Attach invalid_json to the field to provide precise feedback
                if err == "invalid_json":
                    errors["secrets_json"] = "invalid_json"
                else:
                    errors["base"] = err
            else:
                assert method == "secrets" and email and cands
                # Parse secrets.json for passing to validation
                parsed_secrets = json.loads(user_input.get("secrets_json") or "{}")
                result = await async_pick_working_token(email, cands, parsed_secrets)
                if not result:
                    # Syntax was OK but none of the candidates worked online
                    errors["base"] = "cannot_connect"
                else:
                    # Unpack source and token
                    token_source, chosen_token = result
                    # Store only minimal credentials transiently for next step
                    self._auth_data = {
                        DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                        CONF_OAUTH_TOKEN: chosen_token,
                        CONF_GOOGLE_EMAIL: email,
                        # Keep the original bundle transiently (not stored in entry)
                        DATA_SECRET_BUNDLE: parsed_secrets,
                        # Store token source so we know how to use it in device_selection
                        "token_source": token_source,
                    }
                    return await self.async_step_device_selection()

        return self.async_show_form(step_id="secrets_json", data_schema=schema, errors=errors)

    # ---------- Shared helper to create API from stored auth_data ----------
    async def _async_build_api_and_username(self) -> Tuple[GoogleFindMyAPI, Optional[str]]:
        """Build API instance for setup using minimal credentials."""
        email = self._auth_data.get(CONF_GOOGLE_EMAIL)
        token = self._auth_data.get(CONF_OAUTH_TOKEN)
        token_source = self._auth_data.get("token_source")  # May be None for manual path

        if not (email and token):
            raise HomeAssistantError("Missing credentials in setup flow.")

        # Extract fcm_credentials from secrets bundle if available (contains android_id)
        fcm_creds = None
        secret_bundle = self._auth_data.get(DATA_SECRET_BUNDLE)
        if isinstance(secret_bundle, dict):
            fcm_creds = _extract_fcm_credentials_from_secrets(secret_bundle)

        # Pass token to correct parameter based on source type
        # If source is "aas_token", it's already an AAS token, not an OAuth token
        api = GoogleFindMyAPI(
            oauth_token=token if token_source != "aas_token" else None,
            google_email=email,
            fcm_credentials=fcm_creds,
            aas_token=token if token_source == "aas_token" else None,
        )
        return api, email

    # ---------- Device selection (now: connection test + non-secret settings) ----------
    async def async_step_device_selection(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Finalize initial setup.

        Step 1 change: This form no longer contains the `tracked_devices` multi-select.
        We keep the **online validation** (device list fetch) and the remaining options.
        """
        errors: Dict[str, str] = {}

        # Ensure unique_id per Google account to avoid duplicate entries
        email_for_uid = (self._auth_data.get(CONF_GOOGLE_EMAIL) or "").strip().lower()
        if email_for_uid:
            await self.async_set_unique_id(f"{DOMAIN}:{email_for_uid}")
            self._abort_if_unique_id_configured()

        # Online validation: fetch devices once (fail → cannot_connect)
        if not self._available_devices:
            try:
                api, username = await self._async_build_api_and_username()

                # Temporarily register the ephemeral cache to allow token generation during validation
                # when multiple entries are active. Use a unique temporary entry_id.
                temp_entry_id = f"__validation_{id(api)}"
                _register_instance(temp_entry_id, api._cache)  # type: ignore[arg-type]
                _set_default_entry_id(temp_entry_id, force=True)

                try:
                    devices = await api.async_get_basic_device_list(username)
                    if not devices:
                        errors["base"] = "no_devices"
                    else:
                        # store as (name, id) for potential future use (kept for parity)
                        self._available_devices = [(d["name"], d["id"]) for d in devices]
                finally:
                    # Clean up temporary registration
                    _unregister_instance(temp_entry_id)

            except Exception as err:  # noqa: BLE001 - API/transport errors
                _LOGGER.error("Failed to fetch devices during setup: %s", err)
                errors["base"] = "cannot_connect"

        if errors:
            # Keep the step to show an error, but with an empty schema.
            return self.async_show_form(step_id="device_selection", data_schema=vol.Schema({}), errors=errors)

        # Schema **without** OPT_TRACKED_DEVICES (removed in Step 1)
        schema = vol.Schema(
            {
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

            # Store secrets bundle for runtime access to fcm_credentials and android_id
            if self._auth_data.get(DATA_SECRET_BUNDLE):
                data_payload[DATA_SECRET_BUNDLE] = self._auth_data[DATA_SECRET_BUNDLE]

            # Store token source so runtime knows how to use the token
            if self._auth_data.get("token_source"):
                data_payload["token_source"] = self._auth_data["token_source"]

            # Options (no OPT_TRACKED_DEVICES anymore)
            options_payload: Dict[str, Any] = {
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

            # Use email in title for multi-account support
            google_email = self._auth_data.get(CONF_GOOGLE_EMAIL, "Unknown")
            entry_title = f"Google Find My Device ({google_email})"

            # Prefer modern HA that supports options at create time; fallback to data-only.
            try:
                return self.async_create_entry(
                    title=entry_title,
                    data=data_payload,
                    options=options_payload,  # type: ignore[call-arg]
                )
            except TypeError:
                shadow = dict(data_payload)
                shadow.update(options_payload)
                return self.async_create_entry(title=entry_title, data=shadow)

        return self.async_show_form(step_id="device_selection", data_schema=schema)

    # ---------- Reauth ----------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauthentication flow with context."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect new credentials for reauth and validate them."""
        errors: Dict[str, str] = {}

        schema: vol.Schema
        if selector is not None:
            schema = vol.Schema(
                {
                    vol.Required("secrets_json"): selector({"text": {"multiline": True}}),
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required("secrets_json"): str,
                }
            )

        if user_input is not None:
            method, email, cands, err = _interpret_credentials_choice(
                user_input, secrets_field="secrets_json", token_field=CONF_OAUTH_TOKEN, email_field=CONF_GOOGLE_EMAIL
            )
            if err:
                if err == "invalid_json":
                    errors["secrets_json"] = "invalid_json"
                else:
                    errors["base"] = err
            else:
                try:
                    assert method == "secrets" and email and cands
                    # Parse secrets.json for passing to validation
                    parsed_secrets = json.loads(user_input.get("secrets_json") or "{}")
                    result = await async_pick_working_token(email, cands, parsed_secrets)
                    if not result:
                        errors["base"] = "cannot_connect"
                    else:
                        token_source, chosen_token = result
                        new_data = {
                            DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                            CONF_OAUTH_TOKEN: chosen_token,
                            CONF_GOOGLE_EMAIL: email,
                            DATA_SECRET_BUNDLE: parsed_secrets,
                            "token_source": token_source,
                        }

                        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                        assert entry is not None
                        updated_data = dict(entry.data)
                        updated_data.update(new_data)

                        return self.async_update_reload_and_abort(
                            entry=entry,
                            data=updated_data,
                            reason="reauth_successful",
                        )
                except Exception as err2:  # noqa: BLE001
                    _LOGGER.error("Reauth validation failed: %s", err2)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)


class OptionsFlowHandler(config_entries.OptionsFlowWithReload):
    """Options flow to update non-secret settings and optionally refresh credentials.

    NOTE: Step 1 change: `tracked_devices` has been removed from the Settings UI.
    Home Assistant's native device enable/disable will control tracking going forward.
    """

    # ---------- Menu entry ----------
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show a small menu: edit settings vs. update credentials vs. visibility."""
        # Show which account is being configured to prevent confusion
        current_email = self.config_entry.data.get(CONF_GOOGLE_EMAIL, "Unknown")

        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "credentials", "visibility"],
            description_placeholders={"account_email": current_email},
        )

    # ---------- Helpers to access live cache/API ----------
    def _get_entry_cache(self, entry: ConfigEntry) -> Optional[Any]:
        """Return the TokenCache (or equivalent) for this entry if available.

        We prefer `entry.runtime_data` (modern pattern) and fall back to
        `hass.data[DOMAIN][entry_id]`. We avoid assuming a specific concrete
        cache class and never log secrets.
        """
        # Prefer runtime_data (Best Practice)
        rd = getattr(entry, "runtime_data", None)
        if rd is not None and hasattr(rd, "_cache"):
            try:
                return getattr(rd, "_cache")
            except Exception:  # pragma: no cover - defensive
                pass

        # Fallback to hass.data
        data = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if data is not None and hasattr(data, "_cache"):
            try:
                return getattr(data, "_cache")
            except Exception:  # pragma: no cover - defensive
                pass
        if isinstance(data, dict) and "cache" in data:
            return data["cache"]

        return None

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
        """Update non-secret options (without `tracked_devices`)."""
        errors: Dict[str, str] = {}

        entry = self.config_entry
        opt = entry.options
        dat = entry.data

        # Current values with safe fallbacks (tracked_devices removed in Step 1)
        current_interval = opt.get(
            OPT_LOCATION_POLL_INTERVAL,
            dat.get(OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL),
        )
        current_delay = opt.get(OPT_DEVICE_POLL_DELAY, dat.get(OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY))
        current_min_acc = opt.get(
            OPT_MIN_ACCURACY_THRESHOLD,
            dat.get(OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD),
        )
        current_move_thr = opt.get(OPT_MOVEMENT_THRESHOLD, dat.get(OPT_MOVEMENT_THRESHOLD, DEFAULT_MOVEMENT_THRESHOLD))
        current_gh_enabled = opt.get(
            OPT_GOOGLE_HOME_FILTER_ENABLED,
            dat.get(OPT_GOOGLE_HOME_FILTER_ENABLED, DEFAULT_GOOGLE_HOME_FILTER_ENABLED),
        )
        current_gh_keywords = opt.get(
            OPT_GOOGLE_HOME_FILTER_KEYWORDS,
            dat.get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS),
        )
        current_stats = opt.get(OPT_ENABLE_STATS_ENTITIES, dat.get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES))
        current_map_token_exp = opt.get(
            OPT_MAP_VIEW_TOKEN_EXPIRATION,
            dat.get(OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION),
        )

        # Base schema *without* tracked_devices
        base_schema = vol.Schema(
            {
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

    # ---------- Device visibility (restore ignored devices) ----------
    async def async_step_visibility(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show and restore ignored devices by removing them from OPT_IGNORED_DEVICES."""
        entry = self.config_entry
        options = dict(entry.options)
        raw = options.get(OPT_IGNORED_DEVICES) or entry.data.get(OPT_IGNORED_DEVICES) or {}
        ignored_map, _migrated = coerce_ignored_mapping(raw)

        # Abort early if nothing to restore
        if not ignored_map:
            return self.async_abort(reason="no_ignored_devices")

        # Directly use stored names for choices
        choices = ignored_choices_for_ui(ignored_map)

        schema = vol.Schema({vol.Optional("unignore_devices", default=[]): cv.multi_select(choices)})

        if user_input is not None:
            to_restore = user_input.get("unignore_devices") or []
            if not isinstance(to_restore, list):
                to_restore = list(to_restore)  # in case of set/tuple

            # Remove selected ids from the mapping
            for dev_id in to_restore:
                ignored_map.pop(dev_id, None)

            new_options = dict(entry.options)
            new_options[OPT_IGNORED_DEVICES] = ignored_map
            new_options[OPT_OPTIONS_SCHEMA_VERSION] = 2

            # Trigger automatic reload via OptionsFlowWithReload by returning a create_entry result.
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(step_id="visibility", data_schema=schema)

    # ---------- Credentials update (always-empty fields) ----------
    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Allow refreshing credentials without exposing current values."""
        errors: Dict[str, str] = {}
        current_email = self.config_entry.data.get(CONF_GOOGLE_EMAIL, "Unknown")

        if selector is not None:
            schema = vol.Schema(
                {
                    vol.Required("new_secrets_json"): selector({"text": {"multiline": True}}),
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required("new_secrets_json"): str,
                }
            )

        if user_input is not None:
            method, email, cands, err = _interpret_credentials_choice(
                user_input,
                secrets_field="new_secrets_json",
                token_field="new_oauth_token",
                email_field="new_google_email",
            )
            if err:
                if err == "invalid_json":
                    errors["new_secrets_json"] = "invalid_json"
                else:
                    errors["base"] = err
            else:
                try:
                    assert method == "secrets" and email and cands
                    # Parse secrets.json for passing to validation
                    parsed_secrets = json.loads(user_input.get("new_secrets_json") or "{}")
                    result = await async_pick_working_token(email, cands, parsed_secrets)
                    if not result:
                        errors["base"] = "cannot_connect"
                    else:
                        token_source, chosen_token = result
                        new_data = {
                            DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                            CONF_OAUTH_TOKEN: chosen_token,
                            CONF_GOOGLE_EMAIL: email,
                            DATA_SECRET_BUNDLE: parsed_secrets,
                            "token_source": token_source,
                        }

                        entry = self.config_entry
                        updated_data = dict(entry.data)
                        updated_data.update(new_data)

                        # Update credentials in data only
                        self.hass.config_entries.async_update_entry(entry, data=updated_data)
                        # Reload to apply immediately
                        self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
                        return self.async_abort(reason="reconfigure_successful")
                except Exception as err2:  # noqa: BLE001
                    _LOGGER.error("Credentials update failed: %s", err2)
                    errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="credentials",
            data_schema=schema,
            errors=errors,
            description_placeholders={"account_email": current_email},
        )


# ---------- Custom exceptions ----------
class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
