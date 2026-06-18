# tests/test_coordinator_decrypt_reauth_escalation.py
"""Decrypt-failure escalation in the polling coordinator.

These tests prove the bug fix for the "stale shared key" condition: repeated
location-decryption failures must escalate to a Home Assistant reauth flow
(``ConfigEntryAuthFailed``) after ``_MAX_DECRYPT_FAILURES`` consecutive cycles,
while a single transient failure or a successful self-heal must NOT trigger it.

The escalation decision lives in ``PollingOperations.note_decrypt_failure`` (a
synchronous, side-effect-light method), so it is exercised directly via the
``PollingStub`` mixin harness. Cross-mixin ``_set_auth_state`` is mocked.

The cooldown gate reads the clock through the injectable ``_monotonic`` seam
instead of the ambient ``time.monotonic``; tests drive it deterministically so
the outcome never depends on the host's process uptime (the bug that slipped
through CI: a 0.0 sentinel made the first escalation uptime-dependent).
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

from custom_components.googlefindmy.coordinator import polling as polling_mod
from custom_components.googlefindmy.coordinator.polling import (
    _MAX_DECRYPT_FAILURES,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
    SharedKeyMismatchError,
    SharedKeyMissingError,
    StaleOwnerKeyError,
)

from .helpers.polling_mixin_stub import PollingStub


def _make_stub(monotonic: Callable[[], float] | None = None) -> PollingStub:
    """Return a PollingStub seeded with the decrypt-escalation state fields.

    Args:
        monotonic: Optional clock callable for the cooldown gate. Defaults to a
            fixed value far past any cooldown so the threshold is the only gate;
            pass a mutable clock to exercise the cooldown window itself.
    """
    stub = PollingStub()
    stub._consecutive_decrypt_failures = 0
    # None == "never escalated yet"; the first escalation must never consult the
    # cooldown gate (and therefore never depend on the clock value).
    stub._last_decrypt_reauth_monotonic = None
    stub._last_decrypt_error = None
    stub._monotonic = monotonic if monotonic is not None else (lambda: 1_000_000.0)
    stub._set_auth_state = MagicMock()
    return stub


def test_t4_escalates_after_threshold_consecutive_failures() -> None:
    """T4: the Nth consecutive account-wide decrypt failure escalates to reauth.

    Failures 1..N-1 only warn and return False (keep polling, let self-heal try);
    failure N returns True and marks the auth state as failed so the caller raises
    ConfigEntryAuthFailed.
    """
    stub = _make_stub()
    err = SharedKeyMismatchError("stale shared key")

    # Below threshold: no escalation, no auth-state change.
    for attempt in range(1, _MAX_DECRYPT_FAILURES):
        assert stub.note_decrypt_failure(stale=False, error=err, device="dev") is False
        assert stub._consecutive_decrypt_failures == attempt
    stub._set_auth_state.assert_not_called()

    # Threshold reached: escalate.
    assert stub.note_decrypt_failure(stale=False, error=err, device="dev") is True
    stub._set_auth_state.assert_called_once()
    assert stub._set_auth_state.call_args.kwargs.get("failed") is True
    # Counter resets after escalation so it does not grow unbounded.
    assert stub._consecutive_decrypt_failures == 0


def test_t5_success_reset_prevents_premature_escalation() -> None:
    """T5: a reset (successful cycle) clears the counter, so later failures must
    again reach the full threshold before escalating."""
    stub = _make_stub()
    err = DecryptionError("boom")

    # Two failures, then a reset (what the success path does).
    stub.note_decrypt_failure(stale=False, error=err)
    stub.note_decrypt_failure(stale=False, error=err)
    assert stub._consecutive_decrypt_failures == 2
    stub._consecutive_decrypt_failures = 0  # success path reset
    stub._last_decrypt_error = None

    # Two fresh failures must NOT escalate (threshold is 3, not 1).
    assert stub.note_decrypt_failure(stale=False, error=err) is False
    assert stub.note_decrypt_failure(stale=False, error=err) is False
    stub._set_auth_state.assert_not_called()


def test_t6_stale_owner_key_does_not_escalate() -> None:
    """T6: a per-tracker StaleOwnerKeyError never drives the account-wide counter
    and never escalates to an account reauth (it needs a per-tracker re-pair)."""
    stub = _make_stub()
    stale = StaleOwnerKeyError("tracker v1 < v2")

    for _ in range(_MAX_DECRYPT_FAILURES + 2):
        assert stub.note_decrypt_failure(stale=True, error=stale, device="tag") is False

    assert stub._consecutive_decrypt_failures == 0
    stub._set_auth_state.assert_not_called()


def test_t8_cooldown_suppresses_refire() -> None:
    """T8: once escalated, the reauth flow is not re-fired on every poll while the
    cooldown window is open (the un-fixable condition persists each cycle)."""
    clock = {"now": 1_000_000.0}
    stub = _make_stub(lambda: clock["now"])
    err = SharedKeyMissingError("missing")

    # Reach the threshold -> first escalation.
    for _ in range(_MAX_DECRYPT_FAILURES):
        result = stub.note_decrypt_failure(stale=False, error=err)
    assert result is True
    assert stub._set_auth_state.call_count == 1

    # Immediately hit the threshold again within the cooldown window: no re-fire.
    for _ in range(_MAX_DECRYPT_FAILURES):
        result = stub.note_decrypt_failure(stale=False, error=err)
    assert result is False
    assert stub._set_auth_state.call_count == 1  # unchanged

    # The counter keeps growing while suppressed (it is not reset in the cooldown
    # branch), so once the cooldown window elapses the very next failure -- already
    # past the threshold -- escalates again. This yields "at most one escalation
    # per cooldown window".
    clock["now"] += polling_mod._DECRYPT_REAUTH_COOLDOWN_S + 1.0
    assert stub.note_decrypt_failure(stale=False, error=err) is True
    assert stub._set_auth_state.call_count == 2


def test_t9_first_escalation_is_independent_of_process_uptime() -> None:
    """T9 (regression): the first escalation must fire on a freshly booted host.

    Reproduces the CI flake/production bug: with a 0.0 sentinel the cooldown gate
    computed ``monotonic() - 0.0 < cooldown`` and, while process uptime was below
    the cooldown window, wrongly suppressed the very first reauth escalation. With
    the None sentinel the first escalation skips the cooldown gate entirely, so a
    low monotonic value (simulated fresh boot) must still escalate.
    """
    # Monotonic far below the 6h cooldown window -> a fresh-boot runner.
    fresh_boot_clock = 100.0
    assert fresh_boot_clock < polling_mod._DECRYPT_REAUTH_COOLDOWN_S
    stub = _make_stub(lambda: fresh_boot_clock)
    err = SharedKeyMismatchError("stale shared key")

    results = [
        stub.note_decrypt_failure(stale=False, error=err, device="dev")
        for _ in range(_MAX_DECRYPT_FAILURES)
    ]

    # Failures 1..N-1 hold, failure N escalates -- regardless of the low clock.
    assert results == [False] * (_MAX_DECRYPT_FAILURES - 1) + [True]
    stub._set_auth_state.assert_called_once()
    # The escalation timestamp is now recorded (no longer the None sentinel).
    assert stub._last_decrypt_reauth_monotonic == fresh_boot_clock
