# tests/test_fmdn_curve_profile.py
"""Tests for the FMDN curve registry, the scalar formula and the error taxonomy.

Expected ECDH values are built independently of ``curve_profile``: the tests
use the Diffie-Hellman symmetry ``x(r * S) == x(s * R)`` with the true point
``S = s * G`` (including its real Y coordinate), computed directly with the
``cryptography`` or ``ecdsa`` package. The production code only ever sees the
x-coordinate of ``S`` and must recover a usable point on its own.
"""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from custom_components.googlefindmy.FMDNCrypto import foreign_tracker_cryptor
from custom_components.googlefindmy.FMDNCrypto.curve_profile import (
    CURVES_BY_COORD_LEN,
    P256_ORDER,
    SECP160R1,
    SECP160R1_ORDER,
    SECP256R1,
    FmdnCurve,
    ScalarRule,
    curve_for_coord_len,
    reduce_scalar,
    rx_to_ry,
)
from custom_components.googlefindmy.FMDNCrypto.foreign_report_errors import (
    ForeignReportAuthError,
    ForeignReportError,
    ForeignReportStructureError,
    UnsupportedCurveError,
)
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    DecryptionError,
)

# Field prime of P-256 (FIPS 186-4, D.1.2.3), used only to build off-curve inputs.
_P256_PRIME = 2**256 - 2**224 + 2**192 + 2**96 - 1
_P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B

# Fixed, arbitrary scalars; deterministic so failures are reproducible.
_SCALARS = (
    1,
    2,
    0x1D5A4F0B2C3E,
    0x7E3C1A9B5D2F4E6C8A0B1D3F5E7C9A2B4D6F8E0A1C3E5D7F9B2A4C6E8D0F1A3,
)


def _p256_point(scalar: int) -> ec.EllipticCurvePublicNumbers:
    private_key = ec.derive_private_key(scalar, ec.SECP256R1())
    return private_key.public_key().public_numbers()


def _p256_expected_shared_x(r: int, s: int) -> bytes:
    """x(s * (r * G)) via cryptography's own ECDH, with the true point r * G."""
    peer = ec.derive_private_key(r, ec.SECP256R1()).public_key()
    return ec.derive_private_key(s, ec.SECP256R1()).exchange(ec.ECDH(), peer)


def _secp160r1() -> Any:
    import ecdsa  # noqa: PLC0415

    return ecdsa.SECP160r1


def _p256_scalars_by_y_parity() -> dict[int, int]:
    """Return the first scalars from 2 upwards whose public Y is even and odd."""
    found: dict[int, int] = {}
    s = 2
    while len(found) < 2:
        found.setdefault(_p256_point(s).y % 2, s)
        s += 1
    return found


_SEARCH_LIMIT = 1000


