# custom_components/googlefindmy/__init__.py

"""Google Find My Device integration for Home Assistant.

Version: 2.6.6 — Multi-account enabled (E3) + owner-index routing attach
- Multi-account support: multiple config entries are allowed concurrently.
- Duplicate-account protection: if two entries use the same Google email, we raise a
  Repair issue and abort the later entry to avoid mixing credentials/state.
- Entry-scoped TokenCache usage only (no global facade calls).
- Device owner index scaffold (entry_id → canonical_id mapping container).
- Prepared (not executed) migration for entry-scoped device identifiers.
- NEW: Attach HA context to the shared FCM receiver to enable owner-index fallback routing.

Highlights (cumulative)
-----------------------
- Entry-scoped TokenCache (HA Store backend) with migration from legacy secrets.json.
- Multi-entry: allow multiple *active* entries; prevent duplicate entries targeting
  the same Google account (email) across entries.
- Deterministic default-entry choice (previous behavior) REMOVED for MA: we do not set a
  TokenCache "default" in this module; all reads/writes are entry-scoped.
- One-time migration that namespaces entity unique_ids by entry_id (idempotent, collision-aware).
- Services are registered at integration level (async_setup) so they are always visible.
- Clean lifecycle: refcounted shared FCM receiver; coordinator shutdown & cache flush on unload.
- Defensive logging: redact tokens in URLs; never log PII (no coordinates/secrets).

Notes
-----
This module aims to be self-documenting. All public functions include precise docstrings
(purpose, parameters, errors, security considerations). Keep comments/docstrings in English.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import socket
import time
from contextlib import suppress
from datetime import datetime
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, TypedDict, TypeVar, TypeGuard, cast
from collections import defaultdict
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Mapping,
    Sequence,
)
from types import MappingProxyType, SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from weakref import WeakKeyDictionary

from .ProtoDecoders import Common_pb2, DeviceUpdate_pb2, LocationReportsUpload_pb2

from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigSubentry
try:  # pragma: no cover - ConfigEntryDisabler introduced in HA 2023.12
    from homeassistant.config_entries import ConfigEntryDisabler as _ConfigEntryDisabler
except ImportError:  # pragma: no cover - legacy Home Assistant builds
    _ConfigEntryDisabler = None
try:  # pragma: no cover - UnknownEntry introduced alongside subentry helpers
    from homeassistant.config_entries import UnknownEntry as _ConfigUnknownEntry
except ImportError:  # pragma: no cover - legacy Home Assistant builds
    _ConfigUnknownEntry = None
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    Platform,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant

try:
    from homeassistant.helpers.entity import split_entity_id
except ImportError:  # pragma: no cover - fallback for legacy or test environments
    try:
        from homeassistant.core import split_entity_id
    except ImportError:  # pragma: no cover - minimal shim for isolated tests

        def split_entity_id(entity_id: str) -> tuple[str, str]:
            """Split an entity_id into its domain and object ID parts."""

            if "." not in entity_id:
                raise ValueError(entity_id)
            domain, object_id = entity_id.split(".", 1)
            return domain, object_id
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
if TYPE_CHECKING:
    from homeassistant.helpers.entity import Entity
else:  # pragma: no cover - test environments without full Home Assistant
    try:
        from homeassistant.helpers.entity import Entity
    except (ImportError, AttributeError):

        class Entity:  # type: ignore[too-many-ancestors, override]
            """Minimal placeholder for Home Assistant's Entity base class."""

            __slots__ = ()

from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.storage import Store

# Token cache (entry-scoped HA Store-backed cache + registry/facade)
from .Auth.token_cache import (
    TokenCache,
    _register_instance,
    _unregister_instance,
)

# Username key normalization
from .Auth.username_provider import username_string

# Shared FCM provider (HA-managed singleton)
from .NovaApi.ExecuteAction.LocateTracker.location_request import (
    register_fcm_receiver_provider as loc_register_fcm_provider,
    unregister_fcm_receiver_provider as loc_unregister_fcm_provider,
)
from .const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    CONFIG_ENTRY_VERSION as CONFIG_ENTRY_VERSION,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    DATA_SECRET_BUNDLE,
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MIN_POLL_INTERVAL,
    DEFAULT_OPTIONS,
    DEFAULT_DELETE_CACHES_ON_REMOVE,
    DOMAIN,
    OPTION_KEYS,
    OPT_DELETE_CACHES_ON_REMOVE,
    OPT_CONTRIBUTOR_MODE,
    OPT_ALLOW_HISTORY_FALLBACK,
    OPT_DEVICE_POLL_DELAY,
    OPT_IGNORED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MIN_POLL_INTERVAL,
    OPT_OPTIONS_SCHEMA_VERSION,
    DEFAULT_CONTRIBUTOR_MODE,
    CONTRIBUTOR_MODE_HIGH_TRAFFIC,
    CONTRIBUTOR_MODE_IN_ALL_AREAS,
    CACHE_KEY_CONTRIBUTOR_MODE,
    CACHE_KEY_LAST_MODE_SWITCH,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SERVICE_SUBENTRY_TRANSLATION_KEY,
    LEGACY_SERVICE_IDENTIFIER,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    TRACKER_SUBENTRY_TRANSLATION_KEY,
    coerce_ignored_mapping,
    service_device_identifier,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .email import normalize_email, unique_account_id

# Eagerly import diagnostics to prevent blocking calls on-demand
from . import diagnostics  # noqa: F401

try:  # pragma: no cover - OperationNotAllowed introduced alongside HA subentry retries
    from homeassistant.config_entries import OperationNotAllowed as _OperationNotAllowed
except ImportError:  # pragma: no cover - legacy Home Assistant builds
    _FallbackHAError = cast("type[Exception]", HomeAssistantError)

    _OperationNotAllowed = cast(
        "type[Exception]",
        type(
            "OperationNotAllowed",
            (_FallbackHAError,),
            {
                "__doc__": (
                    "Fallback OperationNotAllowed placeholder for pre-subentry Home Assistant cores."
                ),
                "__module__": __name__,
            },
        ),
    )

OperationNotAllowed = cast("type[Exception]", _OperationNotAllowed)

UnknownEntry: type[Exception]
if _ConfigUnknownEntry is None:  # pragma: no cover - legacy Home Assistant builds
    UnknownEntry = cast(
        "type[Exception]",
        type(
            "UnknownEntry",
            (cast("type[Exception]", HomeAssistantError),),
            {
                "__doc__": "Fallback UnknownEntry placeholder for pre-subentry Home Assistant cores.",
                "__module__": __name__,
            },
        ),
    )
else:
    UnknownEntry = cast("type[Exception]", _ConfigUnknownEntry)

__all__ = [
    "Common_pb2",
    "DeviceUpdate_pb2",
    "LocationReportsUpload_pb2",
]


def __getattr__(name: str) -> Any:
    """Expose ProtoDecoder modules at the package root for compatibility."""

    if name in __all__:
        return globals()[name]
    raise AttributeError(name)


CloudDiscoveryRuntimeCallable = Callable[[HomeAssistant], Mapping[str, Any]]
TriggerCloudDiscoveryCallable = Callable[..., Awaitable[Any]]
RedactAccountForLogCallable = Callable[..., str]
if TYPE_CHECKING:
    from homeassistant.helpers.entity_registry import (
        RegistryEntryDisabler as RegistryEntryDisablerType,
    )

    from .NovaApi.ExecuteAction.LocateTracker.location_request import (
        FcmReceiverProtocol as NovaFcmReceiverProtocol,
    )
    from .api import (
        FcmReceiverProtocol as ApiFcmReceiverProtocol,
        register_fcm_receiver_provider as ApiRegisterFcmProviderType,
        unregister_fcm_receiver_provider as ApiUnregisterFcmProviderType,
    )
    from .Auth.fcm_receiver_ha import FcmReceiverHA as FcmReceiverHAType
    from .coordinator import GoogleFindMyCoordinator as GoogleFindMyCoordinatorType
    from .discovery import (
        DiscoveryManager as DiscoveryManagerType,
        async_initialize_discovery_runtime as AsyncInitializeDiscoveryRuntimeType,
        _cloud_discovery_runtime as cloud_discovery_runtime_impl,
        _redact_account_for_log as redact_account_for_log_impl,
        _trigger_cloud_discovery as trigger_cloud_discovery_impl,
    )
    from .map_view import (
        GoogleFindMyMapRedirectView as GoogleFindMyMapRedirectViewType,
        GoogleFindMyMapView as GoogleFindMyMapViewType,
    )
    from .services import async_register_services as AsyncRegisterServicesType

    GoogleFindMyCoordinator = GoogleFindMyCoordinatorType
    DiscoveryManager = DiscoveryManagerType
    GoogleFindMyMapView = GoogleFindMyMapViewType
    GoogleFindMyMapRedirectView = GoogleFindMyMapRedirectViewType
    async_register_services = AsyncRegisterServicesType
    async_initialize_discovery_runtime = AsyncInitializeDiscoveryRuntimeType
    api_register_fcm_provider = ApiRegisterFcmProviderType
    api_unregister_fcm_provider = ApiUnregisterFcmProviderType
    _cloud_discovery_runtime_callable = cast(
        CloudDiscoveryRuntimeCallable, cloud_discovery_runtime_impl
    )
    _redact_account_for_log_callable = cast(
        RedactAccountForLogCallable, redact_account_for_log_impl
    )
    _trigger_cloud_discovery_callable = cast(
        TriggerCloudDiscoveryCallable, trigger_cloud_discovery_impl
    )
else:
    from typing import Any as RegistryEntryDisablerType
    from typing import Any as FcmReceiverHAType

    NovaFcmReceiverProtocol = FcmReceiverHAType
    ApiFcmReceiverProtocol = FcmReceiverHAType

    def _runtime_imports_not_initialized(*_args: Any, **_kwargs: Any) -> Any:
        """Raise when runtime-only imports are used before initialization."""

        raise RuntimeError("Runtime components not initialized")

    class _GoogleFindMyCoordinatorPlaceholder:
        """Placeholder until the coordinator module is imported."""

    class _DiscoveryManagerPlaceholder:
        """Placeholder until the discovery module is imported."""

    GoogleFindMyCoordinator: type[Any] = cast(
        type[Any], _GoogleFindMyCoordinatorPlaceholder
    )
    DiscoveryManager: type[Any] = cast(type[Any], _DiscoveryManagerPlaceholder)
    GoogleFindMyMapView: type[Any] = cast(
        type[Any], type("GoogleFindMyMapViewPlaceholder", (object,), {})
    )
    GoogleFindMyMapRedirectView: type[Any] = cast(
        type[Any], type("GoogleFindMyMapRedirectViewPlaceholder", (object,), {})
    )

    api_register_fcm_provider: Callable[[Callable[[], ApiFcmReceiverProtocol]], None] = cast(
        Callable[[Callable[[], ApiFcmReceiverProtocol]], None],
        _runtime_imports_not_initialized,
    )
    api_unregister_fcm_provider: Callable[[], None] = cast(
        Callable[[], None], _runtime_imports_not_initialized
    )
    async_register_services: Callable[
        [HomeAssistant, Mapping[str, Any]], Awaitable[None]
    ] = cast(
        Callable[[HomeAssistant, Mapping[str, Any]], Awaitable[None]],
        _runtime_imports_not_initialized,
    )
    async_initialize_discovery_runtime: Callable[
        [HomeAssistant], Awaitable[Any]
    ] = cast(
        Callable[[HomeAssistant], Awaitable[Any]],
        _runtime_imports_not_initialized,
    )
    _cloud_discovery_runtime_callable: CloudDiscoveryRuntimeCallable = cast(
        CloudDiscoveryRuntimeCallable,
        _runtime_imports_not_initialized,
    )
    _trigger_cloud_discovery_callable: TriggerCloudDiscoveryCallable = cast(
        TriggerCloudDiscoveryCallable,
        _runtime_imports_not_initialized,
    )
    _redact_account_for_log_callable: RedactAccountForLogCallable = cast(
        RedactAccountForLogCallable,
        _runtime_imports_not_initialized,
    )

_RUNTIME_IMPORTS_LOADED = False
_FCM_RECEIVER_CLASS: type[FcmReceiverHAType] | None = None


def _resolve_fcm_receiver_class() -> type[FcmReceiverHAType]:
    """Import and return the Home Assistant FCM receiver class lazily."""

    global _FCM_RECEIVER_CLASS
    if _FCM_RECEIVER_CLASS is None:
        from .Auth.fcm_receiver_ha import FcmReceiverHA as _FcmReceiverHA

        _FCM_RECEIVER_CLASS = _FcmReceiverHA
    return _FCM_RECEIVER_CLASS


def _resolve_google_home_filter_class() -> type[Any] | None:
    """Import and cache the optional Google Home filter class lazily."""

    global _GOOGLE_HOME_FILTER_CLASS, _GOOGLE_HOME_FILTER_IMPORT_ATTEMPTED
    if not _GOOGLE_HOME_FILTER_IMPORT_ATTEMPTED:
        _GOOGLE_HOME_FILTER_IMPORT_ATTEMPTED = True
        try:
            from .google_home_filter import GoogleHomeFilter as _GoogleHomeFilterClass
        except Exception:  # pragma: no cover - optional module
            _GOOGLE_HOME_FILTER_CLASS = None
        else:
            _GOOGLE_HOME_FILTER_CLASS = _GoogleHomeFilterClass
    return _GOOGLE_HOME_FILTER_CLASS


def _ensure_runtime_imports() -> None:
    """Load runtime-only modules when they are first required."""

    global _RUNTIME_IMPORTS_LOADED
    global api_register_fcm_provider
    global api_unregister_fcm_provider
    global async_register_services
    global async_initialize_discovery_runtime
    global _cloud_discovery_runtime_callable
    global _trigger_cloud_discovery_callable
    global _redact_account_for_log_callable
    global GoogleFindMyCoordinator
    global DiscoveryManager
    global GoogleFindMyMapView
    global GoogleFindMyMapRedirectView

    if _RUNTIME_IMPORTS_LOADED:
        return

    from .api import (  # noqa: E402
        register_fcm_receiver_provider as _api_register_fcm_provider,
        unregister_fcm_receiver_provider as _api_unregister_fcm_provider,
    )
    from .coordinator import (  # noqa: E402
        GoogleFindMyCoordinator as _GoogleFindMyCoordinator,
    )
    from .discovery import (  # noqa: E402
        DiscoveryManager as _DiscoveryManager,
        async_initialize_discovery_runtime as _async_initialize_discovery_runtime,
        _cloud_discovery_runtime as _cloud_discovery_runtime_fn,
        _redact_account_for_log as _redact_account_for_log_fn,
        _trigger_cloud_discovery as _trigger_cloud_discovery_fn,
    )
    from .map_view import (  # noqa: E402
        GoogleFindMyMapRedirectView as _GoogleFindMyMapRedirectView,
        GoogleFindMyMapView as _GoogleFindMyMapView,
    )
    from .services import (  # noqa: E402
        async_register_services as _async_register_services,
    )

    api_register_fcm_provider = _api_register_fcm_provider
    api_unregister_fcm_provider = _api_unregister_fcm_provider
    async_register_services = _async_register_services
    async_initialize_discovery_runtime = _async_initialize_discovery_runtime
    _cloud_discovery_runtime_callable = _cloud_discovery_runtime_fn
    _redact_account_for_log_callable = _redact_account_for_log_fn
    _trigger_cloud_discovery_callable = _trigger_cloud_discovery_fn
    GoogleFindMyCoordinator = _GoogleFindMyCoordinator
    DiscoveryManager = _DiscoveryManager
    GoogleFindMyMapView = _GoogleFindMyMapView
    GoogleFindMyMapRedirectView = _GoogleFindMyMapRedirectView

    _RUNTIME_IMPORTS_LOADED = True


def _cloud_discovery_runtime(hass: HomeAssistant) -> Mapping[str, Any]:
    """Return the cached discovery runtime, loading dependencies if needed."""

    _ensure_runtime_imports()
    return _cloud_discovery_runtime_callable(hass)


async def _trigger_cloud_discovery(*args: Any, **kwargs: Any) -> Any:
    """Trigger cloud discovery with lazy dependency loading."""

    _ensure_runtime_imports()
    return await _trigger_cloud_discovery_callable(*args, **kwargs)


def _redact_account_for_log(*args: Any, **kwargs: Any) -> str:
    """Redact account references for logging with lazy imports."""

    _ensure_runtime_imports()
    return _redact_account_for_log_callable(*args, **kwargs)

try:  # pragma: no cover - compatibility shim for stripped test envs
    from homeassistant.helpers.entity_registry import (
        RegistryEntryDisabler as _RegistryEntryDisabler,
    )
except Exception:  # pragma: no cover - Home Assistant test doubles may omit enum
    _RegistryEntryDisabler = SimpleNamespace(INTEGRATION="integration")

RegistryEntryDisabler = cast("RegistryEntryDisablerType", _RegistryEntryDisabler)

# Optional feature: GoogleHomeFilter (guard import to avoid hard dependency)
if TYPE_CHECKING:
    from .google_home_filter import GoogleHomeFilter as GoogleHomeFilterProtocol
else:
    from typing import Any as GoogleHomeFilterProtocol

_GOOGLE_HOME_FILTER_CLASS: type[Any] | None = None
_GOOGLE_HOME_FILTER_IMPORT_ATTEMPTED = False

try:
    # Helper name has been `config_entry_only_config_schema` since Core 2023.7
    # (renamed from `no_yaml_config_schema`). Retain fallbacks solely so legacy
    # tests lacking the helper keep importing this module without exploding.
    CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
except AttributeError:
    try:
        CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
    except AttributeError:  # pragma: no cover - kept for legacy tests without helpers
        import voluptuous as vol

        CONFIG_SCHEMA = vol.Schema({DOMAIN: vol.Schema({})})

_LOGGER = logging.getLogger(__name__)


