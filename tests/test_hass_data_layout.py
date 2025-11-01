# tests/test_hass_data_layout.py
"""Regression tests for the hass.data layout used by the integration."""

from __future__ import annotations

import asyncio
from datetime import datetime

import importlib
import json
import logging
import sys

from contextlib import suppress
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any
from collections.abc import Awaitable, Callable, Mapping
from unittest.mock import AsyncMock, call

import pytest

from custom_components.googlefindmy.const import (
    ATTR_MODE,
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    MODE_MIGRATE,
    SERVICE_LOCATE_DEVICE,
    SERVICE_REBUILD_REGISTRY,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
)
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.exceptions import ServiceValidationError

if TYPE_CHECKING:
    from custom_components.googlefindmy import RuntimeData


class _StubCache:
    """Lightweight token cache stub used for setup tests."""

    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.closed = False

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self.values[key] = value

    async def flush(self) -> None:  # pragma: no cover - compatibility hook
        return None

    async def close(self) -> None:
        self.closed = True


class _StubConfigEntry:
    """Minimal ConfigEntry-like stub capturing unload callbacks."""

    def __init__(self) -> None:
        self.entry_id: str = "entry-test"
        self.data: dict[str, Any] = {
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
        }
        self.options: dict[str, Any] = {}
        self.title: str = "Test Entry"
        self.runtime_data: RuntimeData | None = None
        self.subentries: dict[str, ConfigSubentry] = {}
        self.state: ConfigEntryState = ConfigEntryState.LOADED
        self.disabled_by: str | None = None
        self._unload_callbacks: list[Callable[[], None]] = []
        self.updated_at = datetime(2024, 1, 1, 0, 0, 0)
        self.created_at = datetime(2024, 1, 1, 0, 0, 0)

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self._unload_callbacks.append(callback)


class _StubBus:
    """Event bus stub providing async_listen_once."""

    def async_listen_once(
        self, _event: str, _callback: Callable[..., Any]
    ) -> Callable[[], None]:
        return lambda: None


class _StubHttp:
    """Stub HTTP component capturing registered views."""

    def __init__(self) -> None:
        self.registered: list[Any] = []

    def register_view(self, view: Any) -> None:
        self.registered.append(view)


class _StubConfigEntries:
    """Minimal config_entries manager stub."""

    def __init__(self, entry: _StubConfigEntry) -> None:
        self._entries: list[_StubConfigEntry] = [entry]
        self.forward_calls: list[tuple[_StubConfigEntry, tuple[str, ...]]] = []
        self.reload_calls: list[str] = []
        self.added_subentries: list[tuple[_StubConfigEntry, ConfigSubentry]] = []
        self.updated_subentries: list[tuple[_StubConfigEntry, ConfigSubentry]] = []
        self.removed_subentries: list[tuple[_StubConfigEntry, str]] = []
        self.entry_update_calls: list[tuple[_StubConfigEntry, dict[str, Any]]] = []
        self.unload_calls: list[str] = []

    def async_entries(self, _domain: str) -> list[_StubConfigEntry]:
        return list(self._entries)

    async def async_forward_entry_setups(
        self, entry: _StubConfigEntry, platforms: list[str]
    ) -> None:
        self.forward_calls.append((entry, tuple(platforms)))

    async def async_unload_platforms(
        self, entry: _StubConfigEntry, _platforms: list[str]
    ) -> bool:
        return True

    def async_add_subentry(
        self, entry: _StubConfigEntry, subentry: ConfigSubentry
    ) -> bool:
        entry.subentries[subentry.subentry_id] = subentry
        self.added_subentries.append((entry, subentry))
        return True

    def async_update_subentry(
        self,
        entry: _StubConfigEntry,
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any] | None = None,
        title: str | None = None,
        unique_id: str | None = None,
    ) -> bool:
        changed = False
        if data is not None:
            subentry.data = MappingProxyType(dict(data))
            changed = True
        if title is not None and subentry.title != title:
            subentry.title = title
            changed = True
        if unique_id is not None and subentry.unique_id != unique_id:
            subentry.unique_id = unique_id
            changed = True
        entry.subentries[subentry.subentry_id] = subentry
        self.updated_subentries.append((entry, subentry))
        return changed

    def async_remove_subentry(self, entry: _StubConfigEntry, subentry_id: str) -> bool:
        entry.subentries.pop(subentry_id, None)
        self.removed_subentries.append((entry, subentry_id))
        return True

    def async_update_entry(self, entry: _StubConfigEntry, **kwargs: Any) -> None:
        self.entry_update_calls.append((entry, dict(kwargs)))

        options = kwargs.get("options")
        if isinstance(options, Mapping):
            entry.options = dict(options)

        data = kwargs.get("data")
        if isinstance(data, Mapping):
            entry.data = dict(data)

        title = kwargs.get("title")
        if isinstance(title, str):
            entry.title = title

        unique_id = kwargs.get("unique_id")
        if isinstance(unique_id, str):
            setattr(entry, "unique_id", unique_id)

        if "disabled_by" in kwargs:
            entry.disabled_by = kwargs["disabled_by"]

        version_value = kwargs.get("version")
        if isinstance(version_value, int):
            entry.version = version_value

    async def async_reload(self, entry_id: str) -> None:
        self.reload_calls.append(entry_id)

    async def async_unload(self, entry_id: str) -> bool:
        self.unload_calls.append(entry_id)
        return True


class _StubServices:
    """Capture service registrations and expose them to tests."""

    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], Callable[..., Any]] = {}

    def async_register(
        self, domain: str, service: str, handler: Callable[..., Any]
    ) -> None:
        self.registered[(domain, service)] = handler


