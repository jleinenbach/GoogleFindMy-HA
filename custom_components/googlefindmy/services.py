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
    OPT_MAP_VIEW_TOKEN_EXPIRATION,  # used for ctx sanity if missing
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
        - "primary_active_entry": Callable[[list[ConfigEntry]], Optional[ConfigEntry]]
        - "opt": Callable[[ConfigEntry, str, Any], Any]
        - "default_map_view_token_expiration": bool
        - "opt_map_view_token_expiration_key": str
        - "redact_url_token": Callable[[str], str]
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
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_active_entry",
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
        try:
            base_url = get_url(
                hass,
                prefer_external=True,
                allow_cloud=True,
                allow_external=True,
                allow_internal=True,
            )
        except HomeAssistantError as err:
            # Visible in logs; not raising a translated error because the service
            # has no user parameters and we don't want to mask the root cause.
            # (services.yaml provides the metadata/UI)
            hass.logger().error("Could not determine base URL for device refresh: %s", err)
            return

        # Choose options from the *active* entry (deterministic primary).
        config_entries = hass.config_entries.async_entries(DOMAIN)
        primary = ctx["primary_active_entry"](config_entries) if ctx.get("primary_active_entry") else None

        token_expiration_enabled = ctx.get("default_map_view_token_expiration", True)
        if primary is not None and ctx.get("opt"):
            key = ctx.get("opt_map_view_token_expiration_key", OPT_MAP_VIEW_TOKEN_EXPIRATION)
            token_expiration_enabled = bool(ctx["opt"](primary, key, token_expiration_enabled))

        ha_uuid = str(hass.data.get("core.uuid", "ha"))
        if token_expiration_enabled:
            week = str(int(time.time() // 604800))  # weekly rotation bucket
            auth_token = hashlib.md5(f"{ha_uuid}:{week}".encode()).hexdigest()[:16]
        else:
            auth_token = hashlib.md5(f"{ha_uuid}:static".encode()).hexdigest()[:16]

        dev_reg = dr.async_get(hass)
        updated_count = 0
        for device in dev_reg.devices.values():
            if any(identifier[0] == DOMAIN for identifier in device.identifiers):
                dev_id = next(
                    (ident for domain, ident in device.identifiers if domain == DOMAIN),
                    None,
                )
                if dev_id:
                    new_config_url = f"{base_url}/api/googlefindmy/map/{dev_id}?token={auth_token}"
                    dev_reg.async_update_device(
                        device_id=device.id,
                        configuration_url=new_config_url,
                    )
                    updated_count += 1
                    if ctx.get("redact_url_token"):
                        hass.logger().debug(
                            "Updated URL for device %s: %s",
                            device.name_by_user or device.name,
                            ctx["redact_url_token"](new_config_url),
                        )

        hass.logger().info("Refreshed URLs for %d Google Find My devices", updated_count)

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

        if isinstance(raw_ids, str):
            target_device_ids = {raw_ids}
        elif isinstance(raw_ids, (list, tuple, set)):
            target_device_ids = {str(x) for x in raw_ids}
        else:
            target_device_ids = set()

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
        entries = hass.config_entries.async_entries(DOMAIN)

        hass.logger().info(
            "googlefindmy.rebuild_registry requested: mode=%s, device_ids=%s",
            mode,
            "none"
            if not raw_ids
            else (raw_ids if isinstance(raw_ids, str) else f"{len(target_device_ids)} ids"),
        )

        if mode == MODE_MIGRATE:
            # soft migrate (data -> options) for all our entries
            for entry_ in entries:
                try:
                    # use the opt function to detect desired keys if needed; here we simply trigger
                    # the migration by re-saving options (performed in __init__.py flow normally).
                    # We reload entries below anyway to pick up changes.
                    pass
                except Exception as err:
                    hass.logger().error("Soft-migrate failed for entry %s: %s", entry_.entry_id, err)
            # Reload all entries to apply migrations
            for entry_ in entries:
                try:
                    await hass.config_entries.async_reload(entry_.entry_id)
                except Exception as err:
                    hass.logger().error("Reload failed for entry %s: %s", entry_.entry_id, err)
            hass.logger().info(
                "googlefindmy.rebuild_registry: soft-migrate completed for %d config entrie(s).",
                len(entries),
            )
            return

        if mode != MODE_REBUILD:
            hass.logger().error(
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
                    hass.logger().error("Failed to remove entity %s: %s", ent.entity_id, err)

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
                hass.logger().error("Failed to remove orphan entity %s: %s", ent.entity_id, err)

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
                    hass.logger().error("Failed to remove device %s: %s", dev_id, err)

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
                hass.logger().error("Reload failed for entry %s: %s", entry_.entry_id, err)

        hass.logger().info(
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
