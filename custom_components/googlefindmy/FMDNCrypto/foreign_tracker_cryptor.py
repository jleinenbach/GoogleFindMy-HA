#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Crypto primitives used to encrypt/decrypt Find My Device payloads with ECDH on
an FMDN curve and AES-EAX for authenticated encryption.

Design goals (feature-neutral, HA-friendly):
- Keep public function signatures unchanged.
- Add clear docstrings, type hints and defensive checks.
- Avoid undefined behavior (e.g., s == 0, invalid lengths).
- Prefer explicitness and readability over micro-optimizations.

Notes:
- ``decrypt_foreign_report`` selects the curve from the length of ``Sx``
  (``curve_profile.curve_for_coord_len``): 20 bytes SECP160R1, 32 bytes
  SECP256R1. ``decrypt`` is a thin wrapper around it for one identity key.
- A reading (``ForeignReading``) names one way to derive the owner-side
  scalar and nonce. SECP160R1 has one reading; SECP256R1 has six
  provisional readings, tried in the order of their evidence, until field
  reports confirm which one real trackers use. Every scalar derivation that
  a full-length ``EidVariant`` uses (``eid_generator.VARIANT_DERIVATIONS``)
  appears among the readings of its curve; the test suite pins that binding.
  Truncated P-256 variants (20 of 32 bytes) are excluded: a finder cannot run
  a P-256 ECDH from them, so their reports are not decryptable.