class _StubHass:
    """Home Assistant core stub with just enough surface for setup."""

    def __init__(
        self, entry: _StubConfigEntry, loop: asyncio.AbstractEventLoop
    ) -> None:
        from homeassistant.core import CoreState

        self.data: dict[str, Any] = {DOMAIN: {}, "core.uuid": "ha-uuid"}
        self.loop = loop
        self.state = CoreState.running
        self.bus = _StubBus()
        self.http = _StubHttp()
        self.config_entries = _StubConfigEntries(entry)
        self._tasks: list[asyncio.Task[Any]] = []
        self.services = _StubServices()

    def async_create_task(
        self, coro: Awaitable[Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = self.loop.create_task(coro, name=name)
        self._tasks.append(task)
        return task

    async def async_add_executor_job(self, func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)


 
def test_service_stats_unique_id_migration_prefers_service_subentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracker-prefixed stats sensor IDs collapse to the service identifier."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        entry = _StubConfigEntry()
        entry.entry_id = "entry-test"

        tracker_subentry = ConfigSubentry(
            data={
                "group_key": TRACKER_SUBENTRY_KEY,
                "features": ("device_tracker", "sensor"),
            },
            subentry_type=SUBENTRY_TYPE_TRACKER,
            title="Devices",
            unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}",
            subentry_id="tracker-subentry",
        )
        service_subentry = ConfigSubentry(
            data={
                "group_key": SERVICE_SUBENTRY_KEY,
                "features": ("binary_sensor",),
            },
            subentry_type=SUBENTRY_TYPE_SERVICE,
            title="Service",
            unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
            subentry_id="service-subentry",
        )
        entry.subentries = {
            tracker_subentry.subentry_id: tracker_subentry,
            service_subentry.subentry_id: service_subentry,
        }

        hass = _StubHass(entry, loop)

        class _RegistryStub:
            def __init__(self) -> None:
                self.entities: dict[str, SimpleNamespace] = {}
                self._by_key: dict[tuple[str, str, str], str] = {}
                self.updated: list[str] = []

            def add(
                self,
                *,
                entity_id: str,
                domain: str,
                platform: str,
                unique_id: str,
                config_entry_id: str,
            ) -> None:
                entry_obj = SimpleNamespace(
                    entity_id=entity_id,
                    domain=domain,
                    platform=platform,
                    unique_id=unique_id,
                    config_entry_id=config_entry_id,
                )
                self.entities[entity_id] = entry_obj
                self._by_key[(domain, platform, unique_id)] = entity_id

            def async_get_entity_id(
                self, domain: str, platform: str, unique_id: str
            ) -> str | None:
                return self._by_key.get((domain, platform, unique_id))

            def async_update_entity(
                self,
                entity_id: str,
                *,
                new_unique_id: str | None = None,
                **_: Any,
            ) -> None:
                entry_obj = self.entities[entity_id]
                if new_unique_id:
                    self._by_key.pop(
                        (entry_obj.domain, entry_obj.platform, entry_obj.unique_id),
                        None,
                    )
                    entry_obj.unique_id = new_unique_id
                    self._by_key[(entry_obj.domain, entry_obj.platform, new_unique_id)] = (
                        entity_id
                    )
                self.updated.append(entity_id)

        class _DeviceRegistryStub:
            def __init__(self) -> None:
                self.devices: dict[str, Any] = {}

            def async_update_device(self, **_: Any) -> None:  # pragma: no cover - stub
                return None

        entity_registry = _RegistryStub()
        entity_registry.add(
            entity_id="sensor.googlefindmy_api_updates",
            domain="sensor",
            platform=integration.DOMAIN,
            unique_id=(
                f"{integration.DOMAIN}_{entry.entry_id}_"
                f"{tracker_subentry.subentry_id}_{service_subentry.subentry_id}_api_updates_total"
            ),
            config_entry_id=entry.entry_id,
        )
        device_registry = _DeviceRegistryStub()

        monkeypatch.setattr(
            integration.er, "async_get", lambda _hass: entity_registry
        )
        monkeypatch.setattr(
            integration.dr, "async_get", lambda _hass: device_registry
        )

        loop.run_until_complete(
            integration._async_migrate_unique_ids(hass, entry)
        )

        migrated = entity_registry.entities["sensor.googlefindmy_api_updates"]
        assert migrated.unique_id == (
            f"{integration.DOMAIN}_{entry.entry_id}_"
            f"{service_subentry.subentry_id}_api_updates_total"
        )
        assert entity_registry.updated == ["sensor.googlefindmy_api_updates"]
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
            with suppress(Exception):
                loop.run_until_complete(task)
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)


