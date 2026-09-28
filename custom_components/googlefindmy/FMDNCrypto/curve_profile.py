# custom_components/googlefindmy/FMDNCrypto/curve_profile.py
"""Curve profiles for FMDN: one registry, one scalar formula, one Y recovery.

The Find Hub Network Accessory Specification lets the accessory manufacturer
choose between two curves (section "Curve selection"): SECP160R1 (the default,
20-byte EIDs) and SECP256R1 (P-256, 32-byte EIDs, requires extended
advertising). A crowdsourced location report carries no curve field; the only
distinguishing feature is the length of ``Sx`` (``publicKeyRandom``). The
decryption procedure is written the same way: "SECP160R1 for 20-byte EIDs or
SECP256R1 for 32-byte EIDs" (Boettger et al., PoPETs 2025(4), section 4.1.5,
step 1).

This module is the single place that knows those facts:

* ``CURVES_BY_COORD_LEN`` maps a coordinate length to its ``FmdnCurve``. A new
  curve is one entry plus its tests; any other length raises
  ``UnsupportedCurveError`` through ``curve_for_coord_len``.
* ``reduce_scalar`` holds the formulas that turn the PRF output ``r'`` into a
  curve scalar. The specification ("EID computation") derives it as
  ``r = r' mod n``; the integration's existing ``MODERN_P256_*`` EID variants
  project into ``[1, n - 1]`` instead. Both are named by ``ScalarRule``; EID
  generation, the hashed-flags mask and report decryption are meant to call
  this function rather than spell a formula out, so they cannot disagree
  unnoticed.
* ``rx_to_ry`` recovers the even Y coordinate for curves whose prime satisfies
  ``p = 3 (mod 4)``; both supported curves do.

Each ``FmdnCurve`` hides its arithmetic backend: SECP160R1 runs on the
``ecdsa`` package through ``_ecdsa_shim``, SECP256R1 on ``cryptography``. Both
libraries are imported lazily on first use.

References:
    - Find Hub Network Accessory Specification, sections "Curve selection" and
      "EID computation":
      https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn
    - Boettger et al., "Okay Google, Where's My Tracker?", PoPETs 2025(4),
      section 4.1.5: https://petsymposium.org/popets/2025/popets-2025-0147.pdf
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final, Protocol, assert_never

from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import (
    CurveFpProtocol,
    load_curve,
    load_point_class,
)
from custom_components.googlefindmy.FMDNCrypto._lazy_crypto import (
    get_ec_module,
    get_p256_curve,
)
from custom_components.googlefindmy.FMDNCrypto.foreign_report_errors import (
    ForeignReportStructureError,
    UnsupportedCurveError,
)

__all__ = [
    "CURVES_BY_COORD_LEN",
    "P256_ORDER",
    "SECP160R1",
    "SECP160R1_ORDER",
    "SECP256R1",
    "FmdnCurve",
    "ScalarRule",
    "curve_for_coord_len",
    "reduce_scalar",
    "rx_to_ry",
]

# Group orders (n). SECP160R1_ORDER equals ``ecdsa.SECP160r1.order``; the test
# suite pins that equality so the literal cannot drift from the library.
SECP160R1_ORDER: Final[int] = 0x0100000000000000000001F4C8F927AED3CA752257
P256_ORDER: Final[int] = (
    0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
)


# Smallest group order for which both scalar rules are defined.
_MIN_ORDER: Final[int] = 2


class ScalarRule(StrEnum):
    """How the 32-byte PRF output ``r'`` becomes the curve scalar ``r``.

    ``MOD_N`` is the specification's rule (section "EID computation":
    ``r = r' mod n``). ``PLUS_ONE`` is ``(r' mod (n - 1)) + 1``, the projection
    the integration's ``MODERN_P256_*`` EID variants use; no public source
    documents it for FMDN.
    """

    MOD_N = "mod_n"
    PLUS_ONE = "plus1"


def reduce_scalar(r_dash_int: int, order: int, rule: ScalarRule) -> int:
    """Reduce the PRF output ``r'`` to a curve scalar under ``rule``.

    Callers derive the scalar from ``r'`` through this function instead of
    spelling out a formula of their own. ``MOD_N`` can return 0 (probability
    ``1/n``); callers treat a zero scalar as a failed attempt, because
    ``0 * G`` is the point at infinity. ``rule`` is coerced through
    ``ScalarRule``, so a stored string such as ``"mod_n"`` is accepted and an
    unknown value raises ``ValueError``.

    Args:
        r_dash_int: The PRF output interpreted as an unsigned integer.
        order: The group order ``n`` of the curve.
        rule: The scalar rule to apply.

    Returns:
        The reduced scalar: in ``[0, n - 1]`` for ``MOD_N``, in ``[1, n - 1]``
        for ``PLUS_ONE``.

    Raises:
        ValueError: If ``r_dash_int`` is negative, ``order`` is below 2 or
            ``rule`` names no ``ScalarRule``.
    """
    rule = ScalarRule(rule)
    if r_dash_int < 0:
        raise ValueError("r' must be a non-negative integer")
    if order < _MIN_ORDER:
        raise ValueError("curve order must be at least 2")
    if rule is ScalarRule.MOD_N:
        return r_dash_int % order  # MOD_N
    if rule is ScalarRule.PLUS_ONE:
        projected_scalar: int = (r_dash_int % (order - 1)) + 1
        return projected_scalar
    assert_never(rule)


def rx_to_ry(Rx: int, curve: CurveFpProtocol) -> int:
    """Recover the even Y coordinate from X on an elliptic curve (point decompression).

    Mathematical Background
    -----------------------
    Elliptic curves in short Weierstrass form satisfy the equation:

        y² = x³ + ax + b  (mod p)

    Given only the X coordinate, we solve for Y using modular arithmetic.
    This is the inverse of "point compression" where we store only X plus
    one bit indicating the sign of Y.

    Algorithm (Tonelli-Shanks for p ≡ 3 mod 4)
    ------------------------------------------
    1. Compute y² = x³ + ax + b (mod p)
    2. For curves where p ≡ 3 (mod 4), the modular square root is:

           y = (y²)^((p+1)/4) mod p

    Why This Formula Works
    ----------------------
    For p ≡ 3 (mod 4), we can verify:

        (y²)^((p+1)/4) mod p
      = y^((p+1)/2) mod p
      = y^((p-1)/2) × y mod p

    By Fermat's Little Theorem, y^(p-1) ≡ 1 (mod p) for non-zero y, so:

        y^((p-1)/2) ≡ ±1 (mod p)

    For quadratic residues (values that have a square root), Euler's criterion
    guarantees y^((p-1)/2) ≡ 1 (mod p). Therefore:

        y^((p-1)/2) × y = 1 × y = y (mod p)

    SECP160r1 Parameters (Reference)
    --------------------------------
    - Field: GF(2^160 - 2^31 - 1)
    - p = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF7FFFFFFF
    - p mod 4 = 3 ✓ (algorithm applicable)
    - Coordinate length: 20 bytes (160 bits)

    Point Compression Standard (ANSI X9.62)
    ----------------------------------------
    - Compressed format: 0x02/0x03 || X (prefix + x-coordinate)
    - 0x02 = even Y, 0x03 = odd Y
    - This function recovers the EVEN Y (canonical form used by FMDN)

    The ±Y Ambiguity
    ----------------
    If y is a valid square root, then -y (which equals p - y in modular
    arithmetic) is also a valid root. By convention, FMDN uses the EVEN
    root (y mod 2 = 0) as the canonical form.

    Args:
        Rx: X coordinate as integer (160-bit for SECP160r1, 256-bit for P-256).
        curve: The elliptic curve object providing p, a, b parameters.

    Returns:
        The even Y coordinate as integer.

    Raises:
        ValueError: If X is not on the curve (y² is not a quadratic residue,
            meaning no valid Y exists for this X on the curve).

    References:
        - RFC 5480: ECC SubjectPublicKeyInfo Format
        - ANSI X9.62: Public Key Cryptography for the Financial Services Industry
        - NIST FIPS 186-4: Digital Signature Standard (DSS)
        - Tonelli-Shanks: https://en.wikipedia.org/wiki/Tonelli-Shanks_algorithm

    Example:
        >>> from ecdsa import SECP160r1
        >>> Rx = 0x4A96B5688EF573284664698968C38BB913CBFC82
        >>> Ry = rx_to_ry(Rx, SECP160r1.curve)
        >>> # Verify point is on curve:
        >>> curve = SECP160r1.curve
        >>> lhs = (Ry * Ry) % curve.p()
        >>> rhs = (Rx**3 + curve.a() * Rx + curve.b()) % curve.p()
        >>> assert lhs == rhs, "Point not on curve"
    """
    p: int = int(curve.p())
    a: int = int(curve.a())
    b: int = int(curve.b())

    Rx_mod: int = Rx % p

    # Compute y^2 = x^3 + a·x + b (mod p)
    Ryy: int = (pow(Rx_mod, 3, p) + (a * Rx_mod) + b) % p

    # For p ≡ 3 (mod 4): y = (y^2)^((p+1)//4) mod p is a square root
    sqrt_candidate: int = pow(Ryy, (p + 1) // 4, p)

    # Verify root
    if (sqrt_candidate * sqrt_candidate) % p != Ryy:
        raise ValueError("The provided X coordinate is not on the curve.")

    # Ensure even y (standardized choice)
    if sqrt_candidate % 2 != 0:
        sqrt_candidate = p - sqrt_candidate

    Ry: int = int(sqrt_candidate)
    return Ry


class _PointBackend(Protocol):
    """Arithmetic a curve backend provides; scalars are already range-checked."""

    def validate_peer_x(self, peer_x: int) -> None:
        """Raise ``ForeignReportStructureError`` unless ``peer_x`` is on the curve."""

    def point_x(self, scalar: int) -> int:
        """Return the x-coordinate of ``scalar * G``."""

    def ecdh_x(self, scalar: int, peer_x: int) -> int:
        """Return the x-coordinate of ``scalar * S`` for the point ``S`` at ``peer_x``."""


class _Secp160r1Backend:
    """SECP160R1 arithmetic on the ``ecdsa`` package (via ``_ecdsa_shim``)."""

    def _peer_point(self, peer_x: int) -> Any:
        curve = load_curve()
        curve_fp: CurveFpProtocol = curve.curve
        p: int = int(curve_fp.p())
        if peer_x >= p:
            raise ForeignReportStructureError(
                "Sx is not a valid SECP160R1 field element"
            )
        try:
            Ry: int = rx_to_ry(peer_x, curve_fp)
        except ValueError as err:
            raise ForeignReportStructureError("Sx is not on SECP160R1") from err
        point_class = load_point_class()
        return point_class(curve_fp, peer_x, Ry)

    def validate_peer_x(self, peer_x: int) -> None:
        self._peer_point(peer_x)

    def point_x(self, scalar: int) -> int:
        product = scalar * load_curve().generator
        x_int: int = int(product.x())
        return x_int

    def ecdh_x(self, scalar: int, peer_x: int) -> int:
        product = scalar * self._peer_point(peer_x)
        shared_x: int = int(product.x())
        return shared_x


class _P256Backend:
    """SECP256R1 (P-256) arithmetic on the ``cryptography`` package.

    ``from_encoded_point`` validates that the point lies on the curve; the
    compressed prefix ``0x02`` selects the even Y. The ECDH x-coordinate does
    not depend on the sign of Y, so reports built from an odd-Y ``S`` decrypt
    the same way.
    """

    def _peer_public_key(self, peer_x: int) -> object:
        ec = get_ec_module()
        encoded: bytes = b"\x02" + peer_x.to_bytes(32, "big")
        try:
            peer_key: object = ec.EllipticCurvePublicKey.from_encoded_point(
                get_p256_curve(), encoded
            )
        except ValueError as err:
            raise ForeignReportStructureError("Sx is not on SECP256R1") from err
        return peer_key

    def validate_peer_x(self, peer_x: int) -> None:
        self._peer_public_key(peer_x)

    def point_x(self, scalar: int) -> int:
        ec = get_ec_module()
        private_key = ec.derive_private_key(scalar, get_p256_curve())
        x_int: int = int(private_key.public_key().public_numbers().x)
        return x_int

    def ecdh_x(self, scalar: int, peer_x: int) -> int:
        ec = get_ec_module()
        peer_key = self._peer_public_key(peer_x)
        private_key = ec.derive_private_key(scalar, get_p256_curve())
        shared: bytes = private_key.exchange(ec.ECDH(), peer_key)
        return int.from_bytes(shared, "big")


@dataclass(frozen=True, slots=True)
class FmdnCurve:
    """One FMDN curve: its names, coordinate length, order and arithmetic.

    Attributes:
        name: Library-neutral curve name (``secp160r1``, ``secp256r1``).
        short_name: Name used inside reading identifiers (``secp160r1``, ``p256``).
        coord_len: Length of an x-coordinate (and of the EID) in bytes.
        order: The group order ``n``.
        backend: Arithmetic backend; not part of equality or ``repr``.
    """

    name: str
    short_name: str
    coord_len: int
    order: int
    backend: _PointBackend = field(repr=False, compare=False)

    def _require_scalar(self, scalar: int) -> None:
        if not 0 < scalar < self.order:
            raise ValueError(f"scalar must lie in [1, n - 1] for {self.name}")

    def _require_peer_len(self, peer_x: bytes) -> None:
        if len(peer_x) != self.coord_len:
            raise ForeignReportStructureError(
                f"Sx must be {self.coord_len} bytes for {self.name} (got {len(peer_x)})"
            )

    def validate_peer_x(self, peer_x: bytes) -> None:
        """Check that ``peer_x`` is an x-coordinate of a point on this curve.

        Raises:
            ForeignReportStructureError: On a wrong length, a value outside
                the field, or an x-coordinate without a point on the curve.
        """
        self._require_peer_len(peer_x)
        self.backend.validate_peer_x(int.from_bytes(peer_x, "big"))

    def point_x(self, scalar: int) -> bytes:
        """Return the big-endian x-coordinate of ``scalar * G``.

        Raises:
            ValueError: If ``scalar`` is outside ``[1, n - 1]``.
        """
        self._require_scalar(scalar)
        x_int: int = self.backend.point_x(scalar)
        return x_int.to_bytes(self.coord_len, "big")

    def ecdh_x(self, scalar: int, peer_x: bytes) -> bytes:
        """Return the big-endian x-coordinate of ``scalar * S``.

        ``S`` is the point whose x-coordinate is ``peer_x``; the even Y is
        chosen, which does not change the result.

        Raises:
            ForeignReportStructureError: If ``peer_x`` is not usable (see
                ``validate_peer_x``).
            ValueError: If ``scalar`` is outside ``[1, n - 1]``.
        """
        self._require_peer_len(peer_x)
        self._require_scalar(scalar)
        shared_x: int = self.backend.ecdh_x(scalar, int.from_bytes(peer_x, "big"))
        return shared_x.to_bytes(self.coord_len, "big")


SECP160R1: Final[FmdnCurve] = FmdnCurve(
    name="secp160r1",
    short_name="secp160r1",
    coord_len=20,
    order=SECP160R1_ORDER,
    backend=_Secp160r1Backend(),
)
SECP256R1: Final[FmdnCurve] = FmdnCurve(
    name="secp256r1",
    short_name="p256",
    coord_len=32,
    order=P256_ORDER,
    backend=_P256Backend(),
)

CURVES_BY_COORD_LEN: Final[Mapping[int, FmdnCurve]] = MappingProxyType(
    {curve.coord_len: curve for curve in (SECP160R1, SECP256R1)}
)


def curve_for_coord_len(length: int) -> FmdnCurve:
    """Return the curve whose coordinates are ``length`` bytes long.

    Raises:
        UnsupportedCurveError: If no registered curve has that length; the
            exception carries the length as ``sx_len``.
    """
    curve = CURVES_BY_COORD_LEN.get(length)
    if curve is None:
        raise UnsupportedCurveError(length)
    return curve