async def _async_self_heal_duplicate_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Detect and remove duplicate entities for this config entry.

    A duplicate is defined as sharing the same config entry, device identifier,
    entity domain, and logical name (``translation_key`` preferred, then
    ``original_name``) while exposing a different ``entity_id`` or
    ``unique_id``. Only one canonical entity in every group is preserved.
    """

    entity_registry = er.async_get(hass)

    groups: dict[tuple[str, str | None, str | None, str], list[er.RegistryEntry]] = {}

    for entity_entry in _iter_config_entry_entities(entity_registry, entry.entry_id):
        if entity_entry.platform != DOMAIN:
            continue

        try:
            entity_domain, _ = split_entity_id(entity_entry.entity_id)
        except ValueError:
            entity_domain = None

        translation_key = getattr(entity_entry, "translation_key", None)
        original_name = getattr(entity_entry, "original_name", None)
        logical_name = (
            (translation_key or "").strip()
            or (original_name or "").strip()
            or entity_entry.entity_id
        )

        device_id = getattr(entity_entry, "device_id", None)
        key = (
            entity_entry.config_entry_id,
            device_id,
            entity_domain,
            logical_name,
        )
        groups.setdefault(key, []).append(entity_entry)

    duplicates: list[str] = []

    for entries in groups.values():
        if len(entries) <= 1:
            continue

        canonical = _pick_canonical_entity_entry(hass, entries)
        for candidate in entries:
            if candidate.entity_id == canonical.entity_id:
                continue
            duplicates.append(candidate.entity_id)

    if not duplicates:
        return

    _LOGGER.info(
        "Removing %s duplicate Google Find My entities for config entry %s: %s",
        len(duplicates),
        entry.entry_id,
        ", ".join(sorted(duplicates)),
    )

    for entity_id in duplicates:
        if entity_id in entity_registry.entities:
            entity_registry.async_remove(entity_id)


def _compute_entity_score(
    hass: HomeAssistant,
    entity_entry: er.RegistryEntry,
) -> int:
    """Return a priority score describing how suitable an entity is to keep."""

    score = 0

    if entity_entry.translation_key:
        score += 4

    if entity_entry.disabled_by is None:
        score += 3

    state = hass.states.get(entity_entry.entity_id)
    if state is not None and state.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        score += 3

    return score


def _pick_canonical_entity_entry(
    hass: HomeAssistant,
    entries: list[er.RegistryEntry],
) -> er.RegistryEntry:
    """Pick the entity registry entry that should remain active."""

    canonical = entries[0]
    best_score = _compute_entity_score(hass, canonical)

    for candidate in entries[1:]:
        score = _compute_entity_score(hass, candidate)
        if score > best_score:
            canonical = candidate
            best_score = score

    return canonical


_CONFIG_FLOW_HELPERS: dict[str, Any] | None = None
# Keep the per-entry health probe timeout small so rebuilds remain responsive.
# Adjust cautiously if probing strategy changes—this is a per-entry budget, not
# a global backoff.
_ENTRY_HEALTH_TIMEOUT = 10.0


@dataclass(slots=True)
class _EntryHealth:
    """Summarize the credential health of a config entry."""

    status: Literal["valid", "invalid", "unknown"]
    reason: str | None = None
    token_source: str | None = None


def _load_config_flow_helpers() -> dict[str, Any] | None:
    """Lazily import config flow helpers used for credential probes."""

    global _CONFIG_FLOW_HELPERS
    if _CONFIG_FLOW_HELPERS is not None:
        return _CONFIG_FLOW_HELPERS

    try:  # pragma: no cover - exercised indirectly in tests
        from . import config_flow as _config_flow
    except Exception as err:  # noqa: BLE001 - defensive import guard
        _LOGGER.debug("Config flow helpers unavailable: %s", err)
        _CONFIG_FLOW_HELPERS = {}
        return _CONFIG_FLOW_HELPERS

    _CONFIG_FLOW_HELPERS = {
        "extract_oauth": getattr(
            _config_flow, "_extract_oauth_candidates_from_secrets", None
        ),
        "new_api": getattr(_config_flow, "_async_new_api_for_probe", None),
        "try_probe": getattr(_config_flow, "_try_probe_devices", None),
        "map_error": getattr(_config_flow, "_map_api_exc_to_error_key", None),
        "guard_error": getattr(_config_flow, "_is_multi_entry_guard_error", None),
        "dependency_not_ready": getattr(_config_flow, "DependencyNotReady", None),
    }
    return _CONFIG_FLOW_HELPERS


def _entry_schema_score(entry: ConfigEntry) -> int:
    """Return an integer describing how complete the entry schema is."""

    score = 0
    for container in (entry.data, entry.options):
        if not isinstance(container, Mapping):
            continue
        if isinstance(container.get(DATA_SECRET_BUNDLE), Mapping):
            score += 5
        elif container.get(DATA_SECRET_BUNDLE) is not None:
            score += 2
        if container.get(DATA_AUTH_METHOD):
            score += 2
        if container.get(CONF_OAUTH_TOKEN):
            score += 1
        if container.get(DATA_AAS_TOKEN):
            score += 1
    if isinstance(getattr(entry, "options", None), Mapping):
        score += len(entry.options)
    return score


def _entry_creation_timestamp(entry: ConfigEntry) -> float:
    """Return the creation timestamp (epoch seconds) or ``inf`` when unknown."""

    timestamp = getattr(entry, "created_at", None)
    if not isinstance(timestamp, datetime):
        timestamp = getattr(entry, "updated_at", None)
    if isinstance(timestamp, datetime):
        try:
            return float(timestamp.timestamp())
        except (OSError, ValueError):  # pragma: no cover - defensive fallback
            return float("inf")
    return float("inf")


async def _async_collect_entry_tokens(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[list[tuple[str, str]], Mapping[str, Any] | None]:
    """Collect candidate OAuth tokens for ``entry`` from all known sources."""

    helpers = _load_config_flow_helpers() or {}
    extract_oauth = helpers.get("extract_oauth")

    seen: set[str] = set()
    tokens: list[tuple[str, str]] = []
    secrets_bundle: Mapping[str, Any] | None = None

    def _add(label: str, value: Any) -> None:
        if not isinstance(value, str):
            return
        candidate = value.strip()
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        tokens.append((label, candidate))

    for container_label, container in ("data", entry.data), ("options", entry.options):
        if not isinstance(container, Mapping):
            continue
        bundle = container.get(DATA_SECRET_BUNDLE)
        if isinstance(bundle, Mapping):
            secrets_bundle = secrets_bundle or cast(Mapping[str, Any], bundle)
            if callable(extract_oauth):
                try:
                    for source, token in extract_oauth(dict(bundle)):
                        _add(f"{container_label}.secrets.{source}", token)
                except Exception as err:  # pragma: no cover - defensive logging
                    _LOGGER.debug(
                        "Secret token extraction failed for %s.%s: %s",
                        entry.entry_id,
                        container_label,
                        err,
                    )
        _add(f"{container_label}.oauth_token", container.get(CONF_OAUTH_TOKEN))
        _add(f"{container_label}.aas_token", container.get(DATA_AAS_TOKEN))

    runtime = getattr(entry, "runtime_data", None)
    cache: TokenCache | None = getattr(runtime, "token_cache", None)
    created_cache: TokenCache | None = None
    if cache is None:
        try:
            created_cache = await TokenCache.create(hass, entry.entry_id)
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "Token cache load failed for entry %s: %s", entry.entry_id, err
            )
        else:
            cache = created_cache

    if cache is not None:
        try:
            cached_values = await cache.all()
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug(
                "Token cache read failed for entry %s: %s", entry.entry_id, err
            )
        else:
            if isinstance(cached_values, Mapping):
                _add("cache.oauth_token", cached_values.get(CONF_OAUTH_TOKEN))
                _add("cache.aas_token", cached_values.get(DATA_AAS_TOKEN))
                cached_bundle = cached_values.get(DATA_SECRET_BUNDLE)
                if isinstance(cached_bundle, Mapping) and secrets_bundle is None:
                    secrets_bundle = cast(Mapping[str, Any], cached_bundle)
                    if callable(extract_oauth):
                        try:
                            for source, token in extract_oauth(dict(cached_bundle)):
                                _add(f"cache.secrets.{source}", token)
                        except Exception as err:  # pragma: no cover - defensive
                            _LOGGER.debug(
                                "Secret token extraction failed for cache (%s): %s",
                                entry.entry_id,
                                err,
                            )

    if created_cache is not None:
        with suppress(Exception):  # pragma: no cover - defensive cleanup
            await created_cache.close()

    return tokens, secrets_bundle


async def _async_assess_entry_health(
    hass: HomeAssistant, entry: ConfigEntry, *, normalized_email: str
) -> _EntryHealth:
    """Probe stored credentials to determine whether ``entry`` is usable."""

    tokens, secrets_bundle = await _async_collect_entry_tokens(hass, entry)
    if not tokens:
        return _EntryHealth(status="unknown", reason="no_tokens")

    helpers = _load_config_flow_helpers() or {}
    new_api = helpers.get("new_api")
    try_probe = helpers.get("try_probe")
    map_error = helpers.get("map_error")
    guard_error = helpers.get("guard_error")
    dependency_not_ready = helpers.get("dependency_not_ready")

    if not callable(new_api) or not callable(try_probe) or not callable(map_error):
        return _EntryHealth(status="unknown", reason="helpers_missing")

    invalid_seen = False
    unknown_reason: str | None = None

    for source, token in tokens:
        try:
            async with asyncio.timeout(_ENTRY_HEALTH_TIMEOUT):
                api = await new_api(
                    email=normalized_email,
                    token=token,
                    secrets_bundle=secrets_bundle,
                )
                await try_probe(api, email=normalized_email, token=token)
        except asyncio.TimeoutError:
            unknown_reason = "timeout"
            continue
        except Exception as err:  # noqa: BLE001 - deliberate broad guard
            if dependency_not_ready and isinstance(err, dependency_not_ready):
                return _EntryHealth(status="unknown", reason="dependency_not_ready")
            if callable(guard_error) and guard_error(err):
                return _EntryHealth(status="valid", reason="guard", token_source=source)
            error_key = map_error(err)
            if error_key == "invalid_auth":
                invalid_seen = True
                continue
            unknown_reason = error_key or "unknown"
            continue
        else:
            return _EntryHealth(status="valid", reason="probe_ok", token_source=source)

    if unknown_reason is not None:
        return _EntryHealth(status="unknown", reason=unknown_reason)
    if invalid_seen:
        return _EntryHealth(status="invalid", reason="invalid_auth")
    return _EntryHealth(status="unknown", reason="no_result")


async def async_coalesce_account_entries(
    hass: HomeAssistant,
    *,
    canonical_entry: ConfigEntry,
) -> ConfigEntry:
    """Ensure only one config entry remains for the account represented by ``canonical_entry``."""

    raw_email, normalized_email = _resolve_entry_email(canonical_entry)
    if not normalized_email:
        _LOGGER.warning(
            "Cannot deduplicate config entry %s: missing normalized email (raw=%s)",
            canonical_entry.entry_id,
            raw_email or "n/a",
        )
        return canonical_entry

    all_entries = hass.config_entries.async_entries(DOMAIN)
    candidates: dict[str, ConfigEntry] = {}
    for candidate in all_entries:
        if _extract_email_from_entry(candidate) == normalized_email:
            candidates[candidate.entry_id] = candidate

    candidates.setdefault(canonical_entry.entry_id, canonical_entry)

    candidate_list = list(candidates.values())
    if len(candidate_list) == 1:
        return candidate_list[0]

    health: dict[str, _EntryHealth] = {}
    for candidate in candidate_list:
        health[candidate.entry_id] = await _async_assess_entry_health(
            hass, candidate, normalized_email=normalized_email
        )

    canonical_id = canonical_entry.entry_id

    def _valid_sort_key(entry: ConfigEntry) -> tuple[Any, ...]:
        """Order valid entries by version, enabled state, schema richness, then caller."""
        not_disabled = 1 if getattr(entry, "disabled_by", None) is None else 0
        return (
            -int(getattr(entry, "version", 0)),
            -not_disabled,
            -_entry_schema_score(entry),
            -1 if entry.entry_id == canonical_id else 0,
            entry.entry_id,
        )

    valid_candidates = [
        candidate
        for candidate in candidate_list
        if health[candidate.entry_id].status == "valid"
    ]
    if valid_candidates:
        winner = sorted(valid_candidates, key=_valid_sort_key)[0]
    else:
        def _fallback_sort_key(entry: ConfigEntry) -> tuple[Any, ...]:
            """Order unknown/invalid entries by schema version, enabled flag, and age."""
            not_disabled = 1 if getattr(entry, "disabled_by", None) is None else 0
            return (
                -int(getattr(entry, "version", 0)),
                -not_disabled,
                _entry_creation_timestamp(entry),
                entry.entry_id,
            )

        winner = sorted(candidate_list, key=_fallback_sort_key)[0]
        _LOGGER.warning(
            "Account %s has no verified credentials; selected entry %s via heuristics",
            _mask_email_for_logs(normalized_email),
            winner.entry_id,
        )

    winner_health = health[winner.entry_id]
    _LOGGER.info(
        "Selected canonical entry %s for account %s with status %s",
        winner.entry_id,
        _mask_email_for_logs(normalized_email),
        winner_health.status,
    )

    _LOGGER.debug(
        "Credential health for account %s → %s",
        _mask_email_for_logs(normalized_email),
        {entry_id: report.status for entry_id, report in health.items()},
    )

    for candidate in candidate_list:
        if candidate.entry_id == winner.entry_id:
            continue
        _LOGGER.info(
            "Removing duplicate Google Find My Device entry %s (account %s); canonical entry is %s",
            candidate.entry_id,
            _mask_email_for_logs(normalized_email),
            winner.entry_id,
        )
        try:
            await hass.config_entries.async_remove(candidate.entry_id)
        except Exception as err:  # noqa: BLE001 - surface unexpected failure
            _LOGGER.error(
                "Failed to remove duplicate entry %s for account %s: %s",
                candidate.entry_id,
                _mask_email_for_logs(normalized_email),
                err,
            )
            raise

    refreshed_getter = getattr(hass.config_entries, "async_get_entry", None)
    if callable(refreshed_getter):
        refreshed = refreshed_getter(winner.entry_id)
        if refreshed is not None:
            winner = refreshed

    return winner

# Platforms provided by this integration
PLATFORMS: list[Platform] = [
    Platform.DEVICE_TRACKER,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


def _feature_name_from_platform(platform: Platform) -> str:
    """Return the Home Assistant domain string for a platform enum."""

    value = getattr(platform, "value", None)
    if isinstance(value, str):
        return value

    if isinstance(platform, str):  # pragma: no cover - defensive fallback
        return platform

    candidate = str(platform)
    if "." in candidate:
        _, candidate = candidate.split(".", 1)
    return candidate.lower()

# ---- Runtime typing helpers -------------------------------------------------


CleanupCallback = Callable[[], Awaitable[None] | None]


def _subentry_entry_id(subentry: Any) -> str | None:
    """Return the identifier Home Assistant uses to track ``subentry``.

    The ConfigSubentry API exposes both ``entry_id`` and ``subentry_id`` across
    core releases. This helper normalizes either attribute to a stable string so
    callers can trigger setup for newly created subentries. Keep this in sync
    with the handbook references in docs/CONFIG_SUBENTRIES_HANDBOOK.md.
    """

    subentry_id = getattr(subentry, "subentry_id", None)
    if isinstance(subentry_id, str) and subentry_id:
        return subentry_id

    subentry_entry_id = getattr(subentry, "entry_id", None)
    if isinstance(subentry_entry_id, str) and subentry_entry_id:
        return subentry_entry_id

    return None


@dataclass(slots=True)
class ConfigEntrySubentryDefinition:
    """Desired state for a managed configuration subentry."""

    key: str
    title: str
    data: Mapping[str, Any]
    subentry_type: str = SUBENTRY_TYPE_TRACKER
    unique_id: str | None = None
    translation_key: str | None = None
    unload: CleanupCallback | None = None


_AwaitableT = TypeVar("_AwaitableT")


class ConfigEntrySubEntryManager:
    """Helper for managing config entry subentries for this integration."""

    __slots__ = (
        "_cleanup",
        "_default_subentry_type",
        "_entry",
        "_hass",
        "_key_field",
        "_managed",
        "_managed_by_subentry_id",
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        key_field: str = "group_key",
        default_subentry_type: str = SUBENTRY_TYPE_TRACKER,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._key_field = key_field
        self._default_subentry_type = default_subentry_type
        self._managed: dict[str, ConfigSubentry] = {}
        self._managed_by_subentry_id: dict[str, str] = {}
        self._cleanup: dict[str, CleanupCallback | None] = {}
        self._refresh_from_entry()

    @staticmethod
    async def _await_subentry_result(
        result: Awaitable[_AwaitableT] | _AwaitableT,
    ) -> _AwaitableT:
        """Return the awaited subentry operation result when needed."""

        if isinstance(result, Awaitable):
            awaited_any = await result
            awaited_value: _AwaitableT = cast(_AwaitableT, awaited_any)
            return awaited_value
        return result

    @staticmethod
    def _subentry_identity(
        candidate: ConfigSubentry | Any,
    ) -> tuple[str | None, str | None, str | None]:
        """Return entry, subentry, and unique identifiers for ``candidate``."""

        if candidate is None:
            return None, None, None

        entry_id_value = getattr(candidate, "entry_id", None)
        entry_id = entry_id_value if isinstance(entry_id_value, str) and entry_id_value else None

        subentry_id_value = getattr(candidate, "subentry_id", None)
        subentry_id = (
            subentry_id_value if isinstance(subentry_id_value, str) and subentry_id_value else None
        )

        unique_id_value = getattr(candidate, "unique_id", None)
        unique_id = unique_id_value if isinstance(unique_id_value, str) and unique_id_value else None

        return entry_id, subentry_id, unique_id

    @staticmethod
    def _is_subentry_like(candidate: Any) -> TypeGuard[ConfigSubentry]:
        """Return ``True`` when ``candidate`` exposes a subentry-like interface."""

        if candidate is None:
            return False

        _, subentry_id, _ = ConfigEntrySubEntryManager._subentry_identity(candidate)
        if not isinstance(subentry_id, str) or not subentry_id:
            return False

        data = getattr(candidate, "data", None)
        if not isinstance(data, Mapping):
            return False

        return True


    def _managed_key_for_subentry_id(self, subentry_id: str) -> str | None:
        """Return the managed key associated with ``subentry_id`` when present."""

        existing_key = self._managed_by_subentry_id.get(subentry_id)
        if existing_key is not None:
            return existing_key

        for managed_key, managed_subentry in self._managed.items():
            candidate_subentry_id = getattr(managed_subentry, "subentry_id", None)
            if isinstance(candidate_subentry_id, str) and candidate_subentry_id == subentry_id:
                self._managed_by_subentry_id[subentry_id] = managed_key
                return managed_key

        return None

    def _pop_managed(self, key: str) -> ConfigSubentry | None:
        """Remove ``key`` from the managed cache and return the stored subentry."""

        stored = self._managed.pop(key, None)
        if stored is None:
            return None

        stored_subentry_id = getattr(stored, "subentry_id", None)
        if isinstance(stored_subentry_id, str):
            mapped_key = self._managed_by_subentry_id.get(stored_subentry_id)
            if mapped_key == key:
                self._managed_by_subentry_id.pop(stored_subentry_id, None)

        return stored

    def _index_managed_subentry(self, key: str, subentry: ConfigSubentry) -> None:
        """Store ``subentry`` under ``key`` and keep the subentry-id index in sync."""

        subentry_id = getattr(subentry, "subentry_id", None)
        if isinstance(subentry_id, str) and subentry_id:
            existing_key = self._managed_by_subentry_id.get(subentry_id)
            if existing_key is None:
                for managed_key, managed_subentry in list(self._managed.items()):
                    candidate_subentry_id = getattr(managed_subentry, "subentry_id", None)
                    if (
                        isinstance(candidate_subentry_id, str)
                        and candidate_subentry_id == subentry_id
                    ):
                        existing_key = managed_key
                        break
            if existing_key is not None and existing_key != key:
                self._pop_managed(existing_key)

        self._managed[key] = subentry

        if isinstance(subentry_id, str) and subentry_id:
            self._managed_by_subentry_id[subentry_id] = key

    def _resolve_updated_subentry(
        self,
        *,
        candidate: ConfigSubentry | Any,
        original: ConfigSubentry,
    ) -> ConfigSubentry:
        """Return the object that should back the managed cache after an update."""

        if self._is_subentry_like(candidate):
            return candidate

        subentry_id = getattr(original, "subentry_id", None)
        if isinstance(subentry_id, str) and subentry_id:
            subentries = getattr(self._entry, "subentries", None)
            if isinstance(subentries, Mapping):
                from_registry = subentries.get(subentry_id)
                if self._is_subentry_like(from_registry):
                    return from_registry

        return original

    def _resolve_registered_subentry(
        self,
        *,
        key: str,
        unique_id: str,
        candidate: ConfigSubentry | None,
        fallback_subentry_id: str | None,
    ) -> ConfigSubentry:
        """Return the registry-backed subentry for ``key``."""

        candidate_entry_id, candidate_subentry_id, candidate_unique_id = (
            self._subentry_identity(candidate)
        )

        target_subentry_id: str | None
        if isinstance(fallback_subentry_id, str) and fallback_subentry_id:
            target_subentry_id = fallback_subentry_id
        else:
            target_subentry_id = candidate_subentry_id

        manager = getattr(self._hass, "config_entries", None)
        refreshed_entry: ConfigEntry | None = None
        if manager is not None:
            get_entry = getattr(manager, "async_get_entry", None)
            if callable(get_entry):
                try:
                    refreshed_entry = get_entry(self._entry.entry_id)
                except Exception as err:  # pragma: no cover - defensive logging
                    _LOGGER.debug(
                        "[%s] async_sync: Failed to refresh parent entry during subentry"
                        " lookup: %s",
                        self._entry.entry_id,
                        err,
                    )

        if isinstance(refreshed_entry, ConfigEntry) and refreshed_entry is not self._entry:
            self._entry = refreshed_entry

        def _scan(entry: Any) -> ConfigSubentry | None:
            subentries = getattr(entry, "subentries", None)
            if not isinstance(subentries, Mapping):
                return None

            if isinstance(target_subentry_id, str):
                direct = subentries.get(target_subentry_id)
                if isinstance(direct, ConfigSubentry):
                    return direct
                if direct is not None:
                    return direct

            for subentry in subentries.values():
                subentry_entry_id, subentry_id, subentry_unique_id = self._subentry_identity(
                    subentry
                )

                if isinstance(target_subentry_id, str) and subentry_id == target_subentry_id:
                    return subentry

                if isinstance(candidate_entry_id, str):
                    if isinstance(subentry_entry_id, str) and subentry_entry_id == candidate_entry_id:
                        return subentry

                resolved_candidate_unique_id: str | None
                if isinstance(candidate_unique_id, str) and candidate_unique_id:
                    resolved_candidate_unique_id = candidate_unique_id
                elif isinstance(unique_id, str) and unique_id:
                    resolved_candidate_unique_id = unique_id
                else:
                    resolved_candidate_unique_id = None
                if (
                    isinstance(subentry_unique_id, str)
                    and resolved_candidate_unique_id is not None
                    and subentry_unique_id == resolved_candidate_unique_id
                ):
                    return subentry

                data = getattr(subentry, "data", None)
                if isinstance(data, Mapping):
                    group_key = data.get(self._key_field)
                    if isinstance(group_key, str) and group_key == key:
                        return subentry

                if (
                    isinstance(fallback_subentry_id, str)
                    and isinstance(subentry_id, str)
                    and subentry_id == fallback_subentry_id
                ):
                    return subentry

            return None

        resolved: ConfigSubentry | None = None
        entries_to_scan: list[Any] = []
        for entry in (refreshed_entry, self._entry):
            if entry is None:
                continue
            if getattr(entry, "subentries", None) is None:
                continue
            if any(candidate is entry for candidate in entries_to_scan):
                continue
            entries_to_scan.append(entry)

        for entry in entries_to_scan:
            resolved = _scan(entry)
            if resolved is not None:
                break

        if resolved is None and isinstance(candidate, ConfigSubentry):
            resolved = candidate

        if resolved is None:
            candidate_descriptor: str
            if isinstance(candidate_entry_id, str) and candidate_entry_id:
                candidate_descriptor = candidate_entry_id
            elif isinstance(candidate_subentry_id, str) and candidate_subentry_id:
                fallback_unique = candidate_unique_id or unique_id
                if isinstance(fallback_unique, str) and fallback_unique:
                    candidate_descriptor = f"{candidate_subentry_id}:{fallback_unique}"
                else:
                    candidate_descriptor = candidate_subentry_id
            elif isinstance(candidate_unique_id, str) and candidate_unique_id:
                candidate_descriptor = candidate_unique_id
            elif isinstance(unique_id, str) and unique_id:
                candidate_descriptor = unique_id
            else:
                candidate_descriptor = "<unset>"
            context = (
                "unique_id={unique_id!r}, key={key!r}, fallback_id={fallback!r},"
                " candidate_ref={candidate!r}"
            ).format(
                unique_id=unique_id,
                key=key,
                fallback=fallback_subentry_id,
                candidate=candidate_descriptor,
            )
            _LOGGER.error(
                "[%s] async_sync: Unable to locate registered subentry for %s after"
                " async_add_subentry",
                self._entry.entry_id,
                context,
            )
            raise HomeAssistantError(
                "Failed to locate registered subentry after creation; see logs for"
                f" context ({context})"
            )

        return resolved

    def _refresh_from_entry(self) -> None:
        """Populate managed mapping from the config entry."""

        self._managed.clear()
        self._managed_by_subentry_id.clear()
        subentries = getattr(self._entry, "subentries", None)
        if not isinstance(subentries, Mapping):
            return

        seen_subentry_ids: set[str] = set()
        for subentry in subentries.values():
            subentry_id = getattr(subentry, "subentry_id", None)
            if isinstance(subentry_id, str) and subentry_id:
                if subentry_id in seen_subentry_ids:
                    continue
                seen_subentry_ids.add(subentry_id)

            data = getattr(subentry, "data", None)
            if isinstance(data, Mapping):
                key = data.get(self._key_field)
            else:
                key = None

            if isinstance(key, str):
                self._index_managed_subentry(key, subentry)

    async def _async_adopt_existing_unique_id(
        self,
        key: str,
        definition: ConfigEntrySubentryDefinition,
        unique_id: str,
        payload: dict[str, Any],
    ) -> ConfigSubentry:
        """Adopt the existing subentry that already owns ``unique_id``."""

        owner: ConfigSubentry | None = None
        for subentry in getattr(self._entry, "subentries", {}).values():
            _, _, candidate_unique_id = self._subentry_identity(subentry)
            if candidate_unique_id == unique_id:
                owner = subentry
                break

        if owner is None:
            raise HomeAssistantError(
                f"Subentry with unique_id '{unique_id}' not found while trying to "
                f"adopt it for key '{key}' in entry {self._entry.entry_id}"
            )

        _, owner_subentry_id, owner_unique_id = self._subentry_identity(owner)

        removal_keys: set[str] = set()

        if isinstance(owner_subentry_id, str) and owner_subentry_id:
            previous_key = self._managed_key_for_subentry_id(owner_subentry_id)
            if previous_key is not None and previous_key != key:
                removal_keys.add(previous_key)

        for old_key, mapped in list(self._managed.items()):
            if old_key == key:
                continue
            _, mapped_subentry_id, mapped_unique_id = self._subentry_identity(mapped)
            if (
                isinstance(owner_subentry_id, str)
                and owner_subentry_id
                and mapped_subentry_id == owner_subentry_id
            ):
                removal_keys.add(old_key)
                continue
            if (
                isinstance(owner_unique_id, str)
                and owner_unique_id
                and mapped_unique_id == owner_unique_id
            ):
                removal_keys.add(old_key)

        for old_key in removal_keys:
            self._pop_managed(old_key)
            self._cleanup.pop(old_key, None)

        update_result = self._hass.config_entries.async_update_subentry(
            self._entry,
            owner,
            data=payload,
            title=definition.title,
        )
        resolved = await self._await_subentry_result(update_result)

        if isinstance(resolved, ConfigSubentry):
            stored = resolved
        else:
            stored = self._entry.subentries.get(
                getattr(owner, "subentry_id", None), owner
            )

        self._index_managed_subentry(key, stored)
        return stored

    async def _deduplicate_subentries(self) -> None:
        """Remove duplicate subentries so each logical group has a single entry."""

        manager = getattr(self._hass, "config_entries", None)
        remove_subentry = (
            getattr(manager, "async_remove_subentry", None)
            if manager is not None
            else None
        )
        if not callable(remove_subentry):
            return

        raw_subentries = getattr(self._entry, "subentries", None)
        candidates: Iterable[ConfigSubentry]
        if isinstance(raw_subentries, Mapping):
            candidates = cast(Iterable[ConfigSubentry], raw_subentries.values())
        elif isinstance(raw_subentries, dict):
            candidates = cast(Iterable[ConfigSubentry], raw_subentries.values())
        else:
            candidates = ()

        subentries: list[ConfigSubentry] = []
        seen_subentry_ids: set[str] = set()
        for subentry in candidates:
            subentry_id = getattr(subentry, "subentry_id", None)
            if isinstance(subentry_id, str) and subentry_id:
                if subentry_id in seen_subentry_ids:
                    continue
                seen_subentry_ids.add(subentry_id)
            subentries.append(subentry)
        if not subentries:
            return

        grouped_by_unique: dict[str, list[ConfigSubentry]] = defaultdict(list)
        grouped_by_group: dict[tuple[str | None, str | None], list[ConfigSubentry]] = (
            defaultdict(list)
        )

        for subentry in subentries:
            subentry_unique_id = getattr(subentry, "unique_id", None)
            if isinstance(subentry_unique_id, str):
                grouped_by_unique[subentry_unique_id].append(subentry)

            data = getattr(subentry, "data", None)
            if isinstance(data, Mapping):
                group_value = data.get(self._key_field)
            else:
                group_value = None
            key_value = group_value if isinstance(group_value, str) and group_value else None
            grouped_by_group[(key_value, getattr(subentry, "subentry_type", None))].append(
                subentry
            )

        def _select_canonical(candidates: list[ConfigSubentry]) -> ConfigSubentry:
            def _sort_key(item: tuple[int, ConfigSubentry]) -> tuple[int, str, int, str]:
                index, candidate = item
                candidate_unique_id = getattr(candidate, "unique_id", None)
                unique_part = candidate_unique_id if isinstance(candidate_unique_id, str) else ""
                candidate_subentry_id = getattr(candidate, "subentry_id", None)
                subentry_part = (
                    candidate_subentry_id if isinstance(candidate_subentry_id, str) else ""
                )
                return (0 if unique_part else 1, unique_part, index, subentry_part)

            return min(enumerate(candidates), key=_sort_key)[1]

        removal_targets: set[str] = set()
        duplicate_descriptors: set[str] = set()

        for unique_id, candidates in grouped_by_unique.items():
            if len(candidates) <= 1:
                continue
            canonical = _select_canonical(candidates)
            duplicate_descriptors.add(f"unique_id={unique_id}")
            for candidate in candidates:
                if candidate is canonical:
                    continue
                candidate_id = getattr(candidate, "subentry_id", None)
                if isinstance(candidate_id, str):
                    removal_targets.add(candidate_id)

        for (key_value, subentry_type), candidates in grouped_by_group.items():
            if len(candidates) <= 1:
                continue
            canonical = _select_canonical(candidates)
            descriptor_key = key_value or "<unset>"
            duplicate_descriptors.add(f"group={descriptor_key}:{subentry_type}")
            for candidate in candidates:
                if candidate is canonical:
                    continue
                candidate_id = getattr(candidate, "subentry_id", None)
                if isinstance(candidate_id, str):
                    removal_targets.add(candidate_id)

        if not removal_targets:
            return

        removed_ids: list[str] = []
        for subentry in subentries:
            subentry_id = getattr(subentry, "subentry_id", None)
            if not isinstance(subentry_id, str) or subentry_id not in removal_targets:
                continue

            removal = remove_subentry(
                self._entry,
                subentry_id=subentry_id,
            )

            try:
                await self._await_subentry_result(removal)
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.error(
                    "Failed to remove duplicate subentry '%s': %s",
                    subentry_id,
                    err,
                )
                raise

            removed_ids.append(subentry_id)

        if removed_ids:
            _LOGGER.info(
                "Removed %s duplicate config subentries for %s (%s)",
                len(removed_ids),
                self._entry.entry_id,
                ", ".join(sorted(duplicate_descriptors)),
            )

        self._refresh_from_entry()

    @property
    def managed_subentries(self) -> dict[str, ConfigSubentry]:
        """Return a copy of the managed subentry mapping."""

        return dict(self._managed)

    def get(self, key: str) -> ConfigSubentry | None:
        """Return the managed subentry for a key when present."""

        return self._managed.get(key)

    def update_visible_device_ids(
        self, key: str, visible_device_ids: Sequence[str]
    ) -> None:
        """Update the visible device identifiers stored in a subentry."""

        subentry = self._managed.get(key)
        if subentry is None:
            return

        normalized = tuple(
            dict.fromkeys(
                str(device_id)
                for device_id in visible_device_ids
                if isinstance(device_id, str) and device_id
            )
        )

        existing_raw = subentry.data.get("visible_device_ids")
        if isinstance(existing_raw, (list, tuple)):
            existing = tuple(
                str(device_id)
                for device_id in existing_raw
                if isinstance(device_id, str) and device_id
            )
        else:
            existing = ()

        if normalized == existing:
            return

        payload = dict(subentry.data)
        payload[self._key_field] = key
        payload["visible_device_ids"] = list(normalized)

        update_result = self._hass.config_entries.async_update_subentry(
            self._entry,
            subentry,
            data=payload,
        )

        if inspect.isawaitable(update_result):

            async def _await_visibility_update() -> None:
                resolved_subentry: ConfigSubentry | None = None
                try:
                    resolved = await self._await_subentry_result(update_result)
                except Exception as err:  # pragma: no cover - defensive logging
                    _LOGGER.debug(
                        "Subentry visibility update for '%s' raised: %s", key, err
                    )
                else:
                    if self._is_subentry_like(resolved):
                        resolved_subentry = resolved
                finally:
                    refreshed_subentry = self._resolve_updated_subentry(
                        candidate=resolved_subentry,
                        original=subentry,
                    )
                    self._index_managed_subentry(key, refreshed_subentry)

            self._hass.async_create_task(
                _await_visibility_update(),
                name=f"{DOMAIN}.subentry_visibility_refresh",
            )
            return

        refreshed = self._resolve_updated_subentry(
            candidate=update_result,
            original=subentry,
        )
        # Ensure local view reflects Home Assistant's stored subentry.
        self._index_managed_subentry(key, refreshed)

    async def async_sync(
        self, definitions: Iterable[ConfigEntrySubentryDefinition]
    ) -> None:
        """Ensure subentries match the provided definitions."""

        desired: dict[str, ConfigEntrySubentryDefinition] = {}
        for definition in definitions:
            desired[definition.key] = definition

        created_subentries: list[ConfigSubentry] = []

        try:
            await self._deduplicate_subentries()
        except Exception as err:  # pragma: no cover - defensive warning
            _LOGGER.warning(
                "[%s] async_sync: Failed to pre-deduplicate subentries: %s",
                self._entry.entry_id,
                err,
            )

        create_subentry = getattr(
            self._hass.config_entries, "async_create_subentry", None
        )

        for key, definition in desired.items():
            payload = dict(definition.data)
            payload[self._key_field] = key
            unique_id = definition.unique_id or f"{self._entry.entry_id}-{key}"
            subentry_type = definition.subentry_type or self._default_subentry_type
            translation_key = definition.translation_key
            cleanup = definition.unload

            existing = self._managed.get(key)
            deduplicated = False

            conflict_key = next(
                (
                    managed_key
                    for managed_key, managed_subentry in self._managed.items()
                    if managed_key != key and managed_subentry.unique_id == unique_id
                ),
                None,
            )
            if conflict_key is not None:
                await self._deduplicate_subentries()
                deduplicated = True
                existing = self._managed.get(key)

            while True:
                if existing is None:
                    _LOGGER.info(
                        "[%s] async_sync: Creating new subentry for key '%s' (unique_id=%s)",
                        self._entry.entry_id,
                        key,
                        unique_id,
                    )
                    _LOGGER.debug(
                        "[%s] async_sync: ADD PAYLOAD: type=%s, title=%s, group_key=%s",
                        self._entry.entry_id,
                        subentry_type,
                        definition.title,
                        definition.data.get("group_key"),
                    )
                    new_subentry: ConfigSubentry | None = None
                    add_result: Awaitable[ConfigSubentry] | ConfigSubentry
                    try:
                        if callable(create_subentry):
                            create_kwargs: dict[str, Any] = {
                                "data": payload,
                                "title": definition.title,
                                "unique_id": unique_id,
                                "subentry_type": subentry_type,
                            }
                            if translation_key is not None:
                                create_kwargs["translation_key"] = translation_key
                            try:
                                add_result = create_subentry(
                                    self._entry,
                                    **create_kwargs,
                                )
                            except TypeError:
                                if "translation_key" in create_kwargs:
                                    create_kwargs.pop("translation_key")
                                    add_result = create_subentry(
                                        self._entry,
                                        **create_kwargs,
                                    )
                                else:
                                    raise
                        else:
                            constructor_kwargs: dict[str, Any] = {
                                "data": MappingProxyType(payload),
                                "title": definition.title,
                                "unique_id": unique_id,
                                "subentry_type": subentry_type,
                            }
                            if translation_key is not None:
                                constructor_kwargs["translation_key"] = translation_key

                            placeholder_subentry_id = "-".join(
                                (
                                    self._entry.entry_id,
                                    key,
                                    "provisional",
                                )
                            )
                            constructor_kwargs.setdefault(
                                "subentry_id",
                                placeholder_subentry_id,
                            )

                            new_subentry = None
                            for drop_translation in (False, True):
                                try:
                                    new_subentry = ConfigSubentry(
                                        **constructor_kwargs,
                                    )
                                except TypeError:
                                    if (
                                        not drop_translation
                                        and "translation_key" in constructor_kwargs
                                    ):
                                        constructor_kwargs.pop("translation_key")
                                        continue
                                else:
                                    break
                            else:
                                saved_subentry_type = constructor_kwargs.pop(
                                    "subentry_type", None
                                )
                                try:
                                    new_subentry = ConfigSubentry(
                                        **constructor_kwargs,
                                    )
                                except TypeError as err:
                                    if saved_subentry_type is not None:
                                        constructor_kwargs["subentry_type"] = (
                                            saved_subentry_type
                                        )
                                    raise HomeAssistantError(
                                        "Failed to instantiate ConfigSubentry fallback;"
                                        " see logs for details"
                                    ) from err
                                finally:
                                    if saved_subentry_type is not None:
                                        constructor_kwargs["subentry_type"] = (
                                            saved_subentry_type
                                        )

                                if not isinstance(new_subentry, ConfigSubentry):
                                    raise HomeAssistantError(
                                        "ConfigSubentry fallback returned unexpected instance"
                                    )
                            if new_subentry is None:
                                raise HomeAssistantError(
                                    "Failed to instantiate ConfigSubentry fallback"
                                )

                            # Create a config subentry through HA's API so Core assigns
                            # a stable subentry_id and will later call async_setup_entry.
                            # According to the Home Assistant subentry model, subentries
                            # are created via async_add_subentry and set up via
                            # async_setup_entry to ensure platform callbacks receive the
                            # config_subentry_id context.
                            add_result = self._hass.config_entries.async_add_subentry(
                                self._entry, new_subentry
                            )

                        resolved_add = await self._await_subentry_result(add_result)
                    except data_entry_flow.AbortFlow as err:
                        if err.reason != "already_configured":
                            raise

                        if not deduplicated:
                            await self._deduplicate_subentries()
                            self._refresh_from_entry()
                            deduplicated = True
                            existing = self._managed.get(key)
                            continue

                        _LOGGER.warning(
                            "Subentry sync for key '%s' in entry %s encountered repeated "
                            "unique_id collision for '%s'; adopting existing owner.",
                            key,
                            self._entry.entry_id,
                            unique_id,
                        )

                        existing = await self._async_adopt_existing_unique_id(
                            key,
                            definition,
                            unique_id,
                            payload,
                        )
                        break

                    fallback_subentry_id: str | None = None
                    stored: ConfigSubentry | None
                    if isinstance(resolved_add, ConfigSubentry):
                        stored = resolved_add
                        fallback_subentry_id = getattr(resolved_add, "subentry_id", None)
                    else:
                        stored = new_subentry
                        if new_subentry is not None:
                            fallback_subentry_id = getattr(new_subentry, "subentry_id", None)

                    stored = self._resolve_registered_subentry(
                        key=key,
                        unique_id=unique_id,
                        candidate=stored,
                        fallback_subentry_id=fallback_subentry_id,
                    )

                    resolved_id = _subentry_entry_id(stored)
                    _LOGGER.debug(
                        "[%s] Added subentry %s (unique_id=%s) to config entry %s",
                        self._entry.entry_id,
                        resolved_id,
                        unique_id,
                        self._entry.entry_id,
                    )

                    self._index_managed_subentry(key, stored)
                    created_subentries.append(stored)
                    break

                _LOGGER.debug(
                    "[%s] async_sync: Updating existing subentry for key '%s' (unique_id=%s)",
                    self._entry.entry_id,
                    key,
                    unique_id,
                )
                try:
                    update_kwargs: dict[str, Any] = {
                        "data": payload,
                        "title": definition.title,
                        "unique_id": unique_id,
                    }
                    if translation_key is not None:
                        update_kwargs["translation_key"] = translation_key
                    try:
                        changed = self._hass.config_entries.async_update_subentry(
                            self._entry,
                            existing,
                            **update_kwargs,
                        )
                    except TypeError:
                        update_kwargs.pop("translation_key", None)
                        changed = self._hass.config_entries.async_update_subentry(
                            self._entry,
                            existing,
                            **update_kwargs,
                        )
                except data_entry_flow.AbortFlow as err:
                    if err.reason != "already_configured":
                        raise

                    if not deduplicated:
                        await self._deduplicate_subentries()
                        self._refresh_from_entry()
                        deduplicated = True
                        existing = self._managed.get(key)
                        continue

                    _LOGGER.warning(
                        "Subentry sync for key '%s' in entry %s encountered repeated "
                        "unique_id collision for '%s'; adopting existing owner.",
                        key,
                        self._entry.entry_id,
                        unique_id,
                    )

                    stored_existing = await self._async_adopt_existing_unique_id(
                        key,
                        definition,
                        unique_id,
                        payload,
                    )
                    existing = stored_existing
                    break

                resolved_update = await self._await_subentry_result(changed)

                if isinstance(resolved_update, ConfigSubentry):
                    stored_existing = resolved_update
                elif resolved_update:
                    stored_existing = self._entry.subentries.get(
                        getattr(existing, "subentry_id", None), existing
                    )
                else:
                    stored_existing = None

                if stored_existing is not None:
                    self._index_managed_subentry(key, stored_existing)
                break

            self._cleanup[key] = cleanup

        desired_ids: set[str] = {
            subentry_id
            for managed_key, subentry in list(self._managed.items())
            if managed_key in desired
            and isinstance(
                (subentry_id := getattr(subentry, "subentry_id", None)),
                str,
            )
        }

        stale_keys: list[str] = []
        for managed_key, subentry in list(self._managed.items()):
            if managed_key in desired:
                continue
            subentry_id = getattr(subentry, "subentry_id", None)
            if isinstance(subentry_id, str) and subentry_id in desired_ids:
                self._pop_managed(managed_key)
                self._cleanup.pop(managed_key, None)
                continue
            stale_keys.append(managed_key)

        for key in stale_keys:
            await self.async_remove(key)

        if created_subentries:
            state = getattr(self._entry, "state", None)
            is_initial_setup = isinstance(state, ConfigEntryState) and state in (
                ConfigEntryState.SETUP_IN_PROGRESS,
                ConfigEntryState.SETUP_RETRY,
            )
            await _async_setup_new_subentries(
                self._hass,
                self._entry,
                created_subentries,
                enforce_registration=is_initial_setup,
            )

    async def async_remove(self, key: str) -> None:
        """Remove a managed subentry and run its cleanup callback."""

        subentry = self._pop_managed(key)
        cleanup = self._cleanup.pop(key, None)
        if cleanup is not None:
            try:
                result = cleanup()
                if inspect.isawaitable(result):
                    await result
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.debug("Subentry cleanup for '%s' raised: %s", key, err)

        if subentry is None:
            return

        subentry_id = getattr(subentry, "subentry_id", None)
        if not isinstance(subentry_id, str) or not subentry_id:
            return

        try:
            remove_result = self._hass.config_entries.async_remove_subentry(
                self._entry, subentry_id
            )
            await self._await_subentry_result(remove_result)
        except Exception as err:  # pragma: no cover - defensive logging
            _LOGGER.debug("Removing subentry '%s' failed: %s", key, err)

    async def async_remove_all(self) -> None:
        """Remove all managed subentries."""

        for key in list(self._managed):
            await self.async_remove(key)


@dataclass(slots=True)
class RuntimeData:
    """Container for per-entry runtime structures shared across platforms."""

    coordinator: GoogleFindMyCoordinator
    token_cache: TokenCache
    subentry_manager: ConfigEntrySubEntryManager
    fcm_receiver: FcmReceiverHAType | None = None
    google_home_filter: GoogleHomeFilterProtocol | None = None
    entity_recovery_manager: EntityRecoveryManager | None = None
    legacy_forwarded_platforms: set[str] | None = None
    legacy_forward_notice: bool = False

    @property
    def cache(self) -> TokenCache:
        """Legacy alias for the entry-scoped token cache."""

        return self.token_cache

type MyConfigEntry = ConfigEntry


SUBENTRY_FORWARD_HELPER_LOG_KEY: Literal[
    "_subentry_forward_helper_logs"
] = "_subentry_forward_helper_logs"

class GoogleFindMyDomainData(TypedDict, total=False):
    """Typed container describing objects stored under ``hass.data[DOMAIN]``."""

    device_owner_index: dict[str, str]
    entries: dict[str, RuntimeData]
    fcm_lock: asyncio.Lock
    fcm_receiver: FcmReceiverHAType
    fcm_refcount: int
    fcm_lock_contention_count: int
    initial_setup_complete: bool
    nova_refcount: int
    services_lock: asyncio.Lock
    services_registered: bool
    providers_registered: bool
    views_registered: bool
    _subentry_forward_helper_logs: set[str]
    _subentry_setup_history: dict[str, set[str]]


def _domain_data(hass: HomeAssistant) -> GoogleFindMyDomainData:
    """Return the typed domain data bucket, creating it on first access."""

    return cast(GoogleFindMyDomainData, hass.data.setdefault(DOMAIN, {}))


def _subentry_setup_tracker(
    hass: HomeAssistant, parent_entry: MyConfigEntry
) -> set[str]:
    """Return (and cache) the per-entry subentry setup tracker."""

    bucket = _domain_data(hass)
    tracker_bucket = bucket.setdefault("_subentry_setup_history", {})
    tracker = tracker_bucket.get(parent_entry.entry_id)
    if not isinstance(tracker, set):
        tracker = set()
        tracker_bucket[parent_entry.entry_id] = tracker
    return tracker


def _registered_subentry_ids(
    hass: HomeAssistant, parent_entry: MyConfigEntry | str | None
) -> set[str]:
    """Return identifiers for subentries currently registered on ``parent_entry``.

    Home Assistant raises ``UnknownSubEntry`` when a subentry setup is triggered
    before the subentry is registered. To catch that early, gather every
    identifier the parent entry advertises so callers can log and abort before
    scheduling setup attempts.
    """

    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        return set()

    parent: Any | None
    if isinstance(parent_entry, str):
        get_entry = getattr(config_entries, "async_get_entry", lambda _entry_id: None)
        parent = get_entry(parent_entry)
    elif parent_entry is None:
        parent = None
    else:
        parent = parent_entry

    if parent is None and hasattr(parent_entry, "entry_id"):
        parent = parent_entry

    if parent is None:
        return set()

    registered: set[str] = set()
    registered.update(
        {registered_id for registered_id in getattr(parent, "_registered_subentry_ids", set()) if isinstance(registered_id, str)}
    )
    parent_subentries = getattr(parent, "subentries", None)
    if isinstance(parent_subentries, Mapping):
        for subentry_id, subentry in parent_subentries.items():
            if isinstance(subentry_id, str) and subentry_id:
                registered.add(subentry_id)

            identifier = _resolve_config_subentry_identifier(subentry)
            if identifier:
                registered.add(identifier)

    async_get_subentries = getattr(config_entries, "async_get_subentries", None)
    if callable(async_get_subentries):
        for subentry in async_get_subentries(parent.entry_id):
            identifier = _resolve_config_subentry_identifier(subentry)
            if identifier:
                registered.add(identifier)

    return registered


async def _async_setup_new_subentries(
    hass: HomeAssistant,
    parent_entry: MyConfigEntry,
    subentries: Iterable[ConfigSubentry | Any],
    *,
    enforce_registration: bool = False,
) -> None:
    """Trigger Home Assistant setup for newly created subentries.

    Home Assistant does not automatically set up subentries when they are added
    programmatically. After creating a ConfigSubentry (via async_sync or other
    helpers), the integration must call ``hass.config_entries.async_setup`` for
    each new entry id so Core can run ``_async_setup_subentry`` and forward
    platforms with the correct ``config_subentry_id`` context. See
    docs/CONFIG_SUBENTRIES_HANDBOOK.md for the canonical lifecycle.
    """

    setup_tracker = _subentry_setup_tracker(hass, parent_entry)

    parent_subentries = getattr(parent_entry, "subentries", None)
    parent_subentry_ids: set[str] = set()
    if isinstance(parent_subentries, Mapping):
        for key, subentry in parent_subentries.items():
            if isinstance(key, str) and key:
                parent_subentry_ids.add(key)
            identifier = _resolve_config_subentry_identifier(subentry)
            if isinstance(identifier, str) and identifier:
                parent_subentry_ids.add(identifier)
            entry_identifier = getattr(subentry, "entry_id", None)
            if isinstance(entry_identifier, str) and entry_identifier:
                parent_subentry_ids.add(entry_identifier)
    resolved_subentries: list[ConfigSubentry | Any] = []
    for subentry in subentries:
        resolved = subentry
        if isinstance(parent_subentries, Mapping):
            unique_id = getattr(subentry, "unique_id", None)
            if isinstance(unique_id, str):
                resolved = next(
                    (
                        candidate
                        for candidate in parent_subentries.values()
                        if getattr(candidate, "unique_id", None) == unique_id
                    ),
                    subentry,
                )
        resolved_subentries.append(resolved)

    setup_targets: list[tuple[str, str | None]] = []
    for subentry in resolved_subentries:
        subentry_entry_id = _subentry_entry_id(subentry)
        fallback_entry_id = None

        explicit_entry_id = getattr(subentry, "entry_id", None)
        if (
            isinstance(explicit_entry_id, str)
            and explicit_entry_id
            and explicit_entry_id != subentry_entry_id
        ):
            fallback_entry_id = explicit_entry_id

        if subentry_entry_id is None:
            subentry_entry_id = fallback_entry_id

        if subentry_entry_id is None or subentry_entry_id in setup_tracker:
            continue
        setup_tracker.add(subentry_entry_id)
        if fallback_entry_id is not None:
            setup_tracker.add(fallback_entry_id)
        setup_targets.append((subentry_entry_id, fallback_entry_id))

    if not setup_targets:
        return

    # Yield to the event loop so Home Assistant can finish publishing the
    # subentry into the registry before setup runs (race guard documented in
    # agents/runtime_patterns/AGENTS.md).
    await asyncio.sleep(0)

    block_till_done = getattr(hass, "async_block_till_done", None)
    if callable(block_till_done):
        await block_till_done()

    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        _LOGGER.debug(
            "[%s] async_setup_entry: Skipping subentry setup because manager missing",
            parent_entry.entry_id,
        )
        return

    get_entry = getattr(config_entries, "async_get_entry", lambda _entry_id: None)
    registered_subentry_ids = _registered_subentry_ids(hass, parent_entry)
    if not registered_subentry_ids:
        _LOGGER.warning(
            "[%s] No registered config subentries visible before scheduling",  # noqa: G004
            parent_entry.entry_id,
        )
    else:
        _LOGGER.debug(
            "[%s] Registered config subentries available before scheduling: %s",
            parent_entry.entry_id,
            sorted(registered_subentry_ids),
        )

    missing_registrations: set[str] = set()
    retry_missing_registrations = False

    for target, fallback_target in setup_targets:
        for candidate in (target, fallback_target):
            if candidate is None:
                continue
            if candidate not in parent_subentry_ids:
                _LOGGER.error(
                    "[%s] Subentry %s not registered for entry %s",  # noqa: G004
                    parent_entry.entry_id,
                    candidate,
                    parent_entry.entry_id,
                )
                raise HomeAssistantError(
                    f"Subentry {candidate} not found in config entry"
                )

        for registration_candidate in (target, fallback_target):
            if registration_candidate is None:
                continue

            if (
                not registered_subentry_ids
                or registration_candidate not in registered_subentry_ids
            ):
                _LOGGER.error(
                    "[%s] Config subentry %s not registered; cannot schedule setup",
                    parent_entry.entry_id,
                    registration_candidate,
                )
                missing_registrations.add(registration_candidate)
                retry_missing_registrations = retry_missing_registrations or not registered_subentry_ids
                if not registered_subentry_ids:
                    raise ConfigEntryNotReady(
                        f"Config subentry {registration_candidate} not registered"
                    )

                raise HomeAssistantError(
                    f"Config subentry {registration_candidate} missing from registry"
                )

            _LOGGER.debug(
                "[%s] Scheduling setup for config subentry '%s'",  # noqa: G004
                parent_entry.entry_id,
                registration_candidate,
            )
            entry_obj = get_entry(registration_candidate)
            state = getattr(entry_obj, "state", None)
            if isinstance(state, ConfigEntryState) and state in (
                ConfigEntryState.SETUP_IN_PROGRESS,
                ConfigEntryState.LOADED,
            ):
                break

            try:
                await hass.config_entries.async_setup(registration_candidate)
                _LOGGER.debug(
                    "[%s] Scheduled setup for config subentry '%s'",
                    parent_entry.entry_id,
                    registration_candidate,
                )
            except UnknownEntry:
                _LOGGER.error(
                    "[%s] Config subentry %s not registered; cannot set up",
                    parent_entry.entry_id,
                    registration_candidate,
                )
                missing_registrations.add(registration_candidate)
                retry_missing_registrations = True
                continue
            except Exception as err:  # pragma: no cover - defensive logging
                _LOGGER.debug(
                    "[%s] async_setup_entry: Subentry '%s' setup raised: %s",
                    parent_entry.entry_id,
                    registration_candidate,
                    err,
                )
                continue

    if missing_registrations:
        message = "; ".join(
            (
                "Config subentry "
                f"{candidate} missing for parent {parent_entry.entry_id}"
                for candidate in sorted(missing_registrations)
            )
        )

        if retry_missing_registrations:
            raise ConfigEntryNotReady(message)

        raise HomeAssistantError(message)


def _ensure_fcm_lock(bucket: GoogleFindMyDomainData) -> asyncio.Lock:
    """Return the shared FCM lock, creating it if missing."""

    lock = bucket.get("fcm_lock")
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        bucket["fcm_lock"] = lock
    return lock


def _ensure_services_lock(bucket: GoogleFindMyDomainData) -> asyncio.Lock:
    """Return the integration services lock, creating it if missing."""

    lock = bucket.get("services_lock")
    if not isinstance(lock, asyncio.Lock):
        lock = asyncio.Lock()
        bucket["services_lock"] = lock
    return lock


def _ensure_entries_bucket(bucket: GoogleFindMyDomainData) -> dict[str, RuntimeData]:
    """Return the per-entry runtime data bucket."""

    entries = bucket.get("entries")
    if not isinstance(entries, dict):
        entries = {}
        bucket["entries"] = entries
    return entries


def _ensure_device_owner_index(bucket: GoogleFindMyDomainData) -> dict[str, str]:
    """Return the shared device owner index mapping."""

    owner_index = bucket.get("device_owner_index")
    if not isinstance(owner_index, dict):
        owner_index = {}
        bucket["device_owner_index"] = owner_index
    return owner_index


def _has_logged_missing_subentry_forward_helper(
    hass: HomeAssistant, entry: ConfigEntry
) -> bool:
    """Return True if the missing forward helper warning already logged."""

    if getattr(entry, "_gfm_logged_subentry_forward_absence", False):
        return True

    entry_id = getattr(entry, "entry_id", None)
    if not isinstance(entry_id, str):
        return False

    bucket = _domain_data(hass)
    logged = bucket.get(SUBENTRY_FORWARD_HELPER_LOG_KEY)
    if isinstance(logged, set):
        return entry_id in logged
    return False


def _mark_missing_subentry_forward_helper_logged(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Record that the missing forward helper warning has been emitted."""

    try:
        setattr(entry, "_gfm_logged_subentry_forward_absence", True)
    except (AttributeError, TypeError):
        pass

    bucket = _domain_data(hass)
    logged = bucket.get(SUBENTRY_FORWARD_HELPER_LOG_KEY)
    if not isinstance(logged, set):
        logged = set()
        bucket[SUBENTRY_FORWARD_HELPER_LOG_KEY] = logged

    entry_id = getattr(entry, "entry_id", None)
    if isinstance(entry_id, str):
        logged.add(entry_id)


