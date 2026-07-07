# tests/test_fcm_receiver_liveness_watchdog.py
"""Data-starvation liveness watchdog (re-arming FCM zombie self-heal).

Covers the AP1-AP4 additions to ``FcmReceiverHA``:

* AP1  the separate ``_entry_last_data_delivery_monotonic`` clock and the
  ``_stamp_data_delivery`` helper (stamped only on a real locate data delivery),
* AP2  the pure ``_is_data_starved`` predicate,
* AP2.5 the entry-scoped ``_entry_last_locate_sent_monotonic`` signal,
* AP3  the re-arming reconnect (``_reconnect_for_starvation`` wrapper +
  ``_maybe_reconnect_starved`` scheduler, backoff/cap/max, one-in-flight), and
* AP4  the throttled supervisor wiring + ``_purge_entry_tokens`` reset/cancel.

The tests exercise the real predicate/scheduler/stamp/reset logic against a
light push-client double, never ``asyncio.run()`` (tests/AGENTS.md). Coroutine
tests are ``async def`` and awaited directly under pytest's managed loop.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha as fcm_mod
from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.const import (
    FCM_DATA_STARVATION_S,
    FCM_ZOMBIE_MAX_RECONNECTS,
    FCM_ZOMBIE_RECONNECT_BACKOFF_BASE_S,
    FCM_ZOMBIE_RECONNECT_BACKOFF_CAP_S,
)


class _RunState:
    """Minimal stand-in for FcmPushClientRunState (only STARTED is compared)."""

    STARTED = "STARTED"


class _WatchdogPc:
    """Push-client double: run_state, STARTED-age anchor, and async stop()."""

    def __init__(
        self,
        run_state: object = _RunState.STARTED,
        *,
        started_monotonic: float | None = None,
        age_s: float = 100.0,
    ) -> None:
        self.run_state = run_state
        # Default: established session well past _CHURN_WINDOW_S (age ~100s).
        self._started_monotonic = (
            (time.monotonic() - age_s)
            if started_monotonic is None
            else started_monotonic
        )
        self.persistent_ids: list[str] = []
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        self.run_state = "STOPPED"


class _DummyHass:
    """Lightweight hass stub whose ``async_create_task`` uses the live loop."""

    def __init__(self) -> None:
        self.created: list[str | None] = []

    def async_create_task(
        self, coro: Any, *, name: str | None = None
    ) -> asyncio.Task[Any]:
        self.created.append(name)
        return asyncio.get_event_loop().create_task(coro, name=name)


class _FakeTask:
    """Minimal task stand-in: reports not-done and swallows the done-callback."""

    def done(self) -> bool:
        return False

    def add_done_callback(self, _cb: Any) -> None:
        return None


class _RecordingHass:
    """Hass stub that records scheduling but never runs the coroutine.

    Used by the synchronous scheduler tests (no running loop): it closes the
    passed coroutine to avoid "coroutine was never awaited" warnings and returns
    a ``_FakeTask`` so no real asyncio task leaks between tests.
    """

    def __init__(self) -> None:
        self.created: list[str | None] = []

    def async_create_task(self, coro: Any, *, name: str | None = None) -> _FakeTask:
        self.created.append(name)
        coro.close()
        return _FakeTask()


def _make_receiver(monkeypatch: pytest.MonkeyPatch) -> FcmReceiverHA:
    """Return a receiver with the run-state enum wired to the local stub."""
    receiver = FcmReceiverHA()
    monkeypatch.setattr(fcm_mod, "FcmPushClientRunState", _RunState)
    return receiver


def _arm_starved(
    receiver: FcmReceiverHA,
    entry_id: str,
    now: float,
    *,
    pc: _WatchdogPc | None = None,
) -> _WatchdogPc:
    """Set up the canonical starved-zombie state at monotonic time *now*.

    Fixed offsets (T0/T1/T3 oracle): data clock = now - (STARVATION + 1),
    activity clock = now - 1 (heartbeat fresh), a locate sent after the last
    delivery, and an established STARTED session (age >= churn).
    """
    live_pc = pc if pc is not None else _WatchdogPc(age_s=100.0)
    receiver.pcs[entry_id] = live_pc  # type: ignore[assignment]
    receiver._entry_last_data_delivery_monotonic[entry_id] = now - (
        FCM_DATA_STARVATION_S + 1.0
    )
    receiver._entry_last_activity_monotonic[entry_id] = now - 1.0
    receiver._entry_last_locate_sent_monotonic[entry_id] = now - 0.5
    return live_pc


# ---------------------------------------------------------------------------
# T0 -- Anti-zombie target proof (BLOCKING). Three asserted mutant vectors.
# ---------------------------------------------------------------------------


def test_t0a_watchdog_disabled_never_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T0a: a disabled watchdog (scheduler no-op) schedules 0 reconnects -> red.

    The mutant disables ``_maybe_reconnect_starved``; the positive path below
    proves the real code schedules exactly one reconnect for the same setup.
    """
    receiver = _make_receiver(monkeypatch)
    hass = _RecordingHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)

    # Mutant: watchdog disabled.
    monkeypatch.setattr(receiver, "_maybe_reconnect_starved", lambda eid, n: None)
    receiver._maybe_reconnect_starved(entry_id, now)
    assert hass.created == []  # disabled -> no reconnect (asserted mutant)

    # Positive control (real code) schedules exactly one.
    real = FcmReceiverHA._maybe_reconnect_starved
    real(receiver, entry_id, now)
    assert hass.created == [f"fcm_zombie_reconnect_{entry_id}"]


