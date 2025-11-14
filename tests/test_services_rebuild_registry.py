# tests/test_services_rebuild_registry.py
"""Regression tests for the googlefindmy.rebuild_registry service."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import services
from custom_components.googlefindmy.const import (
    DOMAIN,
    SERVICE_REBUILD_REGISTRY,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from homeassistant.core import ServiceCall

from tests.helpers import (
    FakeConfigEntriesManager,
    FakeConfigEntry,
    FakeEntityRegistry,
    FakeHass,
    device_registry_async_entries_for_config_entry,
    service_device_stub,
)


async def _register_rebuild_service(hass: FakeHass, ctx: dict[str, Any]) -> Any:
    """Helper to register the rebuild service and return its handler."""

    await services.async_register_services(hass, ctx)
    return hass.services.handlers[(DOMAIN, SERVICE_REBUILD_REGISTRY)]


@pytest.mark.asyncio
async def test_rebuild_registry_reloads_primary_entry(caplog: pytest.LogCaptureFixture) -> None:
    """When no entry IDs are provided, reload the first config entry."""

    manager = FakeConfigEntriesManager(
        [
            FakeConfigEntry(entry_id="primary"),
            FakeConfigEntry(entry_id="secondary"),
        ]
    )
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(ServiceCall({}))

    assert manager.reload_calls == ["primary"]
    assert any(
        "Reloading config entry: primary" in record.message for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_reloads_specific_ids(caplog: pytest.LogCaptureFixture) -> None:
    """Reload only the config entries explicitly requested by ID."""

    manager = FakeConfigEntriesManager(
        [
            FakeConfigEntry(entry_id="primary"),
            FakeConfigEntry(entry_id="secondary"),
            FakeConfigEntry(entry_id="tertiary"),
        ]
    )
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(
        ServiceCall({services.ATTR_ENTRY_ID: ["secondary", "missing", "primary"]})
    )

    assert manager.reload_calls == ["secondary", "primary"]
    assert any(
        "Reloading config entries: ['secondary', 'primary']" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_accepts_single_entry_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Treat a lone entry ID string as a single-item reload request."""

    manager = FakeConfigEntriesManager(
        [
            FakeConfigEntry(entry_id="primary"),
            FakeConfigEntry(entry_id="secondary"),
        ]
    )
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(ServiceCall({services.ATTR_ENTRY_ID: "primary"}))

    assert manager.reload_calls == ["primary"]
    assert any(
        "Reloading config entries: ['primary']" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_logs_warning_for_invalid_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Warn and exit when none of the provided entry IDs are valid."""

    manager = FakeConfigEntriesManager(
        [
            FakeConfigEntry(entry_id="primary"),
        ]
    )
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(
        ServiceCall({services.ATTR_ENTRY_ID: ["missing-1", "missing-2"]})
    )

    assert manager.reload_calls == []
    assert any(
        "No valid config entries found for IDs" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_handles_missing_entries(caplog: pytest.LogCaptureFixture) -> None:
    """Gracefully warn when the integration has no config entries to reload."""

    manager = FakeConfigEntriesManager([])
    hass = FakeHass(manager)

    handler = await _register_rebuild_service(hass, {})

    caplog.set_level(logging.INFO)
    await handler(ServiceCall({}))

    assert manager.reload_calls == []
    assert any(
        "No config entries available to reload." in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rebuild_registry_detaches_orphaned_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove hub links when a tracker lacks the matching subentry."""

    entry = FakeConfigEntry(entry_id="hub-entry", title="Hub Entry")
    tracker_subentry_id = "tracker-subentry"
    tracker_entry_id = "tracker-config-entry"
    service_subentry_id = "service-subentry"

    service_device = service_device_stub(
        entry_id=entry.entry_id,
        service_subentry_id=service_subentry_id,
        device_id="service-device",
        name="Service Device",
        include_hub_link=True,
    )
    orphan_tracker = SimpleNamespace(
        id="orphan-tracker",
        identifiers={(DOMAIN, "tracker-orphan")},
        config_entries={entry.entry_id},
        name="Orphan Tracker",
        config_entries_subentries={entry.entry_id: {None}},
        config_subentry_id=None,
    )

    class RecordingDeviceRegistry:
        """Minimal registry stub tracking update calls for assertions."""

        def __init__(self, devices: Iterable[Any]) -> None:
            self._devices = {device.id: device for device in devices}
            self._identifier_index = {
                identifier: device
                for device in devices
                for identifier in device.identifiers
            }
            self.updated: list[tuple[str, dict[str, Any]]] = []

        def async_get_device(
            self, *, identifiers: set[tuple[str, str]] | None = None, **_: Any
        ) -> Any | None:
            if not identifiers:
                return None
            for identifier in identifiers:
                device = self._identifier_index.get(identifier)
                if device is not None:
                    return device
            return None

        def async_entries_for_config_entry(
            self, entry_id: str
        ) -> tuple[Any, ...]:
            return tuple(
                device
                for device in self._devices.values()
                if entry_id in device.config_entries
            )

        def async_update_device(self, device_id: str, **changes: Any) -> None:
            device = self._devices[device_id]
            if "remove_config_entry_id" in changes:
                entry_to_remove = changes["remove_config_entry_id"]
                mapping = getattr(device, "config_entries_subentries", None)
                removed_entry = False
                if isinstance(mapping, dict):
                    subset = mapping.get(entry_to_remove)
                    if subset is not None:
                        if changes.get("remove_config_subentry_id") is None:
                            if None in subset:
                                subset.discard(None)
                            if not subset:
                                mapping.pop(entry_to_remove, None)
                                removed_entry = True
                        else:
                            subset.discard(changes["remove_config_subentry_id"])
                            if not subset:
                                mapping.pop(entry_to_remove, None)
                                removed_entry = True
                    else:
                        removed_entry = True
                else:
                    removed_entry = True

                if removed_entry:
                    device.config_entries.discard(entry_to_remove)
                elif isinstance(mapping, dict):
                    subset = mapping.get(entry_to_remove)
                    if subset:
                        non_null = [item for item in subset if item is not None]
                        if non_null and len(subset - {None}) == 1:
                            device.config_subentry_id = non_null[0]
                        elif len(subset) == 1 and None in subset:
                            device.config_subentry_id = None
                if removed_entry and not getattr(device, "config_entries_subentries", {}):
                    device.config_subentry_id = None
            self.updated.append((device_id, dict(changes)))

    registry = RecordingDeviceRegistry([service_device, orphan_tracker])

    manager = FakeConfigEntriesManager([entry])
    hass = FakeHass(manager)

    tracker_metadata = SimpleNamespace(
        config_subentry_id=tracker_subentry_id,
        entry_id=tracker_entry_id,
    )
    service_metadata = SimpleNamespace(
        config_subentry_id=service_subentry_id,
        entry_id="service-config-entry",
    )

    def _metadata(*, key: str) -> SimpleNamespace | None:
        if key == TRACKER_SUBENTRY_KEY:
            return tracker_metadata
        if key == SERVICE_SUBENTRY_KEY:
            return service_metadata
        return None

    coordinator = SimpleNamespace(
        config_entry=entry,
        data=[],
        name="Coordinator",
        _ensure_registry_for_devices=lambda devices, ignored: 0,
        _get_ignored_set=lambda: set(),
        _ensure_service_device_exists=lambda: None,
        get_subentry_metadata=_metadata,
    )

    runtime = SimpleNamespace(coordinator=coordinator)
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {}).setdefault("entries", {})[entry.entry_id] = runtime

    monkeypatch.setattr(
        "custom_components.googlefindmy.services.dr.async_get",
        lambda hass: registry,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.services.dr.async_entries_for_config_entry",
        device_registry_async_entries_for_config_entry,
        raising=False,
    )

    entity_registry = FakeEntityRegistry()
    monkeypatch.setattr(
        "custom_components.googlefindmy.services.er.async_get",
        lambda hass: entity_registry,
    )

    await services.async_rebuild_device_registry(hass, ServiceCall({}))

    assert len(registry.updated) == 2
    assert (
        "service-device",
        {
            "remove_config_entry_id": entry.entry_id,
            "remove_config_subentry_id": None,
        },
    ) in registry.updated
    assert (
        "orphan-tracker",
        {
            "remove_config_entry_id": entry.entry_id,
            "remove_config_subentry_id": None,
        },
    ) in registry.updated
    assert entry.entry_id not in orphan_tracker.config_entries
    assert service_device.config_entries_subentries[entry.entry_id] == {
        service_subentry_id
    }
    assert service_device.config_subentry_id == service_subentry_id
    assert entry.entry_id in service_device.config_entries