def _get_fcm_receiver(
    bucket: GoogleFindMyDomainData,
) -> FcmReceiverHAType | None:
    """Return the cached shared FCM receiver if present."""

    receiver: object | None = bucket.get("fcm_receiver")
    receiver_cls = _resolve_fcm_receiver_class()
    if isinstance(receiver, receiver_cls):
        return receiver
    return None


def _set_fcm_receiver(
    bucket: GoogleFindMyDomainData, receiver: FcmReceiverHAType
) -> None:
    """Store the shared FCM receiver."""

    bucket["fcm_receiver"] = receiver


def _pop_fcm_receiver(
    bucket: GoogleFindMyDomainData,
) -> FcmReceiverHAType | None:
    """Remove and return the cached shared FCM receiver."""

    receiver: object | None = bucket.pop("fcm_receiver", None)
    receiver_cls = _resolve_fcm_receiver_class()
    if isinstance(receiver, receiver_cls):
        return receiver
    return None


def _pop_any_fcm_receiver(bucket: GoogleFindMyDomainData) -> object | None:
    """Remove and return the cached shared FCM receiver regardless of type."""

    return bucket.pop("fcm_receiver", None)


def _get_fcm_refcount(bucket: GoogleFindMyDomainData) -> int:
    """Return the current shared FCM refcount."""

    value = bucket.get("fcm_refcount")
    if isinstance(value, int):
        return value
    return 0


def _set_fcm_refcount(bucket: GoogleFindMyDomainData, value: int) -> None:
    """Persist the shared FCM refcount."""

    bucket["fcm_refcount"] = value


def _get_nova_refcount(bucket: GoogleFindMyDomainData) -> int:
    """Return the Nova API session provider refcount."""

    value = bucket.get("nova_refcount")
    if isinstance(value, int):
        return value
    return 0


def _set_nova_refcount(bucket: GoogleFindMyDomainData, value: int) -> None:
    """Persist the Nova API session provider refcount."""

    bucket["nova_refcount"] = value


def _domain_fcm_provider(hass: HomeAssistant) -> FcmReceiverHAType:
    """Return the shared FCM receiver for provider callbacks."""

    bucket = _domain_data(hass)
    receiver = _get_fcm_receiver(bucket)
    if receiver is None:  # pragma: no cover - defensive guard
        raise RuntimeError("Shared FCM receiver unavailable")
    return receiver


async def _async_stop_receiver_if_possible(receiver: object | None) -> None:
    """Invoke ``async_stop`` on ``receiver`` when available."""

    if receiver is None:
        return

    stop_callable = getattr(receiver, "async_stop", None)
    if stop_callable is None:
        return

    try:
        result = stop_callable()
    except Exception as err:  # pragma: no cover - defensive
        _LOGGER.debug("Failed to call async_stop on stale FCM receiver: %s", err)
        return

    if inspect.isawaitable(result):
        try:
            await cast(Awaitable[object], result)
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug("async_stop coroutine failed: %s", err)


def _normalize_device_identifier(device: dr.DeviceEntry | Any, ident: str) -> str:
    """Return the Google device id portion used throughout the coordinator.

    Registry identifiers may be namespaced with the config entry ID and/or a
    subentry identifier (for example, ``<entry_id>:<subentry_id>:<device_id>``).
    The coordinator and ignore lists track devices by their Google-provided
    ``device_id`` alone, so we strip any entry/subentry prefixes and return the
    final segment when present. Service-device identifiers (for example,
    ``integration_<entry_id>``) are preserved as-is because they do not contain
    path separators.
    """

    if not ident:
        return ident

    if ":" not in ident:
        return ident

    parts = ident.split(":")

    config_entries: Collection[str] | None = getattr(device, "config_entries", None)
    if config_entries:
        while len(parts) > 1 and parts[0] in config_entries:
            parts = parts[1:]

    # Prefer the final segment so trackers with entry/subentry prefixes resolve
    # to the same canonical device_id used by the coordinator and ignore list.
    last = parts[-1]
    return last or ident


def _iter_config_entry_entities(
    entity_registry: er.EntityRegistry, entry_id: str
) -> tuple[er.RegistryEntry, ...]:
    """Return entity registry entries belonging to a config entry."""

    helper = getattr(er, "async_entries_for_config_entry", None)
    if callable(helper):
        entries_iterable = helper(entity_registry, entry_id)
    else:
        registry_helper = getattr(
            entity_registry, "async_entries_for_config_entry", None
        )
        if callable(registry_helper):
            entries_iterable = registry_helper(entry_id)
        else:
            entries_iterable = [
                entity_entry
                for entity_entry in getattr(entity_registry, "entities", {}).values()
                if getattr(entity_entry, "config_entry_id", None) == entry_id
            ]

    entries = tuple(entries_iterable)
    if entries:
        return entries

    fallback_entities = getattr(entity_registry, "entities", None)
    candidates: Iterable[er.RegistryEntry]
    if fallback_entities is None:
        candidates = ()
    else:
        values = getattr(fallback_entities, "values", None)
        if callable(values):
            candidates = values()
        elif isinstance(fallback_entities, Mapping):
            candidates = fallback_entities.values()
        else:
            candidates = ()

    return tuple(
        entity_entry
        for entity_entry in candidates
        if getattr(entity_entry, "config_entry_id", None) == entry_id
    )


@dataclass(slots=True)
class _RecoveryRegistration:
    """Container describing recovery metadata for a platform."""

    expected_unique_ids: Callable[[], set[str]]
    entity_factory: Callable[[set[str]], Sequence[Entity]]
    add_entities: AddEntitiesCallback
    update_before_add: bool


