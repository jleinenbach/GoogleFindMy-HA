# tests/test_fcm_receiver_ha_short_run_cap.py
"""Defense-2 regression tests for the FCM supervisor short-run crash cap.

Covers PLAN_HOTFIX_FCM_CASCADING_FAILURE AP-2 (`fcm_receiver_ha.py`):
- 7 building-block tests (threshold trigger, break, reset, re-registration,
  per-entry independence, orthogonality to `_fatal_retry_counts`, unload-cleanup)
- 1 boundary test that pins `<` vs. `<=` drift on `_SHORT_RUN_THRESHOLD_S`
- 1 cross-locale symmetry test for the `fcm_short_run_crash_loop` translation key
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.Auth import fcm_receiver_ha
from custom_components.googlefindmy.Auth.fcm_receiver_ha import (
    _MAX_CONSECUTIVE_SHORT_RUNS,
    _SHORT_RUN_THRESHOLD_S,
    DOMAIN,
    FcmReceiverHA,
)


class _MonoClock:
    """Stateful auto-increment monotonic clock for deterministic run-duration math.

    Each call returns ``value`` then advances by ``step``. ``jump(seconds)``
    skips ahead without consuming a call slot (used to simulate a long run
    between ``entry_start`` and the run-duration check).
    """

    def __init__(self, step: float = 0.01) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        v = self.value
        self.value += self.step
        return v

    def jump(self, seconds: float) -> None:
        self.value += seconds


class _SupervisorPushClientStub:
    """Lightweight ``FcmPushClient`` substitute for supervisor-loop tests.

    Mirrors only the surface the supervisor reads/writes (``start``, ``stop``,
    ``run_state``, ``do_listen``, ``_observe_counter``).

    Two clock-injection hooks model the supervisor's snapshot boundary
    correctly:

    * ``jump_on_start`` advances the clock inside ``start()`` BEFORE the
      supervisor takes the ``entry_start`` snapshot (Z. 960). It models
      pre-monitor elapsed time and does NOT influence ``run_duration``.
    * ``jump_on_run_state_read`` advances the clock on the first
      ``pc.run_state`` access INSIDE the monitor loop (Z. 964), which is
      AFTER the ``entry_start`` snapshot. It is the only correct way to
      inject elapsed-run time that ``run_duration = time.monotonic() -
      entry_start`` actually observes. Use this for boundary math.

    ``run_state`` defaults to ``None`` so the monitor loop exits on the
    first iteration (``state is None`` break path).
    """

    def __init__(
        self,
        *,
        clock: _MonoClock | None = None,
        jump_on_start: float = 0.0,
        jump_on_run_state_read: float = 0.0,
    ) -> None:
        self.clock = clock
        self.jump_on_start = jump_on_start
        self.jump_on_run_state_read = jump_on_run_state_read
        self.do_listen = True
        self.start_calls = 0
        self.stop_calls = 0
        self._observed_short_run_counter: int | None = None
        self._run_state_reads = 0

    @property
    def run_state(self):  # noqa: D401 - mirror attribute access
        self._run_state_reads += 1
        if (
            self._run_state_reads == 1
            and self.clock is not None
            and self.jump_on_run_state_read
        ):
            self.clock.jump(self.jump_on_run_state_read)
        # Implicit ``None`` return forces the ``state is None`` break path
        # in the supervisor monitor loop (Z. 984-989).

    async def start(self) -> None:
        self.start_calls += 1
        if self.clock is not None and self.jump_on_start:
            self.clock.jump(self.jump_on_start)

    async def stop(self) -> None:
        self.stop_calls += 1

    def _observe_counter(self, counter: int) -> None:
        # Last writer wins so tests observe the most recent supervisor pass.
        self._observed_short_run_counter = counter


def _install_supervisor_mocks(
    monkeypatch: pytest.MonkeyPatch,
    receiver: FcmReceiverHA,
    *,
    clock: _MonoClock,
    ensure_clients: list,
) -> AsyncMock:
    """Wire the common supervisor-loop mock surface and return the register mock."""

    monkeypatch.setattr(fcm_receiver_ha.time, "monotonic", clock)

    ensure_iter = iter(ensure_clients)

    async def _ensure(entry_id: str, cache):  # noqa: ARG001
        # Codex Iter-7 (PR #1086): modelling exhaustion as ``None`` mirrors
        # production's ``if not pc`` path, which is designed to back off and
        # retry **forever**. The non-cap tests (long-run reset,
        # re-registration, entry-B independence, 30s boundary) all rely on
        # natural supervisor completion after the configured client list is
        # exhausted, so we MUST stop the supervisor explicitly here instead
        # of falling into the retry-forever branch (CA-TEST-TERMINATION-001).
        try:
            return next(ensure_iter)
        except StopIteration:
            stop_evt = receiver._stop_evts.get(entry_id)
            if stop_evt is not None:
                stop_evt.set()
            return None

    monkeypatch.setattr(receiver, "_ensure_client_for_entry", _ensure)

    register_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)

    monkeypatch.setattr(fcm_receiver_ha.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        fcm_receiver_ha.random, "uniform", lambda *_a, **_kw: 0.0
    )

    real_wait_for = asyncio.wait_for

    async def _instant_wait_for(fut, *, timeout=None):
        if timeout is not None and timeout > 0:
            coro_name = getattr(fut, "__name__", "") or getattr(
                getattr(fut, "cr_code", None), "co_name", ""
            )
            if coro_name == "wait":
                if asyncio.iscoroutine(fut):
                    fut.close()
                raise TimeoutError
        return await real_wait_for(fut, timeout=timeout)

    monkeypatch.setattr(fcm_receiver_ha.asyncio, "wait_for", _instant_wait_for)
    return register_mock


def _install_ir_capture(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    """Capture ``ir.async_create_issue`` and ``ir.async_delete_issue`` calls."""

    create_issue = MagicMock()
    delete_issue = MagicMock()
    monkeypatch.setattr(fcm_receiver_ha.ir, "async_create_issue", create_issue)
    monkeypatch.setattr(fcm_receiver_ha.ir, "async_delete_issue", delete_issue)
    monkeypatch.setattr(
        fcm_receiver_ha.ir,
        "IssueSeverity",
        SimpleNamespace(WARNING="warning", ERROR="error"),
    )
    return create_issue, delete_issue


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_short_run_triggers_repair_issue_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 short runs in one supervisor pass produce exactly one ERROR repair issue."""
    entry_id = "entry-trigger"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)  # 1ms per call -> every run is short
    clients = [_SupervisorPushClientStub(clock=clock) for _ in range(_MAX_CONSECUTIVE_SHORT_RUNS)]
    register_mock = _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients
    )
    create_issue, _delete_issue = _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert register_mock.await_count == _MAX_CONSECUTIVE_SHORT_RUNS
    assert create_issue.call_count == 1
    call = create_issue.call_args
    assert call.args[1] == DOMAIN
    assert call.args[2] == f"fcm_short_run_crash_loop_{entry_id}"
    assert call.kwargs["is_fixable"] is False
    assert call.kwargs["severity"] == fcm_receiver_ha.ir.IssueSeverity.ERROR
    assert call.kwargs["translation_key"] == "fcm_short_run_crash_loop"
    assert receiver._fatal_errors[entry_id].startswith("FCM short-run crash loop")


