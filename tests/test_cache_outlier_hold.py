# tests/test_cache_outlier_hold.py
"""Outlier hold: a single coarse foreign report leaving a zone is held.

Geometry is SHIFTED on purpose (public repository): every point is an east/north
offset in metres from a fictitious zone centre in the open Atlantic. Distances,
accuracies and time offsets are those of two observed incidents (no calendar
dates in the public repository); the earlier incident is reconstructed from
measured distances only.
"""

from __future__ import annotations

import inspect
import json
import logging
import math
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

ORIGIN = (45.0, -30.0)  # fictitious, not a place anyone lives
ZONE_RADIUS_M = 95.0
ZONE_ID = "zone.test_zone"
T0 = 1_800_000_000.0
_EARTH_RADIUS_M = 6_371_008.8

# Offsets (east_m, north_m) computed once from the measured incident distances.
A_26 = (15.7, -60.1)
B2_26 = (167.0, -144.7)
C2_26 = (1.4, -98.0)
A_25 = (15.7, -60.1)
B_25 = (517.7, -562.1)  # 710 m south-east of A
C_25 = (-24.3, -60.1)  # 40 m west of A
# Independent 1/acc**2 means, not the integration's code.
FUSED_A_C2 = (44.999444639, -29.999808257)
FUSED_B2_C2 = (44.999022183, -29.999498346)


def _place(east_m: float, north_m: float) -> tuple[float, float]:
    """Return (lat, lon) of an east/north offset from ``ORIGIN``."""
    lat = ORIGIN[0] + math.degrees(north_m / _EARTH_RADIUS_M)
    lon = ORIGIN[1] + math.degrees(
        east_m / (_EARTH_RADIUS_M * math.cos(math.radians(ORIGIN[0])))
    )
    return lat, lon


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Haversine distance, written here so the test does not reuse cache code."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _foreign(
    offset: tuple[float, float], accuracy: float, seen: float
) -> dict[str, Any]:
    """A foreign (crowd) report as the decrypt layer hands it to the cache."""
    lat, lon = _place(*offset)
    return {
        "latitude": lat,
        "longitude": lon,
        "accuracy": accuracy,
        "last_seen": seen,
        "is_own_report": False,
        "status": "AGGREGATED",
        "location_type": "sensor",
    }


def _coordinator(hass: HomeAssistant) -> Any:
    """A coordinator wired for the real ``update_device_cache`` path.

    Same ``__new__`` pattern as ``tests/test_cache_round_trip.py``; the gate
    switches are the real methods (no config entry, so the defaults apply).
    """
    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord.hass = hass
    coord._device_location_data = {}
    coord._device_names = {}
    coord._device_update_history = {}
    coord._round_trip_anchors = {}
    counts: Counter[str] = Counter()
    coord.increment_stat = lambda name, *_a, **_k: counts.update([name])
    coord.stat_counts = counts
    coord._apply_report_type_cooldown = lambda *_a, **_k: None
    coord._is_on_hass_loop = lambda: True
    coord._run_on_hass_loop = lambda *_a, **_k: None
    return coord


async def _setup_zone(hass: HomeAssistant) -> None:
    """Load one test zone; await the bus so ``async_in_zones`` sees it.

    The zone list is filled by a state-added listener, so without the
    ``async_block_till_done`` the zone exists as a state but no lookup finds it.
    """
    from homeassistant.components import zone

    zone.async_setup_track_zone_entity_ids(hass)
    hass.states.async_set(
        ZONE_ID,
        "0",
        {
            "latitude": ORIGIN[0],
            "longitude": ORIGIN[1],
            "radius": ZONE_RADIUS_M,
            "friendly_name": "Test zone",
        },
    )
    await hass.async_block_till_done()


def _push(
    coord: Any,
    freezer: FrozenDateTimeFactory,
    wall: float,
    row: dict[str, Any],
) -> dict[str, Any]:
    """Deliver ``row`` on the push path at wall-clock ``wall``; return the cache row."""
    freezer.move_to(datetime.fromtimestamp(wall, tz=UTC))
    coord.update_device_cache("dev", dict(row))
    return dict(coord._device_location_data["dev"])


def _active_zone(hass: HomeAssistant, row: dict[str, Any]) -> str | None:
    from homeassistant.components import zone

    active, _ = zone.async_in_zones(
        hass, row["latitude"], row["longitude"], row["accuracy"]
    )
    return None if active is None else active.entity_id


