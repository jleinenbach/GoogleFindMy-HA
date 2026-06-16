# tests/test_foreign_tracker_cryptor.py
"""Tests for foreign_tracker_cryptor decrypt() identity_key length contract."""

from __future__ import annotations

import secrets

import pytest

from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    EIK_LENGTH,
    EidVariant,
    generate_eid_variant,
)
from custom_components.googlefindmy.FMDNCrypto.foreign_tracker_cryptor import (
    _COORD_LEN,
    _get_curve,
    _require_len,
    decrypt,
    encrypt,
    rx_to_ry,
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


def _valid_eid() -> bytes:
    """Build a valid SECP160r1 EID (on-curve x-coordinate) for encrypt() inputs."""
    identity_key = b"\x01" * EIK_LENGTH
    return generate_eid_variant(
        identity_key, 0, EidVariant.LEGACY_SECP160R1_X20_BE
    )


class TestNonceFreshnessContract:
    """H1 (A7-H hardening): AES-EAX nonce uniqueness rests on caller entropy.

    ``encrypt(message, random, eid)`` derives the ephemeral scalar ``s`` solely
    from ``random``; the EAX nonce is ``LRx(8) || LSx(8)`` where ``LSx`` is the
    low 8 bytes of ``(s*G).x``. The only production caller,
    ``fmdn_finder/location_uploader.py:551``, passes ``secrets.token_bytes(32)``
    as ``random``, so a fresh nonce (and HKDF key) is produced per message.

    AP5 flagged this isolated function as a nonce-reuse FAIL (M1); the Tier-4
    code verification (Regel 10) downgraded it to PASS precisely because of that
    caller contract. These tests lock the contract so a future refactor that
    reuses ``random`` (constant/cached bytes, a counter, etc.) — which would
    reuse the EAX nonce and break confidentiality/integrity — turns the audited
    PASS back into a red test instead of a silent regression.
    """

    def test_fresh_random_yields_distinct_nonce_and_ciphertext(self) -> None:
        """Two encryptions of identical plaintext with fresh entropy must differ."""
        eid = _valid_eid()
        message = b"identical-plaintext-payload"
        ct1, sx1 = encrypt(message, secrets.token_bytes(32), eid)
        ct2, sx2 = encrypt(message, secrets.token_bytes(32), eid)
        # Fresh entropy -> different ephemeral S -> different Sx -> different nonce.
        assert sx1 != sx2
        assert ct1 != ct2

    def test_reused_random_repeats_nonce_mutation_guard(self) -> None:
        """Mutation guard proving the freshness test is sharp.

        If a caller violates the entropy contract and passes constant bytes,
        the ephemeral S (hence Sx, hence the nonce) repeats and the ciphertext
        of a fixed plaintext is identical. Asserting that equality here proves
        the freshness test above would actually catch nonce reuse.
        """
        eid = _valid_eid()
        message = b"identical-plaintext-payload"
        fixed_random = b"\x02" * 32
        ct1, sx1 = encrypt(message, fixed_random, eid)
        ct2, sx2 = encrypt(message, fixed_random, eid)
        assert sx1 == sx2  # nonce component repeats under reused entropy
        assert ct1 == ct2


class TestRxToRyCofactorSafety:
    """H2 (A7-H hardening): rx_to_ry rejects off-curve X and recovers on-curve Y.

    AP5 flagged a possible invalid-curve / small-subgroup attack (M4); the
    Tier-4 verification downgraded it to PASS because SECP160r1 has cofactor 1
    (prime order) and ``rx_to_ry`` reconstructs Y on the correct curve, raising
    on a non-quadratic residue (the residue check in the production code). Any
    point it returns therefore lies in the legitimate prime-order group, so no
    small-subgroup confinement is possible.

    These tests lock both halves of that argument: an X with no valid Y must
    raise, and an on-curve X must yield a Y satisfying the curve equation. A
    future relaxation of the residue check (which would admit off-curve points)
    would make the negative case go red.
    """

    def test_off_curve_x_raises(self) -> None:
        """An X whose y^2 is a non-residue has no curve point -> ValueError."""
        curve_fp = _get_curve().curve
        p = int(curve_fp.p())
        a = int(curve_fp.a())
        b = int(curve_fp.b())
        # Find a small x whose y^2 is a quadratic non-residue (Euler's criterion
        # gives p-1 for non-residues). Roughly half of all x qualify.
        non_residue_x = None
        for cand in range(2, 500):
            ryy = (pow(cand, 3, p) + a * cand + b) % p
            if ryy != 0 and pow(ryy, (p - 1) // 2, p) == p - 1:
                non_residue_x = cand
                break
        assert non_residue_x is not None, "expected an off-curve x for the negative case"
        with pytest.raises(ValueError, match="not on the curve"):
            rx_to_ry(non_residue_x, curve_fp)

    def test_on_curve_x_recovers_even_y(self) -> None:
        """The generator's X is on-curve; recovered Y satisfies y^2 = x^3+ax+b."""
        curve = _get_curve()
        curve_fp = curve.curve
        p = int(curve_fp.p())
        a = int(curve_fp.a())
        b = int(curve_fp.b())
        gx = int(curve.generator.x())
        y = rx_to_ry(gx, curve_fp)
        lhs = (y * y) % p
        rhs = (pow(gx, 3, p) + a * gx + b) % p
        assert lhs == rhs  # recovered point lies on the curve
        assert y % 2 == 0  # even-Y canonical form (FMDN convention)