class EntityRecoveryManager:
    """Coordinate recovery of missing entities for a config entry."""

    __slots__ = ("_hass", "_entry", "_coordinator", "_platforms")

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: GoogleFindMyCoordinator,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._platforms: dict[str, _RecoveryRegistration] = {}

    def _register_platform(
        self,
        platform: Platform | str,
        registration: _RecoveryRegistration,
    ) -> None:
        key = platform.value if isinstance(platform, Platform) else str(platform)
        self._platforms[key] = registration

    def register_device_tracker_platform(
        self,
        *,
        expected_unique_ids: Callable[[], set[str]],
        entity_factory: Callable[[set[str]], Sequence[Entity]],
        add_entities: AddEntitiesCallback,
        update_before_add: bool = True,
    ) -> None:
        """Register recovery handlers for the device_tracker platform."""

        self._register_platform(
            Platform.DEVICE_TRACKER,
            _RecoveryRegistration(
                expected_unique_ids=expected_unique_ids,
                entity_factory=entity_factory,
                add_entities=add_entities,
                update_before_add=update_before_add,
            ),
        )

    def register_button_platform(
        self,
        *,
        expected_unique_ids: Callable[[], set[str]],
        entity_factory: Callable[[set[str]], Sequence[Entity]],
        add_entities: AddEntitiesCallback,
        update_before_add: bool = True,
    ) -> None:
        """Register recovery handlers for the button platform."""

        self._register_platform(
            Platform.BUTTON,
            _RecoveryRegistration(
                expected_unique_ids=expected_unique_ids,
                entity_factory=entity_factory,
                add_entities=add_entities,
                update_before_add=update_before_add,
            ),
        )

    def register_sensor_platform(
        self,
        *,
        expected_unique_ids: Callable[[], set[str]],
        entity_factory: Callable[[set[str]], Sequence[Entity]],
        add_entities: AddEntitiesCallback,
        update_before_add: bool = True,
    ) -> None:
        """Register recovery handlers for the sensor platform."""

        self._register_platform(
            Platform.SENSOR,
            _RecoveryRegistration(
                expected_unique_ids=expected_unique_ids,
                entity_factory=entity_factory,
                add_entities=add_entities,
                update_before_add=update_before_add,
            ),
        )

    def register_binary_sensor_platform(
        self,
        *,
        expected_unique_ids: Callable[[], set[str]],
        entity_factory: Callable[[set[str]], Sequence[Entity]],
        add_entities: AddEntitiesCallback,
        update_before_add: bool = True,
    ) -> None:
        """Register recovery handlers for the binary_sensor platform."""

        self._register_platform(
            Platform.BINARY_SENSOR,
            _RecoveryRegistration(
                expected_unique_ids=expected_unique_ids,
                entity_factory=entity_factory,
                add_entities=add_entities,
                update_before_add=update_before_add,
            ),
        )

    def expected_unique_ids_for_platform(
        self, platform: Platform | str
    ) -> set[str]:
        """Return the expected unique IDs for ``platform`` if registered."""

        key = platform.value if isinstance(platform, Platform) else str(platform)
        registration = self._platforms.get(key)
        if registration is None:
            return set()
        return registration.expected_unique_ids()

    async def async_recover_missing_entities(self) -> None:
        """Inspect the entity registry and recover missing entities."""

        if not self._platforms:
            return

        registry_key = getattr(er, "DATA_REGISTRY", "entity_registry")
        entity_registry = self._hass.data.get(registry_key)
        if entity_registry is None:
            entity_registry = er.async_get(self._hass)
        registry_entries = _iter_config_entry_entities(
            entity_registry, self._entry.entry_id
        )

        by_platform: dict[str, set[str]] = {}
        for entry in registry_entries:
            platform = getattr(entry, "domain", None)
            if not isinstance(platform, str):
                continue
            owner = getattr(
                entry,
                "integration_domain",
                getattr(entry, "platform", None),
            )
            if owner is None:
                # Home Assistant 2025.10+ exposes ``integration_domain`` on entity
                # registry entries while older cores only expose ``platform``.
                # Falling back to ``DOMAIN`` keeps legacy builds compatible and
                # documents why both attributes remain supported during recovery.
                owner = DOMAIN
            if owner != DOMAIN:
                continue
            unique_id = getattr(entry, "unique_id", None)
            if not isinstance(unique_id, str) or not unique_id:
                continue
            by_platform.setdefault(platform, set()).add(unique_id)

        for platform, registration in self._platforms.items():
            try:
                expected = registration.expected_unique_ids()
            except Exception as err:  # pragma: no cover - defensive guard
                _LOGGER.debug(
                    "[%s] Failed to build expected unique_id set for %s: %s",
                    self._entry.entry_id,
                    platform,
                    err,
                )
                continue

            if not expected:
                continue

            actual = by_platform.get(platform, set())
            missing = expected - actual
            if not missing:
                continue

            try:
                entities = registration.entity_factory(missing)
            except Exception as err:  # pragma: no cover - defensive guard
                _LOGGER.debug(
                    "[%s] Failed to build recovery entities for %s: %s",
                    self._entry.entry_id,
                    platform,
                    err,
                )
                continue

            to_add: list[Entity] = []
            for entity in entities:
                unique_id = getattr(entity, "unique_id", None)
                if not isinstance(unique_id, str):
                    continue
                if unique_id not in missing:
                    continue
                to_add.append(entity)

            if not to_add:
                continue

            _LOGGER.info(
                "[%s] Recovering %d missing %s entities",
                self._entry.entry_id,
                len(to_add),
                platform,
            )

            try:
                registration.add_entities(to_add, registration.update_before_add)
            except Exception as err:  # pragma: no cover - defensive guard
                _LOGGER.error(
                    "[%s] Failed to add recovered %s entities: %s",
                    self._entry.entry_id,
                    platform,
                    err,
                )

def _default_button_subentry_identifier(subentry_map: Mapping[str, str]) -> str:
    """Return the preferred subentry identifier for button entities."""

    identifier = subentry_map.get("button")
    if isinstance(identifier, str) and identifier:
        return identifier

    identifier = subentry_map.get("device_tracker")
    if isinstance(identifier, str) and identifier:
        return identifier

    return _DEFAULT_SUBENTRY_IDENTIFIER


@dataclass(slots=True)
class _ButtonUniqueIdParts:
    """Container describing the parsed components of a button unique_id."""

    entry_id: str
    subentry_id: str
    google_device_id: str
    action: str


_BUTTON_ACTION_SUFFIXES: tuple[str, ...] = (
    "play_sound",
    "stop_sound",
    "locate_device",
)
_LEGACY_BUTTON_SUBENTRY_PREFIXES: tuple[str, ...] = ("tracker",)


def _normalize_legacy_button_remainder(
    remainder: str,
    *,
    identifier: str,
    suffixes: tuple[str, ...],
) -> str:
    """Strip legacy pseudo-subentry tokens before rebuilding button IDs."""

    action_suffix: str | None = None
    for suffix in suffixes:
        if remainder.endswith(suffix):
            action_suffix = suffix
            payload = remainder[: -len(suffix)]
            break
    else:
        return remainder

    if payload.startswith(f"{identifier}_"):
        return remainder

    for legacy_prefix in _LEGACY_BUTTON_SUBENTRY_PREFIXES:
        token = f"{legacy_prefix}_"
        if payload.startswith(token):
            trimmed = payload[len(token) :]
            if trimmed:
                return f"{trimmed}{action_suffix}"
            break

    return remainder


def _parse_button_unique_id(
    unique_id: str,
    entry: ConfigEntry,
    subentry_map: Mapping[str, str],
    fallback_subentry_id: str,
) -> _ButtonUniqueIdParts | None:
    """Return parsed button unique_id data for relinking heuristics."""

    if not isinstance(unique_id, str) or not unique_id:
        return None

    action: str | None = None
    prefix: str | None = None
    for candidate_action in _BUTTON_ACTION_SUFFIXES:
        suffix = f"_{candidate_action}"
        if unique_id.endswith(suffix):
            action = candidate_action
            prefix = unique_id[: -len(suffix)]
            break

    if action is None or prefix is None:
        try:
            prefix, action = unique_id.rsplit("_", 1)
        except ValueError:
            return None
        if not action:
            return None

    trimmed = prefix
    domain_prefix = f"{DOMAIN}_"
    if trimmed.startswith(domain_prefix):
        trimmed = trimmed[len(domain_prefix) :]

    entry_id = entry.entry_id
    if not isinstance(entry_id, str) or not entry_id:
        return None

    remainder = trimmed

    if ":" in trimmed:
        candidate_entry_id, potential_rest = trimmed.split(":", 1)
        if candidate_entry_id:
            if candidate_entry_id != entry_id:
                return None
            remainder = potential_rest
    elif trimmed.startswith(f"{entry_id}_"):
        remainder = trimmed[len(entry_id) + 1 :]
    elif trimmed.startswith(entry_id + ":"):
        remainder = trimmed[len(entry_id) + 1 :]
    elif trimmed.startswith(entry_id):
        suffix = trimmed[len(entry_id) :]
        if suffix.startswith("_") or suffix.startswith(":"):
            remainder = suffix[1:]
        elif suffix:
            remainder = trimmed

    if not remainder:
        return None

    subentry_id: str | None = None
    google_device_id = remainder

    if ":" in remainder:
        maybe_subentry, maybe_device = remainder.split(":", 1)
        if maybe_device:
            subentry_id = maybe_subentry or None
            google_device_id = maybe_device
        else:
            google_device_id = maybe_subentry
    else:
        known_identifiers = {
            identifier
            for identifier in subentry_map.values()
            if isinstance(identifier, str) and identifier
        }
        for candidate in sorted(known_identifiers, key=len, reverse=True):
            token = f"{candidate}_"
            if remainder.startswith(token):
                subentry_id = candidate
                google_device_id = remainder[len(token) :]
                break

    if not google_device_id:
        return None

    if subentry_id is None:
        subentry_id = fallback_subentry_id

    return _ButtonUniqueIdParts(
        entry_id=entry_id,
        subentry_id=subentry_id,
        google_device_id=google_device_id,
        action=action,
    )


def _iter_tracker_identifier_candidates(
    parts: _ButtonUniqueIdParts,
) -> tuple[tuple[str, str], ...]:
    """Return identifier candidates for locating the tracker device."""

    primary = (DOMAIN, f"{parts.entry_id}:{parts.subentry_id}:{parts.google_device_id}")
    secondary = (DOMAIN, f"{parts.entry_id}:{parts.google_device_id}")
    tertiary = (DOMAIN, parts.google_device_id)
    return primary, secondary, tertiary


def _device_is_service_device(device: dr.DeviceEntry | Any, entry_id: str) -> bool:
    """Return True if the registry device represents the integration service device."""

    if device is None:
        return False

    device_entry_type_cls = getattr(dr, "DeviceEntryType", None)
    if device_entry_type_cls is not None:
        service_entry_type = getattr(device_entry_type_cls, "SERVICE", "service")
    else:
        service_entry_type = "service"
    entry_type = getattr(device, "entry_type", None)
    if entry_type == service_entry_type:
        return True
    if (
        isinstance(entry_type, str)
        and isinstance(service_entry_type, str)
        and entry_type.lower() == service_entry_type.lower()
    ):
        return True

    service_identifiers = {
        service_device_identifier(entry_id)[1],
        LEGACY_SERVICE_IDENTIFIER,
    }

    identifiers = getattr(device, "identifiers", None)
    if not isinstance(identifiers, Collection):
        return False

    for item in identifiers:
        try:
            domain, ident = item
        except (TypeError, ValueError):
            continue
        if domain != DOMAIN or not isinstance(ident, str) or not ident:
            continue
        canonical = _normalize_device_identifier(device, ident)
        if (
            canonical in service_identifiers
            or canonical.endswith(":service")
        ):
            return True

    return False


async def _async_relink_button_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Ensure button entities are linked to their physical tracker devices."""

    try:
        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
    except Exception as err:  # noqa: BLE001 - defensive guard
        _LOGGER.debug(
            "googlefindmy(%s): registry acquisition failed during button relink: %s",
            entry.entry_id,
            err,
        )
        return

    if not getattr(entity_registry, "entities", None):
        return
    if not getattr(device_registry, "devices", None):
        return

    subentry_map = _resolve_subentry_identifier_map(entry)
    fallback_subentry_id = _default_button_subentry_identifier(subentry_map)
    registry_entries = _iter_config_entry_entities(entity_registry, entry.entry_id)

    fixed = 0

    for entity_entry in registry_entries:
        try:
            if entity_entry.domain != "button" or entity_entry.platform != DOMAIN:
                continue

            current_device_id = getattr(entity_entry, "device_id", None)
            current_device = (
                device_registry.async_get(current_device_id)
                if isinstance(current_device_id, str) and current_device_id
                else None
            )

            if current_device and entry.entry_id in getattr(
                current_device, "config_entries", ()
            ):
                if not _device_is_service_device(current_device, entry.entry_id):
                    continue

            parsed = _parse_button_unique_id(
                getattr(entity_entry, "unique_id", ""),
                entry,
                subentry_map,
                fallback_subentry_id,
            )
            if parsed is None:
                _LOGGER.debug(
                    "googlefindmy(%s): unable to parse button unique_id '%s'",
                    entry.entry_id,
                    getattr(entity_entry, "unique_id", ""),
                )
                continue

            target_device: dr.DeviceEntry | Any | None = None
            for candidate in _iter_tracker_identifier_candidates(parsed):
                device = device_registry.async_get_device(identifiers={candidate})
                if device is None:
                    continue
                config_entries = cast(
                    Collection[str], getattr(device, "config_entries", ())
                )
                if entry.entry_id not in config_entries:
                    continue
                if _device_is_service_device(device, entry.entry_id):
                    continue
                target_device = device
                break

            if target_device is None:
                _LOGGER.debug(
                    "googlefindmy(%s): tracker device not found for %s (%s)",
                    entry.entry_id,
                    getattr(entity_entry, "entity_id", "<unknown>"),
                    parsed.google_device_id,
                )
                continue

            if current_device and getattr(current_device, "id", None) == getattr(
                target_device, "id", None
            ):
                continue

            entity_registry.async_update_entity(
                entity_entry.entity_id, device_id=getattr(target_device, "id", None)
            )
            fixed += 1
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "googlefindmy(%s): relink failed for %s: %s",
                entry.entry_id,
                getattr(entity_entry, "entity_id", "<unknown>"),
                err,
            )

    _LOGGER.debug(
        "googlefindmy(%s): relinked %d button entities",
        entry.entry_id,
        fixed,
    )


async def _async_relink_subentry_entities(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Attach tracker/service entities to devices with matching subentry metadata."""

    entry_id = getattr(entry, "entry_id", "") or ""
    if not entry_id:
        return

    try:
        entity_registry = er.async_get(hass)
        device_registry = dr.async_get(hass)
    except Exception as err:  # noqa: BLE001 - defensive guard
        _LOGGER.debug(
            "googlefindmy(%s): registry acquisition failed during entity relink: %s",
            entry_id,
            err,
        )
        return

    if not getattr(entity_registry, "entities", None):
        return
    if not getattr(device_registry, "devices", None):
        return

    subentry_map = _resolve_subentry_identifier_map(entry)
    tracker_subentry_id = subentry_map.get("device_tracker", _DEFAULT_SUBENTRY_IDENTIFIER)
    if not isinstance(tracker_subentry_id, str) or not tracker_subentry_id:
        tracker_subentry_id = _DEFAULT_SUBENTRY_IDENTIFIER

    service_subentry_id = subentry_map.get("binary_sensor", _DEFAULT_SUBENTRY_IDENTIFIER)
    if not isinstance(service_subentry_id, str) or not service_subentry_id:
        service_subentry_id = _DEFAULT_SUBENTRY_IDENTIFIER

    registry_entries = _iter_config_entry_entities(entity_registry, entry.entry_id)

    allowed_domains: tuple[str, ...] = ("sensor", "binary_sensor", "device_tracker")
    fixed = 0

    def _lookup_device(identifier: tuple[str, str]) -> dr.DeviceEntry | Any | None:
        """Return the registry device matching ``identifier`` when available."""

        get_device = getattr(device_registry, "async_get_device", None)
        if callable(get_device):
            try:
                device = get_device(identifiers={identifier})
            except TypeError:
                try:
                    device = cast(Callable[[Collection[tuple[str, str]]], Any], get_device)(
                        {identifier}
                    )
                except TypeError:
                    device = None
        else:
            device = None

        if device is not None:
            return device

        devices_iterable = getattr(device_registry, "devices", {})
        if isinstance(devices_iterable, Mapping):
            candidates: Iterable[Any] = devices_iterable.values()
        else:
            candidates = cast(Iterable[Any], devices_iterable) or ()

        for candidate in candidates:
            identifiers = getattr(candidate, "identifiers", None)
            if not isinstance(identifiers, Collection):
                continue
            if identifier in identifiers:
                return candidate

        return None

    service_identifiers: tuple[tuple[str, str], ...]
    service_identifiers = (
        service_device_identifier(entry_id),
        (DOMAIN, f"{entry_id}:{service_subentry_id}:service"),
    )

    service_device_cache: dr.DeviceEntry | Any | None = None

    def _resolve_service_device() -> dr.DeviceEntry | Any | None:
        """Return the service device assigned to the service subentry."""

        nonlocal service_device_cache
        if service_device_cache is not None:
            return service_device_cache

        for identifier in service_identifiers:
            device = _lookup_device(identifier)
            if device is None:
                continue
            config_entries = cast(
                Collection[str], getattr(device, "config_entries", ())
            )
            if entry_id not in config_entries:
                continue
            device_subentry = getattr(device, "config_subentry_id", None)
            if (
                isinstance(device_subentry, str)
                and device_subentry
                and device_subentry != service_subentry_id
            ):
                continue
            if not _device_is_service_device(device, entry_id):
                continue
            service_device_cache = device
            return service_device_cache

        devices_iterable = getattr(device_registry, "devices", {})
        if isinstance(devices_iterable, Mapping):
            candidates = cast(Iterable[Any], devices_iterable.values())
        else:
            candidates = cast(Iterable[Any], devices_iterable) or ()

        for device in candidates:
            config_entries = cast(
                Collection[str], getattr(device, "config_entries", ())
            )
            if entry_id not in config_entries:
                continue
            if not _device_is_service_device(device, entry_id):
                continue
            device_subentry = getattr(device, "config_subentry_id", None)
            if (
                isinstance(device_subentry, str)
                and device_subentry
                and device_subentry != service_subentry_id
            ):
                continue
            service_device_cache = device
            break

        return service_device_cache

    known_identifiers = {
        identifier
        for identifier in subentry_map.values()
        if isinstance(identifier, str) and identifier
    }

    subentries = getattr(entry, "subentries", None)
    if isinstance(subentries, Mapping):
        for subentry in subentries.values():
            identifier = getattr(subentry, "subentry_id", None)
            if isinstance(identifier, str) and identifier:
                known_identifiers.add(identifier)
            data = getattr(subentry, "data", {}) or {}
            group_key_raw = data.get("group_key") if isinstance(data, Mapping) else None
            group_key = (
                str(group_key_raw).strip()
                if isinstance(group_key_raw, str) and group_key_raw.strip()
                else ""
            )
            if group_key == TRACKER_SUBENTRY_KEY:
                tracker_subentry_id = identifier or tracker_subentry_id
            elif group_key == SERVICE_SUBENTRY_KEY:
                service_subentry_id = identifier or service_subentry_id

    def _extract_tracker_binding_from_tracker(
        entity_entry: er.RegistryEntry,
    ) -> tuple[str, str] | None:
        """Return ``(subentry_id, google_device_id)`` for tracker entities."""

        uid = getattr(entity_entry, "unique_id", "") or ""
        if not uid:
            return None

        remainder = uid
        if remainder.startswith(f"{entry_id}:"):
            remainder = remainder[len(entry_id) + 1 :]

        subentry_candidate: str | None = None
        google_device_id: str | None = None

        if ":" in remainder:
            subentry_candidate, google_device_id = remainder.split(":", 1)
        else:
            google_device_id = remainder

        if not google_device_id:
            return None

        if not subentry_candidate:
            subentry_candidate = tracker_subentry_id

        return subentry_candidate, google_device_id

    def _extract_tracker_binding_from_sensor(
        entity_entry: er.RegistryEntry,
    ) -> tuple[str, str] | None:
        """Return tracker binding metadata for per-device sensors."""

        uid = getattr(entity_entry, "unique_id", "") or ""
        suffix = "_last_seen"
        if not uid or not uid.endswith(suffix):
            return None

        payload = uid[: -len(suffix)]
        if payload.endswith("_"):
            payload = payload[:-1]

        prefix = f"{DOMAIN}_{entry_id}_"
        if entry_id and payload.startswith(prefix):
            remainder = payload[len(prefix) :]
        elif payload.startswith(f"{DOMAIN}_"):
            remainder = payload[len(f"{DOMAIN}_") :]
        elif entry_id and payload.startswith(f"{entry_id}_"):
            remainder = payload[len(entry_id) + 1 :]
        else:
            remainder = payload

        if not remainder:
            return None

        subentry_candidate = tracker_subentry_id
        google_device_id = remainder

        for identifier in sorted(known_identifiers, key=len, reverse=True):
            token = f"{identifier}_"
            if remainder.startswith(token):
                maybe_device = remainder[len(token) :]
                if maybe_device:
                    subentry_candidate = identifier
                    google_device_id = maybe_device
                    break

        if not google_device_id:
            return None

        return subentry_candidate, google_device_id

    def _find_tracker_device(
        subentry_id: str, google_device_id: str
    ) -> dr.DeviceEntry | Any | None:
        """Return the tracker device for ``google_device_id`` when present."""

        parts = _ButtonUniqueIdParts(
            entry_id=entry_id,
            subentry_id=subentry_id,
            google_device_id=google_device_id,
            action="relink",
        )

        for candidate in _iter_tracker_identifier_candidates(parts):
            device = _lookup_device(candidate)
            if device is None:
                continue
            config_entries = cast(
                Collection[str], getattr(device, "config_entries", ())
            )
            if entry_id not in config_entries:
                continue
            device_subentry = getattr(device, "config_subentry_id", None)
            if (
                isinstance(device_subentry, str)
                and device_subentry
                and device_subentry != subentry_id
            ):
                continue
            if _device_is_service_device(device, entry_id):
                continue
            return device

        return None

    for entity_entry in registry_entries:
        try:
            if entity_entry.platform != DOMAIN:
                continue

            domain = getattr(entity_entry, "domain", None)
            if domain not in allowed_domains:
                continue

            current_device_id = getattr(entity_entry, "device_id", None)
            target_device: dr.DeviceEntry | Any | None = None

            if domain == "binary_sensor":
                target_device = _resolve_service_device()
                expected_subentry = service_subentry_id
            elif domain == "sensor":
                binding = _extract_tracker_binding_from_sensor(entity_entry)
                if binding is None:
                    target_device = _resolve_service_device()
                    expected_subentry = service_subentry_id
                else:
                    subentry_id, google_device_id = binding
                    subentry_id = subentry_id or tracker_subentry_id
                    if not google_device_id:
                        continue
                    target_device = _find_tracker_device(subentry_id, google_device_id)
                    expected_subentry = subentry_id
            else:  # device_tracker
                binding = _extract_tracker_binding_from_tracker(entity_entry)
                if binding is None:
                    continue
                subentry_id, google_device_id = binding
                subentry_id = subentry_id or tracker_subentry_id
                if not google_device_id:
                    continue
                target_device = _find_tracker_device(subentry_id, google_device_id)
                expected_subentry = subentry_id

            if target_device is None:
                continue

            target_device_id = getattr(target_device, "id", None)
            if not isinstance(target_device_id, str) or not target_device_id:
                continue

            if (
                isinstance(current_device_id, str)
                and current_device_id
                and current_device_id == target_device_id
            ):
                continue

            device_subentry = getattr(target_device, "config_subentry_id", None)
            if (
                isinstance(device_subentry, str)
                and device_subentry
                and expected_subentry
                and device_subentry != expected_subentry
            ):
                continue

            entity_registry.async_update_entity(
                entity_entry.entity_id, device_id=target_device_id
            )
            fixed += 1
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "googlefindmy(%s): relink failed for %s: %s",
                entry_id,
                getattr(entity_entry, "entity_id", "<unknown>"),
                err,
            )

    if fixed:
        _LOGGER.debug(
            "googlefindmy(%s): relinked %d tracker/service entit(y/ies)",
            entry_id,
            fixed,
        )


def _strip_entry_namespace(entry_id: str, ident: str) -> str:
    """Strip the entry namespace prefix (`<entry_id>:`) when present."""

    if not ident or ":" not in ident:
        return ident

    prefix, canonical = ident.split(":", 1)
    if canonical and prefix == entry_id:
        return canonical

    return ident


def _coerce_alias_iterable(value: Any) -> list[str] | None:
    """Return a sanitized list of alias strings or ``None`` when unavailable."""

    if isinstance(value, str):
        candidate = value.strip()
        return [candidate] if candidate else None

    if isinstance(value, Iterable) and not isinstance(
        value, (str, bytes, bytearray, Mapping)
    ):
        sanitized = [
            alias.strip()
            for alias in value
            if isinstance(alias, str) and alias.strip()
        ]
        if sanitized:
            return sanitized

    return None


def _dedupe_aliases(
    exclude: str | None,
    *sources: Iterable[str] | None,
) -> list[str]:
    """Return a deduplicated alias list excluding the active name."""

    deduped: list[str] = []
    for source in sources:
        if not source:
            continue
        for alias in source:
            if not isinstance(alias, str):
                continue
            candidate = alias.strip()
            if not candidate:
                continue
            if exclude and candidate == exclude:
                continue
            if candidate not in deduped:
                deduped.append(candidate)
    return deduped