@pytest.mark.asyncio
async def test_short_run_break_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The supervisor task completes after the 10th short run (no further iters)."""
    entry_id = "entry-break"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    # Provide more clients than the threshold to detect "loop kept going".
    clients = [
        _SupervisorPushClientStub(clock=clock)
        for _ in range(_MAX_CONSECUTIVE_SHORT_RUNS + 5)
    ]
    register_mock = _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients
    )
    _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert receiver.supervisors[entry_id].done()
    assert register_mock.await_count == _MAX_CONSECUTIVE_SHORT_RUNS


@pytest.mark.asyncio
async def test_long_run_resets_closure_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """9 short + 1 long + 9 short runs must not trigger the threshold."""
    entry_id = "entry-reset"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    # Build 19 clients: short, short, ..., LONG at index 9, short, short, ...
    # The LONG run must use ``jump_on_run_state_read`` (not ``jump_on_start``)
    # so the 60s elapse AFTER the supervisor captures ``entry_start`` at
    # Z. 960; otherwise the jump is consumed by the snapshot itself and
    # ``run_duration`` collapses to ~0, making this run count as short.
    clients = []
    for idx in range(19):
        if idx == 9:
            clients.append(
                _SupervisorPushClientStub(
                    clock=clock, jump_on_run_state_read=60.0
                )
            )
        else:
            clients.append(_SupervisorPushClientStub(clock=clock))
    register_mock = _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients
    )
    create_issue, _delete_issue = _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    # No repair issue: the long run at index 9 resets the counter, so the
    # remaining 9 short runs never reach the threshold of 10.
    assert create_issue.call_count == 0
    assert entry_id not in receiver._fatal_errors
    assert register_mock.await_count == 19


@pytest.mark.asyncio
async def test_re_registration_does_not_inherit_old_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled supervisor's counter must not bleed into a fresh supervisor."""
    entry_id = "entry-rereg"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    # First supervisor: 9 short runs then we cancel.
    clients_first = [
        _SupervisorPushClientStub(clock=clock)
        for _ in range(9)
    ]
    _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients_first
    )
    create_issue, _delete_issue = _install_ir_capture(monkeypatch)

    # Run the first supervisor to natural completion (runs out of clients).
    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)
    # Pre-condition: 9 short runs did NOT trigger the cap.
    assert create_issue.call_count == 0
    assert entry_id not in receiver._fatal_errors

    # Remove the spent stop_evt so a fresh supervisor can start.
    receiver._stop_evts.pop(entry_id, None)
    receiver.supervisors.pop(entry_id, None)

    # Second supervisor: 9 more short runs. If the counter were shared, the
    # combined 18 runs would have crossed the threshold and re-fired.
    clients_second = [
        _SupervisorPushClientStub(clock=clock)
        for _ in range(9)
    ]
    _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients_second
    )
    create_issue_b, _ = _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert create_issue_b.call_count == 0
    assert entry_id not in receiver._fatal_errors


