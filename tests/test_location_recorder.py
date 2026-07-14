# tests/test_location_recorder.py
"""Tests for the recorder-backed location helper."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.googlefindmy.location_recorder import (
    LocationRecorder,
    _coerce_iso,
    _extract_last_seen,
    _normalize_epoch,
)

_MODULE = "custom_components.googlefindmy.location_recorder"


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


# ---------------------------------------------------------------------------
# get_location_history — defensive and filtering paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_location_history_returns_empty_on_recorder_error() -> None:
    """A recorder executor failure must degrade to an empty result, not raise."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    mock_instance = AsyncMock()
    mock_instance.async_add_executor_job.side_effect = RuntimeError("recorder down")

    with patch(f"{_MODULE}.get_instance", return_value=mock_instance):
        results = await recorder.get_location_history("device_tracker.x", hours=1)

    assert results == []


@pytest.mark.asyncio
async def test_get_location_history_entity_absent_returns_empty() -> None:
    """When the entity is missing from the history map, return no locations."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    mock_instance = AsyncMock()
    mock_instance.async_add_executor_job.return_value = {}  # entity_id absent

    with patch(f"{_MODULE}.get_instance", return_value=mock_instance):
        results = await recorder.get_location_history("device_tracker.absent", hours=1)

    assert results == []


@pytest.mark.asyncio
async def test_get_location_history_skips_unusable_states() -> None:
    """Unknown/unavailable states and states lacking coordinates are skipped."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    entity_id = "device_tracker.multi"
    last_changed = datetime(2024, 1, 1, tzinfo=UTC)

    unknown_state = FakeState(
        "unknown", {"latitude": 1.0, "longitude": 2.0}, last_changed
    )
    without_coords = FakeState("home", {"battery": 90}, last_changed)
    valid = FakeState("home", {"latitude": 52.0, "longitude": 13.0}, last_changed)

    mock_instance = AsyncMock()
    mock_instance.async_add_executor_job.return_value = {
        entity_id: [unknown_state, without_coords, valid]
    }

    with patch(f"{_MODULE}.get_instance", return_value=mock_instance):
        results = await recorder.get_location_history(entity_id, hours=1)

    assert len(results) == 1
    assert results[0]["latitude"] == 52.0
    assert results[0]["longitude"] == 13.0


# ---------------------------------------------------------------------------
# get_best_location — scoring edge cases
# ---------------------------------------------------------------------------


def test_get_best_location_empty_returns_empty() -> None:
    """An empty candidate list yields an empty dict."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    assert recorder.get_best_location([]) == {}


def test_get_best_location_none_accuracy_with_semantic_scores_zero() -> None:
    """A missing accuracy with a semantic name scores as perfectly accurate."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    now = 1_700_000_000.0
    semantic = {
        "timestamp": now,
        "last_seen": now,
        "accuracy": None,
        "semantic_name": "Home",
    }
    inaccurate = {
        "timestamp": now,
        "last_seen": now,
        "accuracy": 100,
        "semantic_name": None,
    }

    with patch(f"{_MODULE}.time.time", return_value=now):
        best = recorder.get_best_location([inaccurate, semantic])

    # Equal timestamps -> the semantic record (score 0) beats accuracy=100.
    assert best is semantic


def test_get_best_location_non_numeric_accuracy_scores_infinite() -> None:
    """A non-numeric accuracy is treated as infinitely bad, losing the tie-break."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    now = 1_700_000_000.0
    bad = {
        "timestamp": now,
        "last_seen": now,
        "accuracy": "not-a-number",
        "semantic_name": None,
    }
    good = {
        "timestamp": now,
        "last_seen": now,
        "accuracy": 10,
        "semantic_name": None,
    }

    with patch(f"{_MODULE}.time.time", return_value=now):
        best = recorder.get_best_location([bad, good])

    assert best is good


def test_get_best_location_missing_timestamp_scores_infinite() -> None:
    """A record without any usable timestamp loses to one that has a timestamp."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    now = 1_700_000_000.0
    no_ts = {"accuracy": 5, "semantic_name": None}  # no last_seen, no timestamp
    with_ts = {
        "timestamp": now,
        "last_seen": now,
        "accuracy": 500,
        "semantic_name": None,
    }

    with patch(f"{_MODULE}.time.time", return_value=now):
        best = recorder.get_best_location([no_ts, with_ts])

    # Despite the far worse accuracy, the timestamped record wins.
    assert best is with_ts