def _safe_epoch(value: Any) -> int:
    """Best-effort conversion to an integer epoch timestamp."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sanitize_ignored_meta(device_id: str, meta: Mapping[str, Any]) -> dict[str, Any]:
    """Return sanitized metadata for ignored device bookkeeping."""

    raw_name = meta.get("name") if isinstance(meta, Mapping) else None
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else device_id

    alias_sources: list[Iterable[str]] = []
    if isinstance(meta, Mapping):
        coerced_aliases = _coerce_alias_iterable(meta.get("aliases"))
        if coerced_aliases:
            alias_sources.append(coerced_aliases)

    if isinstance(raw_name, str):
        raw_name_aliases = _coerce_alias_iterable(raw_name)
        if raw_name_aliases:
            alias_sources.append(raw_name_aliases)

    aliases = _dedupe_aliases(name, *alias_sources)

    ignored_at = _safe_epoch(meta.get("ignored_at")) if isinstance(meta, Mapping) else 0
    if not ignored_at:
        ignored_at = int(time.time())

    source = meta.get("source") if isinstance(meta, Mapping) else None
    if not isinstance(source, str) or not source:
        source = "registry"

    return {
        "name": name,
        "aliases": aliases,
        "ignored_at": ignored_at,
        "source": source,
    }


def _merge_sanitized_ignored_meta(
    existing: Mapping[str, Any], incoming: Mapping[str, Any]
) -> dict[str, Any]:
    """Merge two sanitized ignored metadata records."""

    existing_name = (
        existing.get("name") if isinstance(existing.get("name"), str) else None
    )
    incoming_name = (
        incoming.get("name") if isinstance(incoming.get("name"), str) else None
    )
    name = existing_name or incoming_name or ""

    alias_sources: list[Iterable[str]] = []

    existing_aliases = _coerce_alias_iterable(existing.get("aliases"))
    if existing_aliases:
        alias_sources.append(existing_aliases)

    incoming_aliases = _coerce_alias_iterable(incoming.get("aliases"))
    if incoming_aliases:
        alias_sources.append(incoming_aliases)

    name_aliases = [alias for alias in (existing_name, incoming_name) if alias]
    if name_aliases:
        alias_sources.append(name_aliases)

    aliases = _dedupe_aliases(name, *alias_sources)

    ignored_at = max(
        _safe_epoch(existing.get("ignored_at")), _safe_epoch(incoming.get("ignored_at"))
    )
    source = existing.get("source") or incoming.get("source") or "registry"

    if not name:
        name = incoming_name or existing_name or ""

    return {
        "name": name,
        "aliases": aliases,
        "ignored_at": ignored_at,
        "source": source,
    }


def _normalize_ignored_device_map(
    entry_id: str, mapping: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, dict[str, Any]], bool]:
    """Normalize ignored device identifiers for an entry."""

    normalized: dict[str, dict[str, Any]] = {}
    changed = False

    for raw_id, meta in mapping.items():
        canonical = _strip_entry_namespace(entry_id, raw_id)
        if canonical != raw_id:
            changed = True

        sanitized = _sanitize_ignored_meta(canonical, meta)
        existing = normalized.get(canonical)
        if existing is None:
            normalized[canonical] = sanitized
        else:
            normalized[canonical] = _merge_sanitized_ignored_meta(existing, sanitized)
            if normalized[canonical] != existing:
                changed = True

    if normalized != dict(mapping):
        changed = True

    return normalized, changed


def _normalize_visible_device_ids(
    entry_id: str, raw_ids: Iterable[Any]
) -> tuple[list[str], bool]:
    """Normalize visible device identifiers for a subentry."""

    normalized: list[str] = []
    seen: set[str] = set()
    changed = False

    for candidate in raw_ids:
        if not isinstance(candidate, str) or not candidate:
            changed = True
            continue
        canonical = _strip_entry_namespace(entry_id, candidate)
        if canonical != candidate:
            changed = True
        if canonical in seen:
            if canonical != candidate:
                changed = True
            continue
        seen.add(canonical)
        normalized.append(canonical)

    return normalized, changed


def _migrate_entry_identifier_namespaces(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Strip entry namespaces from ignored/visible device identifiers."""

    options = dict(entry.options)
    current_raw = options.get(
        OPT_IGNORED_DEVICES, DEFAULT_OPTIONS.get(OPT_IGNORED_DEVICES)
    )
    ignored_map, _ = coerce_ignored_mapping(current_raw)
    normalized_map, options_changed = _normalize_ignored_device_map(
        entry.entry_id, ignored_map
    )
    if options_changed:
        options[OPT_IGNORED_DEVICES] = normalized_map
        options[OPT_OPTIONS_SCHEMA_VERSION] = 2
    if options_changed and options != entry.options:
        hass.config_entries.async_update_entry(entry, options=options)

    subentries = getattr(entry, "subentries", None)
    if not isinstance(subentries, Mapping):
        return

    for subentry in subentries.values():
        raw_visible = subentry.data.get("visible_device_ids")
        if not isinstance(raw_visible, (list, tuple, set)):
            continue
        normalized_visible, subentry_changed = _normalize_visible_device_ids(
            entry.entry_id, raw_visible
        )
        if not subentry_changed:
            continue
        payload = dict(subentry.data)
        payload["visible_device_ids"] = list(normalized_visible)
        hass.config_entries.async_update_subentry(
            entry,
            subentry,
            data=payload,
        )


# --- BEGIN: Helpers for resolution and manual locate ---------------------------
def _resolve_canonical_from_any(hass: HomeAssistant, arg: str) -> tuple[str, str]:
    """Resolve HA device_id/entity_id/canonical_id -> (canonical_id, friendly_name).

    Resolution order:
    1) If `arg` is a Home Assistant `device_id`: extract our (DOMAIN, identifier)
       from the device registry. Fails if not found/invalid.
    2) If `arg` is an `entity_id`: lookup entity; if it belongs to our DOMAIN
       and is linked to a device, extract the identifier from that device.
    3) Otherwise: treat `arg` as already-canonical Google ID.

    Raises:
        HomeAssistantError: if `arg` is a `device_id`/`entity_id` but cannot be mapped
            to a valid identifier of this integration.

    Security:
        Do not include secrets or coordinates in raised messages or logs.
    """
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    # 1) device_id
    dev = dev_reg.async_get(arg)
    if dev:
        for item in dev.identifiers:
            try:
                domain, ident = item  # expected 2-tuple
            except (TypeError, ValueError):
                continue
            if domain == DOMAIN and isinstance(ident, str) and ident:
                canonical = _normalize_device_identifier(dev, ident)
                friendly = (dev.name_by_user or dev.name or canonical).strip()
                return canonical, friendly
        raise HomeAssistantError(f"Device '{arg}' has no valid {DOMAIN} identifier")

    # 2) entity_id
    if "." in arg and "/" not in arg and ":" not in arg:
        ent = ent_reg.async_get(arg)
        if ent and ent.platform == DOMAIN and ent.device_id:
            dev = dev_reg.async_get(ent.device_id)
            if dev:
                for item in dev.identifiers:
                    try:
                        domain, ident = item
                    except (TypeError, ValueError):
                        continue
                    if domain == DOMAIN and isinstance(ident, str) and ident:
                        canonical = _normalize_device_identifier(dev, ident)
                        friendly = (dev.name_by_user or dev.name or canonical).strip()
                        return canonical, friendly
            raise HomeAssistantError(
                f"Entity '{arg}' is not linked to a valid {DOMAIN} device"
            )

    # 3) fallback: assume canonical id already
    return arg, arg


async def async_handle_manual_locate(
    hass: HomeAssistant, coordinator: GoogleFindMyCoordinator, arg: str
) -> None:
    """Handle manual locate button: resolve target, dispatch, and log correctly.

    Behavior:
        - Resolve any incoming identifier (`device_id`, `entity_id`, or canonical).
        - On success: dispatch the request to the coordinator and log an info line.
        - On failure: raise HomeAssistantError and mirror a redacted error record
          into the coordinator diagnostics buffer (if present).

    This function should be called by your button entity handler.
    """
    try:
        canonical_id, friendly = _resolve_canonical_from_any(hass, arg)
        await coordinator.async_locate_device(canonical_id)
        _LOGGER.info("Successfully submitted manual locate for %s", friendly)
    except HomeAssistantError as err:
        diag_buffer = cast(Any, getattr(coordinator, "_diag", None))
        if diag_buffer is not None and hasattr(diag_buffer, "add_error"):
            diag_buffer.add_error(
                code="manual_locate_resolution_failed",
                context={
                    "device_id": "",
                    "arg": str(arg)[:64],
                    "reason": str(err)[:160],
                },
            )
        _LOGGER.error("Locate failed for '%s': %s", arg, err)
        raise


# --- END: Helpers for resolution and manual locate -----------------------------


def _redact_url_token(url: str) -> str:
    """Return URL with any sensitive query parameter values redacted for logging."""

    try:
        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        sensitive = {
            "token",
            "access_token",
            "id_token",
            "auth",
            "key",
            "apikey",
            "api_key",
            "signature",
        }
        redacted: list[tuple[str, str]] = []
        for k, v in q:
            if k.lower() in sensitive and v:
                red_v = "****"
                if len(v) > 4:
                    red_v = f"{v[:2]}…{v[-2:]}"
                redacted.append((k, red_v))
            else:
                redacted.append((k, v))
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(redacted, doseq=True),
                parts.fragment,
            )
        )
    except Exception:  # pragma: no cover
        return url


def _is_active_entry(entry: ConfigEntry) -> bool:
    """Return True if the entry is considered *active* for guard logic.

    We treat only well-defined 'working' states as active to avoid drift:
    - LOADED: entry is fully operational
    - SETUP_IN_PROGRESS / SETUP_RETRY: entry is being (re)initialized
    All other states are considered non-active.
    """
    if entry.disabled_by:
        return False
    active_states = {
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.SETUP_RETRY,
    }
    setup_error = getattr(ConfigEntryState, "SETUP_ERROR", None)
    if setup_error is not None:
        active_states.add(setup_error)
    return entry.state in active_states


def _primary_active_entry(entries: list[ConfigEntry]) -> ConfigEntry | None:
    """Pick a deterministic 'primary' active entry to avoid mutual aborts.

    Tie-break rule (stable, minimalistic):
        1) Prefer entries that are LOADED over all others.
        2) Otherwise, pick the lexicographically smallest entry_id.
    """
    active = [e for e in entries if _is_active_entry(e)]
    if not active:
        return None
    loaded = [e for e in active if e.state == ConfigEntryState.LOADED]
    pool = loaded or active
    return sorted(pool, key=lambda e: e.entry_id)[0]


# ------------------------------ Data/Options ---------------------------------


def _opt(entry: ConfigEntry, key: str, default: Any) -> Any:
    """Read a configuration value, preferring options over data."""
    if key in entry.options:
        return entry.options.get(key, default)
    return entry.data.get(key, default)


def _effective_config(entry: ConfigEntry) -> dict[str, Any]:
    """Assemble a dict of non-secret runtime settings (options-first)."""
    return {k: _opt(entry, k, None) for k in OPTION_KEYS}


def _normalize_contributor_mode(value: Any) -> str:
    """Return a sanitized contributor mode string."""

    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in (
            CONTRIBUTOR_MODE_HIGH_TRAFFIC,
            CONTRIBUTOR_MODE_IN_ALL_AREAS,
        ):
            return normalized
    return DEFAULT_CONTRIBUTOR_MODE


async def _async_soft_migrate_data_to_options(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Idempotently copy known settings from data -> options (never move secrets)."""
    new_options = dict(entry.options)
    changed = False
    for k in OPTION_KEYS:
        if k not in new_options and k in entry.data:
            new_options[k] = entry.data[k]
            changed = True
    if changed:
        _LOGGER.info(
            "Soft-migrating %d option(s) from data to options for '%s'",
            len(new_options) - len(entry.options),
            _label_entry_for_log(entry),
        )
        hass.config_entries.async_update_entry(entry, options=new_options)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry to the latest schema and enforce duplicate policy."""

    _LOGGER.debug(
        "Config entry %s (version=%s) requested migration; validating account metadata.",
        entry.entry_id,
        entry.version,
    )

    canonical_entry = await async_coalesce_account_entries(
        hass, canonical_entry=entry
    )
    if canonical_entry.entry_id != entry.entry_id:
        _LOGGER.info(
            "Config entry %s removed during migration; canonical entry is %s",
            entry.entry_id,
            canonical_entry.entry_id,
        )
        return True

    entry = canonical_entry

    should_setup, normalized_email = await _ensure_post_migration_consistency(
        hass,
        entry,
        duplicate_issue_cause="migration_duplicate",
    )

    if not should_setup:
        email_for_log = (
            _mask_email_for_logs(normalized_email) if normalized_email else "n/a"
        )
        _LOGGER.info(
            "Migration halted for %s because account %s is not authoritative",
            entry.entry_id,
            email_for_log,
        )
        return False

    await _async_soft_migrate_data_to_options(hass, entry)

    if entry.version != CONFIG_ENTRY_VERSION:
        update_kwargs = {"version": CONFIG_ENTRY_VERSION}
        try:
            hass.config_entries.async_update_entry(entry, **update_kwargs)
        except TypeError:
            _apply_update_entry_fallback(hass, entry, update_kwargs)

    _LOGGER.debug(
        "Config entry %s migrated to version %s",
        entry.entry_id,
        CONFIG_ENTRY_VERSION,
    )
    return True


# ------------------------- Entity/Device migrations --------------------------


async def _async_create_uid_collision_issue(
    hass: HomeAssistant, entry: ConfigEntry, entity_ids: list[str]
) -> None:
    """Create a repair issue for unique_id collisions (batched; idempotent by key)."""
    try:
        preview = ", ".join(entity_ids[:8]) + ("…" if len(entity_ids) > 8 else "")
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"unique_id_collision_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unique_id_collision",
            translation_placeholders={
                "entry": entry.title or entry.entry_id,
                "count": str(len(entity_ids)),
                "entities": preview or "n/a",
            },
        )
    except Exception as err:
        _LOGGER.debug("Failed to create UID collision issue: %s", err)


async def _async_migrate_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Migrate entity unique_ids to the latest entry/subentry-aware schemas."""

    ent_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    current_options = dict(entry.options)
    options_changed = False
    collisions: list[str] = []

    legacy_result: _LegacyUniqueIdMigrationResult | None = None
    if current_options.get("unique_id_migrated") is not True:
        legacy_result = _migrate_legacy_unique_ids(ent_reg, dev_reg, entry)
        if legacy_result.collisions:
            collisions.extend(legacy_result.collisions)
            _LOGGER.warning(
                "Unique-ID migration incomplete for '%s': migrated=%d / total_needed=%d, collisions=%d",
                _label_entry_for_log(entry),
                legacy_result.migrated,
                legacy_result.total_candidates,
                len(legacy_result.collisions),
            )
        else:
            current_options["unique_id_migrated"] = True
            options_changed = True
            if legacy_result.total_candidates or legacy_result.migrated:
                _LOGGER.debug(
                    "Unique-ID migration complete for '%s': migrated=%d, already_scoped=%d, nonprefix=%d",
                    _label_entry_for_log(entry),
                    legacy_result.migrated,
                    legacy_result.skipped_already_scoped,
                    legacy_result.skipped_nonprefix,
                )

    subentry_result: _SubentryUniqueIdMigrationResult | None = None
    if current_options.get("unique_id_subentry_migrated") is not True:
        subentry_result = _migrate_unique_ids_to_subentry(ent_reg, entry)
        if subentry_result.collisions:
            collisions.extend(subentry_result.collisions)
            _LOGGER.warning(
                "Subentry unique-ID migration incomplete for '%s': updated=%d, already_current=%d, skipped=%d, collisions=%d",
                _label_entry_for_log(entry),
                subentry_result.updated,
                subentry_result.already_current,
                subentry_result.skipped,
                len(subentry_result.collisions),
            )
        else:
            current_options["unique_id_subentry_migrated"] = True
            options_changed = True
            if subentry_result.updated or subentry_result.already_current:
                _LOGGER.debug(
                    "Subentry unique-ID migration complete for '%s': updated=%d, already_current=%d, skipped=%d",
                    _label_entry_for_log(entry),
                    subentry_result.updated,
                    subentry_result.already_current,
                    subentry_result.skipped,
                )

    if collisions:
        await _async_create_uid_collision_issue(hass, entry, collisions)

    if options_changed and current_options != dict(entry.options):
        hass.config_entries.async_update_entry(entry, options=current_options)


@dataclass(slots=True)
class _LegacyUniqueIdMigrationResult:
    total_candidates: int = 0
    migrated: int = 0
    skipped_already_scoped: int = 0
    skipped_nonprefix: int = 0
    collisions: list[str] = field(default_factory=list)


def _migrate_legacy_unique_ids(
    ent_reg: er.EntityRegistry, dev_reg: dr.DeviceRegistry, entry: ConfigEntry
) -> _LegacyUniqueIdMigrationResult:
    """Namespace legacy unique_ids by entry id and update service device identifiers."""

    prefix = f"{DOMAIN}_"
    namespaced_prefix = f"{DOMAIN}_{entry.entry_id}_"

    result = _LegacyUniqueIdMigrationResult()

    for ent in list(ent_reg.entities.values()):
        try:
            if ent.platform != DOMAIN or ent.config_entry_id != entry.entry_id:
                continue
            uid = ent.unique_id or ""
            if uid.startswith(namespaced_prefix):
                result.skipped_already_scoped += 1
                continue
            if not uid.startswith(prefix):
                result.skipped_nonprefix += 1
                continue

            result.total_candidates += 1
            new_uid = namespaced_prefix + uid[len(prefix) :]

            existing_eid = ent_reg.async_get_entity_id(
                ent.domain, ent.platform, new_uid
            )
            if existing_eid:
                _LOGGER.warning(
                    "Unique-ID migration skipped (collision): %s -> %s (existing=%s)",
                    uid,
                    new_uid,
                    existing_eid,
                )
                result.collisions.append(ent.entity_id)
                continue

            ent_reg.async_update_entity(ent.entity_id, new_unique_id=new_uid)
            result.migrated += 1
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug("Unique ID migration failed for %s: %s", ent.entity_id, err)

    try:
        for device in list(dev_reg.devices.values()):
            if entry.entry_id not in device.config_entries:
                continue
            if (DOMAIN, "integration") in device.identifiers:
                new_identifiers = set(device.identifiers)
                new_identifiers.remove((DOMAIN, "integration"))
                new_identifiers.add((DOMAIN, f"integration_{entry.entry_id}"))
                dev_reg.async_update_device(
                    device_id=device.id, new_identifiers=new_identifiers
                )
                _LOGGER.info(
                    "Migrated integration service device identifier for entry '%s'",
                    entry.entry_id,
                )
    except Exception as err:  # pragma: no cover - defensive
        _LOGGER.debug("Service device identifier migration skipped: %s", err)

    return result


_DEFAULT_SUBENTRY_IDENTIFIER = "core_tracking"
_DEFAULT_SUBENTRY_FEATURES: tuple[str, ...] = (
    "binary_sensor",
    "button",
    "device_tracker",
    "sensor",
)


def _looks_like_tracker_sensor_suffix(value: str) -> bool:
    """Return True when a sensor unique_id suffix targets a tracker entity."""

    if not value:
        return False

    lowered = value.lower()
    if lowered.endswith("_last_seen"):
        return True
    if "device-" in lowered or "device_" in lowered:
        return True
    return False


@dataclass(slots=True)
class _SubentryUniqueIdMigrationResult:
    updated: int = 0
    already_current: int = 0
    skipped: int = 0
    collisions: list[str] = field(default_factory=list)


def _migrate_unique_ids_to_subentry(
    ent_reg: er.EntityRegistry, entry: ConfigEntry
) -> _SubentryUniqueIdMigrationResult:
    """Update unique_ids to include the stable subentry identifier."""

    entry_id = getattr(entry, "entry_id", "") or ""
    if not entry_id:
        return _SubentryUniqueIdMigrationResult()

    subentry_map = _resolve_subentry_identifier_map(entry)
    result = _SubentryUniqueIdMigrationResult()

    for ent in list(ent_reg.entities.values()):
        try:
            if ent.platform != DOMAIN or ent.config_entry_id != entry.entry_id:
                continue
            decision = _determine_subentry_unique_id(entry_id, subentry_map, ent)
            if decision is None:
                result.skipped += 1
                continue
            if decision == ent.unique_id:
                result.already_current += 1
                continue

            existing_eid = ent_reg.async_get_entity_id(
                ent.domain, ent.platform, decision
            )
            if existing_eid and existing_eid != ent.entity_id:
                result.collisions.append(ent.entity_id)
                continue

            ent_reg.async_update_entity(ent.entity_id, new_unique_id=decision)
            result.updated += 1
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "Subentry unique ID migration failed for %s: %s", ent.entity_id, err
            )

    return result


def _resolve_subentry_identifier_map(entry: ConfigEntry) -> dict[str, str]:
    """Return the stable identifier for each feature for the given entry."""

    mapping: dict[str, str] = {}
    default_identifier: str | None = None
    service_identifier: str | None = None
    tracker_identifier: str | None = None

    subentries = getattr(entry, "subentries", None)
    if isinstance(subentries, Mapping):
        for subentry in subentries.values():
            data = getattr(subentry, "data", {}) or {}
            raw_features = data.get("features")
            features: tuple[str, ...]
            if isinstance(raw_features, (list, tuple, set)):
                normalized = [
                    str(item).strip()
                    for item in raw_features
                    if isinstance(item, str) and item.strip()
                ]
                features = (
                    tuple(normalized) if normalized else _DEFAULT_SUBENTRY_FEATURES
                )
            else:
                features = _DEFAULT_SUBENTRY_FEATURES

            identifier = getattr(subentry, "subentry_id", None)
            if not isinstance(identifier, str) or not identifier:
                group_key = data.get("group_key")
                identifier = (
                    str(group_key).strip() if isinstance(group_key, str) else ""
                )
            if not identifier:
                identifier = _DEFAULT_SUBENTRY_IDENTIFIER

            group_key_raw = data.get("group_key")
            group_key = str(group_key_raw).strip() if isinstance(group_key_raw, str) else ""
            if group_key == SERVICE_SUBENTRY_KEY and not service_identifier:
                service_identifier = identifier
            elif group_key == TRACKER_SUBENTRY_KEY and not tracker_identifier:
                tracker_identifier = identifier

            for feature in features:
                mapping.setdefault(feature, identifier)

            if default_identifier is None:
                default_identifier = identifier

    if default_identifier is None:
        default_identifier = _DEFAULT_SUBENTRY_IDENTIFIER

    if isinstance(service_identifier, str) and service_identifier:
        for feature in SERVICE_FEATURE_PLATFORMS:
            mapping[feature] = service_identifier

    if isinstance(tracker_identifier, str) and tracker_identifier:
        for feature in TRACKER_FEATURE_PLATFORMS:
            mapping.setdefault(feature, tracker_identifier)

    for feature in _DEFAULT_SUBENTRY_FEATURES:
        mapping.setdefault(feature, default_identifier)

    return mapping


def _determine_subentry_unique_id(
    entry_id: str, subentry_map: Mapping[str, str], ent: er.RegistryEntry
) -> str | None:
    """Return the desired unique_id for an entity (or None to skip)."""

    uid = ent.unique_id or ""
    if not uid:
        return None

    feature = ent.domain
    identifier = subentry_map.get(feature, _DEFAULT_SUBENTRY_IDENTIFIER)

    if feature == "device_tracker":
        if uid.count(":") >= 2 and uid.startswith(f"{entry_id}:{identifier}:"):
            return uid
        if uid.startswith(f"{entry_id}:{identifier}:"):
            return uid
        if uid.startswith(f"{entry_id}:"):
            remainder = uid[len(entry_id) + 1 :]
            if remainder:
                return f"{entry_id}:{identifier}:{remainder}"
            return None
        scoped_prefix = f"{DOMAIN}_{entry_id}_"
        if uid.startswith(scoped_prefix):
            remainder = uid[len(scoped_prefix) :]
            if remainder.startswith(f"{identifier}_"):
                return uid
            return f"{entry_id}:{identifier}:{remainder}"
        legacy_prefix = f"{DOMAIN}_"
        if uid.startswith(legacy_prefix):
            remainder = uid[len(legacy_prefix) :]
            return f"{entry_id}:{identifier}:{remainder}"
        return None

    if feature == "binary_sensor":
        binary_sensor_suffixes: tuple[str, ...] = ("polling", "auth_status")
        if uid.count(":") >= 2:
            parts = uid.split(":")
            if len(parts) >= 3 and parts[0] == entry_id and parts[1] == identifier:
                if parts[2] in binary_sensor_suffixes:
                    return uid
            if (
                len(parts) == 2
                and parts[0] == entry_id
                and parts[1] in binary_sensor_suffixes
            ):
                return f"{entry_id}:{identifier}:{parts[1]}"
            return None
        if uid.startswith(f"{entry_id}:"):
            suffix = uid[len(entry_id) + 1 :]
            if suffix in binary_sensor_suffixes:
                return f"{entry_id}:{identifier}:{suffix}"
            return None
        scoped_prefix = f"{DOMAIN}_{entry_id}_"
        if uid.startswith(scoped_prefix):
            suffix = uid[len(scoped_prefix) :]
            if suffix in binary_sensor_suffixes:
                return f"{entry_id}:{identifier}:{suffix}"
        legacy_prefix = f"{DOMAIN}_"
        if uid.startswith(legacy_prefix):
            suffix = uid[len(legacy_prefix) :]
            if suffix in binary_sensor_suffixes:
                return f"{entry_id}:{identifier}:{suffix}"
        return None

    if feature == "sensor":
        tracker_identifier = subentry_map.get("device_tracker")
        if not isinstance(tracker_identifier, str) or not tracker_identifier:
            tracker_identifier = None
        service_identifier = subentry_map.get("binary_sensor")
        if not isinstance(service_identifier, str) or not service_identifier:
            service_identifier = None

        def _select_sensor_identifier(
            remainder: str,
        ) -> tuple[str, str]:
            """Return the identifier and normalized remainder for sensor entities."""

            tracker_prefix = (
                f"{tracker_identifier}_" if tracker_identifier is not None else None
            )
            service_prefix = (
                f"{service_identifier}_" if service_identifier is not None else None
            )

            if service_prefix and remainder.startswith(service_prefix):
                assert service_identifier is not None
                return service_identifier, remainder

            if tracker_prefix and remainder.startswith(tracker_prefix):
                tail = remainder[len(tracker_prefix) :]
                if service_prefix and tail.startswith(service_prefix):
                    assert service_identifier is not None
                    return service_identifier, tail
                chosen = tracker_identifier if tracker_identifier is not None else identifier
                return chosen, remainder

            if service_identifier and not _looks_like_tracker_sensor_suffix(remainder):
                return service_identifier, remainder

            if tracker_identifier:
                return tracker_identifier, remainder

            return identifier, remainder

        scoped_prefix = f"{DOMAIN}_{entry_id}_"
        if uid.startswith(scoped_prefix):
            remainder = uid[len(scoped_prefix) :]
            target_identifier, normalized_remainder = _select_sensor_identifier(
                remainder
            )
            if normalized_remainder.startswith(f"{target_identifier}_"):
                return f"{DOMAIN}_{entry_id}_{normalized_remainder}"
            return f"{DOMAIN}_{entry_id}_{target_identifier}_{normalized_remainder}"
        legacy_prefix = f"{DOMAIN}_"
        if uid.startswith(legacy_prefix):
            remainder = uid[len(legacy_prefix) :]
            if remainder.startswith(f"{entry_id}_"):
                remainder = remainder[len(entry_id) + 1 :]
            target_identifier, normalized_remainder = _select_sensor_identifier(
                remainder
            )
            if normalized_remainder.startswith(f"{target_identifier}_"):
                return f"{DOMAIN}_{entry_id}_{normalized_remainder}"
            return f"{DOMAIN}_{entry_id}_{target_identifier}_{normalized_remainder}"
        return None

    if feature == "button":
        button_suffixes: tuple[str, ...] = (
            "_play_sound",
            "_stop_sound",
            "_locate_device",
        )
        scoped_prefix = f"{DOMAIN}_{entry_id}_"
        if uid.startswith(scoped_prefix):
            remainder = uid[len(scoped_prefix) :]
            if remainder.startswith(f"{identifier}_"):
                return uid
            if any(remainder.endswith(suffix) for suffix in button_suffixes):
                normalized_remainder = _normalize_legacy_button_remainder(
                    remainder,
                    identifier=identifier,
                    suffixes=button_suffixes,
                )
                return f"{DOMAIN}_{entry_id}_{identifier}_{normalized_remainder}"
            return None
        legacy_prefix = f"{DOMAIN}_"
        if uid.startswith(legacy_prefix):
            remainder = uid[len(legacy_prefix) :]
            if remainder.startswith(f"{entry_id}_"):
                remainder = remainder[len(entry_id) + 1 :]
            if remainder.startswith(f"{identifier}_"):
                return uid
            if any(remainder.endswith(suffix) for suffix in button_suffixes):
                normalized_remainder = _normalize_legacy_button_remainder(
                    remainder,
                    identifier=identifier,
                    suffixes=button_suffixes,
                )
                return f"{DOMAIN}_{entry_id}_{identifier}_{normalized_remainder}"
        return None

    return None


async def _async_migrate_device_identifiers_to_entry_scope(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Prepare migration of device identifiers to entry scope (NOT invoked yet).

    Goal:
        Replace legacy device identifiers (DOMAIN, <canonical_id>) with entry-scoped
        identifiers (DOMAIN, f"{entry.entry_id}:{canonical_id}") to avoid collisions
        across accounts.

    Safety:
        - Skips "service"/integration devices.
        - Idempotent: already namespaced identifiers are ignored.
        - Collision-aware: if a target identifier exists, it will skip and log a warning.
    """
    dev_reg = dr.async_get(hass)
    updated = 0
    skipped = 0
    collisions = 0

    for device in list(dev_reg.devices.values()):
        if entry.entry_id not in device.config_entries:
            continue

        # Keep service/integration device untouched
        if (DOMAIN, "integration") in device.identifiers or any(
            domain == DOMAIN and str(ident).startswith("integration_")
            for domain, ident in device.identifiers
        ):
            continue

        our_ids = [(d, i) for (d, i) in device.identifiers if d == DOMAIN]
        if not our_ids:
            continue

        # Build new set of identifiers for this device
        new_identifiers = set(device.identifiers)
        dirty = False

        for _domain, ident in our_ids:
            ident_str = str(ident)
            if ":" in ident_str and ident_str.startswith(entry.entry_id + ":"):
                skipped += 1
                continue  # already namespaced for this entry
            target = (DOMAIN, f"{entry.entry_id}:{ident_str}")

            # Check for collision: if any other device already uses the target ident, skip
            conflict = False
            for dev2 in dev_reg.devices.values():
                if dev2.id == device.id:
                    continue
                if target in dev2.identifiers:
                    conflict = True
                    break
            if conflict:
                collisions += 1
                _LOGGER.warning(
                    "Identifier migration skipped (collision): %s -> %s on device %s",
                    (DOMAIN, ident_str),
                    target,
                    device.id,
                )
                continue

            # Perform substitution
            new_identifiers.discard((DOMAIN, ident_str))
            new_identifiers.add(target)
            dirty = True

        if dirty and new_identifiers != device.identifiers:
            dev_reg.async_update_device(
                device_id=device.id, new_identifiers=new_identifiers
            )
            updated += 1

    _LOGGER.info(
        "Prepared entry-scoped identifier migration (dry): updated=%d, skipped=%d, collisions=%d",
        updated,
        skipped,
        collisions,
    )


