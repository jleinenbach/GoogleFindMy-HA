# tests/test_fcmpushclient_b64_hardening.py
"""Regression suite for ``_safe_urlsafe_b64decode`` (defense 3).

Defense layer 3 of the three-defense hotfix for the FCM listener
poison-message cascade (CA-CASCADING-FAILURE-001). Python 3.14 switched
``binascii.a2b_base64`` to ``strict_mode=True`` by default, which rejects
payloads that contain newlines, leading/trailing whitespace, or missing
padding -- conditions the wild FCM crypto-key and salt headers reliably
produce. Before this helper, ``urlsafe_b64decode`` raised
``binascii.Error: Incorrect padding`` on every notification, the listener
loop's poison-message handler selective-ACKed the message and looped, and
the memory leak observed on Home Assistant Core 2026.6.x followed.

The helper restores the pre-3.14 lenient behaviour locally (whitespace
strip + padding append) without touching the global decoder state. These
tests pin that lenient contract against a future regression that would
re-introduce strict decoding on dirty input.
"""

from __future__ import annotations

import binascii
from base64 import urlsafe_b64encode
from unittest.mock import patch

import pytest

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    CredentialDecryptionError,
    FcmPushClient,
    _safe_urlsafe_b64decode,
)


def _b64_no_pad(data: bytes) -> str:
    """URL-safe base64 string without padding (matches FCM stream format)."""
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_missing_padding_decodes_cleanly() -> None:
    """Unpadded base64 string decodes successfully.

    FCM crypto-key and salt headers routinely omit ``=`` padding. Python
    3.14 strict mode rejects this with ``binascii.Error: Incorrect padding``;
    the helper must re-append padding and decode without raising.
    """
    payload = b"\xde\xad\xbe\xef" * 4  # 16 bytes -> 22 base64 chars (no pad)
    unpadded = _b64_no_pad(payload)
    assert not unpadded.endswith("=")

    assert _safe_urlsafe_b64decode(unpadded) == payload


def test_embedded_newlines_are_stripped() -> None:
    """Base64 strings split by newlines decode after newline stripping.

    Some FCM payload sources (MIME-style splits, debug-log round-tripping)
    inject ``\\n`` mid-payload. Python 3.14 strict mode treats this as an
    invalid character; the helper must strip it before decoding.
    """
    payload = b"hello-world-padding-test"
    encoded = urlsafe_b64encode(payload).decode("ascii")
    # Inject newlines every 8 chars (MIME-style 76-char split, scaled down).
    dirty = "\n".join(encoded[i : i + 8] for i in range(0, len(encoded), 8))
    assert "\n" in dirty

    assert _safe_urlsafe_b64decode(dirty) == payload


def test_leading_and_trailing_whitespace_are_stripped() -> None:
    """Surrounding whitespace (space, tab, CR, vtab, formfeed) is removed.

    HTTP header parsers sometimes leave trailing CRLF or leading SP on the
    decoded parameter value. The helper collapses every ASCII whitespace
    flavour (space, tab, newline, CR, vtab, formfeed) via ``bytes.split``.
    """
    payload = b"trailing-whitespace-defense"
    clean = _b64_no_pad(payload)
    dirty = f" \t{clean}\r\n\x0b\x0c"

    assert _safe_urlsafe_b64decode(dirty) == payload


def test_bytes_input_is_accepted() -> None:
    """``bytes`` input decodes equivalently to ``str`` input.

    Callers in the credential path pass ``str`` from cached JSON; future
    callers in the wire-decode path may pass raw ``bytes`` straight out of
    the socket. The helper accepts both without an extra encode step.
    """
    payload = b"bytes-input-contract"
    encoded_str = _b64_no_pad(payload)
    encoded_bytes = encoded_str.encode("ascii")

    assert _safe_urlsafe_b64decode(encoded_bytes) == payload
    assert _safe_urlsafe_b64decode(encoded_bytes) == _safe_urlsafe_b64decode(
        encoded_str
    )


