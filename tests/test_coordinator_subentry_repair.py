# tests/test_coordinator_subentry_repair.py
"""Regression tests ensuring core subentries are repaired at runtime."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.const import (
    DOMAIN,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr


def _build_subentry(
    entry_id: str,
    *,
    key: str,
    features: tuple[str, ...],
    subentry_type: str,
    title: str,
) -> ConfigSubentry:
    """Return a ConfigSubentry populated with the provided metadata."""

    payload = {"group_key": key, "features": list(features)}
    return ConfigSubentry(
        data=MappingProxyType(payload),
        subentry_type=subentry_type,
        title=title,
        unique_id=f"{entry_id}-{key}",
        subentry_id=f"{entry_id}-{key}-subentry",
    )


class _ManagerStub:
    """Capture repair calls and update the entry's subentry mapping."""

    def __init__(self, entry: Any) -> None:
        self.entry = entry
        self.calls: list[list[tuple[str, tuple[str, ...]]]] = []

    async def async_sync(self, definitions: list[Any]) -> None:
        recorded: list[tuple[str, tuple[str, ...]]] = []
        rebuilt: dict[str, ConfigSubentry] = {}
        for definition in definitions:
            features = definition.data.get("features", ())
            recorded.append(
                (
                    definition.key,
                    tuple(sorted(str(item) for item in features)),
                )
            )
            payload = dict(definition.data)
            payload["group_key"] = definition.key
            subentry = ConfigSubentry(
                data=MappingProxyType(payload),
                subentry_type=definition.subentry_type,
                title=definition.title,
                unique_id=definition.unique_id
                or f"{self.entry.entry_id}-{definition.key}",
                subentry_id=f"{self.entry.entry_id}-{definition.key}-subentry",
            )
            rebuilt[subentry.subentry_id] = subentry
        self.calls.append(recorded)
        self.entry.subentries = rebuilt

    def update_visible_device_ids(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: D401 - interface shim
        """Ignore visibility updates for the repair stub."""
        return None


def _initialize_repair_coordinator(
    loop: asyncio.AbstractEventLoop,
) -> tuple[
    HomeAssistant,
    Any,
    GoogleFindMyCoordinator,
    _ManagerStub,
    list[asyncio.Task[Any]],
    ConfigSubentry,
    ConfigSubentry,
]:
    """Prepare a coordinator instance and capture repair scheduling tasks."""

    hass = HomeAssistant()
    hass.loop = loop
    hass.bus = SimpleNamespace(async_listen=lambda *_args, **_kwargs: (lambda: None))
    hass.data = {DOMAIN: {}}

    created_tasks: list[asyncio.Task[Any]] = []

    def _track_task(coro: Any, *, name: str | None = None) -> asyncio.Task[Any]:
        task = loop.create_task(coro, name=name)
        created_tasks.append(task)
        return task

    hass.async_create_task = _track_task  # type: ignore[assignment]

    entry = SimpleNamespace(
        entry_id="entry-repair",
        title="Repair Coverage",
        data={},
        options={},
        subentries={},
        runtime_data=None,
        async_on_unload=lambda _cb: None,
    )

    tracker_subentry = _build_subentry(
        entry.entry_id,
        key=TRACKER_SUBENTRY_KEY,
        features=TRACKER_FEATURE_PLATFORMS,
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Devices",
    )
    service_subentry = _build_subentry(
        entry.entry_id,
        key=SERVICE_SUBENTRY_KEY,
        features=SERVICE_FEATURE_PLATFORMS,
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title=entry.title,
    )
    entry.subentries = {
        tracker_subentry.subentry_id: tracker_subentry,
        service_subentry.subentry_id: service_subentry,
    }

    runtime_data = SimpleNamespace(
        coordinator=None,
        fcm_receiver=SimpleNamespace(),
        google_home_filter=None,
    )
    entry.runtime_data = runtime_data

    manager = _ManagerStub(entry)

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    coordinator.data = []
    coordinator._enabled_poll_device_ids = set()
    coordinator.allow_history_fallback = False
    coordinator._min_accuracy_threshold = 50
    coordinator._movement_threshold = 10
    coordinator.device_poll_delay = 30
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = TRACKER_SUBENTRY_KEY
    coordinator._warned_bad_identifier_devices = set()
    coordinator._pending_subentry_repair = None
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **_kwargs: None,
        remove_warning=lambda **_kwargs: None,
    )
    coordinator._service_device_ready = False
    coordinator._service_device_id = None
    runtime_data.coordinator = coordinator

    return (
        hass,
        entry,
        coordinator,
        manager,
        created_tasks,
        tracker_subentry,
        service_subentry,
    )


@pytest.mark.asyncio
async def test_coordinator_repairs_missing_core_subentries_on_cold_start() -> None:
    """Cold-start refresh should schedule and execute core subentry repairs."""

    loop = asyncio.get_running_loop()
    (
        hass,
        entry,
        coordinator,
        manager,
        created_tasks,
        tracker_subentry,
        service_subentry,
    ) = _initialize_repair_coordinator(loop)

    coordinator.attach_subentry_manager(manager, is_reload=False)

    coordinator._refresh_subentry_index()

    registry = dr.async_get(hass)
    baseline_created = list(getattr(registry, "created", []))

    entry.subentries.pop(service_subentry.subentry_id)

    before_tasks = len(created_tasks)
    coordinator._refresh_subentry_index()
    assert (
        len(created_tasks) == before_tasks + 1
    ), "missing core subentries should trigger a repair task"

    new_tasks = created_tasks[before_tasks:]
    if new_tasks:
        await asyncio.gather(*new_tasks)

    assert manager.calls, "core subentries should be rebuilt during repair"
    assert coordinator._pending_subentry_repair is None
    assert service_subentry.subentry_id in entry.subentries
    assert tracker_subentry.subentry_id in entry.subentries

    expected_service_id = f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}-subentry"
    expected_tracker_id = f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}-subentry"
    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert service_meta is not None
    assert tracker_meta is not None
    assert service_meta.config_subentry_id == expected_service_id
    assert tracker_meta.config_subentry_id == expected_tracker_id

    registry = dr.async_get(hass)
    current_created = getattr(registry, "created", [])
    assert list(current_created) == baseline_created


@pytest.mark.asyncio
async def test_coordinator_skips_repair_during_reload_refresh() -> None:
    """Reload-driven refresh should skip scheduling core subentry repairs."""

    loop = asyncio.get_running_loop()
    (
        hass,
        entry,
        coordinator,
        manager,
        created_tasks,
        _tracker_subentry,
        service_subentry,
    ) = _initialize_repair_coordinator(loop)

    coordinator.attach_subentry_manager(manager, is_reload=True)

    coordinator._refresh_subentry_index()

    registry = dr.async_get(hass)
    baseline_created = list(getattr(registry, "created", []))

    entry.subentries.pop(service_subentry.subentry_id)

    before_tasks = len(created_tasks)
    coordinator._refresh_subentry_index()
    assert (
        len(created_tasks) == before_tasks
    ), "reload refresh should not schedule core subentry repairs"

    assert not manager.calls, "repair should be skipped during reload refresh"
    assert coordinator._pending_subentry_repair is None
    assert service_subentry.subentry_id not in entry.subentries

    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert service_meta is not None
    assert tracker_meta is not None
    assert service_meta.visible_device_ids == ()

    registry = dr.async_get(hass)
    current_created = getattr(registry, "created", [])
    assert list(current_created) == baseline_created