async def _run_26_11(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> tuple[Any, list[dict[str, Any]]]:
    await _setup_zone(hass)
    coord = _coordinator(hass)
    a = _foreign(A_26, 13.53, T0)
    b2 = _foreign(B2_26, 116.0, T0 + 1149)
    c2 = _foreign(C2_26, 63.35, T0 + 2300)
    after_a = _push(coord, freezer, T0 + 172, a)
    after_b2 = _push(coord, freezer, T0 + 1172, b2)
    after_c2 = _push(coord, freezer, T0 + 2685, c2)
    return coord, [after_a, after_b2, after_c2]


@pytest.mark.asyncio
async def test_incident_26_11_held_report_never_published(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """B2 (116 m, out of the zone A is in) is held; it is never the published row."""
    _coord, (after_a, after_b2, after_c2) = await _run_26_11(hass, freezer)

    assert after_a["last_seen"] == T0
    assert after_b2["last_seen"] == T0, "B2 was published instead of held"
    assert _active_zone(hass, after_b2) == ZONE_ID
    b2_position = _place(*B2_26)
    for row in (after_b2, after_c2):
        assert _distance_m((row["latitude"], row["longitude"]), b2_position) > 1.0
    assert _active_zone(hass, after_c2) == ZONE_ID


@pytest.mark.asyncio
async def test_incident_26_11_follow_up_fuses_with_predecessor_only(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """C2 fuses with A, not with the held B2."""
    _coord, (_after_a, _after_b2, after_c2) = await _run_26_11(hass, freezer)

    published = (after_c2["latitude"], after_c2["longitude"])
    assert _distance_m(published, FUSED_A_C2) <= 0.01, (
        f"published {published} is {_distance_m(published, FUSED_A_C2):.2f} m "
        f"from the A+C2 fusion ({_distance_m(published, FUSED_B2_C2):.2f} m from B2+C2)"
    )


@pytest.mark.asyncio
async def test_incident_25_11_reconstructed_held_and_discarded(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Geometry reconstructed from measured distances only.

    B (99.9 m, 710 m away, out of the zone) is held and never published; the
    zone judgement stays on the test zone through B and the follow-up C.
    """
    await _setup_zone(hass)
    coord = _coordinator(hass)
    a = _foreign(A_25, 15.5, T0)
    b = _foreign(B_25, 99.9, T0 + 278)
    c = _foreign(C_25, 100.0, T0 + 1179)

    after_a = _push(coord, freezer, T0 + 43, a)
    after_b = _push(coord, freezer, T0 + 713, b)
    after_c = _push(coord, freezer, T0 + 1619, c)

    assert after_a["last_seen"] == T0
    assert after_b["last_seen"] == T0, "B was published instead of held"
    b_position = _place(*B_25)
    for row in (after_b, after_c):
        assert _distance_m((row["latitude"], row["longitude"]), b_position) > 1.0
        assert _active_zone(hass, row) == ZONE_ID


# ---------------------------------------------------------------------------
# The fail-open arms, the order against the gates and the entry paths.
# ---------------------------------------------------------------------------

_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "googlefindmy"
    / "manifest.json"
)


def _hold_store(coord: Any) -> dict[str, Any]:
    return dict(getattr(coord, "_device_outlier_hold", None) or {})


async def _a_then(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    b2: dict[str, Any],
    *,
    zones: bool = True,
) -> tuple[Any, dict[str, Any]]:
    """Publish A of the 26th, then deliver ``b2`` on the push path."""
    if zones:
        await _setup_zone(hass)
    coord = _coordinator(hass)
    _push(coord, freezer, T0 + 172, _foreign(A_26, 13.53, T0))
    return coord, _push(coord, freezer, T0 + 1172, b2)


def _b2(**extra: Any) -> dict[str, Any]:
    row = _foreign(B2_26, 116.0, T0 + 1149)
    row.update(extra)
    return row


@pytest.mark.asyncio
async def test_trusted_semantic_report_is_never_held(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A trusted row carries a configured radius, not a measurement."""
    coord, after = await _a_then(hass, freezer, _b2(location_type="trusted"))

    assert after["last_seen"] == T0 + 1149
    assert _hold_store(coord) == {}
    assert coord.stat_counts["outlier_holds"] == 0


@pytest.mark.asyncio
async def test_own_report_is_never_held(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The device's own report is its own position; it is published."""
    coord, after = await _a_then(
        hass, freezer, _b2(is_own_report=True, is_network_report=False)
    )

    assert after["last_seen"] == T0 + 1149
    assert _hold_store(coord) == {}


@pytest.mark.asyncio
async def test_no_hass_means_no_hold(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without hass there is no zone judgement, so nothing is held."""
    await _setup_zone(hass)
    coord = _coordinator(hass)
    _push(coord, freezer, T0 + 172, _foreign(A_26, 13.53, T0))
    coord.hass = None
    after = _push(coord, freezer, T0 + 1172, _b2())

    assert after["last_seen"] == T0 + 1149
    assert _hold_store(coord) == {}


@pytest.mark.asyncio
async def test_no_zones_loaded_means_no_hold(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Zone tracking set up but no zone defined: A is in no zone."""
    from homeassistant.components import zone

    zone.async_setup_track_zone_entity_ids(hass)
    coord, after = await _a_then(hass, freezer, _b2(), zones=False)

    assert after["last_seen"] == T0 + 1149
    assert _hold_store(coord) == {}


@pytest.mark.asyncio
async def test_substituted_accuracy_is_never_held(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A radius the Google Home filter substituted is not a measurement."""
    coord, after = await _a_then(hass, freezer, _b2(_accuracy_substituted=True))

    assert after["last_seen"] == T0 + 1149
    assert _hold_store(coord) == {}


@pytest.mark.asyncio
async def test_zone_check_error_means_no_hold_and_logs_no_coordinates(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing zone lookup publishes the report and logs only the error type."""
    from custom_components.googlefindmy.coordinator import cache as cache_module

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("zone lookup failed")

    await _setup_zone(hass)
    coord = _coordinator(hass)
    _push(coord, freezer, T0 + 172, _foreign(A_26, 13.53, T0))
    # Patch the name the hold calls: the fixture above reloads the Home Assistant
    # modules, so the module object in sys.modules is not the one cache.py bound.
    monkeypatch.setattr(cache_module, "ha_zone", SimpleNamespace(async_in_zones=_boom))
    caplog.set_level(logging.DEBUG, logger="custom_components.googlefindmy")
    after = _push(coord, freezer, T0 + 1172, _b2())

    assert after["last_seen"] == T0 + 1149
    assert _hold_store(coord) == {}
    assert "zone check failed (RuntimeError)" in caplog.text
    for offset in (A_26, B2_26, C2_26):
        for value in _place(*offset):
            assert f"{value:.4f}" not in caplog.text


def test_manifest_lists_zone_after_dependency() -> None:
    """The hold reads ``zone.async_in_zones``; zone loads first when present."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))

    assert "zone" in manifest["after_dependencies"]


@pytest.mark.asyncio
async def test_report_inside_the_zone_is_not_held(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Coarse, much coarser and near, but still in the zone: published."""
    inside = _foreign((0.0, -40.0), 100.0, T0 + 1149)
    assert _active_zone(hass, inside) is None  # no zone loaded yet
    coord, after = await _a_then(hass, freezer, inside)

    assert _active_zone(hass, inside) == ZONE_ID
    assert after["last_seen"] == T0 + 1149
    assert _hold_store(coord) == {}
    assert coord.stat_counts["outlier_holds"] == 0


@pytest.mark.asyncio
async def test_accuracy_gate_rejection_still_belongs_to_216(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A fix #216 rejects is rejected by #216, not held.

    5,000 m away with 1,600 m: outside the zone, at most ten radii away and
    four times coarser than A, so the hold would take it if it ran first.
    """
    far = _foreign((5_000.0, -60.1), 1_600.0, T0 + 600)
    coord, after = await _a_then(hass, freezer, far)

    distance = _distance_m(_place(*A_26), (far["latitude"], far["longitude"]))
    assert 1_700.0 <= distance <= 16_000.0
    assert _active_zone(hass, far) != ZONE_ID
    assert after["last_seen"] == T0
    assert coord.stat_counts["accuracy_gate_rejects"] == 1
    assert coord.get_coarse_fix("dev") is not None
    assert coord.stat_counts["outlier_holds"] == 0
    assert _hold_store(coord) == {}


@pytest.mark.asyncio
async def test_speed_gate_rejection_leaves_hold_untouched(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """With B2 held, an impossible jump is the speed gate's; the hold stays.

    50 km in 100 s against A is 500 m/s, above the 400 m/s cap; 150 m keeps it
    below #216, which would otherwise reject it first.
    """
    coord, _after = await _a_then(hass, freezer, _b2())
    held = _hold_store(coord)
    assert held["dev"]["state"] == "held"

    teleport = _foreign((50_000.0, 0.0), 150.0, T0 + 100)
    after = _push(coord, freezer, T0 + 1200, teleport)

    assert coord.stat_counts["speed_gate_rejects"] == 1
    assert after["last_seen"] == T0
    assert _hold_store(coord) == held
    assert coord.stat_counts["outlier_holds"] == 1


@pytest.mark.asyncio
async def test_held_report_neither_seeds_nor_consumes_an_anchor(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A held report has no anchor contact, in both directions."""
    # Seed: at the base B2 passed the speed gate and seeded an anchor at A.
    coord, _after = await _a_then(hass, freezer, _b2())
    assert coord._round_trip_anchors == {}

    # Seed again, with an anchor already present: it stays exactly as it was.
    await hass.async_block_till_done()
    coord = _coordinator(hass)
    _push(coord, freezer, T0 + 172, _foreign(A_26, 13.53, T0))
    lat, lon = _place(-3_000.0, 0.0)
    coord._round_trip_anchors["dev"] = {"lat": lat, "lon": lon, "ts": T0 - 10}
    before = {k: dict(v) for k, v in coord._round_trip_anchors.items()}
    _push(coord, freezer, T0 + 1172, _b2())
    assert coord._round_trip_anchors == before

    # Consume: rescue geometry. The cached fix is far from the report, the
    # report lands on a remembered anchor and is too fast for the speed gate,
    # so without the hold the round-trip recovery would accept it and burn
    # the anchor after the commit.
    coord = _coordinator(hass)
    _push(coord, freezer, T0 + 10, _foreign((0.0, 0.0), 20.0, T0))
    lat, lon = _place(1_000.0, 0.0)
    coord._round_trip_anchors["dev"] = {"lat": lat, "lon": lon, "ts": T0}
    before = {k: dict(v) for k, v in coord._round_trip_anchors.items()}
    after = _push(coord, freezer, T0 + 12, _foreign((1_000.0, 0.0), 150.0, T0 + 2))

    assert coord.stat_counts["round_trip_recoveries"] == 1
    assert after["last_seen"] == T0
    assert _hold_store(coord)["dev"]["state"] == "held"
    assert coord._round_trip_anchors == before


@pytest.mark.asyncio
async def test_repeat_of_held_report_is_swallowed_without_resolution(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """B2 delivered again stays held, is not counted again."""
    coord, _after = await _a_then(hass, freezer, _b2())
    held = _hold_store(coord)

    after = _push(coord, freezer, T0 + 1232, _b2())

    assert after["last_seen"] == T0
    assert _hold_store(coord) == held
    assert coord.stat_counts["outlier_holds"] == 1


@pytest.mark.asyncio
async def test_regressed_stamp_is_left_to_the_significance_gate(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A stamp older than the published row is dropped, not held."""
    coord, after = await _a_then(hass, freezer, _foreign(B2_26, 116.0, T0 - 100))

    assert after["last_seen"] == T0
    assert _hold_store(coord) == {}
    assert coord.stat_counts["outlier_holds"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["push", "seed", "poll", "locate"])
async def test_entry_paths_hold_alike(
    hass: HomeAssistant,
    use_real_homeassistant_modules: None,
    freezer: FrozenDateTimeFactory,
    path: str,
) -> None:
    """Every entry path that runs the fusion holds B2 the same way.

    Push and seed go through ``update_device_cache``; poll and manual locate
    call the fusion first and skip the commit on ``False`` (``polling.py``,
    ``locate.py``), which is what the direct call below does.
    """
    await _setup_zone(hass)
    coord = _coordinator(hass)
    _push(coord, freezer, T0 + 172, _foreign(A_26, 13.53, T0))
    freezer.move_to(datetime.fromtimestamp(T0 + 1172, tz=UTC))
    if path in ("push", "seed"):
        source = None if path == "push" else "seed"
        coord.update_device_cache("dev", _b2(), source=source)
    else:
        assert coord._apply_weighted_location_fusion("dev", _b2()) is False

    held = _hold_store(coord)["dev"]
    assert held["state"] == "held"
    assert held["last_seen"] == T0 + 1149
    assert held["zone"] == ZONE_ID
    assert coord._device_location_data["dev"]["last_seen"] == T0
    assert coord.stat_counts["outlier_holds"] == 1


def test_hold_counters_are_in_the_stats_table() -> None:
    """The four hold counters are declared where ``increment_stat`` looks."""
    src = inspect.getsource(GoogleFindMyCoordinator.__init__)

    for key in (
        "outlier_holds",
        "outlier_hold_confirms",
        "outlier_hold_discards",
        "outlier_hold_timeouts",
    ):
        assert f'"{key}": 0,' in src
