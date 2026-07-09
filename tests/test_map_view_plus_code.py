# tests/test_map_view_plus_code.py
"""Server-side tests for the map-view Plus Code wiring.

Covers the extracted pure builder ``_build_location_entry`` (and its helper
``_plus_code_for``), so the ``plus_code`` payload field is verified without the
HTTP stack. The client-side copy JS is intentionally not unit-tested (no JS test
harness in the repo); it is exercised via the manual checklist in the plan's AP3
regression check for both the HTTPS and plain-LAN HTTP contexts.
"""

from __future__ import annotations

import math

from custom_components.googlefindmy import map_view


def test_build_location_entry_adds_plus_code() -> None:
    """A valid coordinate yields the correct 10-digit Plus Code in the payload."""
    entry = map_view._build_location_entry(
        lat=47.365590,
        lon=8.524997,
        accuracy=12.0,
        accuracy_estimated=False,
        timestamp="2026-07-09T12:00:00+00:00",
        last_seen=1_700_000_000,
        is_own_report=True,
        semantic_location="Home",
    )
    assert entry["plus_code"] == "8FVC9G8F+6X"
    assert len(entry["plus_code"]) == 11
    assert entry["plus_code"][8] == "+"


def test_build_location_entry_passes_through_existing_fields() -> None:
    """Pre-existing payload fields are unchanged; only plus_code is added."""
    entry = map_view._build_location_entry(
        lat=52.5219,
        lon=13.4132,
        accuracy=8.5,
        accuracy_estimated=True,
        timestamp="2026-07-09T12:00:00+00:00",
        last_seen=1_700_000_123,
        is_own_report=False,
        semantic_location=None,
    )
    assert entry["lat"] == 52.5219
    assert entry["lon"] == 13.4132
    assert entry["accuracy"] == 8.5
    assert entry["accuracy_estimated"] is True
    assert entry["timestamp"] == "2026-07-09T12:00:00+00:00"
    assert entry["last_seen"] == 1_700_000_123
    assert entry["is_own_report"] is False
    assert entry["semantic_location"] is None
    # The added key exists and no coordinate keys leak beyond lat/lon.
    assert "plus_code" in entry
    assert set(entry) == {
        "lat",
        "lon",
        "accuracy",
        "accuracy_estimated",
        "timestamp",
        "last_seen",
        "is_own_report",
        "semantic_location",
        "plus_code",
    }


def test_plus_code_for_rejects_non_finite() -> None:
    """NaN/inf coordinates yield None rather than a bogus code."""
    assert map_view._plus_code_for(math.nan, 13.0) is None
    assert map_view._plus_code_for(52.0, math.inf) is None
    assert map_view._plus_code_for(-math.inf, math.nan) is None


def test_plus_code_for_valid_coordinate() -> None:
    """A finite coordinate returns an 11-character code with the separator."""
    code = map_view._plus_code_for(47.365590, 8.524997)
    assert code == "8FVC9G8F+6X"
