# tests/test_map_view_html_escaping.py
"""Tests for HTML escaping in the map view renderer."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from tests.test_map_view_unique_id_resolution import _load_map_view_module


def test_generate_map_html_escapes_dynamic_content(monkeypatch) -> None:
    """Ensure map HTML escapes device names and entity data."""

    map_view = _load_map_view_module(monkeypatch)
    view = map_view.GoogleFindMyMapView(SimpleNamespace())

    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    html_content = view._generate_map_html(
        "<script>alert(1)</script>",
        [
            {
                "lat": 10.0,
                "lon": 20.0,
                "accuracy": 5,
                "timestamp": now.isoformat(),
                "entity_id": "device_tracker.bad<script>",
                "state": "<img src=x onerror=alert(1)>",
                "semantic_location": "Mall <script>",
                "is_own_report": True,
            }
        ],
        "device<script>",
        now,
        now + timedelta(hours=1),
        accuracy_filter=0,
    )

    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_content
    assert "<script>alert(1)</script>" not in html_content
    assert "device_tracker.bad<script>" not in html_content
    assert "Entity State:</b> <img" not in html_content
    assert "Mall <script>" not in html_content
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_content
