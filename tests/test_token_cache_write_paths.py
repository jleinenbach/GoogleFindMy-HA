# tests/test_token_cache_write_paths.py
"""Two write paths of ``TokenCache`` no other test reached.

* ``set`` schedules no save while the snapshot holds a value that is not JSON
  (one written through ``sync_set``, which does not validate), and says so.
* ``get_or_set`` accepts a plain (non-coroutine) generator.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.Auth import token_cache as tc
from custom_components.googlefindmy.Auth.token_cache import TokenCache


@pytest.fixture
def cache(monkeypatch: pytest.MonkeyPatch) -> TokenCache:
    """A real TokenCache with the Store patched out (no disk I/O)."""

    monkeypatch.setattr(tc, "_INSTANCES", {}, raising=True)
    monkeypatch.setattr(tc, "Store", Mock())
    hass = SimpleNamespace(loop=asyncio.get_event_loop(), data={})
    instance = TokenCache(hass, "entry-write-paths")
    instance._store = Mock()
    return instance


@pytest.mark.asyncio
async def test_an_invalid_snapshot_schedules_no_save(
    cache: TokenCache, caplog: pytest.LogCaptureFixture
) -> None:
    cache.sync_set("unserialisable", {1, 2})

    with caplog.at_level(logging.ERROR):
        await cache.set("valid", "value")

    cache._store.async_delay_save.assert_not_called()  # type: ignore[attr-defined]
    assert any(
        "non-JSON-serializable snapshot" in r.getMessage() for r in caplog.records
    )


@pytest.mark.asyncio
async def test_get_or_set_accepts_a_plain_generator(cache: TokenCache) -> None:
    assert await cache.get_or_set("computed", lambda: "value") == "value"
    assert await cache.get("computed") == "value"
