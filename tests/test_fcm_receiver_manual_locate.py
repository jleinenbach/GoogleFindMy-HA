# tests/test_fcm_receiver_manual_locate.py
"""Tests for manual locate registration and background decode helpers."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha as fcm_mod
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA


class DummyCache:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.data[key] = value


class DummyEntry:
    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id


class DummyCoordinator:
    def __init__(self, entry_id: str, cache: DummyCache, tracked_id: str) -> None:
        self.config_entry = DummyEntry(entry_id)
        self.cache = cache
        self._tracked_id = tracked_id

    def is_device_present(self, device_id: str) -> bool:
        return device_id == self._tracked_id

    def get_device_display_name(self, device_id: str) -> str | None:
        if device_id == self._tracked_id:
            return "Tracked Device"
        return None


def test_manual_locate_registration_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registering manual locate stores callbacks and returns cached token."""

    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    coordinator = DummyCoordinator(entry_id, cache, device_id)

    ensure_calls: list[tuple[str, Any]] = []
    start_calls: list[tuple[str, Any]] = []
    register_calls: list[str] = []

    async def fake_ensure(
        eid: str, provided_cache: Any, generation: int | None = None
    ) -> object:
        ensure_calls.append((eid, provided_cache))
        return object()

    async def fake_start(eid: str, provided_cache: Any) -> None:
        start_calls.append((eid, provided_cache))

    async def fake_register(eid: str) -> bool:
        register_calls.append(eid)
        return True

    monkeypatch.setattr(receiver, "_ensure_client_for_entry", fake_ensure)
    monkeypatch.setattr(receiver, "_start_supervisor_for_entry", fake_start)
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", fake_register)

    receiver.creds[entry_id] = {
        "fcm": {"registration": {"token": "token-123"}},
    }

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    async def _run() -> None:
        receiver.register_coordinator(coordinator)
        await asyncio.sleep(0)
        start_calls.clear()

        token = await receiver.async_register_for_location_updates(
            device_id, manual_callback
        )

        assert token == "token-123"
        assert receiver.location_update_callbacks[device_id] is manual_callback
        assert ensure_calls == [(entry_id, cache)]
        assert start_calls == [(entry_id, cache)]
        assert register_calls == []

        await receiver.async_unregister_for_location_updates(device_id)
        assert device_id not in receiver.location_update_callbacks

    asyncio.run(_run())


def test_run_callback_async_uses_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """The callback helper delegates work to ``asyncio.to_thread`` when available."""

    receiver = FcmReceiverHA()
    invoked: list[tuple[str, str]] = []
    recorded: list[tuple[object, tuple[str, str]]] = []

    async def fake_to_thread(func: Callable[[str, str], Any], /, *args: str) -> None:
        recorded.append((func, args))
        func(*args)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    def callback(canonic: str, payload_hex: str) -> None:
        invoked.append((canonic, payload_hex))

    async def _run() -> None:
        await receiver._run_callback_async(callback, "dev-1", "deadbeef")

    asyncio.run(_run())

    assert recorded == [(callback, ("dev-1", "deadbeef"))]
    assert invoked == [("dev-1", "deadbeef")]


