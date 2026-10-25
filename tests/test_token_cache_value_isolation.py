# tests/test_token_cache_value_isolation.py
"""TokenCache hands out and keeps detached values.

``TokenCache.set`` skips the write when the stored value equals the new one.
While ``get`` returned the stored object itself, a caller that edited that
object in place and then called ``set`` compared the object with itself: the
write was skipped and nothing was saved. ``_invalidate_fcm_tokens`` is such a
caller, so an invalidated FCM registration reached disk only by accident,
when an unrelated key was written later.

The cache now copies container values on the way in and on the way out. The
tests pin both directions and the invalidation path that depends on them.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from custom_components.googlefindmy.Auth import token_cache as tc
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.Auth.token_cache import TokenCache

ENTRY_ID = "entry-value-isolation"


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
    cache = TokenCache(hass, ENTRY_ID)
    cache._store = Mock()
    return cache


def _registration() -> dict[str, Any]:
    """Return a complete FCM registration with a nested ``gcm`` block."""
    return {
        "gcm": {
            "android_id": "1234567890",
            "security_token": "9876543210",
            "token": "gcm-token",
            "app_id": "subscription-app-id",
        },
        "fcm": {"registration": {"token": "fcm-token"}},
    }


def _last_saved_snapshot(cache: TokenCache) -> dict[str, Any]:
    """Return the snapshot the most recent deferred save would write."""
    store = cache._store
    assert isinstance(store, Mock)
    writer = store.async_delay_save.call_args.args[0]
    snapshot = writer()
    assert isinstance(snapshot, dict)
    return snapshot


@pytest.mark.asyncio
async def test_editing_a_read_value_does_not_change_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every read path returns a copy the caller may edit freely."""
    cache = _real_cache(monkeypatch)
    await cache.set("fcm_credentials", _registration())

    read_async = await cache.get("fcm_credentials")
    read_sync = cache.sync_get("fcm_credentials")
    read_or_set = await cache.get_or_set("fcm_credentials", _registration)
    for value in (read_async, read_sync, read_or_set):
        value["gcm"].pop("token")
        value.pop("fcm")

    stored = await cache.get("fcm_credentials")
    assert stored == _registration()


@pytest.mark.asyncio
async def test_editing_a_written_value_does_not_change_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both write paths keep their own copy of a container value."""
    cache = _real_cache(monkeypatch)
    written_async = _registration()
    written_sync = ["token-a", "token-b"]
    await cache.set("fcm_credentials", written_async)
    cache.sync_set("fcm_routing_tokens", written_sync)

    written_async["gcm"]["token"] = "edited"
    written_sync.append("token-c")

    assert await cache.get("fcm_credentials") == _registration()
    assert cache.sync_get("fcm_routing_tokens") == ["token-a", "token-b"]


@pytest.mark.asyncio
async def test_scalar_values_pass_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strings and missing keys are returned as they are."""
    cache = _real_cache(monkeypatch)
    await cache.set("aas_token", "aas_et/value")

    assert await cache.get("aas_token") == "aas_et/value"
    assert cache.sync_get("missing") is None


@pytest.mark.asyncio
async def test_an_invalidated_registration_is_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_invalidate_fcm_tokens`` against a real cache schedules a save.

    The receiver's live registration is the value it read from the cache, as
    on startup. The invalidation edits that value in place and writes it back;
    the write must reach the store with the rescued subscription id.
    """
    cache = _real_cache(monkeypatch)
    await cache.set("fcm_credentials", _registration())
    store = cache._store
    assert isinstance(store, Mock)
    store.async_delay_save.reset_mock()

    receiver = FcmReceiverHA()
    receiver._entry_caches[ENTRY_ID] = cache
    receiver.creds[ENTRY_ID] = await cache.get("fcm_credentials")

    await receiver._invalidate_fcm_tokens(ENTRY_ID)

    assert store.async_delay_save.call_count == 1
    saved = _last_saved_snapshot(cache)["fcm_credentials"]
    assert "fcm" not in saved
    assert saved["gcm"] == {
        "android_id": "1234567890",
        "security_token": "9876543210",
        "orphan_app_id": "subscription-app-id",
    }


@pytest.mark.asyncio
async def test_a_failed_save_of_an_invalidation_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cache that rejects the write leaves a warning without the value."""

    class _FailingCache:
        async def set(self, name: str, value: Any) -> None:
            raise RuntimeError("secret-looking failure text")

    receiver = FcmReceiverHA()
    receiver._entry_caches[ENTRY_ID] = _FailingCache()  # type: ignore[assignment]
    receiver.creds[ENTRY_ID] = _registration()

    with caplog.at_level(logging.WARNING):
        await receiver._invalidate_fcm_tokens(ENTRY_ID)

    records = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING
        and "invalidated FCM registration" in record.getMessage()
    ]
    assert len(records) == 1
    message = records[0].getMessage()
    assert ENTRY_ID in message
    assert "RuntimeError" in message
    assert "secret-looking failure text" not in message


@pytest.mark.asyncio
async def test_a_container_inside_a_tuple_is_detached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not only dicts and lists: any value holding a container is copied."""
    cache = _real_cache(monkeypatch)
    written = ({"nested": 1},)
    await cache.set("tuple_value", written)
    written[0]["nested"] = 2

    read = await cache.get("tuple_value")
    read[0]["nested"] = 3

    assert await cache.get("tuple_value") == ({"nested": 1},)


@pytest.mark.asyncio
async def test_all_hands_out_detached_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Editing a value of ``all()`` in place leaves the cache unchanged."""
    cache = _real_cache(monkeypatch)
    await cache.set("fcm_credentials", _registration())

    snapshot = await cache.all()
    snapshot["fcm_credentials"]["gcm"].pop("token")

    assert await cache.get("fcm_credentials") == _registration()


def test_the_sync_facades_hand_out_and_keep_detached_values() -> None:
    """``get_cached_value``, ``set_cached_value`` and ``get_cached_value_or_set``.

    The module facades reach the default instance's store directly; they copy
    like the instance methods, so no caller shares a container with the cache.
    """

    cache = SimpleNamespace(_data={})
    tc._INSTANCES[ENTRY_ID] = cache

    written = _registration()
    tc.set_cached_value("fcm_credentials", written)
    written["gcm"]["token"] = "edited-after-set"
    read = tc.get_cached_value("fcm_credentials")
    read["gcm"]["token"] = "edited-after-get"
    hit = tc.get_cached_value_or_set("fcm_credentials", _registration)
    hit["gcm"]["token"] = "edited-after-hit"
    generated = tc.get_cached_value_or_set("other", _registration)
    generated["gcm"]["token"] = "edited-after-miss"

    assert cache._data["fcm_credentials"] == _registration()
    assert cache._data["other"] == _registration()
