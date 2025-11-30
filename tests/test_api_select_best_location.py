# tests/test_api_select_best_location.py
"""Regression tests for API location selection tie-breaking."""

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


def test_select_best_location_prefers_owner_with_tied_timestamp() -> None:
    """Owner-sourced entries outrank crowdsourced ones when timestamps tie."""

    api = _make_api()
    records = [
        {
            "last_seen": 2000000000,
            "is_own_report": False,
            "status": "CROWDSOURCED",
            "accuracy": 4.0,
            "tag": "network",
        },
        {
            "last_seen": 2000000000,
            "is_own_report": True,
            "status": "OWNER",
            "accuracy": 15.0,
            "altitude": 25.0,
            "tag": "owner",
        },
        {
            "last_seen": 1999999999,
            "is_own_report": False,
            "status": "AGGREGATED",
            "accuracy": 2.0,
            "tag": "older",
        },
    ]

    best = api._select_best_location(records)

    assert best["tag"] == "owner"
    assert best["is_own_report"] is True
    assert best["altitude"] == 25.0
