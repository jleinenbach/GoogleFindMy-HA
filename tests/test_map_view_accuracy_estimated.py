# tests/test_map_view_accuracy_estimated.py
"""Tests for the map view ``accuracy_estimated`` producer flag and auto-focus.

These tests reuse the lightweight Home Assistant stub loader from
``test_map_view_unique_id_resolution`` so the map module is exercised in
isolation. They cover:

- The consumer read switch: map_view prefers the ``accuracy_estimated``
  attribute set by the producer (cache.py) and only falls back to the validity
  predicate for legacy rows that predate the flag.
- The JavaScript auto-focus fix: the newest real-accuracy point wins regardless
  of whether the newest row overall is estimated.
- Coverage edges: registry suffix matching and timestamp de-duplication.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.const import TRACKER_SUBENTRY_KEY
from tests.test_map_view_unique_id_resolution import (
    _load_map_view_module,
    _StubCoordinator,
    _StubEntityEntry,
    _StubEntityRegistry,
    _StubEntry,
    _StubHass,
)


def _install_history(monkeypatch: pytest.MonkeyPatch, states: list[Any]) -> None:
    """Install a recorder history stub returning ``states`` for the entity."""

    def _stub_history(
        _hass: Any, _start: Any, _end: Any, entity_ids: list[str]
    ) -> dict[str, Any]:
        return {entity_ids[0]: states}

    history_module = ModuleType("homeassistant.components.recorder.history")
    history_module.get_significant_states = _stub_history
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.recorder.history",
        history_module,
    )


def _wire_view(
    monkeypatch: pytest.MonkeyPatch,
    map_view: ModuleType,
    *,
    device_id: str,
    registry: Any,
) -> tuple[Any, list[dict[str, Any]]]:
    """Wire a map view handler with a token resolver and HTML capture hook."""

    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    entry = _StubEntry("entry-acc", coordinator)

    monkeypatch.setattr(
        map_view, "GoogleFindMyCoordinator", _StubCoordinator, raising=False
    )
    monkeypatch.setattr(
        map_view,
        "_resolve_entry_by_token",
        lambda _hass, token: (entry, {token}) if token == "valid" else (None, None),
        raising=False,
    )
    monkeypatch.setattr(
        map_view.er,
        "async_get",
        lambda _hass: registry,
        raising=False,
    )

    captured_locations: list[dict[str, Any]] = []

    def _capture_html(
        self: Any,
        _device_name: str,
        locations: list[dict[str, Any]],
        *_args: Any,
        **_kwargs: Any,
    ) -> str:
        captured_locations.extend(locations)
        return "ok"

    monkeypatch.setattr(
        map_view.GoogleFindMyMapView,
        "_generate_map_html",
        _capture_html,
        raising=False,
    )

    return entry, captured_locations


def _registry_for(entry_id: str, device_id: str, coordinator: Any) -> Any:
    identifier = coordinator.stable_subentry_identifier(key=TRACKER_SUBENTRY_KEY)
    return _StubEntityRegistry(
        [
            _StubEntityEntry(
                entity_id="device_tracker.googlefindmy_primary",
                unique_id=f"{entry_id}:{identifier}:{device_id}",
                config_entry_id=entry_id,
            )
        ]
    )


async def _run_get(map_view: ModuleType, entry: Any, device_id: str) -> Any:
    hass = _StubHass()
    view = map_view.GoogleFindMyMapView(hass)
    request = SimpleNamespace(query={"token": "valid"})
    return await view.get(request, device_id)


@pytest.mark.asyncio
async def test_map_view_prefers_producer_accuracy_estimated_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200m fallback row flagged by the producer must read as estimated.

    The conservative fallback (DEFAULT_ACCURACY_FALLBACK_M = 200) is a valid
    accuracy value, so ``is_valid_accuracy(200.0)`` is True and the legacy
    derivation would mislabel the point as a real measurement. When the producer
    sets ``accuracy_estimated=True`` the consumer must honor it.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-flag"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                "last_seen": "2024-01-01T00:00:00Z",
                # 200m fallback that is indistinguishable from a real 200m fix
                # by value alone; the producer flag disambiguates it.
                "gps_accuracy": 200.0,
                "accuracy_estimated": True,
            },
            last_updated=datetime(2024, 7, 1, tzinfo=UTC),
            state="one",
        ),
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)

    assert response.status == 200
    assert len(captured) == 1
    assert captured[0]["accuracy_estimated"] is True


@pytest.mark.asyncio
async def test_map_view_honors_producer_false_flag_over_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer ``accuracy_estimated=False`` must win even if raw looks invalid.

    If the producer states the value is a real measurement, the consumer must
    not override it with the legacy predicate. We use a raw gps_accuracy that the
    predicate would reject (0.0 error code) so the False flag is the only way to
    reach a non-estimated result, making the switch sharp.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-false"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                "last_seen": "2024-01-01T00:00:00Z",
                # Predicate would flag this as estimated (0.0 error code); the
                # explicit producer False must override that derivation.
                "gps_accuracy": 0.0,
                "accuracy_estimated": False,
            },
            last_updated=datetime(2024, 7, 1, tzinfo=UTC),
            state="one",
        ),
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)

    assert response.status == 200
    assert captured[0]["accuracy_estimated"] is False


@pytest.mark.asyncio
async def test_map_view_legacy_row_without_flag_uses_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy rows without the flag fall back to the validity predicate.

    Recorder history written before this change has no ``accuracy_estimated``
    attribute. The consumer must derive the flag from the raw gps_accuracy:
    a real 12.5m fix is not estimated, while the 0.0 error code is.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-legacy"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                "last_seen": "2024-01-01T00:00:00Z",
                "gps_accuracy": 12.5,
            },
            last_updated=datetime(2024, 7, 1, tzinfo=UTC),
            state="real",
        ),
        SimpleNamespace(
            attributes={
                "latitude": "11.0",
                "longitude": "21.0",
                "last_seen": "2024-01-02T00:00:00Z",
                "gps_accuracy": 0.0,
            },
            last_updated=datetime(2024, 8, 1, tzinfo=UTC),
            state="errorcode",
        ),
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)

    assert response.status == 200
    assert len(captured) == 2
    by_lat = {loc["lat"]: loc for loc in captured}
    assert by_lat[10.0]["accuracy_estimated"] is False
    assert by_lat[11.0]["accuracy_estimated"] is True


@pytest.mark.asyncio
async def test_map_view_registry_suffix_matching_colon_and_underscore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry fallback matches both ``:device_id`` and ``_device_id`` suffixes.

    Covers the suffix fallback branch (map_view registry resolution) for both
    delimiter variants, plus the negative case where no entity matches.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "dev-suffix"

    for unique_id in (f"some_prefix:{device_id}", f"some_prefix_{device_id}"):
        # Registry entry whose unique_id only matches by suffix (exact lookup miss).
        registry = _StubEntityRegistry(
            [
                _StubEntityEntry(
                    entity_id="device_tracker.googlefindmy_suffix",
                    unique_id=unique_id,
                    config_entry_id="entry-acc",
                )
            ]
        )
        entry, captured = _wire_view(
            monkeypatch, map_view, device_id=device_id, registry=registry
        )
        _install_history(
            monkeypatch,
            [
                SimpleNamespace(
                    attributes={
                        "latitude": "10.0",
                        "longitude": "20.0",
                        "last_seen": "2024-01-01T00:00:00Z",
                        "gps_accuracy": 12.5,
                    },
                    last_updated=datetime(2024, 7, 1, tzinfo=UTC),
                    state="one",
                )
            ],
        )

        response = await _run_get(map_view, entry, device_id)
        assert response.status == 200
        # Suffix match resolved the entity, so history rendered one point.
        assert len(captured) == 1


@pytest.mark.asyncio
async def test_map_view_registry_suffix_no_match_renders_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registry entity matches by suffix -> history stays empty."""

    map_view = _load_map_view_module(monkeypatch)

    device_id = "dev-nomatch"
    registry = _StubEntityRegistry(
        [
            _StubEntityEntry(
                entity_id="device_tracker.other",
                unique_id="prefix:different-device",
                config_entry_id="entry-acc",
            )
        ]
    )
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )
    _install_history(
        monkeypatch,
        [
            SimpleNamespace(
                attributes={
                    "latitude": "10.0",
                    "longitude": "20.0",
                    "last_seen": "2024-01-01T00:00:00Z",
                    "gps_accuracy": 12.5,
                },
                last_updated=datetime(2024, 7, 1, tzinfo=UTC),
                state="one",
            )
        ],
    )

    response = await _run_get(map_view, entry, device_id)
    assert response.status == 200
    # No entity resolved, so no history was fetched and no locations captured.
    assert captured == []


