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
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
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
                _LOGGER.error(
                    "Invalid 'device_ids' payload; expected list/tuple/set of device IDs (strings)."
                )
                return
        elif raw_ids is None:
            target_device_ids = set()
        else:
            _LOGGER.error(
                "Invalid 'device_ids' type: %s; expected string, list/tuple/set, or omitted.",
                type(raw_ids).__name__,
            )
            return

        dev_reg = dr.async_get(hass)
        ent_reg = er.async_get(hass)
        entries = hass.config_entries.async_entries(DOMAIN)

        affected_entry_ids: set[str]
        candidate_devices: set[str]

        coalesce_accounts = ctx.get("coalesce_account_entries")
        extract_email = ctx.get("extract_normalized_email")
        if callable(coalesce_accounts):
            processed_accounts: set[str] = set()
            for candidate in list(entries):
                entry_id = getattr(candidate, "entry_id", "<unknown>")
                normalized_email: str | None = None
                if callable(extract_email):
                    try:
                        normalized_email = extract_email(candidate)
                    except Exception as err:  # pragma: no cover - defensive logging
                        _LOGGER.debug(
                            "Failed to resolve normalized email for %s during rebuild: %s",
                            entry_id,
                            err,
                        )
                account_key = normalized_email or f"id:{entry_id}"
                if account_key in processed_accounts:
                    continue
                processed_accounts.add(account_key)
                try:
                    await coalesce_accounts(hass, canonical_entry=candidate)
                except Exception as err:
                    _LOGGER.error(
                        "googlefindmy.rebuild_registry: account deduplication failed for entry %s: %s",
                        entry_id,
                        err,
                    )
                    raise
            entries = hass.config_entries.async_entries(DOMAIN)

        _LOGGER.info(
            "googlefindmy.rebuild_registry requested: mode=%s, device_ids=%s",
            mode,
            "none"
            if not raw_ids
            else (
                raw_ids if isinstance(raw_ids, str) else f"{len(target_device_ids)} ids"
            ),
        )

        soft_migrate = ctx.get("soft_migrate_entry")
        unique_id_migrate = ctx.get("migrate_unique_ids")
        relink_button_devices = ctx.get("relink_button_devices")
        relink_subentry_entities = ctx.get("relink_subentry_entities")
        manager = getattr(hass, "config_entries", None)

        allowed_reload_states: set[ConfigEntryState] = {
            ConfigEntryState.LOADED,
            ConfigEntryState.NOT_LOADED,
            ConfigEntryState.SETUP_ERROR,
            ConfigEntryState.SETUP_RETRY,
            ConfigEntryState.SETUP_IN_PROGRESS,
        }

        failed_unload_state = getattr(ConfigEntryState, "FAILED_UNLOAD", None)
        if failed_unload_state is not None:
            allowed_reload_states.add(failed_unload_state)

        async def _recover_migration_error_entry(entry_: Any) -> tuple[bool, Any | None]:
            """Attempt to recover an entry from MIGRATION_ERROR and signal reload readiness."""

            migrate_entry_callable = None
            migrate_callable = None
            if manager is not None:
                migrate_entry_callable = getattr(manager, "async_migrate_entry", None)
                if not callable(migrate_entry_callable):
                    migrate_entry_callable = None
                if migrate_entry_callable is None:
                    migrate_callable = getattr(manager, "async_migrate", None)
                    if not callable(migrate_callable):
                        migrate_callable = None
            migration_supported = bool(migrate_entry_callable or migrate_callable)

            entry_id = getattr(entry_, "entry_id", "<unknown>")
            _LOGGER.warning(
                "Config entry %s is in migration error state; attempting soft migration instead of reload.",
                entry_id,
            )

            soft_completed = False
            if callable(soft_migrate):
                try:
                    await soft_migrate(hass, entry_)
                except Exception as err:
                    _LOGGER.error(
                        "Soft migration helper failed for entry %s: %s",
                        entry_id,
                        err,
                    )
                else:
                    soft_completed = True
            else:
                _LOGGER.warning(
                    "Soft migration helper unavailable; entry %s remains in migration error state until migration is resolved manually.",
                    entry_id,
                )

            if callable(unique_id_migrate):
                try:
                    await unique_id_migrate(hass, entry_)
                except Exception as err:
                    _LOGGER.error(
                        "Unique ID migration helper failed for entry %s: %s",
                        entry_id,
                        err,
                    )
            elif unique_id_migrate is None:
                _LOGGER.info(
                    "Unique ID migration helper not provided; entry %s skipped.",
                    entry_id,
                )
            else:
                _LOGGER.warning(
                    "Unique ID migration helper for entry %s is not callable; skipped.",
                    entry_id,
                )

            reasons: list[str] = []
            if soft_completed:
                reasons.append("soft migration helpers completed")

            if not migration_supported:
                reasons.append("Home Assistant migration API unavailable")
                refreshed_entry = _entry_for_id(hass, entry_.entry_id) or entry_
                joined_reasons = ", ".join(reasons) or "no helper progress"
                _LOGGER.warning(
                    (
                        "Config entry %s remains in migration error state; Home Assistant "
                        "cannot retry migration automatically (%s); manual migration required."
                    ),
                    entry_id,
                    joined_reasons,
                )
                return False, refreshed_entry

            migration_succeeded = False
            if callable(migrate_entry_callable):
                try:
                    migrate_result = await migrate_entry_callable(entry_)
                except ConfigEntryError as err:
                    _LOGGER.error(
                        "Retrying Home Assistant migration for entry %s failed: %s",
                        entry_id,
                        err,
                    )
                except Exception as err:  # pragma: no cover - defensive guard
                    _LOGGER.error(
                        "Unexpected error while migrating entry %s: %s",
                        entry_id,
                        err,
                    )
                else:
                    migration_succeeded = bool(migrate_result)
                    reasons.append("Home Assistant migration retried")
            elif callable(migrate_callable):
                try:
                    migrate_result = await migrate_callable(entry_.entry_id)
                except ConfigEntryError as err:
                    _LOGGER.error(
                        "Retrying Home Assistant migration for entry %s failed: %s",
                        entry_id,
                        err,
                    )
                except Exception as err:  # pragma: no cover - defensive guard
                    _LOGGER.error(
                        "Unexpected error while migrating entry %s: %s",
                        entry_id,
                        err,
                    )
                else:
                    migration_succeeded = bool(migrate_result)
                    reasons.append("Home Assistant migration retried")

            refreshed_entry = _entry_for_id(hass, entry_.entry_id) or entry_
            refreshed_state = getattr(refreshed_entry, "state", None)

            state_ready = False
            if refreshed_state == ConfigEntryState.MIGRATION_ERROR:
                pass
            elif refreshed_state is None:
                reasons.append("entry state unavailable after recovery attempt")
            elif refreshed_state in allowed_reload_states:
                state_ready = True
                state_name = getattr(refreshed_state, "name", str(refreshed_state))
                if not migration_succeeded:
                    reasons.append(f"entry state recovered ({state_name})")
            else:
                state_name = getattr(refreshed_state, "name", str(refreshed_state))
                reasons.append(f"entry state transitioned to {state_name}")

            should_reload = migration_succeeded or state_ready

            if should_reload:
                joined_reasons = ", ".join(reasons) or "no additional context"
                _LOGGER.info(
                    "Config entry %s recovered from migration error; queued for reload (%s).",
                    entry_id,
                    joined_reasons,
                )
                return True, refreshed_entry

            joined_reasons = ", ".join(reasons) or "no helper progress"
            _LOGGER.warning(
                "Config entry %s remains in migration error state after rebuild helpers; manual migration required (%s).",
                entry_id,
                joined_reasons,
            )
            return False, refreshed_entry

        def _purge_orphan_devices_for_entry(entry_: Any) -> int:
            """Remove orphaned devices for ``entry_`` that no longer have linked entities."""

            entry_id = getattr(entry_, "entry_id", None)
            if not isinstance(entry_id, str) or not entry_id:
                return 0

            try:
                devices_for_entry = dr.async_entries_for_config_entry(dev_reg, entry_id)
                entities_for_entry = er.async_entries_for_config_entry(ent_reg, entry_id)
            except Exception as err:  # pragma: no cover - defensive guard
                _LOGGER.debug(
                    "Device/entity registry scan failed for entry %s during orphan purge: %s",
                    entry_id,
                    err,
                )
                return 0

            linked_device_ids: set[str] = {
                ent.device_id
                for ent in entities_for_entry
                if getattr(ent, "device_id", None)
            }

            removed = 0
            for device in list(devices_for_entry):
                if device.id in linked_device_ids:
                    continue
                if _is_service_device(device) or not _dev_is_ours(device):
                    continue
                try:
                    dev_reg.async_remove_device(device.id)
                except Exception as err:  # pragma: no cover - defensive guard
                    _LOGGER.error(
                        "Failed to remove orphan device %s for entry %s: %s",
                        getattr(device, "id", "<unknown>"),
                        entry_id,
                        err,
                    )
                    continue
                removed += 1
            return removed

        if mode == MODE_MIGRATE:
            # C-2: real soft-migrate path using __init__.py helpers via ctx

            if not callable(soft_migrate):
                _LOGGER.warning(
                    "soft_migrate_entry not provided in context; data→options migration will be skipped."
                )
            if not callable(unique_id_migrate):
                _LOGGER.warning(
                    "migrate_unique_ids not provided in context; unique-id migration will be skipped."
                )
            if not callable(relink_button_devices):
                _LOGGER.warning(
                    "relink_button_devices not provided in context; button relinking will be skipped."
                )
            if not callable(relink_subentry_entities):
                _LOGGER.warning(
                    "relink_subentry_entities not provided in context; tracker/service relinking will be skipped."
                )

            soft_completed = 0
            unique_completed = 0
            relink_completed = 0
            subentry_relink_completed = 0

            for entry_ in entries:
                if callable(soft_migrate):
                    try:
                        await soft_migrate(hass, entry_)
                    except Exception as err:
                        _LOGGER.error(
                            "Soft-migrate failed for entry %s: %s", entry_.entry_id, err
                        )
                    else:
                        soft_completed += 1

                if callable(unique_id_migrate):
                    try:
                        await unique_id_migrate(hass, entry_)
                    except Exception as err:
                        _LOGGER.error(
                            "Unique-ID migration failed for entry %s: %s",
                            entry_.entry_id,
                            err,
                        )
                    else:
                        unique_completed += 1

                if callable(relink_button_devices):
                    try:
                        await relink_button_devices(hass, entry_)
                    except Exception as err:
                        _LOGGER.error(
                            "Button relink failed for entry %s: %s",
                            entry_.entry_id,
                            err,
                        )
                    else:
                        relink_completed += 1

                if callable(relink_subentry_entities):
                    try:
                        await relink_subentry_entities(hass, entry_)
                    except Exception as err:
                        _LOGGER.error(
                            "Tracker/service relink failed for entry %s: %s",
                            entry_.entry_id,
                            err,
                        )
                    else:
                        subentry_relink_completed += 1

            # Reload all entries to apply migrations
            entries_to_reload: list[Any] = []
            migrate_skipped_states: list[tuple[Any, str | ConfigEntryState | None]] = []
            for entry_ in entries:
                state = getattr(entry_, "state", None)
                if state == ConfigEntryState.MIGRATION_ERROR:
                    should_reload, refreshed_entry = await _recover_migration_error_entry(
                        entry_
                    )
                    if should_reload and refreshed_entry is not None:
                        entries_to_reload.append(refreshed_entry)
                    continue
                if state is None or state in allowed_reload_states:
                    entries_to_reload.append(entry_)
                    continue
                migrate_skipped_states.append((entry_, state))

            if migrate_skipped_states:
                for entry_, state in migrate_skipped_states:
                    _LOGGER.info(
                        "Skipping reload for entry %s in unsupported state %s.",
                        getattr(entry_, "entry_id", "<unknown>"),
                        state,
                    )

            for entry_ in entries_to_reload:
                try:
                    await hass.config_entries.async_reload(entry_.entry_id)
                except Exception as err:
                    _LOGGER.error(
                        "Reload failed for entry %s: %s", entry_.entry_id, err
                    )

            _LOGGER.info(
                "googlefindmy.rebuild_registry: migrate completed for %d config entrie(s) (data/options=%d, unique_ids=%d, button_relinks=%d, subentry_relinks=%d)",
                len(entries),
                soft_completed,
                unique_completed,
                relink_completed,
                subentry_relink_completed,
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
            entry_ids = getattr(dev, "config_entries", None)
            if not entry_ids:
                return False
            owns_any = False
            for eid in entry_ids:
                entry_obj = hass.config_entries.async_get_entry(eid)
                if entry_obj is None or entry_obj.domain != DOMAIN:
                    return False
                owns_any = True
            if owns_any:
                return True
            identifiers = getattr(dev, "identifiers", ())
            if not isinstance(identifiers, Iterable):
                return False
            for candidate in identifiers:
                try:
                    domain, _ = candidate
                except (TypeError, ValueError):
                    continue
                if domain == DOMAIN:
                    return True
            return False

        def _is_service_device(dev: dr.DeviceEntry) -> bool:
            # service device can be 'integration' or entry-scoped name
            if any(
                domain == DOMAIN and ident == "integration"
                for domain, ident in dev.identifiers
            ):
                return True
            return any(
                domain == DOMAIN and str(ident).startswith("integration_")
                for domain, ident in dev.identifiers
            )

        # 1) Determine candidate devices (strictly ours), optionally filtered by passed HA device_ids.
        if target_device_ids:
            _LOGGER.debug(
                "Scoped registry rebuild requested for %d device(s)",
                len(target_device_ids),
            )
            affected_entry_ids = set()
            candidate_devices = set()
            for d in target_device_ids:
                dev = dev_reg.async_get(d)
                if dev and _dev_is_ours(dev):
                    candidate_devices.add(dev.id)
                    affected_entry_ids.update(dev.config_entries)

            for ent in list(ent_reg.entities.values()):
                if getattr(ent, "platform", None) != DOMAIN:
                    continue
                device_id = getattr(ent, "device_id", None)
                if isinstance(device_id, str) and device_id in target_device_ids:
                    candidate_devices.add(device_id)
                    entry_id = getattr(ent, "config_entry_id", None)
                    if isinstance(entry_id, str) and entry_id:
                        affected_entry_ids.add(entry_id)
        else:
            _LOGGER.debug(
                "Global registry rebuild requested; scanning all Google Find My entities/devices",
            )
            affected_entry_ids = set()
            candidate_devices = set()
            for ent in list(ent_reg.entities.values()):
                if getattr(ent, "platform", None) != DOMAIN:
                    continue
                device_id = getattr(ent, "device_id", None)
                if isinstance(device_id, str) and device_id:
                    candidate_devices.add(device_id)
                entry_id = getattr(ent, "config_entry_id", None)
                if isinstance(entry_id, str) and entry_id:
                    affected_entry_ids.add(entry_id)

            for dev in dev_reg.devices.values():
                if _dev_is_ours(dev):
                    candidate_devices.add(dev.id)
                    affected_entry_ids.update(dev.config_entries)

        entries_by_id: dict[str, Any] = {
            getattr(entry_, "entry_id", ""): entry_
            for entry_ in entries
            if isinstance(getattr(entry_, "entry_id", None), str)
        }

        if affected_entry_ids:
            prep_entries = [
                entries_by_id[entry_id]
                for entry_id in affected_entry_ids
                if entry_id in entries_by_id
            ]
        else:
            prep_entries = list(entries_by_id.values())

        prepared_for_setup: set[str] = set()

        for entry_ in prep_entries:
            entry_id = getattr(entry_, "entry_id", None)
            if not isinstance(entry_id, str) or not entry_id:
                continue
            state = getattr(entry_, "state", None)
            if state == ConfigEntryState.MIGRATION_ERROR:
                continue
            try:
                unloaded = await hass.config_entries.async_unload(entry_id)
            except Exception as err:
                _LOGGER.error(
                    "Failed to unload entry %s prior to registry rebuild: %s",
                    entry_id,
                    err,
                )
                continue
            if unloaded or state in (
                ConfigEntryState.NOT_LOADED,
                ConfigEntryState.SETUP_ERROR,
                ConfigEntryState.SETUP_RETRY,
            ):
                prepared_for_setup.add(entry_id)

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
                _LOGGER.error(
                    "Failed to remove orphan entity %s: %s", ent.entity_id, err
                )

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

        orphan_device_removals = 0
        for entry_ in entries:
            purged = _purge_orphan_devices_for_entry(entry_)
            if purged:
                orphan_device_removals += purged
                entry_id = getattr(entry_, "entry_id", None)
                if isinstance(entry_id, str) and entry_id:
                    affected_entry_ids.add(entry_id)

        removed_devices += orphan_device_removals

        # 5) Reload entries. If only orphan entities were removed and we didn't touch any known devices,
        #    reload all entries of our domain to be safe.
        if orphan_only_cleanup and not affected_entry_ids:
            to_reload = list(entries)
        else:
            to_reload = [
                e for e in entries if e.entry_id in affected_entry_ids
            ] or list(entries)

        reloadable_entries: list[Any] = []
        skipped_states: list[tuple[Any, str | ConfigEntryState | None]] = []

        for entry_ in to_reload:
            state = getattr(entry_, "state", None)
            if state == ConfigEntryState.MIGRATION_ERROR:
                should_reload, refreshed_entry = await _recover_migration_error_entry(
                    entry_
                )
                if should_reload and refreshed_entry is not None:
                    reloadable_entries.append(refreshed_entry)
                continue
            if state is None or state in allowed_reload_states:
                reloadable_entries.append(entry_)
                continue
            skipped_states.append((entry_, state))

        if skipped_states:
            for entry_, state in skipped_states:
                _LOGGER.info(
                    "Skipping reload for entry %s in unsupported state %s.",
                    getattr(entry_, "entry_id", "<unknown>"),
                    state,
                )

        setups_started = 0
        reloads_started = 0

        for entry_ in reloadable_entries:
            entry_id = getattr(entry_, "entry_id", None)
            if not isinstance(entry_id, str) or not entry_id:
                continue
            try:
                if entry_id in prepared_for_setup:
                    await hass.config_entries.async_setup(entry_id)
                    setups_started += 1
                else:
                    await hass.config_entries.async_reload(entry_id)
                    reloads_started += 1
            except Exception as err:
                _LOGGER.error("Reload failed for entry %s: %s", entry_id, err)

        _LOGGER.info(
            (
                "googlefindmy.rebuild_registry: finished (safe mode): removed %d "
                "entit(y/ies), %d device(s), entries restarted=%d (setup=%d, reload=%d)"
            ),
            removed_entities,
            removed_devices,
            len(reloadable_entries),
            setups_started,
            reloads_started,
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