def test_process_background_update_uses_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background decode is awaited directly and schedules a flush."""

    receiver = FcmReceiverHA()
    schedule_calls: list[tuple[str, str]] = []
    decode_calls: list[tuple[str, str]] = []

    async def decode_stub(entry_id: str, payload_hex: str) -> dict[str, Any]:
        decode_calls.append((entry_id, payload_hex))
        return {"latitude": 1.0, "payload": payload_hex, "entry_id": entry_id}

    monkeypatch.setattr(receiver, "_decode_background_location_async", decode_stub)
    monkeypatch.setattr(receiver, "_schedule_flush", schedule_calls.append)

    async def _run() -> None:
        await receiver._process_background_update(
            "entry-1", "canonic-1", "c0ffee", {"entry-1", "entry-2"}
        )

    asyncio.run(_run())

    key = ("entry-1", "canonic-1")
    assert key in receiver._pending
    assert receiver._pending[key]["latitude"] == 1.0
    assert receiver._pending_targets[key] == {"entry-1", "entry-2"}
    assert schedule_calls == [key]
    assert decode_calls == [("entry-1", "c0ffee")]


# ---------------------------------------------------------------------------
# Readiness gate: fail-closed when the FCM client never reaches STARTED
# ---------------------------------------------------------------------------


class _RunState:
    """Minimal stand-in for FcmPushClientRunState (only STARTED is compared)."""

    STARTED = "STARTED"


class _DummyPc:
    """Push client double exposing only the polled ``run_state`` attribute."""

    def __init__(self, run_state: object) -> None:
        self.run_state = run_state


def _wire_manual_locate(
    monkeypatch: pytest.MonkeyPatch,
    receiver: FcmReceiverHA,
    entry_id: str,
    cache: DummyCache,
) -> None:
    """Stub every collaborator the readiness gate calls before the poll loop."""

    async def _ensure_client(eid: str, c: Any, generation: int | None = None) -> object:
        return object()

    async def _start(eid: str, c: Any) -> None:
        return None

    async def _ensure_token(eid: str) -> str:
        return "token-123"

    async def _persist(eid: str, tok: str) -> None:
        return None

    monkeypatch.setattr(
        receiver, "_select_manual_locate_entry", lambda cid: (entry_id, cache)
    )
    monkeypatch.setattr(receiver, "_ensure_client_for_entry", _ensure_client)
    monkeypatch.setattr(receiver, "_start_supervisor_for_entry", _start)
    monkeypatch.setattr(receiver, "_ensure_token_for_entry", _ensure_token)
    monkeypatch.setattr(receiver, "_update_token_routing", lambda tok, ids: None)
    monkeypatch.setattr(receiver, "_persist_routing_token", _persist)
    monkeypatch.setattr(fcm_mod, "FcmPushClientRunState", _RunState)


@pytest.mark.asyncio
async def test_manual_locate_fails_closed_when_never_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that never reaches STARTED yields None and leaves no callback.

    Regression for the deterministic first-run locate timeout: the gate used to
    fall through to ``return token`` (fail-open), handing a token to a listener
    that was not yet connected so the caller blocked until an opaque 30s timeout.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    receiver.pcs[entry_id] = _DummyPc(run_state="CONNECTING")  # type: ignore[assignment]

    # Do not actually sleep through the readiness budget.
    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token is None
    assert device_id not in receiver.location_update_callbacks


@pytest.mark.asyncio
async def test_manual_locate_returns_token_when_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA happy path: STARTED within budget returns the token, keeps the callback."""
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    receiver.pcs[entry_id] = _DummyPc(run_state=_RunState.STARTED)  # type: ignore[assignment]

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token == "token-123"
    assert receiver.location_update_callbacks[device_id] is manual_callback


@pytest.mark.asyncio
async def test_manual_locate_reads_replacement_client_during_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supervisor restart that swaps in a STARTED client mid-poll is honored.

    Regression (Codex): the gate captured ``pc`` once before waiting.  When a
    concurrent ``_start_supervisor_for_entry`` restart discards that client and
    replaces ``self.pcs[entry_id]`` with a fresh one that reaches STARTED, the
    stale object never transitions, so fail-closed used to abort a request the
    replacement client could actually serve.  The loop must re-read the live
    entry client during the poll.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    stale = _DummyPc(run_state="CONNECTING")
    started = _DummyPc(run_state=_RunState.STARTED)
    receiver.pcs[entry_id] = stale  # type: ignore[assignment]

    # Simulate a concurrent supervisor restart swapping the entry client in
    # while the readiness gate is between polls (on the first ``await sleep``).
    swapped = {"done": False}

    async def _swap_then_no_sleep(_seconds: float) -> None:
        if not swapped["done"]:
            receiver.pcs[entry_id] = started  # type: ignore[assignment]
            swapped["done"] = True

    monkeypatch.setattr(asyncio, "sleep", _swap_then_no_sleep)

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token == "token-123"
    assert receiver.location_update_callbacks[device_id] is manual_callback


# ---------------------------------------------------------------------------
# First-locate delivery reconnect (AP1, T-A): a fresh session can reach STARTED
# yet not deliver the first push until it reconnects.  The trigger fires only
# for a *young* session that has *not yet delivered* a data message.
# ---------------------------------------------------------------------------


