# tests/test_hass_data_layout.py
"""Regression tests for the hass.data layout used by the integration."""

from __future__ import annotations

import asyncio
import importlib
import sys
from contextlib import suppress
from types import ModuleType, SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
)


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
        self.entry_id = "entry-test"
        self.data = {
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
        }
        self.options: dict[str, Any] = {}
        self.title = "Test Entry"
        self.runtime_data: Any = None
        from homeassistant.config_entries import ConfigEntryState

        self.state = ConfigEntryState.LOADED
        self.disabled_by = None
        self._unload_callbacks: list[Callable[[], None]] = []

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self._unload_callbacks.append(callback)


class _StubBus:
    """Event bus stub providing async_listen_once."""

    def async_listen_once(self, _event: str, _callback: Callable[..., Any]) -> Callable[[], None]:
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
        self._entries = [entry]
        self.forward_calls: list[tuple[_StubConfigEntry, tuple[str, ...]]] = []

    def async_entries(self, _domain: str) -> list[_StubConfigEntry]:
        return list(self._entries)

    async def async_forward_entry_setups(self, entry: _StubConfigEntry, platforms: list[str]) -> None:
        self.forward_calls.append((entry, tuple(platforms)))

    async def async_unload_platforms(self, entry: _StubConfigEntry, _platforms: list[str]) -> bool:
        return True

    def async_update_entry(self, entry: _StubConfigEntry, *, options: dict[str, Any]) -> None:
        entry.options = options


class _StubHass:
    """Home Assistant core stub with just enough surface for setup."""

    def __init__(self, entry: _StubConfigEntry, loop: asyncio.AbstractEventLoop) -> None:
        from homeassistant.core import CoreState

        self.data: dict[str, Any] = {DOMAIN: {}, "core.uuid": "ha-uuid"}
        self.loop = loop
        self.state = CoreState.running
        self.bus = _StubBus()
        self.http = _StubHttp()
        self.config_entries = _StubConfigEntries(entry)
        self._tasks: list[asyncio.Task[Any]] = []

    def async_create_task(self, coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
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


def test_hass_data_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The integration stores runtime state only under hass.data[DOMAIN]["entries"]."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        if "homeassistant.components.button" not in sys.modules:
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

        integration = importlib.import_module("custom_components.googlefindmy.__init__")
        coordinator_module = importlib.import_module("custom_components.googlefindmy.coordinator")
        button_module = importlib.import_module("custom_components.googlefindmy.button")
        sys.modules.pop("custom_components.googlefindmy.map_view", None)
        map_view_module = importlib.import_module("custom_components.googlefindmy.map_view")

        config_entries_module = importlib.import_module("homeassistant.config_entries")
        state_cls = config_entries_module.ConfigEntryState
        if not hasattr(state_cls, "SETUP_IN_PROGRESS"):
            setattr(state_cls, "SETUP_IN_PROGRESS", "setup_in_progress")
        if not hasattr(state_cls, "SETUP_RETRY"):
            setattr(state_cls, "SETUP_RETRY", "setup_retry")

        cache = _StubCache()
        monkeypatch.setattr(integration.TokenCache, "create", AsyncMock(return_value=cache))
        monkeypatch.setattr(integration, "_register_instance", lambda *_: None)
        monkeypatch.setattr(integration, "_unregister_instance", lambda *_: cache)
        monkeypatch.setattr(integration, "_async_soft_migrate_data_to_options", AsyncMock())
        monkeypatch.setattr(integration, "_async_migrate_unique_ids", AsyncMock())
        monkeypatch.setattr(integration, "_async_save_secrets_data", AsyncMock())
        monkeypatch.setattr(integration, "_async_seed_manual_credentials", AsyncMock())
        monkeypatch.setattr(integration, "_async_normalize_device_names", AsyncMock())

        class _RegisterViewStub:
            def __init__(self, hass: Any) -> None:
                self.hass = hass

        monkeypatch.setattr(integration, "GoogleFindMyMapView", _RegisterViewStub)
        monkeypatch.setattr(integration, "GoogleFindMyMapRedirectView", _RegisterViewStub)

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
        monkeypatch.setattr(coordinator_module, "GoogleFindMyCoordinator", _StubCoordinator)
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
            setup_ok = await integration.async_setup_entry(hass, entry)
            assert setup_ok is True

            if hass._tasks:
                await asyncio.gather(*hass._tasks)

            domain_bucket = hass.data[DOMAIN]
            assert entry.entry_id not in domain_bucket
            runtime_bucket = domain_bucket["entries"]
            assert entry.entry_id in runtime_bucket

            runtime_data = runtime_bucket[entry.entry_id]
            assert getattr(runtime_data, "coordinator", None) is entry.runtime_data

            added_entities: list[Any] = []

            def _collect_entities(entities: list[Any], _update_before_add: bool = False) -> None:
                added_entities.extend(entities)

            await button_module.async_setup_entry(hass, entry, _collect_entities)
            assert len(added_entities) == 3

            monkeypatch.setattr(
                map_view_module,
                "_resolve_entry_by_token",
                lambda _hass, _token: (entry, {"token"}),
            )
            monkeypatch.setattr(
                map_view_module,
                "async_get_entity_registry",
                lambda _hass: SimpleNamespace(entities={}),
            )

            view = map_view_module.GoogleFindMyMapView(hass)
            request = SimpleNamespace(query={"token": "token"})
            response = await view.get(request, "device-1")
            assert response.status == 200

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
