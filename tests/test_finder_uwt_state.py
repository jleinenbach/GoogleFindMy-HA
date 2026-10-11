# tests/test_finder_uwt_state.py
"""The protection mode a confirmed sighting carries, and where it comes from.

The finder reports this mode to Google. A wrong ``0`` tells the network that
protection is off, so the source rule is pinned case by case.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.googlefindmy.eid_resolver import (
    FMDN_FRAME_TYPE,
    MODERN_FRAME_TYPE,
    ConfirmedSighting,
    EidVariant,
    FlagsOrigin,
    GoogleFindMyEIDResolver,
    UwtSource,
    _effective_uwt_mode,
)
from tests.test_ble_battery_sensor import _make_resolver, _match


@pytest.mark.parametrize(
    ("flags_bit", "origin", "frame_type", "expected"),
    [
        (True, "framed", FMDN_FRAME_TYPE, (None, UwtSource.CONFLICT)),
        (True, "framed", MODERN_FRAME_TYPE, (True, UwtSource.FLAGS_BIT)),
        (True, "framed", None, (True, UwtSource.FLAGS_BIT)),
        (False, "framed", FMDN_FRAME_TYPE, (False, UwtSource.FLAGS_BIT)),
        (False, "framed", MODERN_FRAME_TYPE, (None, UwtSource.CONFLICT)),
        (False, "framed", None, (False, UwtSource.FLAGS_BIT)),
        (None, "absent", FMDN_FRAME_TYPE, (False, UwtSource.FRAME_TYPE)),
        (None, "absent", MODERN_FRAME_TYPE, (True, UwtSource.FRAME_TYPE)),
        (None, "absent", None, (None, UwtSource.UNKNOWN)),
        (True, "guessed", None, (None, UwtSource.UNKNOWN)),
        (True, "guessed", FMDN_FRAME_TYPE, (False, UwtSource.FRAME_TYPE)),
    ],
)
def test_effective_uwt_mode(
    flags_bit: bool | None,
    origin: FlagsOrigin,
    frame_type: int | None,
    expected: tuple[bool | None, UwtSource],
) -> None:
    """A framed bit decides unless the frame type contradicts it (conflict);
    a guessed bit counts like none; then the frame type."""
    assert _effective_uwt_mode(flags_bit, origin, frame_type) == expected


# ---------------------------------------------------------------------------
# Through resolve_eid: the sighting carries the mode the advertisement showed
# ---------------------------------------------------------------------------

_T = 1_800_000_000
_PERIOD = 1024
_HEADER = b"\x00" * 7


def _resolver(*windows: tuple[int, int]) -> GoogleFindMyEIDResolver:
    """Resolver knowing ``dev-u`` under one 20-byte EID per (marker, window)."""
    resolver = _make_resolver()
    for marker, window in windows:
        eid = bytes([marker]) * 20
        resolver._lookup[eid] = [_match("dev-u")]
        resolver._lookup_metadata[eid] = {
            "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
            "rotation_timestamp": window * _PERIOD,
            "timestamp_basis": "pair_date",
            "flags_xor_mask": 0x00,
        }
    return resolver


def _feed(resolver: GoogleFindMyEIDResolver, payload: bytes, at: int) -> None:
    observed = float(at - _T + 50_000)
    with (
        patch("time.time", return_value=float(at)),
        patch("time.monotonic", return_value=observed),
    ):
        assert resolver.resolve_eid(payload, observed_at=observed)


def _framed(marker: int, flags: int | None, frame: int = FMDN_FRAME_TYPE) -> bytes:
    tail = b"" if flags is None else bytes([flags])
    return _HEADER + bytes([frame]) + bytes([marker]) * 20 + tail


def _mode(resolver: GoogleFindMyEIDResolver) -> tuple[bool | None, UwtSource]:
    sighting = resolver.last_confirmed_sighting("dev-u")
    assert sighting is not None
    return sighting.uwt_mode, sighting.uwt_source


def test_confirmed_sighting_defaults_to_an_unknown_mode() -> None:
    """A sighting built without a mode is unknown, never "off": unknown uploads nothing."""
    sighting = ConfirmedSighting(
        variant=EidVariant.LEGACY_SECP160R1_X20_BE, window_counter=0, observed_at=0
    )
    assert (sighting.uwt_mode, sighting.uwt_source) == (None, UwtSource.UNKNOWN)


def test_framed_flags_bit_is_the_source() -> None:
    """A framed advertisement with a decodable flags byte: the bit decides."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, 0x01, MODERN_FRAME_TYPE), _T)
    assert _mode(resolver) == (True, UwtSource.FLAGS_BIT)


