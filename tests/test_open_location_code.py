# tests/test_open_location_code.py
"""Unit tests for the vendored Open Location Code (Plus Code) encoder.

The encoder is vendored verbatim (encode-only path) from
``github.com/google/open-location-code`` at commit
``dcff1534f70a0d7244d0d1c357c20f0aa28ab355``. These vectors are taken verbatim
from that commit's ``test_data/encoding.csv`` (the ``latitude,longitude`` and
final ``code`` columns) and were confirmed to round-trip through ``encode()``
for ``code_length=10``. The float-boundary rows of that CSV (which the upstream
harness checks via the integer path ``encodeIntegers``) are intentionally not
used here, because ``encode()`` on exact cell edges is a known float artifact
shared with upstream and would not add signal.

A 10-digit code is 11 characters long including the ``+`` separator after the
8th digit (e.g. ``8FVC9G8F+6X``) and uses only the OLC alphabet.
"""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.vendor.openlocationcode import encode

# OLC digit alphabet plus the separator; a valid code contains nothing else.
_ALPHABET = set("23456789CFGHJMPQRVWX")

# (latitude, longitude, expected 10-digit code) — verbatim from encoding.csv.
_VECTORS: list[tuple[float, float, str]] = [
    # Landmark from the upstream module docstring (Zurich).
    (47.365590, 8.524997, "8FVC9G8F+6X"),
    (20.3700625, 2.7821875, "7FG49QCJ+2V"),
    (47.0000625, 8.0000625, "8FVC2222+22"),
    (-41.2730625, 174.7859375, "4VCPPQGP+Q9"),
    # South-west extreme (latitude/longitude near -90 / -180).
    (-89.9999375, -179.9999375, "22222222+22"),
    # Latitude clipped at the north pole.
    (90.0, 1.0, "CFX3X2X2+X2"),
    # Latitude beyond +90 is clipped into range.
    (91.3759, -96.45974, "C6X5XGXR+X4"),
]

# Longitude outside [-180, 180) must normalise: 728.0000625 == 8.0000625 (+2*360).
_WRAP_VECTORS: list[tuple[float, float, str]] = [
    (47.0000625, 728.0000625, "8FVC2222+22"),
    (47.0000625, -711.9999375, "8FVC2222+22"),
]


@pytest.mark.parametrize(("lat", "lon", "expected"), _VECTORS)
def test_encode_matches_official_vectors(lat: float, lon: float, expected: str) -> None:
    """Encoding a coordinate at length 10 reproduces the official code."""
    assert encode(lat, lon, 10) == expected


@pytest.mark.parametrize(("lat", "lon", "expected"), _WRAP_VECTORS)
def test_encode_normalizes_longitude(lat: float, lon: float, expected: str) -> None:
    """Out-of-range longitude is normalised into [-180, 180) before encoding."""
    assert encode(lat, lon, 10) == expected


def test_encode_default_length_is_ten() -> None:
    """The default code length is 10 (11 chars incl. separator)."""
    assert encode(47.365590, 8.524997) == "8FVC9G8F+6X"


def test_encode_format_invariants() -> None:
    """A length-10 code is 11 chars, separator at index 8, OLC alphabet only."""
    code = encode(52.5219, 13.4132, 10)
    assert len(code) == 11
    assert code[8] == "+"
    assert set(code) - {"+"} <= _ALPHABET


def test_encode_rejects_invalid_length() -> None:
    """Odd lengths below the pair-code length are rejected (upstream contract)."""
    with pytest.raises(ValueError):
        encode(0.0, 0.0, 1)
    with pytest.raises(ValueError):
        encode(0.0, 0.0, 9)