def _find_x(p: int, a: int, b: int, *, residue: bool, odd_root: bool = False) -> int:
    """Smallest x in [1, _SEARCH_LIMIT) with the requested Euler-criterion result.

    Independent of ``rx_to_ry``: decides residuosity by Euler's criterion and
    computes the raw root ``rhs^((p+1)/4)`` itself. With ``odd_root`` the raw
    root must be odd, which forces ``rx_to_ry`` through its even-Y flip.
    """
    for x in range(1, _SEARCH_LIMIT):
        rhs = (pow(x, 3, p) + a * x + b) % p
        is_residue = pow(rhs, (p - 1) // 2, p) == 1
        if is_residue != residue:
            continue
        if odd_root and pow(rhs, (p + 1) // 4, p) % 2 == 0:
            continue
        return x
    raise AssertionError("search limit reached")


def _p256_off_curve_x() -> int:
    return _find_x(_P256_PRIME, -3, _P256_B, residue=False)


class TestCurveRegistry:
    """Z2: the curve follows from the Sx length through one registry."""

    def test_registry_has_exactly_the_two_specified_lengths(self) -> None:
        assert set(CURVES_BY_COORD_LEN) == {20, 32}
        assert CURVES_BY_COORD_LEN[20] is SECP160R1
        assert CURVES_BY_COORD_LEN[32] is SECP256R1

    def test_registry_is_read_only(self) -> None:
        with pytest.raises(TypeError):
            CURVES_BY_COORD_LEN[33] = SECP256R1  # type: ignore[index]

    @pytest.mark.parametrize(("length", "curve"), [(20, SECP160R1), (32, SECP256R1)])
    def test_curve_for_coord_len_selects_by_length(
        self, length: int, curve: FmdnCurve
    ) -> None:
        assert curve_for_coord_len(length) is curve
        assert curve.coord_len == length

    @pytest.mark.parametrize("length", [0, 5, 19, 21, 31, 33, 64])
    def test_other_lengths_raise_unsupported_curve_error(self, length: int) -> None:
        with pytest.raises(UnsupportedCurveError) as exc_info:
            curve_for_coord_len(length)
        assert exc_info.value.sx_len == length
        assert str(length) in str(exc_info.value)

    def test_curve_names(self) -> None:
        assert (SECP160R1.name, SECP160R1.short_name) == ("secp160r1", "secp160r1")
        assert (SECP256R1.name, SECP256R1.short_name) == ("secp256r1", "p256")

    def test_orders_match_the_libraries(self) -> None:
        assert SECP160R1.order == SECP160R1_ORDER == int(_secp160r1().order)
        assert SECP256R1.order == P256_ORDER
        # P-256 order from FIPS 186-4, D.1.2.3, independent of this module.
        assert P256_ORDER == int(
            "115792089210356248762697446949407573529996955224135760342"
            "422259061068512044369"
        )


class TestReduceScalar:
    """Z12a: one formula site for both scalar rules."""

    @pytest.mark.parametrize("order", [P256_ORDER, SECP160R1_ORDER, 7])
    @pytest.mark.parametrize(
        "r_dash", [0, 1, 6, 7, 8, 2**255 + 12345, 2**256 - 1, P256_ORDER]
    )
    def test_rules_match_their_formulas(self, r_dash: int, order: int) -> None:
        assert reduce_scalar(r_dash, order, ScalarRule.MOD_N) == r_dash % order
        assert (
            reduce_scalar(r_dash, order, ScalarRule.PLUS_ONE)
            == (r_dash % (order - 1)) + 1
        )

    def test_mod_n_can_return_zero_and_plus_one_cannot(self) -> None:
        r_dash = 3 * P256_ORDER
        assert reduce_scalar(r_dash, P256_ORDER, ScalarRule.MOD_N) == 0
        assert reduce_scalar(r_dash, P256_ORDER, ScalarRule.PLUS_ONE) >= 1

    def test_rules_differ_for_a_typical_prf_output(self) -> None:
        r_dash = 2**255 + 12345
        mod_n = reduce_scalar(r_dash, P256_ORDER, ScalarRule.MOD_N)
        plus_one = reduce_scalar(r_dash, P256_ORDER, ScalarRule.PLUS_ONE)
        assert plus_one - mod_n == 1

    def test_rule_values_are_the_reading_id_tokens(self) -> None:
        assert [rule.value for rule in ScalarRule] == ["mod_n", "plus1"]

    def test_stored_rule_strings_are_coerced(self) -> None:
        assert reduce_scalar(9, 7, "mod_n") == 2  # type: ignore[arg-type]
        assert reduce_scalar(9, 7, "plus1") == 4  # type: ignore[arg-type]

    def test_unknown_rule_raises_value_error(self) -> None:
        with pytest.raises(ValueError):
            reduce_scalar(9, 7, "plus2")  # type: ignore[arg-type]

    @pytest.mark.parametrize(("r_dash", "order"), [(-1, P256_ORDER), (5, 1), (5, 0)])
    def test_invalid_inputs_raise(self, r_dash: int, order: int) -> None:
        with pytest.raises(ValueError):
            reduce_scalar(r_dash, order, ScalarRule.MOD_N)


class TestPointX:
    """point_x equals the library's own public key x-coordinate."""

    @pytest.mark.parametrize("scalar", _SCALARS)
    def test_p256_point_x(self, scalar: int) -> None:
        assert SECP256R1.point_x(scalar) == _p256_point(scalar).x.to_bytes(32, "big")

    @pytest.mark.parametrize("scalar", _SCALARS[:3])
    def test_secp160r1_point_x(self, scalar: int) -> None:
        expected = int((scalar * _secp160r1().generator).x()).to_bytes(20, "big")
        assert SECP160R1.point_x(scalar) == expected

    @pytest.mark.parametrize("curve", [SECP160R1, SECP256R1])
    def test_out_of_range_scalar_raises(self, curve: FmdnCurve) -> None:
        for scalar in (0, curve.order, -1):
            with pytest.raises(ValueError):
                curve.point_x(scalar)


class TestP256Ecdh:
    """SECP256R1.ecdh_x against the independent Diffie-Hellman construction."""

    @pytest.mark.parametrize("r", _SCALARS)
    def test_matches_independent_ecdh_for_both_y_parities(self, r: int) -> None:
        for parity, s in _p256_scalars_by_y_parity().items():
            peer_x = _p256_point(s).x.to_bytes(32, "big")
            assert SECP256R1.ecdh_x(r, peer_x) == _p256_expected_shared_x(r, s), (
                f"y parity {parity}"
            )

    def test_off_curve_x_raises_structure_error(self) -> None:
        off_x = _p256_off_curve_x().to_bytes(32, "big")
        with pytest.raises(ForeignReportStructureError):
            SECP256R1.ecdh_x(1, off_x)
        with pytest.raises(ForeignReportStructureError):
            SECP256R1.validate_peer_x(off_x)

    def test_x_outside_the_field_raises_structure_error(self) -> None:
        with pytest.raises(ForeignReportStructureError):
            SECP256R1.validate_peer_x(_P256_PRIME.to_bytes(32, "big"))

    def test_wrong_peer_length_raises_structure_error(self) -> None:
        with pytest.raises(ForeignReportStructureError):
            SECP256R1.ecdh_x(1, bytes(20))

    @pytest.mark.parametrize(("curve", "length"), [(SECP256R1, 20), (SECP160R1, 32)])
    def test_validate_peer_x_rejects_the_other_curves_length(
        self, curve: FmdnCurve, length: int
    ) -> None:
        # x = 0 is a point on P-256, so only the length check can reject bytes(20).
        with pytest.raises(ForeignReportStructureError):
            curve.validate_peer_x(bytes(length))

    def test_valid_peer_passes_validation(self) -> None:
        SECP256R1.validate_peer_x(_p256_point(2).x.to_bytes(32, "big"))


class TestSecp160r1Ecdh:
    """SECP160R1.ecdh_x against ecdsa with the true peer point."""

    @pytest.mark.parametrize("r", _SCALARS[:3])
    def test_matches_independent_ecdh_for_both_y_parities(self, r: int) -> None:
        generator = _secp160r1().generator
        seen_parities: set[int] = set()
        s = 2
        while len(seen_parities) < 2:
            peer = s * generator
            parity = int(peer.y()) % 2
            if parity not in seen_parities:
                seen_parities.add(parity)
                peer_x = int(peer.x()).to_bytes(20, "big")
                expected = int((s * (r * generator)).x()).to_bytes(20, "big")
                assert SECP160R1.ecdh_x(r, peer_x) == expected
            s += 1

    def test_x_outside_the_field_raises_structure_error(self) -> None:
        p = int(_secp160r1().curve.p())
        with pytest.raises(ForeignReportStructureError):
            SECP160R1.validate_peer_x(p.to_bytes(20, "big"))

    def test_off_curve_x_raises_structure_error(self) -> None:
        curve_fp = _secp160r1().curve
        x = _find_x(
            int(curve_fp.p()), int(curve_fp.a()), int(curve_fp.b()), residue=False
        )
        with pytest.raises(ForeignReportStructureError):
            SECP160R1.ecdh_x(1, x.to_bytes(20, "big"))


class TestRxToRyHome:
    """rx_to_ry lives in curve_profile; the cryptor re-exports the same object."""

    def test_cryptor_reexports_the_same_function(self) -> None:
        assert foreign_tracker_cryptor.rx_to_ry is rx_to_ry

    @pytest.mark.parametrize("curve_name", ["secp160r1", "p256"])
    def test_odd_raw_root_is_flipped_to_the_even_y(self, curve_name: str) -> None:
        import ecdsa  # noqa: PLC0415

        curve_fp = (
            ecdsa.SECP160r1 if curve_name == "secp160r1" else ecdsa.NIST256p
        ).curve
        p, a, b = int(curve_fp.p()), int(curve_fp.a()), int(curve_fp.b())
        x = _find_x(p, a, b, residue=True, odd_root=True)
        raw_root = pow((pow(x, 3, p) + a * x + b) % p, (p + 1) // 4, p)
        assert raw_root % 2 == 1
        assert rx_to_ry(x, curve_fp) == p - raw_root

    def test_p256_prime_is_3_mod_4(self) -> None:
        assert _P256_PRIME % 4 == 3


class TestErrorTaxonomy:
    """Z10: the family sits outside the owner-key DecryptionError family."""

    def test_family_derives_from_value_error_not_decryption_error(self) -> None:
        assert issubclass(ForeignReportError, ValueError)
        assert not issubclass(ForeignReportError, DecryptionError)
        assert not issubclass(ForeignReportError, RuntimeError)

    def test_hierarchy(self) -> None:
        assert issubclass(ForeignReportStructureError, ForeignReportError)
        assert issubclass(UnsupportedCurveError, ForeignReportStructureError)
        assert issubclass(ForeignReportAuthError, ForeignReportError)
        assert not issubclass(ForeignReportAuthError, ForeignReportStructureError)

    def test_auth_error_carries_the_attempt_summary(self) -> None:
        err = ForeignReportAuthError(
            "secp256r1", ("p256/mod_n/nonce8", "p256/plus1/nonce8"), 3
        )
        assert err.curve_name == "secp256r1"
        assert err.readings_tried == ("p256/mod_n/nonce8", "p256/plus1/nonce8")
        assert err.keys_tried == 3
        assert "2 readings x 3 keys" in str(err)
