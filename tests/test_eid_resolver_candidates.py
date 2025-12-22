# tests/test_eid_resolver_candidates.py
"""Candidate extraction robustness for modern framed payloads."""

from __future__ import annotations

from custom_components.googlefindmy.eid_resolver import (
    LEGACY_EID_LENGTH,
    MODERN_EID_LENGTH,
    MODERN_FRAME_TYPE,
    GoogleFindMyEIDResolver,
)


def test_extract_candidates_truncated_modern_frame_allows_fallback() -> None:
    """BUG E: truncated modern frame should not return empty candidates."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver._ensure_cache_defaults()
    raw = bytes([MODERN_FRAME_TYPE]) + b"A" * (MODERN_EID_LENGTH - 1)

    candidates, observed_frame = resolver._extract_candidates(raw)

    assert observed_frame == MODERN_FRAME_TYPE, (
        "BUG E: observed_frame not preserved for modern frame; ensure _extract_candidates "
        "sets observed_frame=MODERN_FRAME_TYPE when raw[0] indicates it"
    )
    assert candidates, (
        "BUG E: truncated modern frame caused empty candidates; remove early-return and add "
        "fallback extraction/logging in _extract_candidates"
    )
    assert any(len(cand) == LEGACY_EID_LENGTH for cand in candidates)


def test_extract_candidates_full_modern_frame_keeps_fast_path() -> None:
    """REGRESSION: ideal modern frames must still yield the direct candidate."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver._ensure_cache_defaults()
    payload = b"B" * MODERN_EID_LENGTH
    raw = bytes([MODERN_FRAME_TYPE]) + payload

    candidates, observed_frame = resolver._extract_candidates(raw)

    assert observed_frame == MODERN_FRAME_TYPE
    assert len(candidates) == 1, (
        "REGRESSION: ideal modern framed payload no longer yields correct MODERN_EID_LENGTH "
        "candidate; restore fast-path extraction for full-length payloads"
    )
    assert candidates[0] == payload
