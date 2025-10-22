# tests/test_token_cache_namespace.py

"""Regression tests for TokenCache entry_id propagation and namespacing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import token_cache
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy import api as googlefindmy_api


class _FakeHass:
    """Minimal async Home Assistant stub for TokenCache interactions."""

    async def async_add_executor_job(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)


def test_token_cache_create_exposes_entry_id_for_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TokenCache.create should provide an entry_id namespace to API helpers."""

    captured: dict[str, Any] = {}

    async def _run() -> None:
        hass = _FakeHass()
        cache = await TokenCache.create(hass, "entry-namespace")
        assert cache.entry_id == "entry-namespace"

        async def fake_async_request_device_list(*args: Any, **kwargs: Any) -> str:
            captured["args"] = args
            captured["kwargs"] = kwargs
            return "00"

        def fake_process(
            self: googlefindmy_api.GoogleFindMyAPI, result_hex: str
        ) -> list[dict[str, Any]]:
            captured["processed_hex"] = result_hex
            return []

        monkeypatch.setattr(
            googlefindmy_api,
            "async_request_device_list",
            fake_async_request_device_list,
        )
        monkeypatch.setattr(
            googlefindmy_api.GoogleFindMyAPI,
            "_process_device_list_response",
            fake_process,
        )

        api_instance = googlefindmy_api.GoogleFindMyAPI(cache=cache)
        result = await api_instance.async_get_basic_device_list("user@example.com")

        assert result == []
        assert captured["kwargs"]["cache"] is cache
        assert captured["kwargs"]["namespace"] == "entry-namespace"

        await cache.close()

    asyncio.run(_run())


def test_get_default_cache_errors_distinguish_empty_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No registered caches should surface a targeted guidance message."""

    monkeypatch.setattr(token_cache, "_INSTANCES", {}, raising=False)
    monkeypatch.setattr(token_cache, "_DEFAULT_ENTRY_ID", None, raising=False)

    with pytest.raises(RuntimeError) as err:
        token_cache._get_default_cache()

    assert "No TokenCache registered" in str(err.value)
    assert "entry.runtime_data.token_cache" in str(err.value)


def test_get_default_cache_errors_distinguish_multi_entry_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple registered caches must instruct callers to scope by entry."""

    monkeypatch.setattr(
        token_cache,
        "_INSTANCES",
        {"one": object(), "two": object()},
        raising=False,
    )
    monkeypatch.setattr(token_cache, "_DEFAULT_ENTRY_ID", None, raising=False)

    with pytest.raises(RuntimeError) as err:
        token_cache._get_default_cache()

    assert "Multiple config entries active" in str(err.value)