def test_t0b_predicate_on_activity_clock_never_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T0b: keying starvation on the activity clock (K4) never fires -> red.

    The activity clock is fresh (heartbeat alive), so a predicate that consults
    it instead of the data clock returns False and no reconnect is scheduled.
    """
    receiver = _make_receiver(monkeypatch)
    hass = _RecordingHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)

    def _mutant_activity_predicate(eid: str, n: float) -> bool:
        last_activity = receiver._entry_last_activity_monotonic.get(eid)
        if last_activity is None:
            return False
        return n - last_activity >= FCM_DATA_STARVATION_S

    monkeypatch.setattr(receiver, "_is_data_starved", _mutant_activity_predicate)
    receiver._maybe_reconnect_starved(entry_id, now)
    assert hass.created == []  # activity-clock predicate stays False -> no heal

    # The real data-clock predicate DOES classify this as starved.
    assert FcmReceiverHA._is_data_starved(receiver, entry_id, now) is True


def test_t0c_starvation_threshold_infinite_never_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T0c: an unreachable starvation threshold (inf) never fires -> red."""
    receiver = _make_receiver(monkeypatch)
    hass = _RecordingHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)

    monkeypatch.setattr(fcm_mod, "FCM_DATA_STARVATION_S", float("inf"))
    assert receiver._is_data_starved(entry_id, now) is False
    receiver._maybe_reconnect_starved(entry_id, now)
    assert hass.created == []


# ---------------------------------------------------------------------------
# T1 -- detected + healed + reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t1_zombie_detected_healed_and_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A starved zombie is detected, one reconnect fires, delivery resets state."""
    receiver = _make_receiver(monkeypatch)
    hass = _DummyHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    stale = _arm_starved(receiver, entry_id, now)
    fresh = _WatchdogPc(age_s=0.0)

    monkeypatch.setattr(receiver, "nudge_retry", lambda eid=None: True)

    async def _swap(_seconds: float) -> None:
        if receiver.pcs[entry_id] is stale:
            receiver.pcs[entry_id] = fresh  # type: ignore[assignment]

    monkeypatch.setattr(asyncio, "sleep", _swap)

    receiver._maybe_reconnect_starved(entry_id, now)
    assert receiver._zombie_reconnect_attempts[entry_id] == 1
    task = receiver._zombie_reconnect_tasks[entry_id]
    await task

    assert stale.stopped is True  # the reconnect actually tore down the zombie
    assert receiver.pcs[entry_id] is fresh

    # A real data delivery re-arms: clock advances, backoff/count cleared.
    receiver._stamp_data_delivery({entry_id})
    assert entry_id in receiver._entry_last_data_delivery_monotonic
    assert entry_id not in receiver._zombie_reconnect_attempts
    assert entry_id not in receiver._zombie_next_allowed_reconnect_monotonic


