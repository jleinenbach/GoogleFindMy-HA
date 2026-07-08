# tests/test_fcmpushclient_poison_message.py
"""Defense 1 (CA-CASCADING-FAILURE-001) Inner-Loop-Hardening tests.

Verifies that ``FcmPushClient._listen`` survives per-message decode failures
(``binascii.Error`` padding faults under Python 3.14 strict mode,
``RuntimeError`` from ``_app_data_by_key`` key lookups, generic ``ValueError``
decode errors) by skipping the offending message with a selective-ack instead
of crashing the worker loop. Also verifies the cred-error propagation contract:
``CredentialDecryptionError`` (credential material is missing, structurally
invalid, fails base64 decode, or fails DER private-key parse) is re-raised so
the supervisor surfaces it via STOPPED. Plain ``ValueError`` without that type
is treated as per-message poison (selective-ACK + skip).

The Aggregate-Anti-Cascading-Invariant test runs 100 poison messages followed
by one valid sentinel and proves Defense 1 prevents the supervisor-restart
cascade reported on Home Assistant Core 2026.6.x.
"""

from __future__ import annotations

import binascii
import logging
import ssl
from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import http_ece
from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    _MAX_CONSECUTIVE_DECRYPT_FAILURES,
    CredentialDecryptionError,
    FcmPushClientRunState,
)
from tests.helpers.fcm_poison_stub import (
    FcmPushClientSlim,
    make_poison_data_message,
    make_valid_data_message,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _stream_messages(
    client: FcmPushClientSlim, messages: list[SimpleNamespace]
) -> AsyncMock:
    """Build an ``_receive_msg`` mock that yields ``messages`` then stops the loop.

    After the last message the next call returns ``None`` *and* sets
    ``client.do_listen = False`` so the worker leaves the ``while`` cleanly,
    just as a real stream end would. Without this the test would hang.
    """
    it: Iterator[SimpleNamespace] = iter(messages)

    async def _next() -> SimpleNamespace | None:
        try:
            return next(it)
        except StopIteration:
            client.do_listen = False
            return None

    mock = AsyncMock(side_effect=_next)
    return mock


def _make_handle_side_effect(
    plan: list[BaseException | None],
    record_handled: list[str] | None = None,
) -> AsyncMock:
    """Build a ``_handle_message`` mock that walks ``plan`` per call.

    Each list entry is either an exception instance (raised on that call) or
    ``None`` (treated as a successful decrypt). If ``record_handled`` is given,
    successful decrypts append ``msg.persistent_id`` to it.
    """
    it = iter(plan)

    async def _impl(msg: SimpleNamespace) -> None:
        try:
            step = next(it)
        except StopIteration as exc:  # plan exhausted -> test bug
            raise AssertionError(
                "_handle_message called more often than the plan allows"
            ) from exc
        if isinstance(step, BaseException):
            raise step
        if record_handled is not None:
            record_handled.append(msg.persistent_id)

    return AsyncMock(side_effect=_impl)


def _outer_catch_was_hit(caplog: pytest.LogCaptureFixture) -> bool:
    """True iff the ``except Exception`` block at the bottom of ``_listen`` ran.

    Defense 1 must keep the loop from ever reaching that block, so this is the
    central anti-cascading invariant.
    """
    return any(
        record.levelno == logging.ERROR
        and "Unknown error in listener" in record.getMessage()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Defense 1: per-message decode failures must not stop the worker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poison_message_does_not_stop_worker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``binascii.Error`` decrypt failure leaves the outer-catch untouched."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-001", kind="padding")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")]
    )

    await client._listen()

    assert not _outer_catch_was_hit(caplog)
    assert client._send_selective_ack.await_count == 1


@pytest.mark.asyncio
async def test_poison_message_triggers_selective_ack(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The skipped message is acknowledged so the FCM server stops resending."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-042", kind="padding")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")]
    )

    await client._listen()

    client._send_selective_ack.assert_awaited_once_with("poison-042")


