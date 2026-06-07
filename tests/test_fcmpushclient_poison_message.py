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

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
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
async def test_rate_limit_kicks_in_after_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``log_warn_limit`` caps WARN spam at default 5 while still acking each message."""
    caplog.set_level(logging.WARNING)
    client = FcmPushClientSlim()
    # Default log_warn_limit in FcmPushClientConfig is 5.
    assert client.config.log_warn_limit == 5

    messages = [
        make_poison_data_message(f"poison-{i:03d}", kind="padding")
        for i in range(20)
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
async def test_credential_decryption_error_propagates_to_outer_catch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cred/key-material errors must NOT be swallowed by Defense 1.

    CredentialDecryptionError (typed exception) propagates past the
    per-message ValueError catch. This is the iter-3 contract: the
    discriminator is the exception TYPE, not a string-prefix marker.
    """
    caplog.set_level(logging.DEBUG)
    client = FcmPushClientSlim()
    msg = make_poison_data_message("poison-010", kind="value")
    client._receive_msg = _stream_messages(client, [msg])
    client._handle_message = _make_handle_side_effect(
        [CredentialDecryptionError("Credentials missing FCM key material")]
    )

    await client._listen()

    # The outer ``except Exception`` block must have logged the error so the
    # supervisor knows the worker stopped intentionally; selective-ack must NOT
    # have been sent (this is a config fault, not a per-message poison).
    assert _outer_catch_was_hit(caplog)
    assert client._send_selective_ack.await_count == 0
    # Worker reaches its terminal state via the finally block.
    assert client.run_state == FcmPushClientRunState.STOPPED


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
        [ValueError("Could not deserialize key data (the data may be in an "
                    "incorrect format, the provided password may be incorrect, "
                    "it may be encrypted with an unsupported algorithm, or it "
                    "may be an unsupported key type)")]
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
