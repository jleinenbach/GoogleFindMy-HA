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

import inspect
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry

try:  # pragma: no cover - compatibility shim for stripped environments
    from homeassistant.config_entries import (
        SOURCE_DISCOVERY,
        SOURCE_DISCOVERY_UPDATE,
        DiscoveryKey,
        async_create_discovery_flow,
    )
except Exception:  # noqa: BLE001
    SOURCE_DISCOVERY = "discovery"  # type: ignore[assignment]
    SOURCE_DISCOVERY_UPDATE = "discovery_update"  # type: ignore[assignment]

    try:  # pragma: no cover - runtime optional dependency
        from homeassistant.helpers.discovery_flow import DiscoveryKey as _DiscoveryKey
    except Exception:  # noqa: BLE001

        @dataclass(slots=True)
        class DiscoveryKey:  # type: ignore[no-redef]
            """Fallback DiscoveryKey representation for legacy cores."""

            domain: str
            key: str | tuple[str, ...]
            version: int = 1

    else:  # pragma: no cover - simple aliasing
        DiscoveryKey = _DiscoveryKey  # type: ignore[assignment]

    async def async_create_discovery_flow(  # type: ignore[no-redef]
        hass,
        domain,
        context,
        data,
        *,
        discovery_key=None,
    ) -> None:
        """Fallback helper mirroring modern discovery flow creation."""

        try:
            from homeassistant.helpers.discovery_flow import (
                async_create_flow as _async_create_flow,
            )
        except Exception:  # noqa: BLE001
            await hass.config_entries.flow.async_init(  # type: ignore[attr-defined]
                domain,
                context=context,
                data=data,
            )
            return

        await _async_create_flow(  # type: ignore[call-arg]
            hass,
            domain,
            context,
            data,
            discovery_key=discovery_key,
        )


try:  # pragma: no cover - compatibility shim for stripped environments
    from homeassistant.config_entries import ConfigSubentry, ConfigSubentryFlow
except Exception:  # noqa: BLE001
    try:  # pragma: no cover - best-effort partial import
        from homeassistant.config_entries import ConfigSubentry
    except Exception:  # noqa: BLE001
        ConfigSubentry = None  # type: ignore[assignment]

    class ConfigSubentryFlow:  # type: ignore[no-redef]
        """Fallback stub for Home Assistant's ConfigSubentryFlow."""

        def __init__(self, config_entry: ConfigEntry) -> None:
            self.config_entry = config_entry
            self.subentry: ConfigSubentry | None = None

        async def async_step_user(
            self, user_input: dict[str, Any] | None = None
        ) -> FlowResult:
            raise NotImplementedError

        async def async_step_reconfigure(
            self, user_input: dict[str, Any] | None = None
        ) -> FlowResult:
            raise NotImplementedError

        def async_create_entry(
            self, *, title: str, data: dict[str, Any]
        ) -> FlowResult:
            return {
                "type": "create_entry",
                "title": title,
                "data": data,
            }

        def async_update_and_abort(
            self,
            *,
            data: dict[str, Any],
            title: str | None = None,
            unique_id: str | None = None,
        ) -> FlowResult:
            # FIXME: The real Home Assistant implementation expects
            # ``async_update_and_abort(entry, subentry, *, data=..., ...)``.
            # This stub keeps a simplified signature so legacy test environments
            # can execute the new flows without importing the upstream helper.
            return {
                "type": "abort",
                "reason": "update",
                "data": data,
                "title": title,
                "unique_id": unique_id,
            }

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

from types import MappingProxyType
from .api import GoogleFindMyAPI
from .Auth.adm_token_retrieval import _mask_email as _mask_email_for_logs

from .const import (
    # Core domain & credential keys
    DOMAIN,
    CONF_OAUTH_TOKEN,
    CONF_GOOGLE_EMAIL,
    DATA_AUTH_METHOD,
    DATA_AAS_TOKEN,
    DATA_SECRET_BUNDLE,
    # Options (non-secret runtime settings)
    OPT_LOCATION_POLL_INTERVAL,
    OPT_DEVICE_POLL_DELAY,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_IGNORED_DEVICES,
    OPT_CONTRIBUTOR_MODE,
    OPT_ENABLE_STATS_ENTITIES,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
    # Defaults
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DEFAULT_CONTRIBUTOR_MODE,
    CONTRIBUTOR_MODE_HIGH_TRAFFIC,
    CONTRIBUTOR_MODE_IN_ALL_AREAS,
    OPT_OPTIONS_SCHEMA_VERSION,
    coerce_ignored_mapping,
    DEFAULT_OPTIONS,
    DEFAULT_ENABLE_STATS_ENTITIES,
)

# Standard discovery update info source exposed for helper-triggered updates.
SOURCE_DISCOVERY_UPDATE_INFO = "discovery_update_info"

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

# Optional UI helper for visibility menu
try:
    from .const import ignored_choices_for_ui  # helper that formats UI choices
except Exception:  # noqa: BLE001
    ignored_choices_for_ui = None  # type: ignore[assignment]
# -----------------------------------------------------------------------------------

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class _SubentryOption:
    """Lightweight representation of a selectable subentry."""

    key: str
    label: str
    subentry: ConfigSubentry | None
    visible_device_ids: tuple[str, ...]

    @property
    def subentry_id(self) -> str | None:
        """Return the backing Home Assistant subentry identifier when available."""

        if self.subentry is None:
            return None
        return getattr(self.subentry, "subentry_id", None)


_FIELD_SUBENTRY = "subentry"
_FIELD_REPAIR_TARGET = "target_subentry"
_FIELD_REPAIR_DELETE = "delete_subentry"
_FIELD_REPAIR_FALLBACK = "fallback_subentry"
_FIELD_REPAIR_DEVICES = "device_ids"

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


def _looks_like_jwt(value: str) -> bool:
    """Lightweight detection for JWT-like blobs (Base64URL x3; often starts with 'eyJ')."""
    return value.count(".") >= 2 and value[:3] == "eyJ"


_TRACKER_FEATURE_PLATFORMS: tuple[str, ...] = (
    "button",
    "device_tracker",
    "sensor",
)

_SERVICE_FEATURE_PLATFORMS: tuple[str, ...] = (
    "binary_sensor",
)


def _normalize_feature_list(features: Iterable[str]) -> list[str]:
    """Return a sorted list of unique, lower-cased feature identifiers."""

    normalized: list[str] = []
    for feature in features:
        if not isinstance(feature, str):
            continue
        candidate = feature.strip().lower()
        if candidate:
            normalized.append(candidate)
    ordered = list(dict.fromkeys(normalized))
    return sorted(ordered)


def _normalize_visible_ids(visible_ids: Iterable[str]) -> list[str]:
    """Return a sorted list of unique device identifiers suitable for storage."""

    candidates: list[str] = []
    for device_id in visible_ids:
        if not isinstance(device_id, str):
            continue
        candidate = device_id.strip()
        if candidate:
            candidates.append(candidate)
    return sorted(dict.fromkeys(candidates))


def _derive_feature_settings(
    *, options_payload: Mapping[str, Any], defaults: Mapping[str, Any]
) -> tuple[bool, dict[str, Any]]:
    """Return the Google Home filter flag and feature toggles for a subentry."""

    default_filter_enabled = False
    if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
        if OPT_GOOGLE_HOME_FILTER_ENABLED in options_payload:
            default_filter_enabled = bool(
                options_payload[OPT_GOOGLE_HOME_FILTER_ENABLED]
            )
        elif defaults.get(OPT_GOOGLE_HOME_FILTER_ENABLED) is not None:
            default_filter_enabled = bool(defaults[OPT_GOOGLE_HOME_FILTER_ENABLED])
        elif DEFAULT_GOOGLE_HOME_FILTER_ENABLED is not None:
            default_filter_enabled = bool(DEFAULT_GOOGLE_HOME_FILTER_ENABLED)

    has_filter = default_filter_enabled
    if (
        OPT_GOOGLE_HOME_FILTER_ENABLED is not None
        and OPT_GOOGLE_HOME_FILTER_ENABLED in options_payload
    ):
        has_filter = bool(options_payload[OPT_GOOGLE_HOME_FILTER_ENABLED])

    feature_flags: dict[str, Any] = {}
    if OPT_ENABLE_STATS_ENTITIES is not None:
        if OPT_ENABLE_STATS_ENTITIES in options_payload:
            feature_flags[OPT_ENABLE_STATS_ENTITIES] = bool(
                options_payload[OPT_ENABLE_STATS_ENTITIES]
            )
        elif defaults.get(OPT_ENABLE_STATS_ENTITIES) is not None:
            feature_flags[OPT_ENABLE_STATS_ENTITIES] = bool(
                defaults[OPT_ENABLE_STATS_ENTITIES]
            )

    if OPT_MAP_VIEW_TOKEN_EXPIRATION in options_payload:
        feature_flags[OPT_MAP_VIEW_TOKEN_EXPIRATION] = bool(
            options_payload[OPT_MAP_VIEW_TOKEN_EXPIRATION]
        )

    if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
        feature_flags[OPT_GOOGLE_HOME_FILTER_ENABLED] = has_filter

    contributor_mode = options_payload.get(OPT_CONTRIBUTOR_MODE)
    if contributor_mode is not None:
        feature_flags[OPT_CONTRIBUTOR_MODE] = contributor_mode

    return has_filter, feature_flags


