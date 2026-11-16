# custom_components/googlefindmy/NovaApi/ExecuteAction/LocatePhone/phone_location_crypto.py
"""Key and decryption for the location a phone sends after phone action 4.

The locate request carries 16 random bytes; the phone answers with
``PhoneInformation`` field 36 (``PhoneLocationEnvelope``): a ciphertext with IV
(AES-128-CBC under those 16 bytes, PKCS7 padding) and the fix time in field 4. The
plaintext (``PhoneLocationPlaintext``) holds latitude and longitude as doubles and,
in field 2, the accuracy in metres (confirmed against the phone's own reading for
two fixes, 2026-11-15, n=1 phone).

CBC carries no integrity check: whoever can alter the push (Google, or an
attacker inside the TLS path) can flip bits of the first plaintext block through
the IV. The protocol offers nothing better; the range checks below are the only
filter.

Every failure yields ``None``, never an exception. The module logs at most the
failing stage and lengths, never key, IV, ciphertext, plaintext or coordinates.
"""

from __future__ import annotations

import logging
import math
import secrets
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2
from google.protobuf.message import DecodeError

_LOGGER = logging.getLogger(__name__)

LOCATE_KEY_BYTES = 16
_AES_BLOCK_BYTES = 16
_MAX_ABS_LATITUDE = 90.0
_MAX_ABS_LONGITUDE = 180.0
_NANOS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class PhoneFix:
    """One decrypted phone location."""

    latitude: float
    longitude: float
    timestamp: float
    accuracy: float | None

    def __repr__(self) -> str:
        """Keep coordinates out of reprs that end up in logs or tracebacks."""
        return f"PhoneFix(timestamp={self.timestamp}, accuracy={self.accuracy})"


def new_locate_key() -> bytes:
    """Return a fresh key for one locate request."""
    return secrets.token_bytes(LOCATE_KEY_BYTES)


def _pb2() -> tuple[Any, Any]:
    return DeviceUpdate_pb2, Common_pb2


def _reject(stage: str, length: int) -> None:
    _LOGGER.debug("Phone location discarded at stage %s (%d bytes)", stage, length)


def _open_envelope(envelope_bytes: bytes) -> tuple[bytes, bytes, float] | None:
    """Return ciphertext, IV and fix time of a ``PhoneLocationEnvelope``."""
    pb2, common_pb2 = _pb2()
    try:
        envelope = pb2.PhoneLocationEnvelope()
        envelope.ParseFromString(envelope_bytes)
        cipher_msg = pb2.PhoneLocationCipher()
        cipher_msg.ParseFromString(envelope.encrypted)
        fix_time = common_pb2.Time()
        fix_time.ParseFromString(envelope.time)
    except DecodeError:
        return None
    ciphertext, iv = bytes(cipher_msg.ciphertext), bytes(cipher_msg.iv)
    if (
        len(iv) != _AES_BLOCK_BYTES
        or not ciphertext
        or len(ciphertext) % _AES_BLOCK_BYTES
        or fix_time.seconds <= 0
        or fix_time.nanos >= _NANOS_PER_SECOND
    ):
        return None
    return ciphertext, iv, float(fix_time.seconds) + float(fix_time.nanos) / 1e9


def _decrypt(ciphertext: bytes, iv: bytes, key: bytes) -> bytes | None:
    """Return the AES-128-CBC plaintext without PKCS7 padding, or ``None``."""
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        return None


def _position(plain: bytes) -> tuple[float, float, float | None] | None:
    """Return latitude, longitude and accuracy of a ``PhoneLocationPlaintext``."""
    pb2, _common = _pb2()
    plaintext = pb2.PhoneLocationPlaintext()
    try:
        plaintext.ParseFromString(plain)
    except DecodeError:
        return None
    if not plaintext.HasField("position"):
        return None
    lat = float(plaintext.position.latitude)
    lon = float(plaintext.position.longitude)
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return None
    if abs(lat) > _MAX_ABS_LATITUDE or abs(lon) > _MAX_ABS_LONGITUDE:
        return None
    acc = float(plaintext.accuracyMeters)
    return lat, lon, (acc if math.isfinite(acc) and acc > 0 else None)


def decrypt_phone_location(envelope_bytes: bytes, key: bytes) -> PhoneFix | None:
    """Return the fix in ``envelope_bytes`` decrypted with ``key``, or ``None``.

    ``None`` on a key or IV of the wrong length, a ciphertext that is no whole
    number of blocks, a wrong key (padding), a plaintext that is no valid
    ``PhoneLocationPlaintext``, a missing position or fix time, and coordinates
    that are not finite or outside +-90 / +-180 degrees. The accuracy is kept only
    when it is finite and positive.
    """
    raw = (
        bytes(envelope_bytes) if isinstance(envelope_bytes, (bytes, bytearray)) else b""
    )
    if not isinstance(key, bytes) or len(key) != LOCATE_KEY_BYTES or not raw:
        _reject("input", len(raw))
        return None
    opened = _open_envelope(raw)
    if opened is None:
        _reject("envelope", len(raw))
        return None
    ciphertext, iv, timestamp = opened
    plain = _decrypt(ciphertext, iv, key)
    if plain is None:
        _reject("padding", len(raw))
        return None
    position = _position(plain)
    if position is None:
        _reject("plaintext", len(raw))
        return None
    lat, lon, accuracy = position
    return PhoneFix(latitude=lat, longitude=lon, timestamp=timestamp, accuracy=accuracy)
