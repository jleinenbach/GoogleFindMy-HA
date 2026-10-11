# tests/test_finder_uwt_state.py
"""The protection mode a confirmed sighting carries, and where it comes from.

The finder reports this mode to Google. A wrong ``0`` tells the network that
protection is off, so the source rule is pinned case by case.
"""

from __future__ import annotations

import pytest

from custom_components.googlefindmy.eid_resolver import (
    FMDN_FRAME_TYPE,
    MODERN_FRAME_TYPE,
    FlagsOrigin,
    UwtSource,
    _effective_uwt_mode,
)


@pytest.mark.parametrize(
    ("flags_bit", "origin", "frame_type", "expected"),
    [
        (True, "framed", FMDN_FRAME_TYPE, (True, UwtSource.FLAGS_BIT)),
        (True, "framed", MODERN_FRAME_TYPE, (True, UwtSource.FLAGS_BIT)),
        (True, "framed", None, (True, UwtSource.FLAGS_BIT)),
        (False, "framed", FMDN_FRAME_TYPE, (False, UwtSource.FLAGS_BIT)),
        (False, "framed", MODERN_FRAME_TYPE, (False, UwtSource.FLAGS_BIT)),
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
    """A framed bit wins, a guessed bit counts like none, then the frame type."""
    assert _effective_uwt_mode(flags_bit, origin, frame_type) == expected
