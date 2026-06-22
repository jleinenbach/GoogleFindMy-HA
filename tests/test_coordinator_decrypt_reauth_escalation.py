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

import logging
from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryAuthFailed

from custom_components.googlefindmy.coordinator import polling as polling_mod
from custom_components.googlefindmy.coordinator.helpers.stats import CryptoStatus
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
    stub.note_decrypt_success()  # the real success path reset
    assert stub._consecutive_decrypt_failures == 0
    # The shared entry point clears only the account-wide counter; the diagnostic
    # error class is owned by the poll loop's OK gate, so it survives this call.
    assert stub._last_decrypt_error is not None

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


def test_note_decrypt_success_clears_accumulated_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shared success entry point clears a partial decrypt-failure budget.

    This is the symmetric counterpart of ``note_decrypt_failure`` that both the
    poll cycle and the FCM background-push decode call on a proven decrypt. Two
    non-escalating failures leave the counter at 2; a single success must reset
    it to 0, so a later failure starts the threshold count fresh instead of
    escalating one step early. The diagnostic ``_last_decrypt_error`` is NOT
    cleared here: it is part of the sensor surface owned by the poll loop's OK
    gate (which alone knows whether a per-tracker stale key must keep it), so
    this shared entry point leaves it intact.
    """
    stub = _make_stub()
    err = DecryptionError("boom")
    stub.note_decrypt_failure(stale=False, error=err)
    stub.note_decrypt_failure(stale=False, error=err)
    assert stub._consecutive_decrypt_failures == 2
    assert stub._last_decrypt_error is not None

    with caplog.at_level(logging.INFO, logger=polling_mod.__name__):
        stub.note_decrypt_success()

    assert stub._consecutive_decrypt_failures == 0
    # Counter cleared, diagnostic preserved (owned by the poll OK gate).
    assert stub._last_decrypt_error is not None
    assert "clearing 2 decrypt failure(s)" in caplog.text


def test_note_decrypt_success_is_idempotent_when_no_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """With an empty budget the success entry point is a cheap no-op: it clears
    nothing and stays silent (no misleading "clearing 0 failure(s)" log)."""
    stub = _make_stub()

    with caplog.at_level(logging.INFO, logger=polling_mod.__name__):
        stub.note_decrypt_success()

    assert stub._consecutive_decrypt_failures == 0
    assert stub._last_decrypt_error is None
    assert "clearing" not in caplog.text


@pytest.mark.parametrize(
    "account_wide_failure",
    [CryptoStatus.SHARED_KEY_INVALID, CryptoStatus.SHARED_KEY_MISSING],
)
def test_note_background_decrypt_success_heals_account_wide_failure(
    account_wide_failure: str,
) -> None:
    """A proven background push heals the sensor out of an account-wide failure.

    On push-only setups whose scheduled polls stay idle, the diagnostic
    encryption-key sensor would otherwise stick on a stale ``shared_key_invalid``
    / ``shared_key_missing`` status even though real pushes keep decrypting. The
    background success entry point lifts the account-wide failure to ``OK`` and
    moves the error class with it (one sensor surface), and still clears the
    shared reauth budget.
    """
    stub = _make_stub()
    stub._crypto_status_state = account_wide_failure
    stub._last_decrypt_error = "SharedKeyMismatchError: stale"
    stub._consecutive_decrypt_failures = 2

    stub.note_background_decrypt_success()

    assert stub._crypto_status_state == CryptoStatus.OK
    assert stub._last_decrypt_error is None
    assert stub._consecutive_decrypt_failures == 0


def test_note_background_decrypt_success_preserves_tracker_key_outdated() -> None:
    """A single background decode must NOT wipe a per-tracker stale-key diagnostic.

    A push proves the account-wide shared key, not that a per-tracker outdated
    owner key has been re-paired (it has no cross-tracker view). So when the
    sensor reports ``tracker_key_outdated`` the heal must leave both the status
    and its error class untouched, while still clearing the account-wide budget.
    This is the anti-widening guard for the round-5 diagnostic invariant.
    """
    stub = _make_stub()
    stub._crypto_status_state = CryptoStatus.TRACKER_KEY_OUTDATED
    stub._last_decrypt_error = "StaleOwnerKeyError: tracker v1 < v2"
    stub._consecutive_decrypt_failures = 0

    stub.note_background_decrypt_success()

    assert stub._crypto_status_state == CryptoStatus.TRACKER_KEY_OUTDATED
    assert stub._last_decrypt_error == "StaleOwnerKeyError: tracker v1 < v2"


def test_note_background_decrypt_success_leaves_unknown_untouched() -> None:
    """With no failure to heal, the push success does not fabricate an ``OK``.

    ``UNKNOWN`` is the initial state before any decrypt signal; the OK semantics
    stay anchored to an observed account-wide failure being refuted, so a push
    must not flip a never-failed sensor to OK ahead of the first real proof.
    """
    stub = _make_stub()
    assert stub._crypto_status_state == CryptoStatus.UNKNOWN

    stub.note_background_decrypt_success()

    assert stub._crypto_status_state == CryptoStatus.UNKNOWN


# ---------------------------------------------------------------------------
# _resolve_cycle_decrypt_outcome: the per-cycle verdict that decides escalation.
# Regression suite for the "offline device drags a healthy account into a
# spurious reauth" bug: positive proof (a sibling decrypt) must dominate a single
# device's DecryptionError so the account-wide reauth budget is neither advanced
# nor tripped.
# ---------------------------------------------------------------------------


def test_resolve_mixed_cycle_sibling_success_blocks_spurious_reauth(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The bug: a powered-off device fails to decrypt while siblings succeed.

    With the account-wide budget one step below the threshold, a mixed cycle
    (``cycle_decrypt_error`` set AND ``cycle_had_successful_decrypt`` True) must
    NOT escalate: the sibling success proves the shared key is healthy. The budget
    is cleared (not advanced), the diagnostic sensor heals to OK, a warning (not
    the account-wide ERROR escalation) is logged, and no auth state is set.

    Mutation gate: the pre-fix code advanced the counter here (2 -> 3) and
    returned True, so asserting ``result is False`` with a cleared counter and an
    untouched auth state turns red if the positive-proof guard is removed.
    """
    stub = _make_stub()
    stub._consecutive_decrypt_failures = _MAX_DECRYPT_FAILURES - 1
    err = DecryptionError(
        "All own-report decryptions failed; the cached identity key no longer "
        "matches the server reports."
    )

    with caplog.at_level(logging.WARNING):
        result = stub._resolve_cycle_decrypt_outcome(
            cycle_decrypt_error=err,
            cycle_had_successful_decrypt=True,
            cycle_had_stale_key=False,
        )

    assert result is False
    assert stub._consecutive_decrypt_failures == 0
    assert stub._crypto_status_state == CryptoStatus.OK
    assert stub._last_decrypt_error is None
    stub._set_auth_state.assert_not_called()
    assert any(
        rec.levelno == logging.WARNING
        and "no re-authentication is required" in rec.message
        for rec in caplog.records
    )


