# tests/test_cli_sound_request_entry_selection.py

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.googlefindmy.NovaApi.ExecuteAction.PlaySound import (
    start_sound_request,
    stop_sound_request,
)
from custom_components.googlefindmy.NovaApi.ListDevices import nbe_list_devices
from custom_components.googlefindmy.exceptions import MissingTokenCacheError


class _DummyCache:
    """Minimal async-compatible cache stub for CLI tests."""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        self._values: dict[str, Any] = {}

    async def async_get_cached_value(self, key: str) -> Any:
        return self._values.get(key)

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self._values[key] = value


@pytest.fixture(autouse=True)
def _clear_entry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the CLI helpers never read an implicit entry from the environment."""

    monkeypatch.delenv("GOOGLEFINDMY_ENTRY_ID", raising=False)


def test_start_cli_requires_explicit_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start-sound CLI helper should refuse to auto-select an entry."""

    cache = _DummyCache("entry-one")
    monkeypatch.setattr(
        nbe_list_devices, "get_registered_entry_ids", lambda: ["entry-one"]
    )
    monkeypatch.setattr(nbe_list_devices, "get_cache_for_entry", lambda entry: cache)

    with pytest.raises(MissingTokenCacheError):
        asyncio.run(start_sound_request._async_cli_main(None))


def test_start_cli_uses_selected_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start-sound CLI helper should honor the requested entry selection."""

    caches = {entry: _DummyCache(entry) for entry in ("entry-one", "entry-two")}

    monkeypatch.setattr(
        nbe_list_devices, "get_registered_entry_ids", lambda: sorted(caches)
    )
    monkeypatch.setattr(
        nbe_list_devices, "get_cache_for_entry", lambda entry: caches[entry]
    )

    recorded: dict[str, Any] = {}

    class _Receiver:
        def __init__(self, *, entry_id: str, cache: _DummyCache) -> None:  # type: ignore[override]
            recorded["receiver_entry"] = entry_id
            recorded["receiver_cache"] = cache

        def register_for_location_updates(self, callback: Any) -> str:
            recorded["callback"] = callback
            return "token-123"

    async def _fake_submit(
        canonic_device_id: str,
        gcm_registration_id: str,
        *,
        cache: _DummyCache,
        namespace: str,
        **kwargs: Any,
    ) -> str:
        recorded["device_id"] = canonic_device_id
        recorded["token"] = gcm_registration_id
        recorded["submit_cache"] = cache
        recorded["submit_namespace"] = namespace
        return "ok"

    monkeypatch.setattr(
        "custom_components.googlefindmy.Auth.fcm_receiver.FcmReceiver", _Receiver
    )
    monkeypatch.setattr(
        start_sound_request,
        "async_submit_start_sound_request",
        _fake_submit,
    )
    monkeypatch.setattr(
        start_sound_request,
        "get_example_data",
        lambda key: "example-canonic-id" if key == "sample_canonic_device_id" else None,
    )

    asyncio.run(start_sound_request._async_cli_main("entry-two"))

    assert recorded["receiver_entry"] == "entry-two"
    assert recorded["receiver_cache"] is caches["entry-two"]
    assert recorded["submit_cache"] is caches["entry-two"]
    assert recorded["submit_namespace"] == "entry-two"
    assert recorded["token"] == "token-123"


def test_stop_cli_requires_explicit_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop-sound CLI helper should refuse to auto-select an entry."""

    cache = _DummyCache("entry-one")
    monkeypatch.setattr(
        nbe_list_devices, "get_registered_entry_ids", lambda: ["entry-one"]
    )
    monkeypatch.setattr(nbe_list_devices, "get_cache_for_entry", lambda entry: cache)

    with pytest.raises(MissingTokenCacheError):
        asyncio.run(stop_sound_request._async_cli_main(None))


def test_stop_cli_uses_selected_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop-sound CLI helper should honor the requested entry selection."""

    caches = {entry: _DummyCache(entry) for entry in ("entry-one", "entry-two")}

    monkeypatch.setattr(
        nbe_list_devices, "get_registered_entry_ids", lambda: sorted(caches)
    )
    monkeypatch.setattr(
        nbe_list_devices, "get_cache_for_entry", lambda entry: caches[entry]
    )

    recorded: dict[str, Any] = {}

    class _Receiver:
        def __init__(self, *, entry_id: str, cache: _DummyCache) -> None:  # type: ignore[override]
            recorded["receiver_entry"] = entry_id
            recorded["receiver_cache"] = cache

        def register_for_location_updates(self, callback: Any) -> str:
            recorded["callback"] = callback
            return "token-456"

    async def _fake_submit(
        canonic_device_id: str,
        gcm_registration_id: str,
        *,
        cache: _DummyCache,
        namespace: str,
        **kwargs: Any,
    ) -> str:
        recorded["device_id"] = canonic_device_id
        recorded["token"] = gcm_registration_id
        recorded["submit_cache"] = cache
        recorded["submit_namespace"] = namespace
        return "ok"

    monkeypatch.setattr(
        "custom_components.googlefindmy.Auth.fcm_receiver.FcmReceiver", _Receiver
    )
    monkeypatch.setattr(
        stop_sound_request,
        "async_submit_stop_sound_request",
        _fake_submit,
    )
    monkeypatch.setattr(
        stop_sound_request,
        "get_example_data",
        lambda key: "example-canonic-id" if key == "sample_canonic_device_id" else None,
    )

    asyncio.run(stop_sound_request._async_cli_main("entry-one"))

    assert recorded["receiver_entry"] == "entry-one"
    assert recorded["receiver_cache"] is caches["entry-one"]
    assert recorded["submit_cache"] is caches["entry-one"]
    assert recorded["submit_namespace"] == "entry-one"
    assert recorded["token"] == "token-456"