# ---------------------------------------------------------------------------
# T2 -- healthy session -> no reconnect
# ---------------------------------------------------------------------------


def test_t2_healthy_session_no_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh data clock -> predicate False (mutation-inverting it fires -> red)."""
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)
    # Override: data delivered just now -> healthy.
    receiver._entry_last_data_delivery_monotonic[entry_id] = now - 1.0
    receiver._entry_last_locate_sent_monotonic[entry_id] = now - 0.5

    assert receiver._is_data_starved(entry_id, now) is False

    # Mutation counter-check: a stale data clock would flip it to True.
    receiver._entry_last_data_delivery_monotonic[entry_id] = now - (
        FCM_DATA_STARVATION_S + 1.0
    )
    assert receiver._is_data_starved(entry_id, now) is True


# ---------------------------------------------------------------------------
# T3 -- core bug regression: heartbeats do NOT advance the data clock
# ---------------------------------------------------------------------------


def test_t3_heartbeat_does_not_advance_data_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare heartbeat advances the activity clock but not the data clock.

    ``_stamp_data_delivery`` is the ONLY writer of the data clock; the activity
    clock (K4) is advanced elsewhere by heartbeats. Simulate a heartbeat by
    refreshing only the activity clock: the predicate must stay True.
    """
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)

    # "Heartbeat" tick: only the activity clock moves forward.
    receiver._entry_last_activity_monotonic[entry_id] = now
    assert receiver._is_data_starved(entry_id, now) is True

    # Mutation counter-check: wiring the data clock to the heartbeat clears it.
    receiver._entry_last_data_delivery_monotonic[entry_id] = now
    assert receiver._is_data_starved(entry_id, now) is False


# ---------------------------------------------------------------------------
# T4 -- backoff growth + cap + max ceiling + reset
# ---------------------------------------------------------------------------


def test_t4_backoff_grows_caps_and_hits_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated starvation grows backoff up to the cap and stops at MAX; reset works."""
    receiver = _make_receiver(monkeypatch)
    hass = _RecordingHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"

    # Never actually reconnect; keep the entry pinned starved so we can drive the
    # scheduler through every backoff window deterministically.
    async def _noop_reconnect(eid: str, max_wait_s: float) -> None:
        return None

    monkeypatch.setattr(receiver, "_reconnect_for_starvation", _noop_reconnect)

    windows: list[float] = []
    now = 1000.0
    _arm_starved(receiver, entry_id, now)
    # Re-pin data/activity/locate relative to the synthetic clock each step so
    # the predicate stays True as ``now`` advances.
    for _ in range(FCM_ZOMBIE_MAX_RECONNECTS + 2):
        receiver._entry_last_data_delivery_monotonic[entry_id] = now - (
            FCM_DATA_STARVATION_S + 1.0
        )
        receiver._entry_last_activity_monotonic[entry_id] = now - 1.0
        receiver._entry_last_locate_sent_monotonic[entry_id] = now - 0.5
        # Clear any completed task so the one-in-flight guard does not block.
        receiver._zombie_reconnect_tasks.pop(entry_id, None)
        before = receiver._zombie_reconnect_attempts.get(entry_id, 0)
        receiver._maybe_reconnect_starved(entry_id, now)
        after = receiver._zombie_reconnect_attempts.get(entry_id, 0)
        if after > before:
            windows.append(
                receiver._zombie_next_allowed_reconnect_monotonic[entry_id] - now
            )
            # Advance past the just-planned window for the next iteration.
            now = receiver._zombie_next_allowed_reconnect_monotonic[entry_id]
        else:
            now += 1.0

    # Exactly MAX reconnects scheduled, then the hard ceiling stops it.
    assert receiver._zombie_reconnect_attempts[entry_id] == FCM_ZOMBIE_MAX_RECONNECTS
    assert len(windows) == FCM_ZOMBIE_MAX_RECONNECTS
    # Exponential growth from BASE, doubling, capped.
    expected = [
        min(
            FCM_ZOMBIE_RECONNECT_BACKOFF_BASE_S * (2**i),
            FCM_ZOMBIE_RECONNECT_BACKOFF_CAP_S,
        )
        for i in range(FCM_ZOMBIE_MAX_RECONNECTS)
    ]
    assert windows == expected
    assert windows[-1] == FCM_ZOMBIE_RECONNECT_BACKOFF_CAP_S  # cap reached

    # First real delivery resets count + backoff (re-arm).
    receiver._stamp_data_delivery({entry_id})
    assert entry_id not in receiver._zombie_reconnect_attempts
    assert entry_id not in receiver._zombie_next_allowed_reconnect_monotonic


# ---------------------------------------------------------------------------
# T5 -- one-in-flight guard + shared serialization lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t5_one_in_flight_and_shared_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No second reconnect while one is in flight; watchdog shares the locate lock."""
    receiver = _make_receiver(monkeypatch)
    hass = _DummyHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    stale = _arm_starved(receiver, entry_id, now)

    entered = asyncio.Event()
    release = asyncio.Event()

    monkeypatch.setattr(receiver, "nudge_retry", lambda eid=None: True)

    async def _blocking_stop() -> None:
        stale.stopped = True
        entered.set()
        await release.wait()

    monkeypatch.setattr(stale, "stop", _blocking_stop)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    receiver._maybe_reconnect_starved(entry_id, now)
    await entered.wait()  # first reconnect task is parked mid-stop().

    # One-in-flight guard: a second scheduling attempt does not create a task.
    created_before = len(hass.created)
    receiver._maybe_reconnect_starved(entry_id, now + 10_000.0)
    assert len(hass.created) == created_before  # no second task scheduled

    # The shared first-locate lock is held by the watchdog reconnect.
    assert receiver._get_first_locate_lock(entry_id).locked() is True

    release.set()
    await receiver._zombie_reconnect_tasks[entry_id]
    assert receiver._get_first_locate_lock(entry_id).locked() is False


