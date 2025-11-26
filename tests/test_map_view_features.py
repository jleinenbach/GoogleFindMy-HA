"""Feature coverage for the map view endpoint."""

from __future__ import annotations

import importlib
import sys
from datetime import datetime
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.util.dt import UTC

from custom_components.googlefindmy import map_view as _map_view

map_view = _map_view

if getattr(map_view, "__file__", None) is None:
    importlib.invalidate_caches()
    map_view = importlib.reload(
        importlib.import_module("custom_components.googlefindmy.map_view")
    )
from custom_components.googlefindmy.const import (
    DOMAIN,
    map_token_hex_digest,
    map_token_secret_seed,
)


class _StubConfigEntries:
    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[Any]:
        return list(self._entries) if domain == DOMAIN else []


class _StubRegistryEntry:
    def __init__(self, *, entity_id: str, unique_id: str, config_entry_id: str) -> None:
        self.entity_id = entity_id
        self.unique_id = unique_id
        self.config_entry_id = config_entry_id
        self.platform = DOMAIN


class _StubEntityRegistry:
    def __init__(self, entries: list[_StubRegistryEntry]) -> None:
        self.entities = {entry.entity_id: entry for entry in entries}

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        for entry in self.entities.values():
            if (
                entry.platform == platform
                and entry.unique_id == unique_id
                and entry.entity_id.startswith(f"{domain}.")
            ):
                return entry.entity_id
        return None

    def async_get(self, entity_id: str) -> _StubRegistryEntry | None:
        return self.entities.get(entity_id)


class _StubState:
    def __init__(self, *, latitude: float, longitude: float) -> None:
        self.attributes: dict[str, Any] = {
            "latitude": latitude,
            "longitude": longitude,
            "gps_accuracy": 5,
            "semantic_name": "Office",
            "is_own_report": True,
        }
        self.state = "home"
        self.last_updated = datetime(2024, 1, 1, tzinfo=UTC)


class _StubHass:
    def __init__(self, entries: list[Any]) -> None:
        self.data: dict[str, Any] = {"core.uuid": "test-ha"}
        self.config_entries = _StubConfigEntries(entries)

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


class _StubEntry:
    def __init__(self, entry_id: str, runtime_data: Any) -> None:
        self.entry_id = entry_id
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.runtime_data = runtime_data


def _install_history_stub(
    monkeypatch: pytest.MonkeyPatch,
    entity_id: str,
    state: _StubState,
    *,
    calls: list[str] | None = None,
) -> None:
    history_module = ModuleType("homeassistant.components.recorder.history")

    def _get_significant_states(_hass: Any, _start: Any, _end: Any, _entity_ids: list[str]) -> dict[str, list[_StubState]]:
        if calls is not None:
            calls.extend(_entity_ids)
        return {entity_id: [state]}

    history_module.get_significant_states = _get_significant_states  # type: ignore[attr-defined]

    recorder_module = ModuleType("homeassistant.components.recorder")
    recorder_module.history = history_module
    components_module = ModuleType("homeassistant.components")
    components_module.recorder = recorder_module

    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder.history", history_module)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder_module)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components_module)


@pytest.mark.asyncio
async def test_get_missing_token_returns_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return 401 when no token is provided."""

    hass = _StubHass([])
    view = map_view.GoogleFindMyMapView(hass)

    response = await view.get(SimpleNamespace(query={}), device_id="device123")

    assert response.status == 401


@pytest.mark.asyncio
async def test_get_invalid_token_returns_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return 401 when token does not match any entry."""

    entry = _StubEntry("entry-id", runtime_data=None)
    hass = _StubHass([entry])
    view = map_view.GoogleFindMyMapView(hass)

    response = await view.get(SimpleNamespace(query={"token": "invalid"}), device_id="device123")

    assert response.status == 401


