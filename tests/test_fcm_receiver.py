# tests/test_fcm_receiver.py
from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha
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


# --------------------------------------------------------------------------
# PR B — stop-event lifecycle (AP5) + exception classification (AP6)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supervisor_restart_installs_fresh_stop_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AP5 (finding 4b): a re-setup must not inherit a leftover *set* stop event.

    ``request_stop`` sets the entry's event without installing a fresh one. The
    old ``setdefault`` in ``_start_supervisor_for_entry`` then handed that set
    event to the new supervisor, whose ``while not stop_evt.is_set()`` exited
    immediately (wedged restart). The fix installs a fresh, unset event so the
    supervisor body actually runs.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-wedge"
    stale = asyncio.Event()
    stale.set()  # leftover from a prior request_stop
    receiver._stop_evts[entry_id] = stale

    ran = asyncio.Event()

    async def _ensure(eid: str, _cache: Any) -> None:
        ran.set()
        # Terminate the loop deterministically after one body execution.
        receiver._stop_evts[eid].set()

    monkeypatch.setattr(receiver, "_ensure_client_for_entry", _ensure)
    monkeypatch.setattr(fcm_receiver_ha.asyncio, "sleep", AsyncMock())

    real_wait_for = asyncio.wait_for

    async def _instant_wait_for(fut: Any, *, timeout: Any = None) -> Any:
        # Make the supervisor's nudge-sleep return immediately.
        coro_name = getattr(getattr(fut, "cr_code", None), "co_name", "")
        if coro_name == "wait":
            if asyncio.iscoroutine(fut):
                fut.close()
            raise TimeoutError
        return await real_wait_for(fut, timeout=timeout)

    monkeypatch.setattr(fcm_receiver_ha.asyncio, "wait_for", _instant_wait_for)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await real_wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert ran.is_set()  # body executed -> a fresh, unset event was installed
    assert receiver._stop_evts[entry_id] is not stale  # stale event replaced


@pytest.mark.asyncio
async def test_register_invalidates_tokens_on_corrupt_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AP6 (finding 3a): a KeyError (corrupt creds) escalates via token invalidation."""
    receiver = FcmReceiverHA()
    entry_id = "entry-corrupt"

    class _PcKeyError:
        async def checkin_or_register(self) -> dict[str, str]:
            raise KeyError("gcm")

    receiver.pcs[entry_id] = _PcKeyError()

    invalidated: list[str] = []

    async def _inv(eid: str) -> None:
        invalidated.append(eid)

    monkeypatch.setattr(receiver, "_invalidate_fcm_tokens", _inv)

    result = await receiver._register_for_fcm_entry(entry_id)

    assert result is False
    assert invalidated == [entry_id]


@pytest.mark.asyncio
async def test_invalidate_fcm_tokens_preserves_complete_gcm_identity() -> None:
    """A complete GCM identity survives an FCM-token invalidation.

    Positive control for the corrupt-identity drop: when android_id AND
    security_token are present, only the renewable FCM parts (fcm, keys, the
    gcm token/app_id) are stripped, so ``reregister_keeping_identity()`` can
    take the fast path on the next attempt.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-complete-identity"
    receiver.creds[entry_id] = {
        "gcm": {
            # Both fields are int()-convertible (an int and a numeric string),
            # exactly what reregister_keeping_identity() can consume.
            "android_id": 1234567890123456,
            "security_token": "9876543210987654",
            "token": "gcm-tok",
            "app_id": "app-1",
        },
        "fcm": {"registration": {"token": "fcm-tok"}},
        "keys": {"private": "x"},
    }

    await receiver._invalidate_fcm_tokens(entry_id)

    creds = receiver.creds[entry_id]
    assert "fcm" not in creds
    assert "keys" not in creds
    gcm = creds["gcm"]
    assert gcm["android_id"] == 1234567890123456
    assert gcm["security_token"] == "9876543210987654"
    assert "token" not in gcm  # renewable gcm token stripped
    assert "app_id" not in gcm