def test_generated_js_autofocus_not_coupled_to_last_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-focus must target any non-estimated point, not only the last index.

    Before the fix the JS coupled auto-focus to ``idx === n - 1``: if the newest
    row was estimated, no real point was focused even when older real points
    existed. The fix focuses every non-estimated point so the last (newest, given
    the oldest->newest sort) real point wins.
    """

    map_view = _load_map_view_module(monkeypatch)
    hass = _StubHass()
    view = map_view.GoogleFindMyMapView(hass)

    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)
    locations = [
        {
            "lat": 10.0,
            "lon": 20.0,
            "accuracy": 5.0,
            "accuracy_estimated": False,
            "timestamp": "2024-01-01T00:00:00+00:00",
            "last_seen": start.timestamp(),
            "is_own_report": True,
            "semantic_location": "Sample",
        }
    ]

    html = view._generate_map_html("Device", locations, "device-1", start, end, 0)

    # The auto-focus assignment must condition only on the estimated flag.
    assert "if (!loc.accuracy_estimated) { autoFocusMarker = marker; }" in html
    # And must NOT couple it to the last index any more.
    assert "idx === n - 1 && !loc.accuracy_estimated" not in html


@pytest.mark.asyncio
async def test_map_view_deduplicates_identical_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two states with the same numeric timestamp collapse to one location."""

    map_view = _load_map_view_module(monkeypatch)

    device_id = "dev-dedup"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    same_ts = "2024-01-01T00:00:00Z"
    states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                "last_seen": same_ts,
                "gps_accuracy": 12.5,
            },
            last_updated=datetime(2024, 7, 1, tzinfo=UTC),
            state="first",
        ),
        SimpleNamespace(
            attributes={
                "latitude": "10.1",
                "longitude": "20.1",
                "last_seen": same_ts,
                "gps_accuracy": 13.5,
            },
            last_updated=datetime(2024, 8, 1, tzinfo=UTC),
            state="dup",
        ),
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)
    assert response.status == 200
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_map_view_stale_real_point_uses_accuracy_m_not_gps_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale real fix (gps_accuracy cleared) keeps its real radius via accuracy_m.

    For stale tracker states ``_sync_location_attrs`` clears the core latitude/
    longitude so Home Assistant omits ``gps_accuracy``, but ``_as_ha_attributes``
    still records the cached ``latitude``/``longitude``/``accuracy_m`` and a
    producer ``accuracy_estimated=False``. Reading the value from the absent
    ``gps_accuracy`` (the old behaviour) normalized it to the 200m fallback and,
    paired with the False flag, drew a bogus 200m "real" circle. The point must
    instead carry its real ``accuracy_m`` value (Codex review, PR #1124).
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-stale-real"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                "last_seen": "2024-01-01T00:00:00Z",
                # gps_accuracy intentionally absent (stale state); the stable
                # producer attribute carries the real 35m measurement.
                "accuracy_m": 35.0,
                "accuracy_estimated": False,
            },
            last_updated=datetime(2024, 7, 1, tzinfo=UTC),
            state="stale-real",
        ),
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)

    assert response.status == 200
    assert len(captured) == 1
    # Real radius preserved, not collapsed to the 200m fallback.
    assert captured[0]["accuracy"] == 35.0
    assert captured[0]["accuracy_estimated"] is False


