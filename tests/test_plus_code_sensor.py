# tests/test_plus_code_sensor.py
"""Behavior tests for ``GoogleFindMyPlusCodeSensor``.

Mirrors ``tests/test_last_seen_sensor_no_map_attributes.py``: a stub coordinator
subclasses the real coordinator, and ``sensor.async_setup_entry`` is driven with
a capture callback so the sensor is obtained through the genuine live setup path
(``_build_entities()`` @ sensor.py, alongside the LastSeen sensor).

Covers:
- the success case (a position row yields the correct Plus Code);
- the negative control (no/invalid row -> ``native_value`` is ``None``, no crash);
- the map-marker guard (no ``latitude``/``longitude`` state attributes).

Authored as coroutines and awaited directly (never ``asyncio.run()``): HA tests
run inside pytest-asyncio's managed loop (see ``tests/AGENTS.md`` "Async tests").
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import (
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_entries_stub import make_config_entry

# All tests in this module are async and run in pytest-asyncio's managed loop.
pytestmark = pytest.mark.asyncio

# A fresh timestamp so the position rows pass the sensor's staleness gate (it
# mirrors the tracker: a fix older than the configured threshold is withheld).
_FRESH_LAST_SEEN = time.time()

# A decoder row with a real, recent position. Its lat/lon drive the encoder.
_POSITION_ROW: dict[str, Any] = {
    "id": "dev-1",
    "device_id": "dev-1",
    "name": "Pixel",
    "latitude": 52.5200,
    "longitude": 13.4050,
    "accuracy": 12.0,
    "last_seen": _FRESH_LAST_SEEN,
    "source_label": "Owner",
    "source_rank": 1,
}

# encode(52.5200, 13.4050, 10) at the pinned upstream commit. 11 chars incl. "+".
_EXPECTED_CODE = "9F4MGCC4+22"

# A row that clears the accuracy/staleness gate but has no coordinates -> the
# sensor must yield None at the coordinate guard specifically.
_NO_COORDS_ROW: dict[str, Any] = {
    "id": "dev-1",
    "device_id": "dev-1",
    "name": "Pixel",
    "accuracy": 12.0,
    "last_seen": _FRESH_LAST_SEEN,
}


async def _build_plus_code_sensor(
    row: dict[str, Any] | None,
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> Any:
    """Construct a real ``GoogleFindMyPlusCodeSensor`` via the platform setup path.

    Mirrors the LastSeen test harness: a stub coordinator subclassing the real
    coordinator serves ``row`` from ``get_device_location_data_for_subentry`` so
    the value under test sees a controlled position.
    """

    del (
        deterministic_config_subentry_id
    )  # side effect: patches ensure_config_subentry_id

    device_tracker = importlib.import_module(
        "custom_components.googlefindmy.device_tracker"
    )
    sensor = importlib.import_module("custom_components.googlefindmy.sensor")

    class _StubCoordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self, devices: Iterable[dict[str, Any]]) -> None:
            self._snapshot = list(devices)
            self._listeners: list[Callable[[], None]] = []
            self.hass = SimpleNamespace()
            self.config_entry = make_config_entry(entry_id="entry-id")
            self.stats: dict[str, int] = {}
            self._device_names: dict[str, str] = {}
            self._device_location_data: dict[str, Any] = {}
            self._device_caps: dict[str, Any] = {}
            self._present_last_seen: dict[str, float] = {}
            self._row: dict[str, Any] | None = row

        def async_add_listener(
            self, listener: Callable[[], None]
        ) -> Callable[[], None]:
            self._listeners.append(listener)
            return lambda: None

        def stable_subentry_identifier(
            self, *, key: str | None = None, feature: str | None = None
        ) -> str:
            assert key is not None
            return f"{key}-identifier"

        def get_subentry_metadata(
            self, *, key: str | None = None, feature: str | None = None
        ) -> Any:
            if key is not None:
                resolved = key
            elif feature in {"button", "device_tracker", "sensor"}:
                resolved = TRACKER_SUBENTRY_KEY
            elif feature == "binary_sensor":
                resolved = SERVICE_SUBENTRY_KEY
            else:
                resolved = TRACKER_SUBENTRY_KEY
            return SimpleNamespace(key=resolved)

        def get_subentry_snapshot(
            self, key: str | None = None, *, feature: str | None = None
        ) -> list[dict[str, Any]]:
            return list(self._snapshot)

        def get_device_location_data_for_subentry(
            self, subentry_key: str, device_id: str
        ) -> dict[str, Any] | None:
            return self._row

    class _StubConfigEntry:
        def __init__(self, coordinator: _StubCoordinator) -> None:
            self.runtime_data = coordinator
            self.entry_id = "entry-id"
            self.data: dict[str, Any] = {}
            self.options: dict[str, Any] = {}
            self._unsub: list[Callable[[], None]] = []

        def async_on_unload(self, callback: Callable[[], None]) -> None:
            self._unsub.append(callback)

    coordinator = _StubCoordinator([{"id": "dev-1", "name": "Pixel"}])
    entry = _StubConfigEntry(coordinator)

    sensor_added: list[list[Any]] = []

    def _capture_sensor(entities, update_before_add: bool = False):
        sensor_added.append(list(entities))

    await sensor.async_setup_entry(SimpleNamespace(), entry, _capture_sensor)

    plus_code_entities = [
        entity
        for batch in sensor_added
        for entity in batch
        if isinstance(entity, sensor.GoogleFindMyPlusCodeSensor)
    ]
    assert len(plus_code_entities) == 1
    return plus_code_entities[0]


async def test_plus_code_sensor_encodes_position(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A position row yields the full 10-digit Plus Code as native_value."""
    entity = await _build_plus_code_sensor(
        _POSITION_ROW, deterministic_config_subentry_id
    )
    assert entity.native_value == _EXPECTED_CODE
    assert len(entity.native_value) == 11
    assert entity.native_value[8] == "+"


