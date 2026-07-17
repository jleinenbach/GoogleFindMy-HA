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

import time
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
    nd = _fix(FAR, last_seen=b_ts, acc=50.0)
    result = _fuse(coord, nd)
    assert result is True
    # Fund A: store mutation deferred to update_device_cache; assert the intent marker
    assert nd["_round_trip_anchor_seed"] == {
        "lat": HOME[0],
        "lon": HOME[1],
        "ts": float(b_ts),
    }
    assert "dev" not in coord._round_trip_anchors  # store untouched by unbound fusion


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
    seed_nd = _fix(FAR, last_seen=b_ts, acc=50.0)
    assert _fuse(coord, seed_nd) is True
    # Fund A: store mutation deferred to update_device_cache; assert the intent marker.
    # Anchor ts is the accept time (B), not A's stale last_seen.
    assert seed_nd["_round_trip_anchor_seed"]["ts"] == float(b_ts)
    # The unbound fusion does not seed the store; for the recovery step to read the
    # anchor, apply the deferred seed manually (stands in for update_device_cache).
    coord._round_trip_anchors["dev"] = seed_nd["_round_trip_anchor_seed"]
    # The device now sits at B; a correction back near A arrives 300 s after B
    # (well within the 900 s TTL) but is a kinematic teleport B->A -> recover.
    coord._device_location_data["dev"] = _fix(FAR, last_seen=b_ts)
    return_nd = _fix(HOME, last_seen=b_ts + 300, acc=50.0)
    result = _fuse(coord, return_nd)
    assert result is True
    # Fund A: consume is deferred; the intent marker is set and the anchor survives
    # the unbound fusion (update_device_cache would pop it post-commit).
    assert return_nd["_round_trip_anchor_consume"] is True
    assert "dev" in coord._round_trip_anchors  # not popped by unbound fusion


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
    nd = _fix(HOME, last_seen=BASE_TS + 300, acc=50.0)
    result = _fuse(coord, nd)
    assert result is True
    coord.increment_stat.assert_called_once_with("round_trip_recoveries")
    # Fund A: store mutation deferred to update_device_cache; assert the intent marker.
    # The consume is deferred, so the anchor survives the unbound fusion.
    assert nd["_round_trip_anchor_consume"] is True
    assert "dev" in coord._round_trip_anchors  # not popped by unbound fusion


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
    nd = _fix(HOME, last_seen=BASE_TS + 300, acc=50.0)
    result = _fuse(coord, nd)
    assert result is True
    # Fund A: store mutation deferred to update_device_cache; assert the intent marker.
    # The returning fix marks a consume (one-shot) and NEVER a fresh seed for
    # itself (DF-1 ping-pong bollwerk): the seed marker must be absent.
    assert nd["_round_trip_anchor_consume"] is True
    assert "_round_trip_anchor_seed" not in nd


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
    return_nd = _fix(HOME, last_seen=BASE_TS + 300, acc=50.0)
    result = _fuse(coord, return_nd)
    assert result is True
    coord.increment_stat.assert_called_with("round_trip_recoveries")
    # Fund A: store mutation deferred to update_device_cache; assert the intent marker.
    # The consume is deferred, so the anchor survives the unbound fusion.
    assert return_nd["_round_trip_anchor_consume"] is True
    assert "dev" in coord._round_trip_anchors  # not popped by unbound fusion


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


def test_own_report_clear_jump_marks_supersede_even_without_source_anchor() -> None:
    """An own-report clear jump marks supersede even when THIS device holds no
    anchor (F-CODEX-9).

    The marker no longer keys on the source device's own anchor: a shared tracker
    propagates this fresh trusted position to sibling device ids post-commit, and
    a sibling may hold a stale anchor even when the source does not. Marking
    unconditionally lets the post-commit handling clear those propagated siblings;
    the source-side pop stays a harmless idempotent no-op when the source has no
    anchor.

    Mutation guard: re-gating the marker on ``_anchors.get(device_id)`` (the old
    source-anchor condition) leaves the marker unset here, so this assertion
    fails - and, via update_device_cache, a propagated sibling's stale anchor
    would survive.
    """
    coord = _coord(_fix(FAR, last_seen=BASE_TS, acc=50.0))  # no source anchor
    new_data = _fix(HOME, last_seen=BASE_TS + 300, acc=50.0, is_own=True)
    result = _fuse(coord, new_data)
    assert result is True
    assert new_data.get("_supersede_round_trip_anchor") is True


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


