# tests/test_cache_speed_gate.py
"""Behavioral tests for the kinematic speed gate (Discussion #177).

The gate lives in the clear-jump branch of ``_apply_weighted_location_fusion``:
a far jump (``dist > radius_sum``) whose implied speed ``dist / dt`` exceeds
``DEFAULT_MAX_PLAUSIBLE_SPEED_MPS`` is rejected instead of teleporting the
tracker. Crowd-awareness: cryptographically trusted own-report GPS fixes
(``is_own_report`` truthy) bypass the gate entirely. Missing or degenerate
timestamps fall through to the previous accept-as-is behavior (F4-a, no
regression).

These tests mirror the harness of ``test_cache_fusion_validity.py``: a
``MagicMock(spec=CacheOperations)`` carrying the cached ``existing`` fix, with
``_apply_weighted_location_fusion`` invoked unbound.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.const import (
    DEFAULT_MAX_PLAUSIBLE_SPEED_MPS,
    DEFAULT_SPEED_GATE_ENABLED,
    OPT_SPEED_GATE_ENABLED,
)
from custom_components.googlefindmy.coordinator.cache import (
    CacheOperations,
    _haversine_distance_impl,
)
from custom_components.googlefindmy.coordinator.helpers.update import (
    filter_and_dedupe_devices,
)

CAP = DEFAULT_MAX_PLAUSIBLE_SPEED_MPS  # 400.0 m/s
BASE_TS = 1_700_000_000  # a realistic epoch (avoids pre-Y2K corruption guards)

# ~250 km apart (2.25 deg of latitude), so any sane accuracy sum is far below
# the distance and the clear-jump branch (dist > radius_sum) executes.
HOME = (48.100000, 11.400000)
FAR = (50.350000, 11.400000)


def _teleport_distance() -> float:
    """Distance the fusion sees for a HOME -> FAR jump (same impl as prod)."""
    return _haversine_distance_impl(HOME[0], HOME[1], FAR[0], FAR[1])


def _coord(existing: dict[str, Any], *, gate_enabled: bool = True) -> MagicMock:
    coord = MagicMock(spec=CacheOperations)
    coord._device_location_data = {"dev": existing}
    coord.increment_stat = MagicMock()
    coord._speed_gate_enabled = MagicMock(return_value=gate_enabled)
    return coord


def _existing(*, last_seen: Any = BASE_TS, acc: float = 20.0) -> dict[str, Any]:
    fix: dict[str, Any] = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": acc,
        "location_type": "sensor",
    }
    if last_seen is not None:
        fix["last_seen"] = last_seen
    return fix


def _incoming(
    *,
    last_seen: Any = BASE_TS,
    acc: float | None = 50.0,
    is_own: Any = False,
    lat: float = FAR[0],
    lon: float = FAR[1],
) -> dict[str, Any]:
    fix: dict[str, Any] = {"latitude": lat, "longitude": lon, "accuracy": acc}
    if last_seen is not None:
        fix["last_seen"] = last_seen
    if is_own is not None:
        fix["is_own_report"] = is_own
    return fix


def _fuse(coord: MagicMock, new_data: dict[str, Any]) -> bool:
    return CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)


# ---------------------------------------------------------------- core gate (1-8)


def test_teleport_blocked() -> None:
    """250 km in 300 s (~833 m/s) is physically impossible -> rejected."""
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300))
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")


def test_real_trip_passes() -> None:
    """250 km in 3 h (~23 m/s) is plausible travel -> accepted."""
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 3 * 3600))
    assert result is True
    coord.increment_stat.assert_not_called()


def test_delta_t_non_positive_falls_through() -> None:
    """Equal timestamps -> dt == 0 -> fall through to accept (F4-a)."""
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS))
    assert result is True
    coord.increment_stat.assert_not_called()


def test_missing_timestamp_falls_through() -> None:
    """No last_seen on the incoming fix -> speed not computable -> accept."""
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=None))
    assert result is True
    coord.increment_stat.assert_not_called()


def test_speed_at_cap_boundary() -> None:
    """Just above the cap is rejected; just below passes (integer dt)."""
    dist = _teleport_distance()
    dt_block = math.floor(dist / (CAP + 5.0))  # implied speed >= CAP + 5 > CAP
    dt_pass = math.ceil(dist / (CAP - 5.0))  # implied speed <= CAP - 5 < CAP

    coord_block = _coord(_existing(last_seen=BASE_TS))
    assert _fuse(coord_block, _incoming(last_seen=BASE_TS + dt_block)) is False

    coord_pass = _coord(_existing(last_seen=BASE_TS))
    assert _fuse(coord_pass, _incoming(last_seen=BASE_TS + dt_pass)) is True


def test_trusted_anchor_unaffected() -> None:
    """A trusted existing anchor still snaps overlapping fixes back, ungated."""
    existing = _existing(last_seen=BASE_TS)
    existing["location_type"] = "trusted"
    coord = _coord(existing)
    # Overlapping (near-identical) coordinates so the trusted-anchor branch,
    # which returns before the clear-jump gate, handles it.
    new_data = _incoming(
        last_seen=BASE_TS + 10, lat=HOME[0] + 0.000001, lon=HOME[1] + 0.000001
    )
    result = _fuse(coord, new_data)
    assert result is True
    assert new_data["location_type"] == "trusted"
    coord.increment_stat.assert_not_called()


def test_gate_disabled_option() -> None:
    """With the option off, an impossible jump is accepted as before."""
    coord = _coord(_existing(last_seen=BASE_TS), gate_enabled=False)
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300))
    assert result is True
    coord.increment_stat.assert_not_called()


def test_blocked_teleport_not_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blocked teleport returns False and leaves the cached fix untouched.

    Fusion returning False is the contract that stops the caller before the
    last-good recording / propagation steps (A8); at this layer the proof is
    that the stored HOME fix is not overwritten by the rejected FAR fix.
    """
    existing = _existing(last_seen=BASE_TS)
    coord = _coord(existing)
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300))
    assert result is False
    assert coord._device_location_data["dev"]["latitude"] == HOME[0]
    assert coord._device_location_data["dev"]["longitude"] == HOME[1]


