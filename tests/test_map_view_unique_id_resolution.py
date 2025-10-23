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
        self.runtime_data = SimpleNamespace(coordinator=coordinator)


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


def _load_map_view_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the map_view module with stubbed Home Assistant dependencies."""

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

    helpers_pkg = ModuleType("homeassistant.helpers")
    helpers_pkg.__path__ = []
    helpers_pkg.entity_registry = helpers_module
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers_pkg)

    homeassistant_pkg = ModuleType("homeassistant")
    homeassistant_pkg.__path__ = []
    homeassistant_pkg.helpers = helpers_pkg
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant_pkg)

    dt_module = ModuleType("homeassistant.util.dt")
    dt_module.utcnow = lambda: datetime.now(timezone.utc)
    dt_module.as_local = lambda value: value
    dt_module.UTC = timezone.utc
    dt_module.as_utc = (
        lambda value: value
        if value.tzinfo is not None
        else value.replace(tzinfo=timezone.utc)
    )

    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    dt_module.parse_datetime = _parse_datetime
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
    return map_view


def test_map_view_prefers_exact_unique_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracker selection must match explicit unique_id formats before fallback."""

    map_view = _load_map_view_module(monkeypatch)

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
        map_view.er,
        "async_get",
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


def test_map_view_uses_iso_last_seen_for_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ISO last_seen strings must drive ordering and de-duplication."""

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-iso"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "ISO Device"}])
    entry = _StubEntry("entry-iso", coordinator)

    registry = _StubEntityRegistry(
        [
            _StubEntityEntry(
                entity_id="device_tracker.googlefindmy_primary",
                unique_id=f"{entry.entry_id}:{device_id}",
                config_entry_id=entry.entry_id,
            )
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
        map_view.er,
        "async_get",
        lambda _hass: registry,
        raising=False,
    )

    iso_old = "2024-01-01T00:00:00Z"
    iso_new = "2024-01-02T00:00:00Z"
    history_states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                "last_seen": iso_old,
                "gps_accuracy": 5,
            },
            last_updated=datetime(2024, 7, 1, tzinfo=timezone.utc),
            state="one",
        ),
        SimpleNamespace(
            attributes={
                "latitude": "11.0",
                "longitude": "21.0",
                "last_seen": iso_new,
                "gps_accuracy": 10,
            },
            last_updated=datetime(2024, 5, 1, tzinfo=timezone.utc),
            state="two",
        ),
        SimpleNamespace(
            attributes={
                "latitude": "11.5",
                "longitude": "21.5",
                "last_seen": iso_new,
                "gps_accuracy": 15,
            },
            last_updated=datetime(2024, 8, 1, tzinfo=timezone.utc),
            state="duplicate",
        ),
    ]

    def _stub_history(
        _hass: Any, _start: Any, _end: Any, entity_ids: list[str]
    ) -> dict[str, Any]:
        assert entity_ids == ["device_tracker.googlefindmy_primary"]
        return {"device_tracker.googlefindmy_primary": history_states}

    history_module = ModuleType("homeassistant.components.recorder.history")
    history_module.get_significant_states = _stub_history
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.history",
        history_module,
    )

    captured_locations: list[dict[str, Any]] = []

    def _capture_html(
        self: Any,
        _device_name: str,
        locations: list[dict[str, Any]],
        *_args: Any,
        **_kwargs: Any,
    ) -> str:
        captured_locations.extend(locations)
        return "ok"

    monkeypatch.setattr(
        map_view.GoogleFindMyMapView,
        "_generate_map_html",
        _capture_html,
        raising=False,
    )

    hass = _StubHass()
    view = map_view.GoogleFindMyMapView(hass)

    request = SimpleNamespace(query={"token": "valid"})
    response = asyncio.run(view.get(request, device_id))

    assert response.status == 200
    assert len(captured_locations) == 2

    iso_old_ts = datetime.fromisoformat(iso_old.replace("Z", "+00:00")).timestamp()
    iso_new_ts = datetime.fromisoformat(iso_new.replace("Z", "+00:00")).timestamp()

    assert captured_locations[0]["last_seen"] == pytest.approx(iso_old_ts)
    assert captured_locations[1]["last_seen"] == pytest.approx(iso_new_ts)
    assert captured_locations[0]["last_seen"] < captured_locations[1]["last_seen"]
    assert captured_locations[0]["last_seen"] != pytest.approx(
        history_states[0].last_updated.timestamp()
    )
