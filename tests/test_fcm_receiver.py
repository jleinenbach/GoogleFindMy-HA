# tests/test_fcm_receiver.py
from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.Auth.firebase_messaging.fcmregister import (
    FcmRegisterHTTPError,
)
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.exceptions import FatalRegistrationError
from tests.helpers.config_entries_stub import make_config_entry

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


class _ReadyableReceiver(FcmReceiverHA):
    """Receiver stub with overridable readiness for tests."""

    def __init__(self, *, ready: bool) -> None:
        super().__init__()
        self._ready_flag = ready

    @property
    def is_ready(self) -> bool:
        return self._ready_flag

    ready = is_ready


class _DefaultAwareReceiver(_ReadyableReceiver):
    """Receiver stub that tracks the default entry for token lookups."""

    def __init__(self, mapping: dict[str, str], *, is_ready: bool) -> None:
        super().__init__(ready=is_ready)
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

    entry_a = make_config_entry(entry_id="entry-a")
    entry_b = make_config_entry(entry_id="entry-b")

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

    entry_a = make_config_entry(entry_id="entry-a")
    entry_b = make_config_entry(entry_id="entry-b")

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
async def test_new_entry_ignores_legacy_alias_when_receivers_present() -> None:
    hass = SimpleNamespace(data={DOMAIN: {}})

    entry_a = make_config_entry(entry_id="entry-a")
    entry_b = make_config_entry(entry_id="entry-b")

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

    receiver_b = await _async_acquire_shared_fcm(
        hass,
        entry=entry_b,
        cache=cache_b,
        entry_resolver=lambda: entry_b.entry_id,
    )

    receivers = _get_fcm_receivers(bucket)
    assert receivers[entry_a.entry_id] is receiver_a
    assert receivers[entry_b.entry_id] is receiver_b
    assert receiver_a is not receiver_b
    assert bucket.get("fcm_receiver") is receiver_a

    await _async_release(receiver_a, hass, entry_a)
    await _async_release(receiver_b, hass, entry_b)


@pytest.mark.asyncio
async def test_legacy_fcm_receiver_alias_preserved() -> None:
    hass = SimpleNamespace(data={DOMAIN: {}})

    entry = make_config_entry(entry_id="entry-a")
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


def test_domain_provider_respects_explicit_entry_id() -> None:
    hass = SimpleNamespace(data={DOMAIN: {}})
    bucket = hass.data[DOMAIN]

    receiver_1 = _ReadyableReceiver(ready=True)
    receiver_2 = _ReadyableReceiver(ready=True)

    bucket["fcm_receivers"] = {"entry-1": receiver_1, "entry-2": receiver_2}
    bucket["default_fcm_entry_id"] = "entry-1"

    receiver = _domain_fcm_provider(hass, "entry-2")

    assert receiver is receiver_2


@pytest.mark.asyncio
async def test_domain_provider_prefers_ready_receiver() -> None:
    hass = SimpleNamespace(data={DOMAIN: {}})
    bucket = hass.data[DOMAIN]

    offline_receiver = _ReadyableReceiver(ready=False)
    online_receiver = _ReadyableReceiver(ready=True)

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


async def _async_release(
    receiver: FcmReceiverHA, hass: Any, entry: SimpleNamespace
) -> None:
    receiver.request_stop()
    await _async_release_shared_fcm(hass, entry)


@pytest.mark.asyncio
async def test_register_clears_latched_fatal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = FcmReceiverHA()
    entry_id = "entry-id"
    receiver._fatal_errors[entry_id] = "BadAuthentication"
    receiver._fatal_error = "BadAuthentication"

    class _DummyPc:
        async def checkin_or_register(self) -> dict[str, str]:
            return {"ok": "true"}

    receiver.pcs[entry_id] = _DummyPc()

    token_routes: list[tuple[str, set[str]]] = []
    monkeypatch.setattr(
        receiver,
        "_update_token_routing",
        lambda token, entries: token_routes.append((token, set(entries))),
    )

    persisted_tokens: list[tuple[str, str]] = []

    async def _persist(entry_arg: str, token_arg: str) -> None:
        persisted_tokens.append((entry_arg, token_arg))

    monkeypatch.setattr(receiver, "_persist_routing_token", _persist)
    monkeypatch.setattr(receiver, "get_fcm_token", lambda _entry_id=None: "token-123")

    result = await receiver._register_for_fcm_entry(entry_id)

    assert result is True
    assert entry_id not in receiver._fatal_errors
    assert receiver._fatal_error is None
    assert token_routes == [("token-123", {entry_id})]
    assert persisted_tokens == [(entry_id, "token-123")]