class _ReconnectPc:
    """Push client double exposing run_state, persistent_ids and async stop()."""

    def __init__(
        self,
        run_state: object,
        persistent_ids: list[str] | None = None,
        started_monotonic: float | None = None,
    ) -> None:
        self.run_state = run_state
        self.persistent_ids = list(persistent_ids or [])
        # STARTED-transition anchor consumed by ``_session_age_s``. Defaults to
        # "just reached STARTED" (young session) so the common case is terse;
        # the established-session test passes an old timestamp explicitly.
        self._started_monotonic = (
            time.monotonic() if started_monotonic is None else started_monotonic
        )
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        self.run_state = "STOPPED"


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_manual_locate_forces_reconnect_for_fresh_nondelivering_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Young + no delivery: force one reconnect and serve from the replacement.

    Regression for the deterministic first-locate timeout after a fresh FCM
    registration: the first client reaches STARTED but never receives the push,
    and only a reconnect (a session that subscribes after registration) heals it.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    # Young session: the double defaults to a just-now STARTED stamp, so the
    # per-client age gate (``_session_age_s``) is open (age ~0 < _CHURN_WINDOW_S).
    stale = _ReconnectPc(run_state=_RunState.STARTED, persistent_ids=[])
    fresh = _ReconnectPc(run_state=_RunState.STARTED, persistent_ids=[])
    receiver.pcs[entry_id] = stale  # type: ignore[assignment]

    nudged: list[str | None] = []
    monkeypatch.setattr(
        receiver, "nudge_retry", lambda eid=None: bool(nudged.append(eid)) or True
    )

    # The forced-reconnect wait swaps in the replacement on its first sleep,
    # mirroring the supervisor rebuild.  The first readiness wait does not sleep
    # (stale is already STARTED).
    async def _swap_then_no_sleep(_seconds: float) -> None:
        if receiver.pcs[entry_id] is stale:
            receiver.pcs[entry_id] = fresh  # type: ignore[assignment]

    monkeypatch.setattr(asyncio, "sleep", _swap_then_no_sleep)

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token == "token-123"
    assert stale.stopped is True  # the reconnect actually happened
    assert nudged == [entry_id]  # supervisor nudged to rebuild
    assert receiver.pcs[entry_id] is fresh  # replacement client is live


@pytest.mark.asyncio
async def test_manual_locate_no_reconnect_for_established_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old session (age >= window): no reconnect, token returned unchanged."""
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    # Established session: STARTED well beyond _CHURN_WINDOW_S (per-client age).
    stale = _ReconnectPc(
        run_state=_RunState.STARTED,
        persistent_ids=[],
        started_monotonic=time.monotonic() - 100.0,
    )
    receiver.pcs[entry_id] = stale  # type: ignore[assignment]

    nudged: list[str | None] = []
    monkeypatch.setattr(
        receiver, "nudge_retry", lambda eid=None: bool(nudged.append(eid)) or True
    )
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token == "token-123"
    assert stale.stopped is False
    assert nudged == []
    assert receiver.pcs[entry_id] is stale


@pytest.mark.asyncio
async def test_manual_locate_no_reconnect_when_already_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Young but already-delivering session: the sharpening suppresses reconnect.

    Protects a healthy HA multi-entry session that the aggregate age anchor might
    otherwise mis-classify as young: a non-empty ``persistent_ids`` proves the
    session already delivered a data message, so no needless reconnect fires.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    # Young (default STARTED stamp) but already delivered (persistent_ids set).
    stale = _ReconnectPc(run_state=_RunState.STARTED, persistent_ids=["pid-1"])
    receiver.pcs[entry_id] = stale  # type: ignore[assignment]

    nudged: list[str | None] = []
    monkeypatch.setattr(
        receiver, "nudge_retry", lambda eid=None: bool(nudged.append(eid)) or True
    )
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token == "token-123"
    assert stale.stopped is False
    assert nudged == []


@pytest.mark.asyncio
async def test_manual_locate_reconnect_fails_closed_when_replacement_never_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced reconnect whose replacement never reaches STARTED fails closed."""
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    # Young (default STARTED stamp) and not delivered -> the trigger fires.
    stale = _ReconnectPc(run_state=_RunState.STARTED, persistent_ids=[])
    receiver.pcs[entry_id] = stale  # type: ignore[assignment]

    monkeypatch.setattr(receiver, "nudge_retry", lambda eid=None: True)
    # No replacement ever appears; the post-reconnect wait exhausts its budget.
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token is None
    assert device_id not in receiver.location_update_callbacks
    assert stale.stopped is True  # reconnect was attempted before failing closed


