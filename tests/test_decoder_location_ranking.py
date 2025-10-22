# tests/test_decoder_location_ranking.py
"""Regression tests for decoder location prioritization heuristics."""

from __future__ import annotations

from custom_components.googlefindmy.ProtoDecoders.decoder import _select_best_location


def test_decoder_prefers_newer_coordinates_over_owner_status() -> None:
    """A fresher aggregated report with coordinates outranks an older owner report."""

    older_owner = {
        "status": "OWNER",
        "is_own_report": True,
        "last_seen": 1_700_000_000,
        "latitude": 52.5200,
        "longitude": 13.4050,
        "accuracy": 120.0,
        "altitude": 35.0,
    }

    fresher_aggregated = {
        "status": "aggregated",
        "is_own_report": False,
        "last_seen": 1_700_000_500,
        "latitude": 48.8566,
        "longitude": 2.3522,
        "accuracy": 250.0,
        "altitude": 40.5,
        "_report_hint": "high_traffic",
    }

    best, _ = _select_best_location([older_owner, fresher_aggregated])

    assert best["status"] == "aggregated"
    assert best["last_seen"] == 1_700_000_500.0
    assert best["altitude"] == 40.5