# ---------------------------------------------------------------------------
# T6 -- empty account (no locate) -> no false positive
# ---------------------------------------------------------------------------


def test_t6_empty_account_no_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """No locate ever sent -> not starved even with an old/absent data clock."""
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()
    receiver.pcs[entry_id] = _WatchdogPc(age_s=100.0)  # type: ignore[assignment]
    receiver._entry_last_activity_monotonic[entry_id] = now - 1.0
    # No data delivery, no locate sent.
    assert receiver._is_data_starved(entry_id, now) is False

    # Even an ancient data clock without a locate stays non-starved.
    receiver._entry_last_data_delivery_monotonic[entry_id] = now - (
        FCM_DATA_STARVATION_S + 1.0
    )
    assert receiver._is_data_starved(entry_id, now) is False

    # A stale locate delivered *before* the last delivery also does not qualify
    # (no locate since the last delivery).
    receiver._entry_last_data_delivery_monotonic[entry_id] = now - 10.0
    receiver._entry_last_locate_sent_monotonic[entry_id] = now - 20.0
    assert receiver._is_data_starved(entry_id, now) is False


# ---------------------------------------------------------------------------
# T7 -- teardown reset + in-flight task cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t7_purge_resets_state_and_cancels_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_purge_entry_tokens`` clears all watchdog state and cancels the task."""
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)
    receiver._zombie_reconnect_attempts[entry_id] = 2
    receiver._zombie_next_allowed_reconnect_monotonic[entry_id] = now + 60.0
    receiver._zombie_next_eval_monotonic[entry_id] = now + 30.0

    started = asyncio.Event()

    async def _long_reconnect() -> None:
        started.set()
        await asyncio.Event().wait()  # never completes on its own

    task = asyncio.get_event_loop().create_task(_long_reconnect())
    receiver._zombie_reconnect_tasks[entry_id] = task  # type: ignore[assignment]
    await started.wait()

    receiver._purge_entry_tokens(entry_id)

    assert entry_id not in receiver._entry_last_data_delivery_monotonic
    assert entry_id not in receiver._entry_last_locate_sent_monotonic
    assert entry_id not in receiver._zombie_reconnect_attempts
    assert entry_id not in receiver._zombie_next_allowed_reconnect_monotonic
    assert entry_id not in receiver._zombie_next_eval_monotonic
    assert entry_id not in receiver._zombie_reconnect_tasks
    assert task.cancelled() or task.cancelling()  # cancellation requested
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Re-registration path (button-triggered) also cancels the in-flight zombie
# reconnect task -- mirror of T7 for ``async_reregister_fcm`` (step 4b).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reregister_cancels_zombie_reconnect_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``async_reregister_fcm`` cancels the in-flight watchdog reconnect task.

    Step 2 cancels the supervisor and step 4 stops the client, but the zombie
    reconnect runs in a SEPARATE task; left alive it would race step 5's
    supervisor restart and build a second live ``FcmPushClient`` for the same
    entry (the two-instance subtype-mismatch / InvalidTag symptom). This proves
    the step-4b cancel closes that race, mirroring ``_purge_entry_tokens``.
    """
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()

    # Guard 2 (entry known to receiver) via ``creds`` and NOT ``pcs``, so step 4's
    # ``pcs.pop`` is a no-op and no real ``pc.stop()`` plumbing is required.
    receiver.creds[entry_id] = {"gcm": {"app_id": "APPID"}}  # type: ignore[assignment]
    receiver._zombie_reconnect_attempts[entry_id] = 2
    receiver._zombie_next_allowed_reconnect_monotonic[entry_id] = now + 60.0
    receiver._zombie_next_eval_monotonic[entry_id] = now + 30.0

    # Neutralize the surrounding steps so only the 4b cancel is under test.
    started_supervisor: list[str] = []

    async def _noop_invalidate(eid: str) -> None:
        return None

    async def _noop_start(eid: str, cache: object) -> None:
        started_supervisor.append(eid)

    monkeypatch.setattr(receiver, "_invalidate_fcm_tokens", _noop_invalidate)
    monkeypatch.setattr(receiver, "_start_supervisor_for_entry", _noop_start)

    started = asyncio.Event()

    async def _long_reconnect() -> None:
        started.set()
        await asyncio.Event().wait()  # never completes on its own

    task = asyncio.get_event_loop().create_task(_long_reconnect())
    receiver._zombie_reconnect_tasks[entry_id] = task  # type: ignore[assignment]
    await started.wait()

    result = await receiver.async_reregister_fcm(entry_id)

    assert result is True
    assert started_supervisor == [entry_id]  # step 5 ran (supervisor restarted)
    assert entry_id not in receiver._zombie_reconnect_tasks
    assert entry_id not in receiver._zombie_reconnect_attempts
    assert entry_id not in receiver._zombie_next_allowed_reconnect_monotonic
    assert entry_id not in receiver._zombie_next_eval_monotonic
    assert task.cancelled() or task.cancelling()  # cancellation requested
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# T8 -- deadlock regression: reconnect is scheduled, never inline-awaited
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t8_reconnect_is_scheduled_not_inline_awaited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler returns immediately (task scheduled), never blocking on it.

    Mutation counter-check A: an inline ``await`` on the reconnect would block the
    caller until the replacement is built -- proven here by a reconnect that
    never completes; the scheduler must still return promptly with a pending
    task. Mutation counter-check B: routing the watchdog through
    ``_reconnect_for_first_locate`` performs NO reconnect on an aged session
    (its reuse gate short-circuits), so the zombie is never torn down.
    """
    receiver = _make_receiver(monkeypatch)
    hass = _DummyHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    stale = _arm_starved(receiver, entry_id, now)

    reconnect_ran = asyncio.Event()

    async def _never_finishing(eid: str, max_wait_s: float) -> None:
        reconnect_ran.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(receiver, "_reconnect_for_starvation", _never_finishing)

    # Scheduler returns synchronously even though the reconnect never finishes.
    receiver._maybe_reconnect_starved(entry_id, now)
    task = receiver._zombie_reconnect_tasks[entry_id]
    assert not task.done()  # scheduled, not awaited inline
    await reconnect_ran.wait()  # the task did start on the loop
    assert not task.done()  # still running; caller was never blocked
    task.cancel()

    # Gegen-probe B: the reuse-gated first-locate reconnect does nothing for an
    # aged (age >= churn) zombie -- proving the dedicated wrapper is required.
    monkeypatch.setattr(receiver, "nudge_retry", lambda eid=None: True)

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    stale.stopped = False
    result = await receiver._reconnect_for_first_locate(entry_id, 1.0)
    assert result is stale  # returned as-is, NOT reconnected
    assert stale.stopped is False  # aged zombie was never torn down