def test_frame_type_alone_is_the_source() -> None:
    """Frame 0x41 without a flags byte: the frame type shows the mode."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, None, MODERN_FRAME_TYPE), _T)
    assert _mode(resolver) == (True, UwtSource.FRAME_TYPE)


def test_bare_eid_alone_is_unknown() -> None:
    """A bare EID shows neither frame nor flags."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, bytes([0x71]) * 20, _T)
    assert _mode(resolver) == (None, UwtSource.UNKNOWN)


def test_bare_sighting_in_the_same_window_is_unknown() -> None:
    """A later bare EID in the same window does not take over the earlier mode.

    The mode can change within the window; taking it over would report it
    with the later sighting's time, here 900 s after it was seen.
    """
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, 0x01, MODERN_FRAME_TYPE), _T)
    _feed(resolver, bytes([0x71]) * 20, _T + 900)
    sighting = resolver.last_confirmed_sighting("dev-u")
    assert sighting is not None
    assert sighting.observed_at == _T + 900
    assert (sighting.uwt_mode, sighting.uwt_source) == (None, UwtSource.UNKNOWN)


def test_bare_sighting_in_a_later_window_is_unknown() -> None:
    """An unknown mode in the next window stays unknown."""
    resolver = _resolver((0x71, 7), (0x72, 8))
    _feed(resolver, _framed(0x71, 0x01, MODERN_FRAME_TYPE), _T)
    _feed(resolver, bytes([0x72]) * 20, _T + _PERIOD)
    assert resolver.last_confirmed_sighting("dev-u").window_counter == 8 * _PERIOD  # type: ignore[union-attr]
    assert _mode(resolver) == (None, UwtSource.UNKNOWN)


def test_restarted_counter_does_not_inherit_the_mode() -> None:
    """After a counter restart (smaller window, wall gap over one period)
    an unknown mode stays unknown."""
    resolver = _resolver((0x71, 9), (0x72, 2))
    _feed(resolver, _framed(0x71, 0x01, MODERN_FRAME_TYPE), _T)
    _feed(resolver, bytes([0x72]) * 20, _T + 2 * _PERIOD)
    assert resolver.last_confirmed_sighting("dev-u").window_counter == 2 * _PERIOD  # type: ignore[union-attr]
    assert _mode(resolver) == (None, UwtSource.UNKNOWN)


def test_known_mode_in_the_same_window_replaces() -> None:
    """A known new mode always replaces, also within the window."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, 0x01, MODERN_FRAME_TYPE), _T)
    _feed(resolver, _framed(0x71, 0x00), _T + 1)
    assert _mode(resolver) == (False, UwtSource.FLAGS_BIT)


def test_throwing_decoder_still_records_the_sighting(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The match path never fails on the flags decoder."""
    resolver = _resolver((0x71, 7))

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("decoder broke")

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_update_ble_battery", _boom)
    with caplog.at_level(logging.DEBUG, logger="custom_components.googlefindmy"):
        _feed(resolver, _framed(0x71, 0x01, MODERN_FRAME_TYPE), _T)
        _feed(resolver, _framed(0x71, 0x01, MODERN_FRAME_TYPE), _T + 1)
    assert _mode(resolver) == (True, UwtSource.FRAME_TYPE)
    failures = [
        r for r in caplog.records if "ble_battery_decode_failed" in r.getMessage()
    ]
    # The first failure of a type stays visible (it used to surface as an
    # ERROR from the Bluetooth callback); repeats of the same type drop to DEBUG.
    assert [r.levelno for r in failures] == [logging.WARNING, logging.DEBUG]
    assert all("error_type=RuntimeError" in r.getMessage() for r in failures)
    assert "decoder broke" not in caplog.text


