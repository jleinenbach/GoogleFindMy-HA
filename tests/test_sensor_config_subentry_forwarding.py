"""Sensor setup should propagate forwarded config_subentry_id fallbacks."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import sensor
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    OPT_ENABLE_STATS_ENTITIES,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)


class _ConfigEntryStub:
    """Lightweight config entry shim exposing runtime_data + unload tracking."""

    def __init__(self, coordinator: Any) -> None:
        self.entry_id = "entry-sensor"
        self.data: dict[str, Any] = {
            CONF_GOOGLE_EMAIL: "user@example.com",
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            OPT_ENABLE_STATS_ENTITIES: True,
        }
        self.options: dict[str, Any] = {OPT_ENABLE_STATS_ENTITIES: True}
        self.runtime_data = SimpleNamespace(coordinator=coordinator)
        self._unsub: list[Callable[[], None]] = []

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self._unsub.append(callback)


@pytest.mark.asyncio
async def test_sensor_setup_propagates_service_config_subentry_id(
    stub_coordinator_factory: Callable[..., type[Any]]
) -> None:
    """Service-only setup should reuse the forwarded identifier when metadata is empty."""

    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: None))
    coordinator_cls = stub_coordinator_factory(
        data=[{"id": "tracker-1", "name": "Tracker One"}],
        stats={"background_updates": 1},
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id="entry-sensor"))
    entry = _ConfigEntryStub(coordinator)
    coordinator.config_entry = entry

    captured: list[str | None] = []

    def _capture_entities(
        entities: list[Any],
        update_before_add: bool = False,
        *,
        config_subentry_id: str | None = None,
    ) -> None:
        del entities, update_before_add
        captured.append(config_subentry_id)

    await sensor.async_setup_entry(
        hass,
        entry,
        _capture_entities,
        config_subentry_id=SERVICE_SUBENTRY_KEY,
    )

    assert captured == [SERVICE_SUBENTRY_KEY]


@pytest.mark.asyncio
async def test_sensor_setup_propagates_tracker_config_subentry_id(
    stub_coordinator_factory: Callable[..., type[Any]]
) -> None:
    """Tracker-scoped setup should fall back to the forwarded identifier."""

    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: None))
    coordinator_cls = stub_coordinator_factory(
        data=[{"id": "tracker-2", "name": "Tracker Two"}],
        stats={"background_updates": 2},
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id="entry-sensor"))
    entry = _ConfigEntryStub(coordinator)
    coordinator.config_entry = entry

    captured: list[str | None] = []

    def _capture_entities(
        entities: list[Any],
        update_before_add: bool = False,
        *,
        config_subentry_id: str | None = None,
    ) -> None:
        del entities, update_before_add
        captured.append(config_subentry_id)

    await sensor.async_setup_entry(
        hass,
        entry,
        _capture_entities,
        config_subentry_id=TRACKER_SUBENTRY_KEY,
    )

    assert captured == [TRACKER_SUBENTRY_KEY]
