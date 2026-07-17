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

import pytest

from custom_components.googlefindmy.const import (
    ROUND_TRIP_ANCHOR_RADIUS_M,
    ROUND_TRIP_TTL_S,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
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


def test_out_of_order_echo_preserves_anchor() -> None:
    """An out-of-order echo (delta_anchor < 0) must NOT drop the anchor (F-FABLE-1).

    A delayed/out-of-order report near A can carry a last_seen BETWEEN A and B,
    so its new_ts precedes the anchor ts (delta_anchor < 0). That is not an
    expiry: popping the anchor here would destroy it and strand a later genuine
    return. The echo is still gated (rejected as a teleport), but the anchor
    survives for a later legitimate forward return.

    Mutation guard: on the pre-fix code the housekeeping pop was unconditional,
    so the anchor was dropped here and this ``"dev" in anchors`` assertion failed.
    """
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS + 600)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS), anchors=anchors)
    # new_ts (BASE_TS + 300) < anchor ts (BASE_TS + 600) -> delta_anchor < 0.
    result = _fuse(coord, _fix(HOME, last_seen=BASE_TS + 300, acc=50.0))
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")
    # The anchor is preserved (forward-only housekeeping); still keyed on "dev".
    assert coord._round_trip_anchors["dev"] == {
        "lat": HOME[0],
        "lon": HOME[1],
        "ts": float(BASE_TS + 600),
    }


def test_out_of_order_echo_then_forward_return_recovers() -> None:
    """A genuine forward return recovers after an out-of-order echo preserved the
    anchor (F-FABLE-1 end-to-end).

    The speed gate only evaluates a jump when the incoming fix is forward of the
    cached endpoint (``new_ts > existing_ts``). To exercise a gated out-of-order
    echo whose new_ts still precedes the anchor ts (delta_anchor < 0), the anchor
    ts must sit AFTER the cached endpoint's last_seen - the realistic case where
    the cached row is an older B fix while the anchor was seeded at the later
    accept time. Both echo and forward return are gated (teleport B->A); the echo
    (delta_anchor < 0) must LEAVE the anchor alive, and the later forward return
    (delta_anchor >= 0, within TTL + radius) then recovers and consumes it.

    Mutation guard: on the pre-fix code the out-of-order echo popped the anchor
    unconditionally, so the later forward return finds no anchor, is gated, and
    ``result is False`` (the stranded-return bug this guard closes).
    """
    anchor_ts = BASE_TS + 60
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(anchor_ts)}}
    # Cached endpoint B carries an OLDER last_seen than the anchor ts, so a fix
    # between the two is forward of B (gate fires) yet before the anchor.
    coord = _coord(_fix(FAR, last_seen=BASE_TS), anchors=anchors)

    # Step 1: an out-of-order echo near A at BASE_TS + 30: forward of B
    # (delta_t = 30 s -> ~8333 m/s, gated) but before the anchor ts
    # (delta_anchor = 30 - 60 = -30 < 0). It must be rejected AND preserve the
    # anchor.
    assert _fuse(coord, _fix(HOME, last_seen=BASE_TS + 30, acc=50.0)) is False
    assert coord._round_trip_anchors["dev"]["ts"] == float(anchor_ts)  # alive

    # Step 2: a legitimate forward return near A at BASE_TS + 300: still forward
    # of B (delta_t = 300 s -> ~833 m/s, gated) and now after the anchor ts
    # (delta_anchor = 240 s, within the 900 s TTL, on the anchor) -> recovered
    # and consumed one-shot.
    result = _fuse(coord, _fix(HOME, last_seen=BASE_TS + 300, acc=50.0))
    assert result is True
    coord.increment_stat.assert_called_with("round_trip_recoveries")
    assert "dev" not in coord._round_trip_anchors  # one-shot consumed


# ------------------------------------------- supersede detection (F-FABLE-2, AP-2)


