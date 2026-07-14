# tests/test_map_view_bug_hardening.py
"""Regression tests for three pre-existing map-view robustness defects.

These were surfaced by an audit of ``map_view.py`` (PR #1185) and fixed in the
same PR. None of them originates from the collapsible-filter change; they are
latent hardening gaps in the history/render path:

1. **Non-finite history coordinates (BUG 1).** ``float("nan")`` / ``float("inf")``
   do not raise, so a corrupted recorder ``latitude``/``longitude`` attribute
   slipped through and poisoned the map center. ``center_lat``/``center_lon`` are
   interpolated raw into the Leaflet ``setView`` call, and a bare ``nan`` token is
   a JavaScript ``ReferenceError`` that kills the whole script. The fix skips
   non-finite fixes at the source, mirroring the live-point guard in
   ``_plus_code_for``.

2. **Recorder integration disabled (BUG 2).** ``get_instance()`` raises when the
   recorder integration is not set up (a valid, if rare, configuration). The
   lookup sat outside the ``try`` that wraps the history fetch, so the exception
   propagated and produced an HTTP 500 instead of a map without the track. The
   fix guards the lookup and falls back to the HASS executor.

3. **Cleared datetime-local filter fields (BUG 3).** ``new Date("")`` is an
   Invalid Date and ``.toISOString()`` then throws a ``RangeError``, so the
   "apply filters" button silently did nothing when a field was cleared. The
   first fix guarded each field with ``isNaN(parsed.getTime())`` and only
   overwrote the query parameter when the value parsed. Codex then flagged a
   follow-up defect: because the invalid branch did *nothing*, a stale ``start``/
   ``end`` parameter already on the URL survived, so a cleared field re-applied
   the previous bound on reload and appeared impossible to clear. The final fix
   extracts ``setDateParam(url, id)`` as a total projection of the field state
   onto the URL: valid value -> ``set``, empty/invalid value -> ``delete``. The
   helper is shared by ``start`` and ``end`` so the two cannot drift apart (the
   sibling ``end`` field carried the identical latent defect Codex did not name).
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_map_view_accuracy_estimated import (
    _install_history,
    _registry_for,
    _run_get,
    _wire_view,
)
from tests.test_map_view_unique_id_resolution import (
    _load_map_view_module,
    _StubCoordinator,
)


def _state(lat: str, lon: str, *, when: datetime) -> SimpleNamespace:
    """Build a recorder-history state stub with the given coordinates."""

    return SimpleNamespace(
        attributes={
            "latitude": lat,
            "longitude": lon,
            "last_seen": when.isoformat().replace("+00:00", "Z"),
            "gps_accuracy": 8.0,
            "accuracy_estimated": False,
        },
        last_updated=when,
        state="on",
    )


# --------------------- BUG 1: non-finite history coordinates ---------------------


@pytest.mark.asyncio
async def test_history_skips_non_finite_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``NaN``/``Inf`` fix must be dropped, leaving only finite points.

    Without the guard the corrupted row would be plotted and, worse, drag the
    computed map center to ``NaN`` (a raw ``nan`` token in the ``setView`` call
    is a JS ``ReferenceError``). The finite sibling point must survive so we
    prove the skip is surgical, not a blanket bail-out.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-nan"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    base = datetime(2024, 7, 1, tzinfo=UTC)
    states = [
        _state("nan", "20.0", when=base),  # corrupted latitude -> skipped
        _state("10.0", "inf", when=base + timedelta(minutes=1)),  # corrupted lon
        _state("11.0", "21.0", when=base + timedelta(minutes=2)),  # finite -> kept
    ]
    _install_history(monkeypatch, states)

    response = await _run_get(map_view, entry, device_id)

    assert response.status == 200
    assert len(captured) == 1
    assert captured[0]["lat"] == 11.0
    assert captured[0]["lon"] == 21.0


# --------------------- BUG 2: recorder integration disabled ---------------------


@pytest.mark.asyncio
async def test_history_survives_recorder_get_instance_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising ``get_instance`` must degrade to a 200 map, not an HTTP 500.

    When the recorder integration is disabled ``get_instance(hass)`` raises
    (KeyError on ``hass.data``). The lookup is now inside the guarded block and
    falls back to the HASS executor, so the history fetch still runs and the map
    renders instead of the request blowing up.
    """

    map_view = _load_map_view_module(monkeypatch)

    device_id = "device-norecorder"
    coordinator = _StubCoordinator(devices=[{"id": device_id, "name": "Device"}])
    registry = _registry_for("entry-acc", device_id, coordinator)
    entry, captured = _wire_view(
        monkeypatch, map_view, device_id=device_id, registry=registry
    )

    recorder_mod = sys.modules["homeassistant.components.recorder"]
    calls: list[Any] = []

    def _raise(hass: Any) -> Any:
        calls.append(hass)
        raise KeyError("recorder")  # recorder integration not set up

    monkeypatch.setattr(recorder_mod, "get_instance", _raise, raising=False)

    _install_history(
        monkeypatch, [_state("10.0", "20.0", when=datetime(2024, 7, 1, tzinfo=UTC))]
    )

    response = await _run_get(map_view, entry, device_id)

    assert calls, "get_instance should have been attempted"
    assert response.status == 200
    # Executor fallback kept the history path alive despite the disabled recorder.
    assert len(captured) == 1