def test_get_best_location_old_location_penalized_but_returned() -> None:
    """A single location older than two hours exercises the age penalty branch."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    now = 1_700_000_000.0
    very_old = {
        "timestamp": now - 3 * 3600,
        "last_seen": now - 3 * 3600,
        "accuracy": 5,
        "semantic_name": None,
    }

    with patch(f"{_MODULE}.time.time", return_value=now):
        assert recorder.get_best_location([very_old]) is very_old


def test_get_best_location_falls_back_to_first_on_scoring_error() -> None:
    """If ranking raises unexpectedly, the first candidate is returned as fallback."""

    recorder = LocationRecorder(SimpleNamespace())  # type: ignore[arg-type]
    first = {"timestamp": 1.0, "last_seen": 1.0, "accuracy": 5, "semantic_name": None}
    second = {"timestamp": 2.0, "last_seen": 2.0, "accuracy": 5, "semantic_name": None}

    with patch(f"{_MODULE}._normalize_epoch", side_effect=RuntimeError("boom")):
        best = recorder.get_best_location([first, second])

    assert best is first


# ---------------------------------------------------------------------------
# _normalize_epoch — numeric and string normalization
# ---------------------------------------------------------------------------


def test_normalize_epoch_none_and_blank_string() -> None:
    """None and whitespace-only strings normalize to None."""

    assert _normalize_epoch(None) is None
    assert _normalize_epoch("   ") is None


def test_normalize_epoch_invalid_iso_returns_none() -> None:
    """A non-numeric, non-ISO string normalizes to None."""

    assert _normalize_epoch("not-a-date") is None


def test_normalize_epoch_naive_iso_assumes_utc() -> None:
    """A naive ISO string (no offset) is interpreted as UTC."""

    result = _normalize_epoch("2024-01-01T00:00:00")
    assert result == pytest.approx(datetime(2024, 1, 1, tzinfo=UTC).timestamp())


def test_normalize_epoch_unsupported_type_returns_none() -> None:
    """A value that is neither number nor string normalizes to None."""

    assert _normalize_epoch([1, 2, 3]) is None


def test_normalize_epoch_non_finite_returns_none() -> None:
    """NaN and infinite floats normalize to None."""

    assert _normalize_epoch(float("nan")) is None
    assert _normalize_epoch(float("inf")) is None


def test_normalize_epoch_milliseconds_scaled_to_seconds() -> None:
    """Millisecond epochs (> 1e12) are scaled down to seconds."""

    assert _normalize_epoch(1_700_000_000_000) == pytest.approx(1_700_000_000.0)


# ---------------------------------------------------------------------------
# _extract_last_seen — UTC ISO derivation
# ---------------------------------------------------------------------------


def test_extract_last_seen_falls_back_to_raw_string() -> None:
    """When only last_seen (a string) is present, it doubles as the UTC ISO."""

    ts, utc = _extract_last_seen({"last_seen": "2024-01-01T00:00:00Z"})
    assert ts == pytest.approx(datetime(2024, 1, 1, tzinfo=UTC).timestamp())
    assert utc == "2024-01-01T00:00:00Z"


def test_extract_last_seen_builds_iso_from_numeric_epoch() -> None:
    """A numeric last_seen without a UTC string derives the ISO from the epoch."""

    ts, utc = _extract_last_seen({"last_seen": 1_700_000_000})
    assert ts == pytest.approx(1_700_000_000.0)
    expected = (
        datetime.fromtimestamp(1_700_000_000, tz=UTC).isoformat().replace("+00:00", "Z")
    )
    assert utc == expected


def test_extract_last_seen_overflowing_epoch_yields_no_iso() -> None:
    """An out-of-range epoch keeps the timestamp but cannot build an ISO string."""

    ts, utc = _extract_last_seen({"last_seen": 1e30})
    assert ts is not None
    assert utc is None


# ---------------------------------------------------------------------------
# _coerce_iso
# ---------------------------------------------------------------------------


def test_coerce_iso_variants() -> None:
    """Only non-empty strings survive coercion; blanks and non-strings drop out."""

    assert _coerce_iso("   ") is None
    assert _coerce_iso(12345) is None
    assert _coerce_iso("2024-01-01T00:00:00Z") == "2024-01-01T00:00:00Z"