def _build_subentry_payload(
    *,
    group_key: str,
    features: Iterable[str],
    entry_title: str,
    has_google_home_filter: bool,
    feature_flags: Mapping[str, Any],
    visible_device_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Construct the payload stored on a config subentry."""

    payload: dict[str, Any] = {
        "group_key": group_key,
        "features": _normalize_feature_list(features),
        "fcm_push_enabled": False,
        "has_google_home_filter": has_google_home_filter,
        "feature_flags": dict(feature_flags),
        "entry_title": entry_title,
    }
    if visible_device_ids:
        normalized_ids = _normalize_visible_ids(visible_device_ids)
        if normalized_ids:
            payload["visible_device_ids"] = normalized_ids
    return payload


def _disqualifies_for_persistence(value: str) -> str | None:
    """Return a reason string if token must NOT be persisted.

    IMPORTANT CHANGE:
    - AAS (aas_et/...) master tokens ARE allowed to be stored (they are needed
      to mint service tokens in the background).
    - JWT-like installation/ID tokens are rejected (not stable/refreshable).
    """
    if _looks_like_jwt(value):
        return "token looks like a JWT (installation/ID token), not a stable API token"
    return None


def _is_multi_entry_guard_error(err: Exception) -> bool:
    """Return True if the exception message indicates an entry-scope guard."""
    msg = f"{err}"
    return ("Multiple config entries active" in msg) or ("entry.runtime_data" in msg)


# ---------------------------
# Error mapping for API exceptions
# ---------------------------
def _map_api_exc_to_error_key(err: Exception) -> str:
    """Map library/network errors to HA error keys without leaking details."""
    name = err.__class__.__name__.lower()

    if any(k in name for k in ("auth", "unauthor", "forbidden", "credential")):
        return "invalid_auth"

    status = getattr(err, "status", None) or getattr(err, "status_code", None)
    try:
        if int(status) in (401, 403):
            return "invalid_auth"
    except Exception:
        pass

    if aiohttp is not None and isinstance(
        err, (aiohttp.ClientError, aiohttp.ServerTimeoutError)
    ):  # type: ignore[attr-defined]
        return "cannot_connect"
    if any(k in name for k in ("timeout", "dns", "socket", "connection", "connect")):
        return "cannot_connect"

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
        vol.Required(CONF_OAUTH_TOKEN, description="OAuth/AAS token"): str,
        vol.Required(CONF_GOOGLE_EMAIL, description="Google email address"): str,
    }
)


# ---------------------------
# Extractors (email + token candidates with preference order)
# ---------------------------
def _extract_email_from_secrets(data: dict[str, Any]) -> str | None:
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


def _extract_oauth_candidates_from_secrets(
    data: dict[str, Any],
) -> list[tuple[str, str]]:
    """Return plausible tokens in preferred order from a secrets bundle.

    Priority:
      1) 'aas_token' (Account Authentication Service master token)
      2) Flat OAuth-ish keys ('oauth_token', 'access_token', etc.)
      3) 'fcm_credentials.installation.token' (installation JWT)  [discouraged]
      4) 'fcm_credentials.fcm.registration.token' (registration token)  [discouraged]
    Duplicate values are de-duplicated while preserving source labels.
    """
    cands: list[tuple[str, str]] = []
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
        _add(
            "fcm_registration", data["fcm_credentials"]["fcm"]["registration"]["token"]
        )
    except Exception:
        pass

    return cands


def _cand_labels(cands: list[tuple[str, str]]) -> str:
    """Return a short, safe label list for candidates (no secrets)."""
    labels = [src for src, _ in cands][:5]
    tail = "…" if len(cands) > 5 else ""
    return ", ".join(labels) + tail


# ---------------------------
# API probing helpers (signature-robust)
# ---------------------------
async def _try_probe_devices(
    api: GoogleFindMyAPI, *, email: str, token: str
) -> list[dict[str, Any]]:
    """Call the API to fetch a basic device list using defensive signatures."""
    try:
        return await api.async_get_basic_device_list(username=email, token=token)  # type: ignore[call-arg]
    except TypeError:
        pass
    try:
        return await api.async_get_basic_device_list(email=email, token=token)  # type: ignore[call-arg]
    except TypeError:
        pass
    try:
        return await api.async_get_basic_device_list(email=email)  # type: ignore[call-arg]
    except TypeError:
        pass
    return await api.async_get_basic_device_list()


async def _async_new_api_for_probe(
    email: str,
    token: str,
    *,
    secrets_bundle: dict[str, Any] | None = None,
) -> GoogleFindMyAPI:
    """Create a fresh, ephemeral API instance for pre-flight validation."""
    try:
        return GoogleFindMyAPI(
            oauth_token=token,
            google_email=email,
            secrets_bundle=secrets_bundle,
        )  # type: ignore[call-arg]
    except TypeError:
        try:
            return GoogleFindMyAPI(
                token=token,
                email=email,
                secrets_bundle=secrets_bundle,
            )  # type: ignore[call-arg]
        except TypeError:
            return GoogleFindMyAPI()  # type: ignore[call-arg]


async def async_pick_working_token(
    email: str,
    candidates: list[tuple[str, str]],
    *,
    secrets_bundle: dict[str, Any] | None = None,
) -> str | None:
    """Try the candidate tokens in order until one passes a minimal online validation."""
    for source, token in candidates:
        try:
            api = await _async_new_api_for_probe(
                email=email, token=token, secrets_bundle=secrets_bundle
            )
            await _try_probe_devices(api, email=email, token=token)
            _LOGGER.debug(
                "Token probe OK (source=%s, email=%s).",
                source,
                _mask_email_for_logs(email),
            )
            return token
        except Exception as err:  # noqa: BLE001
            if _is_multi_entry_guard_error(err):
                _LOGGER.debug(
                    (
                        "Token probe guarded but accepted (source=%s, email=%s). "
                        "Deferring to entry-scoped caches for multi-account setup."
                    ),
                    source,
                    _mask_email_for_logs(email),
                )
                return token
            key = _map_api_exc_to_error_key(err)
            _LOGGER.debug(
                "Token probe failed (source=%s, mapped=%s, email=%s).",
                source,
                key,
                _mask_email_for_logs(email),
            )
            continue
    return None


def _cand_labels(candidates: list[tuple[str, str]]) -> str:
    """Return a redacted, human-readable list of token candidate sources."""
    sources = {source for source, _token in candidates if source}
    if not sources:
        return "none"
    return ", ".join(sorted(sources))


# ---------------------------
# Shared interpreter for either/or credential choice (initial flow & options)
# ---------------------------
def _interpret_credentials_choice(
    user_input: dict[str, Any],
    *,
    secrets_field: str,
    token_field: str,
    email_field: str,
) -> tuple[str | None, str | None, list[tuple[str, str]] | None, str | None]:
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
    if _disqualifies_for_persistence(oauth_token):  # only rejects JWT now
        return "manual", None, None, "invalid_token"

    return "manual", google_email, [("manual", oauth_token)], None


# ---------------------------
# Reauth-specific helpers
# ---------------------------
_REAUTH_FIELD_SECRETS = "secrets_json"
_REAUTH_FIELD_TOKEN = "new_oauth_token"


def _interpret_reauth_choice(
    user_input: dict[str, Any],
) -> tuple[str | None, Any | None, str | None]:
    """Interpret reauth input where the email is fixed by the entry."""
    secrets_raw = (user_input.get(_REAUTH_FIELD_SECRETS) or "").strip()
    token_raw = (user_input.get(_REAUTH_FIELD_TOKEN) or "").strip()

    has_secrets = bool(secrets_raw)
    has_token = bool(token_raw)

    if (has_secrets and has_token) or (not has_secrets and not has_token):
        return None, None, "choose_one"

    if has_secrets:
        try:
            parsed = json.loads(secrets_raw)
            if not isinstance(parsed, dict):
                raise TypeError()
        except (json.JSONDecodeError, TypeError):
            return None, None, "invalid_json"

        email = _extract_email_from_secrets(parsed)
        candidates = _extract_oauth_candidates_from_secrets(parsed)
        if not (email and _email_valid(email) and candidates):
            return None, None, "invalid_token"
        return "secrets", parsed, None

    # Manual token path (email is fixed from the entry)
    if not (
        _token_plausible(token_raw) and not _disqualifies_for_persistence(token_raw)
    ):
        return None, None, "invalid_token"

    return "manual", token_raw, None


def _normalize_email(email: str | None) -> str:
    """Normalize emails consistently for unique_id / comparisons."""
    return (email or "").strip().lower()


def _find_entry_by_email(hass, email: str) -> ConfigEntry | None:
    """Return an existing entry that matches the normalized email, if any."""
    target = _normalize_email(email)
    for e in hass.config_entries.async_entries(DOMAIN):
        if _normalize_email(e.data.get(CONF_GOOGLE_EMAIL)) == target:
            return e
    return None


# ---------------------------
# Discovery helpers
# ---------------------------


class DiscoveryFlowError(HomeAssistantError):
    """Raised when a discovery payload cannot be processed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class CloudDiscoveryData:
    """Normalized discovery payload shared by cloud setup hooks."""

    email: str
    unique_id: str
    candidates: tuple[tuple[str, str], ...]
    secrets_bundle: Mapping[str, Any] | None
    title: str | None = None


def _discovery_payload_equivalent(
    first: CloudDiscoveryData, second: CloudDiscoveryData
) -> bool:
    """Return True when two normalized discovery payloads are equivalent."""

    if first.unique_id != second.unique_id or first.email != second.email:
        return False

    if first.candidates != second.candidates:
        return False

    if first.secrets_bundle is None or second.secrets_bundle is None:
        return first.secrets_bundle is None and second.secrets_bundle is None

    return dict(first.secrets_bundle) == dict(second.secrets_bundle)


def _normalize_and_validate_discovery_payload(
    payload: Mapping[str, Any] | None,
) -> CloudDiscoveryData:
    """Normalize raw discovery metadata into a structured payload."""

    if not isinstance(payload, Mapping):
        raise DiscoveryFlowError("invalid_discovery_info")

    payload_dict = dict(payload)
    email_raw = payload_dict.get(CONF_GOOGLE_EMAIL) or payload_dict.get("email")
    if isinstance(email_raw, str):
        email_candidate = email_raw.strip()
    else:
        email_candidate = ""

    secrets_bundle: Mapping[str, Any] | None = None
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add_candidate(label: str, token: Any) -> None:
        if isinstance(token, str) and _token_plausible(token) and token not in seen:
            candidates.append((label, token))
            seen.add(token)

    secrets_raw = (
        payload_dict.get(DATA_SECRET_BUNDLE)
        or payload_dict.get("secrets_json")
        or payload_dict.get("secrets")
    )
    if isinstance(secrets_raw, str):
        try:
            secrets_raw = json.loads(secrets_raw)
        except json.JSONDecodeError as err:
            raise DiscoveryFlowError("invalid_discovery_info") from err

    if isinstance(secrets_raw, Mapping):
        secrets_dict = dict(secrets_raw)
        secrets_bundle = MappingProxyType(secrets_dict)
        email_from_secrets = _extract_email_from_secrets(secrets_dict)
        if email_from_secrets:
            email_candidate = email_candidate or email_from_secrets
        for label, token in _extract_oauth_candidates_from_secrets(secrets_dict):
            _add_candidate(label, token)

    for key in (
        "candidate_tokens",
        "candidates",
        "tokens",
    ):
        value = payload_dict.get(key)
        if isinstance(value, str):
            _add_candidate(key, value)
        elif isinstance(value, Mapping):
            for label, token in value.items():
                _add_candidate(str(label), token)
        elif isinstance(value, Iterable):
            for idx, token in enumerate(value):
                if isinstance(token, Mapping):
                    label = str(token.get("label") or token.get("source") or key)
                    _add_candidate(label, token.get("token"))
                else:
                    _add_candidate(f"{key}_{idx}", token)

    for direct_key, label in (
        (CONF_OAUTH_TOKEN, CONF_OAUTH_TOKEN),
        ("oauth_token", "oauth_token"),
        ("token", "token"),
        ("aas_token", "aas_token"),
    ):
        _add_candidate(label, payload_dict.get(direct_key))

    if not (_email_valid(email_candidate) and email_candidate):
        raise DiscoveryFlowError("invalid_discovery_info")

    if not candidates:
        raise DiscoveryFlowError("cannot_connect")

    normalized_email = _normalize_email(email_candidate)
    title = payload_dict.get("title") or payload_dict.get("name")
    return CloudDiscoveryData(
        email=email_candidate,
        unique_id=normalized_email,
        candidates=tuple(candidates),
        secrets_bundle=secrets_bundle,
        title=str(title) if isinstance(title, str) else None,
    )


