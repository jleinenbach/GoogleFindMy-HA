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
_get_fcm_receivers = cast(
    Callable[[dict[str, Any]], dict[str, Any]], getattr(_MODULE, "_get_fcm_receivers")
)
_domain_fcm_provider = cast(
    Callable[..., Any], getattr(_MODULE, "_domain_fcm_provider")
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


class _DefaultAwareReceiver(SimpleNamespace):
    """Receiver stub that tracks the default entry for token lookups."""

    def __init__(self, mapping: dict[str, str], *, is_ready: bool) -> None:
        super().__init__(is_ready=is_ready)
        self._tokens = mapping
        self.default_entry_id: str | None = None

    def set_default_entry_id(self, entry_id: str | None) -> None:
        self.default_entry_id = entry_id

    def get_fcm_token(self, entry_id: str | None = None) -> str | None:
        target = entry_id or self.default_entry_id
        if target:
            return self._tokens.get(target)
        return None


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


@pytest.mark.asyncio
async def test_acquire_new_entry_keeps_existing_receiver() -> None:
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
    bucket = hass.data[DOMAIN]
    assert bucket.get("fcm_receiver") is receiver_a

    bucket = hass.data[DOMAIN]
    bucket["fcm_receiver"] = object()

    receiver_b = await _async_acquire_shared_fcm(
        hass,
        entry=entry_b,
        cache=cache_b,
        entry_resolver=lambda: entry_b.entry_id,
    )

    receivers = _get_fcm_receivers(bucket)
    assert receivers[entry_a.entry_id] is receiver_a
    assert receivers[entry_b.entry_id] is receiver_b
    assert bucket.get("fcm_receiver") is receiver_a

    await _async_release(receiver_a, hass, entry_a)
    await _async_release(receiver_b, hass, entry_b)


@pytest.mark.asyncio
async def test_legacy_fcm_receiver_alias_preserved() -> None:
    hass = SimpleNamespace(data={DOMAIN: {}})

    entry = _DummyEntry(entry_id="entry-a")
    creds = {"fcm": {"registration": {"token": "token-a"}}}
    cache = _DummyCache(entry.entry_id, creds)

    receiver = await _async_acquire_shared_fcm(
        hass,
        entry=entry,
        cache=cache,
        entry_resolver=lambda: entry.entry_id,
    )

    bucket = hass.data[DOMAIN]
    assert bucket.get("fcm_receiver") is receiver

    legacy_bucket: dict[str, Any] = {"fcm_receiver": receiver}
    receivers = _get_fcm_receivers(legacy_bucket)

    assert legacy_bucket.get("fcm_receiver") is receiver
    assert receivers == {"default": receiver}

    await _async_release(receiver, hass, entry)


@pytest.mark.asyncio
async def test_domain_provider_prefers_ready_receiver() -> None:
    hass = SimpleNamespace(data={DOMAIN: {}})
    bucket = hass.data[DOMAIN]

    offline_receiver = SimpleNamespace(is_ready=False)
    online_receiver = SimpleNamespace(is_ready=True)

    bucket["fcm_receivers"] = {
        "entry-offline": offline_receiver,
        "entry-online": online_receiver,
    }
    bucket["fcm_provider_resolvers"] = {
        "offline": lambda: "entry-offline",
        "online": lambda: "entry-online",
    }
    bucket["default_fcm_entry_id"] = "entry-offline"

    receiver = _domain_fcm_provider(hass)

    assert receiver is online_receiver
    assert bucket.get("default_fcm_entry_id") == "entry-online"


def test_domain_provider_sets_default_entry_on_selected_receiver() -> None:
    hass = SimpleNamespace(data={DOMAIN: {}})
    bucket = hass.data[DOMAIN]

    offline_receiver = _DefaultAwareReceiver(
        {"entry-offline": "offline-token"}, is_ready=False
    )
    online_receiver = _DefaultAwareReceiver(
        {"entry-online": "online-token"}, is_ready=True
    )

    bucket["fcm_receivers"] = {
        "entry-offline": offline_receiver,
        "entry-online": online_receiver,
    }
    bucket["default_fcm_entry_id"] = "entry-offline"

    receiver = _domain_fcm_provider(hass)

    assert receiver is online_receiver
    assert online_receiver.default_entry_id == "entry-online"
    assert online_receiver.get_fcm_token() == "online-token"


async def _async_release(receiver: FcmReceiverHA, hass: Any, entry: _DummyEntry) -> None:
    receiver.request_stop()
    await _async_release_shared_fcm(hass, entry)
