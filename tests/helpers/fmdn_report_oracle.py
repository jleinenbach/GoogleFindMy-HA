# tests/helpers/fmdn_report_oracle.py
"""Independent builder for P-256 FMDN location reports, for tests only.

The builder plays the finder and rebuilds every step from the published
specification instead of importing the integration: the Table 10 PRF input,
the AES-ECB-256 PRF, the owner scalar ``r``, ``R = r * G``, ECDH with an
ephemeral finder key, HKDF-SHA256 and AES-EAX-256. It imports only
``cryptography``, ``Cryptodome`` and the standard library, so a test built on
it checks the integration against the specification and not against itself.

References:
    - Find Hub Network Accessory Specification, sections "EID computation"
      (Table 10, ``r = r' mod n``) and the location-report decryption steps
      (``nonce = LRx || LSx``):
      https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn
    - SEC 2 v2, section 2.4.2 (secp256r1 group order ``n``):
      https://www.secg.org/sec2-v2.pdf
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from Cryptodome.Cipher import AES
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "P256_N",
    "OracleReport",
    "ScalarRuleName",
    "build_p256_report",
    "ephemeral_scalar_with_y_parity",
    "owner_scalar",
    "p256_x",
    "table10_prf_input",
]

# secp256r1 group order n (SEC 2 v2, section 2.4.2).
P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
# Rotation period exponent K; the specification fixes it to 10.
ROTATION_EXPONENT_K = 10

ScalarRuleName = Literal["mod_n", "plus1"]


def table10_prf_input(counter: int) -> bytes:
    """Return the 32-byte Table 10 block for a 32-bit beacon time counter.

    Octets 0-10 are 0xFF, octet 11 is K, octets 12-15 the counter (big
    endian, K lowest bits cleared), octets 16-26 are 0x00, octet 27 is K and
    octets 28-31 repeat the counter.
    """
    k = ROTATION_EXPONENT_K
    ts = (counter & ~((1 << k) - 1) & 0xFFFFFFFF).to_bytes(4, "big")
    return b"\xff" * 11 + bytes([k]) + ts + b"\x00" * 11 + bytes([k]) + ts


def _prf(eik: bytes, block: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(eik), modes.ECB()).encryptor()
    return encryptor.update(block) + encryptor.finalize()


def owner_scalar(eik: bytes, counter: int, rule: ScalarRuleName) -> int:
    """Return the owner scalar ``r`` under the named rule.

    ``mod_n`` is the specification's ``r' mod n``; ``plus1`` is the
    ``(r' mod (n - 1)) + 1`` projection the tests also have to cover.
    """
    r_dash = int.from_bytes(_prf(eik, table10_prf_input(counter)), "big")
    if rule == "mod_n":
        return r_dash % P256_N
    return (r_dash % (P256_N - 1)) + 1


def p256_x(scalar: int) -> bytes:
    """Return the 32-byte big-endian x-coordinate of ``scalar * G``."""
    numbers = (
        ec.derive_private_key(scalar, ec.SECP256R1()).public_key().public_numbers()
    )
    return numbers.x.to_bytes(32, "big")


def ephemeral_scalar_with_y_parity(*, odd_y: bool, start: int = 2) -> int:
    """Return the smallest scalar ``>= start`` whose point has the wanted Y parity."""
    scalar = start
    while True:
        numbers = (
            ec.derive_private_key(scalar, ec.SECP256R1()).public_key().public_numbers()
        )
        if bool(numbers.y & 1) is odd_y:
            return scalar
        scalar += 1


@dataclass(frozen=True)
class OracleReport:
    """A finder-side report plus the values a test may want to compare."""

    encrypted_and_tag: bytes
    sx: bytes
    sy_prefix: bytes
    rx: bytes


def build_p256_report(  # noqa: PLR0913 - mirrors the report parameters one to one
    eik: bytes,
    counter: int,
    plaintext: bytes,
    *,
    rule: ScalarRuleName,
    nonce_half_len: int,
    ephemeral_scalar: int,
) -> OracleReport:
    """Encrypt ``plaintext`` the way a finder does for a P-256 tracker.

    The tracker's public point is ``R = r * G`` with ``r`` from ``rule``; the
    finder uses ``S = ephemeral_scalar * G``. The nonce is
    ``Rx[-h:] || Sx[-h:]`` with ``h = nonce_half_len``.
    """
    curve = ec.SECP256R1()
    rx = p256_x(owner_scalar(eik, counter, rule))
    finder_key = ec.derive_private_key(ephemeral_scalar, curve)
    numbers = finder_key.public_key().public_numbers()
    sx = numbers.x.to_bytes(32, "big")
    sy_prefix = b"\x03" if numbers.y & 1 else b"\x02"

    tracker_point = ec.EllipticCurvePublicKey.from_encoded_point(curve, b"\x02" + rx)
    shared_x = finder_key.exchange(ec.ECDH(), tracker_point)
    aes_key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"").derive(
        shared_x
    )
    nonce = rx[-nonce_half_len:] + sx[-nonce_half_len:]
    cipher = AES.new(aes_key, AES.MODE_EAX, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return OracleReport(ciphertext + tag, sx, sy_prefix, rx)
