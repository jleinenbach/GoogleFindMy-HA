"""Device registry operations for GoogleFindMyCoordinator.

This module contains registry-related methods extracted from main.py
during Phase 2 of the refactoring.

Methods moved here:
- _call_device_registry_api: Core registry call with compatibility handling
- _device_registry_kwargs_need_legacy_retry: Legacy kwarg detection
- _device_registry_build_legacy_kwargs: Legacy kwarg translation
- _device_registry_config_subentry_kwarg_name: Subentry kwarg detection
- _device_registry_allows_translation_update: Translation support check
- _reindex_poll_targets_from_device_registry: Rebuild poll target sets
- _extract_our_identifier: Extract device identifier from registry
- _sync_owner_index: Sync hass.data owner index for FCM fallback
- _ensure_device_name_cache: Lazy device-name cache initialization
- _apply_pending_via_updates: Deprecated no-op (backward compat)
- _device_display_name: Get device name without sensitive data
- _entry_id: Get bound ConfigEntry ID
- _config_entry_exists: Check if config entry is registered
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.config_entries import (
    ConfigEntry,
    UnknownEntry,
    UnknownSubEntry,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntry as EntityRegistryEntry

from ..const import (
    DOMAIN,
    INTEGRATION_VERSION,
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
    SERVICE_DEVICE_MANUFACTURER,
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_TRANSLATION_KEY,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    TRACKER_SUBENTRY_KEY,
    service_device_identifier,
)

from .helpers.registry import (
    build_canonical_unique_id as _build_canonical_unique_id_impl,
    build_entity_unique_id_candidates as _build_entity_unique_id_candidates_impl,
    build_legacy_device_registry_kwargs as _build_legacy_kwargs_impl,
    extract_canonical_device_id as _extract_canonical_device_id_impl,
    extract_device_display_name as _extract_display_name_impl,
    extract_service_subentry_ids as _extract_service_subentry_ids_impl,
    has_hub_link as _has_hub_link_impl,
    has_subentry_link as _has_subentry_link_impl,
    match_entity_by_device_id as _match_entity_by_device_id_impl,
    needs_legacy_kwarg_retry as _needs_legacy_retry_impl,
    parse_device_identifier as _parse_identifier_impl,
    should_defer_service_subentry as _should_defer_service_subentry_impl,
)
from .helpers.subentry import (
    sanitize_subentry_identifier as _sanitize_subentry_id_impl,
)

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)


class RegistryOperations:
    """Device registry operations mixin for GoogleFindMyCoordinator.

    This class contains methods that manage device registry entries,
    including creation, updates, and synchronization of HA device registry
    with the Google Find My device list.
    """

    def _call_device_registry_api(
        self: "GoogleFindMyCoordinator",
        call: Callable[..., Any],
        *,
        base_kwargs: Mapping[str, Any] | None = None,
    ) -> Any:
        """Call a device registry API, handling keyword compatibility."""

        kwargs = dict(base_kwargs or {})
        if "config_subentry_id" in kwargs:
            replacement = self._device_registry_config_subentry_kwarg_name(call)
            if replacement is None:
                kwargs.pop("config_subentry_id")
            elif replacement != "config_subentry_id":
                kwargs[replacement] = kwargs.pop("config_subentry_id")

        try:
            return call(**kwargs)
        except TypeError as err:
            if not self._device_registry_kwargs_need_legacy_retry(call, err, kwargs):
                raise

            legacy_kwargs = self._device_registry_build_legacy_kwargs(kwargs)
            _LOGGER.debug(
                "Retrying device registry call %s with legacy keyword arguments after %s",
                getattr(call, "__qualname__", repr(call)),
                err,
            )
            return call(**legacy_kwargs)
        except (UnknownEntry, UnknownSubEntry) as err:
            kwarg_name = self._device_registry_config_subentry_kwarg_name(call)
            if (
                kwarg_name == "add_config_subentry_id"
                or "config_subentry_id" not in kwargs
            ):
                raise
            _LOGGER.debug(
                "Device registry call %s rejected config_subentry_id (%s); retrying without it",
                getattr(call, "__qualname__", repr(call)),
                err,
            )
            fallback_kwargs = dict(kwargs)
            fallback_kwargs.pop("config_subentry_id", None)
            return call(**fallback_kwargs)

    def _device_registry_kwargs_need_legacy_retry(
        self: "GoogleFindMyCoordinator",
        call: Callable[..., Any],
        err: TypeError,
        kwargs: Mapping[str, Any],
    ) -> bool:
        """Return True when ``kwargs`` must be rewritten for legacy cores."""
        kwarg_name = self._device_registry_config_subentry_kwarg_name(call)
        return _needs_legacy_retry_impl(kwarg_name, str(err), kwargs)

    @staticmethod
    def _device_registry_build_legacy_kwargs(
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Translate modern device-registry kwargs to their legacy names."""
        return _build_legacy_kwargs_impl(kwargs)

    def _device_registry_config_subentry_kwarg_name(
        self: "GoogleFindMyCoordinator", call: Callable[..., Any]
    ) -> str | None:
        """Return the config-subentry kwarg name accepted by ``call``.

        Home Assistant 2025.11 renamed the ``async_update_device`` keyword from
        ``config_subentry_id`` to ``add_config_subentry_id``. Earlier versions still
        expect ``config_subentry_id``. This helper inspects the callable signature
        and returns the supported keyword, caching the result for reuse.
        """

        cache_attr = "_device_registry_config_subentry_kwarg_cache"
        cache_obj = getattr(self, cache_attr, None)
        cache: dict[Callable[..., Any], str | None]
        if isinstance(cache_obj, dict):
            cache = cast(dict[Callable[..., Any], str | None], cache_obj)
        else:
            cache = cast(dict[Callable[..., Any], str | None], {})
            setattr(self, cache_attr, cache)

        func = getattr(call, "__func__", call)
        if func in cache:
            return cache[func]

        try:
            signature = inspect.signature(call)
        except (TypeError, ValueError):  # pragma: no cover - defensive fallback
            kwarg_name: str | None = None
        else:
            parameters = signature.parameters
            if "config_subentry_id" in parameters:
                kwarg_name = "config_subentry_id"
            elif "add_config_subentry_id" in parameters:
                kwarg_name = "add_config_subentry_id"
            elif any(
                param.kind is inspect.Parameter.VAR_KEYWORD
                for param in parameters.values()
            ):
                kwarg_name = "config_subentry_id"
            else:
                kwarg_name = None

        cache[func] = kwarg_name
        return kwarg_name

    def _device_registry_allows_translation_update(
        self: "GoogleFindMyCoordinator", dev_reg: Any
    ) -> bool:
        """Return True if the registry accepts translation metadata during updates."""

        cached = getattr(self, "_device_registry_supports_translation_update", None)
        if isinstance(cached, bool):
            return cached

        update_helper = getattr(dev_reg, "async_update_device", None)
        supports_translation = False
        if callable(update_helper):
            try:
                signature = inspect.signature(update_helper)
            except (TypeError, ValueError):
                supports_translation = False
            else:
                params = signature.parameters
                supports_translation = (
                    "translation_key" in params and "translation_placeholders" in params
                )

        setattr(
            self, "_device_registry_supports_translation_update", supports_translation
        )
        return supports_translation

    @callback
    def _reindex_poll_targets_from_device_registry(
        self: "GoogleFindMyCoordinator",
    ) -> None:
        """Rebuild internal poll target sets from registries (fast, robust, diagnostics-aware).

        Semantics:
        - Consider ONLY devices that belong to THIS config entry (no global scan).
        - A device is "present" if we can extract a valid (DOMAIN, identifier).
        - A device is "enabled for polling" if there is at least one ENABLED
          `device_tracker` entity for our domain on that device AND the device
          itself is not disabled. This preserves the entities-driven polling
          selection and reduces UI churning.

        Multi-account safety:
        - Uses entry-scoped identifiers in the Device Registry:
              (DOMAIN, f"{entry_id}:{device_id}")
          and gracefully accepts legacy identifiers `(DOMAIN, device_id)`.
        """
        dev_reg = dr.async_get(self.hass)
        ent_reg = er.async_get(self.hass)
        entry_id = self._entry_id()

        if not entry_id:
            self._devices_with_entry = set()
            self._enabled_poll_device_ids = set()
            _LOGGER.debug("Skipping DR reindex: no config_entry bound yet")
            return

        # Limit to our integration's devices/entities: avoids interference & improves performance.
        devices_for_entry = dr.async_entries_for_config_entry(dev_reg, entry_id)
        entities_for_entry = er.async_entries_for_config_entry(ent_reg, entry_id)

        present: set[str] = set()
        enabled: set[str] = set()

        # Map device_id -> has_enabled_tracker_entity
        has_enabled_tracker: dict[str, bool] = {}
        for ent in entities_for_entry:
            # We only care about our domain and enabled entities
            if ent.platform != DOMAIN or ent.disabled_by is not None:
                continue
            # Only trackers drive polling
            if ent.domain == "device_tracker" and ent.device_id:
                has_enabled_tracker[ent.device_id] = True

        for dev in devices_for_entry:
            ident = self._extract_our_identifier(dev)
            if not ident:
                continue
            present.add(ident)
            if dev.id in has_enabled_tracker and dev.disabled_by is None:
                enabled.add(ident)

        self._devices_with_entry = present
        self._enabled_poll_device_ids = enabled

        # Update subentry metadata since enabled/present sets may affect visibility
        self._refresh_subentry_index()

        _LOGGER.debug(
            "Reindexed targets for entry %s: %d present / %d enabled (entities-driven)",
            entry_id,
            len(present),
            len(enabled),
        )
        self._schedule_eid_resolver_refresh()

    def _extract_our_identifier(
        self: "GoogleFindMyCoordinator", device: dr.DeviceEntry
    ) -> str | None:
        """Return the first valid (DOMAIN, identifier) from a device, else None.

        Multi-account compatibility:
        - Since 2025.5+ we use **entry-scoped device identifiers** in the Device Registry
          to guarantee global uniqueness across multiple accounts:
              (DOMAIN, f\"{entry_id}:{device_id}\")
        - For backward compatibility we also recognize legacy identifiers:
              (DOMAIN, device_id)

        This helper:
        * Extracts our identifier
        * If it has the namespaced form, it returns the **raw device_id** part
          (the coordinator uses canonical device IDs internally).
        * If malformed tuples are encountered, it logs once and records a diagnostics warning.
        """
        entry_id = self._entry_id()
        for item in device.identifiers:
            result = _parse_identifier_impl(
                item,
                DOMAIN,
                entry_id,
                SERVICE_DEVICE_IDENTIFIER_PREFIX,
                LEGACY_SERVICE_IDENTIFIER,
            )
            if result is not None:
                return result
        return None

    def _sync_owner_index(
        self: "GoogleFindMyCoordinator", devices: list[dict[str, Any]] | None
    ) -> None:
        """Sync hass.data owner index for this entry (FCM fallback support)."""
        hass = getattr(self, "hass", None)
        entry_id = self._entry_id()
        if hass is None or not entry_id:
            return

        try:
            bucket = hass.data.setdefault(DOMAIN, {})
            owner_index: dict[str, str] = bucket.setdefault("device_owner_index", {})
        except Exception as err:  # noqa: BLE001 - defensive guard
            _LOGGER.debug(
                "[entry=%s] Owner-index sync skipped: %s",
                entry_id,
                err,
            )
            return

        seen: set[str] = set()
        for device in devices or []:
            canonical = (
                device.get("canonicalId")
                or device.get("canonical_id")
                or device.get("id")
                or device.get("device_id")
            )
            if canonical is None:
                continue
            if not isinstance(canonical, str):
                canonical = str(canonical)
            canonical = canonical.strip()
            if not canonical:
                continue

            # Do not overwrite an existing routing entry from another account.
            # Shared devices may appear under multiple entries; keep the first
            # registration so FCM messages route to the primary owner instead
            # of the most recently loaded account.
            if canonical not in owner_index or owner_index[canonical] == entry_id:
                owner_index[canonical] = entry_id
                seen.add(canonical)

        if owner_index:
            stale = [
                cid
                for cid, eid in list(owner_index.items())
                if eid == entry_id and cid not in seen
            ]
            for cid in stale:
                owner_index.pop(cid, None)
            if stale:
                _LOGGER.debug(
                    "[entry=%s] Pruned %d stale owner-index entries",
                    entry_id,
                    len(stale),
                )

    def _ensure_device_name_cache(
        self: "GoogleFindMyCoordinator",
    ) -> dict[str, str]:
        """Return the lazily initialized device-name cache."""
        cache = getattr(self, "_device_names", None)
        if cache is None:
            cache = {}
            setattr(self, "_device_names", cache)
        return cache

    def _apply_pending_via_updates(self: "GoogleFindMyCoordinator") -> None:
        """Deprecated no-op retained for backward compatibility."""
        # Tracker devices no longer link to the service device via ``via_device``.
        # Keep the method defined to avoid AttributeError in case third-party
        # callers relied on the old behavior, but return immediately.
        return

    def _device_display_name(
        self: "GoogleFindMyCoordinator", dev: dr.DeviceEntry, fallback: str
    ) -> str:
        """Return the best human-friendly device name without sensitive data."""
        return _extract_display_name_impl(dev.name_by_user, dev.name, fallback)

    def _entry_id(self: "GoogleFindMyCoordinator") -> str | None:
        """Small helper to read the bound ConfigEntry ID (None at very early startup)."""
        entry = getattr(self, "config_entry", None)
        return getattr(entry, "entry_id", None)

    def _config_entry_exists(
        self: "GoogleFindMyCoordinator", entry_id: str | None = None
    ) -> bool:
        """Return True when the coordinator's entry is still registered."""
        hass = getattr(self, "hass", None)
        config_entries = getattr(hass, "config_entries", None)
        if entry_id is None:
            entry_id = self._entry_id()

        if entry_id is None:
            return False

        getter = getattr(config_entries, "async_get_entry", None)
        if callable(getter):
            try:
                return getter(entry_id) is not None
            except Exception:  # pragma: no cover - defensive guard
                return True

        return True

    def _redact_text(
        self: "GoogleFindMyCoordinator", value: str | None, max_len: int = 120
    ) -> str:
        """Return a short, redacted string variant suitable for logs/diagnostics."""
        if not value:
            return ""
        s = str(value)
        return s if len(s) <= max_len else (s[:max_len] + "…")

    def _ensure_service_device_exists(
        self: "GoogleFindMyCoordinator", entry: ConfigEntry | None = None
    ) -> None:
        """Idempotently create/update the per-entry 'service device' in the device registry.

        This keeps diagnostic entities (e.g. polling/auth-status) grouped under a stable
        integration-level device. Safe to call multiple times.

        Subentry fallback rules and the rationale for recording the service
        ``config_subentry_id`` are documented in ``docs/CONFIG_SUBENTRIES_HANDBOOK.md``;
        consult the handbook before changing identifier selection to avoid regressing
        tracker/service separation.
        """
        # Resolve hass
        hass = getattr(self, "hass", None)
        if hass is None:
            return

        # Resolve ConfigEntry (works with either .entry or .config_entry on the coordinator)
        entry = entry or getattr(self, "entry", None) or self.config_entry
        if entry is None:
            _LOGGER.debug(
                "Service-device ensure skipped: ConfigEntry not available on coordinator."
            )
            return

        entry_id = getattr(entry, "entry_id", None)

        # Refresh subentry metadata to obtain the current service subentry context.
        try:
            self._refresh_subentry_index(skip_manager_update=True, skip_repair=True)
        except Exception:  # pragma: no cover - defensive guard
            pass

        service_meta = self._subentry_metadata.get(SERVICE_SUBENTRY_KEY)

        def _normalize_subentry_id(value: Any) -> str | None:
            return _sanitize_subentry_id_impl(value)

        entry_service_subentry_id = _normalize_subentry_id(
            getattr(entry, "service_subentry_id", None)
        )

        entry_subentries = getattr(entry, "subentries", None)
        service_subentry_ids = _extract_service_subentry_ids_impl(
            entry_subentries,
            entry_service_subentry_id,
            SUBENTRY_TYPE_SERVICE,
            SERVICE_SUBENTRY_KEY,
        )

        def _is_real_service_subentry(candidate: Any) -> str | None:
            """Return candidate when it matches a confirmed service subentry.

            When the config entry lacks recorded subentries (for example, after
            provisional creation), fall back to the coordinator's metadata so the
            service device still records a stable ``config_subentry_id``.
            """

            normalized_candidate = _normalize_subentry_id(candidate)
            if normalized_candidate is None:
                return None

            if entry_service_subentry_id is not None:
                if normalized_candidate != entry_service_subentry_id:
                    return None
                if (
                    service_subentry_ids
                    and normalized_candidate not in service_subentry_ids
                ):
                    return None
                return normalized_candidate

            if service_subentry_ids and normalized_candidate in service_subentry_ids:
                return normalized_candidate

            if not service_subentry_ids:
                return normalized_candidate

            return None

        service_config_subentry_id = None
        meta_identifier: Any | None = None
        if service_meta is not None:
            meta_identifier = getattr(service_meta, "config_subentry_id", None)
        for candidate in (meta_identifier, entry_service_subentry_id):
            resolved = _is_real_service_subentry(candidate)
            if resolved is not None:
                service_config_subentry_id = resolved
                break

        current_subentries = getattr(entry, "subentries", None)
        if _should_defer_service_subentry_impl(
            service_config_subentry_id,
            current_subentries,
            entry_id,
            SERVICE_SUBENTRY_KEY,
        ):
            _LOGGER.debug(
                "[%s] Deferring unknown service config_subentry_id %s until registry catches up",
                entry.entry_id,
                service_config_subentry_id,
            )
            service_config_subentry_id = None
        elif (
            service_config_subentry_id is not None
            and isinstance(current_subentries, Mapping)
            and service_config_subentry_id not in current_subentries
        ):
            # Log when using stable default (not deferred but also not in subentries)
            _LOGGER.debug(
                "[%s] Applying stable default service config_subentry_id %s (registry not ready)",
                entry.entry_id,
                service_config_subentry_id,
            )

        service_subentry_identifier: tuple[str, str] | None = None
        if service_config_subentry_id is not None:
            service_subentry_identifier = (
                DOMAIN,
                f"{entry.entry_id}:{service_config_subentry_id}:service",
            )

        setattr(
            self,
            "_service_device_identifier",
            service_device_identifier(entry.entry_id),
        )

        previous_service_identifier_sentinel = object()
        previous_service_identifier = getattr(
            self,
            "_service_device_last_subentry_identifier",
            previous_service_identifier_sentinel,
        )

        # Fast-path: already ensured in this runtime and the service subentry
        # context has not changed.
        if (
            getattr(self, "_service_device_ready", False)
            and getattr(self, "_service_device_id", None)
            and previous_service_identifier is not previous_service_identifier_sentinel
            and service_subentry_identifier is not None
            and previous_service_identifier == service_subentry_identifier
        ):
            self._apply_pending_via_updates()
            return

        dev_reg = dr.async_get(hass)
        if not hasattr(dev_reg, "async_get_or_create") or not hasattr(
            dev_reg, "async_update_device"
        ):
            _LOGGER.debug(
                "Service-device ensure skipped: registry stub missing create/update APIs."
            )
            return
        identifiers: set[tuple[str, str]] = {
            service_device_identifier(entry.entry_id)
        }  # {(DOMAIN, f"integration_<entry_id>")}
        if service_subentry_identifier is not None:
            identifiers.add(service_subentry_identifier)

        def _service_entry_links(device: Any) -> set[str | None]:
            """Return the set of subentry identifiers linked to ``entry``."""

            if not entry_id:
                return set()

            mapping_obj = getattr(device, "config_entries_subentries", None)
            normalized: set[str | None] = set()
            if isinstance(mapping_obj, Mapping):
                raw_links = mapping_obj.get(entry_id)
                if isinstance(raw_links, str):
                    normalized.add(raw_links)
                elif isinstance(raw_links, Iterable) and not isinstance(
                    raw_links, (str, bytes)
                ):
                    for candidate in raw_links:
                        if isinstance(candidate, str):
                            normalized.add(candidate)
                        elif candidate is None:
                            normalized.add(None)
                elif raw_links is None and entry_id in mapping_obj:
                    normalized.add(None)

            if not normalized:
                fallback = getattr(device, "config_subentry_id", None)
                if isinstance(fallback, str):
                    normalized.add(fallback)
                elif fallback is None and isinstance(
                    getattr(device, "config_entries", None), Iterable
                ):
                    for candidate_entry_id in cast(
                        Iterable[Any], getattr(device, "config_entries", ())
                    ):
                        if (
                            isinstance(candidate_entry_id, str)
                            and candidate_entry_id == entry_id
                        ):
                            normalized.add(None)
                            break

            return normalized

        def _service_has_service_link(device: Any) -> bool:
            if service_config_subentry_id is None:
                return False
            links = _service_entry_links(device)
            return _has_subentry_link_impl(links, service_config_subentry_id)

        def _service_has_hub_link(device: Any) -> bool:
            links = _service_entry_links(device)
            return _has_hub_link_impl(links)

        def _detach_service_hub_link(device: Any) -> Any:
            update_call = getattr(dev_reg, "async_update_device", None)
            if not callable(update_call) or not entry_id:
                return device
            device_id = getattr(device, "id", None)
            if not isinstance(device_id, str) or not device_id:
                return device
            self._call_device_registry_api(
                update_call,
                base_kwargs={
                    "device_id": device_id,
                    "remove_config_entry_id": entry_id,
                    "remove_config_subentry_id": None,
                },
            )
            return _refresh_service_device_entry(device)

        get_device = getattr(dev_reg, "async_get_device", None)
        device = None
        if callable(get_device):
            try:
                device = get_device(identifiers=identifiers)
            except TypeError:
                device = None

        def _refresh_service_device_entry(candidate: Any) -> Any:
            """Return a fresh copy of the service device entry when possible."""

            if candidate is None:
                return None

            getter = getattr(dev_reg, "async_get", None)
            device_id = getattr(candidate, "id", None)
            if not callable(getter) or not isinstance(device_id, str) or not device_id:
                return candidate

            try:
                refreshed = getter(device_id)
            except TypeError:
                return candidate

            return candidate if refreshed is None else refreshed

        if (
            device is not None
            and service_config_subentry_id is not None
            and getattr(device, "config_subentry_id", None)
            != service_config_subentry_id
        ):
            device_id = getattr(device, "id", None)
            if isinstance(device_id, str) and device_id:
                _LOGGER.debug(
                    "[%s] Healing service device: correcting config_subentry_id from %s to %s",
                    entry.entry_id,
                    getattr(device, "config_subentry_id", None),
                    service_config_subentry_id,
                )
                healed = self._call_device_registry_api(
                    dev_reg.async_update_device,
                    base_kwargs={
                        "device_id": device_id,
                        "config_subentry_id": service_config_subentry_id,
                        "add_config_entry_id": entry.entry_id,
                    },
                )
                device = _refresh_service_device_entry(healed or device)
                if device is None:
                    _LOGGER.error("[%s] Failed to heal service device", entry.entry_id)
                    raise HomeAssistantError("Failed to heal service device")
            else:
                _LOGGER.debug(
                    "[%s] Service device missing identifier; unable to heal config_subentry_id",
                    entry.entry_id,
                )

        existing_name: str | None = None
        existing_user_name: str | None = None
        has_user_name = False
        if device is not None:
            existing_name = getattr(device, "name", None)
            existing_user_name = getattr(device, "name_by_user", None)
            if isinstance(existing_user_name, str) and existing_user_name.strip():
                has_user_name = True

        entry_title = getattr(entry, "title", None)
        sanitized_entry_title = (
            entry_title.strip()
            if isinstance(entry_title, str) and entry_title.strip()
            else None
        )
        service_device_name = existing_name or sanitized_entry_title

        _LOGGER.debug(
            "Service device registry pre-ensure (entry=%s): name=%s, name_by_user=%s",
            entry.entry_id,
            self._redact_text(existing_name),
            self._redact_text(existing_user_name),
        )

        if device is None:
            create_kwargs: dict[str, Any] = {
                "config_entry_id": entry.entry_id,
                "identifiers": identifiers,
                "manufacturer": SERVICE_DEVICE_MANUFACTURER,
                "model": SERVICE_DEVICE_MODEL,
                "sw_version": INTEGRATION_VERSION,
                "entry_type": dr.DeviceEntryType.SERVICE,
                "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
            }
            if service_device_name:
                create_kwargs["name"] = service_device_name
            create_kwargs["translation_key"] = SERVICE_DEVICE_TRANSLATION_KEY
            create_kwargs["translation_placeholders"] = {}
            if service_config_subentry_id is not None:
                create_kwargs["config_subentry_id"] = service_config_subentry_id

            device = self._call_device_registry_api(
                dev_reg.async_get_or_create,
                base_kwargs=create_kwargs,
            )
            device = _refresh_service_device_entry(device)
            _LOGGER.debug(
                "Created Google Find My service device for entry %s (device_id=%s)",
                entry.entry_id,
                getattr(device, "id", None),
            )
        else:
            # Keep metadata fresh if it drifted (rare)
            raw_device_identifiers = getattr(device, "identifiers", set()) or set()
            device_identifiers = set(raw_device_identifiers)
            identifiers_to_apply = set(identifiers)
            extraneous_service_identifiers: set[tuple[Any, ...]] = set()
            for existing in list(device_identifiers):
                if (
                    isinstance(existing, tuple)
                    and len(existing) == 2
                    and existing[0] == DOMAIN
                    and isinstance(existing[1], str)
                    and existing[1].endswith(":service")
                    and existing not in identifiers_to_apply
                ):
                    extraneous_service_identifiers.add(existing)

            missing_identifiers = identifiers_to_apply - device_identifiers
            needs_identifier_sync = bool(
                missing_identifiers or extraneous_service_identifiers
            )
            current_service_links = {
                candidate
                for candidate in _service_entry_links(device)
                if isinstance(candidate, str)
            }

            dev_translation_key = getattr(device, "translation_key", None)
            dev_translation_placeholders = getattr(
                device, "translation_placeholders", None
            )
            dev_config_subentry_id = getattr(device, "config_subentry_id", None)
            should_remove_service_link = service_config_subentry_id is None and bool(
                current_service_links
            )
            should_add_hub_link = (
                service_config_subentry_id is None
                and not _service_has_hub_link(device)
                and bool(entry_id)
            )

            translation_refresh_required = (
                dev_translation_key != SERVICE_DEVICE_TRANSLATION_KEY
                or (dev_translation_placeholders or {}) != {}
            )
            translation_update_supported = (
                translation_refresh_required
                and self._device_registry_allows_translation_update(dev_reg)
            )

            needs_name_refresh = (
                service_device_name is not None
                and service_device_name != existing_name
                and not has_user_name
            )

            needs_update = (
                device.manufacturer != SERVICE_DEVICE_MANUFACTURER
                or device.model != SERVICE_DEVICE_MODEL
                or device.sw_version != INTEGRATION_VERSION
                or device.entry_type != dr.DeviceEntryType.SERVICE
                or dev_config_subentry_id != service_config_subentry_id
                or translation_refresh_required
                or needs_name_refresh
                or needs_identifier_sync
                or should_remove_service_link
                or should_add_hub_link
            )
            if needs_update:
                update_kwargs: dict[str, Any] = {
                    "device_id": device.id,
                    "manufacturer": SERVICE_DEVICE_MANUFACTURER,
                    "model": SERVICE_DEVICE_MODEL,
                    "sw_version": INTEGRATION_VERSION,
                    "entry_type": dr.DeviceEntryType.SERVICE,
                    "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
                }
                if service_config_subentry_id is not None:
                    update_kwargs["config_subentry_id"] = service_config_subentry_id
                if needs_identifier_sync:
                    new_identifiers = (
                        device_identifiers - extraneous_service_identifiers
                    ) | identifiers_to_apply
                    update_kwargs["new_identifiers"] = new_identifiers
                if entry_id and (
                    service_config_subentry_id is not None
                    or should_remove_service_link
                    or should_add_hub_link
                ):
                    update_kwargs["add_config_entry_id"] = entry.entry_id
                    if service_config_subentry_id is not None:
                        update_kwargs["add_config_subentry_id"] = (
                            service_config_subentry_id
                        )
                if should_remove_service_link and entry_id:
                    update_kwargs["remove_config_entry_id"] = entry.entry_id
                    removal_id: str | None = None
                    if current_service_links:
                        removal_id = next(iter(current_service_links))
                    elif (
                        isinstance(dev_config_subentry_id, str)
                        and dev_config_subentry_id.strip()
                    ):
                        removal_id = dev_config_subentry_id.strip()
                    update_kwargs["remove_config_subentry_id"] = removal_id
                if needs_name_refresh and service_device_name:
                    update_kwargs["name"] = service_device_name

                call_kwargs = dict(update_kwargs)
                if translation_update_supported:
                    call_kwargs["translation_key"] = SERVICE_DEVICE_TRANSLATION_KEY
                    call_kwargs["translation_placeholders"] = {}

                try:
                    self._call_device_registry_api(
                        dev_reg.async_update_device, base_kwargs=call_kwargs
                    )
                except TypeError as err:
                    if translation_update_supported:
                        setattr(
                            self,
                            "_device_registry_supports_translation_update",
                            False,
                        )
                        translation_update_supported = False
                        self._call_device_registry_api(
                            dev_reg.async_update_device, base_kwargs=update_kwargs
                        )
                    else:  # pragma: no cover - propagate unexpected contract errors
                        raise err
                device = _refresh_service_device_entry(device)
                if translation_refresh_required and not translation_update_supported:
                    translation_kwargs: dict[str, Any] = {
                        "config_entry_id": entry.entry_id,
                        "identifiers": identifiers,
                        "manufacturer": SERVICE_DEVICE_MANUFACTURER,
                        "model": SERVICE_DEVICE_MODEL,
                        "sw_version": INTEGRATION_VERSION,
                        "entry_type": dr.DeviceEntryType.SERVICE,
                        "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
                        "translation_key": SERVICE_DEVICE_TRANSLATION_KEY,
                        "translation_placeholders": {},
                    }
                    if service_config_subentry_id is not None:
                        translation_kwargs["config_subentry_id"] = (
                            service_config_subentry_id
                        )
                    if needs_name_refresh and service_device_name:
                        translation_kwargs["name"] = service_device_name
                    device = self._call_device_registry_api(
                        dev_reg.async_get_or_create,
                        base_kwargs=translation_kwargs,
                    )
                    device = _refresh_service_device_entry(device)
                    _LOGGER.debug(
                        "Backfilled service device translation metadata using get_or_create for entry %s",
                        entry.entry_id,
                    )
                _LOGGER.debug(
                    "Updated Google Find My service device metadata for entry %s",
                    entry.entry_id,
                )

        # Book-keeping for quick re-entrance
        self._service_device_ready = True
        self._service_device_id = getattr(device, "id", None)
        setattr(
            self,
            "_service_device_last_subentry_identifier",
            service_subentry_identifier,
        )
        setattr(
            self,
            "_service_device_last_config_subentry_id",
            service_config_subentry_id,
        )

        if device is not None:
            links = _service_entry_links(device)
            has_hub_link = None in links
            if has_hub_link and service_config_subentry_id is not None:
                _LOGGER.info(
                    "[%s] Removing redundant hub link from service device %s",
                    entry.entry_id,
                    getattr(device, "id", "<unknown>"),
                )
                device = _detach_service_hub_link(device)
                self._service_device_id = getattr(device, "id", None)

        if device is not None:
            _LOGGER.debug(
                "Service device registry post-ensure (entry=%s): name=%s, name_by_user=%s",
                entry.entry_id,
                self._redact_text(getattr(device, "name", None)),
                self._redact_text(getattr(device, "name_by_user", None)),
            )

        # Backfill any end devices that were created before the service device was known
        self._apply_pending_via_updates()

    # Optional back-compat alias (some callers may use the public-style name)
    ensure_service_device_exists = _ensure_service_device_exists

    def _find_tracker_entity_entry(
        self: "GoogleFindMyCoordinator", device_id: str
    ) -> EntityRegistryEntry | None:
        """Return the registry entry for a tracker and migrate legacy unique IDs.

        Uses Phase 12 helpers for identifier extraction and unique_id generation.
        """
        ent_reg = er.async_get(self.hass)
        device_reg = dr.async_get(self.hass)
        entry_id = self._entry_id()

        registry_device = (
            device_reg.async_get(device_id) if device_reg is not None else None
        )
        canonical_device_id = device_id
        registry_identifier: str | None = None

        # Use helper to extract canonical device ID from identifiers
        if registry_device is not None:
            identifiers = getattr(registry_device, "identifiers", None)
            registry_identifier = _extract_canonical_device_id_impl(
                identifiers,
                DOMAIN,
                entry_id=entry_id,
                service_prefix=f"{SERVICE_DEVICE_IDENTIFIER_PREFIX}:",
            )
            if registry_identifier:
                canonical_device_id = registry_identifier
                if canonical_device_id != device_id:
                    _LOGGER.debug(
                        "Tracker entity lookup remapped registry_id=%s to canonical_id=%s",
                        device_id,
                        canonical_device_id,
                    )
            else:
                _LOGGER.debug(
                    "Tracker entity lookup found device %s but no matching identifier in %s",
                    device_id,
                    identifiers,
                )
        else:
            _LOGGER.debug(
                "Tracker entity lookup could not find device registry entry for id=%s",
                device_id,
            )

        device_label = (
            self.get_device_display_name(canonical_device_id) or canonical_device_id
        )

        entities_container = getattr(ent_reg, "entities", None)
        ent_registry_values: Sequence[Any] = ()
        if entities_container is not None:
            try:
                ent_registry_values = list(entities_container.values())
            except Exception:  # noqa: BLE001 - best-effort compatibility
                ent_registry_values = ()

        canonical_unique_id: str | None = None
        tracker_subentry_identifier: str | None = None
        tracker_subentry_key: str = TRACKER_SUBENTRY_KEY
        if entry_id:
            tracker_meta: Any | None = None
            meta_getter = getattr(self, "get_subentry_metadata", None)
            if callable(meta_getter):
                try:
                    tracker_meta = meta_getter(feature="device_tracker")
                except TypeError:
                    tracker_meta = None
                except AttributeError:
                    tracker_meta = None
            if tracker_meta is not None:
                candidate_key = getattr(tracker_meta, "key", None)
                if isinstance(candidate_key, str) and candidate_key.strip():
                    tracker_subentry_key = candidate_key.strip()

            identifier_getter = getattr(self, "stable_subentry_identifier", None)
            if callable(identifier_getter):
                try:
                    tracker_subentry_identifier = identifier_getter(
                        key=tracker_subentry_key,
                        feature="device_tracker",
                    )
                except TypeError:
                    tracker_subentry_identifier = identifier_getter(
                        key=tracker_subentry_key
                    )
                except Exception:  # noqa: BLE001 - defensive for legacy coordinators
                    tracker_subentry_identifier = None
            if (
                not isinstance(tracker_subentry_identifier, str)
                or not tracker_subentry_identifier.strip()
            ):
                tracker_subentry_identifier = tracker_subentry_key

            # Use helper to build canonical unique_id
            canonical_unique_id = _build_canonical_unique_id_impl(
                entry_id, tracker_subentry_identifier, canonical_device_id
            )

        def _get_entry_for_unique_id(
            unique_id: str,
        ) -> EntityRegistryEntry | None:
            """Return the registry entry for a given unique_id if it exists."""

            if not unique_id:
                return None

            try:
                entity_id = ent_reg.async_get_entity_id(
                    DEVICE_TRACKER_DOMAIN,
                    DOMAIN,
                    unique_id,
                )
            except TypeError:
                entity_id = None

            if not entity_id:
                return None

            entry: EntityRegistryEntry | None = None
            getter = getattr(ent_reg, "async_get", None)
            if callable(getter):
                try:
                    entry = getter(entity_id)
                except TypeError:
                    entry = None

            if entry is None and ent_registry_values:
                for candidate in ent_registry_values:
                    if getattr(candidate, "entity_id", None) == entity_id:
                        entry = candidate
                        break

            if entry is None:
                entry = SimpleNamespace(
                    entity_id=entity_id,
                    unique_id=unique_id,
                    domain=DEVICE_TRACKER_DOMAIN,
                    platform=DOMAIN,
                    config_entry_id=entry_id,
                )

            return cast("EntityRegistryEntry", entry)

        if canonical_unique_id:
            entry = _get_entry_for_unique_id(canonical_unique_id)
            if entry is not None:
                _LOGGER.debug(
                    "Tracker registry matched canonical unique_id=%s for device '%s' (entity_id=%s)",
                    canonical_unique_id,
                    device_label,
                    entry.entity_id,
                )
                return entry

        # Use helper to build candidate unique_ids
        candidate_unique_ids = _build_entity_unique_id_candidates_impl(
            canonical_device_id,
            entry_id,
            tracker_subentry_identifier,
            DOMAIN,
            subentry_key=tracker_subentry_key if tracker_subentry_key != tracker_subentry_identifier else None,
        )

        for unique_id in candidate_unique_ids:
            entry = _get_entry_for_unique_id(unique_id)
            if entry is None:
                continue

            if canonical_unique_id and entry.unique_id != canonical_unique_id:
                _LOGGER.info(
                    "Migrating tracker entity %s for device '%s' from legacy unique_id=%s to canonical unique_id=%s",
                    entry.entity_id,
                    device_label,
                    entry.unique_id,
                    canonical_unique_id,
                )
                try:
                    update_entity = getattr(ent_reg, "async_update_entity", None)
                    if callable(update_entity):
                        update_entity(
                            entry.entity_id,
                            new_unique_id=canonical_unique_id,
                        )
                        migrated = _get_entry_for_unique_id(canonical_unique_id)
                        if migrated is not None:
                            return migrated
                    else:
                        _LOGGER.debug(
                            "Entity registry for entry %s lacks async_update_entity; skipping canonical migration",
                            entry_id,
                        )
                        return entry
                except ValueError as err:
                    _LOGGER.error(
                        "Failed to migrate tracker entity %s to canonical unique_id=%s: %s",
                        entry.entity_id,
                        canonical_unique_id,
                        err,
                    )
                    return entry

            return entry

        for entry in ent_registry_values:
            # Use helper for fallback entity matching
            if not _match_entity_by_device_id_impl(
                getattr(entry, "unique_id", ""),
                getattr(entry, "config_entry_id", None),
                canonical_device_id,
                entry_id,
                domain=DEVICE_TRACKER_DOMAIN,
                platform=DOMAIN,
                entity_domain=getattr(entry, "domain", ""),
                entity_platform=getattr(entry, "platform", ""),
            ):
                continue
            unique_id = getattr(entry, "unique_id", "")

            if canonical_unique_id and unique_id != canonical_unique_id:
                _LOGGER.info(
                    "Migrating tracker entity %s for device '%s' from heuristic unique_id=%s to canonical unique_id=%s",
                    entry.entity_id,
                    device_label,
                    unique_id,
                    canonical_unique_id,
                )
                try:
                    update_entity = getattr(ent_reg, "async_update_entity", None)
                    if callable(update_entity):
                        update_entity(
                            entry.entity_id,
                            new_unique_id=canonical_unique_id,
                        )
                        migrated = _get_entry_for_unique_id(canonical_unique_id)
                        if migrated is not None:
                            return migrated
                    else:
                        _LOGGER.debug(
                            "Entity registry for entry %s lacks async_update_entity; skipping canonical migration",
                            entry_id,
                        )
                        return entry
                except ValueError as err:
                    _LOGGER.error(
                        "Failed to migrate heuristic tracker entity %s to canonical unique_id=%s: %s",
                        entry.entity_id,
                        canonical_unique_id,
                        err,
                    )
                    return entry

            _LOGGER.debug(
                "Tracker registry fallback matched entity_id=%s (unique_id=%s) for device '%s'",
                entry.entity_id,
                unique_id,
                device_label,
            )
            return entry

        _LOGGER.debug(
            "No entity registry entry for device '%s'; checked unique_id formats %s (canonical=%s registry_id=%s registry_identifier=%s)",
            device_label,
            candidate_unique_ids,
            canonical_unique_id,
            device_id,
            registry_identifier,
        )
        return None

    def find_tracker_entity_entry(
        self: "GoogleFindMyCoordinator", device_id: str
    ) -> EntityRegistryEntry | None:
        """Public wrapper to expose tracker entity lookup to platforms."""
        return self._find_tracker_entity_entry(device_id)