def test_own_report_clear_jump_marks_supersede_with_anchor() -> None:
    """An own-report clear jump with a surviving anchor marks the slot (F-FABLE-2).

    A trusted own-report that is itself a clear jump (dist > radius_sum) away
    from the previous position is a genuine relocation B->C. A pre-existing
    anchor A (from an earlier gated crowd jump) is now stale, so fusion marks
    ``_supersede_round_trip_anchor`` on ``new_data`` for update_device_cache to
    consume post-commit. The own-report itself still bypasses the speed gate and
    is accepted (result True), never seeding or consuming the anchor here.
    """
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS, acc=50.0), anchors=anchors)
    new_data = _fix(HOME, last_seen=BASE_TS + 300, acc=50.0, is_own=True)
    # HOME vs cached FAR is a clear jump; own-report -> gate bypassed.
    result = _fuse(coord, new_data)
    assert result is True
    assert new_data.get("_supersede_round_trip_anchor") is True
    # Fusion neither consumes nor seeds the anchor: the action is deferred.
    assert coord._round_trip_anchors == anchors


def test_own_report_clear_jump_without_anchor_sets_no_marker() -> None:
    """Without a surviving anchor an own-report clear jump sets no marker."""
    coord = _coord(_fix(FAR, last_seen=BASE_TS, acc=50.0))  # no anchors
    new_data = _fix(HOME, last_seen=BASE_TS + 300, acc=50.0, is_own=True)
    result = _fuse(coord, new_data)
    assert result is True
    assert "_supersede_round_trip_anchor" not in new_data


def test_crowd_clear_jump_sets_no_supersede_marker() -> None:
    """A crowd (non-own) clear jump never marks supersede, even with an anchor.

    The marker is keyed on ``incoming_is_own``: a crowd report is not a trusted
    relocation, so it must not invalidate the anchor. (This particular crowd fix
    lands on the anchor and is recovered, which is the anchor's whole purpose.)
    """
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(_fix(FAR, last_seen=BASE_TS, acc=50.0), anchors=anchors)
    new_data = _fix(HOME, last_seen=BASE_TS + 300, acc=50.0)  # is_own absent
    _fuse(coord, new_data)
    assert "_supersede_round_trip_anchor" not in new_data


def test_own_report_clear_jump_disabled_toggle_sets_no_marker() -> None:
    """With the round-trip toggle off, no supersede marker is set."""
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(BASE_TS)}}
    coord = _coord(
        _fix(FAR, last_seen=BASE_TS, acc=50.0), rt_enabled=False, anchors=anchors
    )
    new_data = _fix(HOME, last_seen=BASE_TS + 300, acc=50.0, is_own=True)
    result = _fuse(coord, new_data)
    assert result is True
    assert "_supersede_round_trip_anchor" not in new_data


# ------------------------------- supersede action via update_device_cache (AP-2)


def _make_update_coordinator(
    existing: dict[str, Any],
    *,
    anchors: dict[str, dict[str, Any]] | None = None,
    gate_enabled: bool = True,
    rt_enabled: bool = True,
) -> Any:
    """A coordinator wired for the real ``update_device_cache`` path.

    Mirrors ``tests/test_plus_code_last_known.py::_make_update_coordinator`` but
    also wires the round-trip anchor store and the two feature toggles the
    supersede handling reads, so the F-FABLE-2 post-commit action runs through
    the production commit sequence.
    """
    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_location_data = {"dev": dict(existing)}
    coord._device_names = {}
    coord._device_update_history = {}
    coord._round_trip_anchors = {} if anchors is None else anchors
    coord.increment_stat = lambda *_a, **_k: None
    coord._apply_report_type_cooldown = lambda *_a, **_k: None
    coord._is_on_hass_loop = lambda: True
    coord._run_on_hass_loop = lambda *_a, **_k: None
    coord._speed_gate_enabled = lambda: gate_enabled
    coord._round_trip_confirm_enabled = lambda: rt_enabled
    # Seed the reliable fix as the current last-good.
    coord._record_last_good_location("dev", existing)
    return coord