# ------------------------------------------------------ stat + robustness (9-14)


def test_speed_gate_stat_registered_and_counted() -> None:
    """Mutation probe for F1: a block increments exactly the registered key."""
    coord = _coord(_existing(last_seen=BASE_TS))
    _fuse(coord, _incoming(last_seen=BASE_TS + 300))
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")


def test_delta_t_quantized_to_zero_falls_through() -> None:
    """Sub-second timestamps collapse to dt == 0 via int() -> accept (F4-a)."""
    coord = _coord(_existing(last_seen=float(BASE_TS)))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 0.4))
    assert result is True
    coord.increment_stat.assert_not_called()


def test_new_data_missing_last_seen_falls_through() -> None:
    """Incoming fix without a timestamp -> accept (no false block)."""
    coord = _coord(_existing(last_seen=BASE_TS))
    assert _fuse(coord, _incoming(last_seen=None)) is True


def test_existing_missing_last_seen_falls_through() -> None:
    """Cached fix without a timestamp -> accept (no false block)."""
    coord = _coord(_existing(last_seen=None))
    assert _fuse(coord, _incoming(last_seen=BASE_TS + 300)) is True


def test_regressed_timestamp_gate_falls_through() -> None:
    """new_ts < existing_ts -> dt < 0 -> gate falls through (accept)."""
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS - 100))
    assert result is True
    coord.increment_stat.assert_not_called()