@pytest.mark.asyncio
async def test_poison_message_logs_warning_not_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The skip path emits a rate-limited WARN, never a traceback ERROR."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-003", kind="padding")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")]
    )

    await client._listen()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert errors == []
    assert len(warnings) >= 1


@pytest.mark.asyncio
async def test_poison_message_continues_to_next_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After skipping a poison message the loop processes the next one normally."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    poison = make_poison_data_message("poison-004", kind="padding")
    valid = make_valid_data_message("valid-005")
    client._receive_msg = _stream_messages(client, [poison, valid])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding"), None]
    )

    await client._listen()

    assert client._handle_message.await_count == 2
    # First call: poison; second call: valid message passed through.
    second_call_args = client._handle_message.await_args_list[1].args
    assert second_call_args[0].persistent_id == "valid-005"


@pytest.mark.asyncio
async def test_runtime_error_from_app_data_by_key_caught(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``RuntimeError`` from ``_app_data_by_key`` is treated as poison-class."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-006", kind="value")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [RuntimeError("couldn't find in app_data crypto-key")]
    )

    await client._listen()

    assert not _outer_catch_was_hit(caplog)
    client._send_selective_ack.assert_awaited_once_with("poison-006")


@pytest.mark.asyncio
async def test_ece_exception_from_body_decrypt_caught(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``http_ece.ECEException`` (corrupt body / bad auth tag) is poison-class.

    Regression for Codex Finding 2: ``http_ece.decrypt`` raises
    ``http_ece.ECEException``, which is a SIBLING of ``ValueError`` under
    ``Exception`` -- not a subclass. The old ``except (ValueError, ...)``
    poison arm let it bypass the selective-ACK + skip path entirely, so a
    single corrupt encrypted body was redelivered forever and could
    eventually trip the short-run crash cap. It must be treated as
    per-message poison: ACK + skip, outer-catch untouched.
    """
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-ece-012", kind="value")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [http_ece.ECEException("Decryption error: bad auth tag")]
    )

    await client._listen()

    assert not _outer_catch_was_hit(caplog)
    client._send_selective_ack.assert_awaited_once_with("poison-ece-012")
    # Not a credential fault: no credential signal must be recorded.
    assert client.credential_error is None


@pytest.mark.asyncio
async def test_rate_limit_kicks_in_after_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``log_warn_limit`` caps WARN spam at default 5 while still acking each message."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    # Default log_warn_limit in FcmPushClientConfig is 5.
    assert client.config.log_warn_limit == 5

    messages = [
        make_poison_data_message(f"poison-{i:03d}", kind="padding") for i in range(20)
    ]
    client._receive_msg = _stream_messages(client, messages)
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")] * 20
    )

    await client._listen()

    # Format string identical for every iteration -> rate limiter applies once.
    skip_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "Skipping FCM message that failed to decrypt" in r.getMessage()
    ]
    assert len(skip_warnings) == 5
    # All 20 messages must still be acknowledged (rate limit applies to logs only).
    assert client._send_selective_ack.await_count == 20


@pytest.mark.asyncio
async def test_writer_dead_during_selective_ack_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An OSError during ack-send stops the loop so the supervisor reconnects."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-007", kind="padding")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")]
    )
    client._send_selective_ack = AsyncMock(side_effect=OSError("writer half-dead"))

    await client._listen()

    assert client.do_listen is False


@pytest.mark.asyncio
async def test_ssl_error_during_selective_ack_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ssl.SSLError`` is not always an ``OSError`` subclass -> separate catch."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-008", kind="padding")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")]
    )
    client._send_selective_ack = AsyncMock(side_effect=ssl.SSLError("tls dead"))

    await client._listen()

    assert client.do_listen is False


@pytest.mark.asyncio
async def test_connection_error_during_selective_ack_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``ConnectionError`` during ack triggers the same shutdown path."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-009", kind="padding")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")]
    )
    client._send_selective_ack = AsyncMock(side_effect=ConnectionError("peer gone"))

    await client._listen()

    assert client.do_listen is False


@pytest.mark.asyncio
async def test_credential_decryption_error_surfaces_distinct_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cred/key-material errors surface a DISTINCT credential signal.

    CredentialDecryptionError (typed exception) propagates past the
    per-message poison catch (iter-3 contract: the discriminator is the
    exception TYPE). It is then caught by ``_listen``'s dedicated
    ``except CredentialDecryptionError`` arm, which records
    ``client.credential_error`` and logs a credential-specific error --
    NOT the generic "Unknown error in listener" outer-catch (Codex
    Finding 1). The supervisor reads ``credential_error`` to invalidate
    the bad key material and re-register, instead of restarting against
    the same poison credentials until the short-run crash cap fires.
    """
    caplog.set_level(logging.DEBUG)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-010", kind="value")
    client._receive_msg = _stream_messages(client, [msg])
    cred_err = CredentialDecryptionError("Credentials missing FCM key material")
    client._handle_message = _make_handle_side_effect([cred_err])

    await client._listen()

    # (a) The generic outer ``except Exception`` arm must NOT have run: a
    #     credential fault is no longer degraded to "Unknown error".
    assert not _outer_catch_was_hit(caplog)
    # (b) The distinct credential signal is recorded for the supervisor.
    assert client.credential_error is cred_err
    # (c) A credential-specific ERROR is logged (re-registration required).
    cred_errors = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR
        and "credential material is corrupt" in r.getMessage()
    ]
    assert len(cred_errors) == 1
    # (d) Selective-ack must NOT be sent (config fault, not per-message poison).
    assert client._send_selective_ack.await_count == 0
    # (e) Worker reaches its terminal state via the finally block.
    assert client.run_state == FcmPushClientRunState.STOPPED
    assert client.do_listen is False