@pytest.mark.asyncio
async def test_map_view_stale_fallback_point_stays_estimated_via_accuracy_m(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale fallback fix keeps accuracy_m=200 and its estimated flag.

    Mirror of the stale-real case for the fallback: when gps_accuracy is cleared
    the producer's accuracy_m=200 and accuracy_estimated=True must be read from
    the same source so the point stays estimated and draws no real circle.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-stale-fallback"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                "last_seen": "2024-01-01T00:00:00Z",
                "accuracy_m": 200.0,
                "accuracy_estimated": True,
            },
            last_updated=datetime(2024, 7, 1, tzinfo=UTC),
            state="stale-fallback",
        ),
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)

    assert response.status == 200
    assert len(captured) == 1
    assert captured[0]["accuracy"] == 200.0
    assert captured[0]["accuracy_estimated"] is True


@pytest.mark.asyncio
async def test_map_view_legacy_row_without_flag_prefers_accuracy_m(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy rows without the flag still prefer accuracy_m over gps_accuracy.

    A row that predates the producer flag but already carries accuracy_m (for
    example a stale state) must derive ``estimated`` from the same accuracy_m
    value the radius is drawn from, keeping value and provenance consistent.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-legacy-accm"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                "last_seen": "2024-01-01T00:00:00Z",
                # No flag; accuracy_m is a real measurement -> not estimated.
                "accuracy_m": 18.0,
            },
            last_updated=datetime(2024, 7, 1, tzinfo=UTC),
            state="legacy-accm",
        ),
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)

    assert response.status == 200
    assert len(captured) == 1
    assert captured[0]["accuracy"] == 18.0
    assert captured[0]["accuracy_estimated"] is False


@pytest.mark.asyncio
async def test_map_view_history_uses_last_seen_utc_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row carrying only ``last_seen_utc`` plots at its report time.

    Codex #1177: once the history fallback accepts recorder-only rows, the
    timestamp logic must parse ``last_seen_utc`` as the same fallback as the
    device_tracker restore path. Without it a restored/legacy row lacking the
    plain ``last_seen`` attribute would fall back to ``state.last_updated`` (the
    recorder write/restart time), making a stale fix appear fresh and
    sort/dedupe incorrectly. The report time (2024-01-01) and the write time
    (2024-08-01) are deliberately far apart so the assertion discriminates the
    fallback from the write-time default.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "dev-lsu"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    report_time = datetime(2024, 1, 1, tzinfo=UTC)
    write_time = datetime(2024, 8, 1, tzinfo=UTC)
    states = [
        SimpleNamespace(
            attributes={
                "latitude": "10.0",
                "longitude": "20.0",
                # Only the UTC mirror is present, no plain ``last_seen``.
                "last_seen_utc": "2024-01-01T00:00:00Z",
                "accuracy_m": 15.0,
            },
            last_updated=write_time,
            state="legacy-lsu",
        ),
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)

    assert response.status == 200
    assert len(captured) == 1
    # The point carries the recorded report time, not the recorder write time.
    assert captured[0]["last_seen"] == pytest.approx(report_time.timestamp())
    assert captured[0]["last_seen"] != pytest.approx(write_time.timestamp())
