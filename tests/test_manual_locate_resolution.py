# tests/test_manual_locate_resolution.py
"""Tests for canonical resolution and manual locate helpers."""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

if "custom_components.googlefindmy.diagnostics" not in sys.modules:
    sys.modules["custom_components.googlefindmy.diagnostics"] = ModuleType(
        "custom_components.googlefindmy.diagnostics"
    )

if "custom_components.googlefindmy.map_view" not in sys.modules:
    map_module = ModuleType("custom_components.googlefindmy.map_view")

    class _DummyView:  # pragma: no cover - stub for import
        pass

    map_module.GoogleFindMyMapRedirectView = _DummyView
    map_module.GoogleFindMyMapView = _DummyView
    sys.modules["custom_components.googlefindmy.map_view"] = map_module

gfm = importlib.import_module("custom_components.googlefindmy.__init__")


class _StubDeviceRegistry:
    """Minimal device registry stub with async_get lookup."""

    def __init__(self, mapping: dict[str, SimpleNamespace] | None = None) -> None:
        self._mapping: dict[str, SimpleNamespace] = mapping or {}

    def async_get(self, device_id: str) -> SimpleNamespace | None:
        return self._mapping.get(device_id)


class _StubEntityRegistry:
    """Minimal entity registry stub with async_get lookup."""

    def __init__(self, mapping: dict[str, SimpleNamespace] | None = None) -> None:
        self._mapping: dict[str, SimpleNamespace] = mapping or {}

    def async_get(self, entity_id: str) -> SimpleNamespace | None:
        return self._mapping.get(entity_id)


@pytest.fixture
def hass() -> HomeAssistant:
    """Return a fresh HomeAssistant stub for each test."""

    return HomeAssistant()


@pytest.fixture
def registries(monkeypatch: pytest.MonkeyPatch) -> tuple[_StubDeviceRegistry, _StubEntityRegistry]:
    """Patch integration registries with mutable stubs for each test."""

    device_registry = _StubDeviceRegistry()
    entity_registry = _StubEntityRegistry()
    monkeypatch.setattr(gfm.dr, "async_get", lambda _hass: device_registry)
    monkeypatch.setattr(gfm.er, "async_get", lambda _hass: entity_registry)
    return device_registry, entity_registry


def test_resolve_canonical_from_device_id(
    hass: HomeAssistant, registries: tuple[_StubDeviceRegistry, _StubEntityRegistry]
) -> None:
    """Device IDs map to canonical identifiers using the device registry."""

    device_registry, _ = registries
    device_registry._mapping["device-1"] = SimpleNamespace(  # type: ignore[attr-defined]
        id="ha-device-1",
        identifiers={(gfm.DOMAIN, "canonical-1234")},
        name="Tracker",
        name_by_user="My Keys",
    )
    canonical_id, friendly_name = gfm._resolve_canonical_from_any(hass, "device-1")
    assert canonical_id == "canonical-1234"
    assert friendly_name == "My Keys"


def test_resolve_canonical_from_entity_id(
    hass: HomeAssistant, registries: tuple[_StubDeviceRegistry, _StubEntityRegistry]
) -> None:
    """Entity IDs resolve to canonical identifiers through their linked device."""

    device_registry, entity_registry = registries
    device_registry._mapping["ha-device-2"] = SimpleNamespace(  # type: ignore[attr-defined]
        id="ha-device-2",
        identifiers={(gfm.DOMAIN, "canonical-5678")},
        name="Backpack Tag",
        name_by_user=None,
    )
    entity_registry._mapping["device_tracker.googlefindmy_tracker"] = SimpleNamespace(  # type: ignore[attr-defined]
        platform=gfm.DOMAIN,
        device_id="ha-device-2",
    )
    canonical_id, friendly_name = gfm._resolve_canonical_from_any(
        hass, "device_tracker.googlefindmy_tracker"
    )
    assert canonical_id == "canonical-5678"
    assert friendly_name == "Backpack Tag"


def test_resolve_canonical_invalid_device_identifiers(
    hass: HomeAssistant, registries: tuple[_StubDeviceRegistry, _StubEntityRegistry]
) -> None:
    """Devices missing integration identifiers raise HomeAssistantError."""

    device_registry, _ = registries
    device_registry._mapping["device-3"] = SimpleNamespace(  # type: ignore[attr-defined]
        id="ha-device-3",
        identifiers={("other", "value")},
        name="Unrelated",
        name_by_user=None,
    )
    with pytest.raises(HomeAssistantError) as err:
        gfm._resolve_canonical_from_any(hass, "device-3")
    assert "Device 'device-3' has no valid" in str(err.value)