- ``encrypt`` still covers SECP160R1 only (20-byte EIDs).
"""

# custom_components/googlefindmy/FMDNCrypto/foreign_tracker_cryptor.py

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from custom_components.googlefindmy.example_data_provider import get_example_data
from custom_components.googlefindmy.FMDNCrypto._ecdsa_shim import (
    CurveParametersProtocol,
    load_curve,
    load_curve_fp_class,
    load_point_class,
)
from custom_components.googlefindmy.FMDNCrypto._lazy_crypto import (
    get_aes_class,
    get_hashes_module,
    get_hkdf_class,
)
from custom_components.googlefindmy.FMDNCrypto.curve_profile import (
    BE_MOD_N,
    BE_PLUS_ONE,
    LE_PLUS_ONE,
    SECP160R1,
    SECP256R1,
    FmdnCurve,
    ScalarDerivation,
    curve_for_coord_len,
    reduce_scalar,
)
from custom_components.googlefindmy.FMDNCrypto.curve_profile import (
    rx_to_ry as rx_to_ry,  # noqa: PLC0414 - re-exported for callers of the cryptor
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    EIK_LENGTH,
    FHNA_K,
    VARIANT_DERIVATIONS,
    EidVariant,
    build_table10_prf_input,
    generate_eid_variant,
    prf_aes_256_ecb,
)
from custom_components.googlefindmy.FMDNCrypto.foreign_report_errors import (
    ForeignReportAuthError,
    ForeignReportStructureError,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# AES-EAX authenticates with a 16-byte tag by default in PyCryptodome.
_AES_KEY_LEN: int = 32
_AES_TAG_LEN: int = 16
# SECP160r1 coordinate length in bytes (160 bits)
_COORD_LEN: int = 20
# Nonce used by ``encrypt``: LRx(8) || LSx(8) = 16 bytes (SECP160R1 practice)
_NONCE_LEN: int = 16

# Single definition of the place where users post which reading decrypted
# their P-256 reports (read by the log line and the diagnostics block).
FOREIGN_READING_FEEDBACK_URL: Final[str] = (
    "https://github.com/BSkando/GoogleFindMy-HA/issues/223"
)

# Sources of the readings below: a public URL or a commit of this repository.
_SRC_SPEC: Final[str] = (
    "https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn"
)
_SRC_FORK_PLUS1: Final[str] = "commit 6c95f0f5 (MODERN_P256_* EID variants)"
_SRC_FORK_LE: Final[str] = "commit f9bd9ece (little-endian r' of MODERN_P256_*_LE)"


@dataclass(frozen=True, slots=True)
class ForeignReading:
    """One way to derive the owner-side scalar and nonce for a foreign report.

    Attributes:
        curve: The curve the reading applies to.
        derivation: How the PRF output ``r'`` is read and reduced to ``r``.
        nonce_half_len: Bytes taken from the low end of ``Rx`` and of ``Sx``;
            the nonce is ``Rx[-h:] || Sx[-h:]``.
        source: Public URL or commit of this repository that motivates it.
        provisional: True while no field report has confirmed the reading.
    """

    curve: FmdnCurve
    derivation: ScalarDerivation
    nonce_half_len: int
    source: str
    provisional: bool

    def __post_init__(self) -> None:
        """Reject a nonce half that does not fit into one coordinate."""
        if not 0 < self.nonce_half_len <= self.curve.coord_len:
            raise ValueError(
                f"nonce_half_len must lie in [1, {self.curve.coord_len}] "
                f"for {self.curve.name} (got {self.nonce_half_len})"
            )

    @property
    def reading_id(self) -> str:
        """Stable identifier ``{curve}/{rule}[_le]/nonce{h}``, e.g. ``p256/mod_n/nonce8``.

        Derived from the fields, never stored twice. The middle segment is
        ``ScalarDerivation.id_token`` and is never split at ``_``.
        """
        return (
            f"{self.curve.short_name}/{self.derivation.id_token}/"
            f"nonce{self.nonce_half_len}"
        )


SECP160R1_FOREIGN_READINGS: Final[tuple[ForeignReading, ...]] = (
    ForeignReading(
        curve=SECP160R1,
        derivation=BE_MOD_N,
        nonce_half_len=8,
        source=_SRC_SPEC,
        provisional=False,
    ),
)

# Ordered by evidence: (1) the specification's scalar with the 8+8 nonce used
# in SECP160R1 practice, (2) the specification's scalar with the literal
# "lower 80 bits" nonce, (3) and (4) the integration's existing ``+1``
# projection with either nonce, (5) and (6) the same projection with ``r'``
# read little-endian. No measured source reads ``r'`` little-endian: the
# specification, Nordic's fp_crypto and Atmosic's gfp_crypto (which reverses
# the bytes before reducing) all read it big-endian. The claims in f9bd9ece
# ("Some tracker firmware ...") and 104542e8 (Moto Tag) cite no source.
# Readings 5 and 6 exist because the resolver recognises trackers through
# ``MODERN_P256_X32_LE_SCALAR``; without them such a tracker would be matched
# but its reports would never decrypt.
# fmt: off
P256_FOREIGN_READINGS: Final[tuple[ForeignReading, ...]] = (
    ForeignReading(curve=SECP256R1, derivation=BE_MOD_N, nonce_half_len=8, source=_SRC_SPEC, provisional=True),
    ForeignReading(curve=SECP256R1, derivation=BE_MOD_N, nonce_half_len=10, source=_SRC_SPEC, provisional=True),
    ForeignReading(curve=SECP256R1, derivation=BE_PLUS_ONE, nonce_half_len=8, source=_SRC_FORK_PLUS1, provisional=True),
    ForeignReading(curve=SECP256R1, derivation=BE_PLUS_ONE, nonce_half_len=10, source=_SRC_FORK_PLUS1, provisional=True),
    ForeignReading(curve=SECP256R1, derivation=LE_PLUS_ONE, nonce_half_len=8, source=_SRC_FORK_LE, provisional=True),
    ForeignReading(curve=SECP256R1, derivation=LE_PLUS_ONE, nonce_half_len=10, source=_SRC_FORK_LE, provisional=True),
)
# fmt: on
"""Candidate readings for P-256 foreign reports, all provisional.

Every reading is tried until the AES-EAX tag verifies; the winner is
remembered per device and reported once on the INFO line and in the
diagnostics block, both pointing users to ``FOREIGN_READING_FEEDBACK_URL``.

