# tests/test_button_service_registration.py
"""Ensure button setup handles missing platform service registration."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable
from typing import Any
from collections.abc import Callable

import pytest

from .test_button_setup import _ensure_button_dependencies


class _StubConfigEntry:
    """Config entry stub capturing unload callbacks."""

    def __init__(self) -> None:
        self.entry_id = "entry-test"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.runtime_data: Any | None = None
        self._unload_callbacks: list[Callable[[], None]] = []

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self._unload_callbacks.append(callback)


class _StubHass:
    """Home Assistant stub mirroring the essentials used by the buttons."""

    def __init__(self, loop: asyncio.AbstractEventLoop, domain: str) -> None:
        self.loop = loop
        self.data: dict[str, Any] = {domain: {}, "core.uuid": "ha-uuid"}
        self._tasks: list[asyncio.Task[Any]] = []

    def async_create_task(
        self, coro: Awaitable[Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = self.loop.create_task(coro, name=name)
        self._tasks.append(task)
        return task

    async def async_add_executor_job(self, func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)


def test_button_setup_skips_service_registration_when_platform_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The button platform skips service registration if the platform is missing."""

    _ensure_button_dependencies()
    button_module = importlib.import_module("custom_components.googlefindmy.button")

    class _StubCoordinator(button_module.GoogleFindMyCoordinator):
        """Coordinator stub mimicking the runtime data contract."""

        def __init__(
            self,
            hass: Any,
            config_entry: _StubConfigEntry,
            devices: list[dict[str, Any]],
        ) -> None:
            self.hass = hass
            self.config_entry = config_entry
            self.data = devices
            self._listeners: list[Callable[[], None]] = []
            self._subentry_key = "core_tracking"

        def async_add_listener(
            self, listener: Callable[[], None]
        ) -> Callable[[], None]:
            self._listeners.append(listener)
            return lambda: None

        async def async_request_refresh(
            self,
        ) -> None:  # pragma: no cover - compatibility hook
            return None

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

        def is_device_visible_in_subentry(
            self, subentry_key: str, device_id: str
        ) -> bool:
            return True

    try:
        original_loop = asyncio.get_event_loop()
    except RuntimeError:
        original_loop = None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    hass = _StubHass(loop, button_module.DOMAIN)
    config_entry = _StubConfigEntry()

    coordinator = _StubCoordinator(
        hass,
        config_entry,
        devices=[{"id": "device-1", "name": " Demo "}],
    )
    config_entry.runtime_data = coordinator

    added_entities: list[Any] = []

    def _async_add_entities(entities: list[Any], _: bool = False) -> None:
        added_entities.extend(entities)

    monkeypatch.setattr(
        button_module.entity_platform,
        "async_get_current_platform",
        lambda: None,
    )

    recorded_calls: list[bool] = []

    def _record_call(*_: Any, **__: Any) -> None:
        recorded_calls.append(True)

    monkeypatch.setattr(
        "custom_components.googlefindmy.util_services.register_entity_service",
        _record_call,
    )

    try:
        loop.run_until_complete(
            button_module.async_setup_entry(hass, config_entry, _async_add_entities)
        )
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        asyncio.set_event_loop(original_loop)
        loop.close()

    assert [entity.entity_description.translation_key for entity in added_entities] == [
        "play_sound",
        "stop_sound",
        "locate_device",
    ]
    assert recorded_calls == []
