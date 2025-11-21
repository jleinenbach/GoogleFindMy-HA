# custom_components/googlefindmy/entity.py
"""Common entity helpers for the Google Find My Device integration.

This module centralizes boilerplate shared across the integration's entity
platforms.  Use :class:`GoogleFindMyEntity` (or the per-device
:class:`GoogleFindMyDeviceEntity`) whenever a platform needs to expose an
entity backed by :class:`~custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator`.

Highlights for contributors:

* All entities share ``_attr_has_entity_name = True`` unless a subclass opts
  out (for example, the device tracker sets its own display name).
* Unique IDs should be generated via :meth:`GoogleFindMyEntity.join_parts`
  (or wrappers) so the canonical ``"<entry_id>:<subentry>:<...>"`` schema stays
  consistent across platforms.
* Device registry metadata (identifiers, ``DeviceInfo`` for the service device,
  and best-effort name synchronization) is provided here—avoid duplicating the
  logic in individual platforms.
* Per-device entities should inherit :class:`GoogleFindMyDeviceEntity` to gain
  the map-token helpers and label refresh utilities used by the button, sensor,
  and device_tracker platforms.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping, MutableMapping
from typing import TYPE_CHECKING, Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from .ha_typing import CoordinatorEntity, callback

if TYPE_CHECKING:
    from homeassistant.helpers.entity import Entity
else:  # pragma: no cover - fallback for environments with stubbed helpers
    try:
        from homeassistant.helpers.entity import Entity
    except (ImportError, AttributeError):

        class Entity:  # type: ignore[too-many-ancestors, override]
            """Minimal stand-in for Home Assistant's Entity base class."""

            __slots__ = ()

from .const import (
    CONF_GOOGLE_EMAIL,
    DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
    DOMAIN,
    INTEGRATION_VERSION,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    SERVICE_DEVICE_MANUFACTURER,
    SERVICE_DEVICE_MODEL,
    SERVICE_DEVICE_NAME,
    SERVICE_DEVICE_TRANSLATION_KEY,
    map_token_hex_digest,
    map_token_secret_seed,
    service_device_identifier,
)
from .coordinator import GoogleFindMyCoordinator

_LOGGER = logging.getLogger(__name__)


def resolve_coordinator(entry: ConfigEntry) -> GoogleFindMyCoordinator:
    """Return the coordinator stored on ``entry.runtime_data``.

    Raises ``HomeAssistantError`` if the coordinator is not ready yet.  All
    platforms use this helper to keep runtime-data handling consistent.
    """

    runtime = getattr(entry, "runtime_data", None)
    if isinstance(runtime, GoogleFindMyCoordinator):
        return runtime

    if runtime is not None:
        coordinator = getattr(runtime, "coordinator", None)
        if coordinator is not None:
            return cast(GoogleFindMyCoordinator, coordinator)

    raise HomeAssistantError("googlefindmy coordinator not ready")


def _entry_option(entry: ConfigEntry | None, key: str, default: Any) -> Any:
    """Read an entry option with data fallback (mirrors ``__init__._opt``)."""

    if entry is None:
        return default
    options = getattr(entry, "options", {})
    if isinstance(options, Mapping) and key in options:
        return options.get(key, default)
    data = getattr(entry, "data", {})
    if isinstance(data, Mapping):
        return data.get(key, default)
    return default


def ensure_config_subentry_id(
    entry: ConfigEntry, platform: str, candidate: str | None
) -> str | None:
    """Return a sanitized config_subentry_id or log why it is unavailable."""

    if isinstance(candidate, str):
        normalized = candidate.strip()
        if normalized:
            return normalized

    _LOGGER.warning(
        "[%s] %s platform deferred because config_subentry_id is unavailable; "
        "metadata may be stale or Home Assistant has not finished registering "
        "subentries yet.",
        getattr(entry, "entry_id", "<unknown>"),
        platform,
    )
    return None


def ensure_dispatcher_dependencies(hass: HomeAssistant) -> None:
    """Ensure dispatcher helpers can bind to the provided Home Assistant stub."""

    if not hasattr(hass, "data"):
        hass.data = {}
    if not hasattr(hass, "verify_event_loop_thread"):
        hass.verify_event_loop_thread = lambda *_args: None

    if not hasattr(hass, "async_run_hass_job"):
        hass.async_run_hass_job = _default_async_run_hass_job(hass)


