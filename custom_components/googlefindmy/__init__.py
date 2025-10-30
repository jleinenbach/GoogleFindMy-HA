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

# custom_components/googlefindmy/__init__.py

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict, TypeVar, cast
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from homeassistant.config_entries import ConfigEntry, ConfigEntryState, ConfigSubentry
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

# Token cache (entry-scoped HA Store-backed cache + registry/facade)
from .Auth.token_cache import (
    TokenCache,
    _register_instance,
    _unregister_instance,
)

# Username key normalization
from .Auth.username_provider import username_string

# Shared FCM provider (HA-managed singleton)
from .Auth.fcm_receiver_ha import FcmReceiverHA
from .NovaApi.ExecuteAction.LocateTracker.location_request import (
    register_fcm_receiver_provider as loc_register_fcm_provider,
    unregister_fcm_receiver_provider as loc_unregister_fcm_provider,
)
from .api import (
    register_fcm_receiver_provider as api_register_fcm_provider,
    unregister_fcm_receiver_provider as api_unregister_fcm_provider,
)
from .const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    CONFIG_ENTRY_VERSION,
    DATA_AAS_TOKEN,
    DATA_AUTH_METHOD,
    DATA_SECRET_BUNDLE,
    DEFAULT_DEVICE_POLL_DELAY,
    DEFAULT_LOCATION_POLL_INTERVAL,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DEFAULT_MIN_ACCURACY_THRESHOLD,
    DEFAULT_MIN_POLL_INTERVAL,
    DEFAULT_OPTIONS,
    DOMAIN,
    OPTION_KEYS,
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
    LEGACY_SERVICE_IDENTIFIER,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    coerce_ignored_mapping,
    service_device_identifier,
)
from .coordinator import GoogleFindMyCoordinator
from .email import normalize_email, unique_account_id
from .map_view import GoogleFindMyMapRedirectView, GoogleFindMyMapView
from .discovery import (
    DiscoveryManager,
    async_initialize_discovery_runtime,
    _cloud_discovery_runtime,
    _redact_account_for_log as _redact_account_for_log,
    _trigger_cloud_discovery as _trigger_cloud_discovery,
)

# Eagerly import diagnostics to prevent blocking calls on-demand
from . import diagnostics  # noqa: F401

# Service registration has been moved to a dedicated module (clean separation of concerns)
from .services import async_register_services

if TYPE_CHECKING:
    try:  # pragma: no cover - type-checking fallback for stripped test envs
        from homeassistant.helpers.entity_registry import (
            RegistryEntryDisabler as RegistryEntryDisablerType,
        )
    except ImportError:  # pragma: no cover - Home Assistant test doubles may omit enum
        from enum import StrEnum

        class _RegistryEntryDisablerType(StrEnum):
            """Minimal fallback matching the Home Assistant enum interface."""

            INTEGRATION = "integration"

        RegistryEntryDisablerType = _RegistryEntryDisablerType

    from .NovaApi.ExecuteAction.LocateTracker.location_request import (
        FcmReceiverProtocol as NovaFcmReceiverProtocol,
    )
    from .api import FcmReceiverProtocol as ApiFcmReceiverProtocol
else:
    from typing import Any as RegistryEntryDisablerType

    NovaFcmReceiverProtocol = FcmReceiverHA
    ApiFcmReceiverProtocol = FcmReceiverHA

try:  # pragma: no cover - compatibility shim for stripped test envs
    from homeassistant.helpers.entity_registry import (
        RegistryEntryDisabler as _RegistryEntryDisabler,
    )
except ImportError:  # pragma: no cover - Home Assistant test doubles may omit enum
    from types import SimpleNamespace

    _RegistryEntryDisabler = SimpleNamespace(INTEGRATION="integration")

RegistryEntryDisabler = cast(
    "RegistryEntryDisablerType", _RegistryEntryDisabler
)

# Optional feature: GoogleHomeFilter (guard import to avoid hard dependency)
if TYPE_CHECKING:
    from .google_home_filter import GoogleHomeFilter as GoogleHomeFilterProtocol
else:
    from typing import Any as GoogleHomeFilterProtocol

