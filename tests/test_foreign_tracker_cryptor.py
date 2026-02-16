# tests/test_foreign_tracker_cryptor.py
"""Tests for foreign_tracker_cryptor decrypt() identity_key length contract."""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.FMDNCrypto.eid_generator import EIK_LENGTH
from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
    _COORD_LEN,
    _require_len,
    decrypt,
)


class TestDecryptIdentityKeyLength:
    """Regression: decrypt() must accept a 32-byte EIK, not 20-byte coords.

    A previous bug validated identity_key against _COORD_LEN (20) instead of
    EIK_LENGTH (32).  Since decrypt() passes identity_key to calculate_r() →
    prf_aes_256_ecb() and generate_eid(), both of which require 32-byte EIK,
    the 20-byte check caused ValueError at runtime for every foreign report.
    """

    def test_eik_length_is_32(self) -> None:
        """EIK_LENGTH must be 32 (AES-256 key)."""
        assert EIK_LENGTH == 32

    def test_coord_length_is_20(self) -> None:
        """_COORD_LEN must be 20 (SECP160r1 x-coordinate)."""
        assert _COORD_LEN == 20

    def test_decrypt_rejects_20_byte_identity_key(self) -> None:
        """decrypt() must reject a 20-byte identity_key (old broken behavior)."""
        fake_key_20 = b"\x00" * 20
        fake_encrypted = b"\x00" * 32  # dummy ciphertext + tag
        fake_sx = b"\x00" * 20
        with pytest.raises(ValueError, match="identity_key must be exactly 32 bytes"):
            decrypt(fake_key_20, fake_encrypted, fake_sx, beacon_time_counter=0)

    def test_decrypt_accepts_32_byte_identity_key_past_validation(self) -> None:
        """decrypt() must NOT reject a 32-byte identity_key at the length check.

        We cannot run a full decrypt without valid crypto material, so we
        verify the validation passes and the function proceeds past
        _require_len (it will fail later in the crypto — but not with a
        length error on identity_key).
        """
        fake_key_32 = b"\x01" * 32
        fake_encrypted = b"\x00" * 32
        fake_sx = b"\x00" * 20
        with pytest.raises(Exception) as exc_info:
            decrypt(fake_key_32, fake_encrypted, fake_sx, beacon_time_counter=0)
        # Must NOT be the identity_key length error
        assert "identity_key must be exactly" not in str(exc_info.value)

    def test_require_len_helper(self) -> None:
        """_require_len raises ValueError with descriptive message."""
        with pytest.raises(ValueError, match=r"foo must be exactly 10 bytes \(got 5\)"):
            _require_len("foo", b"\x00" * 5, 10)
        # No error when length matches
        _require_len("bar", b"\x00" * 10, 10)