def _default_async_run_hass_job(hass: HomeAssistant) -> Callable[..., Any]:
    """Return a dispatcher-compatible runner that schedules coroutines."""

    def _run(job: Any, *args: Any) -> Any:
        target = getattr(job, "target", None)
        callable_job = target if callable(target) else (job if callable(job) else None)
        if callable_job is None:
            return None

        result = callable_job(*args)
        if inspect.isawaitable(result):
            awaitable_result: Awaitable[Any] = result
            loop = getattr(hass, "loop", None)
            if loop is None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

            if inspect.iscoroutine(result):
                coroutine = cast(Coroutine[Any, Any, Any], result)
                if loop is not None:
                    return loop.create_task(coroutine)
                return asyncio.create_task(coroutine)

            if loop is not None:
                return asyncio.ensure_future(awaitable_result, loop=loop)

            return asyncio.ensure_future(awaitable_result)

        return result

    return _run


def schedule_add_entities(
    hass: HomeAssistant,
    async_add_entities: AddEntitiesCallback,
    *,
    entities: Iterable[Entity],
    update_before_add: bool = True,
    config_subentry_id: str | None = None,
    log_owner: str,
    logger: logging.Logger | None = None,
) -> None:
    """Call ``async_add_entities`` safely and await coroutine returns if needed."""

    entity_list = list(entities)

    kwargs: dict[str, Any] = {"update_before_add": update_before_add}
    result: Any = None

    if config_subentry_id is not None:
        kwargs["config_subentry_id"] = config_subentry_id

    try:
        result = async_add_entities(entity_list, **kwargs)
    except TypeError as err:
        if config_subentry_id is None or "config_subentry_id" not in str(err):
            raise
        log = logger or _LOGGER
        log.debug(
            "%s: AddEntitiesCallback rejected config_subentry_id; retrying without (error=%s)",
            log_owner,
            err,
        )
        result = async_add_entities(
            entity_list,
            update_before_add=update_before_add,
        )

    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            hass.async_create_task(cast(Coroutine[Any, Any, Any], result))
            return

        asyncio.ensure_future(result)


