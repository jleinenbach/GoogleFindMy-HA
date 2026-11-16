# tests/test_phone_location_crypto.py
"""Decryption of the location a phone sends after phone action 4.

The ciphertext is built here with ``cryptography`` from constants; no
measured key, ciphertext or coordinate appears in this file.
"""

from __future__ import annotations

import logging
import math
import struct
from typing import Any

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.googlefindmy.NovaApi.ExecuteAction.LocatePhone.phone_location_crypto import (
    PhoneFix,
    decrypt_phone_location,
    new_locate_key,
)
from custom_components.googlefindmy.ProtoDecoders import Common_pb2, DeviceUpdate_pb2

_KEY = bytes(range(16))
_WRONG_KEY = bytes(range(100, 116))
_IV = bytes(range(16, 32))
_FIX_SECONDS = 1_800_000_000
_LAT, _LON, _ACC = 12.345678, -98.765432, 11.5


def _plaintext(lat: float = _LAT, lon: float = _LON, acc: float = _ACC) -> bytes:
    plain = DeviceUpdate_pb2.PhoneLocationPlaintext()
    plain.position.latitude = lat
    plain.position.longitude = lon
    plain.accuracyMeters = acc
    return plain.SerializeToString()


def _encrypt(plain: bytes, key: bytes = _KEY, iv: bytes = _IV) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _envelope(
    ciphertext: bytes, *, iv: bytes = _IV, seconds: int | None = _FIX_SECONDS
) -> bytes:
    cipher = DeviceUpdate_pb2.PhoneLocationCipher(ciphertext=ciphertext, iv=iv)
    envelope = DeviceUpdate_pb2.PhoneLocationEnvelope()
    envelope.encrypted = cipher.SerializeToString()
    envelope.phoneLocationEnvelopeField3 = 1
    if seconds is not None:
        envelope.time = Common_pb2.Time(
            seconds=seconds, nanos=500_000_000
        ).SerializeToString()
    return envelope.SerializeToString()


def test_synthetic_ciphertext_decrypts_to_the_fix() -> None:
    fix = decrypt_phone_location(_envelope(_encrypt(_plaintext())), _KEY)
    assert isinstance(fix, PhoneFix)
    assert fix.latitude == pytest.approx(_LAT)
    assert fix.longitude == pytest.approx(_LON)
    assert fix.accuracy == pytest.approx(_ACC)
    assert fix.timestamp == pytest.approx(_FIX_SECONDS + 0.5)


def test_plaintext_matches_the_observed_wire_shape() -> None:
    """Observed wire shape: 25 bytes = `0a 12` + 18 bytes position + `15` + 4 bytes float."""
    plain = _plaintext()
    assert len(plain) == 25
    assert plain[:2] == b"\x0a\x12" and plain[20] == 0x15
    assert struct.unpack("<f", plain[21:])[0] == pytest.approx(_ACC)


def test_wrong_key_gives_none() -> None:
    assert decrypt_phone_location(_envelope(_encrypt(_plaintext())), _WRONG_KEY) is None


def test_valid_padding_but_invalid_protobuf_gives_none() -> None:
    assert decrypt_phone_location(_envelope(_encrypt(b"\xff\xff\xff")), _KEY) is None


def test_plaintext_without_position_gives_none() -> None:
    plain = DeviceUpdate_pb2.PhoneLocationPlaintext(
        accuracyMeters=3.0
    ).SerializeToString()
    assert decrypt_phone_location(_envelope(_encrypt(plain)), _KEY) is None


@pytest.mark.parametrize(
    ("lat", "lon"),
    [(90.0001, 0.5), (-90.0001, 0.5), (0.5, 180.0001), (0.5, -180.0001)],
)
def test_out_of_range_gives_none(lat: float, lon: float) -> None:
    assert (
        decrypt_phone_location(_envelope(_encrypt(_plaintext(lat, lon))), _KEY) is None
    )


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("axis", ["lat", "lon"])
def test_non_finite_coordinate_gives_none(bad: float, axis: str) -> None:
    lat, lon = (bad, _LON) if axis == "lat" else (_LAT, bad)
    assert (
        decrypt_phone_location(_envelope(_encrypt(_plaintext(lat, lon))), _KEY) is None
    )