@pytest.mark.asyncio
async def test_per_entry_supervisors_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry A crash-loops, entry B stays healthy: only A produces a repair issue."""
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock_a = _MonoClock(step=0.001)
    clock_b = _MonoClock(step=0.001)
    # Entry A: 10 short runs (will trip the cap).
    clients_a = [_SupervisorPushClientStub(clock=clock_a) for _ in range(_MAX_CONSECUTIVE_SHORT_RUNS)]
    # Entry B: 5 healthy long runs (well above threshold).
    # ``jump_on_run_state_read`` (not ``jump_on_start``) is required so the
    # 60s elapse AFTER ``entry_start`` snapshot at Z. 960 and actually mark
    # each run as a long run; otherwise the test passes only by accident
    # because 5 short runs also stay below the cap of 10.
    clients_b = [
        _SupervisorPushClientStub(clock=clock_b, jump_on_run_state_read=60.0)
        for _ in range(5)
    ]

    # Single shared monkeypatch fixture but per-receiver wiring.
    # We can't run both supervisors against the same monkeypatched function,
    # so we sequence A then B.
    register_a = _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock_a, ensure_clients=clients_a
    )
    create_issue, _ = _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry("entry-a", None)
    await asyncio.wait_for(receiver.supervisors["entry-a"], timeout=5.0)

    register_b = _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock_b, ensure_clients=clients_b
    )
    await receiver._start_supervisor_for_entry("entry-b", None)
    await asyncio.wait_for(receiver.supervisors["entry-b"], timeout=5.0)

    # Exactly one issue was raised, and it targets entry A.
    assert create_issue.call_count == 1
    assert create_issue.call_args.args[2] == "fcm_short_run_crash_loop_entry-a"
    assert receiver._fatal_errors.get("entry-a", "").startswith(
        "FCM short-run crash loop"
    )
    assert "entry-b" not in receiver._fatal_errors
    assert register_a.await_count == _MAX_CONSECUTIVE_SHORT_RUNS
    assert register_b.await_count == 5


@pytest.mark.asyncio
async def test_fatal_retry_counts_orthogonal_to_short_run_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_fatal_retry_counts` and the closure-counter advance independently.

    A short run pumps the closure counter; a `FatalRegistrationError` from
    the registration path pumps `_fatal_retry_counts`. They share no state
    and must not double-fire the short-run repair issue.
    """
    from custom_components.googlefindmy.exceptions import FatalRegistrationError

    entry_id = "entry-orthogonal"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    clients = [_SupervisorPushClientStub(clock=clock) for _ in range(6)]
    _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients
    )
    create_issue, _ = _install_ir_capture(monkeypatch)

    # Override register: first 5 successes (short runs), 6th raises fatal endpoint.
    side_effects: list = [True, True, True, True, True]
    err = FatalRegistrationError("registration failed")
    err.is_auth_error = False  # endpoint path, not auth
    side_effects.append(err)
    register_mock = AsyncMock(side_effect=side_effects)
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)

    # 6th iteration's FatalRegistrationError triggers endpoint path
    # which retries up to _MAX_FATAL_ENDPOINT_RETRIES (7) before giving up.
    # Refill register with the same error so the endpoint counter exhausts.
    side_effects.extend([err] * 6)

    # Bounded run via wait_for to avoid hanging on register-retry backoff.
    await receiver._start_supervisor_for_entry(entry_id, None)
    try:
        await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)
    except TimeoutError:  # noqa: PERF203 - test bounded fallback
        receiver._stop_evts[entry_id].set()
        await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    # The short-run counter reached 5 (< 10), so no short-run issue.
    short_run_calls = [
        c for c in create_issue.call_args_list
        if c.args[2].startswith("fcm_short_run_crash_loop_")
    ]
    assert len(short_run_calls) == 0
    # _fatal_retry_counts was incremented at least once by the endpoint path.
    counter_key = f"{entry_id}:endpoint"
    assert receiver._fatal_retry_counts.get(counter_key, 0) >= 1