# --------------------------- Shared FCM provider ---------------------------


async def _async_acquire_shared_fcm(hass: HomeAssistant) -> FcmReceiverHAType:
    """Get or create the shared FCM receiver for this HA instance.

    Behavior:
        - Creates and initializes the singleton if missing.
        - Registers provider callbacks for API and LocateTracker once.
        - Maintains a reference counter to support multiple entries.
        - NEW: attaches HA context to enable owner-index fallback routing.
    """
    _ensure_runtime_imports()
    bucket = _domain_data(hass)
    fcm_lock: asyncio.Lock = _ensure_fcm_lock(bucket)
    if not isinstance(bucket.get("providers_registered"), bool):
        bucket["providers_registered"] = False
    if fcm_lock.locked():
        contention = bucket.get("fcm_lock_contention_count")
        if not isinstance(contention, int):
            contention = 0
        bucket["fcm_lock_contention_count"] = contention + 1
    async with fcm_lock:
        refcount = _get_fcm_refcount(bucket)
        providers_registered = bucket.get("providers_registered", False)
        raw_receiver: object | None = bucket.get("fcm_receiver")
        receiver_cls = _resolve_fcm_receiver_class()
        fcm: FcmReceiverHAType | None
        if isinstance(raw_receiver, receiver_cls):
            fcm = raw_receiver
        else:
            fcm = None

        def _method_is_coroutine(receiver: object, name: str) -> bool:
            """Return True if receiver.name is an async callable."""

            attr = getattr(receiver, name, None)
            if attr is None:
                return False
            candidate = getattr(attr, "__func__", attr)
            try:
                candidate = inspect.unwrap(
                    candidate
                )  # unwrap functools.partial / wraps
            except Exception:  # pragma: no cover - defensive
                pass
            if inspect.iscoroutinefunction(candidate):
                return True
            cls_attr = getattr(type(receiver), name, None)
            if cls_attr is not None:
                cls_candidate = getattr(cls_attr, "__func__", cls_attr)
                return inspect.iscoroutinefunction(cls_candidate)
            return False

        if raw_receiver is not None and not isinstance(raw_receiver, receiver_cls):
            _LOGGER.warning(
                "Discarding cached FCM receiver with unexpected type: %s",
                type(raw_receiver).__name__,
            )
            stale = _pop_any_fcm_receiver(bucket)
            await _async_stop_receiver_if_possible(stale)
            fcm = None
        elif fcm is not None and (
            not _method_is_coroutine(fcm, "async_register_for_location_updates")
            or not _method_is_coroutine(fcm, "async_unregister_for_location_updates")
        ):
            _LOGGER.warning(
                "Discarding cached FCM receiver lacking async registration methods"
            )
            stale = _pop_any_fcm_receiver(bucket)
            await _async_stop_receiver_if_possible(stale)
            fcm = None

        if fcm is None:
            fcm = receiver_cls()
            _LOGGER.debug("Initializing shared FCM receiver...")
            ok = await fcm.async_initialize()
            if not ok:
                raise ConfigEntryNotReady("Failed to initialize FCM receiver")

            # --- NEW: Attach HA context for owner-index fallback routing ---
            try:
                attach = getattr(fcm, "attach_hass", None)
                if callable(attach):
                    attach(hass)
                    _LOGGER.debug(
                        "Attached HA context to FCM receiver (owner-index routing enabled)."
                    )
            except Exception as err:
                _LOGGER.debug("FCM attach_hass skipped: %s", err)

            _set_fcm_receiver(bucket, fcm)
            _LOGGER.info("Shared FCM receiver initialized")

            # Register provider for both consumer modules (exactly once on first acquire)
            # Re-registering ensures downstream modules resolve the refreshed instance.
            def provider() -> FcmReceiverHAType:
                """Return the shared FCM receiver for integration consumers."""

                return _domain_fcm_provider(hass)

            provider_fn: Callable[[], FcmReceiverHAType] = provider
            if not providers_registered:
                loc_register_fcm_provider(
                    cast(Callable[[], NovaFcmReceiverProtocol], provider_fn)
                )
                api_register_fcm_provider(
                    cast(Callable[[], ApiFcmReceiverProtocol], provider_fn)
                )
                bucket["providers_registered"] = True

        new_refcount = refcount + 1
        _set_fcm_refcount(bucket, new_refcount)
        _LOGGER.debug("FCM refcount -> %s", new_refcount)
        return fcm


async def _async_release_shared_fcm(hass: HomeAssistant) -> None:
    """Decrease refcount; stop and unregister provider when it reaches zero."""
    _ensure_runtime_imports()
    bucket = _domain_data(hass)
    fcm_lock: asyncio.Lock = _ensure_fcm_lock(bucket)
    async with fcm_lock:
        refcount = _get_fcm_refcount(bucket) - 1
        refcount = max(refcount, 0)
        _set_fcm_refcount(bucket, refcount)
        _LOGGER.debug("FCM refcount -> %s", refcount)

        if refcount != 0:
            return

        fcm = _pop_fcm_receiver(bucket)

        # Unregister providers first (consumers will see provider=None immediately)
        try:
            loc_unregister_fcm_provider()
        except Exception:
            pass
        try:
            api_unregister_fcm_provider()
        except Exception:
            pass

        bucket["providers_registered"] = False

        if fcm is not None:
            try:
                await fcm.async_stop()
                _LOGGER.info("Shared FCM receiver stopped")
            except Exception as err:
                _LOGGER.warning("Stopping FCM receiver failed: %s", err)


# ------------------------------ Setup / Unload -----------------------------


def _resolve_entry_email(entry: ConfigEntry) -> tuple[str | None, str | None]:
    """Return the raw and normalized e-mail associated with a config entry."""

    raw_email: str | None = None
    for container in (entry.data, entry.options):
        if not isinstance(container, Mapping):
            continue
        email_value = container.get(CONF_GOOGLE_EMAIL)
        if isinstance(email_value, str) and email_value.strip():
            raw_email = email_value.strip()
            break

    if raw_email is None:
        secrets_bundle = None
        for container in (entry.data, entry.options):
            if isinstance(container, Mapping):
                bundle_candidate = container.get(DATA_SECRET_BUNDLE)
                if isinstance(bundle_candidate, Mapping):
                    secrets_bundle = bundle_candidate
                    break
        if isinstance(secrets_bundle, Mapping):
            for key in ("google_email", "username", "Email", "email"):
                candidate = secrets_bundle.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    raw_email = candidate.strip()
                    break

    normalized_email = normalize_email(raw_email)
    return raw_email, normalized_email


def _extract_email_from_entry(entry: ConfigEntry) -> str | None:
    """Return the normalized email for ``entry`` if available."""

    _, normalized = _resolve_entry_email(entry)
    return normalized


def _apply_update_entry_fallback(
    hass: HomeAssistant, entry: ConfigEntry, update_kwargs: Mapping[str, Any]
) -> None:
    """Apply entry update fields when stubs reject extended keywords."""

    data = update_kwargs.get("data")
    if isinstance(data, Mapping):
        entry.data = dict(data)

    title = update_kwargs.get("title")
    if isinstance(title, str):
        entry.title = title

    unique_id = update_kwargs.get("unique_id")
    if isinstance(unique_id, str):
        setattr(entry, "unique_id", unique_id)

    options = update_kwargs.get("options")
    if isinstance(options, Mapping):
        hass.config_entries.async_update_entry(entry, options=dict(options))

    version_value = update_kwargs.get("version")
    if isinstance(version_value, int):
        entry.version = version_value


def _format_duplicate_entries(
    entry: ConfigEntry, conflicts: Sequence[ConfigEntry] | None
) -> str:
    """Return a bullet list describing duplicate config entries."""

    ordered: list[ConfigEntry] = [entry, *(conflicts or ())]
    seen: set[str] = set()
    lines: list[str] = []
    for candidate in ordered:
        entry_id = getattr(candidate, "entry_id", "") or ""
        if entry_id in seen:
            continue
        seen.add(entry_id)
        label = candidate.title or entry_id or ""
        if entry_id:
            lines.append(f"- {label} ({entry_id})")
        else:
            lines.append(f"- {label}")
    return "\n".join(lines)


def _mask_email_for_logs(email: str | None) -> str:
    """Return a privacy-friendly representation of an email for logs."""

    if not email or "@" not in email:
        return "<unknown>"

    local, domain = email.split("@", 1)
    if not local:
        return f"*@{domain}"

    masked_local = (local[0] + "***") if len(local) > 1 else "*"
    return f"{masked_local}@{domain}"


def _label_entry_for_log(entry: ConfigEntry) -> str:
    """Return a privacy-safe label for log messages referencing ``entry``."""

    email = _extract_email_from_entry(entry)
    if email:
        return _mask_email_for_logs(email)
    title = getattr(entry, "title", None)
    if isinstance(title, str) and title:
        return title
    entry_id = getattr(entry, "entry_id", None)
    if isinstance(entry_id, str) and entry_id:
        return entry_id
    return "<unknown>"


def _issue_exists(hass: HomeAssistant, issue_id: str) -> bool:
    """Return True if a repair issue with the given ID exists for this domain.

    NOTE: issue_registry.async_get(...) is a synchronous callback helper in Home
    Assistant. Do not await it. The returned registry exposes synchronous
    "async_*" methods that operate in the event loop thread.
    """

    try:
        registry = ir.async_get(hass)
    except Exception:  # pragma: no cover - defensive fallback
        return False

    get_issue = getattr(registry, "async_get_issue", None)
    if not callable(get_issue):
        return False

    try:
        return get_issue(DOMAIN, issue_id) is not None
    except Exception:  # pragma: no cover - defensive fallback
        return False


def _log_duplicate_and_raise_repair_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    normalized_email: str,
    *,
    cause: str,
    conflicts: Sequence[ConfigEntry] | None = None,
) -> None:
    """Create or refresh a Repair issue for duplicate account configuration."""

    issue_id = f"duplicate_account_{entry.entry_id}"
    issue_present = _issue_exists(hass, issue_id)
    log_fn = _LOGGER.debug if issue_present else _LOGGER.warning
    log_fn(
        "googlefindmy %s: duplicate account %s detected (%s)",
        entry.entry_id,
        _mask_email_for_logs(normalized_email),
        cause,
    )
    placeholders: dict[str, Any] = {
        "email": normalized_email,
        "entries": _format_duplicate_entries(entry, conflicts),
    }
    if cause:
        placeholders["cause"] = cause

    issue_severity = getattr(ir, "IssueSeverity", None)
    if issue_severity is not None:
        severity_value = getattr(issue_severity, "WARNING", None)
        if severity_value is None:
            severity_value = getattr(issue_severity, "ERROR", "warning")
    else:
        severity_value = getattr(ir, "WARNING", "warning")

    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity_value,
            translation_key="duplicate_account_entries",
            translation_placeholders=placeholders,
        )
    except Exception as err:  # pragma: no cover - defensive log only
        _LOGGER.debug("Failed to create duplicate-account repair issue: %s", err)


def _clear_duplicate_account_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the duplicate-account Repair issue when resolved."""

    try:
        ir.async_delete_issue(hass, DOMAIN, f"duplicate_account_{entry.entry_id}")
    except Exception:
        return


def _integration_disabled_by_value() -> object:
    """Return the ConfigEntry disabled marker for integration-managed entries."""

    if _ConfigEntryDisabler is None:
        return "integration"

    return getattr(_ConfigEntryDisabler, "INTEGRATION", "integration")


def _is_user_disabled(entry: ConfigEntry) -> bool:
    """Return True if the entry has been disabled explicitly by the user."""

    disabled = getattr(entry, "disabled_by", None)
    if disabled is None:
        return False

    disabled_text = str(disabled).lower()
    return "user" in disabled_text


def _is_integration_disabled(entry: ConfigEntry) -> bool:
    """Return True if the entry has already been disabled by the integration."""

    disabled = getattr(entry, "disabled_by", None)
    if disabled is None:
        return False

    disabled_text = str(disabled).lower()
    return "integration" in disabled_text


def _schedule_duplicate_unload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Schedule an asynchronous unload for a duplicate config entry."""

    unload_candidates = (
        ConfigEntryState.LOADED,
        getattr(ConfigEntryState, "SETUP_RETRY", None),
        getattr(ConfigEntryState, "SETUP_ERROR", None),
    )

    if entry.state not in unload_candidates:
        return

    _LOGGER.debug(
        "Scheduling unload for duplicate entry %s (state=%s)",
        entry.entry_id,
        entry.state,
    )

    hass.async_create_task(
        hass.config_entries.async_unload(entry.entry_id),
        name=f"{DOMAIN}.unload_duplicate.{entry.entry_id}",
    )


def _clear_stale_duplicate_account_issues(
    hass: HomeAssistant,
    *,
    normalized_email: str | None,
    active_entry_ids: Collection[str],
) -> None:
    """Remove lingering duplicate-account issues for a cleared email."""

    if not normalized_email:
        return

    registry = ir.async_get(hass)
    issues_attr = getattr(registry, "issues", None)
    if isinstance(issues_attr, Mapping):
        issues_iterable = list(issues_attr.items())
    else:
        private_issues = getattr(registry, "_issues", None)
        if not isinstance(private_issues, Mapping):
            return
        issues_iterable = list(private_issues.items())

    for key, payload in issues_iterable:
        issue_id: str
        domain = DOMAIN
        if isinstance(key, tuple) and len(key) == 2:
            domain, issue_id = str(key[0]), str(key[1])
        else:
            issue_id = str(key)
            domain = str(payload.get("domain", domain))

        if not issue_id.startswith("duplicate_account_"):
            continue
        if domain != DOMAIN:
            continue
        placeholders = payload.get("translation_placeholders") or {}
        if placeholders.get("email") != normalized_email:
            continue
        suffix = issue_id.removeprefix("duplicate_account_")
        if suffix in active_entry_ids:
            continue
        try:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        except Exception:  # pragma: no cover - defensive cleanup
            continue


def _select_authoritative_entry_id(
    entry: ConfigEntry, duplicates: Sequence[ConfigEntry]
) -> str:
    """Return the entry_id that should remain active for a duplicate account.

    Preference order:
    1. Config entry state (loaded > retry > error > pending > not loaded).
    2. Most recent timestamp, preferring ``updated_at`` over ``created_at``.
    3. Deterministic tiebreaker using ``entry_id`` for stability.
    """

    candidates = [entry, *duplicates]

    def _state_rank(state: ConfigEntryState | None) -> int:
        order: dict[object, int] = {ConfigEntryState.LOADED: 0}

        setup_retry = getattr(ConfigEntryState, "SETUP_RETRY", None)
        if setup_retry is not None:
            order[setup_retry] = 1
        else:  # pragma: no cover - compatibility with very old cores
            order["setup_retry"] = 1

        setup_error = getattr(ConfigEntryState, "SETUP_ERROR", None)
        if setup_error is not None:
            order[setup_error] = 2
        else:  # pragma: no cover
            order["setup_error"] = 2

        migration_error = getattr(ConfigEntryState, "MIGRATION_ERROR", None)
        if migration_error is not None:
            order[migration_error] = 3
        else:  # pragma: no cover
            order["migration_error"] = 3

        setup_in_progress = getattr(ConfigEntryState, "SETUP_IN_PROGRESS", None)
        if setup_in_progress is not None:
            order[setup_in_progress] = 4
        else:  # pragma: no cover
            order["setup_in_progress"] = 4

        order[ConfigEntryState.NOT_LOADED] = 5

        return order.get(state, 5)

    def _candidate_key(candidate: ConfigEntry) -> tuple[int, float, str]:
        state_rank = _state_rank(getattr(candidate, "state", None))

        timestamp = getattr(candidate, "updated_at", None) or getattr(
            candidate, "created_at", None
        )
        ts_rank = float("inf")
        if isinstance(timestamp, datetime):
            try:
                ts_rank = -float(timestamp.timestamp())
            except (OSError, ValueError):  # pragma: no cover - defensive fallback
                ts_rank = float("inf")

        entry_id = str(getattr(candidate, "entry_id", "") or "")
        return (state_rank, ts_rank, entry_id)

    authoritative = min(candidates, key=_candidate_key)
    return str(authoritative.entry_id)


