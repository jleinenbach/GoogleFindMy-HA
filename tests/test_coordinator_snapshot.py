# tests/test_coordinator_snapshot.py
"""Regression tests for coordinator snapshot rehydration fallbacks."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import asyncio

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import (
    GoogleFindMyCoordinator,
    _as_ha_attributes,
)


class _DummyState:
    """Minimal Home Assistant state stub with GPS attributes."""

    def __init__(
        self, latitude: float, longitude: float, accuracy: float | None
    ) -> None:
        self.attributes = {
            "latitude": latitude,
            "longitude": longitude,
            "gps_accuracy": accuracy,
        }
        self.last_updated = datetime(2024, 1, 1, tzinfo=timezone.utc)


class _DummyStates:
    """Provide a mapping-like interface for hass.states."""

    def __init__(self) -> None:
        self._data: dict[str, _DummyState] = {}

    def get(self, entity_id: str) -> _DummyState | None:
        return self._data.get(entity_id)

    def set(self, entity_id: str, state: _DummyState) -> None:
        self._data[entity_id] = state


class _DummyEntityRegistry:
    """Minimal entity registry stub supporting unique_id lookups."""

    def __init__(self) -> None:
        self._entity_ids: dict[tuple[str, str, str], str] = {}

    def add(self, platform: str, domain: str, unique_id: str, entity_id: str) -> None:
        self._entity_ids[(platform, domain, unique_id)] = entity_id

    def async_get_entity_id(
        self, platform: str, domain: str, unique_id: str
    ) -> str | None:
        return self._entity_ids.get((platform, domain, unique_id))


def test_snapshot_uses_entry_scoped_unique_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Coordinator should rehydrate from HA state via entry-scoped tracker unique_id."""

    entity_registry = _DummyEntityRegistry()
    entity_registry.add(
        "device_tracker",
        DOMAIN,
        "entry-1:device-42",
        "device_tracker.googlefindmy_device_42",
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.er.async_get",
        lambda hass: entity_registry,
    )

    hass = SimpleNamespace(states=_DummyStates())
    hass.states.set(
        "device_tracker.googlefindmy_device_42",
        _DummyState(latitude=37.4219999, longitude=-122.0840575, accuracy=5.0),
    )

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass
    coordinator.allow_history_fallback = False
    coordinator._device_location_data = {}
    coordinator.location_poll_interval = 30
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1")

    result = asyncio.run(
        coordinator._async_build_device_snapshot_with_fallbacks(
            devices=[{"id": "device-42", "name": "Pixel 8"}]
        )
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["latitude"] == pytest.approx(37.4219999)
    assert entry["longitude"] == pytest.approx(-122.0840575)
    assert entry["accuracy"] == pytest.approx(5.0)
    assert entry["status"] == "Using current state"
    assert entry["last_seen"] == pytest.approx(
        int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp())
    )


def test_snapshot_logs_formats_when_entity_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Log should clarify which unique_id formats were considered when none match."""

    entity_registry = _DummyEntityRegistry()
    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.er.async_get",
        lambda hass: entity_registry,
    )

    hass = SimpleNamespace(states=_DummyStates())

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass
    coordinator.allow_history_fallback = False
    coordinator._device_location_data = {}
    coordinator.location_poll_interval = 30
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1")

    caplog.set_level("DEBUG")

    result = asyncio.run(
        coordinator._async_build_device_snapshot_with_fallbacks(
            devices=[{"id": "device-99", "name": "Tablet"}]
        )
    )

    assert result[0]["status"] == "Waiting for location poll"
    assert any(
        "checked unique_id formats" in record.message
        and "entry-1:device-99" in record.message
        for record in caplog.records
    )


def test_as_ha_attributes_emits_iso_timestamps() -> None:
    """Coordinator attributes should not expose raw epoch floats."""

    attrs = _as_ha_attributes(
        {
            "id": "device-1",
            "name": "Pixel",
            "status": "online",
            "last_seen": 1_700_000_000,
        }
    )

    assert attrs is not None
    assert attrs["last_seen"] == "2023-11-14T22:13:20Z"
    assert attrs["last_seen_utc"] == "2023-11-14T22:13:20Z"
    assert "last_seen" in attrs and isinstance(attrs["last_seen"], str)