def test_unload_clears_fatal_errors_via_force() -> None:
    """`_clear_fatal_error_for_entry(force=True)` (unload path) releases the latch.

    Defense 2 iter-8: the unregister path must clear the cap latch alongside
    the fatal-error map because the entry itself goes away. Without ``force``
    the clear-path is a no-op while the latch is set (Codex second finding).
    """
    receiver = FcmReceiverHA()
    receiver._fatal_errors["entry-unload"] = "FCM short-run crash loop: ..."
    receiver._fatal_error = "FCM short-run crash loop: ..."
    receiver._short_run_cap_latched.add("entry-unload")

    receiver._clear_fatal_error_for_entry(
        "entry-unload", reason="Entry unregistered", force=True
    )

    assert "entry-unload" not in receiver._fatal_errors
    assert "entry-unload" not in receiver._short_run_cap_latched
    assert receiver._fatal_error is None


# --------------------------------------------------------------------------
# Defense 2 iter-8: cap-latch lifecycle (Codex second finding)
# --------------------------------------------------------------------------
#
# Codex objected that the cap state lives in the same ``_fatal_errors`` map
# that ``_register_for_fcm_entry`` unconditionally clears after a successful
# registration. Registration is NOT a recovery proof for a poison-message
# crash loop: only a real healthy supervisor run (>= 30s) shows that the
# poisoned notification is gone. The tests below pin the new lifecycle
# contract (CA-STATE-LIFECYCLE-ASYMMETRY-001):
#
#   * ``_short_run_cap_latched`` is set at cap-fire time alongside the
#     ``_fatal_errors`` entry and the Repairs issue.
#   * ``_clear_fatal_error_for_entry`` is a no-op while the latch is set
#     (unless ``force=True`` is passed, which only the unregister path does).
#   * Only the healthy-run branch of the supervisor loop releases the latch,
#     pops the cap-related ``_fatal_errors`` entry, and re-derives the
#     aggregate ``_fatal_error``.


def test_clear_fatal_error_is_noop_while_cap_latched() -> None:
    """Registration-success paths cannot drop the latch (Codex iter-8 finding 2)."""
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())
    receiver._fatal_errors["entry-latched"] = "FCM short-run crash loop: ..."
    receiver._fatal_error = "FCM short-run crash loop: ..."
    receiver._short_run_cap_latched.add("entry-latched")

    # ``force`` defaults to ``False``: this is the path that
    # ``_register_for_fcm_entry`` and the credential-update handler take.
    receiver._clear_fatal_error_for_entry(
        "entry-latched", reason="Registration succeeded"
    )

    # Nothing changed: the latch, the fatal-error map, and the aggregate
    # all survive because the registration did not prove a healthy run.
    assert "entry-latched" in receiver._short_run_cap_latched
    assert receiver._fatal_errors["entry-latched"].startswith(
        "FCM short-run crash loop"
    )
    assert receiver._fatal_error == "FCM short-run crash loop: ..."


def test_clear_without_latch_still_clears_fatal_errors() -> None:
    """Without an active cap latch, the clear-path behaves idempotently."""
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())
    receiver._fatal_errors["entry-clean"] = "FCM auth failed ..."
    receiver._fatal_error = "FCM auth failed ..."

    receiver._clear_fatal_error_for_entry(
        "entry-clean", reason="Registration succeeded"
    )

    assert "entry-clean" not in receiver._fatal_errors
    assert receiver._fatal_error is None