@pytest.mark.asyncio
async def test_invalidate_fcm_tokens_drops_corrupt_gcm_identity() -> None:
    """A partial GCM identity is dropped to break the KeyError retry loop.

    Regression for the Codex follow-up on AP6: ``reregister_keeping_identity()``
    subscripts ``credentials["gcm"]["security_token"]`` unguarded and only falls
    back to a full register when ``gcm`` is entirely absent. A block with an
    ``android_id`` but no ``security_token`` therefore raises ``KeyError`` on
    every retry. The invalidation must drop the whole ``gcm`` block so
    ``_register_for_fcm_entry`` takes the full ``checkin_or_register()``
    handshake instead of replaying the same broken identity forever.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-corrupt-identity"
    receiver.creds[entry_id] = {
        "gcm": {"android_id": 1234567890123456},  # security_token missing
        "fcm": {"registration": {"token": "fcm-tok"}},
        "keys": {"private": "x"},
    }

    await receiver._invalidate_fcm_tokens(entry_id)

    creds = receiver.creds[entry_id]
    # Whole gcm block gone -> next _register_for_fcm_entry uses checkin_or_register.
    assert "gcm" not in creds
    assert "fcm" not in creds
    assert "keys" not in creds
    # And the predicate that drives the decision is honest about both shapes:
    # a missing field is incomplete; both int()-convertible fields are complete.
    assert FcmReceiverHA._gcm_identity_is_complete({"android_id": 1}) is False
    assert (
        FcmReceiverHA._gcm_identity_is_complete(
            {"android_id": 1, "security_token": "2"}
        )
        is True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_security_token",
    ["sec-tok", ["1", "2"], {"x": 1}, True, float("inf"), float("-inf")],
    ids=["non-numeric-str", "list", "dict", "bool", "inf", "-inf"],
)
async def test_invalidate_fcm_tokens_drops_malformed_gcm_identity(
    bad_security_token: object,
) -> None:
    """A truthy-but-non-numeric GCM identity is dropped, not preserved.

    Regression for the Codex follow-up: ``reregister_keeping_identity()`` runs
    ``int(security_token)`` only after a truthiness gate, so a truthy value that
    is not ``int()``-convertible passes the gate but raises on every retry —
    ``ValueError``/``TypeError`` for a non-numeric string, list, dict or bool,
    and ``OverflowError`` for a float infinity (JSON ``1e309`` / ``Infinity``).
    The earlier predicate preserved such a block and the supervisor looped
    forever (or, for infinity, ``_invalidate_fcm_tokens`` itself raised and
    stopped the supervisor). The hardened predicate now drops the whole ``gcm``
    block so the next attempt does a full ``checkin_or_register()``.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-malformed-identity"
    receiver.creds[entry_id] = {
        "gcm": {
            "android_id": 1234567890123456,
            "security_token": bad_security_token,
        },
        "fcm": {"registration": {"token": "fcm-tok"}},
        "keys": {"private": "x"},
    }

    await receiver._invalidate_fcm_tokens(entry_id)

    creds = receiver.creds[entry_id]
    # Malformed identity cannot be re-registered -> whole block must be dropped.
    assert "gcm" not in creds
    assert "fcm" not in creds
    assert "keys" not in creds
    # The predicate is the single source of that decision.
    assert (
        FcmReceiverHA._gcm_identity_is_complete(
            {"android_id": 1234567890123456, "security_token": bad_security_token}
        )
        is False
    )


@pytest.mark.asyncio
async def test_register_unexpected_error_is_non_fatal_without_invalidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AP6 (finding 3a): an unexpected error stays non-fatal and does NOT invalidate."""
    receiver = FcmReceiverHA()
    entry_id = "entry-weird"

    class _WeirdError(Exception):
        pass

    class _PcWeird:
        async def checkin_or_register(self) -> dict[str, str]:
            raise _WeirdError("boom")

    receiver.pcs[entry_id] = _PcWeird()

    invalidated: list[str] = []

    async def _inv(eid: str) -> None:
        invalidated.append(eid)

    monkeypatch.setattr(receiver, "_invalidate_fcm_tokens", _inv)

    result = await receiver._register_for_fcm_entry(entry_id)

    assert result is False
    assert invalidated == []  # unexpected path must not invalidate


@pytest.mark.asyncio
async def test_classify_registration_exception_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AP6 (finding 3a): the classification helper routes each error class."""
    receiver = FcmReceiverHA()
    entry_id = "entry-classify"

    calls: list[str] = []

    async def _inv(eid: str) -> None:
        calls.append(eid)

    monkeypatch.setattr(receiver, "_invalidate_fcm_tokens", _inv)

    # Transient: no invalidation.
    await receiver._classify_registration_exception(entry_id, RuntimeError("x"))
    await receiver._classify_registration_exception(entry_id, TimeoutError())
    assert calls == []

    # Structural corruption: invalidate each time.
    await receiver._classify_registration_exception(entry_id, KeyError("gcm"))
    await receiver._classify_registration_exception(entry_id, ValueError("bad"))
    await receiver._classify_registration_exception(entry_id, TypeError("bad"))
    assert calls == [entry_id, entry_id, entry_id]

    # Unexpected: no invalidation.
    class _WeirdError(Exception):
        pass

    await receiver._classify_registration_exception(entry_id, _WeirdError())
    assert calls == [entry_id, entry_id, entry_id]