def test_python314_strict_mode_simulation_does_not_leak() -> None:
    """Strict-mode ``binascii.a2b_base64`` does not see dirty input.

    Simulates Python 3.14's strict default by patching the underlying
    ``binascii.a2b_base64`` to refuse newlines and missing padding (the
    exact failure mode reported on HA Core 2026.6.x). The helper must
    pre-clean the input so that the strict decoder receives a payload it
    accepts -- otherwise the cascade returns.
    """
    real_a2b = binascii.a2b_base64

    def strict_a2b(data: bytes | str, *, strict_mode: bool = False) -> bytes:
        # Mimic Python 3.14 strict_mode=True default: reject whitespace and
        # missing padding regardless of the strict_mode kwarg the caller
        # passes (the strict-mode default flip is the regression source).
        if isinstance(data, str):
            data = data.encode("ascii")
        if any(ws in data for ws in (b" ", b"\t", b"\n", b"\r")):
            raise binascii.Error("Invalid base64-encoded string")
        if len(data) % 4 != 0:
            raise binascii.Error("Incorrect padding")
        return real_a2b(data, strict_mode=True)

    payload = b"strict-mode-survives"
    dirty = f"\n {_b64_no_pad(payload)}\r\n"

    with patch.object(binascii, "a2b_base64", side_effect=strict_a2b):
        # The helper must pre-clean before delegating; otherwise the
        # patched strict decoder would raise.
        assert _safe_urlsafe_b64decode(dirty) == payload


def test_pure_punctuation_corruption_raises_binascii_error() -> None:
    """Non-alphabet punctuation raises instead of decoding to garbage.

    The stdlib's non-validating decoder DISCARDS characters outside the
    base64 alphabet, so ``b"!!!!"`` would decode to ``b""`` silently. The
    validating decoder must reject it with ``binascii.Error`` so that a
    corrupt credential field cannot masquerade as a (wrongly) decodable
    value. Pins the validate=True contract against a regression back to the
    lenient ``urlsafe_b64decode`` default.
    """
    for corrupt in ("!!!!", "@@@@", "????", "(())"):
        with pytest.raises(binascii.Error):
            _safe_urlsafe_b64decode(corrupt)


def test_corrupt_secret_field_reaches_credential_error_not_poison() -> None:
    """A corrupt ``keys.secret`` hits the credential branch, not poison skip.

    Regression for the validating-decode fix. ``keys.secret`` degraded to
    pure punctuation must raise ``CredentialDecryptionError`` out of
    ``_decrypt_raw_data`` -- the signal the supervisor uses to invalidate
    the key material and re-register. Without validation it would decode to
    garbage bytes, the decrypt would fail downstream, and the listener would
    treat permanent credential corruption as recoverable per-message poison
    (ACK + skip), looping forever on bad credentials.

    Adequacy (AGENTS.md 2.2): asserts the error *type* that drives the
    re-registration path, not merely that the decode line executed. The
    valid ``private`` field proves the failure is isolated to the corrupt
    ``secret`` rather than a generic key-section rejection.
    """
    valid_private = _b64_no_pad(b"\x01\x02\x03\x04" * 8)
    crypto_key = _b64_no_pad(b"\xde\xad\xbe\xef" * 4)
    salt = _b64_no_pad(b"\xfe\xed\xfa\xce" * 4)
    credentials: dict[str, object] = {
        "keys": {"private": valid_private, "secret": "!!!!"}
    }
    with pytest.raises(CredentialDecryptionError, match="base64 decode"):
        FcmPushClient._decrypt_raw_data(credentials, crypto_key, salt, b"raw")


def test_end_to_end_dirty_headers_decrypt_path_does_not_cascade() -> None:
    """Dirty crypto-key / salt headers reach ``_decrypt_raw_data`` cleanly.

    End-to-end check: ``_decrypt_raw_data`` is called with whitespace-laden
    header strings (the field the FCM listener actually receives from the
    wire). The helper must absorb the noise so the function reaches the
    credential surface; a credential-stage failure here proves the header
    decode succeeded (without the helper, ``binascii.Error`` would fire
    before the credentials are ever consulted).

    Empty credentials are passed deliberately: the test asserts the failure
    surface, not a full decrypt. The point is that the decrypt is reached.
    """
    payload_key = b"\xde\xad\xbe\xef" * 4
    payload_salt = b"\xfe\xed\xfa\xce" * 4
    crypto_key_dirty = f"\n {_b64_no_pad(payload_key)}\r\n"
    salt_dirty = f"  {_b64_no_pad(payload_salt)}\t"

    # No keys section -> credential surface raises CredentialDecryptionError.
    # The crucial assertion is that we get the CREDENTIAL error, not a
    # header-stage binascii.Error. If defense 3 regresses, the header
    # decode would raise first and this test would fail with a different
    # exception type.
    credentials: dict[str, object] = {"gcm": {"app_id": "x"}}
    with pytest.raises(CredentialDecryptionError, match="missing FCM key material"):
        FcmPushClient._decrypt_raw_data(
            credentials, crypto_key_dirty, salt_dirty, b"raw"
        )