@pytest.mark.asyncio
async def test_rebuild_registry_detaches_redundant_hub_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Remove hub links even when the tracker subentry is already linked."""

    entry = FakeConfigEntry(entry_id="hub-entry", title="Hub Entry")
    tracker_subentry_id = "tracker-subentry"
    tracker_entry_id = "tracker-config-entry"
    service_subentry_id = "service-subentry"

    service_device = service_device_stub(
        entry_id=entry.entry_id,
        service_subentry_id=service_subentry_id,
        device_id="service-device",
        name="Service Device",
        include_hub_link=True,
    )
    redundant_tracker = SimpleNamespace(
        id="tracker-device",
        identifiers={(DOMAIN, "tracker-device")},
        config_entries={entry.entry_id, tracker_entry_id},
        name="Tracker Device",
        config_subentry_id=tracker_subentry_id,
        config_entries_subentries={
            entry.entry_id: {None},
            tracker_entry_id: {tracker_subentry_id},
        },
    )

    class RecordingDeviceRegistry:
        """Minimal registry stub tracking update calls for assertions."""

        def __init__(self, devices: Iterable[Any]) -> None:
            self._devices = {device.id: device for device in devices}
            self._identifier_index = {
                identifier: device
                for device in devices
                for identifier in device.identifiers
            }
            self.updated: list[tuple[str, dict[str, Any]]] = []

        def async_get_device(
            self, *, identifiers: set[tuple[str, str]] | None = None, **_: Any
        ) -> Any | None:
            if not identifiers:
                return None
            for identifier in identifiers:
                device = self._identifier_index.get(identifier)
                if device is not None:
                    return device
            return None

        def async_entries_for_config_entry(
            self, entry_id: str
        ) -> tuple[Any, ...]:
            return tuple(
                device
                for device in self._devices.values()
                if entry_id in device.config_entries
            )

        def async_update_device(self, device_id: str, **changes: Any) -> None:
            device = self._devices[device_id]
            if "remove_config_entry_id" in changes:
                entry_to_remove = changes["remove_config_entry_id"]
                mapping = getattr(device, "config_entries_subentries", None)
                removed_entry = False
                if isinstance(mapping, dict):
                    subset = mapping.get(entry_to_remove)
                    if subset is not None:
                        if changes.get("remove_config_subentry_id") is None:
                            if None in subset:
                                subset.discard(None)
                            if not subset:
                                mapping.pop(entry_to_remove, None)
                                removed_entry = True
                        else:
                            subset.discard(changes["remove_config_subentry_id"])
                            if not subset:
                                mapping.pop(entry_to_remove, None)
                                removed_entry = True
                    else:
                        removed_entry = True
                else:
                    removed_entry = True

                if removed_entry:
                    device.config_entries.discard(entry_to_remove)
                elif isinstance(mapping, dict):
                    subset = mapping.get(entry_to_remove)
                    if subset:
                        non_null = [item for item in subset if item is not None]
                        if non_null and len(subset - {None}) == 1:
                            device.config_subentry_id = non_null[0]
                        elif len(subset) == 1 and None in subset:
                            device.config_subentry_id = None
                if removed_entry and not getattr(device, "config_entries_subentries", {}):
                    device.config_subentry_id = None
            self.updated.append((device_id, dict(changes)))

    registry = RecordingDeviceRegistry([service_device, redundant_tracker])

    manager = FakeConfigEntriesManager([entry])
    hass = FakeHass(manager)

    tracker_metadata = SimpleNamespace(
        config_subentry_id=tracker_subentry_id,
        entry_id=tracker_entry_id,
    )
    service_metadata = SimpleNamespace(
        config_subentry_id=service_subentry_id,
        entry_id="service-config-entry",
    )

    def _metadata(*, key: str) -> SimpleNamespace | None:
        if key == TRACKER_SUBENTRY_KEY:
            return tracker_metadata
        if key == SERVICE_SUBENTRY_KEY:
            return service_metadata
        return None

    coordinator = SimpleNamespace(
        config_entry=entry,
        data=[],
        name="Coordinator",
        _ensure_registry_for_devices=lambda devices, ignored: 0,
        _get_ignored_set=lambda: set(),
        _ensure_service_device_exists=lambda: None,
        get_subentry_metadata=_metadata,
    )

    runtime = SimpleNamespace(coordinator=coordinator)
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {}).setdefault("entries", {})[entry.entry_id] = runtime

    monkeypatch.setattr(
        "custom_components.googlefindmy.services.dr.async_get",
        lambda hass: registry,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.services.dr.async_entries_for_config_entry",
        device_registry_async_entries_for_config_entry,
        raising=False,
    )

    entity_registry = FakeEntityRegistry()
    monkeypatch.setattr(
        "custom_components.googlefindmy.services.er.async_get",
        lambda hass: entity_registry,
    )

    await services.async_rebuild_device_registry(hass, ServiceCall({}))

    assert len(registry.updated) == 2
    assert (
        "service-device",
        {
            "remove_config_entry_id": entry.entry_id,
            "remove_config_subentry_id": None,
        },
    ) in registry.updated
    assert (
        "tracker-device",
        {
            "remove_config_entry_id": entry.entry_id,
            "remove_config_subentry_id": None,
        },
    ) in registry.updated
    assert entry.entry_id not in redundant_tracker.config_entries
    mapping = getattr(redundant_tracker, "config_entries_subentries", {})
    assert entry.entry_id not in mapping
    assert service_device.config_entries_subentries[entry.entry_id] == {
        service_subentry_id
    }
    assert entry.entry_id in service_device.config_entries


@pytest.mark.asyncio
async def test_rebuild_registry_handles_legacy_remove_config_subentry_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry hub-detach updates when legacy cores reject the new keyword."""

    entry = FakeConfigEntry(entry_id="hub-entry", title="Hub Entry")
    tracker_subentry_id = "tracker-subentry"
    tracker_entry_id = "tracker-config-entry"
    service_subentry_id = "service-subentry"

    service_device = service_device_stub(
        entry_id=entry.entry_id,
        service_subentry_id=service_subentry_id,
        device_id="service-device",
        name="Service Device",
        include_hub_link=True,
    )
    redundant_tracker = SimpleNamespace(
        id="tracker-device",
        identifiers={(DOMAIN, "tracker-device")},
        config_entries={entry.entry_id, tracker_entry_id},
        name="Tracker Device",
        config_subentry_id=tracker_subentry_id,
        config_entries_subentries={
            entry.entry_id: {None},
            tracker_entry_id: {tracker_subentry_id},
        },
    )

    class RaisingDeviceRegistry:
        """Registry stub that raises on ``remove_config_subentry_id`` usage."""

        def __init__(self, devices: Iterable[Any]) -> None:
            self._devices = {device.id: device for device in devices}
            self._identifier_index = {
                identifier: device
                for device in devices
                for identifier in device.identifiers
            }
            self.calls: list[tuple[str, dict[str, Any]]] = []
            self.updated: list[tuple[str, dict[str, Any]]] = []

        def async_get_device(
            self, *, identifiers: set[tuple[str, str]] | None = None, **_: Any
        ) -> Any | None:
            if not identifiers:
                return None
            for identifier in identifiers:
                device = self._identifier_index.get(identifier)
                if device is not None:
                    return device
            return None

        def async_entries_for_config_entry(self, entry_id: str) -> tuple[Any, ...]:
            return tuple(
                device
                for device in self._devices.values()
                if entry_id in device.config_entries
            )

        def async_get(self, device_id: str) -> Any | None:
            return self._devices.get(device_id)

        def async_update_device(self, device_id: str, **changes: Any) -> None:
            self.calls.append((device_id, dict(changes)))
            if "remove_config_subentry_id" in changes:
                raise TypeError(
                    "unexpected keyword argument 'remove_config_subentry_id'"
                )

            device = self._devices[device_id]
            if "remove_config_entry_id" in changes:
                entry_to_remove = changes["remove_config_entry_id"]
                mapping = getattr(device, "config_entries_subentries", None)
                removed_entry = False
                if isinstance(mapping, dict):
                    subset = mapping.get(entry_to_remove)
                    if subset is not None:
                        if changes.get("remove_config_subentry_id") is None:
                            if None in subset:
                                subset.discard(None)
                            if not subset:
                                mapping.pop(entry_to_remove, None)
                                removed_entry = True
                        else:
                            subset.discard(changes["remove_config_subentry_id"])
                            if not subset:
                                mapping.pop(entry_to_remove, None)
                                removed_entry = True
                    else:
                        removed_entry = True
                else:
                    removed_entry = True

                if removed_entry:
                    device.config_entries.discard(entry_to_remove)
                elif isinstance(mapping, dict):
                    subset = mapping.get(entry_to_remove)
                    if subset:
                        non_null = [item for item in subset if item is not None]
                        if non_null and len(subset - {None}) == 1:
                            device.config_subentry_id = non_null[0]
                        elif len(subset) == 1 and None in subset:
                            device.config_subentry_id = None
                if removed_entry and not getattr(device, "config_entries_subentries", {}):
                    device.config_subentry_id = None

            self.updated.append((device_id, dict(changes)))

    registry = RaisingDeviceRegistry([service_device, redundant_tracker])

    manager = FakeConfigEntriesManager([entry])
    hass = FakeHass(manager)

    tracker_metadata = SimpleNamespace(
        config_subentry_id=tracker_subentry_id,
        entry_id=tracker_entry_id,
    )
    service_metadata = SimpleNamespace(
        config_subentry_id=service_subentry_id,
        entry_id="service-config-entry",
    )

    def _metadata(*, key: str) -> SimpleNamespace | None:
        if key == TRACKER_SUBENTRY_KEY:
            return tracker_metadata
        if key == SERVICE_SUBENTRY_KEY:
            return service_metadata
        return None

    coordinator = SimpleNamespace(
        config_entry=entry,
        data=[],
        name="Coordinator",
        _ensure_registry_for_devices=lambda devices, ignored: 0,
        _get_ignored_set=lambda: set(),
        _ensure_service_device_exists=lambda: None,
        get_subentry_metadata=_metadata,
    )

    runtime = SimpleNamespace(coordinator=coordinator)
    entry.runtime_data = runtime
    hass.data.setdefault(DOMAIN, {}).setdefault("entries", {})[entry.entry_id] = runtime

    monkeypatch.setattr(
        "custom_components.googlefindmy.services.dr.async_get",
        lambda hass: registry,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.services.dr.async_entries_for_config_entry",
        device_registry_async_entries_for_config_entry,
        raising=False,
    )

    entity_registry = FakeEntityRegistry()
    monkeypatch.setattr(
        "custom_components.googlefindmy.services.er.async_get",
        lambda hass: entity_registry,
    )

    await services.async_rebuild_device_registry(hass, ServiceCall({}))

    assert registry.calls == [
        (
            "service-device",
            {
                "remove_config_entry_id": entry.entry_id,
                "remove_config_subentry_id": None,
            },
        ),
        ("service-device", {"remove_config_entry_id": entry.entry_id}),
        (
            "tracker-device",
            {
                "remove_config_entry_id": entry.entry_id,
                "remove_config_subentry_id": None,
            },
        ),
        ("tracker-device", {"remove_config_entry_id": entry.entry_id}),
    ]
    assert registry.updated == [
        ("service-device", {"remove_config_entry_id": entry.entry_id}),
        ("tracker-device", {"remove_config_entry_id": entry.entry_id}),
    ]
    assert entry.entry_id not in redundant_tracker.config_entries
    assert redundant_tracker.config_entries_subentries[entry.entry_id] == set()
    assert service_device.config_entries_subentries[entry.entry_id] == {
        service_subentry_id
    }
    assert service_device.config_subentry_id == service_subentry_id
