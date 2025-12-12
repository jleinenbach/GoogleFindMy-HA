"""Ephemeral identifier derivation helpers for the FHN Accessory spec."""

from __future__ import annotations

import logging
from typing import cast

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.googlefindmy.example_data_provider import get_example_data
from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import (
    CurveParametersProtocol,
    load_curve,
)

# Constants
K = 10
ROTATION_PERIOD = 1024  # 2^K seconds
EIK_LENGTH = 32
_CURVE: CurveParametersProtocol = load_curve()

_LOGGER = logging.getLogger(__name__)


def _prf_table10(identity_key: bytes, timestamp: int, k: int = K) -> bytes:
    """Derive the 32-byte Table 10 pseudorandom output.

    FHN Accessory Specification v1.3 — Table 10 defines the 32-byte AES-256-ECB
    input buffer used to generate the ephemeral scalar. The layout is:

    * ``0xFF`` repeated for 11 bytes
    * ``K`` (rotation exponent)
    * ``ts_bytes`` (timestamp with the lowest ``K`` bits masked)
    * ``0x00`` repeated for 11 bytes
    * ``K`` again
    * the same ``ts_bytes``

    This helper mirrors that exact construction and encrypts the entire buffer
    with AES-256-ECB to produce the 32-byte seed shared by both legacy and
    modern EID derivations.
    """

    ts_bytes = get_masked_timestamp(timestamp, k)

    data = bytearray(32)
    data[0:11] = b"\xff" * 11
    data[11] = k
    data[12:16] = ts_bytes
    data[16:27] = b"\x00" * 11
    data[27] = k
    data[28:32] = ts_bytes

    cipher = Cipher(algorithms.AES(identity_key), modes.ECB())
    encryptor = cipher.encryptor()
    return encryptor.update(bytes(data)) + encryptor.finalize()


def generate_eid(identity_key: bytes, timestamp: int) -> bytes:
    """Generate the advertised EID per FHN Accessory Specification v1.3.

    The specification defines ``R = r * G`` (with ``r`` derived from the
    pseudorandom construction in Table 10) as the public point; the beacon
    advertises ``Rx`` as the ephemeral identifier for the current rotation
    period. This helper preserves that flow: compute ``r``, multiply by the
    curve generator, and emit the x coordinate.
    """
    # Calculate r
    r = calculate_r(identity_key, timestamp)

    # Compute R = r * G
    curve = _CURVE
    generator = curve.generator
    R = r * generator

    # Return the x coordinate of R as the EID
    return cast(bytes, R.x().to_bytes(20, "big"))


def generate_eid_p256(identity_key: bytes, timestamp: int) -> bytes:
    """Generate a modern (NIST P-256) EID using the Table 10 PRF.

    Modern Find My Device Network trackers appear to reuse the FHN Accessory
    Specification v1.3 Table 10 pseudorandom construction. This helper masks
    the timestamp with ``K = 10`` (rotation period ``2^K``), encrypts the 32-byte
    Table 10 buffer with AES-256-ECB, reduces the result modulo the P-256 order
    (coerced into ``1..n-1``), multiplies by the curve generator, and returns the
    32-byte X coordinate of the public point.
    """

    if len(identity_key) != EIK_LENGTH:
        raise ValueError(
            f"Ephemeral Identity Key must be {EIK_LENGTH} bytes; got {len(identity_key)}"
        )

    seed_bytes = _prf_table10(identity_key, timestamp)

    n: int = (
        0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    )
    seed_int: int = int.from_bytes(seed_bytes, byteorder="big")
    private_scalar: int = (seed_int % (n - 1)) + 1

    curve = ec.SECP256R1()
    private_key = ec.derive_private_key(private_scalar, curve)
    public_key = private_key.public_key()

    public_numbers = public_key.public_numbers()
    x_bytes = public_numbers.x.to_bytes(32, byteorder="big")
    return x_bytes


def calculate_r(identity_key: bytes, timestamp: int) -> int:
    """Compute the random scalar ``r`` per FHN Accessory Specification v1.3.

    FHN Accessory Specification v1.3 - Table 10: Construction of a
    pseudorandom number mandates the AES-ECB-256 input layout (``0xFF`` padding,
    repeated timestamp blocks, and fixed rotation period exponent ``K = 10``)
    used below. The byte construction ensures the encrypted block satisfies the
    spec's requirements for producing the pseudorandom value that is later
    reduced modulo the curve order to obtain ``r``.
    """
    r_dash = _prf_table10(identity_key, timestamp, K)

    # Convert r' to an integer
    r_dash_int = int.from_bytes(r_dash, byteorder="big", signed=False)

    # SECP160R1 parameters
    curve = _CURVE
    curve_order: int = int(curve.order)

    # r' is now projected to the finite field Fp by calculating r = r' mod n
    r_mod: int = r_dash_int % curve_order
    return r_mod


def get_masked_timestamp(timestamp: int, K: int) -> bytes:
    """Return the timestamp with the least-significant bits masked.

    Table 10 requires clearing the lowest ``K`` bits of the beacon time counter
    before encrypting the AES input block. This helper mirrors that requirement
    so the pseudorandom number derives from the rotation-aligned timestamp.

    Args:
        timestamp: The original timestamp as an ``int``.
    """
    # Create a bitmask that has all bits set except for the K least significant bits
    mask = ~((1 << K) - 1)

    # Zero out the K least significant bits
    timestamp &= mask

    # Convert back to a byte array with the same length as the original
    return timestamp.to_bytes(4, byteorder="big")


if __name__ == "__main__":
    sample_identity_key_hex = get_example_data("sample_identity_key")
    sample_identity_key = bytes.fromhex(sample_identity_key_hex)

    # Generate EIDs
    for i in range(1000):
        timestamp = i * ROTATION_PERIOD
        eid = generate_eid(sample_identity_key, timestamp)
        print(f"{timestamp}: {eid.hex()}")
