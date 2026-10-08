# tests/test_log_hygiene_pilot.py
"""Log hygiene for ``TokenCache``: cache key names never reach the log.

Per-account cache keys embed the Google account e-mail
(``owner_key_<email>``, ``shared_key_<email>``), so a key name is personal
data. The error paths for non-JSON-serializable values therefore log the
value type instead of the key. The registry helpers' debug messages avoid
credential wording in front of ``%s`` placeholders so that log scanners do
not flag them as credential disclosure.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.Auth import token_cache as tc
from custom_components.googlefindmy.Auth.token_cache import TokenCache

_EMAIL = "pilot.user@example.com"
_KEY = f"owner_key_{_EMAIL}"


@pytest.fixture(autouse=True)
def _isolate_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-global registry/state around each test."""
    monkeypatch.setattr(tc, "_INSTANCES", {}, raising=True)
    monkeypatch.setattr(
        tc,
        "_STATE",
        {"legacy_migration_done": False, "default_entry_id": None},
        raising=True,
    )


def _real_cache(monkeypatch: pytest.MonkeyPatch) -> TokenCache:
    """Build a real TokenCache with the Store patched out (no disk I/O)."""
    monkeypatch.setattr(tc, "Store", Mock())
    hass = SimpleNamespace(loop=asyncio.get_event_loop(), data={})
    cache = TokenCache(hass, "e1")
    cache._store = Mock()
    return cache


@pytest.mark.asyncio
async def test_set_non_jsonable_logs_type_not_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cache = _real_cache(monkeypatch)
    with caplog.at_level(logging.ERROR, logger=tc._LOGGER.name):
        await cache.set(_KEY, object())

    assert _KEY not in cache._data
    assert caplog.records, "the skip must still be reported"
    assert all(_EMAIL not in r.getMessage() for r in caplog.records)
    assert any("object" in r.getMessage() for r in caplog.records)


def test_snapshot_check_logs_type_not_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cache = _real_cache(monkeypatch)
    cache._data[_KEY] = {1, 2}
    with caplog.at_level(logging.ERROR, logger=tc._LOGGER.name):
        assert cache._is_valid_snapshot() is False

    assert caplog.records
    assert all(_EMAIL not in r.getMessage() for r in caplog.records)
    assert any("set" in r.getMessage() for r in caplog.records)