async def test_plus_code_sensor_has_no_map_coordinates(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """The text sensor must not advertise latitude/longitude (no third marker)."""
    entity = await _build_plus_code_sensor(
        _POSITION_ROW, deterministic_config_subentry_id
    )
    attrs = entity.extra_state_attributes or {}
    assert "latitude" not in attrs
    assert "longitude" not in attrs


async def test_plus_code_sensor_none_row_yields_none(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """No location row -> native_value None (no stale last value), no crash."""
    entity = await _build_plus_code_sensor(None, deterministic_config_subentry_id)
    assert entity.native_value is None


async def test_plus_code_sensor_missing_coords_yields_none(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A row without latitude/longitude -> native_value None, no crash."""
    entity = await _build_plus_code_sensor(
        _NO_COORDS_ROW, deterministic_config_subentry_id
    )
    assert entity.native_value is None


async def test_plus_code_sensor_boolean_coords_yield_none(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Boolean coordinates (bool is an int subclass) -> native_value None."""
    row = {**_POSITION_ROW, "latitude": True, "longitude": False}
    entity = await _build_plus_code_sensor(row, deterministic_config_subentry_id)
    assert entity.native_value is None


async def test_plus_code_sensor_non_finite_coords_yield_none(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """NaN/inf coordinates -> native_value None (no bogus code), no crash."""
    nan_row = {**_POSITION_ROW, "latitude": float("nan")}
    inf_row = {**_POSITION_ROW, "longitude": float("inf")}
    nan_entity = await _build_plus_code_sensor(
        nan_row, deterministic_config_subentry_id
    )
    inf_entity = await _build_plus_code_sensor(
        inf_row, deterministic_config_subentry_id
    )
    assert nan_entity.native_value is None
    assert inf_entity.native_value is None


async def test_plus_code_sensor_accuracy_less_row_yields_none(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """A fresh row with valid lat/lon but no accuracy -> native_value None.

    Display-row policy (Codex #202 / PR #1179): the tracker withholds an
    accuracy-less fix, so the Plus Code must not encode that coordinate either.
    Reverting the accuracy gate turns this test red (mutation check).
    """
    accuracy_less_row = {**_POSITION_ROW, "accuracy": None}
    entity = await _build_plus_code_sensor(
        accuracy_less_row, deterministic_config_subentry_id
    )
    assert entity.native_value is None


async def test_plus_code_sensor_stale_row_yields_none(
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """An accuracy-bearing but stale fix -> native_value None.

    The tracker blanks live coordinates once a fix is older than the configured
    stale threshold; the Plus Code applies the same gate. Reverting the
    staleness gate turns this test red (mutation check).
    """
    stale_row = {**_POSITION_ROW, "last_seen": _FRESH_LAST_SEEN - 1_000_000}
    entity = await _build_plus_code_sensor(stale_row, deterministic_config_subentry_id)
    assert entity.native_value is None