@pytest.mark.asyncio
async def test_force_first_locate_reconnect_swallows_stop_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop() error on the dying client is swallowed; the rebuild still runs."""
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    monkeypatch.setattr(fcm_mod, "FcmPushClientRunState", _RunState)

    class _BoomPc(_ReconnectPc):
        async def stop(self) -> None:
            self.stopped = True
            raise RuntimeError("boom")

    stale = _BoomPc(run_state="STOPPED", persistent_ids=[])
    fresh = _ReconnectPc(run_state=_RunState.STARTED, persistent_ids=[])
    receiver.pcs[entry_id] = stale  # type: ignore[assignment]

    nudged: list[str | None] = []
    monkeypatch.setattr(
        receiver, "nudge_retry", lambda eid=None: bool(nudged.append(eid)) or True
    )

    async def _swap(_seconds: float) -> None:
        receiver.pcs[entry_id] = fresh  # type: ignore[assignment]

    monkeypatch.setattr(asyncio, "sleep", _swap)

    result = await receiver._force_first_locate_reconnect(entry_id, stale, 5.0)  # type: ignore[arg-type]

    assert result is fresh
    assert stale.stopped is True
    assert nudged == [entry_id]


@pytest.mark.asyncio
async def test_force_reconnect_excludes_lingering_started_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale client still STARTED after a failed stop() is not the replacement.

    Hardens the ``exclude_pc`` guard: if ``stop()`` raised and the stale client
    lingers in STARTED with no replacement swapped in, the post-reconnect wait
    must reject the stale instance and let the budget expire (return ``None`` =
    fail closed), rather than mistake the stale for the fresh client.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    monkeypatch.setattr(fcm_mod, "FcmPushClientRunState", _RunState)

    class _LingeringPc(_ReconnectPc):
        async def stop(self) -> None:
            # stop() fails and does NOT transition run_state: it lingers STARTED.
            self.stopped = True
            raise RuntimeError("stop failed; client lingers STARTED")

    stale = _LingeringPc(run_state=_RunState.STARTED, persistent_ids=[])
    receiver.pcs[entry_id] = stale  # type: ignore[assignment]

    monkeypatch.setattr(receiver, "nudge_retry", lambda eid=None: True)
    # No replacement is ever swapped in; the stale lingers in STARTED.
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    result = await receiver._force_first_locate_reconnect(entry_id, stale, 1.0)  # type: ignore[arg-type]

    assert result is None  # stale-but-STARTED must not be taken as the replacement
    assert stale.stopped is True


@pytest.mark.asyncio
async def test_manual_locate_reconnect_for_slow_startup_fresh_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slow-startup fresh session: churn age is measured from STARTED, not supervisor start.

    Codex #1150 follow-up: the supervisor records ``last_start_monotonic`` before
    ``pc.start()``, and the readiness gate allows a fresh MCS connection ~15-20s
    to reach STARTED.  A session that starts slowly (supervisor start long ago)
    but has only *just* reached STARTED and delivered nothing is exactly the case
    the reconnect must heal.  Anchoring the age on the client's own STARTED stamp
    fires the reconnect here; the old anchor (``last_start_monotonic``, set far in
    the past) would compute age >= _CHURN_WINDOW_S and wrongly skip it.  Setting a
    stale ``last_start_monotonic`` proves the decision no longer depends on it.
    """
    receiver = FcmReceiverHA()
    entry_id = "entry-1"
    device_id = "device-canonic"
    cache = DummyCache()
    _wire_manual_locate(monkeypatch, receiver, entry_id, cache)

    # Client only just reached STARTED (young), though the supervisor began the
    # start long ago (slow connection budget consumed before STARTED).
    stale = _ReconnectPc(run_state=_RunState.STARTED, persistent_ids=[])
    fresh = _ReconnectPc(run_state=_RunState.STARTED, persistent_ids=[])
    receiver.pcs[entry_id] = stale  # type: ignore[assignment]
    receiver.last_start_monotonic = time.monotonic() - 100.0  # stale supervisor stamp

    nudged: list[str | None] = []
    monkeypatch.setattr(
        receiver, "nudge_retry", lambda eid=None: bool(nudged.append(eid)) or True
    )

    async def _swap_then_no_sleep(_seconds: float) -> None:
        if receiver.pcs[entry_id] is stale:
            receiver.pcs[entry_id] = fresh  # type: ignore[assignment]

    monkeypatch.setattr(asyncio, "sleep", _swap_then_no_sleep)

    def manual_callback(canonic: str, payload_hex: str) -> None:
        return None

    token = await receiver.async_register_for_location_updates(
        device_id, manual_callback
    )

    assert token == "token-123"
    assert stale.stopped is True  # reconnect fired despite the stale supervisor stamp
    assert nudged == [entry_id]
    assert receiver.pcs[entry_id] is fresh