# ---------------------------------------------------------------------------
# T9 -- target_entries stamp (multi-entry fan-out)
# ---------------------------------------------------------------------------


def test_t9_stamp_all_target_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token-routed delivery stamps EVERY target entry, not just one."""
    receiver = _make_receiver(monkeypatch)
    before = time.monotonic()
    receiver._stamp_data_delivery({"entry-1", "entry-2"})
    after = time.monotonic()

    for eid in ("entry-1", "entry-2"):
        stamped = receiver._entry_last_data_delivery_monotonic[eid]
        assert before <= stamped <= after

    # Mutation counter-check: stamping only ``entry_id`` would leave a co-routed
    # multi-account entry unstamped and thus falsely starving.
    receiver._entry_last_data_delivery_monotonic.clear()
    receiver._stamp_data_delivery({"entry-1"})  # single-entry routing
    assert "entry-1" in receiver._entry_last_data_delivery_monotonic
    assert "entry-2" not in receiver._entry_last_data_delivery_monotonic

    # A ``None`` (broadcast/unknown) routing set stamps nothing (conservative).
    receiver._entry_last_data_delivery_monotonic.clear()
    receiver._stamp_data_delivery(None)
    assert receiver._entry_last_data_delivery_monotonic == {}


# ---------------------------------------------------------------------------
# T10-T13 -- _is_data_starved false-positive guards (storm safety, AP2)
# Each guard rules out a class of non-zombie so the re-arming reconnect never
# fires on a healthy/young/dead/empty session. Every vector pairs the guarded
# verdict with a mutation counter-check that flips it.
# ---------------------------------------------------------------------------


def test_t10_young_session_not_starved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session younger than the churn window is never starved (age guard)."""
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()
    # Fully armed for starvation, but the session is brand new (age < churn 15s).
    _arm_starved(receiver, entry_id, now, pc=_WatchdogPc(age_s=5.0))
    assert receiver._is_data_starved(entry_id, now) is False

    # Mutation counter-check: an established session (age past churn) fires.
    receiver.pcs[entry_id] = _WatchdogPc(age_s=100.0)  # type: ignore[assignment]
    assert receiver._is_data_starved(entry_id, now) is True


