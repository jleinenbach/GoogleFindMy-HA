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

import hashlib
import logging
import time
from typing import Any, Iterable, Optional, Tuple

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError, HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.network import get_url

from .const import (
    DOMAIN,
    SERVICE_LOCATE_DEVICE,
    SERVICE_LOCATE_EXTERNAL,
    SERVICE_PLAY_SOUND,
    SERVICE_STOP_SOUND,
    SERVICE_REFRESH_DEVICE_URLS,
    SERVICE_REBUILD_REGISTRY,
    ATTR_DEVICE_IDS,
    ATTR_MODE,
    MODE_MIGRATE,
    MODE_REBUILD,
    REBUILD_REGISTRY_MODES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,  # ctx provides the key but we keep a local fallback constant
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    LEGACY_SERVICE_IDENTIFIER,
    SERVICE_DEVICE_IDENTIFIER_PREFIX,
)

_LOGGER = logging.getLogger(__name__)


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
        - "primary_active_entry": Callable[[list[ConfigEntry]], Optional[ConfigEntry]]
        - "opt": Callable[[ConfigEntry, str, Any], Any]
        - "default_map_view_token_expiration": bool
        - "opt_map_view_token_expiration_key": str
        - "redact_url_token": Callable[[str], str]
        - "soft_migrate_entry": Callable[[HomeAssistant, Any], Any]  # awaited per entry
    """

    # ---- Small local helpers (no circular imports) ---------------------------

    def _iter_runtimes(hass: HomeAssistant) -> Iterable[Any]:
        """Yield all active runtime containers (RuntimeData) for this integration."""
        entries: dict[str, Any] = hass.data.setdefault(DOMAIN, {}).setdefault(
            "entries", {}
        )
        return entries.values()

    async def _resolve_runtime_for_device_id(device_id: str) -> Tuple[Any, str]:
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
                    active_count = sum(1 for entry in configured_entries if is_active(entry))
                entry_titles = [
                    entry.title or entry.entry_id
                    for entry in configured_entries
                ]
                entries_placeholder = ", ".join(entry_titles) or "—"
            else:
                entries_placeholder = "—"

            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_active_entry",
                translation_placeholders={
                    "entries": entries_placeholder,
                    "active_count": str(active_count),
                    "total_count": str(total_count),
                },
            )

        # Resolve canonical id & friendly name via context resolver (device_id/entity_id/canonical_id)
        try:
            canonical_id, _friendly = ctx["resolve_canonical"](hass, device_id)
        except HomeAssistantError as err:
            # Pass through as translated validation error
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders={"device_id": str(device_id)},
            ) from err

        # 1) Preferred mapping: device registry relation for the passed device_id
        dev_reg = dr.async_get(hass)
        dev = dev_reg.async_get(device_id)
        if dev:
            for entry_id in dev.config_entries:
                runtime = hass.data[DOMAIN]["entries"].get(entry_id)
                if runtime:
                    return runtime, canonical_id

        # 2) Fallback: scan known coordinators for the canonical id
        for runtime in runtimes:
            coord = getattr(runtime, "coordinator", None)
            if coord and hasattr(coord, "get_device_display_name"):
                try:
                    if coord.get_device_display_name(canonical_id):
                        return runtime, canonical_id
                except Exception:
                    pass

        # 3) Not found -> translated error
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="device_not_found",
            translation_placeholders={"device_id": str(device_id)},
        )

    # ---- Service handlers ----------------------------------------------------

    async def async_locate_device_service(call: ServiceCall) -> None:
        """Handle locate device service call (metadata in services.yaml)."""
        raw_device_id = call.data.get("device_id")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders={"device_id": str(raw_device_id)},
            )
        try:
            runtime, canonical_id = await _resolve_runtime_for_device_id(raw_device_id)
            await runtime.coordinator.async_locate_device(canonical_id)
        except ServiceValidationError:
            raise
        except Exception as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="locate_failed",
                translation_placeholders={
                    "device_id": str(raw_device_id),
                    "error": str(err),
                },
            ) from err

    async def async_play_sound_service(call: ServiceCall) -> None:
        """Handle play sound service call."""
        raw_device_id = call.data.get("device_id")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders={"device_id": str(raw_device_id)},
            )
        try:
            runtime, canonical_id = await _resolve_runtime_for_device_id(raw_device_id)
            await runtime.coordinator.async_play_sound(canonical_id)
        except ServiceValidationError:
            raise
        except Exception as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="play_sound_failed",
                translation_placeholders={
                    "device_id": str(raw_device_id),
                    "error": str(err),
                },
            ) from err

    async def async_stop_sound_service(call: ServiceCall) -> None:
        """Handle stop sound service call."""
        raw_device_id = call.data.get("device_id")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders={"device_id": str(raw_device_id)},
            )
        try:
            runtime, canonical_id = await _resolve_runtime_for_device_id(raw_device_id)
            await runtime.coordinator.async_stop_sound(canonical_id)
        except ServiceValidationError:
            raise
        except Exception as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="stop_sound_failed",
                translation_placeholders={
                    "device_id": str(raw_device_id),
                    "error": str(err),
                },
            ) from err

    async def async_locate_external_service(call: ServiceCall) -> None:
        """External locate device service (delegates to locate)."""
        raw_device_id = call.data.get("device_id")
        if not isinstance(raw_device_id, str) or not raw_device_id:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_not_found",
                translation_placeholders={"device_id": str(raw_device_id)},
            )
        # device_name is optional; currently used only for logging on caller side.
        try:
            runtime, canonical_id = await _resolve_runtime_for_device_id(raw_device_id)
            await runtime.coordinator.async_locate_device(canonical_id)
        except ServiceValidationError:
            raise
        except Exception as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="locate_failed",
                translation_placeholders={
                    "device_id": str(raw_device_id),
                    "error": str(err),
                },
            ) from err

    async def async_refresh_device_urls_service(call: ServiceCall) -> None:
        """Refresh configuration URLs for integration devices (absolute URL).

        Security:
            The token is a short-lived (weekly) or static gate derived from the HA UUID.
            All tokens are redacted in logs; the view must validate tokens server-side.
        """
        from homeassistant.exceptions import HomeAssistantError  # local import to avoid top-level dependency

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
        opt_key = ctx.get("opt_map_view_token_expiration_key", OPT_MAP_VIEW_TOKEN_EXPIRATION)

        expiration_cache: dict[str, bool] = {}
        token_cache: dict[str, str] = {}
        ha_uuid = str(hass.data.get("core.uuid", "ha"))
        week_bucket = str(int(time.time() // 604800))

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
                token_src = f"{ha_uuid}:{entry_part}:{week_bucket}"
            else:
                token_src = f"{ha_uuid}:{entry_part}:static"

            token_cache[cache_key] = hashlib.md5(token_src.encode()).hexdigest()[:16]
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
            new_config_url = f"{base_url}/api/googlefindmy/map/{canonical_id}?token={auth_token}"
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

    async def async_rebuild_registry_service(call: ServiceCall) -> None:
        """Migrate soft settings or rebuild the registry (optionally scoped to device_ids).

        Safety invariants:
          - Delete only what is unambiguously ours:
            * Entities: ent.platform == DOMAIN
            * Devices: device.identifiers contains our DOMAIN AND ALL linked config_entries belong to our DOMAIN
            * Never touch the "service"/integration device (identifier == 'integration' or f'integration_<entry_id>')
          - Do not remove any device that still has entities (from any platform).
        Steps:
          1) Determine target devices (ours) from Device Registry.
          2) Remove all entities (ours) linked to those devices.
          3) Remove orphan entities (ours) with no device or missing device.
          4) Remove devices (ours) that now have no entities left and are not the service device.
          5) Reload entries (or all if only global orphan-entity cleanup happened).
        """
        mode: str = str(call.data.get(ATTR_MODE, MODE_REBUILD)).lower()
        raw_ids = call.data.get(ATTR_DEVICE_IDS)

        # C-4: Micro-hardening for device_ids input (developer tools misuse)
        if isinstance(raw_ids, str):
            target_device_ids = {raw_ids}
        elif isinstance(raw_ids, (list, tuple, set)):
            try:
                target_device_ids = {str(x) for x in raw_ids}
            except Exception:
                _LOGGER.error("Invalid 'device_ids' payload; expected list/tuple/set of device IDs (strings).")
                return
        elif raw_ids is None:
            target_device_ids = set()
        else:
            _LOGGER.error("Invalid 'device_ids' type: %s; expected string, list/tuple/set, or omitted.", type(raw_ids).__name__)
            return

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
        entries = hass.config_entries.async_entries(DOMAIN)

        _LOGGER.info(
            "googlefindmy.rebuild_registry requested: mode=%s, device_ids=%s",
            mode,
            "none"
            if not raw_ids
            else (raw_ids if isinstance(raw_ids, str) else f"{len(target_device_ids)} ids"),
        )

        if mode == MODE_MIGRATE:
            # C-2: real soft-migrate path using __init__.py helper via ctx
            soft_migrate = ctx.get("soft_migrate_entry")
            if callable(soft_migrate):
                for entry_ in entries:
                    try:
                        await soft_migrate(hass, entry_)
                    except Exception as err:
                        _LOGGER.error("Soft-migrate failed for entry %s: %s", entry_.entry_id, err)
            else:
                _LOGGER.warning("soft_migrate_entry not provided in context; MIGRATE path is a no-op.")

            # Reload all entries to apply migrations
            for entry_ in entries:
                try:
                    await hass.config_entries.async_reload(entry_.entry_id)
                except Exception as err:
                    _LOGGER.error("Reload failed for entry %s: %s", entry_.entry_id, err)

            _LOGGER.info(
                "googlefindmy.rebuild_registry: soft-migrate completed for %d config entrie(s).",
                len(entries),
            )
            return

        if mode != MODE_REBUILD:
            _LOGGER.error(
                "Unsupported mode '%s' for rebuild_registry; use one of: %s",
                mode,
                ", ".join(REBUILD_REGISTRY_MODES),
            )
            return

        def _dev_is_ours(dev: dr.DeviceEntry | None) -> bool:
            if dev is None:
                return False
            has_our_ident = any(domain == DOMAIN for domain, _ in dev.identifiers)
            if not has_our_ident:
                return False
            for eid in dev.config_entries:
                e = hass.config_entries.async_get_entry(eid)
                if not e or e.domain != DOMAIN:
                    return False
            return True

        def _is_service_device(dev: dr.DeviceEntry) -> bool:
            # service device can be 'integration' or entry-scoped name
            if any(domain == DOMAIN and ident == "integration" for domain, ident in dev.identifiers):
                return True
            return any(
                domain == DOMAIN and str(ident).startswith("integration_")
                for domain, ident in dev.identifiers
            )

        # 1) Determine candidate devices (strictly ours), optionally filtered by passed HA device_ids.
        affected_entry_ids: set[str] = set()
        candidate_devices: set[str] = set()

        if target_device_ids:
            for d in target_device_ids:
                dev = dev_reg.async_get(d)
                if dev and _dev_is_ours(dev):
                    candidate_devices.add(dev.id)
                    affected_entry_ids.update(dev.config_entries)
        else:
            for dev in dev_reg.devices.values():
                if _dev_is_ours(dev):
                    candidate_devices.add(dev.id)
                    affected_entry_ids.update(dev.config_entries)

        removed_entities = 0
        removed_devices = 0

        # 2) Remove our entities linked to candidate devices (includes disabled/hidden).
        for ent in list(ent_reg.entities.values()):
            if ent.platform == DOMAIN and ent.device_id in candidate_devices:
                try:
                    ent_reg.async_remove(ent.entity_id)
                    removed_entities += 1
                except Exception as err:
                    _LOGGER.error("Failed to remove entity %s: %s", ent.entity_id, err)

        # 3) Orphan cleanup (ours only): platform==DOMAIN and (no device_id OR device missing).
        orphan_only_cleanup = False
        for ent in list(ent_reg.entities.values()):
            if ent.platform != DOMAIN:
                continue
            if ent.device_id:
                dev_obj = dev_reg.async_get(ent.device_id)
                if dev_obj is not None:
                    continue  # has a device; not an orphan
            try:
                ent_reg.async_remove(ent.entity_id)
                removed_entities += 1
                orphan_only_cleanup = True
            except Exception as err:
                _LOGGER.error("Failed to remove orphan entity %s: %s", ent.entity_id, err)

        # 4) Remove devices (ours only) that now have no entities left and are not service devices.
        for dev_id in list(candidate_devices):
            dev = dev_reg.async_get(dev_id)
            if dev is None or not _dev_is_ours(dev) or _is_service_device(dev):
                continue
            has_entities = any(e.device_id == dev_id for e in ent_reg.entities.values())
            if not has_entities:
                try:
                    dev_reg.async_remove_device(dev_id)
                    removed_devices += 1
                except Exception as err:
                    _LOGGER.error("Failed to remove device %s: %s", dev_id, err)

        # 5) Reload entries. If only orphan entities were removed and we didn't touch any known devices,
        #    reload all entries of our domain to be safe.
        if orphan_only_cleanup and not affected_entry_ids:
            to_reload = list(entries)
        else:
            to_reload = [e for e in entries if e.entry_id in affected_entry_ids] or list(entries)

        for entry_ in to_reload:
            try:
                await hass.config_entries.async_reload(entry_.entry_id)
            except Exception as err:
                _LOGGER.error("Reload failed for entry %s: %s", entry_.entry_id, err)

        _LOGGER.info(
            "googlefindmy.rebuild_registry: finished (safe mode): removed %d entit(y/ies), %d device(s), entries reloaded=%d",
            removed_entities,
            removed_devices,
            len(to_reload),
        )

    # ---- Actual service registrations (global; visible even without entries) ----
    # NOTE: We intentionally do NOT pass voluptuous schemas here; services.yaml is our SSoT.
    hass.services.async_register(DOMAIN, SERVICE_LOCATE_DEVICE, async_locate_device_service)
    hass.services.async_register(DOMAIN, SERVICE_LOCATE_EXTERNAL, async_locate_external_service)
    hass.services.async_register(DOMAIN, SERVICE_PLAY_SOUND, async_play_sound_service)
    hass.services.async_register(DOMAIN, SERVICE_STOP_SOUND, async_stop_sound_service)
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_DEVICE_URLS, async_refresh_device_urls_service)
    hass.services.async_register(DOMAIN, SERVICE_REBUILD_REGISTRY, async_rebuild_registry_service)
