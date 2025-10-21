# tests/test_token_cache_namespace.py
"""Regression tests for TokenCache entry_id propagation and namespacing."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

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