async def _ensure_post_migration_consistency(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    duplicate_issue_cause: str = "setup_duplicate",
) -> tuple[bool, str | None]:
    """Repair stale metadata and detect duplicates before setup."""

    raw_email, normalized_email = _resolve_entry_email(entry)

    new_data = dict(entry.data)
    if raw_email and new_data.get(CONF_GOOGLE_EMAIL) != raw_email:
        new_data[CONF_GOOGLE_EMAIL] = raw_email

    update_kwargs: dict[str, Any] = {}
    if new_data != entry.data:
        update_kwargs["data"] = new_data

    if raw_email and entry.title != raw_email:
        update_kwargs["title"] = raw_email

    unique_id = unique_account_id(normalized_email)
    current_unique_id = getattr(entry, "unique_id", None)
    if unique_id and current_unique_id != unique_id:
        update_kwargs["unique_id"] = unique_id

    if update_kwargs:
        try:
            hass.config_entries.async_update_entry(entry, **update_kwargs)
        except TypeError:
            _apply_update_entry_fallback(hass, entry, update_kwargs)
        except ValueError:
            if normalized_email:
                _log_duplicate_and_raise_repair_issue(
                    hass,
                    entry,
                    normalized_email,
                    cause="unique_id_conflict_setup",
                )
            update_kwargs.pop("unique_id", None)
            if update_kwargs:
                try:
                    hass.config_entries.async_update_entry(entry, **update_kwargs)
                except TypeError:
                    _apply_update_entry_fallback(hass, entry, update_kwargs)

    duplicates: list[ConfigEntry] = []
    if normalized_email:
        duplicates = [
            candidate
            for candidate in hass.config_entries.async_entries(DOMAIN)
            if candidate.entry_id != entry.entry_id
            and _extract_email_from_entry(candidate) == normalized_email
        ]

    authoritative_entry_id = _select_authoritative_entry_id(entry, duplicates)

    active_issue_entry_ids: set[str] = set()

    if duplicates and normalized_email:
        disabled_by_integration: list[str] = []
        retained_user_disabled: list[str] = []
        manual_action_required: list[str] = []

        _clear_duplicate_account_issue(hass, entry)

        conflicts: list[ConfigEntry] = [entry, *duplicates]

        for candidate in duplicates:
            if candidate.entry_id == authoritative_entry_id:
                _clear_duplicate_account_issue(hass, candidate)
                continue

            if _is_user_disabled(candidate):
                retained_user_disabled.append(candidate.entry_id)
                _clear_duplicate_account_issue(hass, candidate)
                _schedule_duplicate_unload(hass, candidate)
                continue

            if _is_integration_disabled(candidate):
                disabled_by_integration.append(candidate.entry_id)
                _clear_duplicate_account_issue(hass, candidate)
                _schedule_duplicate_unload(hass, candidate)
                continue

            try:
                await hass.config_entries.async_set_disabled_by(
                    candidate.entry_id,
                    _integration_disabled_by_value(),
                )
            except (TypeError, AttributeError):
                manual_action_required.append(candidate.entry_id)
                _LOGGER.warning(
                    "Duplicate entry %s could not be disabled via API (legacy Core). "
                    "Left unloaded; issued repair for manual action.",
                    candidate.entry_id,
                )
                _schedule_duplicate_unload(hass, candidate)
                _log_duplicate_and_raise_repair_issue(
                    hass,
                    candidate,
                    normalized_email,
                    cause=duplicate_issue_cause,
                    conflicts=conflicts,
                )
                active_issue_entry_ids.add(candidate.entry_id)
                continue

            disabled_by_integration.append(candidate.entry_id)
            _clear_duplicate_account_issue(hass, candidate)
            _schedule_duplicate_unload(hass, candidate)

        if manual_action_required:
            _LOGGER.info(
                "Duplicate account %s → authoritative=%s; disabled=%s; user_disabled=%s; manual_action_required=%s",
                _mask_email_for_logs(normalized_email),
                authoritative_entry_id,
                disabled_by_integration,
                retained_user_disabled,
                manual_action_required,
            )
        elif disabled_by_integration or retained_user_disabled:
            _LOGGER.info(
                "Duplicate account %s → authoritative=%s; disabled=%s; user_disabled=%s",
                _mask_email_for_logs(normalized_email),
                authoritative_entry_id,
                disabled_by_integration,
                retained_user_disabled,
            )
    else:
        _clear_duplicate_account_issue(hass, entry)

    _clear_stale_duplicate_account_issues(
        hass,
        normalized_email=normalized_email,
        active_entry_ids=active_issue_entry_ids,
    )

    should_setup = not duplicates or entry.entry_id == authoritative_entry_id
    return should_setup, normalized_email


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration namespace and register global services.

    Rationale:
        Services must be registered from async_setup so they are always available,
        even if no config entry is loaded, which enables frontend validation of
        automations referencing these services.
    """

    _ensure_runtime_imports()
    bucket = _domain_data(hass)
    _ensure_entries_bucket(bucket)  # entry_id -> RuntimeData
    _ensure_device_owner_index(bucket)  # canonical_id -> entry_id (E2.5 scaffold)
    if not isinstance(bucket.get("providers_registered"), bool):
        bucket["providers_registered"] = False

    # Use a lock + idempotent flag to avoid double registration on racey startups.
    services_lock: asyncio.Lock = _ensure_services_lock(bucket)
    async with services_lock:
        services_registered = bucket.get("services_registered")
        if not isinstance(services_registered, bool):
            services_registered = False
        if not services_registered:
            svc_ctx = {
                "domain": DOMAIN,
                "resolve_canonical": _resolve_canonical_from_any,
                "is_active_entry": _is_active_entry,
                "primary_active_entry": _primary_active_entry,
                "opt": _opt,
                "default_map_view_token_expiration": DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
                "opt_map_view_token_expiration_key": OPT_MAP_VIEW_TOKEN_EXPIRATION,
                "redact_url_token": _redact_url_token,
                "soft_migrate_entry": _async_soft_migrate_data_to_options,
                "migrate_unique_ids": _async_migrate_unique_ids,
                "relink_button_devices": _async_relink_button_devices,
                "relink_subentry_entities": _async_relink_subentry_entities,
                "coalesce_account_entries": async_coalesce_account_entries,
                "extract_normalized_email": _extract_email_from_entry,
            }
            await async_register_services(hass, svc_ctx)
            bucket["services_registered"] = True
            _LOGGER.debug("Registered %s services at integration level", DOMAIN)

    return True


def _self_heal_device_registry(hass: HomeAssistant, entry: MyConfigEntry) -> None:
    """Remove stale parent links from tracker devices for the given entry."""

    _LOGGER.debug(
        "[Entry=%s] Starting self-healing cleanup of device registry...",
        entry.entry_id,
    )
    dev_reg = dr.async_get(hass)
    entry_id = entry.entry_id
    correct_service_identifier = service_device_identifier(entry_id)

    registry_devices: Iterable[Any]
    entries_helper: Callable[[Any, str], Iterable[Any]] | None = getattr(
        dr, "async_entries_for_config_entry", None
    )
    if entries_helper is None:
        fallback = getattr(dev_reg, "async_entries_for_config_entry", None)
        if callable(fallback):
            registry_devices = cast(Iterable[Any], fallback(entry_id))
        else:
            _LOGGER.debug(
                "Self-healing: device registry helper missing, skipping cleanup.",
            )
            registry_devices = ()
    else:
        registry_devices = entries_helper(dev_reg, entry_id)

    healed_devices = 0
    for device in registry_devices:
        config_entries: Collection[str] = getattr(device, "config_entries", ())
        if entry_id not in config_entries:
            continue

        identifiers: Collection[tuple[str, str]] = getattr(device, "identifiers", ())
        is_service_device = correct_service_identifier in identifiers
        via_device_id = getattr(device, "via_device_id", None)

        if is_service_device or via_device_id is None:
            continue

        device_name = getattr(device, "name", device.id)
        _LOGGER.debug(
            "Healing device '%s' (ID: %s): Removing incorrect parent link (via_device_id)",
            device_name,
            device.id,
        )
        dev_reg.async_update_device(device.id, via_device_id=None)
        healed_devices += 1

    if healed_devices > 0:
        _LOGGER.info(
            "Self-healing complete: Removed incorrect parent links from %d orphaned devices.",
            healed_devices,
        )
    else:
        _LOGGER.debug(
            "Self-healing: No orphaned devices with incorrect links found.",
        )


def _resolve_config_subentry_identifier(subentry: Any) -> str | None:
    """Return the unique identifier for ``subentry`` when available."""

    if isinstance(subentry, Mapping):
        candidate = subentry.get("config_subentry_id") or subentry.get("subentry_id")
        if isinstance(candidate, str) and candidate:
            return candidate

    for attribute in (
        "config_subentry_id",
        "subentry_id",
        "stable_identifier",
        "key",
        "entry_id",
    ):
        value = getattr(subentry, attribute, None)
        if isinstance(value, str) and value:
            return value

    return None


def _normalize_feature_iterable(candidate: Any) -> tuple[Any, ...]:
    """Return a tuple of feature markers extracted from ``candidate``."""

    if isinstance(candidate, Mapping):
        candidate = candidate.values()

    if isinstance(candidate, str) or candidate is None:
        return ()

    if isinstance(candidate, Iterable):
        values: list[Any] = []
        for item in candidate:
            if isinstance(item, (str, Platform)):
                values.append(item)
        return tuple(values)

    return ()


def _normalize_platforms_from_features(features: Iterable[Any]) -> tuple[Platform, ...]:
    """Convert an iterable of feature markers to Home Assistant platforms."""

    resolved: list[Platform] = []
    for feature in features:
        platform: Platform | None = None
        if isinstance(feature, Platform):
            platform = feature
        elif isinstance(feature, str):
            attr = getattr(Platform, feature.upper(), None)
            if attr is not None:
                platform = cast(Platform | str, attr)
            else:
                try:
                    platform = next(
                        candidate
                        for candidate in Platform
                        if getattr(candidate, "value", None) == feature
                    )
                except (StopIteration, TypeError):
                    platform = None
        if platform is not None:
            resolved.append(platform)

    if not resolved:
        return tuple(PLATFORMS)

    return tuple(dict.fromkeys(resolved))


def _determine_subentry_platforms(subentry: Any) -> tuple[Platform, ...]:
    """Return the platforms that should load for ``subentry``."""

    data = getattr(subentry, "data", None)
    features = _normalize_feature_iterable(getattr(subentry, "features", None))

    if not features and isinstance(data, Mapping):
        features = _normalize_feature_iterable(data.get("features"))

    if not features:
        subentry_type = getattr(subentry, "subentry_type", None)
        key = getattr(subentry, "key", None)
        if subentry_type == SUBENTRY_TYPE_SERVICE or key == SERVICE_SUBENTRY_KEY:
            features = tuple(SERVICE_FEATURE_PLATFORMS)
        elif subentry_type == SUBENTRY_TYPE_TRACKER or key == TRACKER_SUBENTRY_KEY:
            features = tuple(TRACKER_FEATURE_PLATFORMS)
        else:
            features = tuple(_feature_name_from_platform(platform) for platform in PLATFORMS)

    return _normalize_platforms_from_features(features)


def _platform_names(platforms: Iterable[Platform | str]) -> tuple[str, ...]:
    """Return normalized platform names for logging/debugging."""

    names: list[str] = []
    for platform in platforms:
        candidate = _platform_value(platform)
        if candidate:
            names.append(candidate)

    # Preserve ordering while deduplicating so repeated platform enums only
    # appear once in debug output.
    return tuple(dict.fromkeys(names))


def _platform_value(platform: Platform | str) -> str:
    """Return the string value for ``platform`` regardless of enum type."""

    if isinstance(platform, Platform):
        candidate = getattr(platform, "value", None)
        if isinstance(candidate, str) and candidate:
            return candidate
        return str(platform)
    if isinstance(platform, str):
        return platform
    return str(platform)


def _callable_accepts_keyword(callback: Callable[..., Any], keyword: str) -> bool:
    """Return ``True`` if ``callback`` accepts ``keyword`` as a kwarg."""

    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):  # pragma: no cover - CPython limitation for some callables
        return True

    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ) and parameter.name == keyword:
            return True

    return False


def _invoke_with_optional_keyword(
    callback: Callable[..., Any],
    args: tuple[Any, ...],
    keyword: str,
    value: Any,
) -> Any:
    """Invoke ``callback`` while gracefully handling a legacy signature.

    When ``callback`` does not accept ``keyword`` the value is ignored and the
    callable is executed without it. This mirrors the Home Assistant 2025.10+
    helpers that accept ``config_subentry_id`` while remaining compatible with
    earlier releases.
    """

    if value is None:
        return callback(*args)

    accepts_keyword = _callable_accepts_keyword(callback, keyword)
    callback_name = getattr(callback, "__qualname__", repr(callback))

    try:
        return callback(*args, **{keyword: value})
    except TypeError as err:
        if keyword not in str(err):
            raise
        if accepts_keyword:
            _LOGGER.debug(
                "%s advertised keyword '%s' but raised TypeError; retrying without it",  # noqa: G004
                callback_name,
                keyword,
            )
        else:
            _LOGGER.debug(
                "%s rejected optional keyword '%s'; retrying without it",  # noqa: G004
                callback_name,
                keyword,
            )
        return callback(*args)


def _collect_entry_subentries(entry: ConfigEntry) -> tuple[Any, ...]:
    """Return all known subentries associated with ``entry``."""

    runtime_data = getattr(entry, "runtime_data", None)
    containers: list[Iterable[Any]] = []

    if runtime_data is not None:
        manager = getattr(runtime_data, "subentry_manager", None)
        managed = getattr(manager, "managed_subentries", None)
        if isinstance(managed, Mapping):
            containers.append(managed.values())

        coordinator = getattr(runtime_data, "coordinator", None)
    else:
        coordinator = getattr(entry, "coordinator", None)

    entry_subentries = getattr(entry, "subentries", None)
    if isinstance(entry_subentries, Mapping):
        containers.append(entry_subentries.values())
    else:
        managed = getattr(entry_subentries, "managed_subentries", None)
        if isinstance(managed, Mapping):
            containers.append(managed.values())

    metadata = getattr(coordinator, "_subentry_metadata", None)
    if isinstance(metadata, Mapping):
        containers.append(metadata.values())

    collected: dict[str, Any] = {}
    unidentified: list[Any] = []
    for container in containers:
        for subentry in container:
            identifier = _resolve_config_subentry_identifier(subentry)
            if identifier is None:
                unidentified.append(subentry)
                continue
            collected.setdefault(identifier, subentry)

    ordered: list[Any] = list(collected.values())
    for subentry in unidentified:
        if subentry not in ordered:
            ordered.append(subentry)

    return tuple(ordered)

_LEGACY_PLATFORM_TRACKERS: WeakKeyDictionary[ConfigEntry, set[str]] = WeakKeyDictionary()
_LEGACY_NOTICE_TRACKER: WeakKeyDictionary[ConfigEntry, bool] = WeakKeyDictionary()


def _legacy_forwarded_platforms(entry: ConfigEntry) -> set[str]:
    """Return the cached parent-level platform set for legacy cores."""

    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, RuntimeData):
        forwarded = getattr(runtime_data, "legacy_forwarded_platforms", None)
        if isinstance(forwarded, set):
            return forwarded
        runtime_data.legacy_forwarded_platforms = set()
        return runtime_data.legacy_forwarded_platforms

    forwarded = _LEGACY_PLATFORM_TRACKERS.get(entry)
    if forwarded is None:
        forwarded = set()
        _LEGACY_PLATFORM_TRACKERS[entry] = forwarded
    return forwarded


def _legacy_notice_logged(entry: ConfigEntry) -> bool:
    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, RuntimeData):
        return bool(runtime_data.legacy_forward_notice)
    return bool(_LEGACY_NOTICE_TRACKER.get(entry))


def _mark_legacy_notice_logged(entry: ConfigEntry) -> None:
    runtime_data = getattr(entry, "runtime_data", None)
    if isinstance(runtime_data, RuntimeData):
        runtime_data.legacy_forward_notice = True
        return
    _LEGACY_NOTICE_TRACKER[entry] = True


async def _async_setup_legacy_child_subentry(
    hass: HomeAssistant, entry: MyConfigEntry
) -> bool:
    """Legacy setup path for child config entries."""

    subentry_type = getattr(entry, "subentry_type", None)
    group_key = entry.data.get("group_key")
    parent_entry_id = getattr(entry, "parent_entry_id", None)
    _LOGGER.debug(
        "[%s] Setting up subentry (parent_id=%s, type=%s, key=%s)",
        entry.entry_id,
        parent_entry_id,
        subentry_type,
        group_key,
    )

    if not parent_entry_id:
        _LOGGER.debug(
            "[%s] Aborting subentry setup because parent_entry_id is missing",  # noqa: G004
            entry.entry_id,
        )
        return False

    bucket = _domain_data(hass)
    entries_bucket = _ensure_entries_bucket(bucket)

    parent_payload = cast(
        RuntimeData | GoogleFindMyCoordinator | None,
        entries_bucket.get(parent_entry_id),
    )
    if parent_payload is None:
        _LOGGER.debug(
            "[%s] Parent runtime data bucket missing for %s; deferring setup",  # noqa: G004
            entry.entry_id,
            parent_entry_id,
        )
        raise ConfigEntryNotReady(
            f"Parent entry {parent_entry_id} not yet initialized"
        )

    coordinator: GoogleFindMyCoordinator | None = None
    if isinstance(parent_payload, GoogleFindMyCoordinator):
        coordinator = parent_payload
    else:
        coordinator = getattr(parent_payload, "coordinator", None)

    if coordinator is None:
        _LOGGER.debug(
            "[%s] Parent runtime data for %s is missing a ready coordinator; deferring setup",
            entry.entry_id,
            parent_entry_id,
        )
        raise ConfigEntryNotReady(
            f"Parent entry {parent_entry_id} coordinator not ready"
        )

    if isinstance(parent_payload, RuntimeData):
        entry.runtime_data = parent_payload
    else:
        entry.runtime_data = coordinator

    _LOGGER.debug(
        "[%s] Legacy subentry setup complete, runtime data attached (Parent: %s)",
        entry.entry_id,
        parent_entry_id,
    )

    return True


async def _async_setup_subentry(
    hass: HomeAssistant,
    entry: MyConfigEntry,
    subentry: Any | None = None,
) -> bool:
    """Attach parent runtime data during subentry setup.

    NOTE: Home Assistant Core calls this function when setting up each config
    subentry. It must not forward platforms itself. Manual platform forwarding
    via ``ConfigEntries.async_forward_entry_setups`` cannot convey a
    ``config_subentry_id`` (per the 2025.11.2 SSoT signature) and therefore
    cannot be used for subentries. After this function returns ``True``, HA
    drives platform setup for the subentry and injects the correct
    ``config_subentry_id`` when invoking platform ``async_setup_entry``
    handlers.
    """

    if subentry is None:
        return await _async_setup_legacy_child_subentry(hass, entry)

    parent_entry_id = getattr(entry, "parent_entry_id", None)
    if not parent_entry_id:
        _LOGGER.error(
            "[%s] Subentry setup failed: parent_entry_id is missing",
            entry.entry_id,
        )
        return False

    config_entries = getattr(hass, "config_entries", None)
    if config_entries is None:
        _LOGGER.error(
            "[%s] Subentry setup failed: hass.config_entries is unavailable",
            entry.entry_id,
        )
        raise ConfigEntryNotReady("config_entries manager unavailable")

    parent_entry = getattr(config_entries, "async_get_entry", lambda _entry_id: None)(
        parent_entry_id
    )
    if parent_entry is None:
        _LOGGER.error(
            "[%s] Subentry setup failed: parent entry %s not registered",  # noqa: G004
            entry.entry_id,
            parent_entry_id,
        )
        raise ConfigEntryNotReady(f"Parent entry {parent_entry_id} missing")

    subentry_identifier = _resolve_config_subentry_identifier(subentry) or getattr(
        entry, "config_subentry_id", None
    )
    if subentry_identifier is None:
        subentry_identifier = entry.entry_id

    registered_ids = _registered_subentry_ids(hass, parent_entry)
    if not registered_ids or subentry_identifier not in registered_ids:
        _LOGGER.error(
            "[%s] Config subentry %s not registered under parent %s; aborting setup",  # noqa: G004
            entry.entry_id,
            subentry_identifier,
            parent_entry_id,
        )
        raise ConfigEntryNotReady(
            f"Config subentry {subentry_identifier} is not registered"
        )

    bucket = _domain_data(hass)
    entries_bucket = _ensure_entries_bucket(bucket)

    parent_runtime_data = entries_bucket.get(parent_entry_id)
    if parent_runtime_data is None:
        _LOGGER.warning(
            "[%s] Parent runtime data bucket missing for %s; deferring setup",
            entry.entry_id,
            parent_entry_id,
        )
        raise ConfigEntryNotReady(
            f"Parent entry {parent_entry_id} not yet initialized"
        )

    coordinator = getattr(parent_runtime_data, "coordinator", None)
    if coordinator is None:
        _LOGGER.warning(
            "[%s] Parent runtime data for %s is missing a ready coordinator; deferring setup",
            entry.entry_id,
            parent_entry_id,
        )
        raise ConfigEntryNotReady(
            f"Parent entry {parent_entry_id} coordinator not ready"
        )

    runtime_manager = getattr(parent_runtime_data, "subentry_manager", None)
    coordinator = getattr(parent_runtime_data, "coordinator", None)

    if runtime_manager is not None:
        try:
            runtime_manager._refresh_from_entry()  # noqa: SLF001 - internal sync for new subentries
        except Exception as err:  # pragma: no cover - defensive refresh guard
            _LOGGER.debug(
                "[%s] Failed to refresh subentry manager during setup: %s",
                entry.entry_id,
                err,
            )

    if coordinator is not None:
        try:
            coordinator._refresh_subentry_index()  # noqa: SLF001 - keep metadata aligned
        except Exception as err:  # pragma: no cover - defensive refresh guard
            _LOGGER.debug(
                "[%s] Coordinator subentry index refresh deferred: %s",
                entry.entry_id,
                err,
            )
        try:
            await coordinator.async_request_refresh()
        except Exception as err:  # pragma: no cover - defensive refresh guard
            _LOGGER.debug(
                "[%s] Coordinator refresh after subentry setup skipped: %s",
                entry.entry_id,
                err,
            )

    entry.runtime_data = parent_runtime_data

    _LOGGER.debug(
        "[%s] Subentry setup complete, runtime data attached (Parent: %s)",
        entry.entry_id,
        parent_entry_id,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    """Set up a config entry.

    Order of operations (important):
      1) Multi-entry policy: allow multiple entries; prevent duplicate-account entries.
      2) Initialize and register TokenCache (entry-scoped, no default).
      3) Soft-migrate options and unique_ids; acquire and wire the shared FCM provider.
      4) Seed token cache from entry data (secrets bundle or individual tokens).
      5) Build coordinator, register views, and synchronize subentries.
      6) Schedule initial refresh after HA is fully started.
    """
    parent_entry_id = getattr(entry, "parent_entry_id", None)
    if parent_entry_id:
        return await _async_setup_subentry(hass, entry)

    setattr(entry, "_gfm_parent_platforms_unloaded", False)
    setattr(entry, "_gfm_parent_unload_call_count", 0)

    _ensure_runtime_imports()
    # --- Multi-entry policy: allow MA; block duplicate-account (same email) ----
    # Legacy issue cleanup: we no longer block on multiple config entries
    try:
        ir.async_delete_issue(hass, DOMAIN, "multiple_config_entries")
    except Exception:
        pass

    should_setup, normalized_email = await _ensure_post_migration_consistency(
        hass, entry
    )
    if not should_setup:
        _LOGGER.info(
            "Skipping setup for %s due to duplicate account %s",
            entry.entry_id,
            _mask_email_for_logs(normalized_email),
        )
        return False

    pm_setup_start = time.monotonic()

    # Distinguish cold start vs. reload
    domain_bucket = _domain_data(hass)
    _ensure_device_owner_index(domain_bucket)  # ensure present (E2.5)
    if "nova_refcount" not in domain_bucket:
        _set_nova_refcount(domain_bucket, 0)

    # 1) Token cache: create/register early (ENTRY-SCOPED ONLY)
    legacy_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "Auth", "secrets.json"
    )
    cache = await TokenCache.create(hass, entry.entry_id, legacy_path=legacy_path)

    # Ensure deferred writes are flushed on HA shutdown
    async def _flush_on_stop(event: Event) -> None:
        """Flush deferred saves on Home Assistant stop."""
        try:
            await cache.flush()
        except (HomeAssistantError, ValueError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Cache flush on stop raised: %s", err)

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _flush_on_stop)
    )

    # Early, idempotent seeding of TokenCache from entry.data (authoritative SSOT)
    try:
        if DATA_AUTH_METHOD in entry.data:
            await cache.async_set_cached_value(
                DATA_AUTH_METHOD, entry.data[DATA_AUTH_METHOD]
            )
            _LOGGER.debug("Seeded auth_method into TokenCache from entry.data")
        if CONF_OAUTH_TOKEN in entry.data:
            await cache.async_set_cached_value(
                CONF_OAUTH_TOKEN, entry.data[CONF_OAUTH_TOKEN]
            )
            _LOGGER.debug("Seeded oauth_token into TokenCache from entry.data")
        if DATA_AAS_TOKEN in entry.data:
            await cache.async_set_cached_value(
                DATA_AAS_TOKEN, entry.data[DATA_AAS_TOKEN]
            )
            _LOGGER.debug("Seeded aas_token into TokenCache from entry.data")
        if CONF_GOOGLE_EMAIL in entry.data:
            await cache.async_set_cached_value(
                username_string, entry.data[CONF_GOOGLE_EMAIL]
            )
            _LOGGER.debug("Seeded google_email into TokenCache from entry.data")
    except Exception as err:
        _LOGGER.debug("Early TokenCache seeding from entry.data failed: %s", err)

    raw_mode = _opt(entry, OPT_CONTRIBUTOR_MODE, DEFAULT_CONTRIBUTOR_MODE)
    contributor_mode = _normalize_contributor_mode(raw_mode)
    now_epoch = int(time.time())

    cached_mode = None
    try:
        cached_raw = await cache.async_get_cached_value(CACHE_KEY_CONTRIBUTOR_MODE)
    except Exception as err:
        _LOGGER.debug("Failed to read cached contributor mode: %s", err)
        cached_raw = None
    if isinstance(cached_raw, str):
        cached_mode = _normalize_contributor_mode(cached_raw)

    last_mode_switch_epoch: int | None = None
    try:
        cached_switch = await cache.async_get_cached_value(CACHE_KEY_LAST_MODE_SWITCH)
    except Exception as err:
        _LOGGER.debug("Failed to read cached network mode switch timestamp: %s", err)
        cached_switch = None
    if isinstance(cached_switch, (int, float)):
        last_mode_switch_epoch = int(cached_switch)

    if contributor_mode != cached_mode:
        last_mode_switch_epoch = now_epoch
        try:
            await cache.async_set_cached_value(
                CACHE_KEY_CONTRIBUTOR_MODE, contributor_mode
            )
            await cache.async_set_cached_value(
                CACHE_KEY_LAST_MODE_SWITCH, last_mode_switch_epoch
            )
        except Exception as err:
            _LOGGER.debug("Failed to persist contributor mode preference: %s", err)
    elif last_mode_switch_epoch is None:
        last_mode_switch_epoch = now_epoch
        try:
            await cache.async_set_cached_value(
                CACHE_KEY_LAST_MODE_SWITCH, last_mode_switch_epoch
            )
        except Exception as err:
            _LOGGER.debug("Failed to initialize contributor mode timestamp: %s", err)

    # Optional: register HA-managed aiohttp session for Nova API (defer import)
    try:
        from .NovaApi import nova_request as nova

        reg = getattr(nova, "register_hass", None)
        unreg = getattr(nova, "unregister_session_provider", None)
        unreg_hass = getattr(nova, "unregister_hass", None)
        if callable(reg):
            try:
                reg(hass)
            except Exception as err:
                _LOGGER.debug("Nova API register_hass() raised: %s", err)
            else:
                domain_bucket = _domain_data(hass)
                refcount = _get_nova_refcount(domain_bucket) + 1
                _set_nova_refcount(domain_bucket, refcount)
                _LOGGER.debug("Nova session provider refcount -> %s", refcount)

                def _release_nova_session_provider() -> None:
                    inner_bucket = _domain_data(hass)
                    inner_refcount = max(_get_nova_refcount(inner_bucket) - 1, 0)
                    _set_nova_refcount(inner_bucket, inner_refcount)
                    _LOGGER.debug(
                        "Nova session provider refcount -> %s", inner_refcount
                    )
                    if inner_refcount != 0:
                        return
                    if callable(unreg_hass):
                        try:
                            unreg_hass()
                        except Exception as err:  # pragma: no cover - defensive
                            _LOGGER.debug(
                                "Nova unregister_hass raised during unload: %s", err
                            )
                    if callable(unreg):
                        try:
                            unreg()
                        except Exception as err:  # pragma: no cover - defensive
                            _LOGGER.debug(
                                "Nova unregister_session_provider raised: %s", err
                            )

                entry.async_on_unload(_release_nova_session_provider)
        else:
            _LOGGER.debug(
                "Nova API register_hass() not available; continuing with module defaults."
            )
    except Exception as err:
        _LOGGER.debug("Nova API session provider registration skipped: %s", err)

    # Soft-migrate mutable settings from data -> options and unique_ids
    _migrate_entry_identifier_namespaces(hass, entry)
    await _async_soft_migrate_data_to_options(hass, entry)
    await _async_migrate_unique_ids(hass, entry)
    await _async_relink_button_devices(hass, entry)
    await _async_relink_subentry_entities(hass, entry)

    # Acquire shared FCM and create a startup barrier for the first poll cycle.
    fcm_ready_event = asyncio.Event()
    fcm = await _async_acquire_shared_fcm(hass)
    pm_fcm_acquired = time.monotonic()
    fcm_ready_event.set()

    # Signal-only stop on unload (bounded; actual await in async_unload_entry)
    def _on_unload_signal_fcm() -> None:
        try:
            fcm.request_stop()
        except Exception as err:
            _LOGGER.debug("FCM stop signal during unload raised: %s", err)

    entry.async_on_unload(_on_unload_signal_fcm)

    # Credentials seed: legacy bundle OR individual oauth_token+email must be present
    secrets_data = entry.data.get(DATA_SECRET_BUNDLE)
    oauth_token = entry.data.get(CONF_OAUTH_TOKEN)
    aas_token_entry = entry.data.get(DATA_AAS_TOKEN)
    google_email = entry.data.get(CONF_GOOGLE_EMAIL)

    if secrets_data:
        await _async_save_secrets_data(cache, secrets_data)
        _LOGGER.debug("Persisted secrets.json bundle to token cache (entry-scoped)")
        if isinstance(aas_token_entry, str) and aas_token_entry:
            await cache.async_set_cached_value(DATA_AAS_TOKEN, aas_token_entry)
            _LOGGER.debug("Stored pre-provided AAS token in TokenCache (entry-scoped)")
    elif (oauth_token or aas_token_entry) and google_email:
        await _async_seed_manual_credentials(
            cache,
            oauth_token,
            aas_token_entry,
            google_email,
        )
    else:
        _LOGGER.error(
            "No credentials found in config entry (neither secrets_data nor oauth_token+google_email)"
        )
        raise ConfigEntryNotReady("Credentials missing")

    # Remove legacy parent links from tracker devices before building runtime state.
    _self_heal_device_registry(hass, entry)

    # Build effective runtime settings (options-first)
    coordinator = GoogleFindMyCoordinator(
        hass,
        cache=cache,
        location_poll_interval=_opt(
            entry, OPT_LOCATION_POLL_INTERVAL, DEFAULT_LOCATION_POLL_INTERVAL
        ),
        device_poll_delay=_opt(entry, OPT_DEVICE_POLL_DELAY, DEFAULT_DEVICE_POLL_DELAY),
        min_poll_interval=_opt(entry, OPT_MIN_POLL_INTERVAL, DEFAULT_MIN_POLL_INTERVAL),
        min_accuracy_threshold=_opt(
            entry, OPT_MIN_ACCURACY_THRESHOLD, DEFAULT_MIN_ACCURACY_THRESHOLD
        ),
        allow_history_fallback=_opt(
            entry,
            OPT_ALLOW_HISTORY_FALLBACK,
            DEFAULT_OPTIONS.get(OPT_ALLOW_HISTORY_FALLBACK, False),
        ),
        contributor_mode=contributor_mode,
        contributor_mode_switch_epoch=last_mode_switch_epoch,
    )
    coordinator.config_entry = entry  # convenience for platforms

    # --- BEGIN CORRECTED STARTUP ORDER ---

    # STEP 1: Create Sub-Entry-Manager (as before)
    entry_state = getattr(entry, "state", None)
    reload_states: tuple[ConfigEntryState, ...] = (
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.SETUP_RETRY,
    )
    is_reload = False
    if isinstance(entry_state, ConfigEntryState):
        is_reload = entry_state in reload_states
    elif isinstance(entry_state, str):
        reload_values = tuple(
            state.value if hasattr(state, "value") else str(state)
            for state in reload_states
        )
        is_reload = entry_state in reload_values

    # Use the runtime-attached manager here
    _subentry_setup_tracker(hass, entry).clear()

    runtime_subentry_manager = ConfigEntrySubEntryManager(hass, entry)
    coordinator.attach_subentry_manager(
        runtime_subentry_manager, is_reload=is_reload
    )

    # STEP 2: Create and Synchronize Sub-Entries (RUNNING NOW)
    # This is necessary so the coordinator knows the Sub-Entry-IDs in Step 3.
    tracker_features = sorted(TRACKER_FEATURE_PLATFORMS)
    service_features = sorted(SERVICE_FEATURE_PLATFORMS)
    fcm_push_enabled = fcm is not None

    filter_cls = _resolve_google_home_filter_class()
    google_home_filter_instance: GoogleHomeFilterProtocol | None = None
    if filter_cls is not None:
        try:
            google_home_filter_instance = cast(
                GoogleHomeFilterProtocol, filter_cls(hass, _effective_config(entry))
            )
            _LOGGER.debug("Initialized Google Home filter (options-first)")
        except Exception as err:
            _LOGGER.debug("GoogleHomeFilter attach skipped due to: %s", err)
    else:
        _LOGGER.debug("GoogleHomeFilter not available; continuing without it")

    has_google_home_filter = google_home_filter_instance is not None
    entry_title = entry.title or entry.data.get(CONF_GOOGLE_EMAIL, "Google Find My")

    await runtime_subentry_manager.async_sync(
        [
            ConfigEntrySubentryDefinition(
                key=TRACKER_SUBENTRY_KEY,
                title="Google Find My devices",
                data={
                    "features": tracker_features,
                    "fcm_push_enabled": fcm_push_enabled,
                    "has_google_home_filter": has_google_home_filter,
                    "entry_title": entry_title,
                },
                subentry_type=SUBENTRY_TYPE_TRACKER,
                unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}",
                translation_key=TRACKER_SUBENTRY_TRANSLATION_KEY,
            ),
            ConfigEntrySubentryDefinition(
                key=SERVICE_SUBENTRY_KEY,
                title="Google Find Hub Service",
                data={
                    "features": service_features,
                    "fcm_push_enabled": fcm_push_enabled,
                    "has_google_home_filter": has_google_home_filter,
                    "entry_title": entry_title,
                },
                subentry_type=SUBENTRY_TYPE_SERVICE,
                unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
                translation_key=SERVICE_SUBENTRY_TRANSLATION_KEY,
            ),
        ]
    )

    # STEP 3: Run First Refresh (AFTER Sub-Entries exist)
    # Now _ensure_registry_for_devices can find the correct Sub-Entry-IDs.
    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        raise
    except Exception as err:  # noqa: BLE001 - wrap unexpected failures for HA retry
        _LOGGER.error(
            "[%s] Initial coordinator refresh failed: %s", entry.entry_id, err, exc_info=True
        )
        raise ConfigEntryNotReady(f"Initial refresh failed: {err}") from err

    await _async_normalize_device_names(hass)

    # --- END CORRECTED STARTUP ORDER ---

    # Performance metrics injection
    try:
        perf = getattr(coordinator, "performance_metrics", None)
        if not isinstance(perf, dict):
            perf = {}
            setattr(coordinator, "performance_metrics", perf)
        perf["setup_start_monotonic"] = pm_setup_start
        perf["fcm_acquired_monotonic"] = pm_fcm_acquired
    except Exception as err:
        _LOGGER.debug("Failed to set performance metrics on coordinator: %s", err)

    # Hand over the barrier without changing the coordinator's signature.
    setattr(coordinator, "fcm_ready_event", fcm_ready_event)

    # Register the coordinator with the shared FCM receiver
    fcm.register_coordinator(coordinator)
    entry.async_on_unload(lambda: fcm.unregister_coordinator(coordinator))

    # Ensure FCM supervisor is running for background push updates (idempotent).
    try:
        await fcm._start_listening()  # noqa: SLF001
    except AttributeError:
        _LOGGER.debug(
            "FCM receiver has no _start_listening(); relying on on-demand start via per-request registration."
        )

    # Expose runtime object via the typed container (preferred access pattern)
    entity_recovery_manager = EntityRecoveryManager(hass, entry, coordinator)

    runtime_data = RuntimeData(
        coordinator=coordinator,
        token_cache=cache,
        subentry_manager=runtime_subentry_manager,
        fcm_receiver=fcm,
        entity_recovery_manager=entity_recovery_manager,
        google_home_filter=google_home_filter_instance,
    )
    entry.runtime_data = runtime_data
    entries_bucket: dict[str, RuntimeData] = _ensure_entries_bucket(domain_bucket)
    entries_bucket[entry.entry_id] = runtime_data

    entity_registry = er.async_get(hass)
    registry_entries_iterable: Iterable[Any] = _iter_config_entry_entities(
        entity_registry, entry.entry_id
    )

    reactivated = 0
    for entity_entry in registry_entries_iterable:
        if (
            entity_entry.domain == "button"
            and entity_entry.platform == DOMAIN
            and entity_entry.disabled_by == RegistryEntryDisabler.INTEGRATION
        ):
            entity_registry.async_update_entity(
                entity_entry.entity_id, disabled_by=None
            )
            reactivated += 1

    if reactivated:
        _LOGGER.debug(
            "Re-enabled %s button entities disabled by integration", reactivated
        )

    # Owner-index scaffold (E2.5): coordinator will eventually claim canonical_ids
    _ensure_device_owner_index(domain_bucket)

    # STEP 4: Load Platforms
    # Home Assistant Core will automatically set up subentry platforms after this
    # coroutine returns. Manual forwarding (for example, via async_forward_entry_setups)
    # must not be invoked here because the helper's 2025.11.2 SSoT signature does not
    # accept config_subentry_id, which would strip subentry context.

    bucket = domain_bucket

    # Coordinator setup (DR listeners, initial index, etc.)
    try:
        await coordinator.async_setup()
    except Exception as err:
        _LOGGER.warning(
            "Coordinator setup failed early; will recover on next refresh: %s", err
        )

    # Register map views (idempotent across multi-entry)
    views_registered = bucket.get("views_registered")
    if not isinstance(views_registered, bool):
        views_registered = False
    if not views_registered:
        map_view_instance = GoogleFindMyMapView(hass)
        hass.http.register_view(map_view_instance)

        map_redirect_view_instance = GoogleFindMyMapRedirectView(hass)
        hass.http.register_view(map_redirect_view_instance)
        bucket["views_registered"] = True
        _LOGGER.debug("Registered map views")

    # Run duplicate self-healing asynchronously so it also executes on reloads.
    hass.async_create_task(
        _async_self_heal_duplicate_entities(hass, entry),
        name=f"{DOMAIN}.self_heal_duplicates.{entry.entry_id}",
    )

    # Mark initial setup complete (used to distinguish cold start vs. reload)
    domain_bucket["initial_setup_complete"] = True

    # Final performance marker
    try:
        perf = getattr(coordinator, "performance_metrics", None)
        if isinstance(perf, dict):
            perf["setup_end_monotonic"] = time.monotonic()
    except Exception as err:
        _LOGGER.debug("Failed to set setup_end_monotonic: %s", err)

    _register_instance(entry.entry_id, cache)

    # After the parent runtime bucket exists, explicitly set up all known subentries.
    # Home Assistant will handle per-subentry platform forwarding automatically;
    # the integration must only trigger config entry setup here and after
    # programmatic creation (see docs/CONFIG_SUBENTRIES_HANDBOOK.md).
    await _async_setup_new_subentries(
        hass,
        entry,
        entry.subentries.values(),
        enforce_registration=True,
    )

    # Home Assistant will fan out platform setup per subentry after this returns
    # (see docs/CONFIG_SUBENTRIES_HANDBOOK.md). Forwarding here would drop the
    # subentry context and block the subsequent per-subentry setup calls.
    _LOGGER.debug(
        "[%s] Skipping parent platform forward; waiting for Home Assistant to schedule subentry platforms",
        entry.entry_id,
    )

    return True


async def _async_save_secrets_data(
    cache: TokenCache, secrets_data: Mapping[str, Any]
) -> None:
    """Persist a legacy secrets.json bundle into the entry-scoped token cache.

    Notes:
        - Store JSON-serializable values *as-is*. TokenCache validates and normalizes.
        - Uses the *entry-local* cache instance (no global facade).
    """
    enhanced_data = dict(secrets_data)

    # Normalize username key across old/new secrets variants
    google_email = secrets_data.get("username", secrets_data.get("Email"))
    if google_email:
        enhanced_data[username_string] = google_email

    for key, value in enhanced_data.items():
        try:
            if isinstance(value, (str, int, float, bool, dict, list)) or value is None:
                await cache.async_set_cached_value(key, value)
            else:
                await cache.async_set_cached_value(key, json.dumps(value))
        except (OSError, TypeError) as err:
            _LOGGER.warning("Failed to save '%s' to persistent cache: %s", key, err)


async def _async_seed_manual_credentials(
    cache: TokenCache,
    oauth_token: str | None,
    aas_token_entry: str | None,
    google_email: str,
) -> None:
    """Persist manual credential updates and clear stale AAS tokens when absent."""

    token_to_save = oauth_token or aas_token_entry
    if isinstance(token_to_save, str) and token_to_save:
        await _async_save_individual_credentials(cache, token_to_save, google_email)
        _LOGGER.debug("Persisted individual credentials to token cache (entry-scoped)")

    if isinstance(aas_token_entry, str) and aas_token_entry:
        await cache.async_set_cached_value(DATA_AAS_TOKEN, aas_token_entry)
        _LOGGER.debug("Stored AAS token provided alongside manual credentials")
    else:
        await cache.async_set_cached_value(DATA_AAS_TOKEN, None)
        _LOGGER.debug(
            "Cleared entry-scoped AAS token so a fresh value is minted from the new OAuth token"
        )


async def _async_save_individual_credentials(
    cache: TokenCache, oauth_token: str, google_email: str
) -> None:
    """Persist individual credentials (oauth_token + email) to the *entry-scoped* token cache."""
    try:
        await cache.async_set_cached_value(CONF_OAUTH_TOKEN, oauth_token)
        await cache.async_set_cached_value(username_string, google_email)
    except OSError as err:
        _LOGGER.warning("Failed to save individual credentials to cache: %s", err)


# ------------------- Device removal (HA "Delete device" hook) -------------------


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Handle 'Delete device' requests from Home Assistant.

    Semantics:
    - Only act on devices owned by this integration/entry (identifier domain matches and
      the device is linked to this config entry).
    - Purge in-memory caches for the device via the coordinator (keeps UI/state clean).
    - Add the id to `ignored_devices` to prevent automatic re-creation.
    - Return True to allow HA to remove the device record and its entities.
    - Never allow removing the integration's own "service" device (ident startswith 'integration').
    """
    _ensure_runtime_imports()
    if entry.entry_id not in device_entry.config_entries:
        return False

    raw_ident: str | None = None
    canonical_id: str | None = None
    for domain, ident in device_entry.identifiers:
        if domain != DOMAIN or not isinstance(ident, str) or not ident:
            continue
        raw_ident = ident
        canonical_id = _normalize_device_identifier(device_entry, ident)
        break

    if not canonical_id:
        return False

    if _device_is_service_device(device_entry, entry.entry_id):
        return False

    try:
        bucket = _domain_data(hass)
        entries_bucket = _ensure_entries_bucket(bucket)
        runtime: RuntimeData | GoogleFindMyCoordinator | None = entries_bucket.get(
            entry.entry_id
        )
        if runtime is None:
            runtime = getattr(entry, "runtime_data", None)

        coordinator: GoogleFindMyCoordinator | None = None
        purge_device: Callable[[str], Any] | None = None

        if isinstance(runtime, GoogleFindMyCoordinator):
            coordinator = runtime
        elif runtime is not None:
            coordinator = getattr(runtime, "coordinator", None)

        if isinstance(coordinator, GoogleFindMyCoordinator):
            purge_callable = getattr(coordinator, "purge_device", None)
            if callable(purge_callable):
                purge_device = cast(Callable[[str], Any], purge_callable)
        if purge_device is not None:
            purge_device(canonical_id)
    except Exception as err:
        _LOGGER.debug("Coordinator purge failed for %s: %s", canonical_id, err)

    try:
        opts = dict(entry.options)
        current_raw = opts.get(
            OPT_IGNORED_DEVICES, DEFAULT_OPTIONS.get(OPT_IGNORED_DEVICES)
        )
        ignored_map, _migrated = coerce_ignored_mapping(current_raw)

        canonical_meta = ignored_map.get(canonical_id)
        legacy_meta = None
        if raw_ident and raw_ident != canonical_id and raw_ident in ignored_map:
            legacy_meta = ignored_map.pop(raw_ident)

        name_to_store = device_entry.name_by_user or device_entry.name or canonical_id

        alias_sources: list[list[str] | None] = []
        name_sources: list[list[str] | None] = []

        if raw_ident and raw_ident != canonical_id:
            alias_sources.append([raw_ident])

        for meta in (canonical_meta, legacy_meta):
            if not isinstance(meta, Mapping):
                continue

            raw_aliases = meta.get("aliases")
            if isinstance(raw_aliases, Iterable) and not isinstance(
                raw_aliases, (str, bytes, bytearray)
            ):
                sanitized_aliases = [
                    alias
                    for alias in raw_aliases
                    if isinstance(alias, str) and alias
                ]
                if sanitized_aliases:
                    alias_sources.append(sanitized_aliases)
            elif isinstance(raw_aliases, str) and raw_aliases:
                alias_sources.append([raw_aliases])

            raw_name = meta.get("name")
            if isinstance(raw_name, str) and raw_name:
                name_sources.append([raw_name])

        aliases: list[str] = _dedupe_aliases(
            name_to_store,
            *alias_sources,
            *name_sources,
        )

        ignored_at = int(time.time())

        source = next(
            (
                meta.get("source")
                for meta in (canonical_meta, legacy_meta)
                if isinstance(meta, Mapping)
                and isinstance(meta.get("source"), str)
                and meta.get("source")
            ),
            "registry",
        )

        ignored_map[canonical_id] = {
            "name": name_to_store,
            "aliases": aliases,
            "ignored_at": ignored_at,
            "source": source,
        }
        opts[OPT_IGNORED_DEVICES] = ignored_map
        opts[OPT_OPTIONS_SCHEMA_VERSION] = 2

        if opts != entry.options:
            hass.config_entries.async_update_entry(entry, options=opts)
            _LOGGER.info(
                "Marked device '%s' (%s) as ignored for entry '%s'",
                name_to_store,
                canonical_id,
                entry.title,
            )
    except Exception as err:
        _LOGGER.debug("Persisting delete decision failed for %s: %s", canonical_id, err)

    return True