async def _ingest_discovery_credentials(
    flow: ConfigFlow,
    discovery: CloudDiscoveryData,
    *,
    existing_entry: ConfigEntry | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Validate discovery credentials and prepare flow + entry payloads."""

    candidates = list(discovery.candidates)
    secrets_bundle = (
        dict(discovery.secrets_bundle) if discovery.secrets_bundle is not None else None
    )

    try:
        chosen = await async_pick_working_token(
            discovery.email,
            candidates,
            secrets_bundle=secrets_bundle,
        )
    except Exception as err:  # noqa: BLE001
        raise DiscoveryFlowError(_map_api_exc_to_error_key(err)) from err

    if not chosen:
        raise DiscoveryFlowError("cannot_connect")

    to_persist = chosen
    alt_candidate = next(
        (
            token
            for _label, token in candidates
            if not _disqualifies_for_persistence(token)
        ),
        None,
    )
    if _disqualifies_for_persistence(to_persist) and alt_candidate:
        to_persist = alt_candidate

    auth_method = _AUTH_METHOD_SECRETS if secrets_bundle else _AUTH_METHOD_INDIVIDUAL
    auth_data: dict[str, Any] = {
        DATA_AUTH_METHOD: auth_method,
        CONF_GOOGLE_EMAIL: discovery.email,
        CONF_OAUTH_TOKEN: to_persist,
    }
    if secrets_bundle:
        auth_data[DATA_SECRET_BUNDLE] = secrets_bundle
    elif DATA_SECRET_BUNDLE in auth_data:
        auth_data.pop(DATA_SECRET_BUNDLE)

    if isinstance(to_persist, str) and to_persist.startswith("aas_et/"):
        auth_data[DATA_AAS_TOKEN] = to_persist
    else:
        auth_data.pop(DATA_AAS_TOKEN, None)

    if existing_entry is not None:
        updated = {**existing_entry.data, **auth_data}
        if not secrets_bundle:
            updated.pop(DATA_SECRET_BUNDLE, None)
        if not (isinstance(to_persist, str) and to_persist.startswith("aas_et/")):
            updated.pop(DATA_AAS_TOKEN, None)
        updates: dict[str, Any] | None = {"data": updated}
    else:
        updates = None

    flow._auth_data = auth_data  # type: ignore[attr-defined]
    return auth_data, updates


# ---------------------------
# Config Flow
# ---------------------------
class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial config flow for Google Find My Device."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize transient flow state."""
        self._auth_data: dict[str, Any] = {}
        self._available_devices: list[tuple[str, str]] = []
        self._subentry_key_core_tracking = TRACKER_SUBENTRY_KEY
        self._subentry_key_service = SERVICE_SUBENTRY_KEY
        self._pending_discovery_payload: CloudDiscoveryData | None = None
        self._pending_discovery_updates: dict[str, Any] | None = None
        self._pending_discovery_existing_entry: ConfigEntry | None = None
        self._discovery_confirm_pending = False

    def _clear_discovery_confirmation_state(self) -> None:
        """Reset cached discovery confirmation state.

        The base `ConfigFlow` helper `_set_confirm_only()` toggles the
        `context["confirm_only"]` flag so the UI renders a confirmation form.
        This reset helper must clear the same flag whenever we dismiss the
        prompt to keep the state machine in sync with subsequent submissions.
        """

        self._discovery_confirm_pending = False
        self._pending_discovery_payload = None
        self._pending_discovery_updates = None
        self._pending_discovery_existing_entry = None
        context = getattr(self, "context", None)
        if isinstance(context, dict):
            context.pop("confirm_only", None)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        """Return the options flow for an existing config entry."""
        return OptionsFlowHandler()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        _config_entry: ConfigEntry,
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return mapping of supported subentry types to their flow handlers."""

        return {
            SUBENTRY_TYPE_SERVICE: ServiceSubentryFlowHandler,
            SUBENTRY_TYPE_TRACKER: TrackerSubentryFlowHandler,
        }

    async def async_step_discovery(
        self, discovery_info: Mapping[str, Any] | None
    ) -> FlowResult:
        """Handle cloud-triggered discovery payloads."""

        if self._discovery_confirm_pending:
            pending_payload = self._pending_discovery_payload
            is_submission = not discovery_info
            if (
                not is_submission
                and isinstance(discovery_info, Mapping)
                and pending_payload is not None
            ):
                try:
                    normalized_candidate = _normalize_and_validate_discovery_payload(
                        discovery_info
                    )
                except Exception:  # noqa: BLE001
                    is_submission = False
                else:
                    is_submission = _discovery_payload_equivalent(
                        normalized_candidate, pending_payload
                    )

            if not is_submission:
                self._clear_discovery_confirmation_state()
            else:
                updates = self._pending_discovery_updates
                existing_entry = self._pending_discovery_existing_entry
                self._clear_discovery_confirmation_state()

                if existing_entry and updates is not None:
                    self._abort_if_unique_id_configured(updates=updates)
                    return await self.async_abort(reason="already_configured")

                return await self.async_step_device_selection()

        try:
            normalized = _normalize_and_validate_discovery_payload(discovery_info or {})
        except DiscoveryFlowError as err:
            _LOGGER.debug("Discovery ignored due to invalid payload: %s", err.reason)
            return await self.async_abort(reason=err.reason)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Discovery ignored due to unexpected payload: %s", err)
            return await self.async_abort(reason="invalid_discovery_info")

        await self.async_set_unique_id(normalized.unique_id, raise_on_progress=False)

        existing_entry = _find_entry_by_email(self.hass, normalized.email)
        try:
            _, updates = await _ingest_discovery_credentials(
                self, normalized, existing_entry=existing_entry
            )
        except DiscoveryFlowError as err:
            reason = err.reason
            if reason not in {
                "invalid_discovery_info",
                "cannot_connect",
                "invalid_auth",
            }:
                reason = (
                    "cannot_connect" if reason != "invalid_discovery_info" else reason
                )
            return await self.async_abort(reason=reason)

        placeholders = dict(self.context.get("title_placeholders", {}) or {})
        placeholders.setdefault("email", normalized.email)
        self.context["title_placeholders"] = placeholders
        self._pending_discovery_payload = normalized
        self._pending_discovery_updates = updates
        self._pending_discovery_existing_entry = existing_entry
        self._discovery_confirm_pending = True
        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery",
            description_placeholders=placeholders,
        )

    async def async_step_discovery_update_info(
        self, discovery_info: Mapping[str, Any] | None
    ) -> FlowResult:
        """Handle discovery updates for already configured entries."""

        try:
            normalized = _normalize_and_validate_discovery_payload(discovery_info or {})
        except DiscoveryFlowError as err:
            _LOGGER.debug("Discovery update ignored: %s", err.reason)
            return await self.async_abort(reason=err.reason)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Discovery update invalid: %s", err)
            return await self.async_abort(reason="invalid_discovery_info")

        await self.async_set_unique_id(normalized.unique_id, raise_on_progress=False)

        existing_entry = _find_entry_by_email(self.hass, normalized.email)
        if existing_entry is None:
            return await self.async_step_discovery(discovery_info)

        try:
            _, updates = await _ingest_discovery_credentials(
                self, normalized, existing_entry=existing_entry
            )
        except DiscoveryFlowError as err:
            reason = err.reason
            if reason not in {
                "invalid_discovery_info",
                "cannot_connect",
                "invalid_auth",
            }:
                reason = (
                    "cannot_connect" if reason != "invalid_discovery_info" else reason
                )
            return await self.async_abort(reason=reason)

        if updates is None:
            updates = {"data": dict(existing_entry.data)}

        self._abort_if_unique_id_configured(updates=updates)
        return await self.async_abort(reason="already_configured")

    async def async_step_discovery_update(
        self, discovery_info: Mapping[str, Any] | None
    ) -> FlowResult:
        """Provide legacy discovery-update entry point used by the helper."""

        return await self.async_step_discovery_update_info(discovery_info)

    # ------------------ Step: choose authentication path ------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask the user to choose how to provide credentials."""
        if user_input is not None:
            method = user_input.get("auth_method")
            _LOGGER.debug("User step: method selected = %s", method)
            if method == _AUTH_METHOD_SECRETS:
                return await self.async_step_secrets_json()
            if method == _AUTH_METHOD_INDIVIDUAL:
                return await self.async_step_individual_tokens()
            if (
                method is None
                and self._auth_data.get(CONF_OAUTH_TOKEN)
                and self._auth_data.get(CONF_GOOGLE_EMAIL)
            ):
                _LOGGER.debug(
                    "User step: confirm-only submission detected; proceeding to device selection.",
                )
                return await self.async_step_device_selection()
        _LOGGER.debug("User step: presenting auth method selection form.")
        return self.async_show_form(step_id="user", data_schema=STEP_USER_DATA_SCHEMA)

    # ------------------ Step: secrets.json path ------------------
    async def async_step_secrets_json(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and validate secrets.json content, with failover and guard handling."""
        errors: dict[str, str] = {}

        schema = STEP_SECRETS_DATA_SCHEMA
        if selector is not None:
            schema = vol.Schema(
                {vol.Required("secrets_json"): selector({"text": {"multiline": True}})}
            )

        if user_input is not None:
            raw = user_input.get("secrets_json") or ""
            _LOGGER.debug("Secrets step: received input (chars=%d).", len(raw))
            parsed_secrets: dict[str, Any] | None = None
            try:
                parsed_candidate = json.loads(raw)
                if isinstance(parsed_candidate, dict):
                    parsed_secrets = parsed_candidate
                else:
                    raise TypeError()
            except (json.JSONDecodeError, TypeError):
                parsed_secrets = None
            method, email, cands, err = _interpret_credentials_choice(
                user_input,
                secrets_field="secrets_json",
                token_field=CONF_OAUTH_TOKEN,
                email_field=CONF_GOOGLE_EMAIL,
            )
            if err:
                if err == "invalid_json":
                    errors["secrets_json"] = "invalid_json"
                else:
                    errors["base"] = err
            else:
                assert method == "secrets" and email and cands
                uid = _normalize_email(email)
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()

                chosen = await async_pick_working_token(
                    email,
                    cands,
                    secrets_bundle=parsed_secrets,
                )
                if not chosen:
                    _LOGGER.warning(
                        "Token validation failed for %s. No working token found among candidates (%s).",
                        _mask_email_for_logs(email),
                        _cand_labels(cands),
                    )
                    errors["base"] = "cannot_connect"
                else:
                    # Persist validated token; prefer non-JWT candidate when possible
                    to_persist = chosen
                    bad_reason = _disqualifies_for_persistence(to_persist)
                    if bad_reason:
                        alt = next(
                            (
                                v
                                for (_src, v) in cands
                                if not _disqualifies_for_persistence(v)
                            ),
                            None,
                        )
                        if alt:
                            to_persist = alt

                    self._auth_data = {
                        DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                        CONF_OAUTH_TOKEN: to_persist,
                        CONF_GOOGLE_EMAIL: email,
                    }
                    if parsed_secrets is not None:
                        self._auth_data[DATA_SECRET_BUNDLE] = parsed_secrets
                    if isinstance(to_persist, str) and to_persist.startswith("aas_et/"):
                        self._auth_data[DATA_AAS_TOKEN] = to_persist
                    return await self.async_step_device_selection()

        return self.async_show_form(
            step_id="secrets_json", data_schema=schema, errors=errors
        )

    # ------------------ Step: manual token + email ------------------
    async def async_step_individual_tokens(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect manual token and Google email, then validate."""
        errors: dict[str, str] = {}
        if user_input is not None:
            method, email, cands, err = _interpret_credentials_choice(
                user_input,
                secrets_field="secrets_json",
                token_field=CONF_OAUTH_TOKEN,
                email_field=CONF_GOOGLE_EMAIL,
            )
            if err:
                errors["base"] = err
            else:
                assert method == "manual" and email and cands
                uid = _normalize_email(email)
                await self.async_set_unique_id(uid)
                self._abort_if_unique_id_configured()

                chosen = await async_pick_working_token(email, cands)
                if not chosen:
                    _LOGGER.warning(
                        "Token validation failed for %s. No working token found among candidates (%s).",
                        _mask_email_for_logs(email),
                        _cand_labels(cands),
                    )
                    errors["base"] = "cannot_connect"
                else:
                    auth_method = _AUTH_METHOD_INDIVIDUAL
                    self._auth_data = {
                        CONF_OAUTH_TOKEN: chosen,
                        CONF_GOOGLE_EMAIL: email,
                    }
                    if isinstance(chosen, str) and chosen.startswith("aas_et/"):
                        auth_method = _AUTH_METHOD_SECRETS
                        self._auth_data[DATA_AAS_TOKEN] = chosen
                    else:
                        self._auth_data.pop(DATA_AAS_TOKEN, None)
                    self._auth_data[DATA_AUTH_METHOD] = auth_method
                    self._auth_data.pop(DATA_SECRET_BUNDLE, None)
                    return await self.async_step_device_selection()

        return self.async_show_form(
            step_id="individual_tokens",
            data_schema=STEP_INDIVIDUAL_DATA_SCHEMA,
            errors=errors,
        )

    # ------------------ Shared: build API for final probe ------------------
    async def _async_build_api_and_username(self) -> tuple[GoogleFindMyAPI, str, str]:
        """Construct an ephemeral API client from transient flow credentials."""
        email = self._auth_data.get(CONF_GOOGLE_EMAIL)
        oauth = self._auth_data.get(CONF_OAUTH_TOKEN)
        if not (email and oauth):
            raise HomeAssistantError("Missing credentials in setup flow.")
        api = await _async_new_api_for_probe(
            email=email,
            token=oauth,
            secrets_bundle=self._auth_data.get(DATA_SECRET_BUNDLE),
        )
        return api, email, oauth

    # ------------------ Step: device selection & non-secret options ------------------
    async def async_step_device_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Finalize the initial setup: optional device probe + non-secret options."""
        errors: dict[str, str] = {}

        # Ensure unique_id is set (should already be done)
        email_for_uid = _normalize_email(self._auth_data.get(CONF_GOOGLE_EMAIL))
        if email_for_uid and not self.unique_id:
            await self.async_set_unique_id(email_for_uid)
            self._abort_if_unique_id_configured()

        # Try a single probe (optional; setup will re-validate anyway)
        if not self._available_devices:
            try:
                api, username, token = await self._async_build_api_and_username()
                devices = await _try_probe_devices(api, email=username, token=token)
                if devices:
                    self._available_devices = [
                        (d.get("name") or d.get("id") or "", d.get("id") or "")
                        for d in devices
                    ]
            except Exception as err:  # noqa: BLE001
                if not _is_multi_entry_guard_error(err):
                    key = _map_api_exc_to_error_key(err)
                    errors["base"] = key

        # Build options schema dynamically
        schema_fields: dict[Any, Any] = {
            vol.Optional(OPT_LOCATION_POLL_INTERVAL): vol.All(
                vol.Coerce(int), vol.Range(min=60, max=3600)
            ),
            vol.Optional(OPT_DEVICE_POLL_DELAY): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=60)
            ),
            vol.Optional(OPT_MIN_ACCURACY_THRESHOLD): vol.All(
                vol.Coerce(int), vol.Range(min=25, max=500)
            ),
            vol.Optional(OPT_MAP_VIEW_TOKEN_EXPIRATION): bool,
        }
        if OPT_MOVEMENT_THRESHOLD is not None:
            schema_fields[vol.Optional(OPT_MOVEMENT_THRESHOLD)] = vol.All(
                vol.Coerce(int), vol.Range(min=10, max=200)
            )
        if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
            schema_fields[vol.Optional(OPT_GOOGLE_HOME_FILTER_ENABLED)] = bool
        if OPT_GOOGLE_HOME_FILTER_KEYWORDS is not None:
            schema_fields[vol.Optional(OPT_GOOGLE_HOME_FILTER_KEYWORDS)] = str
        if OPT_ENABLE_STATS_ENTITIES is not None:
            schema_fields[vol.Optional(OPT_ENABLE_STATS_ENTITIES)] = bool

        base_schema = vol.Schema(schema_fields)

        # Defaults
        defaults = {
            OPT_LOCATION_POLL_INTERVAL: DEFAULT_LOCATION_POLL_INTERVAL,
            OPT_DEVICE_POLL_DELAY: DEFAULT_DEVICE_POLL_DELAY,
            OPT_MIN_ACCURACY_THRESHOLD: DEFAULT_MIN_ACCURACY_THRESHOLD,
            OPT_MAP_VIEW_TOKEN_EXPIRATION: DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
        }
        if (
            OPT_MOVEMENT_THRESHOLD is not None
            and DEFAULT_MOVEMENT_THRESHOLD is not None
        ):
            defaults[OPT_MOVEMENT_THRESHOLD] = DEFAULT_MOVEMENT_THRESHOLD
        if (
            OPT_GOOGLE_HOME_FILTER_ENABLED is not None
            and DEFAULT_GOOGLE_HOME_FILTER_ENABLED is not None
        ):
            defaults[OPT_GOOGLE_HOME_FILTER_ENABLED] = (
                DEFAULT_GOOGLE_HOME_FILTER_ENABLED
            )
        if (
            OPT_GOOGLE_HOME_FILTER_KEYWORDS is not None
            and DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS is not None
        ):
            defaults[OPT_GOOGLE_HOME_FILTER_KEYWORDS] = (
                DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS
            )
        if (
            OPT_ENABLE_STATS_ENTITIES is not None
            and DEFAULT_ENABLE_STATS_ENTITIES is not None
        ):
            defaults[OPT_ENABLE_STATS_ENTITIES] = DEFAULT_ENABLE_STATS_ENTITIES

        schema_with_defaults = self.add_suggested_values_to_schema(
            base_schema, defaults
        )

        if user_input is not None:
            # Data = credentials; options = runtime settings
            data_payload: dict[str, Any] = {
                DATA_AUTH_METHOD: self._auth_data.get(DATA_AUTH_METHOD),
                # We persist AAS master tokens as well; they are required to mint service tokens.
                CONF_OAUTH_TOKEN: self._auth_data.get(CONF_OAUTH_TOKEN),
                CONF_GOOGLE_EMAIL: self._auth_data.get(CONF_GOOGLE_EMAIL),
            }
            if DATA_SECRET_BUNDLE in self._auth_data:
                data_payload[DATA_SECRET_BUNDLE] = self._auth_data[DATA_SECRET_BUNDLE]
            aas_token = self._auth_data.get(DATA_AAS_TOKEN)
            if isinstance(aas_token, str) and aas_token:
                data_payload[DATA_AAS_TOKEN] = aas_token

            options_payload: dict[str, Any] = {}
            for k in schema_fields.keys():
                # `k` may be a voluptuous marker; retrieve the underlying key
                real_key = next(iter(getattr(k, "schema", {k})))
                options_payload[real_key] = user_input.get(
                    real_key, defaults.get(real_key)
                )
            options_payload[OPT_OPTIONS_SCHEMA_VERSION] = (
                2  # bump schema version at creation
            )

            subentry_context = self._ensure_subentry_context()
            entry_for_update: ConfigEntry | None = None
            entry_id = self.context.get("entry_id")
            if isinstance(entry_id, str):
                entry_for_update = self.hass.config_entries.async_get_entry(entry_id)
            if entry_for_update is not None:
                await self._async_sync_feature_subentries(
                    entry_for_update,
                    options_payload=options_payload,
                    defaults=defaults,
                    context_map=subentry_context,
                )
            else:
                subentry_context.setdefault(self._subentry_key_core_tracking, None)
                subentry_context.setdefault(self._subentry_key_service, None)

            try:
                return self.async_create_entry(
                    # **Change**: title is always the email for clear multi-account display
                    title=self._auth_data.get(CONF_GOOGLE_EMAIL)
                    or "Google Find My Device",
                    data=data_payload,
                    options=options_payload,  # type: ignore[call-arg]
                )
            except TypeError:
                # Older HA cores: merge options into data
                shadow = dict(data_payload)
                shadow.update(options_payload)
                return self.async_create_entry(
                    title=self._auth_data.get(CONF_GOOGLE_EMAIL)
                    or "Google Find My Device",
                    data=shadow,
                )

        return self.async_show_form(
            step_id="device_selection", data_schema=schema_with_defaults, errors=errors
        )

    # ------------------ Reauthentication ------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start a reauthentication flow linked to an existing entry context."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and validate new credentials for this entry, then update+reload."""
        errors: dict[str, str] = {}

        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        fixed_email = _normalize_email(entry.data.get(CONF_GOOGLE_EMAIL))

        if selector is not None:
            schema = vol.Schema(
                {
                    vol.Optional(_REAUTH_FIELD_SECRETS): selector(
                        {"text": {"multiline": True}}
                    ),
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
            method, payload, err = _interpret_reauth_choice(user_input)
            if err:
                if err == "invalid_json":
                    errors[_REAUTH_FIELD_SECRETS] = "invalid_json"
                else:
                    errors["base"] = err
            else:
                try:
                    if method == "manual":
                        token = str(payload)
                        chosen = await async_pick_working_token(
                            fixed_email, [("manual", token)]
                        )
                        if not chosen:
                            _LOGGER.warning(
                                "Token validation failed for %s. No working token found among candidates (%s).",
                                _mask_email_for_logs(fixed_email),
                                _cand_labels([("manual", token)]),
                            )
                            errors["base"] = "cannot_connect"
                        else:
                            if _disqualifies_for_persistence(chosen):
                                _LOGGER.warning(
                                    "Reauth: token looks like a JWT; persisting anyway due to validation."
                                )
                            updated_data = {
                                **entry.data,
                                DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                                CONF_OAUTH_TOKEN: chosen,
                            }
                            if isinstance(chosen, str) and chosen.startswith("aas_et/"):
                                updated_data[DATA_AAS_TOKEN] = chosen
                            else:
                                updated_data.pop(DATA_AAS_TOKEN, None)
                            updated_data.pop(DATA_SECRET_BUNDLE, None)
                            await self._async_clear_cached_aas_token(entry)
                            return self.async_update_reload_and_abort(
                                entry=entry,
                                data=updated_data,
                                reason="reauth_successful",
                            )

                    elif method == "secrets":
                        parsed: dict[str, Any] = dict(payload)  # type: ignore[assignment]
                        extracted_email = _normalize_email(
                            _extract_email_from_secrets(parsed)
                        )
                        cands = _extract_oauth_candidates_from_secrets(parsed)

                        if extracted_email and extracted_email != fixed_email:
                            existing = _find_entry_by_email(self.hass, extracted_email)
                            if existing is not None:
                                return self.async_abort(reason="already_configured")
                            errors["base"] = "email_mismatch"
                        else:
                            chosen = await async_pick_working_token(
                                fixed_email, cands, secrets_bundle=parsed
                            )
                            if not chosen:
                                _LOGGER.warning(
                                    "Token validation failed for %s. No working token found among candidates (%s).",
                                    _mask_email_for_logs(fixed_email),
                                    _cand_labels(cands),
                                )
                                errors["base"] = "cannot_connect"
                            else:
                                # Prefer non-JWT if available
                                to_persist = chosen
                                bad_reason = _disqualifies_for_persistence(to_persist)
                                if bad_reason:
                                    alt = next(
                                        (
                                            v
                                            for (_src, v) in cands
                                            if not _disqualifies_for_persistence(v)
                                        ),
                                        None,
                                    )
                                    if alt:
                                        to_persist = alt
                                updated_data = {
                                    **entry.data,
                                    DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                                    CONF_OAUTH_TOKEN: to_persist,
                                    DATA_SECRET_BUNDLE: parsed,
                                }
                                if isinstance(
                                    to_persist, str
                                ) and to_persist.startswith("aas_et/"):
                                    updated_data[DATA_AAS_TOKEN] = to_persist
                                elif DATA_AAS_TOKEN in updated_data:
                                    updated_data.pop(DATA_AAS_TOKEN, None)
                                await self._async_clear_cached_aas_token(entry)
                                return self.async_update_reload_and_abort(
                                    entry=entry,
                                    data=updated_data,
                                    reason="reauth_successful",
                                )
                except Exception as err2:  # noqa: BLE001
                    if _is_multi_entry_guard_error(err2):
                        # Defer: accept first candidate and reload
                        if method == "manual":
                            manual_token = str(payload)
                            updated_data = {
                                **entry.data,
                                DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                                CONF_OAUTH_TOKEN: manual_token,
                            }
                            if manual_token.startswith("aas_et/"):
                                updated_data[DATA_AAS_TOKEN] = manual_token
                            else:
                                updated_data.pop(DATA_AAS_TOKEN, None)
                            updated_data.pop(DATA_SECRET_BUNDLE, None)
                            await self._async_clear_cached_aas_token(entry)
                            return self.async_update_reload_and_abort(
                                entry=entry,
                                data=updated_data,
                                reason="reauth_successful",
                            )
                        if method == "secrets":
                            parsed = dict(payload)  # type: ignore[assignment]
                            cands = _extract_oauth_candidates_from_secrets(parsed)
                            token_first = cands[0][1] if cands else ""
                            updated_data = {
                                **entry.data,
                                DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                                CONF_OAUTH_TOKEN: token_first,
                                DATA_SECRET_BUNDLE: parsed,
                            }
                            if isinstance(token_first, str) and token_first.startswith(
                                "aas_et/"
                            ):
                                updated_data[DATA_AAS_TOKEN] = token_first
                            else:
                                updated_data.pop(DATA_AAS_TOKEN, None)
                            await self._async_clear_cached_aas_token(entry)
                            return self.async_update_reload_and_abort(
                                entry=entry,
                                data=updated_data,
                                reason="reauth_successful",
                            )
                    errors["base"] = _map_api_exc_to_error_key(err2)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"email": fixed_email},
        )

    async def _async_clear_cached_aas_token(self, entry: ConfigEntry) -> None:
        """Best-effort removal of the cached AAS token for a manual reauth entry."""

        cache = self._get_entry_cache(entry)
        if cache is None:
            return

        for attr in ("async_set_cached_value", "set"):
            setter = getattr(cache, attr, None)
            if not callable(setter):
                continue
            try:
                result = setter(DATA_AAS_TOKEN, None)
                if inspect.isawaitable(result):
                    await result
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Clearing cached AAS token via %s failed: %s", attr, err)
        _LOGGER.debug(
            "No compatible cache setter found to clear the cached AAS token for entry %s",
            entry.entry_id,
        )

    def _get_entry_cache(self, entry: ConfigEntry) -> Any | None:
        """Return the TokenCache (or equivalent) for this entry if available."""

        rd = getattr(entry, "runtime_data", None)
        if rd is not None:
            for attr in ("token_cache", "cache", "_cache"):
                if hasattr(rd, attr):
                    try:
                        return getattr(rd, attr)
                    except Exception:  # pragma: no cover
                        pass

        runtime_bucket = self.hass.data.get(DOMAIN, {}).get("entries", {})
        runtime_entry = runtime_bucket.get(entry.entry_id)
        if runtime_entry is not None:
            for attr in ("_cache", "cache"):
                if hasattr(runtime_entry, attr):
                    try:
                        return getattr(runtime_entry, attr)
                    except Exception:  # pragma: no cover
                        pass
            if isinstance(runtime_entry, dict):
                cache = runtime_entry.get("cache") or runtime_entry.get("_cache")
                if cache is not None:
                    return cache
        return None

    def _ensure_subentry_context(self) -> dict[str, str | None]:
        """Return (and initialize) the flow-scoped subentry identifier mapping."""

        current = self.context.get("subentry_ids")
        if isinstance(current, dict):
            return current
        mapping: dict[str, str | None] = {}
        mapping.setdefault(self._subentry_key_core_tracking, None)
        mapping.setdefault(self._subentry_key_service, None)
        self.context["subentry_ids"] = mapping
        return mapping

    async def _async_sync_feature_subentries(
        self,
        entry: ConfigEntry,
        *,
        options_payload: dict[str, Any],
        defaults: dict[str, Any],
        context_map: dict[str, str | None],
    ) -> None:
        """Ensure the service and tracker subentries match the latest toggles."""

        tracker_key = TRACKER_SUBENTRY_KEY
        service_key = SERVICE_SUBENTRY_KEY
        tracker_unique_id = f"{entry.entry_id}-{tracker_key}"
        service_unique_id = f"{entry.entry_id}-{service_key}"

        entry_title = entry.title or (
            self._auth_data.get(CONF_GOOGLE_EMAIL) or "Google Find My Device"
        )
        tracker_title = "Google Find My devices"

        has_filter, feature_flags = _derive_feature_settings(
            options_payload=options_payload,
            defaults=defaults,
        )

        def _resolve_existing(key: str) -> ConfigSubentry | None:
            existing_id = context_map.get(key)
            subentry_obj: ConfigSubentry | None = None
            if isinstance(existing_id, str):
                subentry_obj = entry.subentries.get(existing_id)
            if subentry_obj is None:
                for candidate in entry.subentries.values():
                    if candidate.data.get("group_key") == key:
                        subentry_obj = candidate
                        break
            return subentry_obj

        def _existing_visible(subentry_obj: ConfigSubentry | None) -> tuple[str, ...]:
            if subentry_obj is None:
                return ()
            data = getattr(subentry_obj, "data", {}) or {}
            raw_visible = data.get("visible_device_ids")
            if isinstance(raw_visible, (list, tuple, set)):
                return tuple(_normalize_visible_ids(raw_visible))
            return ()

        tracker_subentry = _resolve_existing(tracker_key)
        tracker_visible = _existing_visible(tracker_subentry)
        if not tracker_visible and self._available_devices:
            tracker_visible = tuple(
                _normalize_visible_ids(device_id for _, device_id in self._available_devices)
            )

        service_payload = _build_subentry_payload(
            group_key=service_key,
            features=_SERVICE_FEATURE_PLATFORMS,
            entry_title=entry_title,
            has_google_home_filter=has_filter,
            feature_flags=feature_flags,
        )

        tracker_payload = _build_subentry_payload(
            group_key=tracker_key,
            features=_TRACKER_FEATURE_PLATFORMS,
            entry_title=tracker_title,
            has_google_home_filter=has_filter,
            feature_flags=feature_flags,
            visible_device_ids=tracker_visible,
        )

        service_subentry = _resolve_existing(service_key)

        context_map.setdefault(service_key, getattr(service_subentry, "subentry_id", None))
        context_map.setdefault(tracker_key, getattr(tracker_subentry, "subentry_id", None))

        if service_subentry is None:
            created_service = await type(self)._async_create_subentry(
                self,
                entry,
                data=service_payload,
                title=entry_title,
                unique_id=service_unique_id,
                subentry_type=SUBENTRY_TYPE_SERVICE,
            )
            if created_service is not None:
                context_map[service_key] = created_service.subentry_id
        else:
            await type(self)._async_update_subentry(
                self,
                entry,
                service_subentry,
                data=service_payload,
                title=entry_title,
                unique_id=service_unique_id,
            )
            context_map[service_key] = service_subentry.subentry_id

        tracker_subentry = _resolve_existing(tracker_key)
        if tracker_subentry is None:
            created_tracker = await type(self)._async_create_subentry(
                self,
                entry,
                data=tracker_payload,
                title=tracker_title,
                unique_id=tracker_unique_id,
                subentry_type=SUBENTRY_TYPE_TRACKER,
            )
            if created_tracker is not None:
                context_map[tracker_key] = created_tracker.subentry_id
        else:
            await type(self)._async_update_subentry(
                self,
                entry,
                tracker_subentry,
                data=tracker_payload,
                title=tracker_title,
                unique_id=tracker_unique_id,
            )
            context_map[tracker_key] = tracker_subentry.subentry_id

    async def _async_create_subentry(
        self,
        entry: ConfigEntry,
        *,
        data: dict[str, Any],
        title: str,
        unique_id: str | None,
        subentry_type: str,
    ) -> ConfigSubentry | None:
        """Create a config entry subentry using the best available API."""

        manager = getattr(self.hass, "config_entries", None)
        if manager is None:
            return None

        create_fn = getattr(manager, "async_create_subentry", None)
        if callable(create_fn):
            result = create_fn(
                entry,
                data=data,
                title=title,
                unique_id=unique_id,
                subentry_type=subentry_type,
            )
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, ConfigSubentry):
                return result

        add_fn = getattr(manager, "async_add_subentry", None)
        if (
            callable(add_fn) and ConfigSubentry is not None
        ):  # pragma: no cover - legacy fallback
            try:
                subentry = ConfigSubentry(  # type: ignore[call-arg]
                    data=MappingProxyType(dict(data)),
                    subentry_type=subentry_type,
                    title=title,
                    unique_id=unique_id,
                )
            except TypeError:  # pragma: no cover - legacy signature
                subentry = ConfigSubentry(  # type: ignore[call-arg]
                    data=MappingProxyType(dict(data)),
                    title=title,
                    unique_id=unique_id,
                )
            add_fn(entry, subentry)
            return subentry

        return None

    async def _async_update_subentry(
        self,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any],
        title: str,
        unique_id: str | None,
    ) -> None:
        """Update an existing subentry if the API supports it."""

        manager = getattr(self.hass, "config_entries", None)
        if manager is None:
            return

        update_fn = getattr(manager, "async_update_subentry", None)
        if not callable(update_fn):
            return

        result = update_fn(
            entry,
            subentry,
            data=data,
            title=title,
            unique_id=unique_id,
        )
        if inspect.isawaitable(result):
            await result

    def _lookup_subentry(
        self, entry: ConfigEntry, group_key: str
    ) -> ConfigSubentry | None:
        """Return the first subentry matching the requested group key."""

        for candidate in entry.subentries.values():
            if candidate.data.get("group_key") == group_key:
                return candidate
        return None


