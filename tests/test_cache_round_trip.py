# tests/test_cache_round_trip.py
"""Behavioral tests for the stateful round-trip escape hatch (Q2-A, #177).

The escape hatch extends the clear-jump branch of
``_apply_weighted_location_fusion``:

* When a wide jump A->B is *accepted*, the pre-jump position A is remembered as
  a RAM-only, one-shot return anchor (only if ``is_reliable_fix(A)``).
* In the speed gate's reject branch, a fix that would otherwise be dropped is
  *accepted* instead when it lands back within ``ROUND_TRIP_ANCHOR_RADIUS_M`` of
  the anchor and the anchor is younger than ``ROUND_TRIP_TTL_S``. The anchor is
  consumed on recovery (one-shot), so a recovered fix never sets a new anchor
  (the A<->B ping-pong bollwerk, DF-1).

These tests mirror the harness of ``test_cache_speed_gate.py``: a
``MagicMock(spec=CacheOperations)`` carrying the cached ``existing`` fix, with
``_apply_weighted_location_fusion`` invoked unbound.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from custom_components.googlefindmy.const import (
    ROUND_TRIP_ANCHOR_RADIUS_M,
    ROUND_TRIP_TTL_S,
)
from custom_components.googlefindmy.coordinator.cache import (
    CacheOperations,
    _haversine_distance_impl,
)

BASE_TS = 1_700_000_000  # a realistic epoch (avoids pre-Y2K corruption guards)

# A (HOME) and B (FAR) are ~250 km apart (2.25 deg of latitude), so the
# clear-jump branch (dist > radius_sum) always executes and any A<->B jump at a
# few minutes' spacing is far above the 400 m/s cap.
HOME = (48.100000, 11.400000)  # anchor A
FAR = (50.350000, 11.400000)  # teleport target B

_METERS_PER_DEG_LAT = 111_320.0


def _point_north(base: tuple[float, float], meters: float) -> tuple[float, float]:
    """Return a point ``meters`` north of ``base`` (pure latitude offset)."""
    return (base[0] + meters / _METERS_PER_DEG_LAT, base[1])


def _coord(
    existing: dict[str, Any],
    *,
    gate_enabled: bool = True,
    rt_enabled: bool = True,
    anchors: dict[str, dict[str, Any]] | None = None,
) -> MagicMock:
    coord = MagicMock(spec=CacheOperations)
    coord._device_location_data = {"dev": existing}
    coord.increment_stat = MagicMock()
    coord._speed_gate_enabled = MagicMock(return_value=gate_enabled)
    coord._round_trip_confirm_enabled = MagicMock(return_value=rt_enabled)
    coord._round_trip_anchors = {} if anchors is None else anchors
    return coord


def _fix(
    coords: tuple[float, float],
    *,
    last_seen: Any = BASE_TS,
    acc: float | None = 20.0,
    is_own: Any = None,
    estimated: bool = False,
) -> dict[str, Any]:
    fix: dict[str, Any] = {
        "latitude": coords[0],
        "longitude": coords[1],
        "accuracy": acc,
        "location_type": "sensor",
    }
    if last_seen is not None:
        fix["last_seen"] = last_seen
    if is_own is not None:
        fix["is_own_report"] = is_own
    if estimated:
        fix["accuracy_estimated"] = True
    return fix


def _fuse(coord: MagicMock, new_data: dict[str, Any]) -> bool:
    return CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)


# ------------------------------------------------------------------ anchor set (1,2,10)


def test_far_jump_accept_sets_anchor() -> None:
    """An accepted wide jump A->B remembers A as a return anchor (R1).

    The anchor ``ts`` is the ACCEPTED B update's time (F-CODEX-5), not A's
    last_seen, so the TTL clock starts at the jump, not in the past.
    """
    coord = _coord(_fix(HOME, last_seen=BASE_TS))
    # dt = 3 h -> implied speed ~23 m/s -> plausible -> accepted (not gated).
    b_ts = BASE_TS + 3 * 3600
    result = _fuse(coord, _fix(FAR, last_seen=b_ts, acc=50.0))
    assert result is True
    assert coord._round_trip_anchors["dev"] == {
        "lat": HOME[0],
        "lon": HOME[1],
        "ts": float(b_ts),
    }


def test_stale_offline_jump_then_return_recovers() -> None:
    """F-CODEX-5 end-to-end: a device offline for 3 h jumps to B, then returns
    near A 300 s later. The anchor TTL must run from B (accept time), not from
    A's stale last_seen; otherwise the anchor is born ~3 h expired and this
    motivating stale-offline return is stranded.

    Mutation guard: on the pre-fix code (anchor ts = A.last_seen) both the ts
    assertion and the recovery ``result is True`` fail (delta_anchor would be
    3 h + 300 s = 11100 s, well past the 900 s TTL).
    """
    b_ts = BASE_TS + 3 * 3600  # B seen 3 h after A's (stale) last_seen
    coord = _coord(_fix(HOME, last_seen=BASE_TS, acc=50.0))
    # A->B accepted (low implied speed over 3 h) -> A remembered as anchor.
    assert _fuse(coord, _fix(FAR, last_seen=b_ts, acc=50.0)) is True
    # Anchor ts is the accept time (B), not A's stale last_seen.
    assert coord._round_trip_anchors["dev"]["ts"] == float(b_ts)
    # The device now sits at B; a correction back near A arrives 300 s after B
    # (well within the 900 s TTL) but is a kinematic teleport B->A -> recover.
    coord._device_location_data["dev"] = _fix(FAR, last_seen=b_ts)
    result = _fuse(coord, _fix(HOME, last_seen=b_ts + 300, acc=50.0))
    assert result is True
    assert "dev" not in coord._round_trip_anchors  # one-shot consumed


def test_anchor_not_set_for_estimated_existing() -> None:
    """An estimated/accuracy-less A must never become an anchor (R2)."""
    coord = _coord(_fix(HOME, last_seen=BASE_TS, acc=200.0, estimated=True))
    result = _fuse(coord, _fix(FAR, last_seen=BASE_TS + 3 * 3600, acc=50.0))
    assert result is True
    assert "dev" not in coord._round_trip_anchors


def test_anchor_not_set_when_incoming_has_no_last_seen() -> None:
    """Incoming B without last_seen -> anchor_ts is None -> no anchor (Fall 10).

    The anchor TTL clock derives from the accepted B fix (F-CODEX-5), so a B
    without a timestamp cannot seed a timestamped anchor even though A is a
    reliable fix. (A's own last_seen is irrelevant to the anchor ts now.)
    """
    coord = _coord(_fix(HOME, last_seen=BASE_TS))
    # Incoming timestamp missing: the speed gate's forward-check guard
    # (existing_ts and new_ts present) is not satisfied, so the gate never
    # evaluates the jump and the anchor-set inside the gated block is never
    # reached -> no anchor is remembered.
    result = _fuse(coord, _fix(FAR, last_seen=None, acc=50.0))
    assert result is True
    assert "dev" not in coord._round_trip_anchors


def test_own_report_jump_sets_no_anchor() -> None:
    """An own-report A->B jump must NOT seed a return anchor (F-CODEX-6).

    Own reports bypass the speed gate (``not incoming_is_own``), so the jump is
    never evaluated as a plausible crowd move. Seeding an anchor from it would
    let a later stale crowd report near A ride the recovery branch back onto A,
    rolling the tracker off a trusted current B. Anchor seeding therefore lives
    inside the gated block and an own-report jump seeds nothing.

    Mutation guard: on the pre-fix code (anchor-set outside the gate) this
    own-report jump seeded an anchor at HOME, so ``"dev" in anchors`` held.
    """
    coord = _coord(_fix(HOME, last_seen=BASE_TS, acc=50.0))
    result = _fuse(
        coord, _fix(FAR, last_seen=BASE_TS + 3 * 3600, acc=50.0, is_own=True)
    )
    assert result is True
    assert "dev" not in coord._round_trip_anchors


def test_accuracy_less_jump_sets_no_anchor() -> None:
    """An accuracy-less B jump must NOT seed a return anchor (F-CODEX-6).

    A crowd report carrying Android's no-accuracy sentinel (0.0) fails
    ``new_acc_measured`` and bypasses the speed gate (it is meant to fall
    through so downstream sanitization flags it estimated, #1181). It was never
    gate-evaluated, so - like the own-report case - it must not seed an anchor.

    Mutation guard: on the pre-fix code the accuracy-less jump seeded an anchor.
    """
    coord = _coord(_fix(HOME, last_seen=BASE_TS, acc=50.0))
    # acc=0.0 is the no-accuracy sentinel: finite but < MIN_PHYSICAL_ACCURACY_M.
    result = _fuse(coord, _fix(FAR, last_seen=BASE_TS + 3 * 3600, acc=0.0))
    assert result is True
    assert "dev" not in coord._round_trip_anchors


def test_own_report_bypass_then_stale_crowd_near_a_rejected() -> None:
    """End-to-end: an own-report jump must not open a stale-crowd rollback.

    Exploit the fix closes (F-CODEX-6): an own-report GPS jump A->B bypasses the
    gate; if it seeded anchor A, a stale crowd report near A within the TTL would
    be *recovered* (accepted) instead of rejected, rolling the tracker from the
    trusted current B back onto old A. With seeding confined to the gated path,
    no anchor exists and the stale crowd fix is rejected as a teleport.

    Mutation guard: on the pre-fix code step 1 seeds anchor A=HOME, so step 2's
    crowd fix near HOME is recovered and ``result is True`` (the wrong rollback).
    """
    # Step 1: own-report jump HOME->FAR is accepted and seeds NO anchor.
    coord = _coord(_fix(HOME, last_seen=BASE_TS, acc=50.0))
    assert (
        _fuse(coord, _fix(FAR, last_seen=BASE_TS + 3 * 3600, acc=50.0, is_own=True))
        is True
    )
    assert "dev" not in coord._round_trip_anchors
    # Step 2: the device is now at FAR; a stale crowd report lands near HOME
    # quickly (implied speed >> cap). Without an anchor it must be rejected.
    coord._device_location_data["dev"] = _fix(FAR, last_seen=BASE_TS + 3 * 3600)
    result = _fuse(coord, _fix(HOME, last_seen=BASE_TS + 3 * 3600 + 300, acc=50.0))
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")


# ------------------------------------------------------------------ recovery (3,4,7,8)


def test_return_within_radius_recovers() -> None:
    """A gated return near the anchor within TTL is recovered (R3)."""
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS), anchors=anchors)
    # existing is B (FAR); a fix back at A (HOME) 5 min later would be gated
    # (~833 m/s) but lands on the anchor -> recovered.
    result = _fuse(coord, _fix(HOME, last_seen=BASE_TS + 300, acc=50.0))
    assert result is True
    coord.increment_stat.assert_called_once_with("round_trip_recoveries")
    assert "dev" not in coord._round_trip_anchors  # one-shot consumed


def test_return_just_inside_200m_recovers() -> None:
    """A return just inside the 200 m radius recovers (R6 boundary)."""
    inside = _point_north(HOME, 150.0)
    assert (
        _haversine_distance_impl(HOME[0], HOME[1], *inside) < ROUND_TRIP_ANCHOR_RADIUS_M
    )
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS), anchors=anchors)
    result = _fuse(coord, _fix(inside, last_seen=BASE_TS + 300, acc=50.0))
    assert result is True
    coord.increment_stat.assert_called_once_with("round_trip_recoveries")


def test_return_just_outside_200m_rejected() -> None:
    """A return just outside the 200 m radius is still gated (R6 boundary)."""
    outside = _point_north(HOME, 260.0)
    assert (
        _haversine_distance_impl(HOME[0], HOME[1], *outside)
        > ROUND_TRIP_ANCHOR_RADIUS_M
    )
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS), anchors=anchors)
    result = _fuse(coord, _fix(outside, last_seen=BASE_TS + 300, acc=50.0))
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")


def test_recovery_does_not_reset_anchor() -> None:
    """A recovered return fix sets no new anchor (DF-1 ping-pong bollwerk)."""
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS), anchors=anchors)
    result = _fuse(coord, _fix(HOME, last_seen=BASE_TS + 300, acc=50.0))
    assert result is True
    # Anchor consumed and NOT replaced by a fresh one for the returning fix.
    assert coord._round_trip_anchors == {}


def test_recovery_ignores_incoming_reliability() -> None:
    """The returning fix is not re-checked against is_reliable_fix (DF-2)."""
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS), anchors=anchors)
    # Incoming carries accuracy_estimated=True (would fail is_reliable_fix) yet
    # still has a measured accuracy, so it clears new_acc_measured and recovers.
    result = _fuse(coord, _fix(HOME, last_seen=BASE_TS + 300, acc=50.0, estimated=True))
    assert result is True
    coord.increment_stat.assert_called_once_with("round_trip_recoveries")


# ------------------------------------------------------------------ TTL & toggle (5,6,11)


# The anchor timestamp and the gate timestamp are independent: the gate uses the
# cached B fix's last_seen vs the incoming fix, while the anchor TTL uses the
# anchor's own ts vs the incoming fix. An expiry test therefore keeps the jump
# gated (B seen only 300 s before the return) while letting the anchor age past
# the TTL.
_EXPIRED_RETURN_TS = BASE_TS + ROUND_TRIP_TTL_S + 120  # 120 s past the anchor TTL
_GATED_B_SEEN = _EXPIRED_RETURN_TS - 300  # keeps ~833 m/s > cap on the return


def test_anchor_expired_no_recovery() -> None:
    """A return after the TTL is not recovered (R5)."""
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=_GATED_B_SEEN), anchors=anchors)
    result = _fuse(coord, _fix(HOME, last_seen=_EXPIRED_RETURN_TS, acc=50.0))
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")


def test_expired_anchor_is_popped_on_reject() -> None:
    """An expired anchor is dropped (housekeeping) on the next reject (Fall 11)."""
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=_GATED_B_SEEN), anchors=anchors)
    _fuse(coord, _fix(HOME, last_seen=_EXPIRED_RETURN_TS, acc=50.0))
    assert "dev" not in coord._round_trip_anchors


def test_disabled_toggle_no_recovery_and_no_anchor() -> None:
    """With the toggle off there is neither recovery nor anchor set (R4)."""
    # (a) A returning fix near an anchor is NOT recovered when disabled.
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS), rt_enabled=False, anchors=anchors)
    result = _fuse(coord, _fix(HOME, last_seen=BASE_TS + 300, acc=50.0))
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")
    assert coord._round_trip_anchors == anchors  # untouched

    # (b) An accepted far jump sets NO anchor when disabled.
    coord2 = _coord(_fix(HOME, last_seen=BASE_TS), rt_enabled=False)
    result2 = _fuse(coord2, _fix(FAR, last_seen=BASE_TS + 3 * 3600, acc=50.0))
    assert result2 is True
    assert coord2._round_trip_anchors == {}


def test_no_anchor_when_speed_gate_disabled() -> None:
    """Anchor-set is gated on the speed gate: gate off -> no anchor accumulates.

    The recovery path only runs inside the gate's reject branch, so an anchor set
    while the gate is off could never be consumed. It must therefore not be set.
    """
    coord = _coord(_fix(HOME, last_seen=BASE_TS), gate_enabled=False, rt_enabled=True)
    result = _fuse(coord, _fix(FAR, last_seen=BASE_TS + 3 * 3600, acc=50.0))
    assert result is True
    assert coord._round_trip_anchors == {}


def test_out_of_order_return_not_recovered() -> None:
    """A return whose timestamp precedes the anchor (delta < 0) is not recovered.

    Guards against a rewound/out-of-order timestamp being read as an instant
    round trip; the anchor is dropped and the gate rejects as usual.
    """
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS + 600)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS), anchors=anchors)
    # new_ts (BASE_TS + 300) < anchor ts (BASE_TS + 600) -> delta_anchor < 0.
    result = _fuse(coord, _fix(HOME, last_seen=BASE_TS + 300, acc=50.0))
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")
    assert "dev" not in coord._round_trip_anchors  # housekeeping pop
