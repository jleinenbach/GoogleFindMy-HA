# tests/test_api_device_list_dedup.py
"""Regression tests for canonical ID deduplication in the API device list."""

from __future__ import annotations

from typing import Any

import pytest

import custom_components.googlefindmy.api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI


class _StubCache:
    """Minimal cache implementation for exercising the API helper."""

    entry_id = "dedup-entry"

    async def async_get_cached_value(self, key: str) -> Any:
        return None

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        return None


def _make_api() -> GoogleFindMyAPI:
    """Instantiate the API with the stub cache used in these tests."""

    return GoogleFindMyAPI(cache=_StubCache())


def test_process_device_list_response_merges_duplicate_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate canonical IDs are merged while preserving capability hints."""

    api = _make_api()
    parsed = object()

    monkeypatch.setattr(
        api_module, "parse_device_list_protobuf", lambda hex_blob: parsed
    )
    monkeypatch.setattr(
        api_module, "_build_can_ring_index", lambda message: {"shared-id": True}
    )
    monkeypatch.setattr(
        api_module,
        "get_canonic_ids",
        lambda message: [("Primary", "shared-id"), ("Alias", "shared-id")],
    )

    devices = api._process_device_list_response("deadbeef")

    assert devices == [
        {
            "name": "Primary",
            "id": "shared-id",
            "device_id": "shared-id",
            "can_ring": True,
        }
    ]