def test_edges_of_the_range_are_kept() -> None:
    for lat, lon in ((90.0, 180.0), (-90.0, -180.0)):
        assert decrypt_phone_location(_envelope(_encrypt(_plaintext(lat, lon))), _KEY)


@pytest.mark.parametrize("acc", [0.0, -1.0, math.nan, math.inf])
def test_unusable_accuracy_keeps_the_fix_without_accuracy(acc: float) -> None:
    fix = decrypt_phone_location(_envelope(_encrypt(_plaintext(acc=acc))), _KEY)
    assert fix is not None and fix.accuracy is None


@pytest.mark.parametrize(
    ("envelope", "key"),
    [
        (_envelope(_encrypt(_plaintext()), seconds=None), _KEY),
        (_envelope(_encrypt(_plaintext()), iv=b"short"), _KEY),
        (_envelope(_encrypt(_plaintext())[:-1]), _KEY),
        (_envelope(b""), _KEY),
        (b"\xff\xff\xff\xff", _KEY),
        (b"", _KEY),
        (_envelope(_encrypt(_plaintext())), b"k" * 15),
        (_envelope(_encrypt(_plaintext())), "k" * 16),
        ("not bytes", _KEY),
    ],
)
def test_every_malformed_input_gives_none(envelope: Any, key: Any) -> None:
    assert decrypt_phone_location(envelope, key) is None


def test_nothing_secret_reaches_logs_or_repr(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fix = decrypt_phone_location(_envelope(_encrypt(_plaintext())), _KEY)
    decrypt_phone_location(_envelope(_encrypt(_plaintext())), _WRONG_KEY)
    assert "12.34" not in repr(fix) and "98.76" not in repr(fix)
    for record in caplog.records:
        text = record.getMessage()
        for secret in (_KEY.hex(), _WRONG_KEY.hex(), _IV.hex(), "12.34", "98.76"):
            assert secret not in text


def test_new_locate_key_is_random_and_sixteen_bytes() -> None:
    keys = {new_locate_key() for _ in range(8)}
    assert len(keys) == 8
    assert all(len(k) == 16 for k in keys)


def test_nanos_beyond_one_second_gives_none() -> None:
    cipher = DeviceUpdate_pb2.PhoneLocationCipher(
        ciphertext=_encrypt(_plaintext()), iv=_IV
    )
    envelope = DeviceUpdate_pb2.PhoneLocationEnvelope(
        encrypted=cipher.SerializeToString(),
        time=Common_pb2.Time(
            seconds=_FIX_SECONDS, nanos=1_000_000_000
        ).SerializeToString(),
    )
    assert decrypt_phone_location(envelope.SerializeToString(), _KEY) is None


def test_bytearray_works_and_memoryview_is_refused() -> None:
    envelope = _envelope(_encrypt(_plaintext()))
    assert decrypt_phone_location(bytearray(envelope), _KEY) is not None  # type: ignore[arg-type]
    assert decrypt_phone_location(memoryview(envelope), _KEY) is None  # type: ignore[arg-type]


def test_random_inputs_never_raise() -> None:
    """The never-raises contract over random envelopes and random ciphertexts."""
    import random

    rng = random.Random(224)  # deterministic test data, not key material
    for _ in range(400):
        blob = rng.randbytes(rng.randrange(0, 120))
        decrypt_phone_location(blob, _KEY)
        noise = rng.randbytes(16 * rng.randrange(1, 4))
        decrypt_phone_location(_envelope(_encrypt(noise)), _KEY)
        decrypt_phone_location(_envelope(noise), _KEY)
