# custom_components/googlefindmy/services.py

"""Service handlers & registration for Google Find My Device (Home Assistant).

Design goals
------------
- services.yaml is the single source of truth for service metadata (names, descriptions, selectors).
- This module only registers handlers and implements business logic.
- No circular imports: required helpers (e.g. canonical resolver, option reader) are passed via a context.
- PII-safe logs: do not log secrets or coordinates; redact tokens in URLs on info/debug paths.

Compatibility
-------------
- HA 2025.5+; depends on services.yaml for UI/schema validation.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from collections.abc import Iterable, Mapping

from homeassistant import exceptions as ha_exceptions
from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.network import get_url

try:  # Home Assistant 2025.5+: attribute constant exposed
    from homeassistant.const import ATTR_ENTRY_ID
except ImportError:  # pragma: no cover - forward compatibility for HA < 2025.5
    ATTR_ENTRY_ID = "entry_id"

from .const import (
    DOMAIN,
    SERVICE_LOCATE_DEVICE,
    SERVICE_LOCATE_EXTERNAL,
    SERVICE_PLAY_SOUND,
    SERVICE_STOP_SOUND,
    SERVICE_REFRESH_DEVICE_URLS,
    SERVICE_REBUILD_REGISTRY,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,  # ctx provides the key but we keep a local fallback constant
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
    map_token_hex_digest,
    map_token_secret_seed,
    service_device_identifier,
)

_LOGGER = logging.getLogger(__name__)

ServiceValidationError = ha_exceptions.ServiceValidationError
HomeAssistantError = ha_exceptions.HomeAssistantError
ConfigEntryError = getattr(ha_exceptions, "ConfigEntryError", HomeAssistantError)


SERVICE_REBUILD_DEVICE_REGISTRY: str = "rebuild_device_registry"


async def async_rebuild_device_registry(hass: HomeAssistant, call: ServiceCall) -> None:
    """Synchronize and clean Device Registry entries for Google Find My hubs."""

    _LOGGER.info(
        "Service '%s' called: rebuilding device registry.",
        SERVICE_REBUILD_DEVICE_REGISTRY,
    )

    domain_bucket = hass.data.get(DOMAIN, {})
    if not isinstance(domain_bucket, Mapping):
        domain_bucket = {}

    def _extract_hub_details(
        candidate: Any,
    ) -> tuple[str, Mapping[Any, Any]] | None:
        """Return the hub entry_id and coordinators mapping if available."""

        entry_id: str | None
        coordinators: Any

        if isinstance(candidate, Mapping):
            raw_entry_id = candidate.get("entry_id")
            if isinstance(raw_entry_id, str) and raw_entry_id:
                entry_id = raw_entry_id
            elif raw_entry_id is not None:
                entry_id = str(raw_entry_id)
            else:
                entry_id = None
            coordinators = candidate.get("coordinators")
        else:
            raw_entry_id = getattr(candidate, "entry_id", None)
            if isinstance(raw_entry_id, str) and raw_entry_id:
                entry_id = raw_entry_id
            elif raw_entry_id is not None:
                entry_id = str(raw_entry_id)
            else:
                entry_id = None
            coordinators = getattr(candidate, "coordinators", None)

        if not entry_id:
            return None

        if isinstance(coordinators, Mapping):
            return entry_id, coordinators

        return None

    def _iter_hubs() -> list[tuple[str, Mapping[Any, Any]]]:
        """Yield hub entry_id and coordinator mapping tuples."""

        hubs: list[tuple[str, Mapping[Any, Any]]] = []
        seen: set[int] = set()

        hub_bucket = (
            domain_bucket.get("hubs") if isinstance(domain_bucket, Mapping) else None
        )
        if isinstance(hub_bucket, Mapping):
            for item in hub_bucket.values():
                if item is None or id(item) in seen:
                    continue
                seen.add(id(item))
                details = _extract_hub_details(item)
                if details is not None:
                    hubs.append(details)

        for value in domain_bucket.values():
            if value is None or id(value) in seen:
                continue
            details = _extract_hub_details(value)
            if details is not None:
                hubs.append(details)

        return hubs

    processed_coordinators = 0
    seen_coordinators: set[int] = set()

    for _hub_entry_id, coordinators in _iter_hubs():
        for coordinator in coordinators.values():
            if coordinator is None or id(coordinator) in seen_coordinators:
                continue
            update_registry = getattr(coordinator, "async_update_device_registry", None)
            if not callable(update_registry):
                continue
            try:
                await update_registry()
            except Exception as err:  # noqa: BLE001 - defensive logging
                _LOGGER.warning(
                    "Coordinator %s failed during registry rebuild: %s",
                    getattr(coordinator, "name", "<unknown>"),
                    err,
                )
                continue
            processed_coordinators += 1
            seen_coordinators.add(id(coordinator))

    entries_bucket = (
        domain_bucket.get("entries") if isinstance(domain_bucket, Mapping) else None
    )
    if isinstance(entries_bucket, Mapping):
        for runtime in entries_bucket.values():
            coordinator = getattr(runtime, "coordinator", None)
            if coordinator is None or id(coordinator) in seen_coordinators:
                continue
            update_registry = getattr(coordinator, "async_update_device_registry", None)
            if not callable(update_registry):
                continue
            try:
                await update_registry()
            except Exception as err:  # noqa: BLE001 - defensive logging
                _LOGGER.warning(
                    "Runtime coordinator %s failed during registry rebuild: %s",
                    getattr(coordinator, "name", "<unknown>"),
                    err,
                )
                continue
            processed_coordinators += 1
            seen_coordinators.add(id(coordinator))

    _LOGGER.info(
        "Completed device registry ensure phase; processed %d coordinators.",
        processed_coordinators,
    )

    # --- Phase 2: Cleanup Orphaned Devices ---
    _LOGGER.info("Starting device registry cleanup phase...")
    dev_reg = dr.async_get(hass)

    processed_hubs = 0
    cleaned_devices = 0

    for hub_entry_id, coordinators in _iter_hubs():
        if not hub_entry_id:
            continue

        processed_hubs += 1

        service_device_ident = service_device_identifier(hub_entry_id)
        try:
            service_device = dev_reg.async_get_device(
                identifiers={service_device_ident}
            )
        except TypeError:  # pragma: no cover - defensive best effort
            service_device = None
        service_device_id = (
            getattr(service_device, "id", None) if service_device else None
        )

        active_sub_entry_ids: set[str] = set()
        for coordinator in coordinators.values():
            config_entry = getattr(coordinator, "config_entry", None)
            entry_id = getattr(config_entry, "entry_id", None)
            if isinstance(entry_id, str):
                active_sub_entry_ids.add(entry_id)

        active_sub_entry_ids.discard(hub_entry_id)

        _LOGGER.debug(
            "[%s] Hub Cleanup: Found %d active sub-entries. (Service Device ID: %s)",
            hub_entry_id,
            len(active_sub_entry_ids),
            service_device_id,
        )

        try:
            get_devices_for_entry = getattr(
                dr, "async_get_devices_for_config_entry", None
            )
            if callable(get_devices_for_entry):
                all_hub_linked_devices = get_devices_for_entry(dev_reg, hub_entry_id)
            else:
                all_hub_linked_devices = dr.async_entries_for_config_entry(
                    dev_reg, hub_entry_id
                )
        except Exception as err:  # noqa: BLE001 - defensive logging
            _LOGGER.warning(
                "[%s] Hub Cleanup: Failed to get devices for config entry: %s",
                hub_entry_id,
                err,
            )
            continue

        for device in all_hub_linked_devices:
            if device is None:
                continue

            device_id = getattr(device, "id", None)
            if not isinstance(device_id, str) or not device_id:
                continue

            raw_links = getattr(device, "config_entries", set()) or set()
            device_links = {
                str(entry_id)
                for entry_id in raw_links
                if isinstance(entry_id, str) and entry_id
            }

            active_overlap = device_links & active_sub_entry_ids
            if not active_overlap:
                continue

            _LOGGER.info(
                "[%s] Hub Cleanup: Detaching hub entry from device '%s' (ID: %s). Active sub-entry links: %s",
                hub_entry_id,
                getattr(device, "name", None),
                device_id,
                active_overlap,
            )
            try:
                dev_reg.async_update_device(
                    device_id,
                    remove_config_entry_id=hub_entry_id,
                )
                cleaned_devices += 1
            except Exception as err:  # noqa: BLE001 - defensive logging
                _LOGGER.error(
                    "[%s] Hub Cleanup: Failed to remove config entry from device %s: %s",
                    hub_entry_id,
                    device_id,
                    err,
                )

    _LOGGER.info(
        "Device registry cleanup phase complete. Processed %d hubs and removed %d orphaned device links.",
        processed_hubs,
        cleaned_devices,
    )

    # --- Phase 3: remove legacy tracker entities from the Entity Registry -----
    ent_reg = er.async_get(hass)

    managed_entry_ids: set[str] = set()
    entries_bucket = domain_bucket.get("entries") if isinstance(domain_bucket, Mapping) else None
    if isinstance(entries_bucket, Mapping):
        for raw_entry_id in entries_bucket.keys():
            if isinstance(raw_entry_id, str) and raw_entry_id:
                managed_entry_ids.add(raw_entry_id)

    hub_bucket = domain_bucket.get("hubs") if isinstance(domain_bucket, Mapping) else None
    if isinstance(hub_bucket, Mapping):
        for raw_hub_id in hub_bucket.keys():
            if isinstance(raw_hub_id, str) and raw_hub_id:
                managed_entry_ids.add(raw_hub_id)

    for hub_entry_id, _coordinators in _iter_hubs():
        if isinstance(hub_entry_id, str) and hub_entry_id:
            managed_entry_ids.add(hub_entry_id)

    removed_entities = 0
    entities_container = getattr(ent_reg, "entities", None)
    if isinstance(entities_container, Mapping):
        for entry in list(entities_container.values()):
            if getattr(entry, "platform", None) != DOMAIN:
                continue
            if getattr(entry, "domain", None) != DEVICE_TRACKER_DOMAIN:
                continue

            config_entry_id = getattr(entry, "config_entry_id", None)
            if (
                not isinstance(config_entry_id, str)
                or config_entry_id not in managed_entry_ids
            ):
                continue

            unique_id = getattr(entry, "unique_id", None)
            if not isinstance(unique_id, str) or not unique_id:
                continue

            is_canonical = (
                unique_id.startswith(f"{config_entry_id}:")
                and unique_id.count(":") >= 2
            )
            if is_canonical:
                continue

            _LOGGER.info(
                "[%s] Tracker cleanup: removing legacy entity '%s' (unique_id=%s); it no longer matches the canonical entry-scoped schema.",
                config_entry_id,
                getattr(entry, "entity_id", "<unknown>"),
                unique_id,
            )
            try:
                ent_reg.async_remove(entry.entity_id)
                removed_entities += 1
            except Exception as err:  # noqa: BLE001 - defensive logging
                _LOGGER.error(
                    "[%s] Tracker cleanup: failed to remove entity %s: %s",
                    config_entry_id,
                    getattr(entry, "entity_id", "<unknown>"),
                    err,
                )

    _LOGGER.info(
        "Entity registry cleanup phase complete. Removed %d legacy tracker entities.",
        removed_entities,
    )


async def async_register_services(hass: HomeAssistant, ctx: dict[str, Any]) -> None:
    """Register integration-wide services, using services.yaml for metadata.

    Parameters
    ----------
    hass : HomeAssistant
        The running HA instance.
    ctx : dict[str, Any]
        A context passed by __init__.py to avoid import cycles. Expected keys:
        - "domain": str
        - "resolve_canonical": Callable[[HomeAssistant, str], Tuple[str, str]]
        - "is_active_entry": Callable[[ConfigEntry], bool]
        - "primary_active_entry": Callable[[list[ConfigEntry]], ConfigEntry | None]
        - "opt": Callable[[ConfigEntry, str, Any], Any]
        - "default_map_view_token_expiration": bool
        - "opt_map_view_token_expiration_key": str
        - "redact_url_token": Callable[[str], str]
        - "soft_migrate_entry": Callable[[HomeAssistant, Any], Any]  # awaited per entry
        - "migrate_unique_ids": Callable[[HomeAssistant, Any], Any]
        - "relink_button_devices": Callable[[HomeAssistant, Any], Any]
        - "coalesce_account_entries": Callable[[HomeAssistant, ConfigEntry], Awaitable[ConfigEntry]]
        - "extract_normalized_email": Callable[[ConfigEntry], str | None]
    """

    # ---- Small local helpers (no circular imports) ---------------------------

    def _iter_runtimes(hass: HomeAssistant) -> Iterable[Any]:
        """Yield active runtime containers, preferring entry.runtime_data."""

        seen: set[int] = set()
        manager = getattr(hass, "config_entries", None)
        async_entries = getattr(manager, "async_entries", None)
        if callable(async_entries):
            try:
                for entry in async_entries(DOMAIN):
                    runtime = getattr(entry, "runtime_data", None)
                    if runtime is None:
                        continue
                    seen.add(id(runtime))
                    yield runtime
            except Exception:  # pragma: no cover - defensive guard
                pass

        entries: dict[str, Any] = hass.data.setdefault(DOMAIN, {}).setdefault(
            "entries", {}
        )
        for runtime in entries.values():
            if id(runtime) not in seen:
                yield runtime

    def _entry_for_id(hass: HomeAssistant, entry_id: str) -> Any | None:
        """Return the config entry with the given id, if available."""

        manager = getattr(hass, "config_entries", None)
        if manager is None:
            return None

        getter = getattr(manager, "async_get_entry", None)
        if callable(getter):
            try:
                return getter(entry_id)
            except Exception:  # pragma: no cover - defensive guard
                return None

        async_entries = getattr(manager, "async_entries", None)
        if callable(async_entries):
            try:
                for entry in async_entries(DOMAIN):
                    if entry.entry_id == entry_id:
                        return entry
            except Exception:  # pragma: no cover - defensive guard
                return None
        return None

    async def _resolve_runtime_for_device_id(device_id: str) -> tuple[Any, str]:
        """Return the runtime and canonical_id for a device_id or raise translated error.

        Robustness:
        - If no runtime is available at all, fail early with a clear, translated error.
        - Prefer Device Registry mapping from the provided device_id.
        - Fall back to scanning active coordinators for the device's canonical id presence.
        """
        # Early exit: no active runtimes at all
        runtimes = list(_iter_runtimes(hass))
        if not runtimes:
            configured_entries = []
            active_count = 0
            total_count = 0

            config_entries = getattr(hass, "config_entries", None)
            if config_entries is not None:
                try:
                    configured_entries = list(config_entries.async_entries(DOMAIN))
                except Exception:  # pragma: no cover - defensive guard
                    configured_entries = []

            if configured_entries:
                total_count = len(configured_entries)
                is_active = ctx.get("is_active_entry")
                if callable(is_active):
                    active_count = sum(
                        1 for entry in configured_entries if is_active(entry)
                    )
                entry_titles = [
                    entry.title or entry.entry_id for entry in configured_entries
                ]
                entries_placeholder = ", ".join(entry_titles) or "—"
            else:
                entries_placeholder = "—"

            placeholders = {
                "entries": entries_placeholder,
                "active_count": str(active_count),
                "total_count": str(total_count),
            }
            raise ServiceValidationError(
                message=(
                    "No active Google Find My entries (active {active_count}/{total_count};"
                    " entries: {entries}).".format(**placeholders)
                ),
                translation_domain=DOMAIN,
                translation_key="no_active_entry",
                translation_placeholders=placeholders,
            )

        # Resolve canonical id & friendly name via context resolver (device_id/entity_id/canonical_id)
        try:
            canonical_id, _friendly = ctx["resolve_canonical"](hass, device_id)
        except HomeAssistantError as err:
            # Pass through as translated validation error
            placeholders = {"device_id": str(device_id)}
            raise ServiceValidationError(
                message="Device '{device_id}' was not found.".format(**placeholders),
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders=placeholders,
            ) from err

        # 1) Preferred mapping: device registry relation for the passed device_id
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get(device_id)
        if dev:
            for entry_id in dev.config_entries:
                entry = _entry_for_id(hass, entry_id)
                runtime = getattr(entry, "runtime_data", None)
                if runtime:
                    return runtime, canonical_id
                runtime = (
                    hass.data.setdefault(DOMAIN, {})
                    .setdefault("entries", {})
                    .get(entry_id)
                )
                if runtime:
                    return runtime, canonical_id

        # 2) Fallback: scan known coordinators for the canonical id
        for runtime in runtimes:
            coord = getattr(runtime, "coordinator", None)
            if not coord:
                continue

            display_lookup = getattr(coord, "get_device_display_name", None)
            if callable(display_lookup):
                try:
                    display_name = display_lookup(canonical_id)
                except Exception:
                    display_name = None
                if display_name is not None:
                    return runtime, canonical_id

            location_lookup = getattr(coord, "get_device_location_data", None)
            if callable(location_lookup):
                try:
                    location_data = location_lookup(canonical_id)
                except Exception:
                    continue
                if location_data is not None:
                    return runtime, canonical_id

        # 3) Not found -> translated error
        placeholders = {"device_id": str(device_id)}
        raise ServiceValidationError(
            message="Device '{device_id}' was not found.".format(**placeholders),
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders=placeholders,
        )

    # ---- Service handlers ----------------------------------------------------

    async def async_locate_device_service(call: ServiceCall) -> None:
        """Handle locate device service call (metadata in services.yaml)."""
        raw_device_id = call.data.get("device_id")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            placeholders = {"device_id": str(raw_device_id)}
            raise ServiceValidationError(
                message="Device '{device_id}' was not found.".format(**placeholders),
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders=placeholders,
            )
        try:
            runtime, canonical_id = await _resolve_runtime_for_device_id(raw_device_id)
            await runtime.coordinator.async_locate_device(canonical_id)
        except ServiceValidationError:
            raise
        except Exception as err:
            placeholders = {
                "device_id": str(raw_device_id),
                "error": str(err),
            }
            raise ServiceValidationError(
                message="Failed to locate device '{device_id}': {error}".format(
                    **placeholders
                ),
                translation_domain=DOMAIN,
                translation_key="locate_failed",
                translation_placeholders=placeholders,
            ) from err

    async def async_play_sound_service(call: ServiceCall) -> None:
        """Handle play sound service call."""
        raw_device_id = call.data.get("device_id")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            placeholders = {"device_id": str(raw_device_id)}
            raise ServiceValidationError(
                message="Device '{device_id}' was not found.".format(**placeholders),
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders=placeholders,
            )
        try:
            runtime, canonical_id = await _resolve_runtime_for_device_id(raw_device_id)
            await runtime.coordinator.async_play_sound(canonical_id)
        except ServiceValidationError:
            raise
        except Exception as err:
            placeholders = {
                "device_id": str(raw_device_id),
                "error": str(err),
            }
            raise ServiceValidationError(
                message="Failed to play sound on device '{device_id}': {error}".format(
                    **placeholders
                ),
                translation_domain=DOMAIN,
                translation_key="play_sound_failed",
                translation_placeholders=placeholders,
            ) from err

    async def async_stop_sound_service(call: ServiceCall) -> None:
        """Handle stop sound service call."""
        raw_device_id = call.data.get("device_id")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            placeholders = {"device_id": str(raw_device_id)}
            raise ServiceValidationError(
                message="Device '{device_id}' was not found.".format(**placeholders),
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders=placeholders,
            )
        try:
            runtime, canonical_id = await _resolve_runtime_for_device_id(raw_device_id)
            await runtime.coordinator.async_stop_sound(canonical_id)
        except ServiceValidationError:
            raise
        except Exception as err:
            placeholders = {
                "device_id": str(raw_device_id),
                "error": str(err),
            }
            raise ServiceValidationError(
                message="Failed to stop sound on device '{device_id}': {error}".format(
                    **placeholders
                ),
                translation_domain=DOMAIN,
                translation_key="stop_sound_failed",
                translation_placeholders=placeholders,
            ) from err

    async def async_locate_external_service(call: ServiceCall) -> None:
        """External locate device service (delegates to locate)."""
        raw_device_id = call.data.get("device_id")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            placeholders = {"device_id": str(raw_device_id)}
            raise ServiceValidationError(
                message="Device '{device_id}' was not found.".format(**placeholders),
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders=placeholders,
            )
        # device_name is optional; currently used only for logging on caller side.
        try:
            runtime, canonical_id = await _resolve_runtime_for_device_id(raw_device_id)
            await runtime.coordinator.async_locate_device(canonical_id)
        except ServiceValidationError:
            raise
        except Exception as err:
            placeholders = {
                "device_id": str(raw_device_id),
                "error": str(err),
            }
            raise ServiceValidationError(
                message="Failed to locate device '{device_id}': {error}".format(
                    **placeholders
                ),
                translation_domain=DOMAIN,
                translation_key="locate_failed",
                translation_placeholders=placeholders,
            ) from err

    async def async_refresh_device_urls_service(call: ServiceCall) -> None:
        """Refresh configuration URLs for integration devices (absolute URL).

        Security:
            The token is a short-lived (weekly) or static gate derived from the HA UUID.
            All tokens are redacted in logs; the view must validate tokens server-side.
        """
        from homeassistant.exceptions import (
            HomeAssistantError,
        )  # local import to avoid top-level dependency

        try:
            base_url = get_url(
                hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except HomeAssistantError as err:
            _LOGGER.error("Could not determine base URL for device refresh: %s", err)
            return

        entries = hass.config_entries.async_entries(DOMAIN)
        entries_by_id = {entry.entry_id: entry for entry in entries}

        default_expiration = bool(
            ctx.get(
                "default_map_view_token_expiration",
                DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
            )
        )
        opt_reader = ctx.get("opt")
        opt_key = ctx.get(
            "opt_map_view_token_expiration_key", OPT_MAP_VIEW_TOKEN_EXPIRATION
        )

        expiration_cache: dict[str, bool] = {}
        token_cache: dict[str, str] = {}
        ha_uuid = str(hass.data.get("core.uuid", "ha"))
        now = int(time.time())

        def _expiration_enabled(entry_id: str | None) -> bool:
            cache_key = entry_id or ""
            if cache_key in expiration_cache:
                return expiration_cache[cache_key]

            entry = entries_by_id.get(entry_id) if entry_id else None
            enabled = default_expiration

            if entry:
                if callable(opt_reader):
                    try:
                        enabled = bool(opt_reader(entry, opt_key, default_expiration))
                    except Exception:
                        enabled = bool(
                            entry.options.get(
                                opt_key,
                                entry.data.get(opt_key, default_expiration),
                            )
                        )
                else:
                    enabled = bool(
                        entry.options.get(
                            opt_key,
                            entry.data.get(opt_key, default_expiration),
                        )
                    )

            expiration_cache[cache_key] = bool(enabled)
            return expiration_cache[cache_key]

        def _token_for_entry(entry_id: str | None) -> str:
            cache_key = entry_id or ""
            if cache_key in token_cache:
                return token_cache[cache_key]

            entry_part = entry_id or ""
            if _expiration_enabled(entry_id):
                seed = map_token_secret_seed(ha_uuid, entry_part, True, now=now)
            else:
                seed = map_token_secret_seed(ha_uuid, entry_part, False)

            token_cache[cache_key] = map_token_hex_digest(seed)
            return token_cache[cache_key]

        def _device_is_service(device: Any) -> bool:
            identifiers = getattr(device, "identifiers", set()) or set()
            for domain, ident in identifiers:
                if domain != DOMAIN:
                    continue
                ident_str = str(ident)
                if ident_str == LEGACY_SERVICE_IDENTIFIER or ident_str.startswith(
                    SERVICE_DEVICE_IDENTIFIER_PREFIX
                ):
                    return True
            return False

        def _canonical_identifier(device: Any, entry_id: str | None) -> str | None:
            serial = getattr(device, "serial_number", None)
            if isinstance(serial, str) and serial:
                return serial

            identifiers = getattr(device, "identifiers", set()) or set()
            for domain, ident in identifiers:
                if domain != DOMAIN:
                    continue

                ident_str = str(ident)
                if ident_str == LEGACY_SERVICE_IDENTIFIER or ident_str.startswith(
                    SERVICE_DEVICE_IDENTIFIER_PREFIX
                ):
                    continue

                if entry_id:
                    prefix = f"{entry_id}:"
                    if ident_str.startswith(prefix):
                        ident_str = ident_str[len(prefix) :]
                elif ":" in ident_str:
                    candidate, remainder = ident_str.split(":", 1)
                    if candidate in entries_by_id:
                        ident_str = remainder

                return ident_str

            return None

        dev_reg = dr.async_get(hass)
        updated_count = 0
        for device in getattr(dev_reg, "devices", {}).values():
            identifiers = getattr(device, "identifiers", set()) or set()
            if not any(domain == DOMAIN for domain, _ in identifiers):
                continue

            if _device_is_service(device):
                continue

            config_entry_ids = list(getattr(device, "config_entries", None) or [])
            owner_entry_id: str | None = None
            for candidate in config_entry_ids:
                candidate_str = str(candidate)
                if candidate_str in entries_by_id:
                    owner_entry_id = candidate_str
                    break
                if owner_entry_id is None:
                    owner_entry_id = candidate_str

            canonical_id = _canonical_identifier(device, owner_entry_id)
            if not canonical_id:
                continue

            auth_token = _token_for_entry(owner_entry_id)
            new_config_url = (
                f"{base_url}/api/googlefindmy/map/{canonical_id}?token={auth_token}"
            )
            dev_reg.async_update_device(
                device_id=device.id,
                configuration_url=new_config_url,
            )
            updated_count += 1
            if ctx.get("redact_url_token"):
                _LOGGER.debug(
                    "Updated URL for device %s: %s",
                    device.name_by_user or device.name,
                    ctx["redact_url_token"](new_config_url),
                )

        _LOGGER.info("Refreshed URLs for %d Google Find My devices", updated_count)

    async def async_rebuild_device_registry_service(call: ServiceCall) -> None:
        """Run the two-phase Device Registry rebuild + cleanup workflow."""

        await async_rebuild_device_registry(hass, call)

    async def async_rebuild_registry_service(call: ServiceCall) -> None:
        """Handle the service call to reload the integration."""
        _LOGGER.info(
            "Service 'rebuild_device_registry' (reloading config entry) called."
        )

        entry_ids_from_service = call.data.get(ATTR_ENTRY_ID)
        if isinstance(entry_ids_from_service, str):
            provided_entry_ids: list[str] = [entry_ids_from_service]
        elif isinstance(entry_ids_from_service, Iterable):
            provided_entry_ids = list(entry_ids_from_service)
        elif entry_ids_from_service is None:
            provided_entry_ids = []
        else:
            _LOGGER.warning(
                "Invalid %s payload type: %s",
                ATTR_ENTRY_ID,
                type(entry_ids_from_service),
            )
            return

        config_entry_ids: list[str] = []

        entries = hass.config_entries.async_entries(DOMAIN)
        entry: Any | None = entries[0] if entries else None

        if provided_entry_ids:
            config_entry_ids.extend(
                entry_id
                for entry_id in provided_entry_ids
                if any(e.entry_id == entry_id for e in entries)
            )
            if not config_entry_ids:
                _LOGGER.warning(
                    "No valid config entries found for IDs: %s",
                    provided_entry_ids,
                )
                return

        if not config_entry_ids:
            if entry is None:
                _LOGGER.warning("No config entries available to reload.")
                return
            _LOGGER.info("Reloading config entry: %s", entry.entry_id)
            await hass.config_entries.async_reload(entry.entry_id)
            return

        _LOGGER.info("Reloading config entries: %s", config_entry_ids)
        for entry_id in config_entry_ids:
            try:
                await hass.config_entries.async_reload(entry_id)
            except Exception as err:
                _LOGGER.error("Error reloading config entry %s: %s", entry_id, err)

    # ---- Actual service registrations (global; visible even without entries) ----
    # NOTE: We intentionally do NOT pass voluptuous schemas here; services.yaml is our SSoT.
    hass.services.async_register(
        DOMAIN, SERVICE_LOCATE_DEVICE, async_locate_device_service
    )
    hass.services.async_register(
        DOMAIN, SERVICE_LOCATE_EXTERNAL, async_locate_external_service
    )
    hass.services.async_register(DOMAIN, SERVICE_PLAY_SOUND, async_play_sound_service)
    hass.services.async_register(DOMAIN, SERVICE_STOP_SOUND, async_stop_sound_service)
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH_DEVICE_URLS, async_refresh_device_urls_service
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REBUILD_DEVICE_REGISTRY,
        async_rebuild_device_registry_service,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_REBUILD_REGISTRY, async_rebuild_registry_service
    )
