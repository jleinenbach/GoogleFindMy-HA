# tests/test_services_forward_compat.py

"""Forward compatibility tests for entity service registration helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from custom_components.googlefindmy import services
from custom_components.googlefindmy.const import DOMAIN, SERVICE_LOCATE_DEVICE

from custom_components.googlefindmy.util_services import register_entity_service


class _PlatformRecorder:
    """Capture calls to entity service registration methods for assertions."""

    def __init__(self, *, raises_in_new: bool = False) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.raises_in_new = raises_in_new
        self.new_attempted = False

    def async_register_platform_entity_service(self, *args: Any) -> None:
        self.new_attempted = True
        if self.raises_in_new:
            raise TypeError("simulated signature mismatch")
        self.calls.append(("new", args))

    def async_register_entity_service(self, *args: Any) -> None:
        self.calls.append(("legacy", args))


def _integration_root() -> Path:
    return Path(__file__).resolve().parents[1] / "custom_components" / "googlefindmy"


def test_register_entity_service_prefers_platform_specific() -> None:
    """The wrapper should call the new API when available."""

    platform = _PlatformRecorder()
    register_entity_service(platform, "svc", {"field": "value"}, "handler")
    assert platform.new_attempted is True
    assert platform.calls == [("new", ("svc", {"field": "value"}, "handler"))]


def test_register_entity_service_falls_back_on_type_error() -> None:
    """If the new API raises a TypeError, fall back to the legacy method."""

    platform = _PlatformRecorder(raises_in_new=True)
    register_entity_service(platform, "svc", {"field": "value"}, "handler")
    assert platform.new_attempted is True
    assert platform.calls == [("legacy", ("svc", {"field": "value"}, "handler"))]


def test_register_entity_service_legacy_only() -> None:
    """When only the legacy method exists, the wrapper uses it directly."""

    class LegacyOnlyPlatform:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def async_register_entity_service(self, *args: Any) -> None:
            self.calls.append(args)

    platform = LegacyOnlyPlatform()
    register_entity_service(platform, "svc", {"field": "value"}, "handler")
    assert platform.calls == [("svc", {"field": "value"}, "handler")]


def test_register_entity_service_legacy_duplicate_registration() -> None:
    """Duplicate legacy registrations should be ignored to support multiple entries."""

    class LegacyDuplicatePlatform:
        def __init__(self) -> None:
            self.successful_calls: list[tuple[Any, ...]] = []
            self.duplicate_attempts = 0

        def async_register_entity_service(self, *args: Any) -> None:
            if self.successful_calls:
                self.duplicate_attempts += 1
                raise ValueError("already registered")
            self.successful_calls.append(args)

    platform = LegacyDuplicatePlatform()

    register_entity_service(platform, "svc", {"field": "value"}, "handler")
    register_entity_service(platform, "svc", {"field": "value"}, "handler")

    assert platform.successful_calls == [("svc", {"field": "value"}, "handler")]
    assert platform.duplicate_attempts == 1


@pytest.mark.parametrize(
    ("display_value", "location_value", "expect_location_lookup"),
    [("", None, False), (None, {"ok": True}, True)],
)
def test_locate_service_handles_falsy_display_names(
    monkeypatch: pytest.MonkeyPatch,
    display_value: str | None,
    location_value: Any,
    expect_location_lookup: bool,
) -> None:
    """Fallback runtime scan should accept coordinators with falsy display names."""

    class _StubCoordinator:
        def __init__(self) -> None:
            self.display_queries: list[str] = []
            self.location_queries: list[str] = []
            self.locate_calls: list[str] = []

        def get_device_display_name(self, canonical_id: str) -> Any:
            self.display_queries.append(canonical_id)
            return display_value

        def get_device_location_data(self, canonical_id: str) -> Any:
            self.location_queries.append(canonical_id)
            return location_value

        async def async_locate_device(self, canonical_id: str) -> None:
            self.locate_calls.append(canonical_id)

    class _StubRuntime:
        def __init__(self, coordinator: _StubCoordinator) -> None:
            self.coordinator = coordinator

    class _StubConfigEntry:
        def __init__(self, entry_id: str, runtime: _StubRuntime) -> None:
            self.entry_id = entry_id
            self.domain = DOMAIN
            self.runtime_data = runtime
            self.title = "stub"
            self.options: dict[str, Any] = {}
            self.data: dict[str, Any] = {}

    class _StubConfigManager:
        def __init__(self, entries: dict[str, _StubConfigEntry]) -> None:
            self._entries = entries
            self.reload_calls: list[str] = []

        def async_entries(self, domain: str) -> list[_StubConfigEntry]:
            if domain != DOMAIN:
                return []
            return list(self._entries.values())

        def async_get_entry(self, entry_id: str) -> _StubConfigEntry | None:
            return self._entries.get(entry_id)

        async def async_reload(self, entry_id: str) -> None:
            self.reload_calls.append(entry_id)

    class _StubServices:
        def __init__(self) -> None:
            self.registered: dict[tuple[str, str], Any] = {}

        def async_register(self, domain: str, service: str, handler: Any) -> None:
            self.registered[(domain, service)] = handler

    class _StubHass:
        def __init__(self, config_manager: _StubConfigManager) -> None:
            self.config_entries = config_manager
            self.services = _StubServices()
            self.data: dict[str, Any] = {}

    class _StubDeviceRegistry:
        def __init__(self) -> None:
            self.devices: dict[str, Any] = {}

        def async_get(self, device_id: str) -> Any | None:
            return None

    class _StubServiceCall:
        def __init__(self, data: dict[str, Any]) -> None:
            self.data = data

    coordinator = _StubCoordinator()
    runtime = _StubRuntime(coordinator)
    entry_id = "entry-1"
    config_entry = _StubConfigEntry(entry_id, runtime)
    config_manager = _StubConfigManager({entry_id: config_entry})
    hass = _StubHass(config_manager)
    hass.data.setdefault(DOMAIN, {}).setdefault("entries", {})[entry_id] = runtime

    device_registry = _StubDeviceRegistry()
    monkeypatch.setattr(services.dr, "async_get", lambda hass: device_registry)

    canonical_id = "canonical-123"
    ctx = {
        "resolve_canonical": lambda _hass, raw_id: (canonical_id, "friendly"),
        "is_active_entry": lambda entry: True,
    }

    async def _invoke() -> None:
        await services.async_register_services(hass, ctx)
        handler = hass.services.registered[(DOMAIN, SERVICE_LOCATE_DEVICE)]

        service_call = _StubServiceCall({"device_id": "device-42"})

        await handler(service_call)

    asyncio.run(_invoke())

    assert coordinator.locate_calls == [canonical_id]
    assert coordinator.display_queries == [canonical_id]
    if expect_location_lookup:
        assert coordinator.location_queries == [canonical_id]
    else:
        assert coordinator.location_queries == []


@pytest.mark.parametrize(
    "needle",
    ["async_register_entity_service(", "async_register_platform_entity_service("],
)
def test_platforms_use_wrapper_exclusively(needle: str) -> None:
    """Integration code should not call HA APIs directly anymore."""

    offenders: list[Path] = []
    for path in _integration_root().rglob("*.py"):
        if path.name == "util_services.py":
            continue
        text = path.read_text(encoding="utf-8")
        if needle in text:
            offenders.append(path.relative_to(_integration_root()))
    assert offenders == []


def test_wrapper_used_somewhere_in_platforms() -> None:
    """The wrapper must be exercised by at least one integration module."""

    wrapper_callers: list[Path] = []
    for path in _integration_root().rglob("*.py"):
        if path.name == "util_services.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "register_entity_service(" in text:
            wrapper_callers.append(path.relative_to(_integration_root()))
    assert wrapper_callers, (
        "Expected at least one module to invoke register_entity_service"
    )