@pytest.mark.asyncio
async def test_get_authorized_includes_leaflet(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return 200 HTML with Leaflet content when authorized."""

    device_id = "device123"
    coordinator = SimpleNamespace(data=[{"id": device_id, "name": "Test Device"}])
    entry = _StubEntry("entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    def _resolve() -> type[Any]:
        return SimpleNamespace

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", _resolve)

    registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.device123",
        unique_id=f"{entry.entry_id}:{device_id}",
        config_entry_id=entry.entry_id,
    )
    registry = _StubEntityRegistry([registry_entry])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    state = _StubState(latitude=10.0, longitude=20.0)
    _install_history_stub(monkeypatch, registry_entry.entity_id, state)

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    response = await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query={"token": token}),
        device_id=device_id,
    )

    assert response.status == 200
    assert "leaflet" in response.text.lower()


@pytest.mark.asyncio
async def test_get_skips_foreign_registry_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not leak history from a tracker bound to a different config entry."""

    device_id = "device456"
    coordinator = SimpleNamespace(data=[{"id": device_id, "name": "Foreign Device"}])
    entry = _StubEntry("entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    def _resolve() -> type[Any]:
        return SimpleNamespace

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", _resolve)

    registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.device456",
        unique_id=f"{entry.entry_id}:{device_id}",
        config_entry_id="other-entry",
    )
    registry = _StubEntityRegistry([registry_entry])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    # Guard against history lookups when entity_id is rejected
    history_module = ModuleType("homeassistant.components.recorder.history")

    def _get_significant_states(_hass: Any, _start: Any, _end: Any, _entity_ids: list[str]) -> dict[str, list[_StubState]]:
        raise AssertionError("history should not be queried for mismatched entries")

    history_module.get_significant_states = _get_significant_states  # type: ignore[attr-defined]
    recorder_module = ModuleType("homeassistant.components.recorder")
    recorder_module.history = history_module
    components_module = ModuleType("homeassistant.components")
    components_module.recorder = recorder_module

    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder.history", history_module)
    monkeypatch.setitem(sys.modules, "homeassistant.components.recorder", recorder_module)
    monkeypatch.setitem(sys.modules, "homeassistant.components", components_module)

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    response = await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query={"token": token}),
        device_id=device_id,
    )

    assert response.status == 200
    assert "leaflet" in response.text.lower()


@pytest.mark.asyncio
async def test_get_prefers_matching_entry_over_foreign_match(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use registry entries scoped to the token's config entry even with competing IDs."""

    device_id = "device789"
    coordinator = SimpleNamespace(data=[{"id": device_id, "name": "Scoped Device"}])
    entry = _StubEntry("entry-id", runtime_data=coordinator)
    hass = _StubHass([entry])

    def _resolve() -> type[Any]:
        return SimpleNamespace

    monkeypatch.setattr(map_view, "_resolve_coordinator_class", _resolve)

    foreign_registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.foreign_device",
        unique_id=f"{entry.entry_id}:{device_id}",
        config_entry_id="foreign-entry",
    )
    scoped_registry_entry = _StubRegistryEntry(
        entity_id="device_tracker.scoped_device",
        unique_id=f"{DOMAIN}_{device_id}",
        config_entry_id=entry.entry_id,
    )
    registry = _StubEntityRegistry([foreign_registry_entry, scoped_registry_entry])
    monkeypatch.setattr(map_view.er, "async_get", lambda _hass: registry)

    state = _StubState(latitude=15.0, longitude=25.0)
    history_calls: list[str] = []
    _install_history_stub(
        monkeypatch,
        scoped_registry_entry.entity_id,
        state,
        calls=history_calls,
    )

    ha_uuid = hass.data["core.uuid"]
    secret = map_token_secret_seed(ha_uuid, entry.entry_id, False)
    token = map_token_hex_digest(secret)

    response = await map_view.GoogleFindMyMapView(hass).get(
        SimpleNamespace(query={"token": token}),
        device_id=device_id,
    )

    assert response.status == 200
    assert "leaflet" in response.text.lower()
    assert history_calls == [scoped_registry_entry.entity_id]