class GoogleFindMyEntity(CoordinatorEntity[GoogleFindMyCoordinator]):
    """Base entity for Google Find My Device platforms.

    Service-scoped diagnostics **must** pass ``SERVICE_SUBENTRY_KEY`` while
    per-device entities **must** pass ``TRACKER_SUBENTRY_KEY`` when invoking the
    constructor.  The coordinator and device-registry helpers assume every
    entity declares its subentry bucket explicitly so Home Assistant can group
    diagnostics under the shared service device and attach tracker entities to
    their dedicated device entries.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        *,
        subentry_key: str | None = None,
        subentry_identifier: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._subentry_key = subentry_key
        self._subentry_identifier = subentry_identifier

    async def async_added_to_hass(self) -> None:
        """Register coordinator listener and publish the initial state."""

        await super().async_added_to_hass()

        handler = getattr(self, "_handle_coordinator_update", None)
        if callable(handler):
            handler()
        else:
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Bridge coordinator updates even when stubs omit the helper."""

        parent_handler = getattr(super(), "_handle_coordinator_update", None)
        if callable(parent_handler):
            parent_handler()
            return

        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return coordinator availability with stub-friendly fallback."""

        try:
            return bool(getattr(super(), "available"))
        except AttributeError:
            pass
        except Exception as err:  # pragma: no cover - defensive fallback
            _LOGGER.debug("Coordinator availability probe failed for %s: %s", self.entity_id, err)

        for attr in ("last_update_success", "_last_update_success"):
            status = getattr(self.coordinator, attr, None)
            if isinstance(status, bool):
                return status

        return True

    @property
    def entry_id(self) -> str | None:
        """Return the config entry identifier, if known."""

        entry = getattr(self.coordinator, "config_entry", None)
        entry_id = getattr(entry, "entry_id", None)
        return entry_id if isinstance(entry_id, str) and entry_id else None

    @property
    def subentry_identifier(self) -> str | None:
        """Return the coordinator-provided subentry identifier (if set)."""

        return self._subentry_identifier

    @property
    def subentry_key(self) -> str | None:
        """Return the coordinator subentry key for this entity, if known."""

        return getattr(self, "_subentry_key", None)

    @staticmethod
    def join_parts(*parts: str | None, separator: str = ":") -> str:
        """Join truthy string parts with ``separator`` (skipping empty values)."""

        values = [part for part in parts if isinstance(part, str) and part]
        return separator.join(values)

    def build_unique_id(self, *parts: str | None, separator: str = ":") -> str:
        """Helper to compose the canonical unique_id string."""

        return self.join_parts(*parts, separator=separator)

    def service_device_info(
        self, *, include_subentry_identifier: bool = False
    ) -> DeviceInfo:
        """Return the ``DeviceInfo`` for the per-entry service device."""

        entry_id = self.entry_id
        effective_entry_id = entry_id or "default"
        identifiers: set[tuple[str, str]] = {
            service_device_identifier(effective_entry_id)
        }
        if include_subentry_identifier and self._subentry_identifier:
            identifiers.add(
                (
                    DOMAIN,
                    f"{effective_entry_id}:{self._subentry_identifier}:service",
                )
            )

        entry = getattr(self.coordinator, "config_entry", None)
        entry_title: str | None = None
        if entry is not None:
            raw_title = getattr(entry, "title", None)
            if isinstance(raw_title, str) and raw_title.strip():
                entry_title = raw_title.strip()
            else:
                raw_data = getattr(entry, "data", {})
                if isinstance(raw_data, Mapping):
                    email = raw_data.get(CONF_GOOGLE_EMAIL)
                    if isinstance(email, str) and email.strip():
                        entry_title = email.strip()

        service_name = f"{entry_title or SERVICE_DEVICE_NAME} – Service"

        info_kwargs: dict[str, Any] = {
            "identifiers": identifiers,
            "manufacturer": SERVICE_DEVICE_MANUFACTURER,
            "model": SERVICE_DEVICE_MODEL,
            "sw_version": INTEGRATION_VERSION,
            "configuration_url": "https://github.com/BSkando/GoogleFindMy-HA",
            "entry_type": dr.DeviceEntryType.SERVICE,
            "name": service_name,
            "translation_key": SERVICE_DEVICE_TRANSLATION_KEY,
            "translation_placeholders": {},
        }
        return DeviceInfo(**info_kwargs)

    def maybe_update_device_registry_name(self, new_name: str | None) -> None:
        """Best-effort registry name sync while respecting user overrides."""

        if not new_name or not self.entity_id:
            return

        try:
            ent_reg = er.async_get(self.hass)
            ent = ent_reg.async_get(self.entity_id)
            if not ent or not ent.device_id:
                return
            dev_reg = dr.async_get(self.hass)
            dev = dev_reg.async_get(ent.device_id)
        except Exception as err:  # pragma: no cover - defensive best effort
            _LOGGER.debug(
                "Device registry lookup failed for %s: %s", self.entity_id, err
            )
            return

        if not dev or dev.name_by_user or dev.name == new_name:
            return

        try:
            dev_reg.async_update_device(device_id=ent.device_id, name=new_name)
        except Exception as err:  # pragma: no cover - defensive best effort
            _LOGGER.debug(
                "Device registry update failed for %s (%s): %s",
                self.entity_id,
                ent.device_id,
                err,
            )


class GoogleFindMyDeviceEntity(GoogleFindMyEntity):
    """Base class for entities representing a concrete Google device.

    Callers must provide ``subentry_key=TRACKER_SUBENTRY_KEY`` so the entity can
    expose tracker-specific ``DeviceInfo`` identifiers.  Service diagnostics
    should continue using :class:`GoogleFindMyEntity` directly.
    """

    _DEFAULT_DEVICE_LABEL = "Google Find My Device"

    def __init__(
        self,
        coordinator: GoogleFindMyCoordinator,
        device: MutableMapping[str, Any],
        *,
        subentry_key: str,
        subentry_identifier: str,
        fallback_label: str | None = None,
    ) -> None:
        super().__init__(
            coordinator,
            subentry_key=subentry_key,
            subentry_identifier=subentry_identifier,
        )
        self._device = device
        self._subentry_key: str = subentry_key
        self._fallback_label = fallback_label

    @property
    def subentry_key(self) -> str:
        """Return the coordinator subentry key for this entity."""

        return self._subentry_key

    @property
    def device_id(self) -> str:
        """Return the Google device identifier (raises if missing)."""

        raw = self._device.get("id")
        if isinstance(raw, str) and raw:
            return raw
        raise ValueError("Device dictionary is missing a string 'id'")

    def device_label(self) -> str:
        """Return the best-available label for the device."""

        raw_name = self._device.get("name")
        if isinstance(raw_name, str):
            stripped = raw_name.strip()
            if stripped:
                return stripped

        if isinstance(self._fallback_label, str):
            stripped = self._fallback_label.strip()
            if stripped:
                return stripped

        fallback = self._device.get("device_id")
        if isinstance(fallback, str):
            stripped = fallback.strip()
            if stripped:
                return stripped

        raw_id = self._device.get("id")
        if isinstance(raw_id, str):
            stripped = raw_id.strip()
            if stripped:
                return stripped

        return self._DEFAULT_DEVICE_LABEL

    def _base_url(self) -> str:
        """Return the Home Assistant base URL (fallbacks to a safe default)."""

        try:
            return cast(
                str,
                get_url(
                    self.hass,
                    prefer_external=True,
                    allow_cloud=True,
                    allow_external=True,
                    allow_internal=True,
                ),
            )
        except HomeAssistantError as err:  # pragma: no cover - fallback
            _LOGGER.debug("Falling back to default base URL: %s", err)
            return "http://homeassistant.local:8123"

    def _get_map_token(self) -> str:
        """Generate a hardened map token (entry-scoped and optionally time-bound)."""

        config_entry = getattr(self.coordinator, "config_entry", None)

        token_expiration_enabled = bool(
            _entry_option(
                config_entry,
                OPT_MAP_VIEW_TOKEN_EXPIRATION,
                DEFAULT_MAP_VIEW_TOKEN_EXPIRATION,
            )
        )

        entry_id = getattr(config_entry, "entry_id", "") if config_entry else ""
        ha_uuid = str(getattr(self.hass, "data", {}).get("core.uuid", "ha"))
        if token_expiration_enabled:
            seed = map_token_secret_seed(
                ha_uuid,
                entry_id,
                True,
                now=int(time.time()),
            )
        else:
            seed = map_token_secret_seed(ha_uuid, entry_id, False)
        return map_token_hex_digest(seed)

    @staticmethod
    def _build_map_path(device_id: str, token: str, *, redirect: bool = False) -> str:
        """Return the path component for the map view endpoint."""

        suffix = "redirect_map" if redirect else "map"
        return f"/api/googlefindmy/{suffix}/{device_id}?token={token}"

    def device_configuration_url(self, *, redirect: bool = False) -> str:
        """Return a stable configuration URL for the device."""

        token = self._get_map_token()
        path = self._build_map_path(self.device_id, token, redirect=redirect)
        return f"{self._base_url()}{path}"

    def _device_identifiers(self) -> set[tuple[str, str]]:
        """Return the entry-scoped identifiers for this device."""

        entry_id = self.entry_id
        subentry_identifier = self.subentry_identifier or self._subentry_key
        identifiers: set[tuple[str, str]] = {
            (
                DOMAIN,
                self.join_parts(entry_id, subentry_identifier, self.device_id),
            )
        }
        if entry_id:
            identifiers.add((DOMAIN, f"{entry_id}:{self.device_id}"))
        else:
            identifiers.add((DOMAIN, self.device_id))
        return identifiers

    @property
    def device_info(self) -> DeviceInfo:
        """Return ``DeviceInfo`` describing the Google device."""

        label = self.device_label()
        name = label if label and label != self._DEFAULT_DEVICE_LABEL else None
        # Tracker devices intentionally omit ``via_device`` so Home Assistant
        # can attach them to the correct parent device automatically.

        return DeviceInfo(
            identifiers=self._device_identifiers(),
            manufacturer="Google",
            model="Find My Device",
            serial_number=self.device_id,
            configuration_url=self.device_configuration_url(),
            name=name,
        )

    def refresh_device_label_from_coordinator(
        self, *, log_prefix: str | None = None
    ) -> None:
        """Update the cached device label from the coordinator snapshot."""

        try:
            snapshot = self.coordinator.get_subentry_snapshot(self._subentry_key)
        except Exception as err:  # pragma: no cover - defensive best effort
            _LOGGER.debug("Failed to fetch snapshot for %s: %s", self.device_id, err)
            return

        for candidate in snapshot:
            if candidate.get("id") != self.device_id:
                continue
            new_name = candidate.get("name")
            if not isinstance(new_name, str) or not new_name.strip():
                break
            current = self._device.get("name")
            if current == new_name:
                break
            self._device["name"] = new_name
            self._fallback_label = new_name
            self.maybe_update_device_registry_name(new_name)
            if log_prefix:
                _LOGGER.debug(
                    "%s device label refreshed for %s: '%s' -> '%s'",
                    log_prefix,
                    self.device_id,
                    current,
                    new_name,
                )
            break

    def coordinator_has_device(self) -> bool:
        """Return ``True`` if the device is currently visible in the coordinator."""

        try:
            return bool(
                self.coordinator.is_device_visible_in_subentry(
                    self._subentry_key, self.device_id
                )
            )
        except Exception:  # pragma: no cover - defensive best effort
            return True