# ------------------- seed/consume ordering vs. significance gate (Fund A, AP-2)

# Significance-reject axis choice (Fund A ordering tests). Both the anchor SEED
# and CONSUME markers are set only INSIDE the speed gate's ``delta_t > 0`` block
# (crowd, forward-of-cache). The timestamp-REGRESSION reject axis (``new_ts <
# cached last_seen``) forces ``delta_t < 0``, so the gate never fires and NO
# marker is ever set - a regression-based ordering test would be VACUOUS (it
# would not exercise the deferred store mutation at all; verified empirically).
# The only significance-reject axis compatible with ``delta_t > 0`` is the
# FUTURE-DRIFT guard (``new_ts > time.time() + 7200`` in _is_significant_update):
# a forward, plausible jump sets the marker in fusion, then the future-drift
# check discards the payload before the post-commit apply. These two tests
# therefore anchor everything to ``time.time()`` and add a STRUCTURAL guard
# (implied_speed vs. cap / delta vs. TTL) so the behavioral premise stays pinned
# regardless of wall-clock jitter.
_FUTURE_DRIFT_S = 7200  # _is_significant_update's local MAX_ACCEPTED..._DRIFT_S
_SPEED_CAP_MPS = 400.0  # DEFAULT_MAX_PLAUSIBLE_SPEED_MPS


def test_seed_not_applied_when_significance_rejects() -> None:
    """Fund A ordering: a seed marker set mid-fusion must NOT reach the store when
    the significance gate rejects the payload (via ``update_device_cache``).

    An anchorless coordinator takes a crowd clear-jump A->B whose implied speed is
    plausible (accept path -> seed marker set), but whose ``last_seen`` sits past
    the future-drift horizon so ``_is_significant_update`` discards it (early
    return @520, before the post-commit seed apply). The deferred seed must never
    land: no phantom anchor, cache position unchanged.

    Axis note: the regression axis is unusable here (it forces ``delta_t < 0`` so
    the gate never fires and no seed marker is set at all - a vacuous test); the
    future-drift axis is the only significance-reject compatible with the
    ``delta_t > 0`` accept path that seeds. A structural guard pins the accept
    (``implied_speed < cap``) so the premise cannot silently rot.

    Mutation cross-check: revert cache.py's seed to a direct
    ``anchors[device_id]=...`` mid-fusion -> the anchor appears despite the
    rejected payload -> this test goes red.
    """
    now = time.time()
    a_ts = now  # cached A's last_seen
    # B seen far in the future -> significance future-drift reject; delta_t huge
    # but positive so the gate fires and the accept path seeds.
    b_ts = now + _FUTURE_DRIFT_S + 800.0
    coord = _make_update_coordinator(_fix(HOME, last_seen=a_ts, acc=20.0))
    # Structural guard: the jump is a plausible ACCEPT (seed path), not a reject.
    implied_speed = _haversine_distance_impl(*HOME, *FAR) / (b_ts - a_ts)
    assert implied_speed < _SPEED_CAP_MPS

    coord.update_device_cache("dev", _fix(FAR, last_seen=b_ts, acc=20.0))

    # Significance rejected the payload: no seed reached the store, cache unchanged.
    assert "dev" not in coord._round_trip_anchors  # no phantom anchor
    assert coord._device_location_data["dev"]["latitude"] == pytest.approx(HOME[0])


