# tests/test_cli_entry_selection.py

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from custom_components.googlefindmy.exceptions import MissingTokenCacheError
from custom_components.googlefindmy.NovaApi.ListDevices import nbe_list_devices


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
    """_resolve_cli_cache should auto-select a single entry and return the cache."""

    cache = _DummyCache("entry-one")
    monkeypatch.setattr(
        nbe_list_devices, "get_registered_entry_ids", lambda: ["entry-one"]
    )
    monkeypatch.setattr(nbe_list_devices, "get_cache_for_entry", lambda entry: cache)

    # Explicit hint works as before
    resolved_cache, namespace = nbe_list_devices._resolve_cli_cache("entry-one")
    assert resolved_cache is cache
    assert namespace == "entry-one"

    # With exactly one entry registered, None hint auto-selects it
    resolved_cache, namespace = nbe_list_devices._resolve_cli_cache(None)
    assert resolved_cache is cache
    assert namespace == "entry-one"

    # With no entries registered, any hint raises
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


async def test_cli_main_passes_selected_cache(monkeypatch: pytest.MonkeyPatch) -> None:
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

    # Respond "1" to select the first tracker, then "q" to quit the loop.
    _inputs = iter(["1", "q"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(_inputs))

    # Use await instead of asyncio.run() to avoid creating a separate event
    # loop that leaves pycares DNS resolver threads dangling on shutdown.
    await nbe_list_devices._async_cli_main("entry-one")

    assert called["list_cache"] is cache
    assert called["list_namespace"] == "entry-one"
    assert called["loc_cache"] is cache
    assert called["loc_namespace"] == "entry-one"


class _StopAfterResolve(Exception):
    """Sentinel raised from a stubbed _resolve_cli_cache to stop _async_cli_main."""


class TestAsyncCliMainEntryResolution:
    """The env fallback must apply only for an unspecified (None) entry id.

    Regression for Codex review on 17669c7ff2: ``entry_id or env`` conflated an
    explicit empty string (the documented "use the sole default cache" selector
    that ``--entry ""`` sets to defeat ``GOOGLEFINDMY_ENTRY_ID``) with "not
    supplied", so the env value overrode it and ``_resolve_cli_cache`` raised
    ``Unknown entry_id``.  ``_async_cli_main`` now resolves with ``is not None``,
    making it the single source of the env fallback.
    """

    @staticmethod
    def _capture_hint(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        """Stub _resolve_cli_cache to record its hint and short-circuit."""
        seen: dict[str, Any] = {}

        def _stub(hint: Any) -> tuple[Any, str]:
            seen["hint"] = hint
            raise _StopAfterResolve

        monkeypatch.setattr(nbe_list_devices, "_resolve_cli_cache", _stub)
        return seen

    async def test_explicit_empty_entry_id_is_not_overridden_by_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--entry ""`` must win over GOOGLEFINDMY_ENTRY_ID (CLI > env)."""
        monkeypatch.setenv("GOOGLEFINDMY_ENTRY_ID", "from-env")
        seen = self._capture_hint(monkeypatch)

        with pytest.raises(_StopAfterResolve):
            await nbe_list_devices._async_cli_main("")

        assert seen["hint"] == ""

    async def test_none_entry_id_falls_back_to_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unspecified (None) entry id still uses the env var."""
        monkeypatch.setenv("GOOGLEFINDMY_ENTRY_ID", "from-env")
        seen = self._capture_hint(monkeypatch)

        with pytest.raises(_StopAfterResolve):
            await nbe_list_devices._async_cli_main(None)

        assert seen["hint"] == "from-env"

    async def test_explicit_entry_id_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-empty explicit entry id is used verbatim, ignoring env."""
        monkeypatch.setenv("GOOGLEFINDMY_ENTRY_ID", "from-env")
        seen = self._capture_hint(monkeypatch)

        with pytest.raises(_StopAfterResolve):
            await nbe_list_devices._async_cli_main("explicit")

        assert seen["hint"] == "explicit"