Removal criterion: once a field report posted to the feedback URL (the INFO
line or the diagnostics block) confirms one P-256 reading for a real tracker,
and no report contradicts it (only an INFO line naming a different P-256
reading contradicts a confirmation; a WARNING does not),
the other P-256 readings become removal candidates for the next release.
A reading whose derivation an ``EidVariant`` still uses is removed only
together with that variant (``TestReadingsBoundToVariants`` in
``tests/test_foreign_tracker_cryptor_p256.py`` enforces this); removing a
variant requires that stored locks naming it are discarded, not
reinterpreted (``eid_resolver.py`` discards a stored lock whose variant is
unknown). Until then they stay, tracked in the section
"Open item: provisional P-256 readings" of ``docs/CRYPTOGRAPHY.md``; they are
not kept silently.
"""

# Keyed by ``FmdnCurve.name``; ``short_name`` only appears inside reading_id.
READINGS_BY_CURVE: Final[Mapping[str, tuple[ForeignReading, ...]]] = MappingProxyType(
    {
        SECP160R1.name: SECP160R1_FOREIGN_READINGS,
        SECP256R1.name: P256_FOREIGN_READINGS,
    }
)

# Every nonce length a reading can produce (16 and 20 bytes).
_NONCE_LENS: Final[frozenset[int]] = frozenset(
    2 * reading.nonce_half_len
    for readings in READINGS_BY_CURVE.values()
    for reading in readings
)


@dataclass(frozen=True, slots=True)
class ForeignDecryptResult:
    """Outcome of a successful ``decrypt_foreign_report`` call.

    Attributes:
        plaintext: The authenticated plaintext.
        key_index: Position of the identity key that decrypted the report.
        reading: The reading whose tag verified.
    """

    plaintext: bytes
    key_index: int
    reading: ForeignReading


# Use module-level caching for lazy-loaded curve instances
# The getters always return valid objects after first load


def _get_curve() -> CurveParametersProtocol:
    """Get the SECP160r1 curve, loading lazily on first access."""
    return load_curve()


def _get_curve_fp() -> type:
    """Get the CurveFp class, loading lazily on first access."""
    return load_curve_fp_class()


def _get_point() -> type:
    """Get the Point class, loading lazily on first access."""
    return load_point_class()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_len(name: str, b: bytes, expected: int) -> None:
    """Validate a fixed length for bytes-like inputs."""
    if len(b) != expected:
        raise ValueError(f"{name} must be exactly {expected} bytes (got {len(b)})")


# ---------------------------------------------------------------------------
# AES-EAX wrappers (authenticated encryption)
# ---------------------------------------------------------------------------


def encrypt_aes_eax(data: bytes, nonce: bytes, key: bytes) -> tuple[bytes, bytes]:
    """Encrypt and authenticate with AES-EAX-256.

    Args:
        data: Plaintext bytes.
        nonce: 16-byte nonce used for EAX.
        key: 32-byte AES key (AES-256).

    Returns:
        (ciphertext, tag) where tag is 16 bytes.

    Raises:
        ValueError: On invalid nonce/key lengths.
    """
    _require_len("nonce", nonce, _NONCE_LEN)
    _require_len("key", key, _AES_KEY_LEN)

    AES = get_aes_class()
    cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
    m_dash, tag = cipher.encrypt_and_digest(data)
    return m_dash, tag


def _eax_cipher(nonce: bytes, key: bytes, tag: bytes) -> Any:
    """Validate lengths and return an AES-EAX-256 cipher for decryption."""
    if len(nonce) not in _NONCE_LENS:
        raise ValueError(
            f"nonce must be one of {sorted(_NONCE_LENS)} bytes (got {len(nonce)})"
        )
    _require_len("key", key, _AES_KEY_LEN)
    _require_len("tag", tag, _AES_TAG_LEN)
    AES = get_aes_class()
    return AES.new(key, AES.MODE_EAX, nonce=nonce)


def decrypt_aes_eax(m_dash: bytes, tag: bytes, nonce: bytes, key: bytes) -> bytes:
    """Decrypt and verify AES-EAX-256 payloads.

    Raises:
        ValueError: On a nonce length no reading produces, on invalid key/tag
            lengths, or when the tag does not verify.
    """
    plaintext: bytes = _eax_cipher(nonce, key, tag).decrypt_and_verify(m_dash, tag)
    return plaintext


def _try_decrypt_aes_eax(
    m_dash: bytes, tag: bytes, nonce: bytes, key: bytes
) -> bytes | None:
    """Like ``decrypt_aes_eax``, but return None when the tag does not verify.

    A failed tag is the expected outcome for every wrong reading or key, so
    ``decrypt_foreign_report`` tests readings with this helper and raises one
    aggregated ``ForeignReportAuthError`` after all attempts. Length errors
    still raise.
    """
    cipher = _eax_cipher(nonce, key, tag)
    try:
        plaintext: bytes = cipher.decrypt_and_verify(m_dash, tag)
    except ValueError:  # PyCryptodome signals a tag mismatch this way
        return None
    return plaintext


# ---------------------------------------------------------------------------
# SECP160r1 EID helpers
# ---------------------------------------------------------------------------


def calculate_r(identity_key: bytes, time_counter_u32: int) -> int:
    """Derive the scalar ``r`` for SECP160r1 from the Table 10 PRF output."""

    prf_input = build_table10_prf_input(time_counter_u32, k=FHNA_K)
    prf_output = prf_aes_256_ecb(identity_key, prf_input)
    # The derivation of LEGACY_SECP160R1_X20_BE, read from the variant table.
    curve, derivation = VARIANT_DERIVATIONS[EidVariant.LEGACY_SECP160R1_X20_BE]
    return reduce_scalar(
        derivation.read_prf_output(prf_output), curve.order, derivation.rule
    )


def encrypt(message: bytes, random: bytes, eid: bytes) -> tuple[bytes, bytes]:
    """Encrypt a message for a tracker identity using ECDH + AES-EAX-256.

    Args:
        message: Plaintext to encrypt.
        random: Caller-provided random bytes (entropy source for s).
        eid: 20-byte X coordinate (compressed point) for the receiver.

    Returns:
        (encrypted_with_tag, Sx) where encrypted_with_tag = m' || tag (tag=16B),
        and Sx is 20-byte X coordinate of S.

    Raises:
        ValueError: On invalid inputs (lengths) or curve mismatch.
    """
    # Curve parameters
    curve = _get_curve()
    order: int = int(curve.order)

    # Validate EID length (x coordinate on SECP160r1)
    _require_len("eid", eid, _COORD_LEN)

    # Derive scalar s from caller-provided randomness; guard s != 0
    s = int.from_bytes(random, byteorder="big", signed=False) % order
    if s == 0:
        # Extremely unlikely; avoid the point at infinity by bumping to 1
        s = 1

    # S = s·G
    generator = curve.generator
    S = s * generator

    # Rebuild R from EID (x only) and choose even Y
    Rx = int.from_bytes(eid, byteorder="big")
    Ry = rx_to_ry(Rx, curve.curve)
    Point = _get_point()
    R = Point(curve.curve, Rx, Ry)

    # Derive AES-256 key via HKDF-SHA256 over (s·R).x (20 bytes)
    HKDF = get_hkdf_class()
    hashes = get_hashes_module()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"")
    k: bytes = hkdf.derive((s * R).x().to_bytes(_COORD_LEN, "big"))

    # Nonce = LRx(8) || LSx(8)
    LRx = Rx.to_bytes(_COORD_LEN, "big")[-8:]
    LSx = S.x().to_bytes(_COORD_LEN, "big")[-8:]
    nonce: bytes = LRx + LSx  # 16 bytes

    # Encrypt (AES-EAX-256) → m' || tag
    m_dash, tag = encrypt_aes_eax(message, nonce, k)
    encrypted_with_tag: bytes = m_dash + tag
    return encrypted_with_tag, S.x().to_bytes(_COORD_LEN, "big")


def _ordered_readings(
    readings: tuple[ForeignReading, ...], preferred_reading_id: str | None
) -> tuple[ForeignReading, ...]:
    """Return ``readings`` with the preferred one first; unknown ids are ignored."""
    preferred = [r for r in readings if r.reading_id == preferred_reading_id]
    return (*preferred, *(r for r in readings if r.reading_id != preferred_reading_id))


def _shared_material(
    curve: FmdnCurve, r_dash: bytes, derivation: ScalarDerivation, sx: bytes
) -> tuple[bytes, bytes] | None:
    """Return ``(Rx, AES key)`` for one derivation, or None for a zero scalar."""
    r = reduce_scalar(derivation.read_prf_output(r_dash), curve.order, derivation.rule)
    if r == 0:
        # 0 * G is the point at infinity: this reading cannot apply.
        return None
    rx = curve.point_x(r)
    HKDF = get_hkdf_class()
    hashes = get_hashes_module()
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"")
    aes_key: bytes = hkdf.derive(curve.ecdh_x(r, sx))
    return rx, aes_key


def decrypt_foreign_report(
    identity_keys: Iterable[bytes],
    encrypted_and_tag: bytes,
    sx: bytes,
    beacon_time_counter: int,
    *,
    preferred_reading_id: str | None = None,
) -> ForeignDecryptResult:
    """Decrypt a crowdsourced location report with every key and reading.

    Construction per key and reading (Find Hub Network Accessory
    Specification, "EID computation"; Boettger et al., PoPETs 2025(4),
    section 4.1.5):

    1) Select the curve from ``len(sx)``; check the report structure once.
    2) ``r'`` = AES-ECB-256(EIK, Table 10 input); ``r`` from the reading's
       scalar derivation (byte order and rule); ``R = r * G``.
    3) ``k`` = HKDF-SHA256((r * S).x), with ``S`` rebuilt from ``sx``.
    4) ``nonce`` = ``Rx[-h:] || Sx[-h:]`` with ``h`` the reading's half length.
    5) AES-EAX-256 decrypt and verify ``m' || tag``.

    The PRF runs once per key; the scalar, ``R`` and the ECDH run once per key
    and distinct derivation (three for P-256); each reading adds one tag
    check. The preferred reading is tried first.

    Args:
        identity_keys: 32-byte identity key candidates, primary key first.
        encrypted_and_tag: Ciphertext concatenated with the 16-byte tag.
        sx: X coordinate of the finder's ephemeral point ``S``.
        beacon_time_counter: Time counter the report was encrypted for.
        preferred_reading_id: ``reading_id`` that decrypted earlier reports
            of the same device, if known.

    Returns:
        The plaintext, the index of the matching key and the reading.

    Raises:
        ValueError: On an empty key list or a key that is not 32 bytes.
        UnsupportedCurveError: If ``len(sx)`` maps to no supported curve.
        ForeignReportStructureError: On a short payload or an ``sx`` that is
            not on the selected curve; raised before any key is used.
        ForeignReportAuthError: If no key and no reading verified the tag.
    """
    keys: tuple[bytes, ...] = tuple(identity_keys)
    if not keys:
        raise ValueError("identity_keys must contain at least one key")
    for key in keys:
        _require_len("identity_key", key, EIK_LENGTH)

    # Structure checks: once, before any key candidate.
    curve = curve_for_coord_len(len(sx))
    if len(encrypted_and_tag) < _AES_TAG_LEN:
        raise ForeignReportStructureError(
            "encryptedAndTag must be at least 16 bytes (contains tag)."
        )
    curve.validate_peer_x(sx)

    readings = _ordered_readings(READINGS_BY_CURVE[curve.name], preferred_reading_id)
    m_dash: bytes = encrypted_and_tag[:-_AES_TAG_LEN]
    tag: bytes = encrypted_and_tag[-_AES_TAG_LEN:]
    prf_input = build_table10_prf_input(beacon_time_counter, k=FHNA_K)

    for key_index, key in enumerate(keys):
        r_dash = prf_aes_256_ecb(key, prf_input)
        material_by_derivation: dict[ScalarDerivation, tuple[bytes, bytes] | None] = {}
        for reading in readings:
            derivation = reading.derivation
            if derivation not in material_by_derivation:
                material_by_derivation[derivation] = _shared_material(
                    curve, r_dash, derivation, sx
                )
            material = material_by_derivation[derivation]
            if material is None:
                continue
            rx, aes_key = material
            half = reading.nonce_half_len
            plaintext = _try_decrypt_aes_eax(
                m_dash, tag, rx[-half:] + sx[-half:], aes_key
            )
            if plaintext is not None:
                return ForeignDecryptResult(plaintext, key_index, reading)

    raise ForeignReportAuthError(
        curve.name,
        tuple(reading.reading_id for reading in readings),
        len(keys),
    )


def decrypt(
    identity_key: bytes, encryptedAndTag: bytes, Sx: bytes, beacon_time_counter: int
) -> bytes:
    """Decrypt a foreign location report for one identity key.

    Thin wrapper around ``decrypt_foreign_report``; the curve follows from the
    length of ``Sx`` (20 bytes SECP160R1, 32 bytes SECP256R1).

    Args:
        identity_key: 32-byte Ephemeral Identity Key (EIK) used as AES-256
            key for the Table-10 PRF and EID derivation.
        encryptedAndTag: Ciphertext concatenated with 16-byte tag.
        Sx: X coordinate of ephemeral S (20 or 32 bytes).
        beacon_time_counter: Time counter used to derive r.

    Returns:
        Decrypted plaintext.

    Raises:
        ValueError: On invalid input lengths or verification failure; the
            ``ForeignReportError`` subclasses name the cause.
    """
    result = decrypt_foreign_report(
        (identity_key,), encryptedAndTag, Sx, beacon_time_counter
    )
    return result.plaintext


# ---------------------------------------------------------------------------
# CLI helpers (manual testing)
# ---------------------------------------------------------------------------


def _get_keys() -> tuple[bytes, bytes]:
    # Returns a test identity key and public key pair
    identity_key = bytes.fromhex(get_example_data("sample_identity_key"))
    public_key = bytes.fromhex(get_example_data("sample_public_key"))
    return identity_key, public_key


def _get_random_bytes(length: int) -> bytes:
    # Returns random bytes of a specified length
    return secrets.token_bytes(length)


def _create_random_eid(identity_key: bytes) -> bytes:
    # Uses generate_eid_variant to create a random EID
    beacon_time_counter: int = int.from_bytes(_get_random_bytes(4), byteorder="big")
    return generate_eid_variant(
        identity_key,
        beacon_time_counter,
        EidVariant.LEGACY_SECP160R1_X20_BE,
    )


async def _async_cli() -> None:  # pragma: no cover - manual testing only
    # Example usage
    identity_key, public_key = _get_keys()
    beacon_time_counter = 0

    # Generate random data to encrypt
    random_data = _get_random_bytes(_COORD_LEN)
    eid = _create_random_eid(identity_key)

    # Encrypt
    encryptedAndTag, Sx = encrypt(random_data, random_data, eid)
    print(f"Encrypted: {encryptedAndTag.hex()}")

    # Decrypt
    decrypted = decrypt(identity_key, encryptedAndTag, Sx, beacon_time_counter)
    print(f"Decrypted: {decrypted.hex()}")


def _cli() -> None:  # pragma: no cover - manual testing only
    asyncio.run(_async_cli())


if __name__ == "__main__":  # pragma: no cover - manual testing only
    _cli()