def test_hass_data_layout(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The integration stores runtime state only under hass.data[DOMAIN]["entries"]."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        if "homeassistant.components.button" not in sys.modules:
            homeassistant_root = sys.modules.get("homeassistant")
            if homeassistant_root is None:
                homeassistant_root = ModuleType("homeassistant")
                homeassistant_root.__path__ = []  # type: ignore[attr-defined]
                sys.modules["homeassistant"] = homeassistant_root

            components_pkg = sys.modules.get("homeassistant.components")
            if components_pkg is None:
                components_pkg = ModuleType("homeassistant.components")
                components_pkg.__path__ = []  # type: ignore[attr-defined]
                sys.modules["homeassistant.components"] = components_pkg

            helpers_pkg = sys.modules.get("homeassistant.helpers")
            if helpers_pkg is None:
                helpers_pkg = ModuleType("homeassistant.helpers")
                helpers_pkg.__path__ = []  # type: ignore[attr-defined]
                sys.modules["homeassistant.helpers"] = helpers_pkg

            setattr(homeassistant_root, "components", components_pkg)
            setattr(homeassistant_root, "helpers", helpers_pkg)

            button_component = ModuleType("homeassistant.components.button")

            class _ButtonEntity:  # pragma: no cover - structural stub
                pass

            class _ButtonEntityDescription:  # pragma: no cover - structural stub
                def __init__(self, **kwargs: Any) -> None:
                    for key, value in kwargs.items():
                        setattr(self, key, value)

            button_component.ButtonEntity = _ButtonEntity
            button_component.ButtonEntityDescription = _ButtonEntityDescription
            sys.modules["homeassistant.components.button"] = button_component
            setattr(components_pkg, "button", button_component)

        if "homeassistant.components.http" not in sys.modules:
            http_component = ModuleType("homeassistant.components.http")

            class _HomeAssistantView:  # pragma: no cover - structural stub
                url: str = ""
                name: str = ""
                requires_auth = False

                def __init__(self, hass=None) -> None:
                    self.hass = hass

            http_component.HomeAssistantView = _HomeAssistantView
            sys.modules["homeassistant.components.http"] = http_component
            components_pkg = sys.modules.get("homeassistant.components")
            if components_pkg is not None:
                setattr(components_pkg, "http", http_component)

        if "homeassistant.loader" not in sys.modules:
            homeassistant_root = sys.modules.setdefault(
                "homeassistant", ModuleType("homeassistant")
            )
            if not hasattr(homeassistant_root, "__path__"):
                homeassistant_root.__path__ = []  # type: ignore[attr-defined]
            loader_module = ModuleType("homeassistant.loader")

            async def _async_get_integration(
                _hass: Any, _domain: str
            ) -> SimpleNamespace:
                return SimpleNamespace(name="googlefindmy", version="0.0.0")

            loader_module.async_get_integration = _async_get_integration
            sys.modules["homeassistant.loader"] = loader_module
            setattr(homeassistant_root, "loader", loader_module)

        helpers_pkg = sys.modules.setdefault(
            "homeassistant.helpers", ModuleType("homeassistant.helpers")
        )
        if not hasattr(helpers_pkg, "__path__"):
            helpers_pkg.__path__ = []  # type: ignore[attr-defined]
        entity_module = sys.modules.get("homeassistant.helpers.entity")
        if entity_module is None:
            entity_module = ModuleType("homeassistant.helpers.entity")
            sys.modules["homeassistant.helpers.entity"] = entity_module
            setattr(helpers_pkg, "entity", entity_module)

        if not hasattr(entity_module, "DeviceInfo"):

            class _DeviceInfo:
                def __init__(self, **kwargs: Any) -> None:
                    for key, value in kwargs.items():
                        setattr(self, key, value)

            entity_module.DeviceInfo = _DeviceInfo

        entity_platform_module = sys.modules.get(
            "homeassistant.helpers.entity_platform"
        )
        if entity_platform_module is None:
            entity_platform_module = ModuleType("homeassistant.helpers.entity_platform")
            sys.modules["homeassistant.helpers.entity_platform"] = (
                entity_platform_module
            )
            setattr(helpers_pkg, "entity_platform", entity_platform_module)

        if not hasattr(entity_platform_module, "AddEntitiesCallback"):
            entity_platform_module.AddEntitiesCallback = Callable[[list[Any]], None]

        helpers_pkg = sys.modules.setdefault(
            "homeassistant.helpers", ModuleType("homeassistant.helpers")
        )
        if not hasattr(helpers_pkg, "__path__"):
            helpers_pkg.__path__ = []  # type: ignore[attr-defined]
        update_coordinator_module = sys.modules.get(
            "homeassistant.helpers.update_coordinator"
        )
        if update_coordinator_module is None:
            update_coordinator_module = ModuleType(
                "homeassistant.helpers.update_coordinator"
            )
            sys.modules["homeassistant.helpers.update_coordinator"] = (
                update_coordinator_module
            )
            setattr(helpers_pkg, "update_coordinator", update_coordinator_module)

        if not hasattr(update_coordinator_module, "CoordinatorEntity"):

            class _CoordinatorEntity:
                def __init__(self, coordinator: Any | None = None) -> None:
                    self.coordinator = coordinator

            update_coordinator_module.CoordinatorEntity = _CoordinatorEntity

        if not hasattr(update_coordinator_module, "DataUpdateCoordinator"):

            class _DataUpdateCoordinator:
                def __init__(
                    self,
                    hass: Any,
                    logger: Any | None = None,
                    *,
                    name: str | None = None,
                    update_interval: Any | None = None,
                ) -> None:  # noqa: D401 - stub signature
                    self.hass = hass
                    self.logger = logger
                    self.name = name
                    self.update_interval = update_interval

                async def async_config_entry_first_refresh(self) -> None:
                    return None

            update_coordinator_module.DataUpdateCoordinator = _DataUpdateCoordinator

        if not hasattr(update_coordinator_module, "UpdateFailed"):

            class _UpdateFailed(Exception):
                pass

            update_coordinator_module.UpdateFailed = _UpdateFailed

        integration = importlib.import_module("custom_components.googlefindmy")
        config_entries_module = importlib.import_module("homeassistant.config_entries")
        state_cls = config_entries_module.ConfigEntryState
        if not hasattr(state_cls, "SETUP_IN_PROGRESS"):
            setattr(state_cls, "SETUP_IN_PROGRESS", "setup_in_progress")
        if not hasattr(state_cls, "SETUP_RETRY"):
            setattr(state_cls, "SETUP_RETRY", "setup_retry")
        coordinator_module = importlib.import_module(
            "custom_components.googlefindmy.coordinator"
        )
        button_module = importlib.import_module("custom_components.googlefindmy.button")
        sys.modules.pop("custom_components.googlefindmy.map_view", None)
        map_view_module = importlib.import_module(
            "custom_components.googlefindmy.map_view"
        )

        config_entries_module = importlib.import_module("homeassistant.config_entries")
        state_cls = config_entries_module.ConfigEntryState
        if not hasattr(state_cls, "SETUP_IN_PROGRESS"):
            setattr(state_cls, "SETUP_IN_PROGRESS", "setup_in_progress")
        if not hasattr(state_cls, "SETUP_RETRY"):
            setattr(state_cls, "SETUP_RETRY", "setup_retry")

        cache = _StubCache()
        monkeypatch.setattr(
            integration.TokenCache, "create", AsyncMock(return_value=cache)
        )
        monkeypatch.setattr(integration, "_register_instance", lambda *_: None)
        monkeypatch.setattr(integration, "_unregister_instance", lambda *_: cache)
        monkeypatch.setattr(
            integration, "_async_soft_migrate_data_to_options", AsyncMock()
        )
        monkeypatch.setattr(integration, "_async_migrate_unique_ids", AsyncMock())
        monkeypatch.setattr(
            integration, "_async_relink_button_devices", AsyncMock()
        )
        monkeypatch.setattr(integration, "_async_save_secrets_data", AsyncMock())
        monkeypatch.setattr(integration, "_async_seed_manual_credentials", AsyncMock())
        monkeypatch.setattr(integration, "_async_normalize_device_names", AsyncMock())
        monkeypatch.setattr(
            integration, "_async_release_shared_fcm", AsyncMock(return_value=None)
        )

        class _RegisterViewStub:
            def __init__(self, hass: Any) -> None:
                self.hass = hass

        monkeypatch.setattr(integration, "GoogleFindMyMapView", _RegisterViewStub)
        monkeypatch.setattr(
            integration, "GoogleFindMyMapRedirectView", _RegisterViewStub
        )

        dummy_fcm = SimpleNamespace(
            register_coordinator=lambda *_: None,
            unregister_coordinator=lambda *_: None,
            _start_listening=AsyncMock(return_value=None),
            request_stop=lambda: None,
        )
        monkeypatch.setattr(
            integration,
            "_async_acquire_shared_fcm",
            AsyncMock(return_value=dummy_fcm),
        )

        coordinator_cls = stub_coordinator_factory()
        # Ensure isinstance checks in platform modules resolve to the stub coordinator.
        monkeypatch.setattr(
            coordinator_module, "GoogleFindMyCoordinator", coordinator_cls
        )
        monkeypatch.setattr(integration, "GoogleFindMyCoordinator", coordinator_cls)
        monkeypatch.setattr(button_module, "GoogleFindMyCoordinator", coordinator_cls)
        monkeypatch.setattr(
            map_view_module,
            "GoogleFindMyCoordinator",
            coordinator_cls,
            raising=False,
        )

        entry = _StubConfigEntry()
        hass = _StubHass(entry, loop)

        # Recorder history module stub required by the map view handler.
        history_module = ModuleType("homeassistant.components.recorder.history")
        history_module.get_significant_states = lambda *_args, **_kwargs: {}
        sys.modules["homeassistant.components.recorder.history"] = history_module

        async def _exercise() -> None:
            assert await integration.async_setup(hass, {}) is True
            setup_ok = await integration.async_setup_entry(hass, entry)
            assert setup_ok is True

            if hass._tasks:
                await asyncio.gather(*hass._tasks)

            domain_bucket = hass.data[DOMAIN]
            assert entry.entry_id not in domain_bucket
            runtime_bucket = domain_bucket["entries"]
            assert entry.entry_id in runtime_bucket

            runtime_data = runtime_bucket[entry.entry_id]
            assert runtime_data is entry.runtime_data
            assert isinstance(runtime_data, integration.RuntimeData)
            assert runtime_data.coordinator is entry.runtime_data.coordinator
            assert runtime_data.token_cache is cache
            assert runtime_data.cache is cache
            assert runtime_data.subentry_manager is not None

            subentry_manager = runtime_data.subentry_manager
            managed = subentry_manager.managed_subentries
            assert TRACKER_SUBENTRY_KEY in managed
            assert SERVICE_SUBENTRY_KEY in managed
            service_subentry = managed[SERVICE_SUBENTRY_KEY]
            core_subentry = managed[TRACKER_SUBENTRY_KEY]
            assert core_subentry.data["group_key"] == TRACKER_SUBENTRY_KEY
            tracker_features = core_subentry.data["features"]
            assert tracker_features == sorted(TRACKER_FEATURE_PLATFORMS)
            assert all(isinstance(feature, str) for feature in tracker_features)
            assert all(feature == feature.lower() for feature in tracker_features)
            assert core_subentry.data["fcm_push_enabled"] is True
            assert core_subentry.data["has_google_home_filter"] is False
            assert core_subentry.unique_id.endswith(TRACKER_SUBENTRY_KEY)

            assert service_subentry.data["group_key"] == SERVICE_SUBENTRY_KEY
            service_features = service_subentry.data["features"]
            assert service_features == sorted(SERVICE_FEATURE_PLATFORMS)
            assert all(isinstance(feature, str) for feature in service_features)
            assert all(feature == feature.lower() for feature in service_features)
            assert service_subentry.data["fcm_push_enabled"] is True
            assert service_subentry.data["has_google_home_filter"] is False
            assert service_subentry.unique_id == f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"

            added_entities: list[Any] = []

            def _collect_entities(
                entities: list[Any], _update_before_add: bool = False
            ) -> None:
                added_entities.extend(entities)

            await button_module.async_setup_entry(hass, entry, _collect_entities)
            assert len(added_entities) == 3

            monkeypatch.setattr(
                map_view_module,
                "_resolve_entry_by_token",
                lambda _hass, _token: (entry, {"token"}),
            )

            class _StubEntityRegistry:
                def async_get_entity_id(
                    self, _domain: str, _platform: str, _unique_id: str
                ) -> str | None:
                    return None

                def async_get(self, _entity_id: str) -> Any | None:
                    return None

            monkeypatch.setattr(
                map_view_module.er,
                "async_get",
                lambda _hass: _StubEntityRegistry(),
            )

            view = map_view_module.GoogleFindMyMapView(hass)
            request = SimpleNamespace(query={"token": "token"})
            response = await view.get(request, "device-1")
            assert response.status == 200

            migrate_handler = hass.services.registered[
                (DOMAIN, SERVICE_REBUILD_REGISTRY)
            ]
            await migrate_handler(SimpleNamespace(data={ATTR_MODE: MODE_MIGRATE}))
            assert integration._async_soft_migrate_data_to_options.await_count == 2
            assert (
                integration._async_soft_migrate_data_to_options.await_args_list[-1]
                == call(hass, entry)
            )
            assert integration._async_migrate_unique_ids.await_count == 2
            assert (
                integration._async_migrate_unique_ids.await_args_list[-1]
                == call(hass, entry)
            )
            assert integration._async_relink_button_devices.await_count == 2
            assert (
                integration._async_relink_button_devices.await_args_list[-1]
                == call(hass, entry)
            )
            assert hass.config_entries.reload_calls == [entry.entry_id]

            assert await integration.async_unload_entry(hass, entry) is True
            assert not entry.subentries
            assert not subentry_manager.managed_subentries
            assert hass.config_entries.removed_subentries

        loop.run_until_complete(_exercise())
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
            with suppress(Exception):
                loop.run_until_complete(task)
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)


