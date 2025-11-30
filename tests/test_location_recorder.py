# tests/test_location_recorder.py
"""Tests for the recorder-backed location helper."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.googlefindmy.location_recorder import LocationRecorder


def test_get_location_history_prefers_last_seen() -> None:
    """Recorder history should favor the stored last_seen timestamp."""

    hass = SimpleNamespace()
    recorder = LocationRecorder(hass)  # type: ignore[arg-type]
    entity_id = "device_tracker.pixel"

    last_changed = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    last_seen_iso = "2024-01-01T00:05:00Z"
    state = FakeState(
        "home",
        {
            "latitude": 52.0,
            "longitude": 13.0,
            "gps_accuracy": 15,
            "last_seen": last_seen_iso,
            "last_seen_utc": last_seen_iso,
        },
        last_changed,
    )

    mock_instance = AsyncMock()
    mock_instance.async_add_executor_job.return_value = {entity_id: [state]}

    with patch(
        "custom_components.googlefindmy.location_recorder.get_instance",
        return_value=mock_instance,
    ):
        results = asyncio.run(recorder.get_location_history(entity_id, hours=1))

    call_args = mock_instance.async_add_executor_job.call_args
    assert call_args is not None
    _, _, start_time, end_time, *_ = call_args.args
    assert start_time.tzinfo is not None
    assert end_time.tzinfo is not None
    assert start_time.utcoffset() == timedelta(0)
    assert end_time.utcoffset() == timedelta(0)

    assert len(results) == 1
    entry = results[0]
    expected_ts = datetime(2024, 1, 1, 0, 5, tzinfo=UTC).timestamp()

    assert entry["timestamp"] == pytest.approx(expected_ts)
    assert entry["last_seen"] == pytest.approx(expected_ts)
    assert entry["last_seen_utc"] == last_seen_iso
    assert entry["timestamp"] > last_changed.timestamp()


def test_get_location_history_fallbacks_to_last_changed() -> None:
    """If last_seen is missing, fall back to last_changed timestamps."""

    hass = SimpleNamespace()
    recorder = LocationRecorder(hass)  # type: ignore[arg-type]
    entity_id = "device_tracker.tablet"

    last_changed = datetime.now(tz=UTC) - timedelta(minutes=10)
    state = FakeState(
        "not_home",
        {
            "latitude": 40.0,
            "longitude": -70.0,
        },
        last_changed,
    )

    mock_instance = AsyncMock()
    mock_instance.async_add_executor_job.return_value = {entity_id: [state]}

    with patch(
        "custom_components.googlefindmy.location_recorder.get_instance",
        return_value=mock_instance,
    ):
        results = asyncio.run(recorder.get_location_history(entity_id, hours=1))

    assert len(results) == 1
    entry = results[0]
    assert entry["last_seen"] is None
    assert entry["last_seen_utc"] is None
    assert entry["timestamp"] == pytest.approx(last_changed.timestamp())


def test_get_best_location_prefers_newer_last_seen() -> None:
    """Scoring should prefer records with newer last_seen timestamps."""

    hass = SimpleNamespace()
    recorder = LocationRecorder(hass)  # type: ignore[arg-type]
    now = datetime(2024, 1, 1, 2, 0, tzinfo=UTC).timestamp()

    older = {
        "timestamp": now - 3600,
        "last_seen": now - 3600,
        "accuracy": 5,
        "semantic_name": None,
    }
    newer = {
        # timestamp appears older but last_seen indicates fresher data
        "timestamp": now - 4000,
        "last_seen": now - 1200,
        "accuracy": 50,
        "semantic_name": None,
    }

    with patch(
        "custom_components.googlefindmy.location_recorder.time.time", return_value=now
    ):
        best = recorder.get_best_location([older, newer])

    assert best is newer


def test_get_best_location_handles_equal_rankings() -> None:
    """Duplicate rankings should not trigger tuple comparison errors."""

    hass = SimpleNamespace()
    recorder = LocationRecorder(hass)  # type: ignore[arg-type]
    now = 1_700_000_000.0

    first = {
        "timestamp": now - 60,
        "accuracy": 25,
        "semantic_name": None,
    }
    second = {
        "timestamp": now - 60,
        "accuracy": 25,
        "semantic_name": None,
    }

    with patch(
        "custom_components.googlefindmy.location_recorder.time.time", return_value=now
    ):
        best = recorder.get_best_location([first, second])

    assert best is first


class FakeState:
    """Minimal stand-in for Home Assistant State objects used by recorder."""

    def __init__(
        self, state: str, attributes: dict[str, object], last_changed: datetime
    ) -> None:
        self.state = state
        self.attributes = attributes
        self.last_changed = last_changed