# ------------------------------- Misc helpers ---------------------------------


async def _async_normalize_device_names(hass: HomeAssistant) -> None:
    """One-time normalization: strip legacy 'Find My - ' prefix from device names."""
    try:
        dev_reg = dr.async_get(hass)
        updated = 0
        for device in list(dev_reg.devices.values()):
            if not any(domain == DOMAIN for domain, _ in device.identifiers):
                continue
            if device.name_by_user:
                continue  # user-chosen names stay untouched
            name = device.name or ""
            if name.startswith("Find My - "):
                new_name = name[len("Find My - ") :].strip()
                if new_name and new_name != name:
                    dev_reg.async_update_device(device_id=device.id, name=new_name)
                    updated += 1
        if updated:
            _LOGGER.info(
                'Normalized %d device name(s) by removing legacy "Find My - " prefix',
                updated,
            )
    except Exception as err:
        _LOGGER.debug("Device name normalization skipped due to: %s", err)


async def _async_unload_subentry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    """Unload a subentry (tracker or service) by unloading its platforms."""

    _LOGGER.debug(
        "[%s] Unloading subentry (parent_id=%s, type=%s, key=%s)",
        entry.entry_id,
        getattr(entry, "parent_entry_id", None),
        getattr(entry, "subentry_type", None),
        entry.data.get("group_key"),
    )
    result: Any = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Home Assistant returns ``False`` when any platform refuses to unload; keep
    # the runtime data so retry attempts can reuse the existing coordinator.
    unload_success = bool(result)
    if unload_success and hasattr(entry, "runtime_data"):
        setattr(entry, "runtime_data", None)
    return unload_success


async def _async_unload_parent_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    """Unload a parent entry by unloading children and cleaning up resources."""

    _ensure_runtime_imports()
    _LOGGER.debug("[%s] Unloading parent entry", entry.entry_id)

    bucket = _domain_data(hass)
    entries_bucket = _ensure_entries_bucket(bucket)
    runtime_data: RuntimeData | None = entries_bucket.pop(entry.entry_id, None)
    if runtime_data is None:
        runtime_data_raw = getattr(entry, "runtime_data", None)
        if isinstance(runtime_data_raw, RuntimeData):
            runtime_data = runtime_data_raw

    subentries = _collect_entry_subentries(entry)

    unload_parent_platforms = True
    unload_callable = getattr(hass.config_entries, "async_unload_platforms", None)
    unload_call_count = int(getattr(entry, "_gfm_parent_unload_call_count", 0) or 0)
    in_progress = bool(getattr(entry, "_gfm_parent_unload_in_progress", False))
    unload_completed = bool(
        getattr(entry, "_gfm_parent_platforms_unloaded", False)
    )

    if unload_completed and not in_progress:
        _LOGGER.debug(
            "[%s] Parent platforms already unloaded; skipping duplicate call",
            entry.entry_id,
        )
        return True
    if in_progress:
        _LOGGER.debug(
            "[%s] Parent unload already in progress; ignoring duplicate request",
            entry.entry_id,
        )
        return True

    overall_success = False
    setattr(entry, "_gfm_parent_unload_in_progress", True)
    try:
        if callable(unload_callable):
            setattr(entry, "_gfm_parent_unload_call_count", unload_call_count + 1)
            try:
                result = unload_callable(entry, tuple(PLATFORMS))
                if isinstance(result, Awaitable):
                    result = await result
                unload_parent_platforms = bool(result) if result is not None else True
            except Exception as err:  # noqa: BLE001 - defensive logging
                _LOGGER.error(
                    "[%s] Failed to unload parent platforms: %s",  # noqa: G004
                    entry.entry_id,
                    err,
                )
                unload_parent_platforms = False
            else:
                if unload_parent_platforms:
                    setattr(entry, "_gfm_parent_platforms_unloaded", True)
        else:
            _LOGGER.debug(
                "[%s] Home Assistant instance lacks async_unload_platforms; skipping parent unload",
                entry.entry_id,
            )
            setattr(entry, "_gfm_parent_platforms_unloaded", True)

        if not unload_parent_platforms:
            if runtime_data is not None:
                entries_bucket[entry.entry_id] = runtime_data
            _LOGGER.debug(
                "[%s] Parent platform unload aborted; leaving subentries managed by HA",  # noqa: G004
                entry.entry_id,
            )
            return False

        async def _unload_config_subentry(subentry: Any) -> bool:
            identifier = _resolve_config_subentry_identifier(subentry)
            platforms = _determine_subentry_platforms(subentry)

            forward_unload = getattr(
                hass.config_entries, "async_forward_entry_unload", None
            )
            if isinstance(identifier, str) and identifier and callable(forward_unload):
                all_unloaded = True
                unload_awaitables: list[Awaitable[Any]] = []
                for platform in platforms:
                    platform_name = getattr(platform, "value", str(platform))
                    result = _invoke_with_optional_keyword(
                        forward_unload,
                        (entry, platform_name),
                        "config_subentry_id",
                        identifier,
                    )
                    if inspect.isawaitable(result):
                        unload_awaitables.append(result)
                    else:
                        all_unloaded = all_unloaded and (
                            bool(result) if result is not None else True
                        )

                if unload_awaitables:
                    gathered = await asyncio.gather(
                        *unload_awaitables, return_exceptions=True
                    )
                    for outcome in gathered:
                        if isinstance(outcome, BaseException):
                            all_unloaded = False
                            continue
                        if isinstance(outcome, bool):
                            all_unloaded = all_unloaded and outcome
                        else:
                            all_unloaded = all_unloaded and True

                return all_unloaded

            entry_id = getattr(subentry, "entry_id", None)
            unload_child = getattr(hass.config_entries, "async_unload", None)
            if isinstance(entry_id, str) and entry_id and callable(unload_child):
                result = unload_child(entry_id)
                if isinstance(result, Awaitable):
                    result = await result
                return bool(result)

            remove_callable = getattr(
                hass.config_entries, "async_remove_subentry", None
            )
            if isinstance(identifier, str) and identifier and callable(remove_callable):
                result = remove_callable(entry, identifier)
                if isinstance(result, Awaitable):
                    result = await result
                return bool(result)

            return True

        unload_results = await asyncio.gather(
            *(_unload_config_subentry(subentry) for subentry in subentries),
            return_exceptions=True,
        )

        unload_success = all(
            isinstance(result, bool) and result for result in unload_results
        )
        if not unload_success:
            _LOGGER.error(
                "[%s] Failed to unload one or more subentries; aborting parent unload",
                entry.entry_id,
            )
            for index, result in enumerate(unload_results):
                if isinstance(result, Exception):
                    sub_obj = subentries[index]
                    sub_id = _resolve_config_subentry_identifier(sub_obj) or getattr(
                        sub_obj, "entry_id", f"index {index}"
                    )
                    _LOGGER.debug(
                        "[%s] Subentry %s unload failed: %s",
                        entry.entry_id,
                        sub_id,
                        result,
                    )
            if runtime_data is not None:
                entries_bucket[entry.entry_id] = runtime_data
            _LOGGER.debug(
                "[%s] Subentry unload failed; runtime data restored for HA retry",  # noqa: G004
                entry.entry_id,
            )
            return False

        if runtime_data is not None:
            coordinator = runtime_data.coordinator
            if coordinator is not None:
                await coordinator.async_shutdown()

            if runtime_data.subentry_manager is not None:
                try:
                    await runtime_data.subentry_manager.async_remove_all()
                except Exception as err:
                    _LOGGER.debug("Subentry cleanup raised during unload: %s", err)

            cache = runtime_data.token_cache
            if cache is not None:
                fallback_cache = _unregister_instance(entry.entry_id)
                try:
                    await cache.close()
                    _LOGGER.debug(
                        "TokenCache for entry '%s' has been flushed and closed.",
                        entry.entry_id,
                    )
                except Exception as err:
                    _LOGGER.warning(
                        "Closing TokenCache for entry '%s' failed: %s",
                        entry.entry_id,
                        err,
                    )
                if fallback_cache is not None and fallback_cache is not cache:
                    with suppress(Exception):
                        await fallback_cache.close()

        try:
            await _async_release_shared_fcm(hass)
        except Exception as err:
            _LOGGER.debug("FCM release during parent unload raised: %s", err)

        try:
            owner_index: dict[str, str] = _ensure_device_owner_index(bucket)
            stale = [
                cid for cid, eid in list(owner_index.items()) if eid == entry.entry_id
            ]
            for cid in stale:
                owner_index.pop(cid, None)
            if stale:
                _LOGGER.debug(
                    "Cleared %d owner-index claim(s) for entry '%s'",
                    len(stale),
                    entry.entry_id,
                )
        except Exception as err:
            _LOGGER.debug("Owner-index cleanup failed: %s", err)

        if hasattr(entry, "runtime_data"):
            with suppress(Exception):
                setattr(entry, "runtime_data", None)

        overall_success = True
        return True
    finally:
        setattr(entry, "_gfm_parent_unload_in_progress", False)
        if not overall_success:
            setattr(entry, "_gfm_parent_platforms_unloaded", False)
            setattr(entry, "_gfm_parent_unload_call_count", unload_call_count)


async def async_unload_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    """Unload a config entry.

    Notes:
        - This function is now a router.
        - Parent unload logic is in `_async_unload_parent_entry`.
        - Subentry unload logic is in `_async_unload_subentry`.
    """

    parent_entry_id = getattr(entry, "parent_entry_id", None)
    if parent_entry_id:
        return await _async_unload_subentry(hass, entry)
    return await _async_unload_parent_entry(hass, entry)


async def async_remove_entry(hass: HomeAssistant, entry: MyConfigEntry) -> None:
    """Handle removal of a config entry and purge persisted caches if requested."""

    _ensure_runtime_imports()

    bucket = _domain_data(hass)
    entries_bucket = bucket.get("entries")

    runtime: RuntimeData | GoogleFindMyCoordinator | None = None
    if isinstance(entries_bucket, dict):
        runtime = entries_bucket.pop(entry.entry_id, None)

    fallback_runtime = getattr(entry, "runtime_data", None)
    if runtime is None and isinstance(
        fallback_runtime, (RuntimeData, GoogleFindMyCoordinator)
    ):
        runtime = fallback_runtime

    coordinator: GoogleFindMyCoordinator | None = None
    token_cache: Any | None = None
    google_home_filter: GoogleHomeFilterProtocol | None = None

    if isinstance(runtime, GoogleFindMyCoordinator):
        coordinator = runtime
    elif isinstance(runtime, RuntimeData):
        coordinator = runtime.coordinator
        token_cache = runtime.token_cache
        google_home_filter = runtime.google_home_filter

    if coordinator is None and isinstance(fallback_runtime, GoogleFindMyCoordinator):
        coordinator = fallback_runtime
    if token_cache is None and isinstance(fallback_runtime, RuntimeData):
        token_cache = fallback_runtime.token_cache
        if google_home_filter is None:
            google_home_filter = fallback_runtime.google_home_filter

    if coordinator is not None:
        try:
            await coordinator.async_shutdown()
        except Exception as err:
            _LOGGER.debug("Coordinator async_shutdown raised during removal: %s", err)

    if google_home_filter is not None:
        shutdown = getattr(google_home_filter, "async_shutdown", None)
        if callable(shutdown):
            try:
                result = shutdown()
                if inspect.isawaitable(result):
                    await result
            except Exception as err:
                _LOGGER.debug(
                    "Google Home filter shutdown during removal raised: %s", err
                )

    try:
        await _async_release_shared_fcm(hass)
    except Exception as err:
        _LOGGER.debug("FCM release during async_remove_entry raised: %s", err)

    try:
        owner_index = _ensure_device_owner_index(bucket)
        stale = [cid for cid, eid in list(owner_index.items()) if eid == entry.entry_id]
        for cid in stale:
            owner_index.pop(cid, None)
        if stale:
            _LOGGER.debug(
                "Cleared %d owner-index claim(s) for entry '%s' during removal",
                len(stale),
                entry.entry_id,
            )
    except Exception as err:
        _LOGGER.debug("Owner-index cleanup during removal failed: %s", err)

    entries_bucket = _ensure_entries_bucket(bucket)
    entries_bucket.pop(entry.entry_id, None)

    if isinstance(runtime, RuntimeData):
        runtime.google_home_filter = None
        runtime.fcm_receiver = None
    if isinstance(fallback_runtime, RuntimeData):
        fallback_runtime.google_home_filter = None
        fallback_runtime.fcm_receiver = None

    fallback_cache = _unregister_instance(entry.entry_id)
    if token_cache is None and fallback_cache is not None:
        token_cache = fallback_cache
    elif fallback_cache is not None and fallback_cache is not token_cache:
        close_fallback = getattr(fallback_cache, "close", None)
        if callable(close_fallback):
            try:
                result = close_fallback()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass

    if hasattr(entry, "runtime_data"):
        try:
            setattr(entry, "runtime_data", None)
        except Exception:
            pass

    purge_option = _opt(
        entry, OPT_DELETE_CACHES_ON_REMOVE, DEFAULT_DELETE_CACHES_ON_REMOVE
    )
    try:
        should_purge = cv.boolean(purge_option)
    except Exception:
        should_purge = bool(purge_option)

    issue_id = f"cache_purged_{entry.entry_id}"
    display_name = entry.title or entry.entry_id

    if should_purge:
        removed = False
        if token_cache is not None and hasattr(token_cache, "async_remove_store"):
            close_callable = getattr(token_cache, "close", None)
            if callable(close_callable):
                try:
                    result = close_callable()
                    if inspect.isawaitable(result):
                        await result
                except Exception as err:
                    _LOGGER.debug("Closing TokenCache before removal raised: %s", err)
            try:
                remove_callable = getattr(token_cache, "async_remove_store")
                remove_result = remove_callable()
                if inspect.isawaitable(remove_result):
                    await remove_result
                removed = True
            except Exception as err:
                _LOGGER.warning(
                    "Removing TokenCache store for entry '%s' failed: %s",
                    entry.entry_id,
                    err,
                )
        else:
            store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY}_{entry.entry_id}")
            try:
                remove_callable = getattr(store, "async_remove", None)
                if remove_callable is None:
                    raise AttributeError("Store.async_remove unavailable")
                remove_result = remove_callable()
                if inspect.isawaitable(remove_result):
                    await remove_result
                removed = True
            except Exception as err:
                _LOGGER.warning(
                    "Removing TokenCache store for entry '%s' failed (no cache instance): %s",
                    entry.entry_id,
                    err,
                )

        if removed:
            _LOGGER.info(
                "Removed TokenCache store for entry '%s' (%s).",
                entry.entry_id,
                display_name,
            )
            try:
                ir.async_create_issue(
                    hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.INFO,
                    translation_key="cache_purged",
                    translation_placeholders={"entry_title": display_name},
                )
            except Exception as err:
                _LOGGER.debug("Failed to create cache purge issue: %s", err)
        else:
            try:
                ir.async_delete_issue(hass, DOMAIN, issue_id)
            except Exception as err:
                _LOGGER.debug(
                    "Failed to delete cache purge issue after unsuccessful purge: %s",
                    err,
                )
    else:
        _LOGGER.info(
            "Preserved TokenCache store for entry '%s' (%s); option disabled.",
            entry.entry_id,
            display_name,
        )
        try:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
        except Exception as err:
            _LOGGER.debug(
                "Failed to delete cache purge issue when retention is requested: %s",
                err,
            )

def _get_local_ip_sync() -> str:
    """Best-effort local IP discovery via UDP connect (executor-only)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return cast(str, s.getsockname()[0])
    except OSError:
        return ""
