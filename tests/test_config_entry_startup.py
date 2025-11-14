from __future__ import annotations

import asyncio
import functools
import importlib
from typing import Any

import pytest

from custom_components.googlefindmy.const import TRACKER_SUBENTRY_KEY

from tests.test_hass_data_layout import _prepare_async_setup_entry_harness


@pytest.mark.asyncio
async def test_device_trackers_populate_after_initial_refresh(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory,
) -> None:
    """Initial setup should create tracker entities once the first refresh succeeds."""

    loop = asyncio.get_running_loop()

    snapshot = [{"id": "tracker-1", "name": "Backpack"}]

    async def _first_refresh(self) -> None:  # type: ignore[no-untyped-def]
        self.first_refresh_calls += 1  # type: ignore[attr-defined]
        self.data = list(snapshot)

    def _find_tracker(self, device_id: str):  # type: ignore[no-untyped-def]
        del device_id
        return None

    factory = functools.partial(
        stub_coordinator_factory,
        data=[],
        methods={
            "async_config_entry_first_refresh": _first_refresh,
            "find_tracker_entity_entry": _find_tracker,
        },
    )

    harness = _prepare_async_setup_entry_harness(monkeypatch, factory, loop)
    integration = harness.integration
    entry = harness.entry
    hass = harness.hass
    coordinator_cls = harness.coordinator_cls

    device_tracker = importlib.import_module("custom_components.googlefindmy.device_tracker")
    monkeypatch.setattr(device_tracker, "GoogleFindMyCoordinator", coordinator_cls)

    added_entities: list[Any] = []

    def _async_add_entities(entities: list[Any], update_before_add: bool = False) -> None:
        added_entities.extend(entities)
        assert update_before_add is True

    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup_entry(hass, entry)

    if hass._tasks:
        await asyncio.gather(*hass._tasks)

    runtime_data = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime_data, "coordinator", None)
    assert coordinator is not None
    assert getattr(coordinator, "first_refresh_calls", 0) == 1
    assert coordinator.get_subentry_snapshot(TRACKER_SUBENTRY_KEY) == snapshot

    await device_tracker.async_setup_entry(hass, entry, _async_add_entities)

    assert added_entities, "Tracker entities should be created on startup"
    tracker = added_entities[0]
    assert getattr(tracker, "device_id", None) == "tracker-1"
    assert tracker.unique_id.endswith(":tracker-1")
    assert getattr(entry.runtime_data.coordinator, "first_refresh_calls", 0) == 1

    # Coordinator snapshot should reflect the first refresh payload for subsequent scans.
    follow_up = device_tracker.resolve_coordinator(entry).get_subentry_snapshot(
        TRACKER_SUBENTRY_KEY
    )
    assert follow_up == snapshot
