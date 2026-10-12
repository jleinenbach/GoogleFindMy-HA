# tests/test_poll_startup.py
"""Location polling must not delay HA startup, even while a request is pending."""

from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_coordinator_predictive_polling import coordinator as coordinator_fixture

coordinator = coordinator_fixture


@pytest.mark.parametrize("entry_bound", [True, False])
@pytest.mark.asyncio
async def test_pending_poll_does_not_block_startup_and_is_cancelled(
    hass: HomeAssistant,
    coordinator: GoogleFindMyCoordinator,
    monkeypatch: pytest.MonkeyPatch,
    entry_bound: bool,
) -> None:
    """Exercise real HA task tracking and startup with a held location request."""
    coord: Any = coordinator
    # The repository replaces registry modules with stubs. A separate real HA
    # instance exercises its startup barrier without the fixture's registry
    # cleanup listeners calling those unrelated stubs on the started event.
    startup_hass = type(hass)(hass.config.config_dir)
    coord.hass = startup_hass
    entry = None
    if entry_bound:
        # Use the real core method on our canonical entry double, including the
        # entry-owned task set it uses to cancel background work on unload.
        entry = make_config_entry(entry_id="startup-test", _background_tasks=set())
        entry.async_create_background_task = partial(
            MockConfigEntry.async_create_background_task, entry
        )
    coord.config_entry = entry
    coord._last_device_list = [{"id": "dev-id"}]
    coord._last_list_poll_mono = time.monotonic()
    coord._last_poll_mono = 0.0
    coord._device_location_data["dev-id"] = {"last_seen": time.time()}

    monkeypatch.setattr(coord, "_ensure_service_device_exists", lambda: None)
    monkeypatch.setattr(coord, "_ensure_registry_for_devices", lambda *_a, **_kw: 0)
    monkeypatch.setattr(coord, "_refresh_subentry_index", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        coord, "_async_build_device_snapshot_with_fallbacks", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(coord, "_store_subentry_snapshots", lambda _snap: None)

    poll_started = asyncio.Event()
    request_pending = asyncio.Event()
    poll_cancelled = asyncio.Event()

    async def held_poll(_devices: object, *, force: bool = False) -> None:
        poll_started.set()
        try:
            await request_pending.wait()
        finally:
            poll_cancelled.set()

    monkeypatch.setattr(coord, "_async_start_poll_cycle", held_poll)
    try:
        await coord._async_update_data()
        await asyncio.wait_for(poll_started.wait(), timeout=1)
        poll_task = coord._poll_cycle_task
        assert poll_task is not None
        assert not poll_task.done()
        # This distinguishes true HA background work from a fire-and-forget
        # ordinary task, which is still part of the startup barrier.
        assert poll_task in startup_hass._background_tasks
        assert poll_task not in startup_hass._tasks
        if entry is not None:
            assert poll_task in entry._background_tasks

        await asyncio.wait_for(startup_hass.async_start(), timeout=1)
        assert startup_hass.state is type(startup_hass.state).running
        assert not poll_task.done()

        await coord._async_cancel_poll_cycle()
        assert poll_task.cancelled()
        assert poll_cancelled.is_set()
        assert coord._poll_cycle_task is None
        assert coord._poll_cycle_teardown
    finally:
        await coord._async_cancel_poll_cycle()
        await startup_hass.async_stop()