def test_consume_not_applied_when_significance_rejects() -> None:
    """Fund A ordering: a consume marker set mid-fusion must NOT pop the anchor when
    the significance gate rejects the payload (via ``update_device_cache``).

    A coordinator holds anchor A (seeded low, TTL-valid) with the device cached at
    B. A crowd return lands on A: it is gated as a teleport (implied speed > cap)
    but within TTL+radius of the anchor, so the recovery path fires and sets the
    consume marker. Its ``last_seen`` is past the future-drift horizon, so
    ``_is_significant_update`` discards the payload (early return @520) before the
    post-commit consume apply. The anchor must SURVIVE and the cache stay at B.

    Geometry: recovery requires ``delta_t > 0`` (gate fires), ``0 <= delta_anchor
    <= TTL`` and ``return_dist <= radius``; the future-drift reject needs
    ``new_ts > now + 7200``. Anchor ts is set 600 s below new_ts (TTL-valid,
    delta_anchor >= 0) and cached B's last_seen 300 s below new_ts (delta_t > 0,
    ~833 m/s > cap). A structural guard pins ``delta <= ROUND_TRIP_TTL_S``.

    Axis note: the regression axis would force ``delta_t < 0`` so the recovery path
    never fires and no consume marker is set (vacuous); future-drift is the only
    reject axis compatible with the ``delta_t > 0`` recovery path.

    Mutation cross-check: revert cache.py's consume to a direct
    ``anchors.pop(device_id, None)`` mid-fusion -> the anchor is consumed despite
    the rejected payload -> this test goes red.
    """
    now = time.time()
    new_ts = now + _FUTURE_DRIFT_S + 800.0  # future-drift reject; forward of cache
    cached_b_ts = new_ts - 300.0  # delta_t = 300 s -> ~833 m/s > cap (gate fires)
    anchor_ts = new_ts - 600.0  # delta_anchor = 600 s: TTL-valid and >= 0
    # Structural guard: the return sits inside the anchor TTL (recovery path live).
    delta = new_ts - anchor_ts
    assert delta <= ROUND_TRIP_TTL_S
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": anchor_ts}}
    coord = _make_update_coordinator(
        _fix(FAR, last_seen=cached_b_ts, acc=20.0), anchors=anchors
    )

    # A crowd return onto A: recovery-consume marker set in fusion, then the
    # payload is future-drift rejected before the post-commit consume apply.
    coord.update_device_cache("dev", _fix(HOME, last_seen=new_ts, acc=20.0))

    # The anchor survives (consume never applied) and the cache stays at B.
    assert coord._round_trip_anchors["dev"]["lat"] == pytest.approx(HOME[0])
    assert coord._device_location_data["dev"]["latitude"] == pytest.approx(FAR[0])


def test_seed_applied_when_significance_accepts() -> None:
    """Fund A ordering (positive control to test_seed_not_applied...): an ACCEPTED
    crowd clear-jump seeds the anchor post-commit via ``update_device_cache``.

    An anchorless coordinator takes a plausible A->B jump (dt = 3 h -> ~23 m/s <
    cap, accept path -> seed marker) that the significance gate ACCEPTS (B is a
    large, significant move from A). The post-commit apply then seeds the anchor
    with A's coordinates and the accept-time (B's) ts. This exercises the
    post-commit seed block including the lazy-init branch: ``_round_trip_anchors``
    is forced to ``None`` beforehand (the harness would otherwise pre-init ``{}``
    and the lazy-init would never fire), covering cache.py 615-622.
    """
    b_ts = BASE_TS + 3 * 3600  # dt = 3 h -> implied ~23 m/s < cap -> accept
    coord = _make_update_coordinator(_fix(HOME, last_seen=BASE_TS, acc=20.0))
    # Force the lazy-init branch of the post-commit seed apply (cache.py 619-621).
    coord._round_trip_anchors = None

    coord.update_device_cache("dev", _fix(FAR, last_seen=b_ts, acc=20.0))

    # Seed applied post-commit through the lazy-init path; cache advanced to B.
    assert coord._round_trip_anchors["dev"] == {
        "lat": HOME[0],
        "lon": HOME[1],
        "ts": float(b_ts),
    }
    assert coord._device_location_data["dev"]["latitude"] == pytest.approx(FAR[0])


def test_consume_applied_when_significance_accepts() -> None:
    """Fund A ordering (positive control to test_consume_not_applied...): an
    ACCEPTED recovery return pops the anchor post-commit via ``update_device_cache``.

    A coordinator holds anchor A (TTL-valid) with the device cached at B. A crowd
    return lands on A within TTL+radius: it is gated as a teleport (implied speed >
    cap) yet recovers on the anchor, so the recovery path sets the consume marker.
    Its ``last_seen`` is forward of cached B (no regression) and near-present (no
    future-drift), so the significance gate ACCEPTS the payload. The post-commit
    apply then pops the one-shot anchor, covering cache.py 611-614.
    """
    cached_b_ts = BASE_TS  # cached B's last_seen
    anchor_ts = BASE_TS  # delta_anchor = 300 s -> TTL-valid and >= 0
    new_ts = BASE_TS + 300  # delta_t = 300 s -> ~833 m/s > cap; > cached B -> accept
    # Structural guard: the return sits inside the anchor TTL (recovery path live).
    assert new_ts - anchor_ts <= ROUND_TRIP_TTL_S
    anchors = {"dev": {"lat": HOME[0], "lon": HOME[1], "ts": float(anchor_ts)}}
    coord = _make_update_coordinator(
        _fix(FAR, last_seen=cached_b_ts, acc=20.0), anchors=anchors
    )

    coord.update_device_cache("dev", _fix(HOME, last_seen=new_ts, acc=20.0))

    # Consume applied post-commit: the one-shot anchor is popped; cache moved to A.
    assert "dev" not in coord._round_trip_anchors
    assert coord._device_location_data["dev"]["latitude"] == pytest.approx(HOME[0])


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