def test_supersede_action_invalidates_anchor_after_commit() -> None:
    """F-FABLE-2 action end-to-end: an own-report clear jump B->C invalidates a
    stale anchor A after committing, so a later stale crowd echo near A cannot
    roll the tracker back.

    Sequence over the real ``update_device_cache`` commit path:
    1. A crowd jump A->B is accepted and seeds anchor A (ts = accept time of B).
    2. An own-report clear jump B->C commits and the post-commit supersede action
       pops anchor A (device no longer in ``_round_trip_anchors``).
    3. A later stale crowd echo near A is gated (no anchor to recover on) and is
       dropped by fusion, so the cached position stays at C.

    Mutation guard: neutralizing the post-commit pop leaves anchor A alive, so
    step 3's crowd echo recovers onto A and the ``device not in anchors``
    assertion (and the position-stays-at-C assertion) fail.
    """
    # C is a third location, a clear jump north of both HOME (A) and FAR (B).
    C = (46.000000, 11.400000)
    t0 = BASE_TS
    # Step 1: crowd jump A(HOME) -> B(FAR) over 3 h (plausible) seeds anchor A.
    coord = _make_update_coordinator(_fix(HOME, last_seen=t0, acc=20.0))
    b_ts = t0 + 3 * 3600
    coord.update_device_cache("dev", _fix(FAR, last_seen=b_ts, acc=20.0))
    assert coord._round_trip_anchors["dev"]["lat"] == HOME[0]
    assert coord._device_location_data["dev"]["latitude"] == FAR[0]

    # Step 2: own-report clear jump B(FAR) -> C over 3 h (own bypasses the gate,
    # commits) -> post-commit supersede pops anchor A.
    c_ts = b_ts + 3 * 3600
    coord.update_device_cache("dev", _fix(C, last_seen=c_ts, acc=20.0, is_own=True))
    assert coord._device_location_data["dev"]["latitude"] == pytest.approx(C[0])
    assert "dev" not in coord._round_trip_anchors  # anchor invalidated

    # Step 3: a stale crowd echo near A arrives quickly after C (teleport speed).
    # With the anchor gone it cannot recover; fusion rejects it and the cached
    # position stays at C.
    coord.update_device_cache("dev", _fix(HOME, last_seen=c_ts + 300, acc=20.0))
    assert coord._device_location_data["dev"]["latitude"] == pytest.approx(C[0])


def test_supersede_not_triggered_when_significance_rejects() -> None:
    """F-CODEX-7 non-recurrence: an own-report clear jump that is REJECTED by the
    significance gate (timestamp regression) must NOT invalidate the anchor.

    The supersede marker is popped early (pre-commit) but the action runs only
    AFTER the significance gate and the commit. A rejected payload (early return
    @512) therefore never reaches the pop, so a discarded own-report leaves the
    anchor intact.

    Mutation guard: moving the pop BEFORE the significance gate (pre-commit)
    would invalidate the anchor even though the payload is discarded, so this
    ``anchor survives`` assertion fails.
    """
    t0 = BASE_TS
    # Seed anchor A and place the device at B via a plausible crowd jump.
    coord = _make_update_coordinator(_fix(HOME, last_seen=t0, acc=20.0))
    b_ts = t0 + 3 * 3600
    coord.update_device_cache("dev", _fix(FAR, last_seen=b_ts, acc=20.0))
    assert coord._round_trip_anchors["dev"]["lat"] == HOME[0]

    # An own-report clear jump to C but with a REGRESSED timestamp (before B):
    # fusion marks supersede (own clear jump + surviving anchor), yet the
    # significance gate rejects the payload (new_ts < cached B ts) -> early
    # return, so the post-commit supersede action never runs.
    C = (46.000000, 11.400000)
    regressed_ts = b_ts - 1000  # < cached B last_seen -> significance rejects
    coord.update_device_cache(
        "dev", _fix(C, last_seen=regressed_ts, acc=20.0, is_own=True)
    )
    # The payload was dropped: cached position stays at B, anchor A survives.
    assert coord._device_location_data["dev"]["latitude"] == pytest.approx(FAR[0])
    assert coord._round_trip_anchors["dev"]["lat"] == HOME[0]