def test_t11_missing_activity_clock_not_starved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No activity clock recorded -> not starved (heartbeat health unknown)."""
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)
    del receiver._entry_last_activity_monotonic[entry_id]
    assert receiver._is_data_starved(entry_id, now) is False

    # Mutation counter-check: a fresh activity clock restores the starved verdict.
    receiver._entry_last_activity_monotonic[entry_id] = now - 1.0
    assert receiver._is_data_starved(entry_id, now) is True


def test_t12_dead_heartbeat_not_starved(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale activity clock is a dead socket (idle-reset's job), not a zombie."""
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)
    # Heartbeat older than the freshness window (FCM_IDLE_RESET_AFTER_S = 90s).
    receiver._entry_last_activity_monotonic[entry_id] = now - (
        receiver._activity_stale_after_s + 1.0
    )
    assert receiver._is_data_starved(entry_id, now) is False

    # Mutation counter-check: a fresh heartbeat is a live-but-silent zombie.
    receiver._entry_last_activity_monotonic[entry_id] = now - 1.0
    assert receiver._is_data_starved(entry_id, now) is True


def test_t13_never_delivered_with_locate_is_starved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never delivered, but an old locate is pending -> starved (no data clock)."""
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)
    # No data delivery ever; the locate is older than the starvation threshold.
    del receiver._entry_last_data_delivery_monotonic[entry_id]
    receiver._entry_last_locate_sent_monotonic[entry_id] = now - (
        FCM_DATA_STARVATION_S + 1.0
    )
    assert receiver._is_data_starved(entry_id, now) is True

    # Mutation counter-check: a recent locate (within threshold) is not yet starved.
    receiver._entry_last_locate_sent_monotonic[entry_id] = now - 1.0
    assert receiver._is_data_starved(entry_id, now) is False


# ---------------------------------------------------------------------------
# T14 -- reconnect wrapper aborts when the client vanished under the lock (AP3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t14_reconnect_wrapper_no_client_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper returns without reconnecting when no live client is present."""
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    calls: list[tuple[str, Any, float]] = []

    async def _record(eid: str, pc: Any, wait: float) -> None:
        calls.append((eid, pc, wait))

    monkeypatch.setattr(receiver, "_force_first_locate_reconnect", _record)

    # No pc registered: the supervisor tore it down before the lock was acquired.
    await receiver._reconnect_for_starvation(entry_id, 30.0)
    assert calls == []

    # Mutation counter-check: a live STARTED client reconnects unconditionally.
    receiver.pcs[entry_id] = _WatchdogPc(age_s=100.0)  # type: ignore[assignment]
    await receiver._reconnect_for_starvation(entry_id, 30.0)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# T15-T16 -- scheduler early-exit guards (AP3): no hass, open backoff window