# ---------------------------
# Subentry Flow Handlers
# ---------------------------


class _BaseSubentryFlow(ConfigSubentryFlow):
    """Shared helpers for Google Find My config subentry flows."""

    _group_key: str
    _subentry_type: str
    _features: tuple[str, ...]

    def __init__(self, config_entry: ConfigEntry) -> None:
        try:
            super().__init__(config_entry)  # type: ignore[misc]
        except TypeError:  # pragma: no cover - legacy stub compatibility
            super().__init__()  # type: ignore[call-arg]
            self.config_entry = config_entry
        else:
            self.config_entry = getattr(self, "config_entry", config_entry)

    @property
    def _entry_id(self) -> str:
        return getattr(self.config_entry, "entry_id", "")

    def _resolve_existing(self) -> ConfigSubentry | None:
        candidate = getattr(self, "subentry", None)
        if isinstance(candidate, ConfigSubentry):
            return candidate
        for subentry in getattr(self.config_entry, "subentries", {}).values():
            if subentry.data.get("group_key") == self._group_key:
                return subentry
        return None

    def _current_options_payload(self) -> dict[str, Any]:
        payload = dict(getattr(self.config_entry, "options", {}))
        for key in (
            OPT_MAP_VIEW_TOKEN_EXPIRATION,
            OPT_GOOGLE_HOME_FILTER_ENABLED,
            OPT_ENABLE_STATS_ENTITIES,
            OPT_CONTRIBUTOR_MODE,
        ):
            if key not in payload and key in self.config_entry.data:
                payload[key] = self.config_entry.data[key]
        return payload

    def _defaults_for_entry(self) -> dict[str, Any]:
        defaults = dict(DEFAULT_OPTIONS)
        for key in (
            OPT_MAP_VIEW_TOKEN_EXPIRATION,
            OPT_GOOGLE_HOME_FILTER_ENABLED,
            OPT_ENABLE_STATS_ENTITIES,
            OPT_CONTRIBUTOR_MODE,
        ):
            if key in self.config_entry.data:
                defaults[key] = self.config_entry.data[key]
        return defaults

    def _entry_title(self) -> str:
        return getattr(self.config_entry, "title", None) or "Google Find My Device"

    def _visible_device_ids(self) -> tuple[str, ...]:
        subentry = self._resolve_existing()
        if subentry is None:
            return ()
        raw_visible = getattr(subentry, "data", {}).get("visible_device_ids")
        if isinstance(raw_visible, (list, tuple, set)):
            return tuple(_normalize_visible_ids(raw_visible))
        return ()

    def _build_payload(self) -> tuple[dict[str, Any], str, str]:
        options_payload = self._current_options_payload()
        defaults = self._defaults_for_entry()
        has_filter, feature_flags = _derive_feature_settings(
            options_payload=options_payload,
            defaults=defaults,
        )
        title = self._entry_title()
        visible_ids = self._visible_device_ids()
        payload = _build_subentry_payload(
            group_key=self._group_key,
            features=self._features,
            entry_title=title,
            has_google_home_filter=has_filter,
            feature_flags=feature_flags,
            visible_device_ids=visible_ids,
        )
        unique_id = f"{self._entry_id}-{self._group_key}"
        return payload, title, unique_id

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        payload, title, _ = self._build_payload()
        return self.async_create_entry(title=title, data=payload)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        payload, title, unique_id = self._build_payload()
        return self.async_update_and_abort(
            data=payload,
            title=title,
            unique_id=unique_id,
        )


