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
from collections.abc import Iterable

from homeassistant import exceptions as ha_exceptions
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import device_registry as dr
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
)

_LOGGER = logging.getLogger(__name__)

ServiceValidationError = ha_exceptions.ServiceValidationError
HomeAssistantError = ha_exceptions.HomeAssistantError
ConfigEntryError = getattr(ha_exceptions, "ConfigEntryError", HomeAssistantError)


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
                "Invalid %s payload type: %s", ATTR_ENTRY_ID, type(entry_ids_from_service)
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
                _LOGGER.error(
                    "Error reloading config entry %s: %s", entry_id, err
                )

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
        DOMAIN, SERVICE_REBUILD_REGISTRY, async_rebuild_registry_service
    )
