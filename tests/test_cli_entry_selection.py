# tests/test_cli_entry_selection.py

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from custom_components.googlefindmy.NovaApi.ListDevices import nbe_list_devices
from custom_components.googlefindmy.exceptions import MissingTokenCacheError


class _DummyCache:
    """Simple cache stub exposing entry_id and async cache methods."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id

    async def async_get_cached_value(
        self, key: str
    ) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    async def async_set_cached_value(
        self, key: str, value: Any
    ) -> None:  # pragma: no cover - unused
        raise NotImplementedError


def test_resolve_cli_cache_requires_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """_resolve_cli_cache should enforce entry selection and return the cache."""

    cache = _DummyCache("entry-one")
    monkeypatch.setattr(
        nbe_list_devices, "get_registered_entry_ids", lambda: ["entry-one"]
    )
    monkeypatch.setattr(nbe_list_devices, "get_cache_for_entry", lambda entry: cache)

    resolved_cache, namespace = nbe_list_devices._resolve_cli_cache("entry-one")
    assert resolved_cache is cache
    assert namespace == "entry-one"

    with pytest.raises(MissingTokenCacheError):
        nbe_list_devices._resolve_cli_cache(None)

    monkeypatch.setattr(nbe_list_devices, "get_registered_entry_ids", lambda: [])
    with pytest.raises(MissingTokenCacheError):
        nbe_list_devices._resolve_cli_cache("entry-one")


def test_resolve_cli_cache_multiple_entries_require_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI helper should raise a clear error when multiple caches exist."""

    monkeypatch.setattr(
        nbe_list_devices,
        "get_registered_entry_ids",
        lambda: ["entry-one", "entry-two"],
    )

    with pytest.raises(RuntimeError) as err:
        nbe_list_devices._resolve_cli_cache(None)

    message = str(err.value)
    assert "Multiple token caches registered" in message
    assert "GOOGLEFINDMY_ENTRY_ID" in message

def test_cli_main_passes_selected_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI helper should forward the selected cache/namespace to API calls."""

    cache = _DummyCache("entry-one")
    monkeypatch.setattr(
        nbe_list_devices, "get_registered_entry_ids", lambda: ["entry-one"]
    )
    monkeypatch.setattr(nbe_list_devices, "get_cache_for_entry", lambda entry: cache)

    called: dict[str, Any] = {}

    async def fake_async_request_device_list(
        *, cache: Any, namespace: str, **kwargs: Any
    ) -> str:
        called["list_cache"] = cache
        called["list_namespace"] = namespace
        return "00"

    async def fake_get_location_data_for_device(
        device_id: str,
        device_name: str,
        *,
        cache: Any,
        namespace: str,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        called["loc_cache"] = cache
        called["loc_namespace"] = namespace
        return [{"canonic_id": device_id}]

    monkeypatch.setattr(
        nbe_list_devices, "async_request_device_list", fake_async_request_device_list
    )
    monkeypatch.setattr(
        nbe_list_devices, "parse_device_list_protobuf", lambda _: "proto"
    )
    monkeypatch.setattr(
        nbe_list_devices, "get_canonic_ids", lambda _: [("Tracker", "id-1")]
    )
    fake_spot_module = types.SimpleNamespace(refresh_custom_trackers=lambda _: None)
    fake_location_module = types.SimpleNamespace(
        get_location_data_for_device=fake_get_location_data_for_device
    )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.googlefindmy.SpotApi.UploadPrecomputedPublicKeyIds.upload_precomputed_public_key_ids",
        fake_spot_module,
    )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request",
        fake_location_module,
    )

    monkeypatch.setattr("builtins.input", lambda prompt="": "1")

    asyncio.run(nbe_list_devices._async_cli_main("entry-one"))

    assert called["list_cache"] is cache
    assert called["list_namespace"] == "entry-one"
    assert called["loc_cache"] is cache
    assert called["loc_namespace"] == "entry-one"