@pytest.mark.asyncio
async def test_register_success_skips_routing_writes_for_tombstoned_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registration attempt that finishes after the entry was torn down must
    not resurrect token routing for the tombstoned entry (AP4 teardown race).

    The routing writes RE-CREATE the per-entry state ``_purge_entry_tokens``
    just removed, so they must be skipped while the entry is tombstoned. The
    fatal/repair clears only REMOVE state, so they stay teardown-safe and still
    run, and the attempt still reports success.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-tombstoned"
    receiver._fatal_errors[entry_id] = "BadAuthentication"
    receiver._fatal_error = "BadAuthentication"
    # The synchronous teardown purged this entry while this in-flight
    # registration was still running.
    receiver._unregistered.add(entry_id)

    class _DummyPc:
        async def checkin_or_register(self) -> dict[str, str]:
            return {"ok": "true"}

    receiver.pcs[entry_id] = _DummyPc()

    token_routes: list[tuple[str, set[str]]] = []
    monkeypatch.setattr(
        receiver,
        "_update_token_routing",
        lambda token, entries: token_routes.append((token, set(entries))),
    )

    persisted_tokens: list[tuple[str, str]] = []

    async def _persist(entry_arg: str, token_arg: str) -> None:
        persisted_tokens.append((entry_arg, token_arg))

    monkeypatch.setattr(receiver, "_persist_routing_token", _persist)
    monkeypatch.setattr(receiver, "get_fcm_token", lambda _entry_id=None: "token-123")

    result = await receiver._register_for_fcm_entry(entry_id)

    assert result is True
    # Routing writes are suppressed for the tombstoned entry...
    assert token_routes == []
    assert persisted_tokens == []
    # ...but the teardown-safe clears still run.
    assert entry_id not in receiver._fatal_errors
    assert receiver._fatal_error is None


@pytest.mark.asyncio
async def test_credentials_update_clears_latched_fatal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receiver = FcmReceiverHA()
    entry_id = "entry-credentials"
    receiver._fatal_errors[entry_id] = "BadAuthentication"
    receiver._fatal_error = "BadAuthentication"

    token_routes: list[tuple[str, set[str]]] = []
    monkeypatch.setattr(
        receiver,
        "_update_token_routing",
        lambda token, entries: token_routes.append((token, set(entries))),
    )

    async def _persist(entry_arg: str, token_arg: str) -> None:
        token_routes.append((token_arg, {entry_arg}))

    async def _save(entry_arg: str) -> None:
        token_routes.append(("save", {entry_arg}))

    monkeypatch.setattr(receiver, "_persist_routing_token", _persist)
    monkeypatch.setattr(receiver, "_async_save_credentials_for_entry", _save)
    monkeypatch.setattr(receiver, "get_fcm_token", lambda _entry_id=None: "token-abc")

    receiver._on_credentials_updated_for_entry(
        entry_id, {"fcm": {"registration": {"token": "token-abc"}}}
    )

    # _dispatch_to_hass_loop tracks tasks in _active_tasks; gather them
    if receiver._active_tasks:
        await asyncio.gather(*list(receiver._active_tasks))

    assert entry_id not in receiver._fatal_errors
    assert receiver._fatal_error is None
    assert token_routes == [
        ("token-abc", {entry_id}),
        ("token-abc", {entry_id}),
        ("save", {entry_id}),
    ]


@pytest.mark.asyncio
async def test_register_raises_fatal_on_fcm_register_http_401() -> None:
    """fcm_install/fcm_register raising FcmRegisterHTTPError(401) must surface
    as FatalRegistrationError(is_auth_error=True), not as a transient runtime
    error swallowed by the generic catch.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-401"

    class _PcRaising401:
        async def checkin_or_register(self) -> dict[str, str]:
            raise FcmRegisterHTTPError(
                "fcm_install fatal status 401", status=401
            )

    receiver.pcs[entry_id] = _PcRaising401()

    with pytest.raises(FatalRegistrationError) as exc_info:
        await receiver._register_for_fcm_entry(entry_id)

    assert exc_info.value.is_auth_error is True
    assert "401" in str(exc_info.value)


@pytest.mark.asyncio
async def test_register_raises_fatal_on_fcm_register_http_404() -> None:
    """FcmRegisterHTTPError(404) must surface as FatalRegistrationError
    (is_auth_error=False) so the endpoint retry budget runs.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-404"

    class _PcRaising404:
        async def checkin_or_register(self) -> dict[str, str]:
            raise FcmRegisterHTTPError(
                "fcm_register fatal status 404", status=404
            )

    receiver.pcs[entry_id] = _PcRaising404()

    with pytest.raises(FatalRegistrationError) as exc_info:
        await receiver._register_for_fcm_entry(entry_id)

    assert exc_info.value.is_auth_error is False
    assert "404" in str(exc_info.value)


@pytest.mark.asyncio
async def test_register_keeps_transient_runtime_error_transient() -> None:
    """Plain RuntimeError (non-FcmRegisterHTTPError) must remain transient —
    must not be escalated to FatalRegistrationError.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-transient"

    class _PcRaisingPlain:
        async def checkin_or_register(self) -> dict[str, str]:
            raise RuntimeError("Registration did not yield credentials")

    receiver.pcs[entry_id] = _PcRaisingPlain()

    result = await receiver._register_for_fcm_entry(entry_id)

    assert result is False
    assert entry_id not in receiver._fatal_errors
