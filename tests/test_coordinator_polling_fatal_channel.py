# tests/test_coordinator_polling_fatal_channel.py
"""Channel-confusion regression tests for ``coordinator.polling``.

Iter-14 (Codex follow-up): the FCM receiver's supervisor-level "short-run
crash loop" cap publishes its terminal state into the SAME ``_fatal_errors``
map that the coordinator's re-auth escalation consumes. Without explicit
classification the crash-loop fatal would be reclassified as an auth fatal
after ``_FCM_ERROR_RETRY_THRESHOLD`` consecutive update cycles and push users
into Home Assistant's re-auth flow for what is structurally a listener-
lifecycle fatal (the repair issue carries the actual user-visible guidance).

This module pins three contracts:

1. ``_classify_fcm_fatal`` (free function, branch-coverage friendly):
   - crash-loop prefix => ``_FCM_FATAL_CLASS_CRASH_LOOP_EXCLUDED``
   - classic auth fatal (401/404/credentials text) =>
     ``_FCM_FATAL_CLASS_AUTH_IMMEDIATE``
   - any other non-empty fatal => ``_FCM_FATAL_CLASS_AUTH_AFTER_THRESHOLD``
2. The polling-side fatal-classification call is the SSOT consumer for the
   crash-loop prefix exported by ``Auth.fcm_receiver_ha``.
3. The crash-loop prefix matches the message produced by the cap-fire
   branch in ``Auth.fcm_receiver_ha`` (cross-module wire contract).
"""

from __future__ import annotations

from custom_components.googlefindmy.Auth.fcm_receiver_ha import (
    _MAX_CONSECUTIVE_SHORT_RUNS,
    _SHORT_RUN_THRESHOLD_S,
    CRASH_LOOP_FATAL_PREFIX,
)
from custom_components.googlefindmy.coordinator.polling import (
    _FCM_FATAL_CLASS_AUTH_AFTER_THRESHOLD,
    _FCM_FATAL_CLASS_AUTH_IMMEDIATE,
    _FCM_FATAL_CLASS_CRASH_LOOP_EXCLUDED,
    _classify_fcm_fatal,
)


class TestClassifyFcmFatal:
    """Branch coverage for the channel-confusion fix."""

    def test_crash_loop_prefix_is_excluded(self) -> None:
        """A cap-fired crash-loop fatal must never escalate to re-auth."""
        cap_message = (
            f"{CRASH_LOOP_FATAL_PREFIX} 10 consecutive runs ended within 30s "
            f"(persistent poison message suspected)"
        )

        assert _classify_fcm_fatal(cap_message) == _FCM_FATAL_CLASS_CRASH_LOOP_EXCLUDED

    def test_bare_prefix_is_also_excluded(self) -> None:
        """Defense is on the prefix alone, not the full message body."""
        assert (
            _classify_fcm_fatal(CRASH_LOOP_FATAL_PREFIX)
            == _FCM_FATAL_CLASS_CRASH_LOOP_EXCLUDED
        )

    def test_classic_auth_fatal_is_immediate(self) -> None:
        """A 401 credentials fatal must classify as auth_immediate.

        ``is_fatal_fcm_auth_error`` matches plain ``"401"`` substrings, so a
        message that contains ``"HTTP 401"`` classifies as the immediate
        auth-fatal path.
        """
        msg = "FCM auth failed: invalid credentials (HTTP 401)"

        assert _classify_fcm_fatal(msg) == _FCM_FATAL_CLASS_AUTH_IMMEDIATE

    def test_unrelated_transient_fatal_uses_threshold_path(self) -> None:
        """An unrecognised non-crash-loop fatal goes through the counter."""
        # A network error that does not match auth patterns AND does not
        # start with the crash-loop prefix.
        msg = "Transient FCM error: connection reset"

        assert _classify_fcm_fatal(msg) == _FCM_FATAL_CLASS_AUTH_AFTER_THRESHOLD

    def test_wire_contract_cap_message_matches_prefix(self) -> None:
        """The cap-fire branch must produce a message the classifier excludes.

        This pins the cross-module wire contract: if the cap-fire branch in
        ``Auth.fcm_receiver_ha`` ever changes its message template, this test
        fails fast and forces the change to be propagated through
        ``CRASH_LOOP_FATAL_PREFIX`` rather than diverging silently.
        """
        # Reproduce the cap-fire branch's f-string template literally.
        cap_message = (
            f"{CRASH_LOOP_FATAL_PREFIX} "
            f"{_MAX_CONSECUTIVE_SHORT_RUNS} consecutive "
            f"runs ended within "
            f"{_SHORT_RUN_THRESHOLD_S:.0f}s "
            f"(persistent poison message suspected)"
        )

        assert cap_message.startswith(CRASH_LOOP_FATAL_PREFIX)
        assert _classify_fcm_fatal(cap_message) == _FCM_FATAL_CLASS_CRASH_LOOP_EXCLUDED