# ---------------- shared-device anchor propagation (F-CODEX-9, AP-3 sweep)


def _make_shared_update_coordinator(
    *,
    ik: bytes,
    rt_enabled: bool = True,
) -> Any:
    """A coordinator wired for ``update_device_cache`` with two devices sharing
    one identity key, both carrying a stale round-trip anchor at HOME (A).

    ``dev_a`` is the source that receives the own-report clear jump; ``dev_b``
    shares the physical tracker via ``identity_key`` and holds an OLDER cached
    position so the post-commit propagation adopts the fresh location.
    """
    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_location_data = {
        "dev_a": {**_fix(FAR, last_seen=BASE_TS, acc=20.0), "identity_key": ik},
        "dev_b": {**_fix(FAR, last_seen=BASE_TS - 10, acc=20.0), "identity_key": ik},
    }
    coord._device_names = {}
    coord._device_update_history = {}
    coord._identity_key_to_devices = {ik: {"dev_a", "dev_b"}}
    coord._round_trip_anchors = {
        "dev_a": {"lat": HOME[0], "lon": HOME[1], "ts": BASE_TS},
        "dev_b": {"lat": HOME[0], "lon": HOME[1], "ts": BASE_TS},
    }
    coord.increment_stat = lambda *_a, **_k: None
    coord._apply_report_type_cooldown = lambda *_a, **_k: None
    coord._is_on_hass_loop = lambda: True
    coord._run_on_hass_loop = lambda *_a, **_k: None
    coord._speed_gate_enabled = lambda: True
    coord._round_trip_confirm_enabled = lambda: rt_enabled
    coord._record_last_good_location("dev_a", coord._device_location_data["dev_a"])
    coord._record_last_good_location("dev_b", coord._device_location_data["dev_b"])
    return coord


def test_supersede_invalidates_propagated_shared_anchor() -> None:
    """F-CODEX-9: a trusted own-report clear jump on a shared tracker invalidates
    the round-trip anchor of every propagated shared target, not just the source.

    Two device ids share one physical tracker (identity key). Both hold a stale
    anchor at A. An own-report clear jump B->C on ``dev_a`` commits, pops its own
    anchor, and propagates C to ``dev_b``. The post-commit supersede action must
    ride that same propagation and drop ``dev_b``'s stale anchor too, otherwise a
    later crowd echo near A on ``dev_b`` would take the round-trip recovery path
    and roll the propagated target back to the stale A position.

    Mutation guard: dropping ``supersede_anchors`` in the propagation loop leaves
    ``dev_b``'s anchor alive, so (a) the ``dev_b not in anchors`` assertion fails
    and (b) the step-3 crowd echo, still WITHIN the anchor TTL, recovers ``dev_b``
    back onto A. The geometry keeps the echo inside ROUND_TRIP_TTL_S so (b) is a
    live guard, not a vacuous TTL-expiry pass.
    """
    ik = b"\x11" * 32
    coord = _make_shared_update_coordinator(ik=ik)

    # Own-report clear jump B(FAR) -> C on dev_a. C is a clear jump far from both
    # HOME (A) and FAR (B). c_ts is newer than dev_b's cached B ts (propagation
    # adopts C) and close to the anchor ts (BASE_TS) so the step-3 echo lands
    # within ROUND_TRIP_TTL_S of the anchor - the behavioral guard stays live.
    C = (46.000000, 11.400000)
    c_ts = BASE_TS + 300
    coord.update_device_cache(
        "dev_a",
        {**_fix(C, last_seen=c_ts, acc=20.0, is_own=True), "identity_key": ik},
    )

    # Source anchor popped (existing F-FABLE-2 behavior).
    assert "dev_a" not in coord._round_trip_anchors
    # Fresh location propagated to the shared target...
    assert coord._device_location_data["dev_b"]["latitude"] == pytest.approx(C[0])
    # ...and its stale anchor invalidated with it (F-CODEX-9, the fix).
    assert "dev_b" not in coord._round_trip_anchors

    # Behavioral proof: a later crowd echo near A on dev_b, arriving WITHIN the
    # anchor TTL (delta_anchor = 600 s <= ROUND_TRIP_TTL_S), is now gated (no
    # anchor to recover on) and dropped, so dev_b stays at C. With the anchor
    # still alive (mutation) it would recover onto A.
    echo_ts = c_ts + 300  # BASE_TS + 600, delta from anchor ts (BASE_TS) < 900
    assert echo_ts - BASE_TS <= ROUND_TRIP_TTL_S
    coord.update_device_cache("dev_b", _fix(HOME, last_seen=echo_ts, acc=20.0))
    assert coord._device_location_data["dev_b"]["latitude"] == pytest.approx(C[0])