def test_setup_entry_reactivates_disabled_button_entities(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Disabled button entities are re-enabled during setup."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        entry = _StubConfigEntry()
        hass = _StubHass(entry, loop)

        dummy_cache = _StubCache()

        async def _fake_create(cls, hass_obj, entry_id, legacy_path=None) -> _StubCache:  # type: ignore[override]
            assert hass_obj is hass
            assert entry_id == entry.entry_id
            return dummy_cache

        monkeypatch.setattr(
            integration.TokenCache,
            "create",
            classmethod(_fake_create),
        )
        monkeypatch.setattr(
            integration,
            "_async_soft_migrate_data_to_options",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            integration,
            "_async_migrate_unique_ids",
            AsyncMock(return_value=None),
        )
        monkeypatch.setattr(
            integration,
            "_register_instance",
            lambda *_: None,
        )
        monkeypatch.setattr(
            integration,
            "_unregister_instance",
            lambda *_: None,
        )

        dummy_fcm = SimpleNamespace(
            register_coordinator=lambda *_: None,
            unregister_coordinator=lambda *_: None,
            _start_listening=AsyncMock(return_value=None),
            request_stop=lambda: None,
        )

        monkeypatch.setattr(
            integration,
            "_async_acquire_shared_fcm",
            AsyncMock(return_value=dummy_fcm),
        )

        class _RegisterViewStub:
            def __init__(self, hass_obj: Any) -> None:
                self.hass = hass_obj

        monkeypatch.setattr(integration, "GoogleFindMyMapView", _RegisterViewStub)
        monkeypatch.setattr(
            integration, "GoogleFindMyMapRedirectView", _RegisterViewStub
        )

        coordinator_cls = stub_coordinator_factory()
        monkeypatch.setattr(integration, "GoogleFindMyCoordinator", coordinator_cls)

        disabled_marker = integration.RegistryEntryDisabler.INTEGRATION

        registry_entries = [
            SimpleNamespace(
                entity_id="button.googlefindmy_disabled",
                platform=DOMAIN,
                domain="button",
                disabled_by=disabled_marker,
                config_entry_id=entry.entry_id,
            ),
            SimpleNamespace(
                entity_id="button.googlefindmy_enabled",
                platform=DOMAIN,
                domain="button",
                disabled_by=None,
                config_entry_id=entry.entry_id,
            ),
            SimpleNamespace(
                entity_id="button.other_integration",
                platform="other",
                domain="button",
                disabled_by=disabled_marker,
                config_entry_id="other-entry",
            ),
        ]

        class _RegistryStub:
            def __init__(self) -> None:
                self.updated: list[str] = []

            def async_update_entity(self, entity_id: str, **changes: Any) -> None:
                self.updated.append(entity_id)
                for entry_obj in registry_entries:
                    if entry_obj.entity_id == entity_id and "disabled_by" in changes:
                        entry_obj.disabled_by = changes["disabled_by"]

        registry = _RegistryStub()

        def _entries_for_config_entry(
            _registry: Any, config_entry_id: str
        ) -> list[SimpleNamespace]:
            assert _registry is registry
            if config_entry_id != entry.entry_id:
                return []
            return list(registry_entries)

        monkeypatch.setattr(integration.er, "async_get", lambda _hass: registry)
        monkeypatch.setattr(
            integration.er,
            "async_entries_for_config_entry",
            _entries_for_config_entry,
            raising=False,
        )

        loop.run_until_complete(integration.async_setup_entry(hass, entry))

        assert registry_entries[0].disabled_by is None
        assert registry.updated == ["button.googlefindmy_disabled"]
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
            with suppress(Exception):
                loop.run_until_complete(task)
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)