# ---------------------- anchor survival across estimated-existing skip (F2, AP-3)


def test_anchor_surviving_estimated_skip_expires_by_ttl() -> None:
    """An anchor that survives an estimated-``existing`` gate skip stays bounded
    by the existing TTL/radius; it does not become permanently consumable (F2).

    F-CODEX-1 makes the whole gate block fall through when ``existing`` is an
    estimated fallback (is_reliable_fix False). No consume and no housekeeping pop
    run on that path, so a pre-existing anchor simply survives UNTOUCHED - it must
    not thereby gain an extended life. This test pins that the surviving anchor is
    still only recoverable within ROUND_TRIP_TTL_S: a later return that lands on
    the anchor coordinates but arrives past the TTL is NOT recovered (it is gated
    as a normal teleport), proving the survival does not widen the TTL window.

    Mutation guard: none of the AP-3 production edits are asserted positively here
    (this is a bounding/regression guard); it documents that the estimated-skip
    path leaves the anchor's TTL semantics unchanged.
    """
    anchor_ts = BASE_TS
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(anchor_ts)}}

    # Step 1: a fast crowd fix against an ESTIMATED cached endpoint. F-CODEX-1
    # makes the gate block fall through entirely (no consume, no pop), so the
    # anchor survives untouched. existing is FAR/estimated; incoming lands far
    # from the anchor (at FAR) so even if the gate ran it would not recover.
    coord = _coord(
        _fix(FAR, last_seen=BASE_TS, acc=200.0, estimated=True), anchors=anchors
    )
    assert _fuse(coord, _fix(FAR, last_seen=BASE_TS + 300, acc=50.0)) is True
    assert coord._round_trip_anchors["dev"]["ts"] == float(anchor_ts)  # untouched

    # Step 2: the device is now at a RELIABLE B; a return lands exactly on the
    # anchor coordinates but PAST the TTL (delta_anchor > ROUND_TRIP_TTL_S). The
    # surviving anchor must not recover it: it is gated as a normal teleport.
    expired_return_ts = anchor_ts + ROUND_TRIP_TTL_S + 120
    gated_b_seen = expired_return_ts - 300  # keeps ~833 m/s > cap on the return
    coord._device_location_data["dev"] = _fix(FAR, last_seen=gated_b_seen, acc=20.0)
    result = _fuse(coord, _fix(HOME, last_seen=expired_return_ts, acc=50.0))
    assert result is False
    coord.increment_stat.assert_called_with("speed_gate_rejects")


def test_anchor_surviving_estimated_skip_radius_bounded() -> None:
    """The anchor that survived an estimated-existing skip is also still
    radius-bounded: a within-TTL return OUTSIDE the anchor radius does NOT recover
    (companion to the TTL bound, F2).
    """
    anchor_ts = BASE_TS
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(anchor_ts)}}
    coord = _coord(
        _fix(FAR, last_seen=BASE_TS, acc=200.0, estimated=True), anchors=anchors
    )
    # Estimated-existing skip leaves the anchor untouched.
    assert _fuse(coord, _fix(FAR, last_seen=BASE_TS + 300, acc=50.0)) is True

    # Device now at a reliable B; a within-TTL return that lands just OUTSIDE the
    # 200 m radius of the anchor is not recovered.
    outside = _point_north(HOME, 260.0)
    assert (
        _haversine_distance_impl(HOME[0], HOME[1], *outside)
        > ROUND_TRIP_ANCHOR_RADIUS_M
    )
    coord._device_location_data["dev"] = _fix(FAR, last_seen=BASE_TS, acc=20.0)
    result = _fuse(coord, _fix(outside, last_seen=BASE_TS + 300, acc=50.0))
    assert result is False
    coord.increment_stat.assert_called_with("speed_gate_rejects")