def test_resolve_account_wide_failure_without_sibling_escalates() -> None:
    """A decrypt failure with NOT A SINGLE sibling success stays account-wide.

    The verdict must still advance the consecutive-cycle budget and, at the
    threshold, escalate to reauth -- the genuine stale-shared-key path that the
    fix must preserve (the counterpart to the mixed-cycle suppression above).
    """
    stub = _make_stub()
    err = SharedKeyMismatchError("stale shared key")

    # Failures 1..N-1: account-wide, below threshold -> count up, no escalation.
    for attempt in range(1, _MAX_DECRYPT_FAILURES):
        assert (
            stub._resolve_cycle_decrypt_outcome(
                cycle_decrypt_error=err,
                cycle_had_successful_decrypt=False,
                cycle_had_stale_key=False,
            )
            is False
        )
        assert stub._consecutive_decrypt_failures == attempt
    stub._set_auth_state.assert_not_called()

    # Threshold reached -> escalate.
    assert (
        stub._resolve_cycle_decrypt_outcome(
            cycle_decrypt_error=err,
            cycle_had_successful_decrypt=False,
            cycle_had_stale_key=False,
        )
        is True
    )
    stub._set_auth_state.assert_called_once()


def test_resolve_clean_success_cycle_heals_without_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cycle with a success and no decrypt error heals the sensor silently.

    No ``cycle_decrypt_error`` means no per-device warning; positive proof still
    sets the OK status and clears any partial budget.
    """
    stub = _make_stub()
    stub._consecutive_decrypt_failures = 2

    with caplog.at_level(logging.WARNING):
        result = stub._resolve_cycle_decrypt_outcome(
            cycle_decrypt_error=None,
            cycle_had_successful_decrypt=True,
            cycle_had_stale_key=False,
        )

    assert result is False
    assert stub._crypto_status_state == CryptoStatus.OK
    assert stub._consecutive_decrypt_failures == 0
    assert not any(rec.levelno == logging.WARNING for rec in caplog.records)


def test_resolve_per_device_failure_with_stale_key_keeps_sensor() -> None:
    """A per-device decrypt failure plus a stale tracker key, with sibling success.

    Sibling proof clears the account-wide budget, but the ``not
    cycle_had_stale_key`` gate keeps the diagnostic sensor from flipping to OK so
    a still-outstanding per-tracker stale key is not masked. No escalation.
    """
    stub = _make_stub()
    stub._consecutive_decrypt_failures = 2
    stub._crypto_status_state = CryptoStatus.TRACKER_KEY_OUTDATED
    err = DecryptionError("own reports failed")

    result = stub._resolve_cycle_decrypt_outcome(
        cycle_decrypt_error=err,
        cycle_had_successful_decrypt=True,
        cycle_had_stale_key=True,
    )

    assert result is False
    assert stub._consecutive_decrypt_failures == 0
    # OK gate is suppressed by the stale-key flag: sensor stays as-is.
    assert stub._crypto_status_state == CryptoStatus.TRACKER_KEY_OUTDATED
    assert stub._last_decrypt_error is None


@pytest.mark.parametrize(
    ("error_factory", "expected_status"),
    [
        pytest.param(
            lambda: SharedKeyMissingError("missing"),
            CryptoStatus.SHARED_KEY_MISSING,
            id="missing",
        ),
        pytest.param(
            lambda: SharedKeyMismatchError("mismatch"),
            CryptoStatus.SHARED_KEY_INVALID,
            id="mismatch",
        ),
    ],
)
def test_resolve_account_wide_key_failure_escalates_despite_sibling(
    error_factory: Callable[[], DecryptionError],
    expected_status: CryptoStatus,
) -> None:
    """A shared-key failure stays account-wide even when a sibling decrypts.

    ``SharedKeyMissingError`` / ``SharedKeyMismatchError`` mean the bundle's shared
    key itself cannot unwrap the owner key, so a sibling decrypting from cached
    owner/EIK material does NOT prove the key healthy. Unlike a plain per-device
    ``DecryptionError``, positive proof must NOT downgrade it: the verdict has to
    advance the budget every cycle and escalate at the threshold, otherwise the
    broken device is stranded without a reauth prompt (the Codex finding).

    Mutation gate: the pre-fix gate (``not cycle_had_successful_decrypt`` only)
    returned False and cleared the budget here, so asserting an advancing counter
    and a threshold escalation turns red if the shared-key carve-out is removed.
    """
    stub = _make_stub()

    # Below threshold: account-wide failure counts up despite sibling success.
    for attempt in range(1, _MAX_DECRYPT_FAILURES):
        assert (
            stub._resolve_cycle_decrypt_outcome(
                cycle_decrypt_error=error_factory(),
                cycle_had_successful_decrypt=True,
                cycle_had_stale_key=False,
            )
            is False
        )
        assert stub._consecutive_decrypt_failures == attempt
    stub._set_auth_state.assert_not_called()
    # Sibling success must NOT heal the sensor while the shared key is broken; the
    # concrete subclass maps to its diagnostic status (note_decrypt_failure).
    assert stub._crypto_status_state == expected_status

    # Threshold reached -> escalate to reauth.
    assert (
        stub._resolve_cycle_decrypt_outcome(
            cycle_decrypt_error=error_factory(),
            cycle_had_successful_decrypt=True,
            cycle_had_stale_key=False,
        )
        is True
    )
    stub._set_auth_state.assert_called_once()


def test_prefer_account_wide_decrypt_error_upgrades_per_device_to_shared_key() -> None:
    """Capture priority: a shared-key failure wins over a per-device one.

    A mixed cycle can poll an offline device (plain ``DecryptionError``) before the
    device whose shared-key unwrap fails. First-capture-wins would let the resolver
    see only the per-device error and wrongly downgrade on sibling success, so the
    capture must upgrade to the account-wide subclass regardless of poll order.
    """
    per_device = DecryptionError("own reports failed")
    shared_key = SharedKeyMismatchError("stale shared key")

    # None seed -> take the first error, whatever its class.
    assert (
        PollingStub._prefer_account_wide_decrypt_error(None, per_device) is per_device
    )
    # Per-device captured first, shared-key seen later -> upgrade.
    assert (
        PollingStub._prefer_account_wide_decrypt_error(per_device, shared_key)
        is shared_key
    )
    # Shared-key captured first, per-device seen later -> keep the account-wide one.
    assert (
        PollingStub._prefer_account_wide_decrypt_error(shared_key, per_device)
        is shared_key
    )
    # Two account-wide errors -> stable, keep the first (order-independent).
    other_shared = SharedKeyMissingError("missing")
    assert (
        PollingStub._prefer_account_wide_decrypt_error(shared_key, other_shared)
        is shared_key
    )


# ---------------------------------------------------------------------------
# _decrypt_failure_is_account_wide: the single discriminator shared by the
# escalation verdict and the cycle-failure reconciliation. Pure predicate.
# ---------------------------------------------------------------------------


def test_account_wide_predicate_none_is_not_account_wide() -> None:
    """No decrypt error this cycle is never an account-wide failure."""
    stub = _make_stub()
    assert stub._decrypt_failure_is_account_wide(None, True) is False
    assert stub._decrypt_failure_is_account_wide(None, False) is False


def test_account_wide_predicate_without_sibling_is_account_wide() -> None:
    """Not a single device decrypted: the failure is account-wide by absence of
    any positive proof, whatever the concrete error class."""
    stub = _make_stub()
    assert (
        stub._decrypt_failure_is_account_wide(DecryptionError("offline"), False) is True
    )


def test_account_wide_predicate_per_device_with_sibling_is_local() -> None:
    """A plain per-device failure that a sibling success refutes is device-local."""
    stub = _make_stub()
    assert (
        stub._decrypt_failure_is_account_wide(DecryptionError("offline"), True) is False
    )


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: SharedKeyMissingError("missing"), id="missing"),
        pytest.param(lambda: SharedKeyMismatchError("mismatch"), id="mismatch"),
    ],
)
def test_account_wide_predicate_shared_key_survives_sibling(
    error_factory: Callable[[], DecryptionError],
) -> None:
    """A shared-key subclass stays account-wide even with sibling success: a
    sibling decrypting from cached material cannot refute a broken shared key."""
    stub = _make_stub()
    assert stub._decrypt_failure_is_account_wide(error_factory(), True) is True


# ---------------------------------------------------------------------------
# _finalize_cycle_decrypt_state: resolve the verdict AND reconcile the cycle's
# failure state with it. Regression suite for the Codex finding that a
# downgraded per-device decrypt error still marked the whole coordinator update
# failed every cycle (the eager except-block bookkeeping was never reconciled
# with the sibling-success downgrade).
# ---------------------------------------------------------------------------


def test_finalize_downgraded_per_device_failure_does_not_fail_cycle() -> None:
    """THE Codex finding: a per-device failure refuted by a sibling success must
    neither escalate NOR mark the coordinator update failed.

    Below the reauth threshold, a mixed cycle (offline device + sibling success)
    must leave ``cycle_failed`` False and ``last_exception`` None, so the finally
    block records ``last_poll_result='success'`` and never calls
    ``async_set_update_error``.

    Mutation gate: restoring the eager ``cycle_failed = True`` /
    ``last_exception = dec_err`` in the except block (or widening the account-wide
    guard to always-True) makes this return ``(True, <err>, ...)`` -- the assert
    turns red.
    """
    stub = _make_stub()
    stub._consecutive_decrypt_failures = _MAX_DECRYPT_FAILURES - 1
    err = DecryptionError("All own-report decryptions failed")

    cycle_failed, last_exception, reauth_exc = stub._finalize_cycle_decrypt_state(
        cycle_decrypt_error=err,
        cycle_had_successful_decrypt=True,
        cycle_had_stale_key=False,
        cycle_failed=False,
        last_exception=None,
    )

    assert cycle_failed is False
    assert last_exception is None
    assert reauth_exc is None
    # Sibling proof cleared the budget rather than advancing it.
    assert stub._consecutive_decrypt_failures == 0


def test_finalize_downgrade_preserves_independent_failure() -> None:
    """A co-occurring non-decrypt failure (e.g. a timeout) survives the downgrade.

    The benign decrypt path must leave an already-set ``cycle_failed`` /
    ``last_exception`` untouched -- it may only refrain from ADDING a failure, never
    clear a real one a sibling success does not refute.
    """
    stub = _make_stub()
    other_failure = TimeoutError("device timed out")

    cycle_failed, last_exception, reauth_exc = stub._finalize_cycle_decrypt_state(
        cycle_decrypt_error=DecryptionError("offline"),
        cycle_had_successful_decrypt=True,
        cycle_had_stale_key=False,
        cycle_failed=True,
        last_exception=other_failure,
    )

    assert cycle_failed is True
    assert last_exception is other_failure
    assert reauth_exc is None


def test_finalize_account_wide_below_threshold_fails_without_reauth() -> None:
    """A genuine account-wide failure below the threshold fails the cycle and
    surfaces the decrypt error, but does not yet escalate to reauth."""
    stub = _make_stub()
    err = DecryptionError("no device decrypted this cycle")

    cycle_failed, last_exception, reauth_exc = stub._finalize_cycle_decrypt_state(
        cycle_decrypt_error=err,
        cycle_had_successful_decrypt=False,
        cycle_had_stale_key=False,
        cycle_failed=False,
        last_exception=None,
    )

    assert cycle_failed is True
    assert last_exception is err
    assert reauth_exc is None
    assert stub._consecutive_decrypt_failures == 1


def test_finalize_account_wide_preserves_prior_independent_failure() -> None:
    """An account-wide failure below the threshold must not clobber a prior error.

    When another device already failed this cycle (e.g. a timeout on device A) and
    a shared-key failure surfaces on device B, the account-wide branch surfaces the
    cycle as failed but keeps the FIRST captured ``last_exception`` (the ``if
    last_exception is None`` guard). Mutation gate for that guard: dropping it makes
    ``last_exception`` the shared-key error and turns the assert red.
    """
    stub = _make_stub()
    other_failure = TimeoutError("device A timed out")
    err = SharedKeyMismatchError("device B: stale shared key")

    cycle_failed, last_exception, reauth_exc = stub._finalize_cycle_decrypt_state(
        cycle_decrypt_error=err,
        cycle_had_successful_decrypt=True,
        cycle_had_stale_key=False,
        cycle_failed=True,
        last_exception=other_failure,
    )

    assert cycle_failed is True
    assert last_exception is other_failure
    assert reauth_exc is None
    # Account-wide failure still advanced the consecutive-cycle budget.
    assert stub._consecutive_decrypt_failures == 1


def test_finalize_escalates_at_threshold_with_reauth_exc() -> None:
    """At the consecutive-cycle threshold the verdict escalates: finalize returns a
    ``ConfigEntryAuthFailed`` as both ``last_exception`` and the reauth signal, even
    when a sibling decrypted (shared-key subclass cannot be refuted)."""
    stub = _make_stub()
    stub._consecutive_decrypt_failures = _MAX_DECRYPT_FAILURES - 1
    err = SharedKeyMismatchError("stale shared key")

    cycle_failed, last_exception, reauth_exc = stub._finalize_cycle_decrypt_state(
        cycle_decrypt_error=err,
        cycle_had_successful_decrypt=True,
        cycle_had_stale_key=False,
        cycle_failed=False,
        last_exception=None,
    )

    assert cycle_failed is True
    assert isinstance(reauth_exc, ConfigEntryAuthFailed)
    assert last_exception is reauth_exc
    stub._set_auth_state.assert_called_once()


def test_finalize_clean_cycle_stays_success() -> None:
    """A clean cycle (no decrypt error, sibling success) stays success and heals the
    diagnostic sensor without failing the coordinator update."""
    stub = _make_stub()
    stub._consecutive_decrypt_failures = 2

    cycle_failed, last_exception, reauth_exc = stub._finalize_cycle_decrypt_state(
        cycle_decrypt_error=None,
        cycle_had_successful_decrypt=True,
        cycle_had_stale_key=False,
        cycle_failed=False,
        last_exception=None,
    )

    assert cycle_failed is False
    assert last_exception is None
    assert reauth_exc is None
    assert stub._consecutive_decrypt_failures == 0
    assert stub._crypto_status_state == CryptoStatus.OK
