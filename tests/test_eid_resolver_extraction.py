# tests/test_eid_resolver_extraction.py
"""EID extraction edge cases, including truncated modern frames."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.googlefindmy.eid_resolver import (
    LEGACY_EID_LENGTH,
    MODERN_FRAME_TYPE,
    GoogleFindMyEIDResolver,
)


def test_extract_truncated_modern_frame() -> None:
    """Modern frame header with legacy-length payload should still yield a candidate."""

    hass = MagicMock()
    resolver = GoogleFindMyEIDResolver(hass)

    header = bytes([MODERN_FRAME_TYPE, 0x00])
    fake_eid_data = b"\xaa" * LEGACY_EID_LENGTH
    payload = header + fake_eid_data

    candidates, frame_type = resolver._extract_candidates(payload)

    assert frame_type == MODERN_FRAME_TYPE
    assert len(candidates) == 1
    assert candidates[0] == fake_eid_data
    assert len(candidates[0]) == LEGACY_EID_LENGTH


def test_extract_standard_legacy_frame() -> None:
    """Legacy frame extraction should remain unaffected."""

    hass = MagicMock()
    resolver = GoogleFindMyEIDResolver(hass)

    payload = bytes([0x40, 0x00]) + (b"\xbb" * LEGACY_EID_LENGTH)

    candidates, frame_type = resolver._extract_candidates(payload)

    assert frame_type == 0x40
    assert len(candidates) == 1
    assert candidates[0] == b"\xbb" * LEGACY_EID_LENGTH
    assert len(candidates[0]) == LEGACY_EID_LENGTH
