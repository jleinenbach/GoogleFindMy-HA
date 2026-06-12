# tests/test_fcmpushclient_handle_data_message.py
"""Branch-coverage suite for ``FcmPushClient._handle_data_message``.

Covers the 8 branch groups of the method (plan R2-R8) that no existing test
exercises: the poison stub mocks ``_handle_message`` away, so this method ran
~0 % on the unit level before. Each test forces exactly one branch group via
the additive ``FcmHandleSlim`` builder (composition, real unbound method bound
to a light container) and a controlled ``_decrypt_raw_data`` return value.

No production change; ``fcm_poison_stub.py`` is not edited.
"""
from __future__ import annotations

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    ErrorType,
)
from tests.helpers.fcm_handle_stub import (
    FalsyCredentials,
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

        client._handle_data_message(msg)

        assert not client.callback.called
        assert client.warnings == []

    def test_subtype_mismatch_logs_warning(self) -> None:
        """``subtype`` != registered app id -> warning, processing continues (R3 True)."""
        client = FcmHandleSlim()  # credentials gcm.app_id == "APPID"
        _set_decrypt(client, b'{"ok": true}')
        msg = make_data_message(subtype="OTHER")

        client._handle_data_message(msg)

        assert any("does not match" in w for w in client.warnings)
        # Processing continued past the warning to the callback.
        assert client.callback.called

    def test_subtype_match_no_warning(self) -> None:
        """``subtype`` == registered app id -> no mismatch warning (R3 False)."""
        client = FcmHandleSlim()
        _set_decrypt(client, b'{"ok": true}')
        msg = make_data_message(subtype="APPID")

        client._handle_data_message(msg)

        assert client.warnings == []
        assert client.callback.called

    def test_empty_credentials_guard_returns(self) -> None:
        """Falsy-but-indexable credentials -> guard at line 608 returns (R4).

        ``FalsyCredentials`` keeps ``["gcm"]["app_id"]`` (line 601) working
        while evaluating falsy for ``if not self.credentials`` (line 608), the
        only way to reach the guard without the earlier lookup raising.
        """
        client = FcmHandleSlim(
            credentials=FalsyCredentials({"gcm": {"app_id": "APPID"}})
        )
        # decrypt must NOT be reached; leave it unset so any accidental call
        # would raise AttributeError and fail the test loudly.
        msg = make_data_message(subtype="APPID")

        client._handle_data_message(msg)

        assert not client.callback.called
        assert client.warnings == []

    def test_payload_dict_passed_through(self) -> None:
        """JSON object payload -> forwarded to callback verbatim (R5a)."""
        client = FcmHandleSlim()
        _set_decrypt(client, b'{"k": "v"}')
        msg = make_data_message()

        client._handle_data_message(msg)

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

        client._handle_data_message(msg)

        client._try_increment_error_count.assert_called_once_with(
            ErrorType.NOTIFY
        )
        assert not client._reset_error_count.called


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