def test_setup_entry_failure_does_not_register_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setup failures must not leave a TokenCache registered in the facade."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        entry = _StubConfigEntry()
        hass = _StubHass(entry, loop)

        monkeypatch.setattr(
            integration.ir, "async_delete_issue", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            integration.ir, "async_create_issue", lambda *args, **kwargs: None
        )
        monkeypatch.setattr(
            integration,
            "_async_migrate_unique_ids",
            AsyncMock(return_value=None),
        )

        dummy_cache = _StubCache()

        async def _fake_create(cls, hass_obj, entry_id, legacy_path=None) -> _StubCache:  # type: ignore[override]
            assert hass_obj is hass
            assert entry_id == entry.entry_id
            return dummy_cache

        monkeypatch.setattr(
            integration.TokenCache,
            "create",
            classmethod(_fake_create),
        )

        register_calls: list[tuple[str, Any]] = []
        monkeypatch.setattr(
            integration,
            "_register_instance",
            lambda entry_id, cache: register_calls.append((entry_id, cache)),
        )

        dummy_fcm = SimpleNamespace(
            register_coordinator=lambda *_: None,
            unregister_coordinator=lambda *_: None,
            _start_listening=AsyncMock(return_value=None),
            request_stop=lambda: None,
        )
        monkeypatch.setattr(
            integration,
            "_async_acquire_shared_fcm",
            AsyncMock(return_value=dummy_fcm),
        )

        def _boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("boom during coordinator init")

        monkeypatch.setattr(integration, "GoogleFindMyCoordinator", _boom)

        with pytest.raises(RuntimeError):
            loop.run_until_complete(integration.async_setup_entry(hass, entry))

        assert register_calls == []
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
            with suppress(Exception):
                loop.run_until_complete(task)
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_issue_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate-account repair issue renders with translated placeholders."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        existing_entry = _StubConfigEntry()
        existing_entry.entry_id = "entry-existing"
        existing_entry.title = "Primary Account"
        existing_entry.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        existing_entry.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"

        new_entry = _StubConfigEntry()
        new_entry.entry_id = "entry-new"
        new_entry.title = "Duplicate Account"
        new_entry.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        new_entry.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"

        hass = _StubHass(new_entry, loop)
        hass.config_entries._entries.append(existing_entry)

        recorded_issues: list[dict[str, Any]] = []

        monkeypatch.setattr(
            integration.ir, "async_delete_issue", lambda *_, **__: None, raising=False
        )

        def _record_issue(
            _hass: Any, _domain: str, issue_id: str, **kwargs: Any
        ) -> None:
            recorded_issues.append({"id": issue_id, **kwargs})

        monkeypatch.setattr(
            integration.ir, "async_create_issue", _record_issue, raising=False
        )

        async def _exercise() -> bool:
            return await integration.async_setup_entry(hass, new_entry)

        result = loop.run_until_complete(_exercise())
        assert result is False
        assert recorded_issues, "Expected duplicate-account issue to be recorded"

        issue = recorded_issues[-1]
        assert issue["translation_key"] == "duplicate_account_entries"
        placeholders = issue["translation_placeholders"]
        assert placeholders["email"] == "dup@example.com"
        assert "Primary Account" in placeholders["entries"]

        translation = json.loads(
            Path("custom_components/googlefindmy/translations/en.json").read_text(
                encoding="utf-8"
            )
        )
        template = translation["issues"]["duplicate_account_entries"]["description"]
        rendered = template.format(**placeholders)
        assert "dup@example.com" in rendered
        assert "Primary Account" in rendered
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_issue_cleanup_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolved duplicate-account issues are cleared during normal setup."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        config_entries_module = importlib.import_module("homeassistant.config_entries")
        state_cls = config_entries_module.ConfigEntryState
        if not hasattr(state_cls, "SETUP_IN_PROGRESS"):
            setattr(state_cls, "SETUP_IN_PROGRESS", "setup_in_progress")
        if not hasattr(state_cls, "SETUP_RETRY"):
            setattr(state_cls, "SETUP_RETRY", "setup_retry")

        entry = _StubConfigEntry()
        hass = _StubHass(entry, loop)

        delete_calls: list[tuple[Any, str, str]] = []

        def _delete_issue(
            hass_arg: Any, domain: str, issue_id: str, **_: Any
        ) -> None:
            delete_calls.append((hass_arg, domain, issue_id))

        monkeypatch.setattr(
            integration.ir, "async_delete_issue", _delete_issue, raising=False
        )

        create_calls: list[tuple[Any, str, str]] = []

        def _record_create(
            hass_arg: Any, domain: str, issue_id: str, **_: Any
        ) -> None:
            create_calls.append((hass_arg, domain, issue_id))

        monkeypatch.setattr(
            integration.ir, "async_create_issue", _record_create, raising=False
        )

        def _fail_domain_data(_hass: Any) -> None:
            raise RuntimeError("stop after duplicate cleanup")

        monkeypatch.setattr(integration, "_domain_data", _fail_domain_data)

        with pytest.raises(RuntimeError):
            loop.run_until_complete(integration.async_setup_entry(hass, entry))

        issue_ids = [issue_id for *_hass, _domain, issue_id in delete_calls]
        assert (
            f"duplicate_account_{entry.entry_id}" in issue_ids
        ), "Expected duplicate-account cleanup to delete stale issue"
        cleanup_index = issue_ids.index(f"duplicate_account_{entry.entry_id}")
        cleanup_call = delete_calls[cleanup_index]
        assert cleanup_call[0] is hass
        assert cleanup_call[1] == DOMAIN
        assert create_calls == []
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_mixed_states_prefer_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loaded duplicates remain authoritative; others auto-disable and clean up."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        config_entries_module = importlib.import_module("homeassistant.config_entries")
        state_cls = config_entries_module.ConfigEntryState
        if not hasattr(state_cls, "SETUP_RETRY"):
            setattr(state_cls, "SETUP_RETRY", "setup_retry")

        loaded_entry = _StubConfigEntry()
        loaded_entry.entry_id = "entry-loaded"
        loaded_entry.title = "Loaded Account"
        loaded_entry.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        loaded_entry.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"
        loaded_entry.state = ConfigEntryState.LOADED
        loaded_entry.updated_at = datetime(2024, 1, 2, 12, 0, 0)

        retry_entry = _StubConfigEntry()
        retry_entry.entry_id = "entry-retry"
        retry_entry.title = "Retry Account"
        retry_entry.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        retry_entry.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"
        retry_entry.state = getattr(
            ConfigEntryState, "SETUP_RETRY", ConfigEntryState.NOT_LOADED
        )
        retry_entry.updated_at = datetime(2024, 1, 3, 12, 0, 0)

        create_calls: list[tuple[Any, str, str, dict[str, Any]]] = []
        delete_calls: list[tuple[Any, str, str]] = []

        def _record_create(
            hass_arg: Any, domain: str, issue_id: str, **kwargs: Any
        ) -> None:
            create_calls.append((hass_arg, domain, issue_id, kwargs))

        def _record_delete(hass_arg: Any, domain: str, issue_id: str, **_: Any) -> None:
            delete_calls.append((hass_arg, domain, issue_id))

        monkeypatch.setattr(
            integration.ir, "async_create_issue", _record_create, raising=False
        )
        monkeypatch.setattr(
            integration.ir, "async_delete_issue", _record_delete, raising=False
        )

        hass_loaded = _StubHass(loaded_entry, loop)
        hass_loaded.config_entries._entries.append(retry_entry)

        should_setup_loaded, normalized_email = integration._ensure_post_migration_consistency(  # type: ignore[attr-defined]
            hass_loaded,
            loaded_entry,
        )
        assert should_setup_loaded is True
        assert normalized_email == "dup@example.com"

        if hass_loaded._tasks:
            loop.run_until_complete(asyncio.gather(*hass_loaded._tasks))

        delete_issue_ids = [issue_id for *_hass, _domain, issue_id in delete_calls]
        assert (
            f"duplicate_account_{loaded_entry.entry_id}" in delete_issue_ids
        ), "Authoritative entry should clear its repair issue"
        assert (
            f"duplicate_account_{retry_entry.entry_id}" in delete_issue_ids
        ), "Duplicate entry repair issue should be cleared after auto-disable"

        assert (
            hass_loaded.config_entries.unload_calls.count(retry_entry.entry_id) >= 1
        ), "Duplicate entry should be unloaded when disabled"
        assert not create_calls, "Auto-disabled duplicates must not raise new issues"
        assert "integration" in str(retry_entry.disabled_by).lower()

        create_calls.clear()
        delete_calls.clear()

        hass_retry = _StubHass(retry_entry, loop)
        hass_retry.config_entries._entries.append(loaded_entry)

        should_setup_retry, normalized_retry = integration._ensure_post_migration_consistency(  # type: ignore[attr-defined]
            hass_retry,
            retry_entry,
        )
        assert should_setup_retry is False
        assert normalized_retry == "dup@example.com"
        assert "integration" in str(retry_entry.disabled_by).lower()
        assert not create_calls, "Duplicate should remain disabled without new issues"

        if hass_retry._tasks:
            loop.run_until_complete(asyncio.gather(*hass_retry._tasks))

        delete_issue_ids = [issue_id for *_hass, _domain, issue_id in delete_calls]
        assert (
            f"duplicate_account_{retry_entry.entry_id}" in delete_issue_ids
        ), "Duplicate entry issues should stay cleared"
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_auto_disables_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-authoritative entries are disabled, unloaded, and cleaned up."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        authoritative = _StubConfigEntry()
        authoritative.entry_id = "entry-authoritative"
        authoritative.title = "Authoritative"
        authoritative.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        authoritative.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"
        authoritative.state = ConfigEntryState.LOADED

        duplicate_loaded = _StubConfigEntry()
        duplicate_loaded.entry_id = "entry-duplicate-loaded"
        duplicate_loaded.title = "Loaded Duplicate"
        duplicate_loaded.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        duplicate_loaded.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"
        duplicate_loaded.state = ConfigEntryState.LOADED

        duplicate_error = _StubConfigEntry()
        duplicate_error.entry_id = "entry-duplicate-error"
        duplicate_error.title = "Error Duplicate"
        duplicate_error.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        duplicate_error.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"
        duplicate_error.state = getattr(
            ConfigEntryState, "SETUP_ERROR", ConfigEntryState.LOADED
        )

        duplicate_user = _StubConfigEntry()
        duplicate_user.entry_id = "entry-duplicate-user"
        duplicate_user.title = "User Disabled"
        duplicate_user.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        duplicate_user.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"
        duplicate_user.state = ConfigEntryState.LOADED
        duplicate_user.disabled_by = "user"

        create_calls: list[tuple[Any, str, str, dict[str, Any]]] = []
        delete_calls: list[tuple[Any, str, str]] = []

        def _record_create(
            hass_arg: Any, domain: str, issue_id: str, **kwargs: Any
        ) -> None:
            create_calls.append((hass_arg, domain, issue_id, kwargs))

        def _record_delete(hass_arg: Any, domain: str, issue_id: str, **_: Any) -> None:
            delete_calls.append((hass_arg, domain, issue_id))

        monkeypatch.setattr(
            integration.ir, "async_create_issue", _record_create, raising=False
        )
        monkeypatch.setattr(
            integration.ir, "async_delete_issue", _record_delete, raising=False
        )

        hass = _StubHass(authoritative, loop)
        hass.config_entries._entries.extend(
            [duplicate_loaded, duplicate_error, duplicate_user]
        )

        should_setup, normalized_email = integration._ensure_post_migration_consistency(  # type: ignore[attr-defined]
            hass,
            authoritative,
        )
        assert should_setup is True
        assert normalized_email == "dup@example.com"

        if hass._tasks:
            loop.run_until_complete(asyncio.gather(*hass._tasks))

        assert "integration" in str(duplicate_loaded.disabled_by).lower()
        assert "integration" in str(duplicate_error.disabled_by).lower()
        assert "user" in str(duplicate_user.disabled_by).lower()

        for duplicate in (duplicate_loaded, duplicate_error, duplicate_user):
            assert (
                hass.config_entries.unload_calls.count(duplicate.entry_id) >= 1
            ), "Every duplicate should be unloaded"

        delete_issue_ids = [issue_id for *_hass, _domain, issue_id in delete_calls]
        assert (
            f"duplicate_account_{duplicate_loaded.entry_id}" in delete_issue_ids
        )
        assert (
            f"duplicate_account_{duplicate_error.entry_id}" in delete_issue_ids
        )
        assert (
            f"duplicate_account_{duplicate_user.entry_id}" in delete_issue_ids
        )
        assert (
            f"duplicate_account_{authoritative.entry_id}" in delete_issue_ids
        )

        assert not create_calls
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_legacy_core_disable_fallback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy cores raise TypeError but still unload and raise repair issues."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        authoritative = _StubConfigEntry()
        authoritative.entry_id = "entry-authoritative"
        authoritative.title = "Authoritative"
        authoritative.data[CONF_GOOGLE_EMAIL] = "legacy@example.com"
        authoritative.data[DATA_SECRET_BUNDLE]["username"] = "legacy@example.com"
        authoritative.state = ConfigEntryState.LOADED

        duplicate_legacy = _StubConfigEntry()
        duplicate_legacy.entry_id = "entry-duplicate-legacy"
        duplicate_legacy.title = "Legacy Duplicate"
        duplicate_legacy.data[CONF_GOOGLE_EMAIL] = "legacy@example.com"
        duplicate_legacy.data[DATA_SECRET_BUNDLE]["username"] = "legacy@example.com"
        duplicate_legacy.state = ConfigEntryState.LOADED

        hass = _StubHass(authoritative, loop)
        hass.config_entries._entries.append(duplicate_legacy)

        original_update = hass.config_entries.__class__.async_update_entry

        def _legacy_update_entry(
            self: _StubConfigEntries, entry: _StubConfigEntry, **kwargs: Any
        ) -> None:
            if "disabled_by" in kwargs:
                raise TypeError("disabled_by is not supported")
            return original_update(self, entry, **kwargs)

        monkeypatch.setattr(
            hass.config_entries.__class__,
            "async_update_entry",
            _legacy_update_entry,
        )

        create_calls: list[tuple[Any, str, str, dict[str, Any]]] = []
        delete_calls: list[tuple[Any, str, str]] = []

        def _record_create(
            hass_arg: Any, domain: str, issue_id: str, **kwargs: Any
        ) -> None:
            create_calls.append((hass_arg, domain, issue_id, kwargs))

        def _record_delete(hass_arg: Any, domain: str, issue_id: str, **_: Any) -> None:
            delete_calls.append((hass_arg, domain, issue_id))

        monkeypatch.setattr(
            integration.ir, "async_create_issue", _record_create, raising=False
        )
        monkeypatch.setattr(
            integration.ir, "async_delete_issue", _record_delete, raising=False
        )

        caplog.set_level(logging.INFO)

        should_setup, normalized_email = integration._ensure_post_migration_consistency(  # type: ignore[attr-defined]
            hass,
            authoritative,
        )
        assert should_setup is True
        assert normalized_email == "legacy@example.com"

        if hass._tasks:
            loop.run_until_complete(asyncio.gather(*hass._tasks))

        assert hass.config_entries.unload_calls.count(duplicate_legacy.entry_id) >= 1
        assert duplicate_legacy.disabled_by is None

        warning_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert any(
            "could not be disabled via API" in message for message in warning_messages
        ), "Fallback warning should be emitted for legacy disable handling"

        info_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.INFO
        ]
        assert any(
            "manual_action_required=['entry-duplicate-legacy']" in message
            for message in info_messages
        ), "Manual action list should log the legacy duplicate entry"

        create_issue_ids = [issue_id for *_hass, _domain, issue_id, _kwargs in create_calls]
        assert (
            f"duplicate_account_{duplicate_legacy.entry_id}" in create_issue_ids
        ), "Repair issue should be created for the legacy duplicate"

        delete_issue_ids = [issue_id for *_hass, _domain, issue_id in delete_calls]
        assert (
            f"duplicate_account_{duplicate_legacy.entry_id}" not in delete_issue_ids
        ), "Legacy duplicate issues should remain open for manual action"
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_all_not_loaded_prefers_newest_timestamp() -> None:
    """Among inactive duplicates, the freshest update wins."""

    integration = importlib.import_module("custom_components.googlefindmy")

    primary_entry = _StubConfigEntry()
    primary_entry.entry_id = "entry-primary"
    primary_entry.state = ConfigEntryState.NOT_LOADED
    primary_entry.updated_at = datetime(2024, 1, 1, 12, 0, 0)
    primary_entry.created_at = datetime(2024, 1, 1, 11, 0, 0)

    newer_entry = _StubConfigEntry()
    newer_entry.entry_id = "entry-newer"
    newer_entry.state = ConfigEntryState.NOT_LOADED
    newer_entry.updated_at = datetime(2024, 1, 2, 12, 0, 0)
    newer_entry.created_at = datetime(2024, 1, 2, 11, 0, 0)

    authoritative = integration._select_authoritative_entry_id(  # type: ignore[attr-defined]
        primary_entry,
        [newer_entry],
    )
    assert authoritative == "entry-newer"