def test_supersede_preserves_fresher_shared_target_anchor() -> None:
    """F-CODEX-9 precision: a shared target that is FRESHER than the superseding
    location (propagation skipped by the freshness gate) keeps its own anchor.

    The invalidation rides the propagation data-flow, so a target that never
    adopts the superseding location must not lose its anchor - the fix clears
    only what it actually propagated (SA-8d scoping).

    Mutation guard: clearing anchors unconditionally (before the freshness gate,
    Option A) would drop the fresher target's anchor, so this ``anchor survives``
    assertion fails.
    """
    ik = b"\x22" * 32
    coord = _make_shared_update_coordinator(ik=ik)
    # Make dev_b FRESHER than the incoming own-report so propagation is skipped.
    fresh_ts = BASE_TS + 10 * 3600
    coord._device_location_data["dev_b"]["last_seen"] = fresh_ts

    C = (46.000000, 11.400000)
    c_ts = BASE_TS + 3 * 3600  # OLDER than dev_b's fresh_ts -> propagation skipped
    coord.update_device_cache(
        "dev_a",
        {**_fix(C, last_seen=c_ts, acc=20.0, is_own=True), "identity_key": ik},
    )

    # dev_b did NOT adopt C (it is fresher) -> its anchor stays intact.
    assert coord._device_location_data["dev_b"]["latitude"] == pytest.approx(FAR[0])
    assert coord._round_trip_anchors["dev_b"]["lat"] == pytest.approx(HOME[0])


def test_supersede_clears_shared_anchor_when_source_has_no_anchor() -> None:
    """F-CODEX-9 (R1): a shared target's stale anchor is cleared even when the
    SOURCE device holds no anchor of its own.

    The supersede marker is set on any trusted own clear-jump, not only when the
    source holds an anchor, so a propagated sibling that still carries a stale
    anchor is invalidated too. Without this, an own clear-jump on an anchorless
    source would propagate the fresh position but leave the sibling's anchor
    alive, keeping the F-CODEX-9 rollback reachable.

    Mutation guard: re-gating the marker on the source anchor (the old condition)
    leaves ``dev_b``'s anchor alive; (a) the ``dev_b not in anchors`` assertion
    fails and (b) the within-TTL crowd echo recovers ``dev_b`` onto A.
    """
    ik = b"\x33" * 32
    coord = _make_shared_update_coordinator(ik=ik)
    # Source dev_a holds NO anchor; only the shared target dev_b does.
    del coord._round_trip_anchors["dev_a"]
    assert coord._round_trip_anchors["dev_b"]["lat"] == pytest.approx(HOME[0])

    C = (46.000000, 11.400000)
    c_ts = BASE_TS + 300
    coord.update_device_cache(
        "dev_a",
        {**_fix(C, last_seen=c_ts, acc=20.0, is_own=True), "identity_key": ik},
    )
    # Fresh location propagated and the sibling's stale anchor cleared.
    assert coord._device_location_data["dev_b"]["latitude"] == pytest.approx(C[0])
    assert "dev_b" not in coord._round_trip_anchors

    # Behavioral proof: a within-TTL crowd echo near A on dev_b is now dropped.
    echo_ts = c_ts + 300
    assert echo_ts - BASE_TS <= ROUND_TRIP_TTL_S
    coord.update_device_cache("dev_b", _fix(HOME, last_seen=echo_ts, acc=20.0))
    assert coord._device_location_data["dev_b"]["latitude"] == pytest.approx(C[0])