# --------------------- BUG 3: cleared datetime-local fields ---------------------


def _render_html(monkeypatch: pytest.MonkeyPatch) -> str:
    """Render real map HTML (no capture stub) for JS-structure assertions."""

    map_view = _load_map_view_module(monkeypatch)
    hass = SimpleNamespace(config=SimpleNamespace(language="en"))
    view = map_view.GoogleFindMyMapView(hass)
    now = datetime(2024, 1, 1, tzinfo=UTC)
    return view._generate_map_html(
        "MyPhone",
        [
            {
                "lat": 52.52,
                "lon": 13.41,
                "accuracy": 8.0,
                "accuracy_estimated": False,
                "timestamp": now.isoformat(),
                "last_seen": 1_735_000_000,
                "is_own_report": True,
                "semantic_location": "Home",
                "plus_code": "9F4MGCC7+Q7",
            }
        ],
        "device-1",
        now,
        now + timedelta(days=1),
        0,
    )


def test_apply_filters_uses_shared_date_param_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both filter fields go through the single ``setDateParam`` helper.

    Extracting one helper is what keeps ``start`` and ``end`` from drifting:
    Codex flagged only ``start``, but ``end`` carried the identical defect. A
    shared function makes the two provably identical.
    """

    html = _render_html(monkeypatch)
    assert "function setDateParam(url, id)" in html
    assert "setDateParam(url, 'start')" in html
    assert "setDateParam(url, 'end')" in html
    # The valid branch still emits a guarded ISO instant.
    assert "isNaN(parsed.getTime())" in html
    assert "url.searchParams.set(id, parsed.toISOString())" in html


def test_apply_filters_deletes_cleared_date_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the Codex follow-up: a cleared field DELETES its param.

    Previously the invalid-date branch did nothing, so a stale ``start``/``end``
    already on the URL survived and re-applied the old bound on reload (the
    field appeared impossible to clear). The invalid branch must now remove the
    parameter. Mutation check: reverting ``delete(id)`` back to a no-op turns
    this assertion red.
    """

    html = _render_html(monkeypatch)
    assert "url.searchParams.delete(id)" in html


def test_apply_filters_has_no_unguarded_toisostring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old crash pattern (parse then immediately ``toISOString``) is gone.

    Locks the regression: previously ``var start = parsed.toISOString();`` ran
    unconditionally and threw on a cleared field. The only ``toISOString`` call
    left must be the single guarded one inside ``setDateParam``.
    """

    html = _render_html(monkeypatch)
    # Ignore ``//`` comment lines (they legitimately mention ``.toISOString()``);
    # only executable JS is under test here.
    code = "\n".join(
        line for line in html.splitlines() if not line.lstrip().startswith("//")
    )
    stripped = code.replace("url.searchParams.set(id, parsed.toISOString())", "")
    assert "toISOString()" not in stripped
    # No per-field set survives outside the shared helper (would resurrect drift).
    assert "searchParams.set('start'" not in html
    assert "searchParams.set('end'" not in html