def test_duplicate_account_tie_breaker_by_entry_id() -> None:
    """Equal states and timestamps fall back to entry_id ordering."""

    integration = importlib.import_module("custom_components.googlefindmy")

    candidate_a = _StubConfigEntry()
    candidate_a.entry_id = "entry-a"
    candidate_a.state = ConfigEntryState.NOT_LOADED
    candidate_a.updated_at = datetime(2024, 1, 2, 12, 0, 0)
    candidate_a.created_at = datetime(2024, 1, 1, 12, 0, 0)

    candidate_b = _StubConfigEntry()
    candidate_b.entry_id = "entry-b"
    candidate_b.state = ConfigEntryState.NOT_LOADED
    candidate_b.updated_at = datetime(2024, 1, 2, 12, 0, 0)
    candidate_b.created_at = datetime(2024, 1, 1, 12, 0, 0)

    authoritative = integration._select_authoritative_entry_id(  # type: ignore[attr-defined]
        candidate_b,
        [candidate_a],
    )
    assert authoritative == "entry-a"


def test_duplicate_account_clear_stale_issues_for_all() -> None:
    """When duplicates are gone, all related issues are purged."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        entry = _StubConfigEntry()
        entry.entry_id = "entry-authoritative"
        entry.data[CONF_GOOGLE_EMAIL] = "solo@example.com"
        entry.data[DATA_SECRET_BUNDLE]["username"] = "solo@example.com"

        hass = _StubHass(entry, loop)

        registry = integration.ir.async_get(hass)
        registry.async_create_issue(  # type: ignore[attr-defined]
            DOMAIN,
            "duplicate_account_entry-removed",
            translation_key="duplicate_account_entries",
            translation_placeholders={"email": "solo@example.com"},
        )

        integration._ensure_post_migration_consistency(  # type: ignore[attr-defined]
            hass,
            entry,
        )

        assert (
            registry.async_get_issue(  # type: ignore[attr-defined]
                DOMAIN, "duplicate_account_entry-removed"
            )
            is None
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_cleanup_keeps_active_tuple_key_issues() -> None:
    """Only stale duplicate-account issues are removed for tuple-key registries."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        authoritative = _StubConfigEntry()
        authoritative.entry_id = "entry-authoritative"
        authoritative.state = ConfigEntryState.LOADED
        email = "duo@example.com"
        authoritative.data[CONF_GOOGLE_EMAIL] = email
        authoritative.data[DATA_SECRET_BUNDLE]["username"] = email

        duplicate = _StubConfigEntry()
        duplicate.entry_id = "entry-duplicate"
        duplicate.state = ConfigEntryState.NOT_LOADED
        duplicate.data[CONF_GOOGLE_EMAIL] = email
        duplicate.data[DATA_SECRET_BUNDLE]["username"] = email

        hass = _StubHass(authoritative, loop)
        hass.config_entries._entries.append(duplicate)

        registry = integration.ir.async_get(hass)
        registry.async_create_issue(  # type: ignore[attr-defined]
            DOMAIN,
            "duplicate_account_entry-stale",
            translation_placeholders={"email": email},
        )
        registry.async_create_issue(  # type: ignore[attr-defined]
            DOMAIN,
            f"duplicate_account_{duplicate.entry_id}",
            translation_placeholders={"email": email},
        )

        should_setup, normalized_email = integration._ensure_post_migration_consistency(  # type: ignore[attr-defined]
            hass,
            authoritative,
        )

        assert should_setup is True
        assert normalized_email == email
        assert (
            registry.async_get_issue(  # type: ignore[attr-defined]
                DOMAIN,
                "duplicate_account_entry-stale",
            )
            is None
        )
        active_issue = registry.async_get_issue(  # type: ignore[attr-defined]
            DOMAIN,
            f"duplicate_account_{duplicate.entry_id}",
        )
        assert active_issue is not None
        placeholders = active_issue.get("translation_placeholders", {})
        assert placeholders.get("email") == email
        assert authoritative.entry_id in str(placeholders.get("entries", ""))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_cleanup_respects_string_key_issue_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale cleanup handles registries that expose string-key issue mappings."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        class _StringKeyIssueRegistry:
            def __init__(self) -> None:
                self.issues: dict[str, dict[str, Any]] = {}

            def async_get_issue(
                self, domain: str, issue_id: str
            ) -> dict[str, Any] | None:
                issue = self.issues.get(issue_id)
                if issue and issue.get("domain") == domain:
                    return issue
                return None

            def async_create_issue(
                self,
                domain: str,
                issue_id: str,
                **data: Any,
            ) -> None:
                self.issues[issue_id] = {
                    **data,
                    "domain": domain,
                    "issue_id": issue_id,
                }

            def async_delete_issue(self, domain: str, issue_id: str) -> None:
                self.issues.pop(issue_id, None)

        registry = _StringKeyIssueRegistry()

        monkeypatch.setattr(
            integration.ir,
            "async_get",
            lambda hass: registry,
        )
        monkeypatch.setattr(
            integration.ir,
            "async_create_issue",
            lambda hass, domain, issue_id, **data: registry.async_create_issue(
                domain, issue_id, **data
            ),
        )
        monkeypatch.setattr(
            integration.ir,
            "async_delete_issue",
            lambda hass, domain, issue_id: registry.async_delete_issue(
                domain, issue_id
            ),
        )

        authoritative = _StubConfigEntry()
        authoritative.entry_id = "string-key-authoritative"
        authoritative.state = ConfigEntryState.LOADED
        email = "mapped@example.com"
        authoritative.data[CONF_GOOGLE_EMAIL] = email
        authoritative.data[DATA_SECRET_BUNDLE]["username"] = email

        duplicate = _StubConfigEntry()
        duplicate.entry_id = "string-key-duplicate"
        duplicate.state = ConfigEntryState.NOT_LOADED
        duplicate.data[CONF_GOOGLE_EMAIL] = email
        duplicate.data[DATA_SECRET_BUNDLE]["username"] = email

        hass = _StubHass(authoritative, loop)
        hass.config_entries._entries.append(duplicate)

        registry.async_create_issue(
            DOMAIN,
            "duplicate_account_retired-entry",
            translation_placeholders={"email": email},
        )
        registry.async_create_issue(
            DOMAIN,
            f"duplicate_account_{duplicate.entry_id}",
            translation_placeholders={"email": email},
        )

        should_setup, normalized_email = integration._ensure_post_migration_consistency(  # type: ignore[attr-defined]
            hass,
            authoritative,
        )

        assert should_setup is True
        assert normalized_email == email
        assert (
            registry.async_get_issue(
                DOMAIN,
                "duplicate_account_retired-entry",
            )
            is None
        )
        active_issue = registry.async_get_issue(
            DOMAIN,
            f"duplicate_account_{duplicate.entry_id}",
        )
        assert active_issue is not None
        placeholders = active_issue.get("translation_placeholders", {})
        assert placeholders.get("email") == email
        assert authoritative.entry_id in str(placeholders.get("entries", ""))
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_issue_exists_helper_is_synchronous() -> None:
    """_issue_exists interacts with the registry helpers without awaiting."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        entry = _StubConfigEntry()
        hass = _StubHass(entry, loop)

        assert (
            integration._issue_exists(  # type: ignore[attr-defined]
                hass,
                "missing_issue",
            )
            is False
        )
        registry = integration.ir.async_get(hass)
        registry.async_create_issue(  # type: ignore[attr-defined]
            DOMAIN,
            "duplicate_account_entry-test",
            is_fixable=False,
            severity="warning",
            translation_key="duplicate_account_entries",
            translation_placeholders={"email": "user@example.com"},
        )
        assert (
            integration._issue_exists(  # type: ignore[attr-defined]
                hass,
                "duplicate_account_entry-test",
            )
            is True
        )
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_duplicate_account_issue_log_level_downgrades_when_existing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Existing repair issues cause duplicate detection logs to drop to DEBUG."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        integration = importlib.import_module("custom_components.googlefindmy")

        entry = _StubConfigEntry()
        entry.entry_id = "entry-dup"
        entry.data[CONF_GOOGLE_EMAIL] = "dup@example.com"
        entry.data[DATA_SECRET_BUNDLE]["username"] = "dup@example.com"

        hass = _StubHass(entry, loop)

        caplog.set_level(logging.DEBUG)

        caplog.clear()
        integration._log_duplicate_and_raise_repair_issue(  # type: ignore[attr-defined]
            hass,
            entry,
            "dup@example.com",
            cause="setup_duplicate",
            conflicts=[],
        )
        warning_records = [
            record
            for record in caplog.records
            if "duplicate account" in record.getMessage()
        ]
        assert warning_records
        assert warning_records[-1].levelno == logging.WARNING

        caplog.clear()
        integration._log_duplicate_and_raise_repair_issue(  # type: ignore[attr-defined]
            hass,
            entry,
            "dup@example.com",
            cause="setup_duplicate",
            conflicts=[],
        )
        debug_records = [
            record
            for record in caplog.records
            if "duplicate account" in record.getMessage()
        ]
        assert debug_records
        assert debug_records[-1].levelno == logging.DEBUG
    finally:
        loop.close()
        asyncio.set_event_loop(None)


def test_service_no_active_entry_placeholders() -> None:
    """Service validation exposes counts/list placeholders for inactive setups."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        services_module = importlib.import_module(
            "custom_components.googlefindmy.services"
        )

        entries = [
            SimpleNamespace(title="Account One", entry_id="entry-1", active=False),
            SimpleNamespace(title="Account Two", entry_id="entry-2", active=False),
        ]

        class _ConfigEntriesStub:
            def async_entries(self, domain: str) -> list[Any]:
                assert domain == DOMAIN
                return list(entries)

        class _ServicesStub:
            def __init__(self) -> None:
                self.registered: dict[tuple[str, str], Callable[..., Any]] = {}

            def async_register(
                self, domain: str, service: str, handler: Callable[..., Any]
            ) -> None:
                self.registered[(domain, service)] = handler

        hass = SimpleNamespace(
            data={},
            services=_ServicesStub(),
            config_entries=_ConfigEntriesStub(),
        )

        ctx: dict[str, Any] = {
            "domain": DOMAIN,
            "resolve_canonical": lambda _hass, device_id: (device_id, device_id),
            "is_active_entry": lambda entry: getattr(entry, "active", False),
            "primary_active_entry": lambda entries_list: None,
            "opt": lambda entry, key, default=None: default,
            "default_map_view_token_expiration": False,
            "opt_map_view_token_expiration_key": "map_view_token_expiration",
            "redact_url_token": lambda token: token,
            "soft_migrate_entry": AsyncMock(),
        }

        loop.run_until_complete(services_module.async_register_services(hass, ctx))

        handler = hass.services.registered[(DOMAIN, SERVICE_LOCATE_DEVICE)]
        call = SimpleNamespace(data={"device_id": "device-1"})

        with pytest.raises(ServiceValidationError) as errinfo:
            loop.run_until_complete(handler(call))

        error = errinfo.value
        placeholders = error.translation_placeholders
        assert placeholders["active_count"] == "0"
        assert placeholders["total_count"] == "2"
        assert "Account One" in placeholders["entries"]

        translation = json.loads(
            Path("custom_components/googlefindmy/translations/en.json").read_text(
                encoding="utf-8"
            )
        )
        template = translation["exceptions"]["no_active_entry"]["message"]
        rendered = template.format(**placeholders)
        assert "0/2" in rendered
        assert "Account One" in rendered
    finally:
        loop.close()
        asyncio.set_event_loop(None)