class ServiceSubentryFlowHandler(_BaseSubentryFlow):
    """Config subentry flow for the hub/service feature group."""

    _group_key = SERVICE_SUBENTRY_KEY
    _subentry_type = SUBENTRY_TYPE_SERVICE
    _features = _SERVICE_FEATURE_PLATFORMS

    def _visible_device_ids(self) -> tuple[str, ...]:
        return ()


class TrackerSubentryFlowHandler(_BaseSubentryFlow):
    """Config subentry flow for tracked device feature groups."""

    _group_key = TRACKER_SUBENTRY_KEY
    _subentry_type = SUBENTRY_TYPE_TRACKER
    _features = _TRACKER_FEATURE_PLATFORMS

    def _entry_title(self) -> str:
        return "Google Find My devices"

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

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Display a small menu for settings, credentials refresh, or visibility."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["settings", "credentials", "visibility", "repairs"],
        )

    # ---------- Helpers for live API/cache access ----------
    def _get_entry_cache(self, entry: ConfigEntry) -> Any | None:
        """Proxy to the ConfigFlow cache lookup helper."""

        return ConfigFlow._get_entry_cache(self, entry)

    async def _async_clear_cached_aas_token(self, entry: ConfigEntry) -> None:
        """Proxy to the ConfigFlow cache-clearing helper."""

        await ConfigFlow._async_clear_cached_aas_token(self, entry)

    async def _async_build_api_from_entry(self, entry: ConfigEntry) -> GoogleFindMyAPI:
        """Construct API object from the live entry context (cache-first)."""
        cache = self._get_entry_cache(entry)
        if cache is not None:
            session = async_get_clientsession(self.hass)
            try:
                return GoogleFindMyAPI(cache=cache, session=session)  # type: ignore[call-arg]
            except TypeError:
                return GoogleFindMyAPI(cache=cache)  # type: ignore[call-arg]

        oauth = entry.data.get(CONF_OAUTH_TOKEN)
        email = entry.data.get(CONF_GOOGLE_EMAIL)
        if oauth and email:
            try:
                return GoogleFindMyAPI(oauth_token=oauth, google_email=email)  # type: ignore[call-arg]
            except TypeError:
                return GoogleFindMyAPI(token=oauth, email=email)  # type: ignore[call-arg]

        raise RuntimeError(
            "GoogleFindMyAPI requires either `cache=` or minimal flow credentials."
        )

    # ---------- Shared subentry helpers ----------
    def _gather_subentry_options(self) -> list[_SubentryOption]:
        """Return ordered subentry options available for selection."""

        entry = self.config_entry
        options: list[_SubentryOption] = []
        seen_keys: set[str] = set()

        subentries = getattr(entry, "subentries", None)
        if isinstance(subentries, dict):
            for subentry in subentries.values():
                data = dict(getattr(subentry, "data", {}) or {})
                raw_key = data.get("group_key")
                if isinstance(raw_key, str) and raw_key.strip():
                    key = raw_key.strip()
                else:
                    key = str(getattr(subentry, "subentry_id", "core_tracking"))
                label = (
                    getattr(subentry, "title", None)
                    or data.get("entry_title")
                    or key.replace("_", " ").title()
                )
                raw_visible = data.get("visible_device_ids")
                if isinstance(raw_visible, Iterable) and not isinstance(
                    raw_visible, (str, bytes)
                ):
                    visible = tuple(
                        str(item)
                        for item in raw_visible
                        if isinstance(item, str) and item
                    )
                else:
                    visible = ()
                options.append(
                    _SubentryOption(
                        key=key,
                        label=str(label),
                        subentry=subentry,
                        visible_device_ids=visible,
                    )
                )
                seen_keys.add(key)

        if not options:
            title = getattr(entry, "title", None) or "Core tracking"
            options.append(
                _SubentryOption(
                    key="core_tracking",
                    label=str(title),
                    subentry=None,
                    visible_device_ids=(),
                )
            )

        options.sort(key=lambda opt: opt.label.lower())
        return options

    def _subentry_choice_map(
        self,
    ) -> tuple[dict[str, str], dict[str, _SubentryOption]]:
        """Return mapping of subentry keys to labels and option objects."""

        options = self._gather_subentry_options()
        label_map = {opt.key: opt.label for opt in options}
        option_map = {opt.key: opt for opt in options}
        return label_map, option_map

    @staticmethod
    def _default_subentry_key(choices: dict[str, str]) -> str:
        """Return the default subentry key for UI defaults."""

        if "core_tracking" in choices:
            return "core_tracking"
        return next(iter(choices), "core_tracking")

    async def _async_update_feature_group_subentry(
        self,
        entry: ConfigEntry,
        subentry_option: _SubentryOption,
        options_payload: Mapping[str, Any],
    ) -> None:
        """Update feature group metadata on the selected subentry."""

        subentry = subentry_option.subentry
        if subentry is None:
            return

        data = dict(getattr(subentry, "data", {}) or {})
        data.setdefault("group_key", subentry_option.key)

        raw_flags = data.get("feature_flags")
        if isinstance(raw_flags, Mapping):
            feature_flags = {str(key): raw_flags[key] for key in raw_flags}
        else:
            feature_flags = {}

        if OPT_ENABLE_STATS_ENTITIES is not None:
            if OPT_ENABLE_STATS_ENTITIES in options_payload:
                feature_flags[OPT_ENABLE_STATS_ENTITIES] = bool(
                    options_payload[OPT_ENABLE_STATS_ENTITIES]
                )
        if OPT_MAP_VIEW_TOKEN_EXPIRATION in options_payload:
            feature_flags[OPT_MAP_VIEW_TOKEN_EXPIRATION] = bool(
                options_payload[OPT_MAP_VIEW_TOKEN_EXPIRATION]
            )
        if OPT_GOOGLE_HOME_FILTER_ENABLED is not None and (
            OPT_GOOGLE_HOME_FILTER_ENABLED in options_payload
        ):
            feature_flags[OPT_GOOGLE_HOME_FILTER_ENABLED] = bool(
                options_payload[OPT_GOOGLE_HOME_FILTER_ENABLED]
            )
            data["has_google_home_filter"] = bool(
                options_payload[OPT_GOOGLE_HOME_FILTER_ENABLED]
            )
        if OPT_CONTRIBUTOR_MODE in options_payload:
            feature_flags[OPT_CONTRIBUTOR_MODE] = options_payload[OPT_CONTRIBUTOR_MODE]

        if feature_flags:
            data["feature_flags"] = feature_flags

        if "entry_title" in data or getattr(entry, "title", None):
            data["entry_title"] = getattr(entry, "title", None) or data.get(
                "entry_title"
            )

        await ConfigFlow._async_update_subentry(  # type: ignore[misc]
            self,
            entry,
            subentry,
            data=data,
            title=getattr(subentry, "title", None) or data.get("entry_title"),
            unique_id=getattr(subentry, "unique_id", None),
        )

    async def _async_refresh_subentry_entry_title(
        self, entry: ConfigEntry, subentry_option: _SubentryOption
    ) -> None:
        """Ensure the subentry reflects the current entry title."""

        subentry = subentry_option.subentry
        if subentry is None:
            return

        data = dict(getattr(subentry, "data", {}) or {})
        new_title = getattr(entry, "title", None)
        if not new_title:
            return
        if (
            data.get("entry_title") == new_title
            and getattr(subentry, "title", None) == new_title
        ):
            return
        data["entry_title"] = new_title
        await ConfigFlow._async_update_subentry(  # type: ignore[misc]
            self,
            entry,
            subentry,
            data=data,
            title=new_title,
            unique_id=getattr(subentry, "unique_id", None),
        )

    async def _async_assign_devices_to_subentry(
        self, entry: ConfigEntry, target_key: str, device_ids: list[str]
    ) -> set[str]:
        """Assign devices to the target subentry while removing from others."""

        if not device_ids:
            return set()

        changed: set[str] = set()
        options = self._gather_subentry_options()

        for option in options:
            subentry = option.subentry
            if subentry is None:
                continue

            data = dict(getattr(subentry, "data", {}) or {})
            raw_visible = data.get("visible_device_ids")
            if isinstance(raw_visible, Iterable) and not isinstance(
                raw_visible, (str, bytes)
            ):
                visible = [
                    str(item) for item in raw_visible if isinstance(item, str) and item
                ]
            else:
                visible = list(option.visible_device_ids)

            before = list(visible)
            if option.key == target_key:
                for dev_id in device_ids:
                    if dev_id not in visible:
                        visible.append(dev_id)
            else:
                visible = [dev for dev in visible if dev not in device_ids]

            if visible == before:
                continue

            data["visible_device_ids"] = tuple(sorted(dict.fromkeys(visible)))
            await ConfigFlow._async_update_subentry(  # type: ignore[misc]
                self,
                entry,
                subentry,
                data=data,
                title=getattr(subentry, "title", None),
                unique_id=getattr(subentry, "unique_id", None),
            )
            changed.add(option.key)

        return changed

    async def _async_remove_subentry(
        self, entry: ConfigEntry, subentry_option: _SubentryOption
    ) -> bool:
        """Remove a subentry using the config entries API when available."""

        subentry_id = subentry_option.subentry_id
        if not subentry_id:
            return False

        manager = getattr(self.hass, "config_entries", None)
        remove_fn = getattr(manager, "async_remove_subentry", None)
        if not callable(remove_fn):
            return False

        result = remove_fn(entry, subentry_id)
        if inspect.isawaitable(result):
            result = await result
        return bool(result)

    def _device_choice_map(self) -> dict[str, str]:
        """Return mapping of device IDs to display labels for UI selectors."""

        entry = self.config_entry
        choices: dict[str, str] = {}

        runtime = getattr(entry, "runtime_data", None)
        coordinator = None
        if runtime is not None:
            coordinator = getattr(runtime, "coordinator", None) or getattr(
                runtime, "data", None
            )
        if coordinator is None:
            coordinator = getattr(entry, "runtime_data", None)

        datasets: list[Iterable[Any]] = []
        if coordinator is not None:
            data_attr = getattr(coordinator, "data", None)
            if isinstance(data_attr, Iterable):
                datasets.append(data_attr)

        for dataset in datasets:
            for candidate in dataset:
                if not isinstance(candidate, Mapping):
                    continue
                device_id = candidate.get("device_id") or candidate.get("id")
                if not isinstance(device_id, str) or not device_id:
                    continue
                name = candidate.get("name")
                if not isinstance(name, str) or not name.strip():
                    name = device_id
                choices.setdefault(device_id, name)

        if not choices:
            for option in self._gather_subentry_options():
                for device_id in option.visible_device_ids:
                    choices.setdefault(device_id, device_id)

        return dict(sorted(choices.items(), key=lambda item: item[1].lower()))

    # ---------- Settings (non-secret) ----------
    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Update non-secret options in a single form."""
        errors: dict[str, str] = {}

        entry = self.config_entry
        opt = entry.options
        dat = entry.data

        def _get(cur_key, default_val):
            return opt.get(cur_key, dat.get(cur_key, default_val))

        current = {
            OPT_LOCATION_POLL_INTERVAL: _get(
                OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL
            ),
            OPT_DEVICE_POLL_DELAY: _get(
                OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY
            ),
            OPT_MIN_ACCURACY_THRESHOLD: _get(
                OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD
            ),
            OPT_MAP_VIEW_TOKEN_EXPIRATION: _get(
                OPT_MAP_VIEW_TOKEN_EXPIRATION, DEFAULT_MAP_VIEW_TOKEN_EXPIRATION
            ),
            OPT_CONTRIBUTOR_MODE: _get(OPT_CONTRIBUTOR_MODE, DEFAULT_CONTRIBUTOR_MODE),
        }
        if (
            OPT_MOVEMENT_THRESHOLD is not None
            and DEFAULT_MOVEMENT_THRESHOLD is not None
        ):
            current[OPT_MOVEMENT_THRESHOLD] = _get(
                OPT_MOVEMENT_THRESHOLD, DEFAULT_MOVEMENT_THRESHOLD
            )
        if (
            OPT_GOOGLE_HOME_FILTER_ENABLED is not None
            and DEFAULT_GOOGLE_HOME_FILTER_ENABLED is not None
        ):
            current[OPT_GOOGLE_HOME_FILTER_ENABLED] = _get(
                OPT_GOOGLE_HOME_FILTER_ENABLED, DEFAULT_GOOGLE_HOME_FILTER_ENABLED
            )
        if (
            OPT_GOOGLE_HOME_FILTER_KEYWORDS is not None
            and DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS is not None
        ):
            current[OPT_GOOGLE_HOME_FILTER_KEYWORDS] = _get(
                OPT_GOOGLE_HOME_FILTER_KEYWORDS, DEFAULT_GOOGLE_HOME_FILTER_KEYWORDS
            )
        if (
            OPT_ENABLE_STATS_ENTITIES is not None
            and DEFAULT_ENABLE_STATS_ENTITIES is not None
        ):
            current[OPT_ENABLE_STATS_ENTITIES] = _get(
                OPT_ENABLE_STATS_ENTITIES, DEFAULT_ENABLE_STATS_ENTITIES
            )

        choices, option_map = self._subentry_choice_map()
        default_subentry = self._default_subentry_key(choices)

        fields: dict[Any, Any] = {
            vol.Required(_FIELD_SUBENTRY, default=default_subentry): vol.In(choices)
        }
        option_markers: list[Any] = []

        def _register(marker: Any, validator: Any) -> None:
            fields[marker] = validator
            option_markers.append(marker)

        _register(
            vol.Optional(OPT_LOCATION_POLL_INTERVAL),
            vol.All(vol.Coerce(int), vol.Range(min=60, max=3600)),
        )
        _register(
            vol.Optional(OPT_DEVICE_POLL_DELAY),
            vol.All(vol.Coerce(int), vol.Range(min=1, max=60)),
        )
        _register(
            vol.Optional(OPT_MIN_ACCURACY_THRESHOLD),
            vol.All(vol.Coerce(int), vol.Range(min=25, max=500)),
        )
        _register(vol.Optional(OPT_MAP_VIEW_TOKEN_EXPIRATION), bool)
        if OPT_MOVEMENT_THRESHOLD is not None:
            _register(
                vol.Optional(OPT_MOVEMENT_THRESHOLD),
                vol.All(vol.Coerce(int), vol.Range(min=10, max=200)),
            )
        if OPT_GOOGLE_HOME_FILTER_ENABLED is not None:
            _register(vol.Optional(OPT_GOOGLE_HOME_FILTER_ENABLED), bool)
        if OPT_GOOGLE_HOME_FILTER_KEYWORDS is not None:
            _register(vol.Optional(OPT_GOOGLE_HOME_FILTER_KEYWORDS), str)
        if OPT_ENABLE_STATS_ENTITIES is not None:
            _register(vol.Optional(OPT_ENABLE_STATS_ENTITIES), bool)
        _register(
            vol.Optional(OPT_CONTRIBUTOR_MODE),
            vol.In([CONTRIBUTOR_MODE_HIGH_TRAFFIC, CONTRIBUTOR_MODE_IN_ALL_AREAS]),
        )

        base_schema = vol.Schema(fields)
        schema_with_defaults = self.add_suggested_values_to_schema(base_schema, current)

        if user_input is not None:
            selected_key = str(user_input.get(_FIELD_SUBENTRY, default_subentry))
            if selected_key not in choices:
                errors[_FIELD_SUBENTRY] = "invalid_subentry"
            else:
                new_options = dict(entry.options)
                for marker in option_markers:
                    real_key = next(iter(getattr(marker, "schema", {marker})))
                    if real_key in user_input:
                        new_options[real_key] = user_input[real_key]
                    else:
                        new_options[real_key] = current.get(real_key)
                new_options[OPT_OPTIONS_SCHEMA_VERSION] = 2

                subentry_option = option_map.get(selected_key)
                if subentry_option is not None:
                    await self._async_update_feature_group_subentry(
                        entry, subentry_option, new_options
                    )

                return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(
            step_id="settings", data_schema=schema_with_defaults, errors=errors
        )

    # ---------- Visibility (restore ignored devices) ----------
    async def async_step_visibility(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Display ignored devices and allow restoring them (remove from OPT_IGNORED_DEVICES)."""
        entry = self.config_entry
        options = dict(entry.options)
        raw = (
            options.get(OPT_IGNORED_DEVICES)
            or entry.data.get(OPT_IGNORED_DEVICES)
            or {}
        )
        ignored_map, _migrated = coerce_ignored_mapping(raw)

        if not ignored_map:
            return self.async_abort(reason="no_ignored_devices")

        if callable(ignored_choices_for_ui):
            choices = ignored_choices_for_ui(ignored_map)  # type: ignore[misc]
        else:
            choices = {
                dev_id: (meta.get("name") or dev_id)
                for dev_id, meta in ignored_map.items()
            }

        subentry_choices, _ = self._subentry_choice_map()
        default_subentry = self._default_subentry_key(subentry_choices)

        schema = vol.Schema(
            {
                vol.Required(_FIELD_SUBENTRY, default=default_subentry): vol.In(
                    subentry_choices
                ),
                vol.Optional("unignore_devices", default=[]): cv.multi_select(choices),
            }
        )

        if user_input is not None:
            selected_key = str(user_input.get(_FIELD_SUBENTRY, default_subentry))
            if selected_key not in subentry_choices:
                return self.async_show_form(
                    step_id="visibility",
                    data_schema=schema,
                    errors={_FIELD_SUBENTRY: "invalid_subentry"},
                )

            raw_restore = user_input.get("unignore_devices") or []
            if not isinstance(raw_restore, list):
                raw_restore = list(raw_restore)
            to_restore = [
                str(dev_id) for dev_id in raw_restore if isinstance(dev_id, str)
            ]
            for dev_id in to_restore:
                ignored_map.pop(dev_id, None)

            new_options = dict(entry.options)
            new_options[OPT_IGNORED_DEVICES] = ignored_map
            new_options[OPT_OPTIONS_SCHEMA_VERSION] = 2

            if to_restore:
                await self._async_assign_devices_to_subentry(
                    entry, selected_key, to_restore
                )

            return self.async_create_entry(title="", data=new_options)

        return self.async_show_form(step_id="visibility", data_schema=schema)

    async def async_step_repairs(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Entry point for subentry repair operations."""

        subentry_choices, _ = self._subentry_choice_map()
        if not subentry_choices:
            return self.async_abort(reason="repairs_no_subentries")

        return self.async_show_menu(
            step_id="repairs",
            menu_options=["repairs_move", "repairs_delete"],
        )

    async def async_step_repairs_move(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Assign selected devices to a subentry, removing them from others."""

        subentry_choices, _ = self._subentry_choice_map()
        if not subentry_choices:
            return self.async_abort(reason="repairs_no_subentries")

        default_subentry = self._default_subentry_key(subentry_choices)
        device_choices = self._device_choice_map()

        schema = vol.Schema(
            {
                vol.Required(_FIELD_REPAIR_TARGET, default=default_subentry): vol.In(
                    subentry_choices
                ),
                vol.Optional(_FIELD_REPAIR_DEVICES, default=[]): cv.multi_select(
                    device_choices
                ),
            }
        )

        if user_input is not None:
            target_key = str(user_input.get(_FIELD_REPAIR_TARGET, default_subentry))
            if target_key not in subentry_choices:
                return self.async_show_form(
                    step_id="repairs_move",
                    data_schema=schema,
                    errors={_FIELD_REPAIR_TARGET: "invalid_subentry"},
                )

            raw_devices = user_input.get(_FIELD_REPAIR_DEVICES) or []
            if not isinstance(raw_devices, list):
                raw_devices = list(raw_devices)
            device_ids = [
                str(dev_id) for dev_id in raw_devices if isinstance(dev_id, str)
            ]

            if not device_ids:
                return self.async_abort(reason="repair_no_devices")

            changed = await self._async_assign_devices_to_subentry(
                self.config_entry, target_key, device_ids
            )

            placeholders = {
                "subentry": subentry_choices[target_key],
                "count": str(len(device_ids)),
            }

            if not changed:
                return self.async_abort(
                    reason="subentry_move_success",
                    description_placeholders=placeholders,
                )

            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )
            return self.async_abort(
                reason="subentry_move_success", description_placeholders=placeholders
            )

        return self.async_show_form(step_id="repairs_move", data_schema=schema)

    async def async_step_repairs_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Remove a subentry after optionally moving its devices to a fallback."""

        subentry_choices, option_map = self._subentry_choice_map()
        removable_choices = {
            key: label
            for key, label in subentry_choices.items()
            if option_map[key].subentry
        }
        if not removable_choices or len(removable_choices) <= 1:
            return self.async_abort(reason="subentry_delete_invalid")

        schema = vol.Schema(
            {
                vol.Required(_FIELD_REPAIR_DELETE): vol.In(removable_choices),
                vol.Required(
                    _FIELD_REPAIR_FALLBACK,
                    default=self._default_subentry_key(subentry_choices),
                ): vol.In(subentry_choices),
            }
        )

        if user_input is not None:
            errors: dict[str, str] = {}
            target_key = str(user_input.get(_FIELD_REPAIR_DELETE, ""))
            fallback_key = str(user_input.get(_FIELD_REPAIR_FALLBACK, ""))

            if target_key not in removable_choices:
                errors[_FIELD_REPAIR_DELETE] = "invalid_subentry"
            if fallback_key not in subentry_choices or fallback_key == target_key:
                errors[_FIELD_REPAIR_FALLBACK] = "invalid_subentry"

            if errors:
                return self.async_show_form(
                    step_id="repairs_delete", data_schema=schema, errors=errors
                )

            devices = list(option_map[target_key].visible_device_ids)
            if devices:
                await self._async_assign_devices_to_subentry(
                    self.config_entry, fallback_key, devices
                )

            removed = await self._async_remove_subentry(
                self.config_entry, option_map[target_key]
            )
            if not removed:
                return self.async_abort(reason="subentry_remove_failed")

            placeholders = {
                "subentry": subentry_choices[target_key],
                "fallback": subentry_choices[fallback_key],
                "count": str(len(devices)),
            }

            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self.config_entry.entry_id)
            )
            return self.async_abort(
                reason="subentry_delete_success",
                description_placeholders=placeholders,
            )

        return self.async_show_form(step_id="repairs_delete", data_schema=schema)

    # ---------- Credentials refresh ----------
    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Refresh credentials without exposing current ones.

        IMPORTANT CHANGE:
        - This step NO LONGER allows changing the Google email to avoid cross-account
          mutations that can break unique_id semantics. Use a new integration entry
          to add another account.
        """
        errors: dict[str, str] = {}

        subentry_choices, option_map = self._subentry_choice_map()
        default_subentry = self._default_subentry_key(subentry_choices)

        if selector is not None:
            schema = vol.Schema(
                {
                    vol.Required(_FIELD_SUBENTRY, default=default_subentry): vol.In(
                        subentry_choices
                    ),
                    vol.Optional("new_secrets_json"): selector(
                        {"text": {"multiline": True}}
                    ),
                    vol.Optional("new_oauth_token"): str,
                }
            )
        else:
            schema = vol.Schema(
                {
                    vol.Required(_FIELD_SUBENTRY, default=default_subentry): vol.In(
                        subentry_choices
                    ),
                    vol.Optional("new_secrets_json"): str,
                    vol.Optional("new_oauth_token"): str,
                }
            )

        if user_input is not None:
            selected_key = str(user_input.get(_FIELD_SUBENTRY, default_subentry))
            if selected_key not in subentry_choices:
                errors[_FIELD_SUBENTRY] = "invalid_subentry"
            else:
                has_secrets = bool((user_input.get("new_secrets_json") or "").strip())
                has_token = bool((user_input.get("new_oauth_token") or "").strip())
                if (has_secrets and has_token) or (not has_secrets and not has_token):
                    errors["base"] = "choose_one"
                else:
                    try:
                        entry = self.config_entry
                        email = entry.data.get(CONF_GOOGLE_EMAIL)
                        selected_option = option_map.get(selected_key)

                        async def _finalize_success(
                            updated_data: dict[str, Any],
                        ) -> FlowResult:
                            await self._async_clear_cached_aas_token(entry)
                            self.hass.config_entries.async_update_entry(
                                entry, data=updated_data
                            )
                            if selected_option is not None:
                                await self._async_refresh_subentry_entry_title(
                                    entry, selected_option
                                )
                            self.hass.async_create_task(
                                self.hass.config_entries.async_reload(entry.entry_id)
                            )
                            return self.async_abort(reason="reconfigure_successful")

                        if has_token:
                            token = (user_input.get("new_oauth_token") or "").strip()
                            if not (
                                _token_plausible(token)
                                and not _disqualifies_for_persistence(token)
                            ):
                                errors["base"] = "invalid_token"
                            else:
                                chosen = await async_pick_working_token(
                                    email, [("manual", token)]
                                )
                                if not chosen:
                                    _LOGGER.warning(
                                        "Token validation failed for %s. No working token found among candidates (%s).",
                                        _mask_email_for_logs(email),
                                        _cand_labels([("manual", token)]),
                                    )
                                    errors["base"] = "cannot_connect"
                                else:
                                    updated_data = {
                                        **entry.data,
                                        DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                                        CONF_OAUTH_TOKEN: chosen,
                                    }
                                    updated_data.pop(DATA_SECRET_BUNDLE, None)
                                    if isinstance(chosen, str) and chosen.startswith(
                                        "aas_et/"
                                    ):
                                        updated_data[DATA_AAS_TOKEN] = chosen
                                    else:
                                        updated_data.pop(DATA_AAS_TOKEN, None)
                                    return await _finalize_success(updated_data)

                        if has_secrets and "new_secrets_json" in user_input:
                            try:
                                parsed = json.loads(user_input["new_secrets_json"])
                                if not isinstance(parsed, dict):
                                    raise TypeError()
                            except Exception:
                                errors["new_secrets_json"] = "invalid_json"
                            else:
                                cands = _extract_oauth_candidates_from_secrets(parsed)
                                if not cands:
                                    errors["base"] = "invalid_token"
                                else:
                                    chosen = await async_pick_working_token(
                                        email, cands, secrets_bundle=parsed
                                    )
                                    if not chosen:
                                        _LOGGER.warning(
                                            "Token validation failed for %s. No working token found among candidates (%s).",
                                            _mask_email_for_logs(email),
                                            _cand_labels(cands),
                                        )
                                        errors["base"] = "cannot_connect"
                                    else:
                                        to_persist = chosen
                                        if _disqualifies_for_persistence(to_persist):
                                            alt = next(
                                                (
                                                    v
                                                    for (_src, v) in cands
                                                    if not _disqualifies_for_persistence(
                                                        v
                                                    )
                                                ),
                                                None,
                                            )
                                            if alt:
                                                to_persist = alt
                                        updated_data = {
                                            **entry.data,
                                            DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                                            CONF_OAUTH_TOKEN: to_persist,
                                            DATA_SECRET_BUNDLE: parsed,
                                        }
                                        if isinstance(
                                            to_persist, str
                                        ) and to_persist.startswith("aas_et/"):
                                            updated_data[DATA_AAS_TOKEN] = to_persist
                                        else:
                                            updated_data.pop(DATA_AAS_TOKEN, None)
                                        return await _finalize_success(updated_data)
                    except Exception as err2:  # noqa: BLE001
                        if _is_multi_entry_guard_error(err2):
                            entry = self.config_entry
                            if has_token:
                                token_value = user_input["new_oauth_token"].strip()
                                updated_data = {
                                    **entry.data,
                                    DATA_AUTH_METHOD: _AUTH_METHOD_INDIVIDUAL,
                                    CONF_OAUTH_TOKEN: token_value,
                                }
                                updated_data.pop(DATA_SECRET_BUNDLE, None)
                                if token_value.startswith("aas_et/"):
                                    updated_data[DATA_AAS_TOKEN] = token_value
                                else:
                                    updated_data.pop(DATA_AAS_TOKEN, None)
                            else:
                                parsed = json.loads(user_input["new_secrets_json"])
                                cands = _extract_oauth_candidates_from_secrets(parsed)
                                token_first = cands[0][1] if cands else ""
                                updated_data = {
                                    **entry.data,
                                    DATA_AUTH_METHOD: _AUTH_METHOD_SECRETS,
                                    CONF_OAUTH_TOKEN: token_first,
                                    DATA_SECRET_BUNDLE: parsed,
                                }
                                if isinstance(
                                    token_first, str
                                ) and token_first.startswith("aas_et/"):
                                    updated_data[DATA_AAS_TOKEN] = token_first
                                else:
                                    updated_data.pop(DATA_AAS_TOKEN, None)
                            return await _finalize_success(updated_data)
                        errors["base"] = _map_api_exc_to_error_key(err2)

        return self.async_show_form(
            step_id="credentials", data_schema=schema, errors=errors
        )


# ---------- Custom exceptions ----------
class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect to the remote service."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate invalid authentication was provided."""