@pytest.mark.asyncio
async def test_cap_fire_sets_short_run_cap_latched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap-fire installs the latch alongside the fatal-error map and the issue."""
    entry_id = "entry-cap-latch"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    clients = [
        _SupervisorPushClientStub(clock=clock)
        for _ in range(_MAX_CONSECUTIVE_SHORT_RUNS)
    ]
    _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients
    )
    create_issue, _ = _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert create_issue.call_count == 1
    assert entry_id in receiver._short_run_cap_latched
    assert receiver._fatal_errors[entry_id].startswith("FCM short-run crash loop")


@pytest.mark.asyncio
async def test_cap_latch_survives_registration_success_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A simulated registration-success path cannot drop the latch.

    Defense 2 iter-8 (Codex second finding): without the latch, a reload or
    credential update after cap-fire would clear the fatal-error map and the
    Repairs issue before the supervisor proved the poison message is gone,
    leaving the user blind during the next 10-run burst.
    """
    entry_id = "entry-survive"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    clients = [
        _SupervisorPushClientStub(clock=clock)
        for _ in range(_MAX_CONSECUTIVE_SHORT_RUNS)
    ]
    _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients
    )
    _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert entry_id in receiver._short_run_cap_latched
    pre_message = receiver._fatal_errors[entry_id]

    # Simulate ``_register_for_fcm_entry`` success path (Z. 1260) and the
    # credential-update path (Z. 2069). Neither must release the latch.
    receiver._clear_fatal_error_for_entry(
        entry_id, reason="Registration succeeded"
    )
    receiver._clear_fatal_error_for_entry(
        entry_id, reason="Credentials updated for entry"
    )

    assert entry_id in receiver._short_run_cap_latched
    assert receiver._fatal_errors[entry_id] == pre_message


# --------------------------------------------------------------------------
# Defense 2 iter-10: cap-latch guard granularity (Codex follow-up finding)
# --------------------------------------------------------------------------
#
# Codex objected that the iter-8 latch guard was unconditional and blocked
# every later ``_clear_fatal_error_for_entry`` call while the latch was set.
# Scenario: the cap fires, then the user reloads or re-registers; the new
# registration hits a terminal 401/404 and overwrites ``_fatal_errors[entry_id]``
# with a non-cap message; a credentials recovery later tries to clear that
# fatal but is blocked by the latch guard, so the coordinator keeps seeing
# the stale auth-fatal even after the underlying problem is fixed.
#
# CA-GUARD-GRANULARITY-001: the latch guard is now SELECTIVE:
#
#   * Latch active AND stored fatal IS the cap message
#     -> full no-op (cap state + Repairs UI artefact preserved).
#   * Latch active AND stored fatal is a NON-cap message
#     -> clear the non-cap fatal, but keep the latch and the Repairs UI
#        artefact (cap warning stays visible until a healthy run releases it).
#   * Latch active AND no fatal stored
#     -> full no-op (treat as cap-only state).
#   * Latch not active OR force=True
#     -> normal clear-all path (unchanged).


def test_clear_clears_non_cap_fatal_while_cap_latched() -> None:
    """Non-cap fatals must clear normally even while the cap latch is active.

    Defense 2 iter-10 (Codex follow-up): the iter-8 guard was too broad and
    blocked every clear-call, so a terminal 401/404 written into
    ``_fatal_errors`` by a re-registration attempt after cap-fire could never
    be dropped by a subsequent credentials recovery. The guard now blocks
    only when the stored fatal IS the cap message; non-cap fatals are
    dropped while latch and Repairs UI artefact stay in place.
    """
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    # Cap is latched (set by an earlier cap-fire); a subsequent
    # re-registration then overwrote the fatal map with a non-cap auth error.
    receiver._short_run_cap_latched.add("entry-mixed")
    receiver._fatal_errors["entry-mixed"] = (
        "FCM registration failed: HTTP 401 Unauthorized"
    )
    receiver._fatal_error = "FCM registration failed: HTTP 401 Unauthorized"

    # Credentials recovery path: ``_register_for_fcm_entry`` calls this after
    # a new successful registration. The non-cap fatal MUST clear so the
    # coordinator does not see a stale auth-fatal.
    receiver._clear_fatal_error_for_entry(
        "entry-mixed", reason="Credentials updated for entry"
    )

    # Non-cap fatal cleared.
    assert "entry-mixed" not in receiver._fatal_errors
    assert receiver._fatal_error is None
    # Cap latch SURVIVES: only a real healthy supervisor run may drop it.
    assert "entry-mixed" in receiver._short_run_cap_latched