@pytest.mark.asyncio
async def test_unmarked_value_error_is_treated_as_per_message_poison(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Plain ValueError without CredentialDecryptionError type = per-message poison.

    Regression for the iter-2 stealth-drop class (CA-CRED-VS-MSG-DECRYPT-001
    iter-3): under the old string-prefix marker discipline a plain ValueError
    from upstream libraries (e.g. cryptography.load_der_private_key raising
    "Could not deserialize key data") would silently drop EVERY incoming
    message while the listener stayed "healthy". After the iter-3 fix the
    discriminator is the exception TYPE: untyped ValueError is treated as
    per-message poison (ACK + skip), only CredentialDecryptionError escalates.
    """
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-011", kind="value")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [
            ValueError(
                "Could not deserialize key data (the data may be in an "
                "incorrect format, the provided password may be incorrect, "
                "it may be encrypted with an unsupported algorithm, or it "
                "may be an unsupported key type)"
            )
        ]
    )

    await client._listen()

    # Per-message-poison path: outer catch NOT hit, ACK sent, worker keeps running
    # until the stream end (do_listen=False from _stream_messages).
    assert not _outer_catch_was_hit(caplog)
    client._send_selective_ack.assert_awaited_once_with("poison-011")


# ---------------------------------------------------------------------------
# Aggregate Anti-Cascading-Invariant (Iteration-2-L1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_cascading_on_repeated_poison_messages(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """100 poison messages must not cascade into a supervisor restart storm.

    Proves the bug class as a whole: alternating ``binascii.Error`` and
    ``RuntimeError`` failures on 100 consecutive messages keep the worker in
    its operational state, every message is acked in order, and the
    outer-catch is never entered. A final valid sentinel message is processed
    normally to show the worker is still responsive after the cascade attempt.
    """
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()

    poison_messages = [
        make_poison_data_message(
            persistent_id=f"poison-{i:03d}",
            kind="padding" if i % 2 == 0 else "value",
        )
        for i in range(100)
    ]
    sentinel = make_valid_data_message("valid-001")
    messages = poison_messages + [sentinel]

    handled: list[str] = []

    # Per-iteration run_state snapshot via _handle_message side_effect.
    iter_states: list[FcmPushClientRunState] = []

    plan: list[BaseException | None] = []
    for i in range(100):
        if i % 2 == 0:
            plan.append(binascii.Error("Incorrect padding"))
        else:
            plan.append(RuntimeError("couldn't find in app_data crypto-key"))
    plan.append(None)  # sentinel: valid message passes through

    plan_iter = iter(plan)

    async def _instrumented_handle(msg: SimpleNamespace) -> None:
        iter_states.append(client.run_state)
        try:
            step = next(plan_iter)
        except StopIteration as exc:
            raise AssertionError("plan exhausted") from exc
        if isinstance(step, BaseException):
            raise step
        handled.append(msg.persistent_id)

    client._receive_msg = _stream_messages(client, messages)
    client._handle_message = AsyncMock(side_effect=_instrumented_handle)

    await client._listen()

    # (a) run_state stays STARTED throughout the poison cascade.
    assert all(state == FcmPushClientRunState.STARTED for state in iter_states), (
        f"run_state drift during cascade: {iter_states!r}"
    )
    # (b) All 100 poison messages acked in order.
    expected_acks = [f"poison-{i:03d}" for i in range(100)]
    actual_acks = [call.args[0] for call in client._send_selective_ack.await_args_list]
    assert actual_acks == expected_acks
    # (c) Outer catch never entered.
    assert not _outer_catch_was_hit(caplog)
    # (d) Sentinel valid message processed normally after the cascade.
    assert handled == ["valid-001"]


# ---------------------------------------------------------------------------
# Stale-key escalation (Codex follow-up on PR #181): a sustained run of
# http_ece.ECEException means the stored DH/auth-secret keys no longer match
# the server registration. A single ECE stays per-message poison; a run of
# _MAX_CONSECUTIVE_DECRYPT_FAILURES escalates to CredentialDecryptionError so
# the supervisor re-registers instead of ACKing every push forever.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sustained_ece_failures_escalate_to_credential_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A run of ``_MAX_CONSECUTIVE_DECRYPT_FAILURES`` ECEs escalates to re-register.

    Codex follow-up on PR #181 ("do not ACK every ECE decrypt failure"): when
    the stored key material is syntactically valid but stale/mismatched, every
    push fails the auth tag with ``http_ece.ECEException``. ACKing each one
    forever leaves ``credential_error`` unset, so the supervisor never
    re-registers and push updates silently stop. The sustained run must surface
    as ``CredentialDecryptionError`` instead.
    """
    caplog.set_level(logging.ERROR)
    client = FcmPushClientSlim()
    n = _MAX_CONSECUTIVE_DECRYPT_FAILURES
    messages = [
        make_poison_data_message(f"ece-{i:03d}", kind="value") for i in range(n)
    ]
    client._receive_msg = _stream_messages(client, messages)
    client._handle_message = _make_handle_side_effect(
        [http_ece.ECEException("Decryption error: bad auth tag")] * n
    )

    await client._listen()

    # (a) The threshold message escalated: a credential signal is recorded so
    #     the supervisor invalidates the stale tokens and re-registers.
    assert isinstance(client.credential_error, CredentialDecryptionError)
    assert "stale" in str(client.credential_error)
    # (b) It is NOT degraded to the generic "Unknown error" outer-catch.
    assert not _outer_catch_was_hit(caplog)
    # (c) The first n-1 were ACK+skipped (poison); the nth escalated instead of
    #     ACKing, so exactly n-1 selective-acks were sent.
    assert client._send_selective_ack.await_count == n - 1
    # (d) Worker reaches its terminal state and stops listening.
    assert client.run_state == FcmPushClientRunState.STOPPED
    assert client.do_listen is False


@pytest.mark.asyncio
async def test_ece_failures_below_threshold_stay_poison(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_MAX_CONSECUTIVE_DECRYPT_FAILURES - 1`` ECEs stay per-message poison.

    Below the escalation threshold the listener must keep ACKing+skipping (a
    handful of genuinely corrupt bodies is not a credential fault), so the
    single-ECE poison contract (Codex Finding 2) is preserved for short runs.
    """
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    n = _MAX_CONSECUTIVE_DECRYPT_FAILURES - 1
    messages = [
        make_poison_data_message(f"ece-{i:03d}", kind="value") for i in range(n)
    ]
    client._receive_msg = _stream_messages(client, messages)
    client._handle_message = _make_handle_side_effect(
        [http_ece.ECEException("Decryption error: bad auth tag")] * n
    )

    await client._listen()

    # No escalation: every message ACK+skipped, no credential signal recorded.
    assert client.credential_error is None
    assert client._send_selective_ack.await_count == n
    assert not _outer_catch_was_hit(caplog)


@pytest.mark.asyncio
async def test_non_ece_poison_never_escalates(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``binascii.Error``/``RuntimeError`` are per-message and never re-register.

    Only ``http_ece.ECEException`` (the auth-tag/body surface, which depends on
    the stored DH/auth-secret keys) signals stale credentials. Header decode and
    app_data lookup faults vary per message and must NOT accumulate toward
    re-registration, even far beyond the ECE escalation threshold.
    """
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    count = _MAX_CONSECUTIVE_DECRYPT_FAILURES * 3
    messages = [
        make_poison_data_message(f"p-{i:03d}", kind="padding") for i in range(count)
    ]
    client._receive_msg = _stream_messages(client, messages)
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")] * count
    )

    await client._listen()

    assert client.credential_error is None
    assert client._send_selective_ack.await_count == count
    assert not _outer_catch_was_hit(caplog)


# ---------------------------------------------------------------------------
# RMQ2 dedup parity for the poison path (Codex follow-up on PR #1173): a
# selective-acked poison push must also be recorded in ``persistent_ids`` so the
# next ``_login`` reports it in ``received_persistent_id``. The decrypt-failure
# raise unwinds ``_handle_message`` before its own append, so the ack site in
# ``_process_one_inbound_message`` records it instead -- but only on a real ack.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poison_ack_records_persistent_id_for_rmq2(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A selective-acked poison push is recorded in ``persistent_ids`` (RMQ2 dedup).

    Without this the next ``_login`` omits the id from ``received_persistent_id``,
    so the server replays the same undecryptable push across reconnects (repeated
    decrypt failures drifting toward a bogus re-registration).
    """
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-rmq2-001", kind="padding")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")]
    )

    await client._listen()

    client._send_selective_ack.assert_awaited_once_with("poison-rmq2-001")
    assert client.persistent_ids == ["poison-rmq2-001"]


@pytest.mark.asyncio
async def test_dead_writer_poison_not_recorded_for_rmq2(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A poison push whose ack fails (dead writer) is NOT recorded for RMQ2.

    ``_ack_or_disconnect`` returns ``False`` when the writer half is dead, so the
    push was never acknowledged. Recording it would wrongly tell RMQ2 the server
    may drop it; instead the supervisor reconnects, the server replays it, and a
    later successful ack records it then. ``persistent_ids`` must stay empty.
    """
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-rmq2-002", kind="padding")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [binascii.Error("Incorrect padding")]
    )
    client._send_selective_ack = AsyncMock(side_effect=OSError("writer half-dead"))

    await client._listen()

    assert client.do_listen is False
    assert client.persistent_ids == []


@pytest.mark.asyncio
async def test_escalating_ece_not_recorded_but_prior_acks_are(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On stale-key escalation only the acked ECEs are recorded, not the escalating one.

    The nth consecutive ECE raises ``CredentialDecryptionError`` *before* the ack
    site, so it is never acked and never recorded. The first n-1 were acked, so
    ``persistent_ids`` holds exactly those n-1 ids for RMQ2 dedup.
    """
    caplog.set_level(logging.ERROR)
    client = FcmPushClientSlim()
    n = _MAX_CONSECUTIVE_DECRYPT_FAILURES
    messages = [
        make_poison_data_message(f"ece-rmq2-{i:03d}", kind="value") for i in range(n)
    ]
    client._receive_msg = _stream_messages(client, messages)
    client._handle_message = _make_handle_side_effect(
        [http_ece.ECEException("Decryption error: bad auth tag")] * n
    )

    await client._listen()

    assert isinstance(client.credential_error, CredentialDecryptionError)
    expected = [f"ece-rmq2-{i:03d}" for i in range(n - 1)]
    assert client.persistent_ids == expected
