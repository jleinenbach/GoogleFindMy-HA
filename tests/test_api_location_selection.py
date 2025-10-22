# tests/test_api_location_selection.py
"""Unit tests for location record selection heuristics."""

from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.api import GoogleFindMyAPI


class _StubCache:
    """Minimal cache satisfying the API protocol for unit tests."""

    entry_id = "test-entry"

    async def async_get_cached_value(
        self, key: str
    ) -> Any:  # pragma: no cover - not used
        return None

    async def async_set_cached_value(
        self, key: str, value: Any
    ) -> None:  # pragma: no cover - not used
        return None


def _make_api() -> GoogleFindMyAPI:
    """Helper to build the API with the lightweight stub cache."""

    return GoogleFindMyAPI(cache=_StubCache())


def test_select_best_location_prefers_owner_report() -> None:
    """Records with identical timestamps favor the owner's report."""

    api = _make_api()
    records = [
        {
            "last_seen": "1700000000",
            "is_own_report": False,
            "accuracy": 5.0,
            "tag": "network",
        },
        {
            "last_seen": "1700000000",
            "is_own_report": True,
            "accuracy": 15.0,
            "tag": "owner",
        },
        {
            "last_seen": "1699999999",
            "is_own_report": False,
            "accuracy": 1.0,
            "tag": "older",
        },
    ]

    best = api._select_best_location(records)

    assert best["tag"] == "owner"
    assert best["is_own_report"] is True


def test_select_best_location_prefers_precision_without_owner() -> None:
    """Accuracy breaks ties when ownership data is absent or false."""

    api = _make_api()
    records = [
        {
            "last_seen": 1800000000,
            "is_own_report": False,
            "accuracy": 25.0,
            "tag": "coarse",
        },
        {
            "last_seen": 1800000000,
            "is_own_report": False,
            "accuracy": 5.0,
            "tag": "precise",
        },
        {
            "last_seen": 1799999999,
            "is_own_report": False,
            "accuracy": 1.0,
            "tag": "older",
        },
    ]

    best = api._select_best_location(records)

    assert best["tag"] == "precise"
    assert best["accuracy"] == 5.0