def test_clear_preserves_repair_issue_when_clearing_non_cap_fatal() -> None:
    """Clearing a non-cap fatal while latched must not delete the Repairs issue.

    The Repairs UI artefact (``fcm_short_run_crash_loop_<entry_id>``) is the
    user-visible cap warning. Dropping it during an unrelated credentials
    recovery would invalidate the cap warning before the listener proves the
    poison message is gone (the very failure mode iter-8 pinned).
    """
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    deleted_issues: list[tuple[str, str]] = []

    def fake_async_delete_issue(hass, domain: str, issue_id: str) -> None:
        deleted_issues.append((domain, issue_id))

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(
            fcm_receiver_ha.ir, "async_delete_issue", fake_async_delete_issue
        )

        receiver._short_run_cap_latched.add("entry-keep-issue")
        receiver._fatal_errors["entry-keep-issue"] = (
            "FCM registration failed: HTTP 404 Not Found"
        )
        receiver._fatal_error = "FCM registration failed: HTTP 404 Not Found"

        receiver._clear_fatal_error_for_entry(
            "entry-keep-issue", reason="Credentials updated for entry"
        )
    finally:
        monkey.undo()

    # Non-cap fatal cleared but the Repairs UI artefact must remain (no
    # ``async_delete_issue`` call for the cap issue id).
    assert "entry-keep-issue" not in receiver._fatal_errors
    assert deleted_issues == []
    assert "entry-keep-issue" in receiver._short_run_cap_latched


def test_clear_no_stored_fatal_while_latched_is_full_noop() -> None:
    """Latch active and no fatal stored: full no-op, latch survives."""
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    receiver._short_run_cap_latched.add("entry-no-fatal")
    # No entry in ``_fatal_errors``.

    receiver._clear_fatal_error_for_entry(
        "entry-no-fatal", reason="Registration succeeded"
    )

    assert "entry-no-fatal" in receiver._short_run_cap_latched
    assert "entry-no-fatal" not in receiver._fatal_errors


@pytest.mark.asyncio
async def test_healthy_run_releases_cap_latch_and_fatal_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy run (>= 30s) clears the latch, the fatal map entry, and the issue."""
    entry_id = "entry-recover"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    # 10 short runs trip the cap; one healthy run afterwards must release it.
    # The healthy run is modelled as a long supervisor pass (jump after the
    # ``entry_start`` snapshot via ``jump_on_run_state_read``).
    short_clients = [
        _SupervisorPushClientStub(clock=clock)
        for _ in range(_MAX_CONSECUTIVE_SHORT_RUNS)
    ]
    healthy_client = _SupervisorPushClientStub(
        clock=clock, jump_on_run_state_read=60.0
    )
    _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock,
        ensure_clients=[*short_clients, healthy_client],
    )
    create_issue, delete_issue = _install_ir_capture(monkeypatch)

    # Phase 1: 10 short runs trip the cap and stop the supervisor.
    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert create_issue.call_count == 1
    assert entry_id in receiver._short_run_cap_latched
    assert receiver._fatal_errors[entry_id].startswith("FCM short-run crash loop")

    # Phase 2: simulate a reload (fresh stop_evt + fresh supervisor task).
    receiver._stop_evts.pop(entry_id, None)
    receiver.supervisors.pop(entry_id, None)

    # The healthy run drops the latch from inside the supervisor loop's
    # else branch (counter reset path).
    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    assert entry_id not in receiver._short_run_cap_latched
    assert entry_id not in receiver._fatal_errors
    assert receiver._fatal_error is None
    # The Repairs issue is deleted at least once on the healthy path.
    delete_calls_for_entry = [
        call
        for call in delete_issue.call_args_list
        if call.args[2] == f"fcm_short_run_crash_loop_{entry_id}"
    ]
    assert delete_calls_for_entry, (
        "expected a delete_issue call for the short-run crash loop key"
    )


# --------------------------------------------------------------------------
# Boundary test (Iteration-2-L3)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_run_boundary_exactly_30s_does_not_count_as_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`<` vs. `<=` drift sentry: run_duration == 30.0 must NOT count as short."""
    assert _SHORT_RUN_THRESHOLD_S == 30.0  # pin the constant against silent drift

    entry_id = "entry-boundary"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    # Frozen clock (``step=0.0``): every ``time.monotonic()`` call returns
    # the same value unless ``.jump(seconds)`` is invoked. This is the only
    # way to drive ``run_duration`` to *exactly* 30.0 — any auto-increment
    # would push the read at Z. 1008 past the boundary by a few ``step``s
    # and let an off-by-one bug (`<=` instead of `<`) hide inside the noise.
    clock = _MonoClock(step=0.0)

    # Critical ordering (Codex Iter-5 finding):
    # ``jump_on_start`` would fire BEFORE the supervisor records
    # ``entry_start = time.monotonic()`` (Z. 960), so the jump is baked
    # into the snapshot and ``run_duration`` ends up ~0s. Instead, hook
    # the jump on the first ``pc.run_state`` read (Z. 964), which is the
    # first clock-observable event INSIDE the monitor loop, AFTER the
    # snapshot. Then ``time.monotonic() - entry_start == 30.0`` exactly.
    stub = _SupervisorPushClientStub(clock=clock, jump_on_run_state_read=30.0)
    _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=[stub]
    )
    create_issue, _ = _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    # The else-branch was hit (counter reset to 0), not the short-run branch.
    # If `<` ever drifts to `<=`, ``run_duration == 30.0`` falls into the
    # short-run path and ``_observed_short_run_counter`` becomes 1.
    assert stub._observed_short_run_counter == 0, (
        f"Boundary 30.0s was counted as short — `<` drifted to `<=`? "
        f"Counter={stub._observed_short_run_counter}"
    )
    assert create_issue.call_count == 0
    assert entry_id not in receiver._fatal_errors


