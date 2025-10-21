# tests/test_map_view_unique_id_resolution.py
"""Tests for resolving map view tracker entities by exact unique_id match."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timezone
import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import DOMAIN


class _StubCoordinator:
    """Coordinator stub that exposes a devices snapshot."""

    def __init__(self, devices: list[dict[str, Any]]) -> None:
        self.data = devices


class _StubEntry:
    """Config entry stub carrying runtime data."""

    def __init__(self, entry_id: str, coordinator: _StubCoordinator) -> None:
        self.entry_id = entry_id
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.runtime_data = coordinator


class _StubHass:
    """Minimal Home Assistant stub for the map view handler."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        """Execute the provided callable synchronously during tests."""

        return func(*args)


class _StubEntityEntry:
    """Entity registry entry stub used for lookup assertions."""

    def __init__(
        self,
        *,
        entity_id: str,
        unique_id: str,
        config_entry_id: str,
    ) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id
        self.platform = DOMAIN


class _StubEntityRegistry:
    """Entity registry stub that emulates HA lookups."""

    def __init__(self, entries: list[_StubEntityEntry]) -> None:
        ordered = OrderedDict((entry.entity_id, entry) for entry in entries)
        self.entities: OrderedDict[str, _StubEntityEntry] = ordered

    def async_get_entity_id(
        self, domain: str, platform: str, unique_id: str
    ) -> str | None:
        for entry in self.entities.values():
            if (
                entry.entity_id.startswith(f"{domain}.")
                and entry.platform == platform
                and entry.unique_id == unique_id
            ):
                return entry.entity_id
        return None

    def async_get(self, entity_id: str) -> _StubEntityEntry | None:
        return self.entities.get(entity_id)


def test_map_view_prefers_exact_unique_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracker selection must match explicit unique_id formats before fallback."""

    http_module = ModuleType("homeassistant.components.http")

    class _HttpViewStub:
        def __init__(self, hass: Any | None = None) -> None:
            self.hass = hass

    http_module.HomeAssistantView = _HttpViewStub
    monkeypatch.setitem(sys.modules, "homeassistant.components.http", http_module)

    core_module = ModuleType("homeassistant.core")

    class _HomeAssistantStub:  # pragma: no cover - structural stub
        pass

    core_module.HomeAssistant = _HomeAssistantStub
    monkeypatch.setitem(sys.modules, "homeassistant.core", core_module)

    helpers_module = ModuleType("homeassistant.helpers.entity_registry")
    helpers_module.async_get = lambda _hass: None
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.entity_registry", helpers_module
    )

    dt_module = ModuleType("homeassistant.util.dt")
    dt_module.utcnow = lambda: datetime.now(timezone.utc)
    dt_module.as_local = lambda value: value
    dt_module.UTC = timezone.utc
    monkeypatch.setitem(sys.modules, "homeassistant.util.dt", dt_module)

    util_module = ModuleType("homeassistant.util")
    util_module.dt = dt_module
    monkeypatch.setitem(sys.modules, "homeassistant.util", util_module)

    custom_components_pkg = ModuleType("custom_components")
    custom_components_pkg.__path__ = [
        str(Path(__file__).resolve().parents[1] / "custom_components")
    ]
    monkeypatch.setitem(sys.modules, "custom_components", custom_components_pkg)

    googlefindmy_pkg = ModuleType("custom_components.googlefindmy")
    googlefindmy_pkg.__path__ = [
        str(Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy")
    ]
    monkeypatch.setitem(sys.modules, "custom_components.googlefindmy", googlefindmy_pkg)

    coordinator_module = ModuleType("custom_components.googlefindmy.coordinator")
    coordinator_module.GoogleFindMyCoordinator = _StubCoordinator
    monkeypatch.setitem(
        sys.modules, "custom_components.googlefindmy.coordinator", coordinator_module
    )

    module_name = "custom_components.googlefindmy.map_view"
    spec = importlib.util.spec_from_file_location(
        module_name,
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "googlefindmy"
        / "map_view.py",
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError("Failed to load map_view module for testing")
    map_view = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, map_view)
    spec.loader.exec_module(map_view)

    device_id = "device-abc"
    coordinator = _StubCoordinator(
        devices=[
            {"id": device_id, "name": "Primary Device"},
            {"id": "device-abc-shadow", "name": "Shadow Device"},
        ]
    )
    entry = _StubEntry("entry-123", coordinator)

    target_unique_id = f"{entry.entry_id}:{device_id}"
    overlapping_unique_id = f"{entry.entry_id}:{device_id}-shadow"

    registry = _StubEntityRegistry(
        [
            _StubEntityEntry(
                entity_id="device_tracker.googlefindmy_shadow",
                unique_id=overlapping_unique_id,
                config_entry_id=entry.entry_id,
            ),
            _StubEntityEntry(
                entity_id="device_tracker.googlefindmy_primary",
                unique_id=target_unique_id,
                config_entry_id=entry.entry_id,
            ),
        ]
    )

    monkeypatch.setattr(
        map_view, "GoogleFindMyCoordinator", _StubCoordinator, raising=False
    )
    monkeypatch.setattr(
        map_view,
        "_resolve_entry_by_token",
        lambda _hass, token: (entry, {token}) if token == "valid" else (None, None),
        raising=False,
    )
    monkeypatch.setattr(
        map_view,
        "async_get_entity_registry",
        lambda _hass: registry,
        raising=False,
    )

    history_calls: list[list[str]] = []

    def _stub_history(
        _hass: Any, _start: Any, _end: Any, entity_ids: list[str]
    ) -> dict[str, Any]:
        history_calls.append(list(entity_ids))
        return {}

    history_module = ModuleType("homeassistant.components.recorder.history")
    history_module.get_significant_states = _stub_history
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.history",
        history_module,
    )

    hass = _StubHass()
    view = map_view.GoogleFindMyMapView(hass)

    request = SimpleNamespace(query={"token": "valid"})
    response = asyncio.run(view.get(request, device_id))

    assert response.status == 200
    assert history_calls == [["device_tracker.googlefindmy_primary"]]
