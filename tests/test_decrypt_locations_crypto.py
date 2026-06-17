# tests/test_decrypt_locations_crypto.py
"""Audit coverage for the local AES-GCM envelope unwrap primitive.

``decrypt_locations`` is dominated by network/cache orchestration and protobuf
parsing; the only self-contained cryptographic primitive in the module is
``_try_unwrap_aesgcm_envelope`` (the M1-Nonce audit point: nonce is the envelope
prefix, the AAD is the device id, and any authentication failure must fail
closed to ``None``). These tests drive that primitive against an independent
AES-GCM oracle so a regression flips an assertion rather than passing silently.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    _AESGCM_NONCE_LEN,
    _try_unwrap_aesgcm_envelope,
)

_KEY = bytes(range(32))
_NONCE = bytes(range(_AESGCM_NONCE_LEN))
_PLAINTEXT = b"identity-key-material-32-bytes!!"
_DEVICE_ID = "device-canonic-id-42"


def _seal(key: bytes, nonce: bytes, plaintext: bytes, device_id: str) -> bytes:
    """Build an AES-GCM envelope (nonce || ciphertext) with device_id as AAD."""

    ciphertext = AESGCM(key).encrypt(nonce, plaintext, device_id.encode())
    return nonce + ciphertext


def test_unwrap_roundtrip_with_matching_aad_returns_plaintext() -> None:
    """A well-formed envelope with the correct key and AAD decrypts to plaintext."""

    envelope = _seal(_KEY, _NONCE, _PLAINTEXT, _DEVICE_ID)
    assert _try_unwrap_aesgcm_envelope(envelope, _KEY, _DEVICE_ID) == _PLAINTEXT


def test_unwrap_nonce_is_envelope_prefix() -> None:
    """The primitive must split nonce/ciphertext at _AESGCM_NONCE_LEN, not elsewhere.

    Sealing with a distinct nonce and re-prefixing it proves the prefix is the
    nonce actually fed to AES-GCM: shifting the split point would break the tag.
    """

    nonce = bytes([0xA5] * _AESGCM_NONCE_LEN)
    envelope = _seal(_KEY, nonce, _PLAINTEXT, _DEVICE_ID)
    assert envelope[:_AESGCM_NONCE_LEN] == nonce
    assert _try_unwrap_aesgcm_envelope(envelope, _KEY, _DEVICE_ID) == _PLAINTEXT


def test_unwrap_wrong_device_id_fails_closed() -> None:
    """A mismatched AAD (device_id) must fail closed to None, never plaintext."""

    envelope = _seal(_KEY, _NONCE, _PLAINTEXT, _DEVICE_ID)
    assert _try_unwrap_aesgcm_envelope(envelope, _KEY, "other-device-id") is None


def test_unwrap_wrong_key_fails_closed() -> None:
    """A wrong wrapping key must fail closed to None."""

    envelope = _seal(_KEY, _NONCE, _PLAINTEXT, _DEVICE_ID)
    wrong_key = bytes([0xFF]) + _KEY[1:]
    assert _try_unwrap_aesgcm_envelope(envelope, wrong_key, _DEVICE_ID) is None


def test_unwrap_tampered_ciphertext_fails_closed() -> None:
    """Flipping a ciphertext byte must fail the GCM tag check and return None."""

    envelope = bytearray(_seal(_KEY, _NONCE, _PLAINTEXT, _DEVICE_ID))
    envelope[-1] ^= 0x01
    assert _try_unwrap_aesgcm_envelope(bytes(envelope), _KEY, _DEVICE_ID) is None


def test_unwrap_none_device_id_is_guarded() -> None:
    """Without a device_id there is no AAD to bind, so the primitive declines."""

    envelope = _seal(_KEY, _NONCE, _PLAINTEXT, _DEVICE_ID)
    assert _try_unwrap_aesgcm_envelope(envelope, _KEY, None) is None


def test_unwrap_envelope_too_short_is_guarded() -> None:
    """An envelope no longer than the nonce cannot carry ciphertext and is declined."""

    too_short = bytes(_AESGCM_NONCE_LEN)  # exactly nonce length, no ciphertext
    assert _try_unwrap_aesgcm_envelope(too_short, _KEY, _DEVICE_ID) is None
    assert _try_unwrap_aesgcm_envelope(b"", _KEY, _DEVICE_ID) is None