# ---------------------------------------------------------------------------


def test_t15_scheduler_without_hass_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scheduler is a safe no-op (no crash, no attempt) when hass is absent."""
    receiver = _make_receiver(monkeypatch)
    receiver._hass = None  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)
    receiver._maybe_reconnect_starved(entry_id, now)  # must not raise
    assert entry_id not in receiver._zombie_reconnect_attempts

    # Mutation counter-check: with a hass the same setup schedules one reconnect.
    hass = _RecordingHass()
    receiver._hass = hass  # type: ignore[assignment]
    receiver._maybe_reconnect_starved(entry_id, now)
    assert len(hass.created) == 1
    assert receiver._zombie_reconnect_attempts[entry_id] == 1


def test_t16_scheduler_inside_backoff_window_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside an open backoff window the scheduler defers (no new reconnect)."""
    receiver = _make_receiver(monkeypatch)
    hass = _RecordingHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)
    # Backoff window still open: the next allowed reconnect is in the future.
    receiver._zombie_next_allowed_reconnect_monotonic[entry_id] = now + 60.0
    receiver._maybe_reconnect_starved(entry_id, now)
    assert hass.created == []

    # Mutation counter-check: once the window has elapsed the reconnect schedules.
    receiver._zombie_next_allowed_reconnect_monotonic[entry_id] = now - 1.0
    receiver._maybe_reconnect_starved(entry_id, now)
    assert len(hass.created) == 1