def test_gate_after_haversine_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A haversine failure returns True before the gate is reached (F8)."""

    def _boom(*_args: Any, **_kwargs: Any) -> float:
        raise ValueError("boom")

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.cache._haversine_distance_impl",
        _boom,
    )
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300))
    assert result is True
    coord.increment_stat.assert_not_called()


# ---------------------------------------------------- crowd-awareness Q1 (15-17)


def test_own_report_bypasses_gate() -> None:
    """A trusted own-report GPS fix skips the gate even when it looks fast."""
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300, is_own=True))
    assert result is True
    coord.increment_stat.assert_not_called()


def test_crowd_report_gated() -> None:
    """An explicit crowd report (is_own_report False) is gated and rejected."""
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300, is_own=False))
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")


def test_jetstream_speed_under_cap_passes() -> None:
    """Even a gated crowd fix at record jetstream speed (~369 m/s) passes.

    Proves the Q3 headroom: the cap (400 m/s) sits above the documented record
    tailwind airliner ground speed, so a legitimate fast fix is not blocked.
    """
    dist = _teleport_distance()
    dt = math.ceil(dist / 369.0)  # implied speed ~369 m/s < 400 cap
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + dt, is_own=False))
    assert result is True
    coord.increment_stat.assert_not_called()


# ----------------------------------------------- config-entry + F1-a guard (18-19)


def test_config_entry_none_uses_default() -> None:
    """_speed_gate_enabled tolerates a missing/None config_entry (P4)."""
    coord_none = MagicMock()
    coord_none.config_entry = None
    assert CacheOperations._speed_gate_enabled(coord_none) is DEFAULT_SPEED_GATE_ENABLED

    entry = MagicMock()
    entry.options = {OPT_SPEED_GATE_ENABLED: False}
    coord_opt = MagicMock()
    coord_opt.config_entry = entry
    assert CacheOperations._speed_gate_enabled(coord_opt) is False


def test_basic_list_seed_carries_no_coordinates() -> None:
    """F1-a regression guard: the structural seed transform never emits an entry
    that is both own-report-trusted and coordinate-bearing.

    The seed path (polling -> update_device_cache -> fusion) skips the
    ``sanitize_decoder_row`` hardening the locate/poll paths run, so a spurious
    ``is_own_report=True`` carrying coordinates could bypass the crowd-aware
    gate. The verified invariant is that the basic device list is minimal
    (id/name/can_ring; ``is_own_report`` placeholder None, no coordinates) and
    the structural normalize/dedupe transform neither injects coordinates nor a
    truthy own-report flag. This test pins that transform; it breaks if a future
    payload change starts carrying both into the seed, at which point F1 must be
    re-evaluated (see plan N5/P5).
    """
    basic_list = [
        {
            "id": "dev1",
            "name": "Keys",
            "can_ring": True,
            "is_own_report": None,
            "latitude": None,
            "longitude": None,
        },
        {"id": "  dev2  ", "name": "Backpack"},
        {"id": "dev1", "name": "Keys duplicate"},  # dedupe: dropped
    ]
    filtered, seen = filter_and_dedupe_devices(basic_list)

    assert seen == {"dev1", "dev2"}
    for entry in filtered:
        has_coords = (
            entry.get("latitude") is not None and entry.get("longitude") is not None
        )
        assert not (bool(entry.get("is_own_report")) and has_coords)


# ----------------------------------------- accuracy-less bypass guard (20, #1181)


def test_accuracy_less_crowd_fix_bypasses_gate() -> None:
    """An accuracy-less crowd fix falls through the gate (regression #1181).

    A far crowd fix without a measured accuracy (accuracy=None) is not a clean
    teleport discriminant: the decoder strips the accuracy number (acc_rank
    = -inf) and the downstream _is_significant_update sanitization flags it
    ``accuracy_estimated=True``. Hard-dropping it here via ``return False`` would
    skip that sanitization and break the last-good protection asserted by
    tests/test_plus_code_last_known.py. The reliable last-good stays protected by
    is_reliable_fix, independent of this gate, so the fix must pass through
    (result True) and the reject counter must NOT fire. This is the mutation
    guard for the ``new_acc_raw is not None`` clause: without it the result would
    be False.
    """
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300, acc=None, is_own=False))
    assert result is True
    coord.increment_stat.assert_not_called()


# ------------------------------------ invalid-accuracy sentinel bypass (21-22)


def test_sentinel_zero_accuracy_crowd_fix_bypasses_gate() -> None:
    """A crowd fix with Android's no-accuracy sentinel (0.0) falls through.

    Codex review of 5bfeafd: ``accuracy=0.0`` coerces to a non-None float, so a
    guard testing only ``new_acc_raw is not None`` would still hard-drop the fix
    before _is_significant_update can sanitize it into accuracy_estimated=True.
    The sentinel belongs to the same error-code set that _safe_accuracy() maps
    to the 200m fallback, so it must be treated as accuracy-less and pass
    through (result True), leaving the counter untouched.
    """
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300, acc=0.0, is_own=False))
    assert result is True
    coord.increment_stat.assert_not_called()


def test_subphysical_accuracy_crowd_fix_bypasses_gate() -> None:
    """A crowd fix with a sub-physical accuracy (< MIN_PHYSICAL_ACCURACY_M).

    A finite value below the 0.001 m physical floor is also an error code (same
    branch in _safe_accuracy). It must be treated as accuracy-less and bypass
    the gate rather than being hard-dropped.
    """
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300, acc=0.0005, is_own=False))
    assert result is True
    coord.increment_stat.assert_not_called()


# ------------------------------ cryptographic own-report provenance (23-24)


def test_spoofed_network_own_flag_is_gated() -> None:
    """A network report with a spoofed is_own_report=True is still gated.

    Codex review of 74600c2: this integration's crowdsourced uploader stamps
    network reports with is_own_report=True while they carry a non-empty
    publicKeyRandom (decrypt_locations.py:1991-1998). The decrypt layer hardens
    is_own_report = is_own_report and not is_network_report
    (decrypted_location.py:39); the gate mirrors that invariant so a spoofed
    own-flag on an impossible teleport cannot skip the gate. With both flags set
    the fix is treated as a crowd report and the teleport is rejected.
    """
    coord = _coord(_existing(last_seen=BASE_TS))
    fix = _incoming(last_seen=BASE_TS + 300, is_own=True)
    fix["is_network_report"] = True
    result = _fuse(coord, fix)
    assert result is False
    coord.increment_stat.assert_called_once_with("speed_gate_rejects")


def test_genuine_own_report_without_network_flag_bypasses() -> None:
    """A genuine own report (is_own_report=True, no network flag) bypasses.

    Mutation guard for the ``not bool(new_data.get("is_network_report"))``
    clause: a real own GPS fix never carries the network flag, so an absent
    is_network_report must be treated as not-network and keep the bypass intact
    (result True, counter untouched) even at teleport speed.
    """
    coord = _coord(_existing(last_seen=BASE_TS))
    result = _fuse(coord, _incoming(last_seen=BASE_TS + 300, is_own=True))
    assert result is True
    coord.increment_stat.assert_not_called()
