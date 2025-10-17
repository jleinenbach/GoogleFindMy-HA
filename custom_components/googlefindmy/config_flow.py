# custom_components/googlefindmy/config_flow.py
"""Config flow for the Google Find My Device custom integration.

This module implements the complete configuration and options flows for the
integration, following Home Assistant best practices:

Key design decisions (Best Practice):
- Test-before-configure: We validate credentials *before* creating a config entry.
  If validation fails, no entry is created, the form is shown again with an error.
- Early unique_id: We set the config entry unique ID (normalized Google email)
  as soon as it is known, to avoid duplicate flows and duplicate entries.
- No persistence during the flow: We never write tokens/secrets to disk before
  `async_create_entry`. All flow-time validation uses ephemeral clients only.
- Duplicate protection: If a config entry for the same Google account already
  exists, we abort the flow using `_abort_if_unique_id_configured()`.
- Guard handling: If the API raises a "multiple config entries" guard (e.g.,
  "Multiple config entries active" / "... pass entry.runtime_data"), we accept
  the candidate and *defer* validation to setup, where an entry-scoped cache
  exists. We do *not* skip online validation in general.
- Defensive API calls: We support multiple call signatures for the basic
  device-list probe and map likely exceptions to HA-standard error keys
  (`invalid_auth`, `cannot_connect`, `unknown`) without leaking sensitive data.

Security & privacy:
- No secrets in logs or exceptions; messages are redacted and bounded.
- No secrets are persisted before `async_create_entry`.
- Email addresses are normalized (lowercased) before being used as unique IDs.

Docstring & comments:
- All docstrings and inline comments are written in English.
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

# Optional network exception typing (robust mapping without hard dependency)
try:  # pragma: no cover - environment dependent
    import aiohttp
except Exception:  # noqa: BLE001
    aiohttp = None  # type: ignore[assignment]

# Selector is not guaranteed in older cores; import defensively.
try:  # pragma: no cover - environment dependent
    from homeassistant.helpers.selector import selector
except Exception:  # noqa: BLE001
    selector = None  # type: ignore[assignment]

from .api import GoogleFindMyAPI

from .const import (
    # Core domain & credential keys
    DOMAIN,
    CONF_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
    DATA_AUTH_METHOD,
    # Options (non-secret runtime settings)
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_IGNORED_DEVICES,
    # Defaults
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_OPTIONS_SCHEMA_VERSION,
    coerce_ignored_mapping,
)

# --- Soft optional imports for additional options (keep the flow robust) ----------
# If these constants are not present in your build, the fields are omitted.
try:
    from .const import OPT_MOVEMENT_THRESHOLD, DEFAULT_MOVEMENT_THRESHOLD
except Exception:  # noqa: BLE001
    OPT_MOVEMENT_THRESHOLD = None  # type: ignore[assignment]
    DEFAULT_MOVEMENT_THRESHOLD = None  # type: ignore[assignment]

try:
    from .const import (
        OPT_GOOGLE_HOME_FILTER_ENABLED,
        OPT_GOOGLE_HOME_FILTER_KEYWORDS,
        DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
        DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS,
    )
except Exception:  # noqa: BLE001
    OPT_GOOGLE_HOME_FILTER_ENABLED = None  # type: ignore[assignment]
    OPT_GOOGLE_HOME_FILTER_KEYWORDS = None  # type: ignore[assignment]
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED = None  # type: ignore[assignment]
    DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS = None  # type: ignore[assignment]

try:
    from .const import OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES
except Exception:  # noqa: BLE001
    OPT_ENABLE_STATS_ENTITIES = None  # type: ignore[assignment]
    DEFAULT_ENABLE_STATS_ENTITIES = None  # type: ignore[assignment]

# Optional UI helper for visibility menu
try:
    from .const import ignored_choices_for_ui  # helper that formats UI choices
except Exception:  # noqa: BLE001
    ignored_choices_for_ui = None  # type: ignore[assignment]
# -----------------------------------------------------------------------------------

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Backcompat base for OptionsFlow: prefer OptionsFlowWithReload if present
# ---------------------------------------------------------------------
try:  # pragma: no cover - environment dependent
    OptionsFlowBase = config_entries.OptionsFlowWithReload  # type: ignore[attr-defined]
except Exception:
    OptionsFlowBase = config_entries.OptionsFlow  # type: ignore[assignment]

# ---------------------------
# Validators (format/plausibility)
# ---------------------------
_EMAIL_RE = re.compile(
    r"^(?=.{3,254}$)[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$"
)
_TOKEN_RE = re.compile(r"^\S{16,}$")


def _email_valid(value: str) -> bool:
    """Return True if value looks like a real email address."""
    return bool(_EMAIL_RE.match(value or ""))


def _token_plausible(value: str) -> bool:
    """Return True if value looks like a token (no spaces, at least 16 chars)."""
    return bool(_TOKEN_RE.match(value or ""))


def _looks_like_aas(value: str) -> bool:
    """Heuristically detect AAS-shaped tokens (Android AuthSub)."""
    return value.startswith("aas_et/") or value.startswith("aas_et.")


def _looks_like_jwt(value: str) -> bool:
    """Lightweight detection for JWT-like blobs (Base64URL x3; often starts with 'eyJ')."""
    return value.count(".") >= 2 and value[:3] == "eyJ"


def _disqualifies_oauth_for_persistence(value: str) -> Optional[str]:
    """Return a reason string if token must not be persisted as OAuth (AAS/JWT)."""
    if _looks_like_aas(value):
        return "token resembles an AAS token (aas_et…), not an OAuth token"
    if _looks_like_jwt(value):
        return "token looks like a JWT (installation/ID token), not an OAuth token"
    return None


def _is_multi_entry_guard_error(err: Exception) -> bool:
    """Return True if the exception message indicates an entry-scope guard.

    We do not rely on a specific exception type to retain compatibility across builds.
    """
    msg = f"{err}"
    return ("Multiple config entries active" in msg) or ("entry.runtime_data" in msg)


# ---------------------------
# Error mapping for API exceptions
# ---------------------------
def _map_api_exc_to_error_key(err: Exception) -> str:
    """Map library/network errors to HA error keys without leaking details.

    Returns one of: "invalid_auth", "cannot_connect", "unknown".
    """
    # Library-provided types (optional, soft detection)
    name = err.__class__.__name__.lower()

    # Authentication-ish signals
    if any(k in name for k in ("auth", "unauthor", "forbidden", "credential")):
        return "invalid_auth"

    # If the error exposes status/response codes, interpret 401/403 as auth issues.
    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    try:
        if int(status) in (401, 403):
            return "invalid_auth"
    except Exception:
        pass

    # Known network/transport buckets
    if aiohttp is not None and isinstance(err, (aiohttp.ClientError, aiohttp.ServerTimeoutError)):  # type: ignore[attr-defined]
        return "cannot_connect"
    if any(k in name for k in ("timeout", "dns", "socket", "connection", "connect")):
        return "cannot_connect"

    # Guard is not an error for the flow (handled elsewhere)
    if _is_multi_entry_guard_error(err):
        return "unknown"

    return "unknown"


# ---------------------------
# Auth method choice UI
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

# ---------------------------
# Extractors (email + token candidates with preference order)
# ---------------------------
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
    # Nested fallback shapes
    try:
        val = data["account"]["email"]
        if isinstance(val, str) and "@" in val:
            return val
    except Exception:
        pass
    return None


def _extract_oauth_candidates_from_secrets(data: Dict[str, Any]) -> List[Tuple[str, str]]:
    """Return plausible tokens in preferred order from a secrets bundle.

    Priority:
      1) 'aas_token' (Account Authentication Service token)
      2) Flat OAuth-ish keys ('oauth_token', 'access_token', etc.)
      3) 'fcm_credentials.installation.token' (installation JWT)
      4) 'fcm_credentials.fcm.registration.token' (registration token)
    Duplicate values are de-duplicated while preserving source labels.
    """
    cands: List[Tuple[str, str]] = []
    seen: set[str] = set()

    def _add(label: str, value: Any) -> None:
        if isinstance(value, str) and _token_plausible(value) and value not in seen:
            cands.append((label, value))
            seen.add(value)

    _add("aas_token", data.get("aas_token"))

    for key in (
        CONF_OAUTH_TOKEN,
        "oauth_token",
        "oauthToken",
        "OAuthToken",
        "access_token",
        "token",
        "adm_token",
        "admToken",
        "Auth",
    ):
        _add(key, data.get(key))

    try:
        _add("fcm_installation", data["fcm_credentials"]["installation"]["token"])
    except Exception:
        pass
    try:
        _add("fcm_registration", data["fcm_credentials"]["fcm"]["registration"]["token"])
    except Exception:
        pass

    return cands


def _cand_labels(cands: List[Tuple[str, str]]) -> str:
    """Return a short, safe label list for candidates (no secrets)."""
    labels = [src for src, _ in cands][:5]
    tail = "…" if len(cands) > 5 else ""
    return ", ".join(labels) + tail


# ---------------------------
# API probing helpers (signature-robust)
# ---------------------------
async def _try_probe_devices(api: GoogleFindMyAPI, *, email: str, token: str) -> List[Dict[str, Any]]:
    """Call the API to fetch a basic device list using defensive signatures.

    The function attempts multiple call conventions to remain compatible with
    different versions of `api.py`. If a call shape fails with TypeError, it
    retries with a different signature. Any other exception is propagated.
    """
    # Preferred: explicit kwargs with names used by recent builds
    try:
        return await api.async_get_basic_device_list(username=email, token=token)  # type: ignore[call-arg]
    except TypeError:
        pass

    # Alternate names
    try:
        return await api.async_get_basic_device_list(email=email, token=token)  # type: ignore[call-arg]
    except TypeError:
        pass

    # Fallback: only email
    try:
        return await api.async_get_basic_device_list(email=email)  # type: ignore[call-arg]
    except TypeError:
        pass

    # Last resort: no-arg probing (may rely on instance fields set at construction)
    return await api.async_get_basic_device_list()


async def _async_new_api_for_probe(email: str, token: str) -> GoogleFindMyAPI:
    """Create a fresh, ephemeral API instance for pre-flight validation.

    We avoid registering any global caches or touching persistent storage. If
    constructor kwargs change across builds, we try multiple shapes.
    """
    try:
        return GoogleFindMyAPI(oauth_token=token, google_email=email)  # type: ignore[call-arg]
    except TypeError:
        try:
            return GoogleFindMyAPI(token=token, email=email)  # type: ignore[call-arg]
        except TypeError:
            return GoogleFindMyAPI()  # type: ignore[call-arg]


async def async_pick_working_token(email: str, candidates: List[Tuple[str, str]]) -> Optional[str]:
    """Try the candidate tokens in order until one passes a minimal online validation.

    - On success: return the working token string.
    - On multi-entry guard: return the *current* token and let setup validate later.
    - On failure: continue with the next candidate; return None if none validate.
    """
    for source, token in candidates:
        try:
            api = await _async_new_api_for_probe(email=email, token=token)
            await _try_probe_devices(api, email=email, token=token)
            _LOGGER.debug("Token probe OK (source=%s, email=%s).", source, email)
            return token
        except Exception as err:  # noqa: BLE001 - capture library/network/auth
            if _is_multi_entry_guard_error(err):
                _LOGGER.info(
                    "Auth guard: multiple config entries detected; deferring token validation to setup "
                    "(source=%s, email=%s).",
                    source,
                    email,
                )
                return token
            # For diagnostics, map (but do not leak specifics). Keep going.
            key = _map_api_exc_to_error_key(err)
            _LOGGER.debug("Token probe failed (source=%s, mapped=%s, email=%s).", source, key, email)
            continue
    return None


# ---------------------------
# Shared interpreter for either/or credential choice (initial flow & options)
# ---------------------------
def _interpret_credentials_choice(
    user_input: Dict[str, Any],
    *,
    secrets_field: str,
    token_field: str,
    email_field: str,
) -> Tuple[Optional[str], Optional[str], Optional[List[Tuple[str, str]]], Optional[str]]:
    """Normalize flow input into a single authentication method.

    Returns:
        (method, email, token_candidates, error_key)
        - method: "secrets" | "manual" | None
        - email: normalized email string or None
        - token_candidates: list[(source_label, token)] in preference order
        - error_key: translation key if a validation error is detected
    """
    secrets_json = (user_input.get(secrets_field) or "").strip()
    oauth_token = (user_input.get(token_field) or "").strip()
    google_email = (user_input.get(email_field) or "").strip()

    has_secrets = bool(secrets_json)
    has_token = bool(oauth_token)
    has_email = bool(google_email)

    # Disallow mixing; require exactly one path.
    if has_secrets and (has_token or has_email):
        return None, None, None, "choose_one"
    if not has_secrets and not (has_token and has_email):
        return None, None, None, "choose_one"

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

    # Manual path: basic plausibility and shape-based negative checks
    if not (_email_valid(google_email) and _token_plausible(oauth_token)):
        return "manual", None, None, "invalid_token"
    if _disqualifies_oauth_for_persistence(oauth_token):
        return "manual", None, None, "invalid_token"

    return "manual", google_email, [("manual", oauth_token)], None


# ---------------------------
# Reauth-specific helpers
# ---------------------------
_REAUTH_FIELD_SECRETS = "secrets_json"
_REAUTH_FIELD_TOKEN = "new_oauth_token"


def _interpret_reauth_choice(user_input: Dict[str, Any]) -> Tuple[Optional[str], Optional[Any], Optional[str]]:
    """Interpret reauth input where the email is fixed by the entry.

    Returns:
        (method, payload, error_key)
        - method: "secrets" | "manual" | None
        - payload: dict (parsed secrets) for "secrets", str (token) for "manual"
        - error_key: translation key if validation fails
    """
    secrets_raw = (user_input.get(_REAUTH_FIELD_SECRETS) or "").strip()
    token_raw = (user_input.get(_REAUTH_FIELD_TOKEN) or "").strip()

    has_secrets = bool(secrets_raw)
    has_token = bool(token_raw)

    # Exactly one must be provided
    if (has_secrets and has_token) or (not has_secrets and not has_token):
        return None, None, "choose_one"

    if has_secrets:
        try:
            parsed = json.loads(secrets_raw)
            if not isinstance(parsed, dict):
                raise TypeError()
        except (json.JSONDecodeError, TypeError):
            return None, None, "invalid_json"

        # Extract minimal plausibility from secrets (must contain at least 1 candidate token + an email)
        email = _extract_email_from_secrets(parsed)
        candidates = _extract_oauth_candidates_from_secrets(parsed)
        if not (email and _email_valid(email) and candidates):
            return None, None, "invalid_token"
        return "secrets", parsed, None

    # Manual token path (email is fixed from the entry)
    if not (_token_plausible(token_raw) and not _disqualifies_oauth_for_persistence(token_raw)):
        return None, None, "invalid_token"

    return "manual", token_raw, None


def _normalize_email(email: str | None) -> str:
    """Normalize emails consistently for unique_id / comparisons."""
    return (email or "").strip().lower()


def _find_entry_by_email(hass, email: str) -> Optional[ConfigEntry]:
    """Return an existing entry that matches the normalized email, if any."""
    target = _normalize_email(email)
    for e in hass.config_entries.async_entries(DOMAIN):
        if _normalize_email(e.data.get(CONF_GOOGLE_EMAIL)) == target:
            return e
    return None


# ---------------------------
# Config Flow
# ---------------------------
class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow for Google Find My Device."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize transient flow state."""
        self._auth_data: Dict[str, Any] = {}
        self._available_devices: List[Tuple[str, str]] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        """Return the options flow for an existing config entry."""
        return OptionsFlowHandler()

    # ------------------ Step: choose authentication path ------------------
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask the user to choose how to provide credentials."""
        if user_input is not None:
            method = user_input.get("auth_method")
            _LOGGER.debug("User step: method selected = %s", method)
            if method == _AUTH_METHOD_SECRETS:
                return await self.async_step_secrets_json()
            if method == _AUTH_METHOD_INDIVIDUAL:
                return await self.async_step_individual_tokens()
        _LOGGER.debug("User step: presenting auth method selection form.")
        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

    # ------------------ Step: secrets.json path ------------------
    async def async_step_secrets_json(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect and validate secrets.json content, with failover and guard handling."""
        errors: Dict[str, str] = {}

        schema = STEP_SECRETS_DATA_SCHEMA
        if selector is not None:
            schema = vol.Schema({vol.Required("secrets_json"): selector({"text": {"multiline": True}})})

        if user_input is not None:
            raw = (user_input.get("secrets_json") or "")
            _LOGGER.debug(
                "Secrets step: received input (chars=%d). Starting parse/validation.",
                len(raw),
            )
            method, email, cands, err = _interpret_credentials_choice(
                user_input, secrets_field="secrets_json", token_field=CONF_OAUTH_TOKEN, email_field=CONF_GOOGLE_EMAIL
            )
            if err:
                if err == "invalid_json":
                    errors["secrets_json"] = "invalid_json"
                    _LOGGER.debug("Secrets step: invalid JSON in secrets.json.")
                else:
                    errors["base"] = err
                    _LOGGER.debug("Secrets step: input error '%s' (empty/mixed/invalid).", err)
            else:
                assert method == "secrets" and email and cands
                _LOGGER.debug(
                    "Secrets step: parsed OK (email=%s, candidates=%d: %s).",
                    _normalize_email(email),
                    len(cands),
                    _cand_labels(cands),
                )
                # Early unique_id to avoid duplicate flows/entries for this account
                uid = _normalize_email(email)
                await self.async_set_unique_id(uid)
                _LOGGER.debug("Secrets step: set unique_id=%s and checked for duplicates.", uid)
                self._abort_if_unique_id_configured()

                # Online validation of candidates (defer only if guard is raised)
                chosen = await async_pick_working_token(email, cands)
                if not chosen:
                    errors["base"] = "cannot_connect"
                    _LOGGER.info(
                        "Secrets step: no candidate token validated online (email=%s) -> cannot_connect.",
                        uid,
                    )
                else:
                    # Persist an OAuth-shaped candidate if possible; otherwise keep the validated value.
                    to_persist = chosen
                    if _disqualifies_oauth_for_persistence(to_persist):
                        alt = next((v for (_src, v) in cands if not _disqualifies_oauth_for_persistence(v)), None)
                        if alt:
                            to_persist = alt
                            _LOGGER.debug(
                                "Secrets step: validated non-OAuth-shaped token; persisting alternative OAuth-shaped candidate."
                            )
                        else:
                            _LOGGER.warning(
                                "Secrets step: only non-OAuth-shaped token available; persisting validated value."
                            )

                    self._auth_data = {
                        DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                        CONF_OAUTH_TOKEN: to_persist,
                        CONF_GOOGLE_EMAIL: email,
                    }
                    _LOGGER.info(
                        "Secrets step: staged credentials for email=%s (method=secrets).",
                        uid,
                    )
                    return await self.async_step_device_selection()

        if errors:
            _LOGGER.debug("Secrets step: showing form with errors=%s", errors)
        else:
            _LOGGER.debug("Secrets step: presenting form.")
        return self.async_show_form(step_id="secrets_json", data_schema=schema, errors=errors)

    # ------------------ Step: manual token + email ------------------
    async def async_step_individual_tokens(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect manual OAuth token and Google email, then validate."""
        errors: Dict[str, str] = {}
        if user_input is not None:
            _LOGGER.debug("Manual token step: received input (email present=%s).", bool(user_input.get(CONF_GOOGLE_EMAIL)))
            method, email, cands, err = _interpret_credentials_choice(
                user_input, secrets_field="secrets_json", token_field=CONF_OAUTH_TOKEN, email_field=CONF_GOOGLE_EMAIL
            )
            if err:
                errors["base"] = err
                _LOGGER.debug("Manual token step: input error '%s'.", err)
            else:
                assert method == "manual" and email and cands
                uid = _normalize_email(email)
                await self.async_set_unique_id(uid)
                _LOGGER.debug("Manual token step: set unique_id=%s and checked for duplicates.", uid)
                self._abort_if_unique_id_configured()

                # Validate the single manual token (defer only on guard)
                chosen = await async_pick_working_token(email, cands)
                if not chosen:
                    errors["base"] = "cannot_connect"
                    _LOGGER.info(
                        "Manual token step: token did not validate online (email=%s) -> cannot_connect.",
                        uid,
                    )
                else:
                    self._auth_data = {
                        DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                        CONF_OAUTH_TOKEN: chosen,
                        CONF_GOOGLE_EMAIL: email,
                    }
                    _LOGGER.info(
                        "Manual token step: staged credentials for email=%s (method=manual).",
                        uid,
                    )
                    return await self.async_step_device_selection()

        if errors:
            _LOGGER.debug("Manual token step: showing form with errors=%s", errors)
        else:
            _LOGGER.debug("Manual token step: presenting form.")
        return self.async_show_form(step_id="individual_tokens", data_schema=STEP_INDIVIDUAL_DATA_SCHEMA, errors=errors)

    # ------------------ Shared: build API for final probe ------------------
    async def _async_build_api_and_username(self) -> Tuple[GoogleFindMyAPI, str, str]:
        """Construct an ephemeral API client from transient flow credentials."""
        email = self._auth_data.get(CONF_GOOGLE_EMAIL)
        oauth = self._auth_data.get(CONF_OAUTH_TOKEN)
        if not (email and oauth):
            raise HomeAssistantError("Missing credentials in setup flow.")
        api = await _async_new_api_for_probe(email=email, token=oauth)
        return api, email, oauth

    # ------------------ Step: device selection & non-secret options ------------------
    async def async_step_device_selection(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Finalize the initial setup: optional device probe + non-secret options."""
        errors: Dict[str, str] = {}

        # Safety: ensure unique_id is set (should already be done)
        email_for_uid = _normalize_email(self._auth_data.get(CONF_GOOGLE_EMAIL))
        if email_for_uid and not self.unique_id:
            await self.async_set_unique_id(email_for_uid)
            _LOGGER.debug("Device selection: unique_id set late to %s.", email_for_uid)
            self._abort_if_unique_id_configured()

        # Attempt a single device probe (optional; setup will re-validate anyway)
        if not self._available_devices:
            try:
                api, username, token = await self._async_build_api_and_username()
                _LOGGER.debug("Device selection: probing devices for email=%s.", _normalize_email(username))
                devices = await _try_probe_devices(api, email=username, token=token)
                if devices:
                    self._available_devices = [(d.get("name") or d.get("id") or "", d.get("id") or "") for d in devices]
                    _LOGGER.debug("Device selection: probe returned %d device(s).", len(self._available_devices))
                else:
                    _LOGGER.debug("Device selection: probe returned no devices.")
            except Exception as err:  # noqa: BLE001
                if _is_multi_entry_guard_error(err):
                    _LOGGER.info(
                        "Auth guard: device probe deferred to setup due to multiple config entries "
                        "(email=%s, entry_id=%s).",
                        self._auth_data.get(CONF_GOOGLE_EMAIL),
                        getattr(getattr(self, 'config_entry', None), 'entry_id', None),
                    )
                else:
                    key = _map_api_exc_to_error_key(err)
                    if key == "invalid_auth":
                        errors["base"] = "invalid_auth"
                    elif key == "cannot_connect":
                        errors["base"] = "cannot_connect"
                    else:
                        errors["base"] = "unknown"
                    _LOGGER.debug("Device selection: probe failed (mapped=%s).", key)

        # Build options schema dynamically based on available constants
        schema_fields: Dict[Any, Any] = {
            vol.Optional(OPT_LOCATION_POLL_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
            vol.Optional(OPT_DEVICE_POLL_DELAY): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
            vol.Optional(OPT_MIN_ACCURACY_THRESHOLD): vol.All(vol.Coerce(int), vol.Range(min=25, max=500)),
            vol.Optional(OPT_MAP_VIEW_TOKEN_EXPIRATION): bool,
        }
        if OPT_MOVEMENT_THRESHOLD is not None:
            schema_fields[vol.Optional(OPT_MOVEMENT_THRESHOLD)] = vol.All(vol.Coerce(int), vol.Range(min=10, max=200))
        if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
            schema_fields[vol.Optional(OPT_GOOGLE_HOME_FILTER_ENABLED)] = bool
        if OPT_GOOGLE_HOME_FILTER_KEYWORDS is not None:
            schema_fields[vol.Optional(OPT_GOOGLE_HOME_FILTER_KEYWORDS)] = str
        if OPT_ENABLE_STATS_ENTITIES is not None:
            schema_fields[vol.Optional(OPT_ENABLE_STATS_ENTITIES)] = bool

        base_schema = vol.Schema(schema_fields)

        if errors:
            _LOGGER.debug("Device selection: showing form with errors=%s", errors)
            return self.async_show_form(step_id="device_selection", data_schema=base_schema, errors=errors)

        # Default values (only for present fields)
        defaults = {
            OPT_LOCATION_POLL_INTERVAL: DEFAULT_LOCATION_POLL_INTERVAL,
            OPT_DEVICE_POLL_DELAY: DEFAULT_DEVICE_POLL_DELAY,
            OPT_MIN_ACCURACY_THRESHOLD: DEFAULT_MIN_ACCURACY_THRESHOLD,
            OPT_MAP_VIEW_TOKEN_EXPIRATION: DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
        }
        if OPT_MOVEMENT_THRESHOLD is not None and DEFAULT_MOVEMENT_THRESHOLD is not None:
            defaults[OPT_MOVEMENT_THRESHOLD] = DEFAULT_MOVEMENT_THRESHOLD
        if OPT_GOOGLE_HOME_FILTER_ENABLED is not None and DEFAULT_GOOGLE_HOME_FILTER_ENABLED is not None:
            defaults[OPT_GOOGLE_HOME_FILTER_ENABLED] = DEFAULT_GOOGLE_HOME_FILTER_ENABLED
        if OPT_GOOGLE_HOME_FILTER_KEYWORDS is not None and DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS is not None:
            defaults[OPT_GOOGLE_HOME_FILTER_KEYWORDS] = DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS
        if OPT_ENABLE_STATS_ENTITIES is not None and DEFAULT_ENABLE_STATS_ENTITIES is not None:
            defaults[OPT_ENABLE_STATS_ENTITIES] = DEFAULT_ENABLE_STATS_ENTITIES

        schema_with_defaults = self.add_suggested_values_to_schema(base_schema, defaults)

        if user_input is not None:
            # Data = minimal credentials; options = non-secret runtime settings
            data_payload: Dict[str, Any] = {
                DATA_AUTH_METHOD: self._auth_data.get(DATA_AUTH_METHOD),
                # PATCH/CLARIFICATION by jleinenbach:
                # The key "oauth_token" is misleading. The token stored here is actually the
                # long-lived AAS (Android AuthSub) "Master Token" obtained from gpsoauth.
                # This master token (often prefixed with "aas_et/") is essential for
                # autonomously generating new, short-lived service tokens in the background,
                # ensuring the integration continues to work without requiring manual re-authentication.
                CONF_OAUTH_TOKEN: self._auth_data.get(CONF_OAUTH_TOKEN),
                CONF_GOOGLE_EMAIL: self._auth_data.get(CONF_GOOGLE_EMAIL),
            }

            options_payload: Dict[str, Any] = {}
            for k in schema_fields.keys():
                # `k` is a voluptuous marker; retrieve the underlying key
                if hasattr(k, "schema"):
                    real_key = next(iter(k.schema))  # type: ignore[attr-defined]
                else:
                    real_key = k
                options_payload[real_key] = user_input.get(real_key, defaults.get(real_key))

            try:
                _LOGGER.info(
                    "Creating config entry for email=%s (with options).",
                    _normalize_email(self._auth_data.get(CONF_GOOGLE_EMAIL)),
                )
                return self.async_create_entry(
                    title=self._auth_data.get(CONF_GOOGLE_EMAIL) or "Google Find My Device",
                    data=data_payload,
                    options=options_payload,  # type: ignore[call-arg]
                )
            except TypeError:
                # Older HA cores do not support options in create_entry; coalesce to data.
                _LOGGER.info(
                    "Creating config entry for email=%s (options merged into data for legacy core).",
                    _normalize_email(self._auth_data.get(CONF_GOOGLE_EMAIL)),
                )
                shadow = dict(data_payload)
                shadow.update(options_payload)
                return self.async_create_entry(
                    title=self._auth_data.get(CONF_GOOGLE_EMAIL) or "Google Find My Device",
                    data=shadow,
                )

        _LOGGER.debug("Device selection: presenting form.")
        return self.async_show_form(step_id="device_selection", data_schema=schema_with_defaults)

    # ------------------ Reauthentication ------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start a reauthentication flow linked to an existing entry context."""
        _LOGGER.debug("Reauth: starting for existing entry.")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Collect and validate new credentials for this entry, then update+reload.

        UX requirements:
        - The email address is fixed by the existing entry and must not be editable.
        - The form accepts exactly one of:
          * Full `secrets.json` (multiline) OR
          * A new OAuth token (single-line).
        - If `secrets.json` belongs to a different Google account:
          * If another entry with that email already exists -> abort("already_configured")
          * Else -> show `email_mismatch` error (user should add a new integration instead).
        """
        errors: Dict[str, str] = {}

        # Resolve the fixed entry/email once; used for both validation and placeholder.
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        fixed_email = _normalize_email(entry.data.get(CONF_GOOGLE_EMAIL))

        # Schema with only secrets/token (never echo secrets; no email field)
        if selector is not None:
            schema = vol.Schema(
                {
                    vol.Optional(_REAUTH_FIELD_SECRETS): selector({"text": {"multiline": True}}),
                    vol.Optional(_REAUTH_FIELD_TOKEN): str,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Optional(_REAUTH_FIELD_SECRETS): str,
                    vol.Optional(_REAUTH_FIELD_TOKEN): str,
                }
            )

        if user_input is not None:
            _LOGGER.debug("Reauth: input received for fixed_email=%s.", fixed_email)
            method, payload, err = _interpret_reauth_choice(user_input)
            if err:
                if err == "invalid_json":
                    errors[_REAUTH_FIELD_SECRETS] = "invalid_json"
                    _LOGGER.debug("Reauth: invalid JSON in secrets.")
                else:
                    errors["base"] = err
                    _LOGGER.debug("Reauth: input error '%s'.", err)
            else:
                try:
                    if method == "manual":
                        # Validate the single manual token against the fixed email.
                        token = str(payload)
                        chosen = await async_pick_working_token(fixed_email, [("manual", token)])
                        if not chosen:
                            errors["base"] = "cannot_connect"
                            _LOGGER.info("Reauth: manual token did not validate -> cannot_connect (email=%s).", fixed_email)
                        else:
                            to_persist = chosen
                            if _disqualifies_oauth_for_persistence(to_persist):
                                _LOGGER.warning("Reauth: validated token is non-OAuth-shaped; persisting as provided.")
                            updated_data = dict(entry.data)
                            updated_data.update(
                                {
                                    DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                                    CONF_OAUTH_TOKEN: to_persist,
                                    CONF_GOOGLE_EMAIL: fixed_email,
                                }
                            )
                            _LOGGER.info("Reauth: updating entry and reloading (email=%s, method=manual).", fixed_email)
                            return self.async_update_reload_and_abort(entry=entry, data=updated_data, reason="reauth_successful")

                    elif method == "secrets":
                        parsed: Dict[str, Any] = dict(payload)  # type: ignore[assignment]
                        extracted_email = _normalize_email(_extract_email_from_secrets(parsed))
                        cands = _extract_oauth_candidates_from_secrets(parsed)
                        _LOGGER.debug(
                            "Reauth: secrets parsed (extracted_email=%s, candidates=%d: %s).",
                            extracted_email,
                            len(cands),
                            _cand_labels(cands),
                        )

                        # If secrets.json belongs to a different account, apply the CF-3 rules:
                        if extracted_email and extracted_email != fixed_email:
                            existing = _find_entry_by_email(self.hass, extracted_email)
                            if existing is not None:
                                _LOGGER.info(
                                    "Reauth: secrets belong to already-configured account (%s) -> abort already_configured.",
                                    extracted_email,
                                )
                                return self.async_abort(reason="already_configured")
                            errors["base"] = "email_mismatch"
                            _LOGGER.debug(
                                "Reauth: email mismatch (fixed=%s, provided=%s).",
                                fixed_email,
                                extracted_email,
                            )
                        else:
                            # Validate candidates; defer on guard.
                            chosen = await async_pick_working_token(fixed_email, cands)
                            if not chosen:
                                errors["base"] = "cannot_connect"
                                _LOGGER.info("Reauth: token(s) did not validate -> cannot_connect (email=%s).", fixed_email)
                            else:
                                to_persist = chosen
                                if _disqualifies_oauth_for_persistence(to_persist):
                                    alt = next((v for (_src, v) in cands if not _disqualifies_oauth_for_persistence(v)), None)
                                    if alt:
                                        to_persist = alt
                                        _LOGGER.debug("Reauth: non-OAuth-shaped token -> persisting OAuth-shaped alternative.")
                                    else:
                                        _LOGGER.warning("Reauth: only non-OAuth-shaped token available; persisting validated value.")

                                updated_data = dict(entry.data)
                                updated_data.update(
                                    {
                                        DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                                        CONF_OAUTH_TOKEN: to_persist,
                                        CONF_GOOGLE_EMAIL: fixed_email,
                                    }
                                )
                                _LOGGER.info("Reauth: updating entry and reloading (email=%s, method=secrets).", fixed_email)
                                return self.async_update_reload_and_abort(entry=entry, data=updated_data, reason="reauth_successful")
                except Exception as err2:  # noqa: BLE001
                    if _is_multi_entry_guard_error(err2):
                        _LOGGER.info(
                            "Auth guard: reauth deferred to setup (multiple entries) "
                            "(fixed_email=%s, entry_id=%s, method=%s).",
                            fixed_email,
                            getattr(entry, "entry_id", None),
                            method,
                        )
                        # Accept the first candidate and allow setup to validate with entry-scoped cache
                        if method == "manual":
                            token = str(payload)
                            updated_data = dict(entry.data)
                            updated_data.update(
                                {
                                    DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                                    CONF_OAUTH_TOKEN: token,
                                    CONF_GOOGLE_EMAIL: fixed_email,
                                }
                            )
                            return self.async_update_reload_and_abort(entry=entry, data=updated_data, reason="reauth_successful")
                        if method == "secrets":
                            parsed = dict(payload)  # type: ignore[assignment]
                            cands = _extract_oauth_candidates_from_secrets(parsed)
                            token_first = cands[0][1] if cands else ""
                            updated_data = dict(entry.data)
                            updated_data.update(
                                {
                                    DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                                    CONF_OAUTH_TOKEN: token_first,
                                    CONF_GOOGLE_EMAIL: fixed_email,
                                }
                            )
                            return self.async_update_reload_and_abort(entry=entry, data=updated_data, reason="reauth_successful")

                    _LOGGER.error("Reauth validation failed: %s", err2)
                    key = _map_api_exc_to_error_key(err2)
                    errors["base"] = key

        # Pass the fixed email via description placeholders so the UI can display it as text.
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"email": fixed_email},
        )


# ---------------------------
# Options Flow
# ---------------------------
class OptionsFlowHandler(OptionsFlowBase):
    """Options flow to update non-secret settings and optionally refresh credentials.

    Notes:
        - Device inclusion/exclusion is controlled by HA's device enable/disable.
          We no longer present a `tracked_devices` multi-select here.
        - Returning `async_create_entry` with the new options triggers a reload
          automatically when using `OptionsFlowWithReload` (if available).
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Display a small menu for settings, credentials refresh, or visibility."""
        return self.async_show_menu(step_id="init", menu_options=["settings", "credentials", "visibility"])

    # ---------- Helpers for live API/cache access ----------
    def _get_entry_cache(self, entry: ConfigEntry) -> Optional[Any]:
        """Return the TokenCache (or equivalent) for this entry if available.

        We prefer `entry.runtime_data` (modern pattern) and fall back to `hass.data`.
        We never log secrets and we do not assume a specific concrete cache class.
        """
        rd = getattr(entry, "runtime_data", None)
        if rd is not None:
            for attr in ("_cache", "cache"):
                if hasattr(rd, attr):
                    try:
                        return getattr(rd, attr)
                    except Exception:  # pragma: no cover
                        pass

        data = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if data is not None:
            for attr in ("_cache", "cache"):
                if hasattr(data, attr):
                    try:
                        return getattr(data, attr)
                    except Exception:  # pragma: no cover
                        pass

        if isinstance(data, dict):
            return data.get("cache") or data.get("_cache")
        return None

    async def _async_build_api_from_entry(self, entry: ConfigEntry) -> GoogleFindMyAPI:
        """Construct API object from the live entry context (cache-first)."""
        cache = self._get_entry_cache(entry)
        if cache is not None:
            session = async_get_clientsession(self.hass)
            try:
                return GoogleFindMyAPI(cache=cache, session=session)  # type: ignore[call-arg]
            except TypeError:
                return GoogleFindMyAPI(cache=cache)  # type: ignore[call-arg]

        # Last resort: attempt a minimal credential boot for options-only calls.
        oauth = entry.data.get(CONF_OAUTH_TOKEN)
        email = entry.data.get(CONF_GOOGLE_EMAIL)
        if oauth and email:
            try:
                return GoogleFindMyAPI(oauth_token=oauth, google_email=email)  # type: ignore[call-arg]
            except TypeError:
                return GoogleFindMyAPI(token=oauth, email=email)  # type: ignore[call-arg]

        raise RuntimeError("GoogleFindMyAPI requires either `cache=` or minimal flow credentials.")

    # ---------- Settings (non-secret) ----------
    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Update non-secret options in a single form."""
        errors: Dict[str, str] = {}

        entry = self.config_entry
        opt = entry.options
        dat = entry.data

        def _get(cur_key, default_val):
            return opt.get(cur_key, dat.get(cur_key, default_val))

        current = {
            OPT_LOCATION_POLL_INTERVAL: _get(OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL),
            OPT_DEVICE_POLL_DELAY: _get(OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY),
            OPT_MIN_ACCURACY_THRESHOLD: _get(OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD),
            OPT_MAP_VIEW_TOKEN_EXPIRATION: _get(OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION),
        }
        if OPT_MOVEMENT_THRESHOLD is not None and DEFAULT_MOVEMENT_THRESHOLD is not None:
            current[OPT_MOVEMENT_THRESHOLD] = _get(OPT_MOVEMENT_THRESHOLD, DEFAULT_MOVEMENT_THRESHOLD)
        if OPT_GOOGLE_HOME_FILTER_ENABLED is not None and DEFAULT_GOOGLE_HOME_FILTER_ENABLED is not None:
            current[OPT_GOOGLE_HOME_FILTER_ENABLED] = _get(OPT_GOOGLE_HOME_FILTER_ENABLED, DEFAULT_GOOGLE_HOME_FILTER_ENABLED)
        if OPT_GOOGLE_HOME_FILTER_KEYWORDS is not None and DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS is not None:
            current[OPT_GOOGLE_HOME_FILTER_KEYWORDS] = _get(OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS)
        if OPT_ENABLE_STATS_ENTITIES is not None and DEFAULT_ENABLE_STATS_ENTITIES is not None:
            current[OPT_ENABLE_STATS_ENTITIES] = _get(OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES)

        fields: Dict[Any, Any] = {
            vol.Optional(OPT_LOCATION_POLL_INTERVAL): vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
            vol.Optional(OPT_DEVICE_POLL_DELAY): vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
            vol.Optional(OPT_MIN_ACCURACY_THRESHOLD): vol.All(vol.Coerce(int), vol.Range(min=25, max=500)),
            vol.Optional(OPT_MAP_VIEW_TOKEN_EXPIRATION): bool,
        }
        if OPT_MOVEMENT_THRESHOLD is not None:
            fields[vol.Optional(OPT_MOVEMENT_THRESHOLD)] = vol.All(vol.Coerce(int), vol.Range(min=10, max=200))
        if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
            fields[vol.Optional(OPT_GOOGLE_HOME_FILTER_ENABLED)] = bool
        if OPT_GOOGLE_HOME_FILTER_KEYWORDS is not None:
            fields[vol.Optional(OPT_GOOGLE_HOME_FILTER_KEYWORDS)] = str
        if OPT_ENABLE_STATS_ENTITIES is not None:
            fields[vol.Optional(OPT_ENABLE_STATS_ENTITIES)] = bool

        base_schema = vol.Schema(fields)

        if user_input is not None:
            new_options = dict(current)
            for k in list(current.keys()):
                if k in user_input:
                    new_options[k] = user_input[k]
            # Returning create_entry triggers automatic reload when supported.
            _LOGGER.info("Options: updating non-secret settings for entry_id=%s.", entry.entry_id)
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(base_schema, current),
            errors=errors,
        )

    # ---------- Visibility (restore ignored devices) ----------
    async def async_step_visibility(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Display ignored devices and allow restoring them (remove from OPT_IGNORED_DEVICES)."""
        entry = self.config_entry
        options = dict(entry.options)
        raw = options.get(OPT_IGNORED_DEVICES) or entry.data.get(OPT_IGNORED_DEVICES) or {}
        ignored_map, _migrated = coerce_ignored_mapping(raw)

        if not ignored_map:
            _LOGGER.debug("Visibility: no ignored devices for entry_id=%s.", entry.entry_id)
            return self.async_abort(reason="no_ignored_devices")

        if callable(ignored_choices_for_ui):
            choices = ignored_choices_for_ui(ignored_map)  # type: ignore[misc]
        else:
            # Fallback: simple id->name mapping
            choices = {dev_id: (meta.get("name") or dev_id) for dev_id, meta in ignored_map.items()}

        schema = vol.Schema({vol.Optional("unignore_devices", default=[]): cv.multi_select(choices)})

        if user_input is not None:
            to_restore = user_input.get("unignore_devices") or []
            if not isinstance(to_restore, list):
                to_restore = list(to_restore)
            for dev_id in to_restore:
                ignored_map.pop(dev_id, None)

            new_options = dict(entry.options)
            new_options[OPT_IGNORED_DEVICES] = ignored_map
            new_options[OPT_OPTIONS_SCHEMA_VERSION] = 2
            _LOGGER.info(
                "Visibility: restored %d device(s) from ignored list (entry_id=%s).",
                len(to_restore),
                entry.entry_id,
            )
            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(step_id="visibility", data_schema=schema)

    # ---------- Credentials refresh ----------
    async def async_step_credentials(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Allow refreshing credentials without exposing current ones.

        Exactly one method must be provided (secrets.json *or* manual token + email).
        We validate (with guard-aware deferral) and then update the entry, followed by reload.
        """
        errors: Dict[str, str] = {}

        if selector is not None:
            schema = vol.Schema(
                {
                    vol.Optional("new_secrets_json"): selector({"text": {"multiline": True}}),
                    vol.Optional("new_oauth_token"): str,
                    vol.Optional("new_google_email"): str,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Optional("new_secrets_json"): str,
                    vol.Optional("new_oauth_token"): str,
                    vol.Optional("new_google_email"): str,
                }
            )

        if user_input is not None:
            _LOGGER.debug(
                "Credentials update: received input (has secrets=%s, has token+email=%s).",
                bool(user_input.get("new_secrets_json")),
                bool(user_input.get("new_oauth_token") and user_input.get("new_google_email")),
            )
            method, email, cands, err = _interpret_credentials_choice(
                user_input,
                secrets_field="new_secrets_json",
                token_field="new_oauth_token",
                email_field="new_google_email",
            )
            if err:
                if err == "invalid_json":
                    errors["new_secrets_json"] = "invalid_json"
                    _LOGGER.debug("Credentials update: invalid JSON in secrets.")
                else:
                    errors["base"] = err
                    _LOGGER.debug("Credentials update: input error '%s'.", err)
            else:
                try:
                    assert email and cands
                    _LOGGER.debug(
                        "Credentials update: parsed OK (email=%s, candidates=%d: %s).",
                        _normalize_email(email),
                        len(cands),
                        _cand_labels(cands),
                    )
                    chosen = await async_pick_working_token(email, cands)
                    if not chosen:
                        errors["base"] = "cannot_connect"
                        _LOGGER.info(
                            "Credentials update: token(s) did not validate online (email=%s) -> cannot_connect.",
                            _normalize_email(email),
                        )
                    else:
                        to_persist = chosen
                        if _disqualifies_oauth_for_persistence(to_persist):
                            alt = next((v for (_src, v) in cands if not _disqualifies_oauth_for_persistence(v)), None)
                            if alt:
                                to_persist = alt
                                _LOGGER.debug("Credentials update: non-OAuth-shaped token -> persisting alternative.")
                            else:
                                _LOGGER.warning("Credentials update: only non-OAuth-shaped token; persisting validated value.")

                        entry = self.config_entry
                        updated_data = dict(entry.data)
                        updated_data.update(
                            {
                                DATA_AUTH_METHOD: (_AUTH_METHOD_SECRETS if method == "secrets" else _AUTH_METHOD_INDIVIDUAL),
                                CONF_OAUTH_TOKEN: to_persist,
                                CONF_GOOGLE_EMAIL: email,
                            }
                        )
                        self.hass.config_entries.async_update_entry(entry, data=updated_data)
                        self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
                        _LOGGER.info(
                            "Credentials update: entry updated and reload scheduled (email=%s, method=%s).",
                            _normalize_email(email),
                            method,
                        )
                        return self.async_abort(reason="reconfigure_successful")
                except Exception as err2:  # noqa: BLE001
                    if _is_multi_entry_guard_error(err2):
                        # Defer: accept first candidate and reload
                        entry = self.config_entry
                        updated_data = dict(entry.data)
                        updated_data.update(
                            {
                                DATA_AUTH_METHOD: (_AUTH_METHOD_SECRETS if method == "secrets" else _AUTH_METHOD_INDIVIDUAL),
                                CONF_OAUTH_TOKEN: cands[0][1],
                                CONF_GOOGLE_EMAIL: email,
                            }
                        )
                        self.hass.config_entries.async_update_entry(entry, data=updated_data)
                        self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))
                        _LOGGER.info(
                            "Auth guard: credentials update deferred to setup (email=%s, method=%s).",
                            _normalize_email(email),
                            method,
                        )
                        return self.async_abort(reason="reconfigure_successful")
                    _LOGGER.error("Credentials update failed: %s", err2)
                    key = _map_api_exc_to_error_key(err2)
                    errors["base"] = key

        return self.async_show_form(step_id="credentials", data_schema=schema, errors=errors)


# ---------- Custom exceptions (for callers that want to raise HA-native errors) ----------
class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the remote service."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate invalid authentication was provided."""