GoogleHomeFilterFactory: Callable[
    [HomeAssistant, Mapping[str, Any]], GoogleHomeFilterProtocol
] | None

try:
    from .google_home_filter import GoogleHomeFilter as _GoogleHomeFilterClass
except Exception:  # pragma: no cover
    GoogleHomeFilterFactory = None
else:
    GoogleHomeFilterFactory = cast(
        "Callable[[HomeAssistant, Mapping[str, Any]], GoogleHomeFilterProtocol]",
        _GoogleHomeFilterClass,
    )

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


@dataclass(slots=True)
class ConfigEntrySubentryDefinition:
    """Desired state for a managed configuration subentry."""

    key: str
    title: str
    data: Mapping[str, Any]
    subentry_type: str = SUBENTRY_TYPE_TRACKER
    unique_id: str | None = None
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

    def _refresh_from_entry(self) -> None:
        """Populate managed mapping from the config entry."""

        self._managed.clear()
        for subentry in self._entry.subentries.values():
            key = subentry.data.get(self._key_field)
            if isinstance(key, str):
                self._managed[key] = subentry

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
                    if isinstance(resolved, ConfigSubentry):
                        resolved_subentry = resolved
                finally:
                    refreshed_subentry = resolved_subentry or self._entry.subentries.get(
                        subentry.subentry_id, subentry
                    )
                    if refreshed_subentry is None:
                        refreshed_subentry = subentry
                    self._managed[key] = refreshed_subentry

            self._hass.async_create_task(
                _await_visibility_update(),
                name=f"{DOMAIN}.subentry_visibility_refresh",
            )
            return

        refreshed = update_result if isinstance(update_result, ConfigSubentry) else None
        if refreshed is None:
            refreshed = self._entry.subentries.get(subentry.subentry_id, subentry)
        if refreshed is None:
            refreshed = subentry
        # Ensure local view reflects Home Assistant's stored subentry.
        self._managed[key] = refreshed

    async def async_sync(
        self, definitions: Iterable[ConfigEntrySubentryDefinition]
    ) -> None:
        """Ensure subentries match the provided definitions."""

        desired: dict[str, ConfigEntrySubentryDefinition] = {}
        for definition in definitions:
            desired[definition.key] = definition

        for key, definition in desired.items():
            payload = dict(definition.data)
            payload[self._key_field] = key
            unique_id = definition.unique_id or f"{self._entry.entry_id}-{key}"
            subentry_type = definition.subentry_type or self._default_subentry_type
            cleanup = definition.unload

            existing = self._managed.get(key)
            if existing is None:
                new_subentry = ConfigSubentry(
                    data=MappingProxyType(payload),
                    subentry_type=subentry_type,
                    title=definition.title,
                    unique_id=unique_id,
                )
                add_result = self._hass.config_entries.async_add_subentry(
                    self._entry, new_subentry
                )
                resolved_add = await self._await_subentry_result(add_result)

                if isinstance(resolved_add, ConfigSubentry):
                    stored = resolved_add
                else:
                    stored = self._entry.subentries.get(
                        new_subentry.subentry_id, new_subentry
                    )

                self._managed[key] = stored
            else:
                changed = self._hass.config_entries.async_update_subentry(
                    self._entry,
                    existing,
                    data=payload,
                    title=definition.title,
                    unique_id=unique_id,
                )
                resolved_update = await self._await_subentry_result(changed)

                if isinstance(resolved_update, ConfigSubentry):
                    stored_existing = resolved_update
                elif resolved_update:
                    stored_existing = self._entry.subentries.get(
                        existing.subentry_id, existing
                    )
                else:
                    stored_existing = None

                if stored_existing is not None:
                    self._managed[key] = stored_existing

            self._cleanup[key] = cleanup

        stale_keys = [key for key in list(self._managed) if key not in desired]
        for key in stale_keys:
            await self.async_remove(key)

    async def async_remove(self, key: str) -> None:
        """Remove a managed subentry and run its cleanup callback."""

        subentry = self._managed.pop(key, None)
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

        try:
            remove_result = self._hass.config_entries.async_remove_subentry(
                self._entry, subentry.subentry_id
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
    fcm_receiver: FcmReceiverHA | None = None
    google_home_filter: GoogleHomeFilterProtocol | None = None

    @property
    def cache(self) -> TokenCache:
        """Legacy alias for the entry-scoped token cache."""

        return self.token_cache

type MyConfigEntry = ConfigEntry


class GoogleFindMyDomainData(TypedDict, total=False):
    """Typed container describing objects stored under ``hass.data[DOMAIN]``."""

    cloud_scan_results: Mapping[str, Any]
    device_owner_index: dict[str, str]
    discovery_manager: DiscoveryManager
    entries: dict[str, RuntimeData]
    fcm_lock: asyncio.Lock
    fcm_receiver: FcmReceiverHA
    fcm_refcount: int
    fcm_lock_contention_count: int
    initial_setup_complete: bool
    nova_refcount: int
    services_lock: asyncio.Lock
    services_registered: bool
    views_registered: bool


def _domain_data(hass: HomeAssistant) -> GoogleFindMyDomainData:
    """Return the typed domain data bucket, creating it on first access."""

    return cast(GoogleFindMyDomainData, hass.data.setdefault(DOMAIN, {}))


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


def _get_discovery_manager(
    bucket: GoogleFindMyDomainData,
) -> DiscoveryManager | None:
    """Return the discovery manager if already initialized."""

    manager = bucket.get("discovery_manager")
    if isinstance(manager, DiscoveryManager):
        return manager
    return None


def _set_discovery_manager(
    bucket: GoogleFindMyDomainData, manager: DiscoveryManager
) -> None:
    """Store the shared discovery manager."""

    bucket["discovery_manager"] = manager


def _get_fcm_receiver(bucket: GoogleFindMyDomainData) -> FcmReceiverHA | None:
    """Return the cached shared FCM receiver if present."""

    receiver = bucket.get("fcm_receiver")
    if isinstance(receiver, FcmReceiverHA):
        return receiver
    return None


def _set_fcm_receiver(bucket: GoogleFindMyDomainData, receiver: FcmReceiverHA) -> None:
    """Store the shared FCM receiver."""

    bucket["fcm_receiver"] = receiver


def _pop_fcm_receiver(bucket: GoogleFindMyDomainData) -> FcmReceiverHA | None:
    """Remove and return the cached shared FCM receiver."""

    receiver = bucket.pop("fcm_receiver", None)
    if isinstance(receiver, FcmReceiverHA):
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


def _ensure_cloud_scan_results(
    bucket: GoogleFindMyDomainData, results: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Ensure the shared cloud discovery results mapping is stored."""

    existing = bucket.get("cloud_scan_results")
    if isinstance(existing, Mapping):
        return existing
    bucket["cloud_scan_results"] = results
    return results


def _domain_fcm_provider(hass: HomeAssistant) -> FcmReceiverHA:
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
    """Return canonical identifier, stripping entry namespace when applicable."""

    if ":" not in ident:
        return ident

    config_entries: Collection[str] | None = getattr(device, "config_entries", None)
    if not config_entries:
        return ident

    prefix, canonical = ident.split(":", 1)
    if canonical and prefix in config_entries:
        return canonical

    return ident


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

    return tuple(entries_iterable)


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
    if "." in arg:
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
    """Return URL with any 'token' query parameter value redacted for safe logging."""
    try:
        parts = urlsplit(url)
        q = parse_qsl(parts.query, keep_blank_values=True)
        redacted: list[tuple[str, str]] = []
        for k, v in q:
            if k.lower() == "token" and v:
                red_v = (v[:2] + "…" + v[-2:]) if len(v) > 4 else "****"
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
    return entry.state in {
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.SETUP_RETRY,
    }


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
            entry.title,
        )
        hass.config_entries.async_update_entry(entry, options=new_options)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle config entry migrations for legacy Google Find My entries."""

    raw_email, normalized_email = _resolve_entry_email(entry)

    new_data = dict(entry.data)
    if raw_email and new_data.get(CONF_GOOGLE_EMAIL) != raw_email:
        new_data[CONF_GOOGLE_EMAIL] = raw_email

    update_kwargs: dict[str, Any] = {}
    if new_data != entry.data:
        update_kwargs["data"] = new_data

    if raw_email and entry.title != raw_email:
        update_kwargs["title"] = raw_email

    version_update_required = entry.version != CONFIG_ENTRY_VERSION

    conflict: ConfigEntry | None = None
    others = [
        candidate
        for candidate in hass.config_entries.async_entries(DOMAIN)
        if candidate.entry_id != entry.entry_id
    ]

    for other in others:
        other_normalized = _extract_email_from_entry(other)
        if normalized_email and other_normalized == normalized_email:
            conflict = other
            break

    unique_id = unique_account_id(normalized_email)
    current_unique_id = getattr(entry, "unique_id", None)
    if unique_id and current_unique_id != unique_id and not conflict:
        update_kwargs["unique_id"] = unique_id

    if conflict and normalized_email:
        _log_duplicate_and_raise_repair_issue(
            hass,
            entry,
            normalized_email,
            cause="pre_migration_duplicate",
            conflicts=[conflict],
        )

    if version_update_required:
        update_kwargs["version"] = CONFIG_ENTRY_VERSION

    if update_kwargs:
        if conflict and "unique_id" in update_kwargs:
            update_kwargs.pop("unique_id", None)

        try:
            hass.config_entries.async_update_entry(entry, **update_kwargs)
        except ValueError:
            if normalized_email:
                _log_duplicate_and_raise_repair_issue(
                    hass,
                    entry,
                    normalized_email,
                    cause="unique_id_conflict",
                )
            update_kwargs.pop("unique_id", None)
            if update_kwargs:
                hass.config_entries.async_update_entry(entry, **update_kwargs)

    if version_update_required:
        entry.version = CONFIG_ENTRY_VERSION

    if not conflict:
        _clear_duplicate_account_issue(hass, entry)

    await _async_soft_migrate_data_to_options(hass, entry)

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
                entry.title,
                legacy_result.migrated,
                legacy_result.total_candidates,
                len(legacy_result.collisions),
            )
        else:
            current_options["unique_id_migrated"] = True
            options_changed = True
            if legacy_result.total_candidates or legacy_result.migrated:
                _LOGGER.info(
                    "Unique-ID migration complete for '%s': migrated=%d, already_scoped=%d, nonprefix=%d",
                    entry.title,
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
                entry.title,
                subentry_result.updated,
                subentry_result.already_current,
                subentry_result.skipped,
                len(subentry_result.collisions),
            )
        else:
            current_options["unique_id_subentry_migrated"] = True
            options_changed = True
            if subentry_result.updated or subentry_result.already_current:
                _LOGGER.info(
                    "Subentry unique-ID migration complete for '%s': updated=%d, already_current=%d, skipped=%d",
                    entry.title,
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


async def _async_acquire_shared_fcm(hass: HomeAssistant) -> FcmReceiverHA:
    """Get or create the shared FCM receiver for this HA instance.

    Behavior:
        - Creates and initializes the singleton if missing.
        - Registers provider callbacks for API and LocateTracker once.
        - Maintains a reference counter to support multiple entries.
        - NEW: attaches HA context to enable owner-index fallback routing.
    """
    bucket = _domain_data(hass)
    fcm_lock: asyncio.Lock = _ensure_fcm_lock(bucket)
    if fcm_lock.locked():
        contention = bucket.get("fcm_lock_contention_count")
        if not isinstance(contention, int):
            contention = 0
        bucket["fcm_lock_contention_count"] = contention + 1
    async with fcm_lock:
        refcount = _get_fcm_refcount(bucket)
        raw_receiver = cast(object | None, bucket.get("fcm_receiver"))
        fcm = raw_receiver if isinstance(raw_receiver, FcmReceiverHA) else None

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

        if raw_receiver is not None and not isinstance(raw_receiver, FcmReceiverHA):
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
            fcm = FcmReceiverHA()
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
            def provider() -> FcmReceiverHA:
                """Return the shared FCM receiver for integration consumers."""

                return _domain_fcm_provider(hass)

            provider_fn: Callable[[], FcmReceiverHA] = provider
            loc_register_fcm_provider(
                cast(Callable[[], NovaFcmReceiverProtocol], provider_fn)
            )
            api_register_fcm_provider(
                cast(Callable[[], ApiFcmReceiverProtocol], provider_fn)
            )

        new_refcount = refcount + 1
        _set_fcm_refcount(bucket, new_refcount)
        _LOGGER.debug("FCM refcount -> %s", new_refcount)
        return fcm


async def _async_release_shared_fcm(hass: HomeAssistant) -> None:
    """Decrease refcount; stop and unregister provider when it reaches zero."""
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

        if fcm is not None:
            try:
                await fcm.async_stop()
                _LOGGER.info("Shared FCM receiver stopped")
            except Exception as err:
                _LOGGER.warning("Stopping FCM receiver failed: %s", err)


# ------------------------------ Setup / Unload -----------------------------


def _resolve_entry_email(entry: ConfigEntry) -> tuple[str | None, str | None]:
    """Return the raw and normalized e-mail associated with a config entry."""

    email_value = entry.data.get(CONF_GOOGLE_EMAIL)
    raw_email: str | None
    if isinstance(email_value, str) and email_value.strip():
        raw_email = email_value.strip()
    else:
        raw_email = None
        secrets_bundle = entry.data.get(DATA_SECRET_BUNDLE)
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


def _log_duplicate_and_raise_repair_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
    normalized_email: str,
    *,
    cause: str,
    conflicts: Sequence[ConfigEntry] | None = None,
) -> None:
    """Create or refresh a Repair issue for duplicate account configuration."""

    _LOGGER.warning(
        "googlefindmy %s: duplicate account %s detected (%s)",
        entry.entry_id,
        normalized_email,
        cause,
    )
    issue_id = f"duplicate_account_{entry.entry_id}"
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


def _select_authoritative_entry_id(
    entry: ConfigEntry, duplicates: Sequence[ConfigEntry]
) -> str:
    """Return the entry_id that should remain active for a duplicate account."""

    if not duplicates:
        return str(entry.entry_id)
    candidates = [entry, *duplicates]
    # Deterministic ordering by entry_id keeps behaviour predictable across restarts.
    authoritative = min(candidates, key=lambda candidate: candidate.entry_id)
    return str(authoritative.entry_id)


def _ensure_post_migration_consistency(
    hass: HomeAssistant, entry: ConfigEntry
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

    if duplicates and normalized_email:
        _log_duplicate_and_raise_repair_issue(
            hass,
            entry,
            normalized_email,
            cause="setup_duplicate",
            conflicts=duplicates,
        )
    else:
        _clear_duplicate_account_issue(hass, entry)

    authoritative_entry_id = _select_authoritative_entry_id(entry, duplicates)
    should_setup = not duplicates or entry.entry_id == authoritative_entry_id
    return should_setup, normalized_email


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the integration namespace and register global services.

    Rationale:
        Services must be registered from async_setup so they are always available,
        even if no config entry is loaded, which enables frontend validation of
        automations referencing these services.
    """
    bucket = _domain_data(hass)
    runtime = _cloud_discovery_runtime(hass)
    discovery_manager = _get_discovery_manager(bucket)
    if discovery_manager is None:
        discovery_manager = await async_initialize_discovery_runtime(hass)
        _set_discovery_manager(bucket, discovery_manager)
    _ensure_cloud_scan_results(bucket, runtime["results"])
    _ensure_entries_bucket(bucket)  # entry_id -> RuntimeData
    _ensure_device_owner_index(bucket)  # canonical_id -> entry_id (E2.5 scaffold)

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
            }
            await async_register_services(hass, svc_ctx)
            bucket["services_registered"] = True
            _LOGGER.debug("Registered %s services at integration level", DOMAIN)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    """Set up a config entry.

    Order of operations (important):
      1) Multi-entry policy: allow multiple entries; prevent duplicate-account entries.
      2) Initialize and register TokenCache (entry-scoped, no default).
      3) Soft-migrate options and unique_ids; acquire and wire the shared FCM provider.
      4) Seed token cache from entry data (secrets bundle or individual tokens).
      5) Build coordinator, register views, forward platforms.
      6) Schedule initial refresh after HA is fully started.
    """
    # --- Multi-entry policy: allow MA; block duplicate-account (same email) ----
    # Legacy issue cleanup: we no longer block on multiple config entries
    try:
        ir.async_delete_issue(hass, DOMAIN, "multiple_config_entries")
    except Exception:
        pass

    should_setup, normalized_email = _ensure_post_migration_consistency(hass, entry)
    if not should_setup:
        _LOGGER.warning(
            "Skipping setup for %s due to duplicate account %s",
            entry.entry_id,
            normalized_email or "",
        )
        return False

    pm_setup_start = time.monotonic()

    # Distinguish cold start vs. reload
    domain_bucket = _domain_data(hass)
    initial_setup_complete = domain_bucket.get("initial_setup_complete")
    is_reload = (
        bool(initial_setup_complete)
        if isinstance(initial_setup_complete, bool)
        else False
    )
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
        except Exception as err:
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

    subentry_manager = ConfigEntrySubEntryManager(hass, entry)
    coordinator.attach_subentry_manager(subentry_manager)

    # Expose runtime object via the typed container (preferred access pattern)
    runtime_data = RuntimeData(
        coordinator=coordinator,
        token_cache=cache,
        subentry_manager=subentry_manager,
        fcm_receiver=fcm,
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

    # Optional: attach Google Home filter (options-first configuration)
    if GoogleHomeFilterFactory is not None:
        try:
            google_home_filter = GoogleHomeFilterFactory(hass, _effective_config(entry))
            runtime_data.google_home_filter = google_home_filter
            _LOGGER.debug("Initialized Google Home filter (options-first)")
        except Exception as err:
            _LOGGER.debug("GoogleHomeFilter attach skipped due to: %s", err)
    else:
        _LOGGER.debug("GoogleHomeFilter not available; continuing without it")

    tracker_features = sorted(TRACKER_FEATURE_PLATFORMS)
    service_features = sorted(SERVICE_FEATURE_PLATFORMS)
    fcm_push_enabled = runtime_data.fcm_receiver is not None
    has_google_home_filter = runtime_data.google_home_filter is not None
    entry_title = entry.title
    await subentry_manager.async_sync(
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
            ),
            ConfigEntrySubentryDefinition(
                key=SERVICE_SUBENTRY_KEY,
                title=entry_title,
                data={
                    "features": service_features,
                    "fcm_push_enabled": fcm_push_enabled,
                    "has_google_home_filter": has_google_home_filter,
                    "entry_title": entry_title,
                },
                subentry_type=SUBENTRY_TYPE_SERVICE,
                unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
            ),
        ]
    )

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
        hass.http.register_view(GoogleFindMyMapView(hass))
        hass.http.register_view(GoogleFindMyMapRedirectView(hass))
        bucket["views_registered"] = True
        _LOGGER.debug("Registered map views")

    # Forward platforms so RestoreEntity can populate immediately
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Defer the first refresh until HA is fully started
    listener_active = False

    async def _do_first_refresh(_: Any) -> None:
        """Perform the initial coordinator refresh after HA has started."""
        nonlocal listener_active
        listener_active = False
        try:
            if is_reload:
                _LOGGER.info(
                    "Integration reloaded: forcing an immediate device scan window."
                )
                coordinator.force_poll_due()

            await coordinator.async_request_refresh()
            last_update_success = getattr(coordinator, "last_update_success", None)
            if last_update_success is False:
                _LOGGER.warning(
                    "Initial refresh failed; entities will recover on subsequent polls."
                )
            await _async_normalize_device_names(hass)
        except Exception as err:
            _LOGGER.error(
                "Initial refresh raised an unexpected error: %s", err, exc_info=True
            )

    if hass.state == CoreState.running:
        hass.async_create_task(
            _do_first_refresh(None), name="googlefindmy.initial_refresh"
        )
    else:
        unsub = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _do_first_refresh
        )
        listener_active = True

        def _safe_unsub() -> None:
            if listener_active:
                unsub()

        entry.async_on_unload(_safe_unsub)

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

    if canonical_id == "integration" or canonical_id == f"integration_{entry.entry_id}":
        return False

    try:
        runtime = getattr(entry, "runtime_data", None)
        coordinator: GoogleFindMyCoordinator | None = None
        if isinstance(runtime, GoogleFindMyCoordinator):
            coordinator = runtime
        elif runtime is not None:
            coordinator = getattr(runtime, "coordinator", None)
        if not isinstance(coordinator, GoogleFindMyCoordinator):
            runtime_bucket = hass.data.get(DOMAIN, {}).get("entries", {})
            runtime_entry = runtime_bucket.get(entry.entry_id)
            coordinator = getattr(runtime_entry, "coordinator", None)
        if isinstance(coordinator, GoogleFindMyCoordinator):
            coordinator.purge_device(canonical_id)
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


async def async_unload_entry(hass: HomeAssistant, entry: MyConfigEntry) -> bool:
    """Unload a config entry.

    Notes:
        - FCM stop is *signaled* via the unload hook registered during setup.
          The awaited stop and refcount release are handled here to avoid long
          awaits inside `async_on_unload`.
        - TokenCache is explicitly closed here to flush and mark the cache closed.
        - Device owner index cleanup: remove all canonical_id claims for this entry (E2.5).
    """
    runtime_data: RuntimeData | GoogleFindMyCoordinator | None = getattr(
        entry, "runtime_data", None
    )
    subentry_manager: ConfigEntrySubEntryManager | None = None
    entries_bucket: dict[str, RuntimeData] | None = None

    try:
        bucket = _domain_data(hass)
        entries_bucket = _ensure_entries_bucket(bucket)

        stored_runtime = entries_bucket.get(entry.entry_id)
        if not isinstance(runtime_data, RuntimeData) and isinstance(
            stored_runtime, RuntimeData
        ):
            runtime_data = stored_runtime

        coordinator: GoogleFindMyCoordinator | None = None
        if isinstance(runtime_data, GoogleFindMyCoordinator):
            coordinator = runtime_data
        elif isinstance(runtime_data, RuntimeData):
            coordinator = runtime_data.coordinator
            subentry_manager = runtime_data.subentry_manager
        if coordinator is not None:
            await coordinator.async_shutdown()
    except Exception as err:
        _LOGGER.debug("Coordinator async_shutdown raised during unload: %s", err)

    unloaded = bool(await hass.config_entries.async_unload_platforms(entry, PLATFORMS))
    if unloaded:
        if entries_bucket is None:
            entries_bucket = _ensure_entries_bucket(_domain_data(hass))

        if subentry_manager is None:
            stored = entries_bucket.get(entry.entry_id)
            if isinstance(stored, RuntimeData):
                subentry_manager = stored.subentry_manager

        if subentry_manager is not None:
            try:
                await subentry_manager.async_remove_all()
            except Exception as err:
                _LOGGER.debug("Subentry cleanup raised during unload: %s", err)

        removed_runtime = entries_bucket.pop(entry.entry_id, None)

        cache = None
        if isinstance(runtime_data, RuntimeData):
            cache = runtime_data.token_cache
        elif isinstance(removed_runtime, RuntimeData):
            cache = removed_runtime.token_cache

        fallback_cache = _unregister_instance(entry.entry_id)
        if cache is None:
            cache = fallback_cache

        if cache:
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

        if fallback_cache and fallback_cache is not cache:
            try:
                await fallback_cache.close()
            except Exception:
                pass

    try:
        await _async_release_shared_fcm(hass)
    except Exception as err:
        _LOGGER.debug("FCM release during async_unload_entry raised: %s", err)

    # Cleanup owner index (E2.5)
    try:
        bucket = _domain_data(hass)
        owner_index: dict[str, str] = _ensure_device_owner_index(bucket)
        stale = [cid for cid, eid in list(owner_index.items()) if eid == entry.entry_id]
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

    if unloaded and hasattr(entry, "runtime_data"):
        try:
            setattr(entry, "runtime_data", None)
        except Exception:
            pass

    return unloaded


def _get_local_ip_sync() -> str:
    """Best-effort local IP discovery via UDP connect (executor-only)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return cast(str, s.getsockname()[0])
    except OSError:
        return ""
