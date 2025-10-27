# tests/test_hass_data_layout.py
"""Regression tests for the hass.data layout used by the integration."""

from __future__ import annotations

import asyncio

import importlib
import json
import sys

from contextlib import suppress
from pathlib import Path
from types import MappingProxyType, ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, Any
from collections.abc import Awaitable, Callable
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

    def async_update_entry(
        self, entry: _StubConfigEntry, *, options: dict[str, Any]
    ) -> None:
        entry.options = options

    async def async_reload(self, entry_id: str) -> None:
        self.reload_calls.append(entry_id)


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


class _StubCoordinator:
    """Minimal coordinator stub satisfying platform expectations."""

    def __init__(self, hass: _StubHass, *, cache: _StubCache, **_: Any) -> None:
        self.hass = hass
        self.cache = cache
        self.data = [{"id": "device-1", "name": "Device"}]
        self.stats = {"background_updates": 1}
        self.performance_metrics: dict[str, Any] = {}
        self.last_update_success = True
        self.config_entry: _StubConfigEntry | None = None
        self._purged: list[str] = []
        self._subentry_key = "core_tracking"

    def async_add_listener(self, _listener: Callable[[], None]) -> Callable[[], None]:
        return lambda: None

    def force_poll_due(self) -> None:  # pragma: no cover - reload path
        return None

    async def async_setup(self) -> None:
        return None

    async def async_refresh(self) -> None:
        return None

    async def async_shutdown(self) -> None:
        return None

    def purge_device(self, device_id: str) -> None:
        self._purged.append(device_id)

    def get_subentry_key_for_feature(self, feature: str) -> str:
        return self._subentry_key

    def stable_subentry_identifier(
        self, *, key: str | None = None, feature: str | None = None
    ) -> str:
        return "core_tracking"

    def get_subentry_snapshot(
        self, key: str | None = None, *, feature: str | None = None
    ) -> list[dict[str, Any]]:
        return list(self.data)

    def is_device_visible_in_subentry(self, subentry_key: str, device_id: str) -> bool:
        return True

    def attach_subentry_manager(self, manager: Any) -> None:
        self.subentry_manager = manager


def test_hass_data_layout(monkeypatch: pytest.MonkeyPatch) -> None:
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

        # Ensure isinstance checks in platform modules resolve to the stub coordinator.
        monkeypatch.setattr(
            coordinator_module, "GoogleFindMyCoordinator", _StubCoordinator
        )
        monkeypatch.setattr(integration, "GoogleFindMyCoordinator", _StubCoordinator)
        monkeypatch.setattr(button_module, "GoogleFindMyCoordinator", _StubCoordinator)
        monkeypatch.setattr(
            map_view_module,
            "GoogleFindMyCoordinator",
            _StubCoordinator,
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
            assert "core_tracking" in managed
            core_subentry = managed["core_tracking"]
            assert core_subentry.data["group_key"] == "core_tracking"
            features = core_subentry.data["features"]
            assert features == [
                "binary_sensor",
                "button",
                "device_tracker",
                "sensor",
            ]
            assert all(isinstance(feature, str) for feature in features)
            assert all(feature == feature.lower() for feature in features)
            assert core_subentry.data["fcm_push_enabled"] is True
            assert core_subentry.data["has_google_home_filter"] is False
            assert core_subentry.unique_id.endswith("core_tracking")

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
            assert integration._async_soft_migrate_data_to_options.await_args_list[
                -1
            ] == call(hass, entry)
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

        monkeypatch.setattr(integration, "GoogleFindMyCoordinator", _StubCoordinator)

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