def test_resolve_canonical_invalid_entity_mapping(
    hass: HomeAssistant, registries: tuple[_StubDeviceRegistry, _StubEntityRegistry]
) -> None:
    """Entities without a valid linked device raise HomeAssistantError."""

    _, entity_registry = registries
    entity_registry._mapping["device_tracker.googlefindmy_missing"] = SimpleNamespace(  # type: ignore[attr-defined]
        platform=gfm.DOMAIN,
        device_id="missing-device",
    )
    with pytest.raises(HomeAssistantError) as err:
        gfm._resolve_canonical_from_any(hass, "device_tracker.googlefindmy_missing")
    assert "is not linked to a valid" in str(err.value)


def test_resolve_canonical_passthrough(
    hass: HomeAssistant, registries: tuple[_StubDeviceRegistry, _StubEntityRegistry]
) -> None:
    """Canonical identifiers pass through untouched when registries have no match."""

    canonical_id, friendly_name = gfm._resolve_canonical_from_any(hass, "abc123")
    assert (canonical_id, friendly_name) == ("abc123", "abc123")


class _StubCoordinator:
    """Coordinator stub capturing locate requests and diagnostics."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._diag = SimpleNamespace(errors=[])

    async def async_locate_device(self, canonical_id: str) -> None:
        self.calls.append(canonical_id)


def test_async_handle_manual_locate_success(
    hass: HomeAssistant, registries: tuple[_StubDeviceRegistry, _StubEntityRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    """Manual locate dispatches to the coordinator and logs success."""

    device_registry, _ = registries
    device_registry._mapping["device-4"] = SimpleNamespace(  # type: ignore[attr-defined]
        id="ha-device-4",
        identifiers={(gfm.DOMAIN, "canonical-4")},
        name="Bike Tag",
        name_by_user="Bicycle",
    )
    coordinator = _StubCoordinator()
    caplog.set_level("INFO")
    asyncio.run(gfm.async_handle_manual_locate(hass, coordinator, "device-4"))
    assert coordinator.calls == ["canonical-4"]
    assert "Successfully submitted manual locate for Bicycle" in caplog.text


def test_async_handle_manual_locate_namespaced_identifier(
    hass: HomeAssistant, registries: tuple[_StubDeviceRegistry, _StubEntityRegistry]
) -> None:
    """Namespaced registry identifiers are normalized before dispatch."""

    device_registry, _ = registries
    device_registry._mapping["device-namespace"] = SimpleNamespace(  # type: ignore[attr-defined]
        id="ha-device-ns",
        identifiers={(gfm.DOMAIN, "entry-1:canonical-ns")},
        name="Namespaced Tag",
        name_by_user=None,
        config_entries={"entry-1"},
    )
    coordinator = _StubCoordinator()
    asyncio.run(
        gfm.async_handle_manual_locate(hass, coordinator, "device-namespace")
    )
    assert coordinator.calls == ["canonical-ns"]


class _DiagRecorder:
    """Diagnostics stub recording add_error calls."""

    def __init__(self) -> None:
        self.errors: list[tuple[str, dict[str, str]]] = []

    def add_error(self, *, code: str, context: dict[str, str]) -> None:
        self.errors.append((code, context))


class _FailingCoordinator(_StubCoordinator):
    """Coordinator stub raising during locate to exercise error handling."""

    def __init__(self) -> None:
        super().__init__()
        self._diag = _DiagRecorder()

    async def async_locate_device(self, canonical_id: str) -> None:
        raise HomeAssistantError("locate failed")


def test_async_handle_manual_locate_failure_adds_diagnostic(
    hass: HomeAssistant, registries: tuple[_StubDeviceRegistry, _StubEntityRegistry], caplog: pytest.LogCaptureFixture
) -> None:
    """Resolution failures propagate errors and add diagnostics."""

    device_registry, _ = registries
    device_registry._mapping["device-5"] = SimpleNamespace(  # type: ignore[attr-defined]
        id="ha-device-5",
        identifiers={(gfm.DOMAIN, "canonical-5")},
        name="Broken",
        name_by_user=None,
    )
    coordinator = _FailingCoordinator()
    caplog.set_level("ERROR")
    with pytest.raises(HomeAssistantError):
        asyncio.run(gfm.async_handle_manual_locate(hass, coordinator, "device-5"))
    assert coordinator._diag.errors  # type: ignore[attr-defined]
    code, context = coordinator._diag.errors[0]  # type: ignore[index]
    assert code == "manual_locate_resolution_failed"
    assert context["arg"] == "device-5"
    assert "Locate failed" in caplog.text
