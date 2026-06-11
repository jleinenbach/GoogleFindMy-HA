# tests/test_fcmpushclient_credential_decryption.py
"""Direct unit tests for FcmPushClient._decrypt_raw_data credential failures.

Regression suite for CA-CRED-VS-MSG-DECRYPT-001 iter-3 (Codex review on
commit 14b5a45451): when the cached FCM credential material is structurally
broken, ``_decrypt_raw_data`` must raise ``CredentialDecryptionError`` (a
typed subclass of ``ValueError``) so the listener loop's poison-message
defense can discriminate credential corruption (escalate to supervisor) from
per-message decode failures (selective-ACK + skip).

The iter-2 implementation used a string-prefix marker ("Credentials ...")
which silently leaked plain ``ValueError`` from upstream libraries -- most
notably ``cryptography.hazmat.primitives.serialization.load_der_private_key``
raising ``ValueError("Could not deserialize key data ...")`` for valid base64
but invalid DER content. That leak caused the listener to ACK every incoming
message while silently dropping it. The iter-3 fix wraps every credential
decode surface and raises ``CredentialDecryptionError`` instead, so the
discriminator is the exception TYPE rather than the message text.
"""
from __future__ import annotations

from base64 import urlsafe_b64encode

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    CredentialDecryptionError,
    FcmPushClient,
)


def _b64(data: bytes) -> str:
    """URL-safe base64 string without padding (matches FCM credential format)."""
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_missing_keys_section_raises_typed_error() -> None:
    """``credentials["keys"]`` absent -> CredentialDecryptionError."""
    credentials: dict[str, object] = {"gcm": {"app_id": "x"}}
    with pytest.raises(CredentialDecryptionError, match="missing FCM key material"):
        FcmPushClient._decrypt_raw_data(
            credentials, "crypto", "salt", b"raw"
        )


def test_invalid_key_value_types_raise_typed_error() -> None:
    """``keys.private``/``keys.secret`` non-string -> CredentialDecryptionError."""
    credentials: dict[str, object] = {
        "keys": {"private": 42, "secret": None},
    }
    with pytest.raises(
        CredentialDecryptionError, match="contain invalid key values"
    ):
        FcmPushClient._decrypt_raw_data(
            credentials, "crypto", "salt", b"raw"
        )


def test_non_base64_key_material_raises_typed_error() -> None:
    """``keys.private`` not valid base64 -> CredentialDecryptionError, not bare binascii.Error.

    Covers the b64decode failure surface (iter-2 fix). The error chain
    preserves the original binascii.Error via ``__cause__``.
    """
    credentials: dict[str, object] = {
        "keys": {
            "private": "###not-base64###",
            "secret": _b64(b"valid-secret-bytes"),
        },
    }
    with pytest.raises(
        CredentialDecryptionError, match="failed base64 decode"
    ) as exc_info:
        FcmPushClient._decrypt_raw_data(
            credentials, _b64(b"k"), _b64(b"s"), b"raw"
        )
    # __cause__ chain preserves the underlying binascii.Error for diagnostics.
    assert exc_info.value.__cause__ is not None


def test_non_ascii_in_private_key_raises_typed_error() -> None:
    """``keys.private`` with non-ASCII bytes -> CredentialDecryptionError.

    THIS IS THE ITER-4 CODEX REGRESSION (commit ae70dccc90). ``.encode("ascii")``
    on a corrupted cached key value raises ``UnicodeEncodeError`` BEFORE
    ``urlsafe_b64decode`` is reached. ``UnicodeEncodeError`` is a subclass of
    ``ValueError`` but NOT of ``binascii.Error``, so the iter-3 catch
    ``except binascii.Error`` let it through, the listener loop's per-message
    ValueError handler swallowed it, and every push was selective-ACKed and
    silently dropped instead of surfacing a credential fault. The iter-4 fix
    widens the catch to ``(binascii.Error, UnicodeError)``.
    """
    credentials: dict[str, object] = {
        "keys": {
            "private": "☃snowman",  # non-ASCII -> UnicodeEncodeError on .encode("ascii")
            "secret": _b64(b"valid-secret-bytes-16b"),
        },
    }
    with pytest.raises(
        CredentialDecryptionError, match="failed base64 decode"
    ) as exc_info:
        FcmPushClient._decrypt_raw_data(
            credentials, _b64(b"k"), _b64(b"s"), b"raw"
        )
    # __cause__ preserves the underlying UnicodeEncodeError for diagnostics.
    assert exc_info.value.__cause__ is not None
    assert isinstance(exc_info.value.__cause__, UnicodeError)


def test_non_ascii_in_secret_raises_typed_error() -> None:
    """``keys.secret`` with non-ASCII bytes -> CredentialDecryptionError.

    Mirror of the iter-4 regression for the secret field: both cred encodes
    run inside the same try-block, so the same widened catch must apply.
    """
    credentials: dict[str, object] = {
        "keys": {
            "private": _b64(b"\xde\xad\xbe\xef" * 16),
            "secret": "café",  # non-ASCII -> UnicodeEncodeError
        },
    }
    with pytest.raises(
        CredentialDecryptionError, match="failed base64 decode"
    ) as exc_info:
        FcmPushClient._decrypt_raw_data(
            credentials, _b64(b"k"), _b64(b"s"), b"raw"
        )
    assert isinstance(exc_info.value.__cause__, UnicodeError)


def test_corrupted_der_private_key_raises_typed_error() -> None:
    """Valid base64 but invalid DER content -> CredentialDecryptionError.

    THIS IS THE ITER-3 CODEX REGRESSION. Under the iter-2 string-marker
    discipline this path leaked a plain ValueError("Could not deserialize key
    data ...") from cryptography, which the listener loop misinterpreted as
    per-message poison and silently ACKed every incoming message while
    dropping all notifications. The iter-3 fix wraps load_der_private_key and
    re-raises as CredentialDecryptionError so the supervisor can escalate.
    """
    # Valid base64 of plausible-length garbage that load_der_private_key will
    # reject. "deadbeef" * 8 = 64 bytes, looks key-sized but is not DER.
    fake_der_payload = b"\xde\xad\xbe\xef" * 16
    credentials: dict[str, object] = {
        "keys": {
            "private": _b64(fake_der_payload),
            "secret": _b64(b"valid-secret-bytes-16b"),
        },
    }
    with pytest.raises(
        CredentialDecryptionError, match="failed DER private-key parse"
    ) as exc_info:
        FcmPushClient._decrypt_raw_data(
            credentials, _b64(b"k"), _b64(b"s"), b"raw"
        )
    # __cause__ preserves the underlying cryptography error for diagnostics.
    assert exc_info.value.__cause__ is not None


def test_credential_decryption_error_is_value_error_subclass() -> None:
    """Subclass-of-ValueError contract: backward compat for legacy ``except ValueError``.

    Any external caller that catches broad ValueError still catches
    CredentialDecryptionError. The listener loop's poison-message defense
    uses ``isinstance`` discrimination (CredentialDecryptionError first, then
    ValueError) to enforce the iter-3 propagation contract.
    """
    assert issubclass(CredentialDecryptionError, ValueError)
    try:
        raise CredentialDecryptionError("test")
    except ValueError:
        pass
    else:
        pytest.fail("CredentialDecryptionError should be catchable as ValueError")
