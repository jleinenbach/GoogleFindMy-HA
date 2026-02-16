# tests/test_eid_resolver_extraction.py
"""EID extraction edge cases, including truncated modern frames."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.googlefindmy.eid_resolver import (
    FMDN_FRAME_TYPE,
    LEGACY_EID_LENGTH,
    MODERN_FRAME_TYPE,
    GoogleFindMyEIDResolver,
)


def test_extract_truncated_modern_frame() -> None:
    """Modern frame header with legacy-length payload should still yield a candidate."""

    hass = MagicMock()
    resolver = GoogleFindMyEIDResolver(hass)

    fake_eid_data = b"\xaa" * LEGACY_EID_LENGTH
    # 0x41 + 20-byte EID = 21 bytes (no flags byte)
    payload = bytes([MODERN_FRAME_TYPE]) + fake_eid_data

    candidates, frame_type = resolver._extract_candidates(payload)

    assert frame_type == MODERN_FRAME_TYPE
    assert len(candidates) == 1
    assert candidates[0] == fake_eid_data
    assert len(candidates[0]) == LEGACY_EID_LENGTH


def test_extract_standard_legacy_frame_with_flags() -> None:
    """Legacy frame with EID and hashed-flags byte extracts the 20-byte EID."""

    hass = MagicMock()
    resolver = GoogleFindMyEIDResolver(hass)

    fake_eid_data = b"\xbb" * LEGACY_EID_LENGTH
    flags_byte = b"\xcc"
    # 0x40 + 20-byte EID + 1-byte flags = 22 bytes
    payload = bytes([FMDN_FRAME_TYPE]) + fake_eid_data + flags_byte

    candidates, frame_type = resolver._extract_candidates(payload)

    assert frame_type == FMDN_FRAME_TYPE
    assert len(candidates) == 1
    assert candidates[0] == fake_eid_data
    assert len(candidates[0]) == LEGACY_EID_LENGTH


def test_extract_legacy_frame_eid_starting_with_zero() -> None:
    """EID starting with 0x00 must not be confused with padding.

    Regression test: a previous heuristic (_legacy_payload_start) assumed
    that a 0x00 byte after the frame type was padding and shifted the
    extraction window by one.  Per the FMDN spec, byte 1 is always the
    first EID byte.
    """

    hass = MagicMock()
    resolver = GoogleFindMyEIDResolver(hass)

    # EID intentionally starts with 0x00
    fake_eid_data = b"\x00" + b"\xdd" * (LEGACY_EID_LENGTH - 1)
    flags_byte = b"\xee"
    # 0x40 + 20-byte EID (starting with 0x00) + 1-byte flags = 22 bytes
    payload = bytes([FMDN_FRAME_TYPE]) + fake_eid_data + flags_byte

    candidates, frame_type = resolver._extract_candidates(payload)

    assert frame_type == FMDN_FRAME_TYPE
    assert len(candidates) == 1
    assert candidates[0] == fake_eid_data
    assert len(candidates[0]) == LEGACY_EID_LENGTH


def test_extract_modern_frame_eid_starting_with_zero() -> None:
    """Modern frame: EID starting with 0x00 must not be confused with padding."""

    hass = MagicMock()
    resolver = GoogleFindMyEIDResolver(hass)

    fake_eid_data = b"\x00" + b"\xff" * (LEGACY_EID_LENGTH - 1)
    flags_byte = b"\xab"
    # 0x41 + 20-byte EID (starting with 0x00) + 1-byte flags = 22 bytes
    payload = bytes([MODERN_FRAME_TYPE]) + fake_eid_data + flags_byte

    candidates, frame_type = resolver._extract_candidates(payload)

    assert frame_type == MODERN_FRAME_TYPE
    assert len(candidates) == 1
    assert candidates[0] == fake_eid_data
    assert len(candidates[0]) == LEGACY_EID_LENGTH