def test_guessed_flags_byte_does_not_decide() -> None:
    """A sliding-window hit reads its flags byte by guessing: mode unknown.

    Frame 0x41 with a 20-byte EID forces the window search. The EID the
    lookup knows sits one byte off the framed position, so only a window
    candidate matches, and ``_guess_flags_byte`` supplies a decodable byte
    with bit 7 set. That byte may be EID material and must not decide.
    """
    eid = bytes(range(0x30, 0x44))
    payload = _HEADER + bytes([MODERN_FRAME_TYPE, 0x99]) + eid[:19] + bytes([0x01])
    assert payload[9:29] == eid[:19] + b"\x01"
    resolver = _make_resolver()
    window_eid = payload[9:29]
    resolver._lookup[window_eid] = [_match("dev-u")]
    resolver._lookup_metadata[window_eid] = {
        "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
        "rotation_timestamp": 7 * _PERIOD,
        "timestamp_basis": "pair_date",
        "flags_xor_mask": 0x00,
    }
    _feed(resolver, payload, _T)
    assert resolver.get_ble_battery_state("dev-u") is not None
    assert resolver.get_ble_battery_state("dev-u").uwt_mode is True  # type: ignore[union-attr]
    assert _mode(resolver) == (None, UwtSource.UNKNOWN)


def test_frame_and_bit_disagreeing_is_a_conflict() -> None:
    """Frame 0x40 with the bit set contradicts itself: no mode, source CONFLICT."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, 0x01), _T)
    assert _mode(resolver) == (None, UwtSource.CONFLICT)


def test_conflict_does_not_inherit_the_known_mode() -> None:
    """A contradicting advertisement replaces a known mode in the same window:
    a CONFLICT is evidence of its own."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, 0x00), _T)
    _feed(resolver, _framed(0x71, 0x00, MODERN_FRAME_TYPE), _T + 1)
    assert _mode(resolver) == (None, UwtSource.CONFLICT)


def test_bare_sighting_after_a_conflict_is_unknown() -> None:
    """A later bare EID after a conflict shows no mode of its own: unknown."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, 0x01), _T)
    _feed(resolver, bytes([0x71]) * 20, _T + 1)
    assert _mode(resolver) == (None, UwtSource.UNKNOWN)


def test_later_frame_only_reading_decides_for_itself() -> None:
    """A later frame-type-only reading in the same window is a sighting of its
    own: it replaces an earlier conflict with the mode its frame type shows."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, 0x01), _T)
    _feed(resolver, _framed(0x71, None), _T + 1)
    assert _mode(resolver) == (False, UwtSource.FRAME_TYPE)


def test_consistent_bit_reading_replaces_a_conflict() -> None:
    """A framed reading whose bit agrees with its frame type replaces a conflict."""
    resolver = _resolver((0x71, 7))
    _feed(resolver, _framed(0x71, 0x01), _T)
    _feed(resolver, _framed(0x71, 0x00), _T + 1)
    assert _mode(resolver) == (False, UwtSource.FLAGS_BIT)


def test_conflict_does_not_carry_into_a_later_window() -> None:
    """A conflict is not carried into the next window either."""
    resolver = _resolver((0x71, 7), (0x72, 8))
    _feed(resolver, _framed(0x71, 0x01), _T)
    _feed(resolver, _framed(0x72, None), _T + _PERIOD)
    assert _mode(resolver) == (False, UwtSource.FRAME_TYPE)
