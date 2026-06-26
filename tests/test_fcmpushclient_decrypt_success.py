# tests/test_fcmpushclient_decrypt_success.py
"""Success-path unit test for ``FcmPushClient._decrypt_raw_data``.

The credential-failure suite (``test_fcmpushclient_credential_decryption.py``)
covers every *raising* branch of ``_decrypt_raw_data`` but never reaches the
final decrypt call (lines 540-548): the call into ``http_ece.decrypt`` and the
``return decrypted``. This test closes that gap (plan R1) without touching the
SUT.

Discipline:
* The DER private-key hurdle at line 531 is passed with a *real* P-256 key
  (not patched), so ``load_der_private_key`` executes for real and the success
  path stays SUT-true (plan AE2).
* ``http_decrypt`` is a module-level indirection (``fcmpushclient.py:83``;
  ``http_decrypt = http_ece.decrypt``) and is monkeypatched at the module
  attribute, so the test is deterministic without real ECE crypto material and
  without disturbing ``http_ece`` globally (plan AE1).
"""

from __future__ import annotations

from base64 import urlsafe_b64encode

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.googlefindmy.Auth.firebase_messaging import fcmpushclient
from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    FcmPushClient,
)


def _b64(data: bytes) -> str:
    """URL-safe base64 string without padding (matches FCM credential format)."""
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _valid_p256_der() -> bytes:
    """Generate a real, parseable P-256 PKCS8 DER private key.

    A genuine key lets ``load_der_private_key`` (line 531) succeed for real
    instead of being shadowed by a patch, keeping the success path honest.
    """
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def test_decrypt_raw_data_returns_decrypted_via_patched_http_decrypt(
    monkeypatch,
) -> None:
    """Valid credentials + patched ``http_decrypt`` -> returns decrypted bytes.

    Exercises lines 540-548: the ``http_decrypt`` call with the assembled
    keyword arguments and ``return decrypted``. Asserts the keyword contract
    (``version="aesgcm"``, decoded ``dh``/``salt``/``auth_secret``) the
    production code passes downstream.
    """
    secret_bytes = b"0123456789abcdef"  # 16-byte auth secret
    credentials = {
        "keys": {
            "private": _b64(_valid_p256_der()),
            "secret": _b64(secret_bytes),
        }
    }

    captured: dict[str, object] = {}
    sentinel = b"DECRYPTED-SENTINEL"

    def fake_http_decrypt(
        raw_data: bytes,
        *,
        salt: bytes,
        private_key: object,
        dh: bytes,
        version: str,
        auth_secret: bytes,
    ) -> bytes:
        captured["raw_data"] = raw_data
        captured["salt"] = salt
        captured["private_key"] = private_key
        captured["dh"] = dh
        captured["version"] = version
        captured["auth_secret"] = auth_secret
        assert version == "aesgcm"
        return sentinel

    monkeypatch.setattr(fcmpushclient, "http_decrypt", fake_http_decrypt)

    result = FcmPushClient._decrypt_raw_data(
        credentials,
        _b64(b"cryptokey"),
        _b64(b"salt-bytes"),
        b"rawbody",
    )

    assert result == sentinel
    # The keyword contract the SUT assembles for the ECE decryptor.
    assert captured["raw_data"] == b"rawbody"
    assert captured["dh"] == b"cryptokey"
    assert captured["salt"] == b"salt-bytes"
    assert captured["auth_secret"] == secret_bytes
    assert captured["version"] == "aesgcm"
    # The real P-256 key object parsed by load_der_private_key was forwarded.
    assert isinstance(captured["private_key"], ec.EllipticCurvePrivateKey)
