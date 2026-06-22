# tests/test_coordinator_snapshot.py
"""Regression tests for coordinator snapshot rehydration fallbacks."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.coordinator import (
    GoogleFindMyCoordinator,
    _as_ha_attributes,
    _sync_get_last_gps_from_history,
)
from custom_components.googlefindmy.coordinator import registry as coordinator_registry
from tests.helpers.config_entries_stub import make_config_entry


class _DummyState:
    """Minimal Home Assistant state stub with GPS attributes."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        accuracy: float | None,
        extra_attrs: dict[str, object] | None = None,
    ) -> None:
        self.attributes = {
            "latitude": latitude,
            "longitude": longitude,
            "gps_accuracy": accuracy,
        }
        if extra_attrs:
            self.attributes.update(extra_attrs)
        self.last_updated = datetime(2024, 1, 1, tzinfo=UTC)


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


async def test_snapshot_uses_entry_scoped_unique_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Coordinator should rehydrate from HA state via entry-scoped tracker unique_id."""

    entity_registry = _DummyEntityRegistry()
    entity_registry.add(
        "device_tracker",
        DOMAIN,
        "entry-1:device-42",
        "device_tracker.googlefindmy_device_42",
    )
    # Use object-based patching (er is imported in registry.py)
    monkeypatch.setattr(
        coordinator_registry.er, "async_get", lambda hass: entity_registry
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
    coordinator.config_entry = make_config_entry(entry_id="entry-1")

    result = await coordinator._async_build_device_snapshot_with_fallbacks(
        devices=[{"id": "device-42", "name": "Pixel 8"}]
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["latitude"] == pytest.approx(37.4219999)
    assert entry["longitude"] == pytest.approx(-122.0840575)
    assert entry["accuracy"] == pytest.approx(5.0)
    assert entry["status"] == "Using current state"
    assert entry["last_seen"] == pytest.approx(
        int(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    )


async def test_snapshot_preserves_recorded_last_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted last_seen attribute should win over last_updated."""

    entity_registry = _DummyEntityRegistry()
    entity_registry.add(
        "device_tracker",
        DOMAIN,
        "entry-1:device-42",
        "device_tracker.googlefindmy_device_42",
    )
    # Use object-based patching (er is imported in registry.py)
    monkeypatch.setattr(
        coordinator_registry.er, "async_get", lambda hass: entity_registry
    )

    hass = SimpleNamespace(states=_DummyStates())
    iso_seen = "2024-02-03T12:34:56Z"
    hass.states.set(
        "device_tracker.googlefindmy_device_42",
        _DummyState(
            latitude=37.4219999,
            longitude=-122.0840575,
            accuracy=5.0,
            extra_attrs={"last_seen": iso_seen},
        ),
    )

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass
    coordinator.allow_history_fallback = False
    coordinator._device_location_data = {}
    coordinator.location_poll_interval = 30
    coordinator.config_entry = make_config_entry(entry_id="entry-1")

    result = await coordinator._async_build_device_snapshot_with_fallbacks(
        devices=[{"id": "device-42", "name": "Pixel 8"}]
    )

    entry = result[0]
    expected_ts = datetime.fromisoformat(iso_seen.replace("Z", "+00:00")).timestamp()
    assert entry["last_seen"] == pytest.approx(expected_ts)
    assert entry["last_seen_utc"] == iso_seen