# --------------------------------------------------------------------------
# Cross-locale symmetry (Iteration-2-L2)
# --------------------------------------------------------------------------


def test_translation_key_present_in_all_locales(integration_root: Path) -> None:
    """`fcm_short_run_crash_loop` exists in every locale file under `issues`."""
    key = "fcm_short_run_crash_loop"
    files = {
        "strings.json": integration_root / "strings.json",
        "de.json": integration_root / "translations" / "de.json",
        "en.json": integration_root / "translations" / "en.json",
        "es.json": integration_root / "translations" / "es.json",
        "fr.json": integration_root / "translations" / "fr.json",
        "he.json": integration_root / "translations" / "he.json",
        "it.json": integration_root / "translations" / "it.json",
        "nl.json": integration_root / "translations" / "nl.json",
        "pl.json": integration_root / "translations" / "pl.json",
        "pt-BR.json": integration_root / "translations" / "pt-BR.json",
        "pt.json": integration_root / "translations" / "pt.json",
    }

    titles: dict[str, str] = {}
    descriptions: dict[str, str] = {}
    for label, path in files.items():
        assert path.exists(), f"locale file missing: {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "issues" in data, f"{label}: no issues section"
        assert key in data["issues"], f"{label}: missing key {key!r}"
        entry = data["issues"][key]
        assert isinstance(entry, dict), f"{label}: {key!r} must be a dict"
        assert entry.get("title", "").strip(), f"{label}: empty title for {key!r}"
        assert (
            entry.get("description", "").strip()
        ), f"{label}: empty description for {key!r}"
        assert len(entry["description"]) >= 50, (
            f"{label}: description too short ({len(entry['description'])} chars) "
            f"for a repair-issue body"
        )
        titles[label] = entry["title"]
        descriptions[label] = entry["description"]

    # No copy-paste drift between German and English description.
    assert descriptions["de.json"] != descriptions["en.json"], (
        "Copy-paste drift: de.json description matches en.json — translation missing"
    )
    # Strings.json (master) matches en.json (English locale)
    assert descriptions["strings.json"] == descriptions["en.json"], (
        "strings.json description must mirror en.json"
    )