# ---------------------------------------------------------------------------
# T17 -- delivery integration: the cb-hit handler branch stamps the data clock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t17_delivery_stamps_data_clock_via_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real cb-hit delivery through the handler stamps the data clock.

    Drives ``_handle_notification_async`` straight to the ``if cb:`` branch with
    controlled parsing/routing doubles; the real ``_stamp_data_delivery`` call
    at the delivery site must advance the routed entry's data clock. A mutation
    dropping that call would leave the clock unstamped -> this test goes red.
    """
    receiver = _make_receiver(monkeypatch)
    entry_id = "entry-1"
    canonic_id = "canonic-1"
    cb_calls: list[str] = []

    async def _cb(cid: str, hex_str: str) -> None:
        cb_calls.append(cid)

    monkeypatch.setattr(receiver, "_extract_hex_payload", lambda payload: "deadbeef")

    async def _canonic(_hex: str) -> str:
        return canonic_id

    monkeypatch.setattr(receiver, "_extract_canonic_id_async", _canonic)
    monkeypatch.setattr(receiver, "_extract_push_token", lambda envelope: None)
    monkeypatch.setattr(
        receiver, "_route_target_entries", lambda e, c, t: ({entry_id}, "test")
    )
    monkeypatch.setattr(receiver, "_coordinators_for_entries", lambda entries: [])
    monkeypatch.setattr(receiver, "_log_push_received", lambda *a, **k: None)

    async def _run_cb(cb: Any, cid: str, hex_str: str) -> None:
        await cb(cid, hex_str)

    monkeypatch.setattr(receiver, "_run_callback_async", _run_cb)
    receiver.location_update_callbacks[canonic_id] = _cb  # type: ignore[assignment]

    before = time.monotonic()
    await receiver._handle_notification_async(entry_id, {"foo": "bar"})
    after = time.monotonic()

    # The delivery-site stamp ran: the routed entry's data clock is fresh.
    stamped = receiver._entry_last_data_delivery_monotonic[entry_id]
    assert before <= stamped <= after
    # The cb-hit branch really executed (proves we reached the stamp call site).
    assert cb_calls == [canonic_id]


# ---------------------------------------------------------------------------
# T18 -- F1 regression: the watchdog bounds the reconnect's wait-for-STARTED
# to the conservative readiness budget (~22s), never FCM_DATA_STARVATION_S.
# ---------------------------------------------------------------------------


def test_t18_watchdog_wait_budget_is_ready_budget_not_starvation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scheduler passes ``_max_ready_wait_s()`` (~22s) to the reconnect.

    ``_reconnect_for_starvation`` holds the shared per-entry first-locate lock
    while awaiting STARTED; binding the wait to FCM_DATA_STARVATION_S (900s)
    would starve a concurrent manual locate on the same entry for up to 15
    minutes (Codex F1). Mutation guard: reverting the scheduler to pass
    ``float(FCM_DATA_STARVATION_S)`` makes ``recorded[0] == 900.0`` -> red.
    """
    receiver = _make_receiver(monkeypatch)
    hass = _RecordingHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)

    recorded: list[float] = []

    async def _noop() -> None:
        return None

    def _capture(eid: str, max_wait_s: float) -> Any:
        # Sync spy: record the budget at coroutine-creation time. The async
        # body never runs (``_RecordingHass`` closes the coroutine), so the
        # value must be captured here, not inside an awaited body.
        recorded.append(max_wait_s)
        return _noop()

    monkeypatch.setattr(receiver, "_reconnect_for_starvation", _capture)

    receiver._maybe_reconnect_starved(entry_id, now)

    # Exactly one reconnect scheduled, carrying the bounded readiness budget.
    assert hass.created == [f"fcm_zombie_reconnect_{entry_id}"]
    assert recorded == [fcm_mod._max_ready_wait_s()]
    # The budget is the conservative ~22s, strictly below the 900s starvation
    # threshold whose lock-hold would starve a concurrent manual locate.
    assert recorded[0] == pytest.approx(22.0)
    assert recorded[0] < float(FCM_DATA_STARVATION_S)


def test_t19_scheduling_failure_during_shutdown_spends_no_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown-time ``async_create_task`` failure leaves scheduler state intact.

    During HA shutdown the event loop is closing and ``async_create_task`` raises
    ``RuntimeError`` (Codex F3). The scheduler must swallow it AND leave the
    attempt counter, backoff window and task registry untouched, so the tick does
    not "spend" a reconnect attempt with no task ever running (which would drift
    the entry toward ``FCM_ZOMBIE_MAX_RECONNECTS`` while never reconnecting).
    Mutation guards: (a) dropping the ``try/except`` lets the ``RuntimeError``
    propagate -> red; (b) committing the counter before task creation leaves
    ``entry_id in _zombie_reconnect_attempts`` -> red.
    """
    receiver = _make_receiver(monkeypatch)

    class _ShutdownHass:
        """Stub whose ``async_create_task`` fails as during a closing loop."""

        def __init__(self) -> None:
            self.created: list[str | None] = []

        def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
            self.created.append(name)
            coro.close()  # avoid "coroutine was never awaited" warning
            raise RuntimeError("Event loop is closed")

    hass = _ShutdownHass()
    receiver._hass = hass  # type: ignore[assignment]
    entry_id = "entry-1"
    now = time.monotonic()
    _arm_starved(receiver, entry_id, now)

    # Must not raise despite the loop-closed failure.
    receiver._maybe_reconnect_starved(entry_id, now)

    # It attempted to schedule exactly once...
    assert hass.created == [f"fcm_zombie_reconnect_{entry_id}"]
    # ...but committed NO scheduler state: no spent attempt, no backoff window,
    # no dangling task registration.
    assert entry_id not in receiver._zombie_reconnect_attempts
    assert entry_id not in receiver._zombie_next_allowed_reconnect_monotonic
    assert entry_id not in receiver._zombie_reconnect_tasks


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