async def test_snapshot_logs_formats_when_entity_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Log should clarify which unique_id formats were considered when none match."""

    entity_registry = _DummyEntityRegistry()
    # Use object-based patching (er is imported in registry.py)
    monkeypatch.setattr(
        coordinator_registry.er, "async_get", lambda hass: entity_registry
    )

    hass = SimpleNamespace(states=_DummyStates())

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass
    coordinator.allow_history_fallback = False
    coordinator._device_location_data = {}
    coordinator.location_poll_interval = 30
    coordinator.config_entry = make_config_entry(entry_id="entry-1")

    caplog.set_level("DEBUG")

    result = await coordinator._async_build_device_snapshot_with_fallbacks(
        devices=[{"id": "device-99", "name": "Tablet"}]
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


def test_history_helper_preserves_last_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    """Recorder fallback should forward stored last_seen timestamps."""

    iso_seen = "2024-02-05T08:00:00Z"
    history_state = SimpleNamespace(
        attributes={
            "latitude": 48.137154,
            "longitude": 11.576124,
            "gps_accuracy": 25.0,
            "last_seen": iso_seen,
        },
        last_updated=datetime(2024, 2, 6, tzinfo=UTC),
    )

    def _fake_get_last_state_changes(hass, limit, entity_ids):
        return {entity_ids[0]: [history_state]}

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.main.recorder_history.get_last_state_changes",
        _fake_get_last_state_changes,
        raising=False,
    )

    hass = SimpleNamespace()
    result = _sync_get_last_gps_from_history(
        hass, "device_tracker.googlefindmy_device_42"
    )

    assert result is not None
    expected_ts = datetime.fromisoformat(iso_seen.replace("Z", "+00:00")).timestamp()
    assert result["last_seen"] == pytest.approx(expected_ts)
    assert result["status"] == "Using historical data"


def test_history_helper_carries_accuracy_estimated_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconstructed history rows must carry the producer accuracy_estimated flag.

    The recorded state of a 200m fallback fix keeps accuracy_m=200 and
    accuracy_estimated=True. Rebuilding the row from gps_accuracy alone dropped
    the flag, so the rebuilt 200m value re-entered the cache looking like a real
    measurement (PR #1124 defect class). The reader must take the value from
    accuracy_m and forward the flag.
    """

    history_state = SimpleNamespace(
        attributes={
            "latitude": 48.137154,
            "longitude": 11.576124,
            # Stable producer pair; gps_accuracy intentionally absent.
            "accuracy_m": 200.0,
            "accuracy_estimated": True,
            "last_seen": "2024-02-05T08:00:00Z",
        },
        last_updated=datetime(2024, 2, 6, tzinfo=UTC),
    )

    def _fake_get_last_state_changes(hass, limit, entity_ids):
        return {entity_ids[0]: [history_state]}

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.main.recorder_history.get_last_state_changes",
        _fake_get_last_state_changes,
        raising=False,
    )

    hass = SimpleNamespace()
    result = _sync_get_last_gps_from_history(
        hass, "device_tracker.googlefindmy_device_42"
    )

    assert result is not None
    assert result["accuracy"] == pytest.approx(200.0)
    assert result["accuracy_estimated"] is True


async def test_snapshot_current_state_carries_accuracy_estimated_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live-state snapshot path must carry the producer accuracy_estimated flag.

    Sibling of the history path: reusing a published 200m fallback state to build
    a snapshot entry must read accuracy from accuracy_m and forward
    accuracy_estimated, so the cache does not reclassify the fallback as real.
    """

    entity_registry = _DummyEntityRegistry()
    entity_registry.add(
        "device_tracker",
        DOMAIN,
        "entry-1:device-42",
        "device_tracker.googlefindmy_device_42",
    )
    monkeypatch.setattr(
        coordinator_registry.er, "async_get", lambda hass: entity_registry
    )

    hass = SimpleNamespace(states=_DummyStates())
    hass.states.set(
        "device_tracker.googlefindmy_device_42",
        _DummyState(
            latitude=37.4219999,
            longitude=-122.0840575,
            accuracy=None,
            extra_attrs={"accuracy_m": 200.0, "accuracy_estimated": True},
        ),
    )

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass
    coordinator.allow_history_fallback = False
    coordinator._device_location_data = {}
    coordinator.location_poll_interval = 30
    coordinator.config_entry = make_config_entry(entry_id="entry-1")

    result = await coordinator._async_build_device_snapshot_with_fallbacks(
        devices=[{"id": "device-42", "name": "Pixel 8"}]
    )

    assert len(result) == 1
    entry = result[0]
    assert entry["status"] == "Using current state"
    assert entry["accuracy"] == pytest.approx(200.0)
    assert entry["accuracy_estimated"] is True