# --------------------------------------------------------------------------
# Defense 2 iter-9 (Codex PR #1086 follow-up):
# Cap-fire must TERMINATE the supervisor; the inner-loop ``break`` alone
# falls through to the per-iteration restart block and keeps spawning fresh
# FCM clients (retry pressure + log spam continue indefinitely despite the
# cap latch). See CA-DEFENSE-TERMINATION-001.
#
# These tests deliberately do NOT rely on the helper's
# StopIteration->stop_evt.set() emergency brake (CA-TEST-TERMINATION-001):
# they install their own ``_ensure_client_for_entry`` mock that fails the
# test the moment the supervisor calls it AFTER the cap should have fired.
# That isolates the production-side termination contract from the test-helper
# safety net.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cap_fire_terminates_outer_loop_without_helper_safety_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap-fire must stop the supervisor in production, not via the test helper.

    Codex iter-9 (PR #1086): ``break`` after the cap fires only exits the
    inner monitor loop; the outer ``while not stop_evt.is_set()`` keeps
    spinning and ``_ensure_client_for_entry`` is called for an 11th client.
    The production fix sets ``stop_evt`` and pops ``_stop_evts[entry_id]``
    inside the cap-fire branch, so the outer loop exits before any further
    ensure call.
    """
    entry_id = "entry-cap-terminates"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    clients = [
        _SupervisorPushClientStub(clock=clock)
        for _ in range(_MAX_CONSECUTIVE_SHORT_RUNS + 5)
    ]
    ensure_iter = iter(clients)
    ensure_call_count = 0

    async def _ensure(entry_id_inner: str, cache):  # noqa: ARG001
        # NOTE: no StopIteration->stop_evt.set() escape hatch here. If the
        # production cap fails to terminate the supervisor, this returns the
        # 11th client and we explicitly fail the test below.
        nonlocal ensure_call_count
        ensure_call_count += 1
        return next(ensure_iter)

    monkeypatch.setattr(fcm_receiver_ha.time, "monotonic", clock)
    monkeypatch.setattr(receiver, "_ensure_client_for_entry", _ensure)
    register_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(receiver, "_register_for_fcm_entry", register_mock)
    monkeypatch.setattr(fcm_receiver_ha.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(
        fcm_receiver_ha.random, "uniform", lambda *_a, **_kw: 0.0
    )

    real_wait_for = asyncio.wait_for

    async def _instant_wait_for(fut, *, timeout=None):
        if timeout is not None and timeout > 0:
            coro_name = getattr(fut, "__name__", "") or getattr(
                getattr(fut, "cr_code", None), "co_name", ""
            )
            if coro_name == "wait":
                if asyncio.iscoroutine(fut):
                    fut.close()
                raise TimeoutError
        return await real_wait_for(fut, timeout=timeout)

    monkeypatch.setattr(fcm_receiver_ha.asyncio, "wait_for", _instant_wait_for)
    _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    # Production-side termination contract: exactly _MAX ensure calls.
    # An 11th call would mean the outer loop kept running after cap-fire.
    assert ensure_call_count == _MAX_CONSECUTIVE_SHORT_RUNS, (
        f"Supervisor kept spinning after cap-fire: ensure was called "
        f"{ensure_call_count} times (expected {_MAX_CONSECUTIVE_SHORT_RUNS}). "
        f"Defense 2 cap is a paper tiger."
    )
    assert receiver.supervisors[entry_id].done()
    assert entry_id in receiver._short_run_cap_latched


@pytest.mark.asyncio
async def test_cap_fire_pops_stop_evt_for_legitimate_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After cap-fire, ``_stop_evts[entry_id]`` is gone so a reload restarts cleanly.

    The cap-fire branch calls ``stop_evt.set()`` AND
    ``self._stop_evts.pop(entry_id, None)``. Without the pop, the next
    legitimate restart via ``_start_supervisor_for_entry`` would reuse the
    already-set event through ``setdefault`` and the new supervisor would
    terminate immediately.
    """
    entry_id = "entry-cap-pop"
    receiver = FcmReceiverHA()
    receiver.attach_hass(SimpleNamespace())

    clock = _MonoClock(step=0.001)
    clients = [
        _SupervisorPushClientStub(clock=clock)
        for _ in range(_MAX_CONSECUTIVE_SHORT_RUNS)
    ]
    _install_supervisor_mocks(
        monkeypatch, receiver, clock=clock, ensure_clients=clients
    )
    _install_ir_capture(monkeypatch)

    await receiver._start_supervisor_for_entry(entry_id, None)
    await asyncio.wait_for(receiver.supervisors[entry_id], timeout=5.0)

    # After cap-fire the stop event has been consumed AND removed so that a
    # legitimate ``_register_for_fcm_entry`` or reload constructs a fresh one.
    assert entry_id not in receiver._stop_evts, (
        "Stale stop event lingers in _stop_evts after cap-fire; a "
        "legitimate restart would short-circuit because setdefault would "
        "reuse the already-set event."
    )
