from __future__ import annotations

import importlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import DOMAIN

_MODULE = importlib.import_module("custom_components.googlefindmy")
_async_acquire_shared_fcm = cast(
    Callable[..., Any], getattr(_MODULE, "_async_acquire_shared_fcm")
)
_async_release_shared_fcm = cast(
    Callable[..., Any], getattr(_MODULE, "_async_release_shared_fcm")
)


class _DummyCache:
    def __init__(self, entry_id: str, creds: dict[str, Any]) -> None:
        self.entry_id = entry_id
        self._data: dict[str, Any] = {"fcm_credentials": creds}

    async def get(self, key: str) -> Any:
        return self._data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._data[key] = value


class _DummyEntry(SimpleNamespace):
    entry_id: str


@pytest.mark.asyncio
async def test_entry_scoped_receivers_use_entry_cache() -> None:
    hass = SimpleNamespace(data={DOMAIN: {}})

    entry_a = _DummyEntry(entry_id="entry-a")
    entry_b = _DummyEntry(entry_id="entry-b")

    creds_a = {"fcm": {"registration": {"token": "token-a"}}}
    creds_b = {"fcm": {"registration": {"token": "token-b"}}}

    cache_a = _DummyCache(entry_a.entry_id, creds_a)
    cache_b = _DummyCache(entry_b.entry_id, creds_b)

    receiver_a = await _async_acquire_shared_fcm(
        hass,
        entry=entry_a,
        cache=cache_a,
        entry_resolver=lambda: entry_a.entry_id,
    )
    receiver_b = await _async_acquire_shared_fcm(
        hass,
        entry=entry_b,
        cache=cache_b,
        entry_resolver=lambda: entry_b.entry_id,
    )

    assert isinstance(receiver_a, FcmReceiverHA)
    assert isinstance(receiver_b, FcmReceiverHA)
    assert receiver_a is not receiver_b

    assert receiver_a.get_fcm_token(entry_a.entry_id) == "token-a"
    assert receiver_b.get_fcm_token(entry_b.entry_id) == "token-b"

    await _async_release(receiver_a, hass, entry_a)
    await _async_release(receiver_b, hass, entry_b)


async def _async_release(receiver: FcmReceiverHA, hass: Any, entry: _DummyEntry) -> None:
    receiver.request_stop()
    await _async_release_shared_fcm(hass, entry)
