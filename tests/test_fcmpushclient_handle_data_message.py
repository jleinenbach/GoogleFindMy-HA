# tests/test_fcmpushclient_handle_data_message.py
"""Branch-coverage suite for ``FcmPushClient._handle_data_message``.

Covers the 8 branch groups of the method (plan R2-R8) that no existing test
exercises: the poison stub mocks ``_handle_message`` away, so this method ran
~0 % on the unit level before. Each test forces exactly one branch group via
the additive ``FcmHandleSlim`` builder (composition, real unbound method bound
to a light container) and a controlled ``_decrypt_raw_data`` return value.

The missing-credentials guard (R4) is exercised with the real production
states ``None``/``{}`` after the accompanying guard reorder in
``fcmpushclient.py`` moves the ``if not self.credentials: return`` check ahead
of the ``["gcm"]["app_id"]`` dereference; the poison stub is left untouched.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    _MAX_CONSECUTIVE_DECRYPT_FAILURES,
    ErrorType,
)
from tests.helpers.fcm_handle_stub import (
    FcmHandleSlim,
    make_data_message,
)


def _set_decrypt(client: FcmHandleSlim, payload: bytes) -> None:
    """Pin the instance ``_decrypt_raw_data`` to a fixed ``bytes`` payload.

    Set as an instance attribute (not bound), so the production 4-arg call
    ``self._decrypt_raw_data(self.credentials, crypto_key, salt, raw_data)``
    resolves without ``self`` binding.
    """
    client._decrypt_raw_data = (  # type: ignore[attr-defined]
        lambda credentials, crypto_key, salt, raw_data: payload
    )


class TestHandleDataMessage:
    """One test per branch group of ``_handle_data_message`` (lines 579-655)."""

    def test_deleted_messages_early_return(self) -> None:
        """``message_type == deleted_messages`` -> early return, no callback (R2)."""
        client = FcmHandleSlim()
        msg = make_data_message(message_type="deleted_messages")

        assert client._handle_data_message(msg) is False

        assert not client.callback.called
        assert client.warnings == []

    def test_subtype_mismatch_drops_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``subtype`` != registered app id -> DEBUG log + DROP before decrypt (R3 True).

        A subtype mismatch means the push was encrypted for a *superseded* GCM
        registration (an old client still listening after an FCM re-registration
        minted a fresh ``wp:...#<uuid>`` app_id). Its key material can never
        match, so decrypting would fail with a guaranteed ``ECEException`` /
        InvalidTag; that failure is caught upstream and counts toward
        ``_consecutive_decrypt_failures``, escalating to a bogus re-registration
        cascade. The handler therefore returns after logging.

        Contract: dropping a foreign-subtype push is an *expected, benign* event,
        so it is logged at DEBUG, never WARNING -- surfacing it at warning level
        only alarmed users about a non-issue. This test pins that level: the
        message appears at DEBUG and ``warnings`` stays empty. Regression guard:
        ``_decrypt_raw_data`` is left unset, so a fall-through would raise
        ``AttributeError`` and fail loudly; the callback must not fire.
        """
        client = FcmHandleSlim()  # credentials gcm.app_id == "APPID"
        msg = make_data_message(subtype="OTHER")

        with caplog.at_level(logging.DEBUG):
            delivered = client._handle_data_message(msg)

        # A dropped foreign-subtype push is not a delivery: the caller must
        # selective-ack it but must NOT record it in ``persistent_ids`` (Codex P2).
        assert delivered is False
        # Emitted at DEBUG, never WARNING (benign, expected event).
        assert any(
            "does not match" in r.getMessage() and r.levelno == logging.DEBUG
            for r in caplog.records
        )
        assert client.warnings == []
        # Positively pin the "never WARNING" contract, not just the empty
        # ``warnings`` list: no record on this path may reach WARNING level.
        assert not any(r.levelno == logging.WARNING for r in caplog.records)
        # Dropped before decrypt/callback: foreign-subtype push is not for us.
        assert not client.callback.called

    def test_subtype_match_no_warning(self) -> None:
        """``subtype`` == registered app id -> no mismatch warning (R3 False)."""
        client = FcmHandleSlim()
        _set_decrypt(client, b'{"ok": true}')
        msg = make_data_message(subtype="APPID")

        assert client._handle_data_message(msg) is True

        assert client.warnings == []
        assert client.callback.called

    @pytest.mark.parametrize("credentials", [None, {}], ids=["none", "empty"])
    def test_missing_credentials_guard_returns(self, credentials: Any) -> None:
        """Missing credentials (``None`` or ``{}``) -> guard returns early (R4).

        After the production reorder the ``if not self.credentials: return``
        guard sits *before* the ``["gcm"]["app_id"]`` dereference, so the real
        production states reach it without raising: ``None`` (un-registered
        client) and ``{}`` (cleared credentials). No artificial falsy dict is
        needed. ``_decrypt_raw_data`` is left unset so any accidental call past
        the guard would raise ``AttributeError`` and fail the test loudly.
        """
        client = FcmHandleSlim(credentials=credentials)
        msg = make_data_message(subtype="APPID")

        assert client._handle_data_message(msg) is False

        assert not client.callback.called
        assert client.warnings == []

    def test_payload_dict_passed_through(self) -> None:
        """JSON object payload -> forwarded to callback verbatim (R5a)."""
        client = FcmHandleSlim()
        _set_decrypt(client, b'{"k": "v"}')
        msg = make_data_message()

        assert client._handle_data_message(msg) is True

        client.callback.assert_called_once()
        ret_val = client.callback.call_args.args[0]
        assert ret_val == {"k": "v"}

    def test_payload_non_dict_json_wrapped_raw_json(self) -> None:
        """Non-object JSON -> warning + ``{"_raw_json": ...}`` wrap (R5b)."""
        client = FcmHandleSlim()
        _set_decrypt(client, b"[1, 2, 3]")
        msg = make_data_message()

        client._handle_data_message(msg)

        assert any("not an object" in w for w in client.warnings)
        ret_val = client.callback.call_args.args[0]
        assert ret_val == {"_raw_json": [1, 2, 3]}

    def test_payload_text_not_json_wrapped_raw_text(self) -> None:
        """Decodable non-JSON text -> ``{"_raw_text": ...}`` (R5c, covers R7)."""
        client = FcmHandleSlim()
        _set_decrypt(client, b"plain text")
        msg = make_data_message()

        client._handle_data_message(msg)

        ret_val = client.callback.call_args.args[0]
        assert ret_val == {"_raw_text": "plain text"}

    def test_payload_non_utf8_wrapped_raw_bytes(self) -> None:
        """Non-UTF-8 payload -> ``{"_raw_bytes": hex}`` (R5d, covers R6)."""
        client = FcmHandleSlim()
        _set_decrypt(client, b"\xff\xfe")
        msg = make_data_message()

        client._handle_data_message(msg)

        ret_val = client.callback.call_args.args[0]
        assert ret_val == {"_raw_bytes": "fffe"}

    def test_callback_success_resets_error_count(self) -> None:
        """Callback succeeds -> ``_reset_error_count(NOTIFY)``, no increment (R8 success)."""
        client = FcmHandleSlim()
        _set_decrypt(client, b'{"k": "v"}')
        msg = make_data_message()

        client._handle_data_message(msg)

        client._reset_error_count.assert_called_once_with(ErrorType.NOTIFY)
        assert not client._try_increment_error_count.called

    def test_callback_exception_increments_error_count(self) -> None:
        """Callback raises -> ``logger.exception`` + increment, no reset (R8 except)."""
        client = FcmHandleSlim()
        client.callback.side_effect = RuntimeError("boom")
        _set_decrypt(client, b'{"k": "v"}')
        msg = make_data_message()

        # Delivered = decrypted + dispatched; a raising HA callback still counts as
        # a delivery (the payload reached the callback), matching prior semantics.
        assert client._handle_data_message(msg) is True

        client._try_increment_error_count.assert_called_once_with(ErrorType.NOTIFY)
        assert not client._reset_error_count.called

    def test_successful_decrypt_resets_stale_key_failure_counter(self) -> None:
        """A successful decrypt clears the consecutive-ECE escalation counter.

        Codex follow-up on PR #181: the stale-key counter
        (``_consecutive_decrypt_failures``) only escalates on an UNINTERRUPTED
        run of auth-tag failures. A real decrypt success proves the stored keys
        still match the server, so it must reset the counter -- otherwise an
        isolated corrupt body weeks apart could eventually force an unwarranted
        re-registration. The reset is gated on a real decrypt success here (not
        on ``_handle_message`` generally), so heartbeats cannot reset it.
        """
        client = FcmHandleSlim()
        # One short of the escalation threshold, as a prior run would leave it.
        client._consecutive_decrypt_failures = _MAX_CONSECUTIVE_DECRYPT_FAILURES - 1
        _set_decrypt(client, b'{"ok": true}')
        msg = make_data_message(subtype="APPID")

        client._handle_data_message(msg)

        assert client._consecutive_decrypt_failures == 0
        assert client.callback.called


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
