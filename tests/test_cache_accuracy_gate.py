# tests/test_cache_accuracy_gate.py
"""Behavioral tests for the comparative accuracy gate (#216, core of #211).

The gate lives in the clear-jump branch of ``_apply_weighted_location_fusion``,
right after the kinematic speed gate. Where the speed gate asks whether a jump
is physically *possible*, this one asks whether the incoming fix is good enough
to *displace* what is already cached.

It is a comparison, never an absolute cut-off. Its removed predecessor
(``min_accuracy_threshold``, default 100 m, removed one day after it shipped in
47a18cc5, "causing too many problems") discarded on the incoming radius alone
and froze trackers. A coarse fix still tells us the city, and without any fix we
would not even know that - so nothing is discarded unless a better, reliable and
still-fresh alternative is actually present.

Harness mirrors ``tests/test_cache_speed_gate.py``: a
``MagicMock(spec=CacheOperations)`` carrying the cached ``existing`` fix, with
``_apply_weighted_location_fusion`` invoked unbound. That suite stubs no
``ConfigEntry`` either, so ``tests/AGENTS.md``'s ``make_config_entry`` rule does
not bite here; it is used only in the one case that exercises the real option
lookup through ``resolve_stale_threshold``.

PROVENANCE OF THE NUMBERS in ``FIELD_CASES``: these are not invented. They are
the eleven fixes the planned rule would have discarded on the maintainer's own
production Home Assistant instance, extracted from the recorder database over
10.4 days and ~30k deduplicated tracker events on 2026-09-06. Collection method,
caveats and the raw rows are committed alongside them in
``docs/ACCURACY_GATE_FIELD_DATA.md``, in redacted form (stable device labels, no
absolute coordinates). Without that record these are just numbers again in six
months.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.const import (
    DEFAULT_ACCURACY_GATE_ENABLED,
    DEFAULT_STALE_THRESHOLD,
    OPT_ACCURACY_GATE_ENABLED,
    OPT_STALE_THRESHOLD,
)
from custom_components.googlefindmy.coordinator.cache import (
    MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S,
    CacheOperations,
    _haversine_distance_impl,
)
from custom_components.googlefindmy.coordinator.helpers.geo import (
    ACCURACY_BUCKET_STATS,
    ACCURACY_GATE_MIN_M,
    ACCURACY_GATE_RATIO,
    accuracy_bucket,
)
from tests.helpers import drain_loop
from tests.helpers.config_entries_stub import make_config_entry

# ~5.5 km apart: far beyond any accuracy sum used here, so the clear-jump branch
# (dist > radius_sum) executes, and slow enough over the timespans below that the
# speed gate never fires (that gate is exercised in its own suite).
HOME = (49.866000, 10.839000)
FAR = (49.916000, 10.839000)


def _now() -> float:
    """Wall-clock now. The gate compares against it, not against report deltas."""
    return time.time()


def _coord(
    existing: dict[str, Any] | None,
    *,
    gate_enabled: bool = True,
    entry: Any = None,
) -> MagicMock:
    coord = MagicMock(spec=CacheOperations)
    coord._device_location_data = {"dev": existing} if existing else {}
    coord.increment_stat = MagicMock()
    coord._speed_gate_enabled = MagicMock(return_value=True)
    coord._round_trip_confirm_enabled = MagicMock(return_value=True)
    coord._accuracy_gate_enabled = MagicMock(return_value=gate_enabled)
    # Real implementations: the gate must write the coarse fix through the
    # production code path, not through a mock that swallows it.
    coord._record_coarse_fix = lambda dev, row: CacheOperations._record_coarse_fix(
        coord, dev, row
    )
    coord.get_coarse_fix = lambda dev: CacheOperations.get_coarse_fix(coord, dev)
    coord._device_coarse_fix = {}
    if entry is not None:
        coord.config_entry = entry
    return coord


def _existing(
    *,
    age_s: float = 300.0,
    acc: float = 20.0,
    estimated: bool = False,
) -> dict[str, Any]:
    fix: dict[str, Any] = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": acc,
        "last_seen": _now() - age_s,
        "location_type": "sensor",
    }
    if estimated:
        fix["accuracy_estimated"] = True
    return fix


def _incoming(
    *,
    acc: float | None = 1600.0,
    is_own: bool = False,
    age_s: float = 0.0,
    lat: float = FAR[0],
    lon: float = FAR[1],
) -> dict[str, Any]:
    fix: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "last_seen": _now() - age_s,
        "is_own_report": is_own,
    }
    if acc is not None:
        fix["accuracy"] = acc
    return fix


def _fuse(coord: MagicMock, new_data: dict[str, Any]) -> bool:
    return CacheOperations._apply_weighted_location_fusion(coord, "dev", new_data)


def _rejects(coord: MagicMock) -> int:
    return sum(
        1
        for call in coord.increment_stat.call_args_list
        if call.args and call.args[0] == "accuracy_gate_rejects"
    )


# ------------------------------------------------- (a)-(c) nothing to lose


def test_no_existing_fix_accepts_coarse() -> None:
    """(a) Cold start: a 1600 m fix is all we have, so it is accepted."""
    coord = _coord(None)
    assert _fuse(coord, _incoming()) is True
    assert _rejects(coord) == 0


def test_stale_existing_accepts_coarse() -> None:
    """(b) Cached fix older than stale_threshold -> coarse wins (guards A-1).

    This is the exact failure mode of the removed predecessor: it discarded
    without asking whether the alternative was still worth anything, and froze
    the tracker on an ancient position.
    """
    coord = _coord(_existing(age_s=DEFAULT_STALE_THRESHOLD + 60))
    assert _fuse(coord, _incoming()) is True
    assert _rejects(coord) == 0


def test_unreliable_existing_accepts_coarse() -> None:
    """(c) Cached fix is the sanitized 200 m estimate -> coarse wins."""
    coord = _coord(_existing(acc=200.0, estimated=True))
    assert _fuse(coord, _incoming()) is True
    assert _rejects(coord) == 0


def test_existing_without_timestamp_accepts_coarse() -> None:
    """Unknown age counts as not-trustworthy, so the incoming fix wins."""
    existing = _existing()
    del existing["last_seen"]
    coord = _coord(existing)
    assert _fuse(coord, _incoming()) is True
    assert _rejects(coord) == 0


# ------------------------------------------------- (d)-(f) the comparison


def test_coarse_jump_against_fresh_precise_fix_is_rejected() -> None:
    """(d) 1600 m against a fresh 20 m fix -> rejected, counter rises once."""
    coord = _coord(_existing(acc=20.0, age_s=300))
    assert _fuse(coord, _incoming(acc=1600.0)) is False
    assert _rejects(coord) == 1


def test_below_lower_bound_is_not_rejected() -> None:
    """(e) 150 m never fires: below ACCURACY_GATE_MIN_M, whatever the ratio."""
    assert 150.0 < ACCURACY_GATE_MIN_M
    coord = _coord(_existing(acc=5.0, age_s=300))  # ratio 30, but too fine
    assert _fuse(coord, _incoming(acc=150.0)) is True
    assert _rejects(coord) == 0


def test_ratio_not_reached_is_not_rejected() -> None:
    """(f) 300 m against 200 m is only 1.5x worse -> accepted."""
    assert 300.0 < ACCURACY_GATE_RATIO * 200.0
    coord = _coord(_existing(acc=200.0, age_s=300))
    assert _fuse(coord, _incoming(acc=300.0)) is True
    assert _rejects(coord) == 0


def test_ratio_boundary() -> None:
    """Exactly at the ratio rejects; just below it does not."""
    base = 60.0
    at = ACCURACY_GATE_RATIO * base  # 240 m, above the 200 m lower bound
    assert at >= ACCURACY_GATE_MIN_M
    coord_at = _coord(_existing(acc=base, age_s=300))
    assert _fuse(coord_at, _incoming(acc=at)) is False
    coord_below = _coord(_existing(acc=base, age_s=300))
    assert _fuse(coord_below, _incoming(acc=at - 1.0)) is True


def test_lower_bound_boundary() -> None:
    """Exactly at ACCURACY_GATE_MIN_M rejects; one metre below does not."""
    coord_at = _coord(_existing(acc=1.0, age_s=300))
    assert _fuse(coord_at, _incoming(acc=ACCURACY_GATE_MIN_M)) is False
    coord_below = _coord(_existing(acc=1.0, age_s=300))
    assert _fuse(coord_below, _incoming(acc=ACCURACY_GATE_MIN_M - 1.0)) is True


# ------------------------------------------------- (g)-(j) shape of the rule


def test_own_report_is_gated_too() -> None:
    """(g) No own-report bypass, unlike the speed gate (E6).

    A phone reports ITS OWN position, not the tracker's, so an own fix with a
    1600 m radius is exactly as coarse as a foreign one. The speed gate's bypass
    is about cryptographic provenance; this gate is about physical uncertainty.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    assert _fuse(coord, _incoming(acc=1600.0, is_own=True)) is False
    assert _rejects(coord) == 1


def test_accuracy_less_fix_falls_through() -> None:
    """(h) No measured accuracy -> falls through to downstream sanitization."""
    coord = _coord(_existing(acc=20.0, age_s=300))
    assert _fuse(coord, _incoming(acc=None)) is True
    assert _rejects(coord) == 0


def test_android_zero_sentinel_falls_through() -> None:
    """(h') Android's 0.0 no-accuracy sentinel is not a gating discriminant."""
    coord = _coord(_existing(acc=20.0, age_s=300))
    assert _fuse(coord, _incoming(acc=0.0)) is True
    assert _rejects(coord) == 0


def test_gate_disabled_behaves_like_before() -> None:
    """(i) Switch off -> today's behavior, the coarse fix passes."""
    coord = _coord(_existing(acc=20.0, age_s=300), gate_enabled=False)
    assert _fuse(coord, _incoming(acc=1600.0)) is True
    assert _rejects(coord) == 0


def test_default_is_enabled() -> None:
    """The switch defaults to on (V-2: it exists for diagnosis, not for tuning)."""
    assert DEFAULT_ACCURACY_GATE_ENABLED is True


def test_resolver_reads_the_option() -> None:
    """The real resolver honours the option and its default."""
    coord = MagicMock(spec=CacheOperations)
    coord.config_entry = make_config_entry(
        entry_id="acc-gate", data={}, options={OPT_ACCURACY_GATE_ENABLED: False}
    )
    assert CacheOperations._accuracy_gate_enabled(coord) is False
    coord.config_entry = make_config_entry(entry_id="acc-gate-2", data={}, options={})
    assert CacheOperations._accuracy_gate_enabled(coord) is True


def test_stale_threshold_option_is_honoured() -> None:
    """A shortened stale_threshold makes the cached fix stale -> coarse wins.

    Uses the real option lookup (make_config_entry) rather than the default, so
    the age comparison is bound to the shared ``stale_threshold`` option and not
    to a second, private time constant (E3).
    """
    entry = make_config_entry(
        entry_id="acc-gate-stale", data={}, options={OPT_STALE_THRESHOLD: 120}
    )
    coord = _coord(_existing(acc=20.0, age_s=300), entry=entry)
    assert _fuse(coord, _incoming(acc=1600.0)) is True
    assert _rejects(coord) == 0


def test_no_round_trip_anchor_contact() -> None:
    """(j) The gate neither seeds nor consumes a round-trip anchor (E7).

    Two levels, both asserted. The store must be untouched (that is the effect),
    AND the rejected payload must carry no deferred anchor intent (that is the
    data). The second half is what a first implementation got wrong: the speed
    gate's accept path pencils ``_round_trip_anchor_seed`` onto the payload just
    before this gate rejects it. It has no effect today, because the caller
    aborts on a False return and the marker is only applied after a commit - but
    a rejected payload carrying a live anchor intent is a trap for whoever
    touches that ordering next.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    coord._round_trip_anchors = {}
    payload = _incoming(acc=1600.0)
    assert _fuse(coord, payload) is False
    assert coord._round_trip_anchors == {}
    assert "_round_trip_anchor_seed" not in payload
    assert "_round_trip_anchor_consume" not in payload
    assert "_supersede_round_trip_anchor" not in payload


def test_no_supersede_marker_on_rejected_own_report() -> None:
    """An own clear jump this gate rejects must not invalidate a live anchor.

    ``_supersede_round_trip_anchor`` is set for every own clear jump before this
    gate runs. If the gate rejects that jump, no relocation happened, so the
    existing anchor stays valid.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    payload = _incoming(acc=1600.0, is_own=True)
    assert _fuse(coord, payload) is False
    assert "_supersede_round_trip_anchor" not in payload


def test_coarse_fix_is_recorded_before_the_drop() -> None:
    """The discarded fix survives as side information (E9, feeds AP5).

    The write must happen before ``return False``: the caller aborts the whole
    payload on a False return, so a write placed after it would never run.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    assert _fuse(coord, _incoming(acc=1600.0)) is False
    coarse = coord.get_coarse_fix("dev")
    assert coarse is not None
    assert coarse["accuracy"] == 1600.0
    assert coarse["latitude"] == FAR[0]
    assert coarse["longitude"] == FAR[1]


def test_no_coarse_fix_recorded_on_accept() -> None:
    """An accepted fix leaves no coarse residue."""
    coord = _coord(_existing(acc=200.0, age_s=300))
    assert _fuse(coord, _incoming(acc=300.0)) is True
    assert coord.get_coarse_fix("dev") is None


def test_published_position_is_untouched_on_reject() -> None:
    """A rejected payload never rewrites the cached coordinates."""
    existing = _existing(acc=20.0, age_s=300)
    coord = _coord(existing)
    assert _fuse(coord, _incoming(acc=1600.0)) is False
    assert coord._device_location_data["dev"]["latitude"] == HOME[0]
    assert coord._device_location_data["dev"]["accuracy"] == 20.0


# ------------------------------------------------- (k) real field regressions

# (new_acc, existing_acc, age_s, jump_m, label). Measured on the maintainer's
# production instance over 10.4 days; see the provenance note in the module
# docstring. Every one of these was published as a position before this gate.
FIELD_CASES = [
    # The narrowest case in the whole dataset: ratio 4.3. If this one stops
    # failing, the factor has been chosen too large - factor 6 would let the
    # single worst incident (an 18.3 km jump) through.
    (404.9, 94.5, 343, 18294, "narrowest ratio 4.3, 18.3 km jump"),
    # The zone false alarm: 458.8 m radius, 345.9 m from a 32 m home zone, so
    # Home Assistant computed 345.9 - 32 = 313.9 < 458.8 and published "home".
    (458.8, 25.8, 345, 5542, "zone false alarm not_home -> home"),
    (208.8, 5.5, 972, 4782, "208 m vs 5.5 m"),
    (319.0, 3.0, 687, 1856, "319 m vs 3 m"),
    (331.8, 32.2, 918, 1126, "331 m vs 32 m"),
    (456.3, 18.3, 403, 1113, "456 m vs 18 m"),
    (202.4, 3.8, 342, 750, "202 m, just above the lower bound"),
    (273.7, 8.9, 2734, 413, "273 m vs 8.9 m"),
    (204.5, 10.1, 902, 402, "204 m vs 10 m"),
    (281.1, 62.5, 1521, 399, "281 m vs 62.5 m, ratio 4.5"),
    (210.8, 15.8, 2851, 281, "210 m vs 15.8 m"),
]


@pytest.mark.parametrize(
    ("new_acc", "existing_acc", "age_s", "jump_m", "label"),
    FIELD_CASES,
    ids=[case[4] for case in FIELD_CASES],
)
def test_measured_field_cases_are_rejected(
    new_acc: float, existing_acc: float, age_s: int, jump_m: int, label: str
) -> None:
    """Every fix the rule discarded on real data must still be discarded."""
    lat_offset = jump_m / 111_320.0  # metres -> degrees of latitude
    coord = _coord(_existing(acc=existing_acc, age_s=age_s))
    payload = _incoming(acc=new_acc, lat=HOME[0] + lat_offset, lon=HOME[1])
    # Guard the fixture itself: a shrunken jump would silently land in the
    # overlapping-circles branch and never reach the gate.
    assert (
        _haversine_distance_impl(HOME[0], HOME[1], payload["latitude"], HOME[1])
        > existing_acc + new_acc
    ), f"fixture {label} no longer produces a clear jump"
    assert _fuse(coord, payload) is False, label
    assert _rejects(coord) == 1


def test_measured_owner_report_is_not_rejected() -> None:
    """The counter-case: the worst owner report measured (176.5 m) survives.

    No owner report in the whole 10.4-day dataset exceeded this value, which is
    why the 200 m lower bound discards none of them - and why the predecessor's
    100 m default would have discarded seven.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    assert _fuse(coord, _incoming(acc=176.5, is_own=True)) is True
    assert _rejects(coord) == 0


# ------------------------------------------------- AP4a: the distribution

# Counting how often the gate fired does not tell us whether 200 m and factor 4
# sit in the right place. Only the distribution of the REPORTED accuracy does,
# and it has to be collected before the gate - otherwise it never sees the very
# fixes that were discarded.


def _cache_coord() -> MagicMock:
    """A coordinator double for the ``update_device_cache`` write path."""
    coord = MagicMock(spec=CacheOperations)
    coord._device_location_data = {}
    coord.increment_stat = MagicMock()
    coord._speed_gate_enabled = MagicMock(return_value=True)
    coord._round_trip_confirm_enabled = MagicMock(return_value=True)
    coord._accuracy_gate_enabled = MagicMock(return_value=True)
    coord._record_coarse_fix = lambda dev, row: CacheOperations._record_coarse_fix(
        coord, dev, row
    )
    coord.count_accuracy_class = lambda row: CacheOperations.count_accuracy_class(
        coord, row
    )
    # Route the fusion through the real implementation: with the plain spec mock
    # the gate would never run and this suite would measure nothing.
    coord._apply_weighted_location_fusion = lambda dev, data: (
        CacheOperations._apply_weighted_location_fusion(coord, dev, data)
    )
    coord._device_coarse_fix = {}
    return coord


def _bucket_calls(coord: MagicMock) -> list[str]:
    keys = set(ACCURACY_BUCKET_STATS.values())
    return [
        call.args[0]
        for call in coord.increment_stat.call_args_list
        if call.args and call.args[0] in keys
    ]


@pytest.mark.parametrize(
    ("accuracy", "expected_class"),
    [
        (5.0, "<10"),
        (25.0, "10-50"),
        (120.0, "50-200"),
        (300.0, "200-500"),
        (1600.0, "500-2000"),  # the radius reported in BSkando#216
        (5000.0, ">2000"),
    ],
    ids=["<10", "10-50", "50-200", "200-500", "500-2000", ">2000"],
)
def test_accuracy_class_counted_once(accuracy: float, expected_class: str) -> None:
    """(k) One fix raises exactly the counter of its class, including the three new ones."""
    coord = _cache_coord()
    CacheOperations.update_device_cache(
        coord, "dev", {"latitude": HOME[0], "longitude": HOME[1], "accuracy": accuracy}
    )
    assert _bucket_calls(coord) == [ACCURACY_BUCKET_STATS[expected_class]]


def test_accuracy_less_fix_is_not_counted() -> None:
    """A fix without measured accuracy raises no class counter.

    Otherwise the 200 m fallback the downstream sanitization substitutes would
    skew the distribution towards exactly the class this gate acts on. Those
    cases are counted by ``accuracy_sanitized_count`` instead.
    """
    coord = _cache_coord()
    CacheOperations.update_device_cache(
        coord, "dev", {"latitude": HOME[0], "longitude": HOME[1]}
    )
    assert _bucket_calls(coord) == []


def test_distribution_sees_more_than_the_rejects() -> None:
    """(l) The counting sits BEFORE the gate, so it also sees discarded fixes.

    Discriminating assertion: were the counting (wrongly) placed inside the gate,
    the class total would EQUAL the reject count instead of exceeding it. Two
    fixes go in, only the second is discarded - so two classes are counted
    against one reject.
    """
    coord = _cache_coord()
    coord._device_location_data = {"dev": _existing(acc=20.0, age_s=300)}
    # 1) a fine fix close by: accepted, counted
    CacheOperations.update_device_cache(
        coord,
        "dev",
        {
            "latitude": HOME[0],
            "longitude": HOME[1],
            "accuracy": 25.0,
            "last_seen": _now(),
        },
    )
    # Re-seed the cached fix. The commit path further down ``update_device_cache``
    # runs against mocked helpers here and leaves the slot empty, which is a
    # limit of this harness, not production behaviour - without re-seeding the
    # second call would find no baseline and the gate could not fire at all.
    coord._device_location_data["dev"] = _existing(acc=20.0, age_s=300)
    # 2) the coarse jump: discarded by the gate, counted all the same
    CacheOperations.update_device_cache(
        coord,
        "dev",
        {
            "latitude": FAR[0],
            "longitude": FAR[1],
            "accuracy": 1600.0,
            "last_seen": _now(),
        },
    )
    rejects = _rejects(coord)
    counted = _bucket_calls(coord)
    assert rejects == 1
    assert len(counted) == 2
    assert len(counted) > rejects
    # The discarded fix in particular must appear in the distribution.
    assert ACCURACY_BUCKET_STATS["500-2000"] in counted


def test_classes_and_counters_are_in_sync() -> None:
    """(m) Every class ``accuracy_bucket`` can produce owns a counter, and vice versa.

    A class added later without a counter would fail silently: ``increment_stat``
    only logs a warning for an unknown key, so the fix would simply not be
    counted and the distribution would quietly go wrong.
    """
    produced = {
        accuracy_bucket(v)
        for v in (0, 5, 9.99, 10, 49.9, 50, 199.9, 200, 499.9, 500, 1999.9, 2000, 1e9)
    }
    produced.discard(None)
    assert produced == set(ACCURACY_BUCKET_STATS)
    assert len(set(ACCURACY_BUCKET_STATS.values())) == len(ACCURACY_BUCKET_STATS)


def test_counter_keys_are_initialized_from_the_single_table() -> None:
    """The init derives the counters from the table instead of relisting them.

    A source-level check, and named as such: it cannot prove the values, only
    that no second hand-written list exists. The behavioural half is the restore
    test below.
    """
    import inspect

    from custom_components.googlefindmy.coordinator.main import GoogleFindMyCoordinator

    src = inspect.getsource(GoogleFindMyCoordinator.__init__)
    assert "ACCURACY_BUCKET_STATS" in src
    for key in ACCURACY_BUCKET_STATS.values():
        assert f'"{key}"' not in src, f"{key} is listed a second time by hand"


@pytest.mark.asyncio
async def test_class_counters_survive_a_restart() -> None:
    """The counters rehydrate from the persisted stats cache after a restart.

    ``_async_load_stats`` iterates ``self.stats.keys()``, so a key absent from
    the init surface would be dropped on every restart and the distribution
    would never accumulate beyond one session. Mirrors
    ``tests/test_coordinator_canonicless_stats.py``.
    """
    from unittest.mock import AsyncMock

    from tests.helpers.main_coordinator_stub import MainCoordinatorStub

    persisted = {key: 42 for key in ACCURACY_BUCKET_STATS.values()}

    async def _cached(key: str) -> dict[str, int] | None:
        return persisted if key == "integration_stats" else None

    coord = MainCoordinatorStub(
        config_entry=make_config_entry(entry_id="acc-gate-restore")
    )
    coord.stats = dict.fromkeys(ACCURACY_BUCKET_STATS.values(), 0)
    coord._stats_save_task = None
    coord._cache.async_get_cached_value = AsyncMock(side_effect=_cached)

    await coord._async_load_stats()

    for key in ACCURACY_BUCKET_STATS.values():
        assert coord.stats[key] == 42


# ------------------------------------------------- AP5: the coarse fix stays visible

# Discarding the coarse fix as a POSITION is the point; discarding its
# information is not. A wide fix still names the city, and without any fix we
# would not even know that. It therefore surfaces as ``coarse_*`` attributes,
# never as latitude/longitude - so Home Assistant's zone logic cannot see it.


def _tracker_entity(coarse: dict[str, Any] | None, display: dict[str, Any]):
    from types import SimpleNamespace

    from custom_components.googlefindmy import device_tracker as dt

    coordinator = SimpleNamespace(
        config_entry=make_config_entry(entry_id="coarse-entry", data={}, options={}),
        get_coarse_fix=lambda dev: dict(coarse) if coarse else None,
        get_display_location_data_for_subentry=lambda key, dev: display,
    )
    entity = dt.GoogleFindMyDeviceTracker.__new__(dt.GoogleFindMyDeviceTracker)
    entity.coordinator = coordinator
    # ``device_id`` and ``subentry_key`` are read-only properties backed by
    # these two attributes.
    entity._device = {"id": "dev", "name": "Tracker"}
    entity._subentry_key = "core_tracking"
    entity._attr_extra_state_attributes = {}
    entity._select_display_row = lambda: display
    entity._is_location_stale = lambda: False
    entity._get_location_age = lambda row=None: 60.0
    entity._get_location_status = lambda row=None: "Online"
    entity._attr_name = "Tracker"
    dt.GoogleFindMyDeviceTracker._sync_location_attrs(entity)
    return entity


def test_coarse_fix_surfaces_as_side_information() -> None:
    """The discarded fix is readable, and it is NOT the published position."""
    display = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": 20.0,
        "last_seen": _now() - 60,
    }
    coarse = {
        "latitude": FAR[0],
        "longitude": FAR[1],
        "accuracy": 1600.0,
        "last_seen": _now() - 30,
    }
    entity = _tracker_entity(coarse, display)
    attrs = entity._attr_extra_state_attributes

    assert attrs["coarse_latitude"] == FAR[0]
    assert attrs["coarse_longitude"] == FAR[1]
    assert attrs["coarse_accuracy"] == 1600.0
    assert "coarse_last_seen" in attrs

    # The entity itself stays on the better position: this is what stops the
    # zone false alarm, because HA reads latitude/longitude/location_accuracy,
    # never the coarse_* keys.
    assert entity._attr_latitude == HOME[0]
    assert entity._attr_longitude == HOME[1]
    assert entity._attr_location_accuracy == 20.0
    assert attrs.get("latitude") != FAR[0]


def test_no_coarse_attributes_without_a_discarded_fix() -> None:
    """No gate rejects, no coarse_* keys - the normal case adds nothing."""
    display = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": 20.0,
        "last_seen": _now() - 60,
    }
    entity = _tracker_entity(None, display)
    assert not [
        k for k in entity._attr_extra_state_attributes if k.startswith("coarse")
    ]


def test_tracker_tolerates_a_coordinator_without_the_accessor() -> None:
    """An older/partial coordinator double must not break the entity."""
    from types import SimpleNamespace

    from custom_components.googlefindmy import device_tracker as dt

    display = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": 20.0,
        "last_seen": _now() - 60,
    }
    coordinator = SimpleNamespace(
        config_entry=make_config_entry(entry_id="no-accessor", data={}, options={}),
        get_display_location_data_for_subentry=lambda key, dev: display,
    )
    entity = dt.GoogleFindMyDeviceTracker.__new__(dt.GoogleFindMyDeviceTracker)
    entity.coordinator = coordinator
    # ``device_id`` and ``subentry_key`` are read-only properties backed by
    # these two attributes.
    entity._device = {"id": "dev", "name": "Tracker"}
    entity._subentry_key = "core_tracking"
    entity._attr_extra_state_attributes = {}
    entity._select_display_row = lambda: display
    entity._is_location_stale = lambda: False
    entity._get_location_age = lambda row=None: 60.0
    entity._get_location_status = lambda row=None: "Online"
    dt.GoogleFindMyDeviceTracker._sync_location_attrs(entity)
    assert entity._attr_latitude == HOME[0]


def test_diagnostics_reports_the_coarse_fix_without_coordinates() -> None:
    """The diagnostics dump carries the class and the age, never the position.

    A dump is routinely pasted into a public issue; a raw radius or a pair of
    coordinates would be re-identifying (P1-3). The coordinates stay on the
    entity, where only the user sees them.
    """
    from tests.helpers.main_coordinator_stub import MainCoordinatorStub

    coord = MainCoordinatorStub(config_entry=make_config_entry(entry_id="coarse-diag"))
    coord.data = [
        {
            "device_id": "dev",
            "accuracy": 20.0,
            "last_seen": _now() - 60,
            "is_own_report": False,
        }
    ]
    coord._device_location_data = {"dev": {"device_type": 1}}
    coord._present_last_seen = {}
    coord._device_coarse_fix = {
        "dev": {
            "latitude": FAR[0],
            "longitude": FAR[1],
            "accuracy": 1600.0,
            "last_seen": _now() - 120,
        }
    }

    entries = coord.build_per_device_diagnostics()

    assert len(entries) == 1
    entry = entries[0]
    assert entry["coarse_fix_accuracy_bucket"] == "500-2000"
    assert entry["coarse_fix_age_s"] == pytest.approx(120, abs=15)
    flat = repr(entry)
    assert str(FAR[0]) not in flat
    assert "1600" not in flat


# ------------------------------------------------- review findings, pinned

# Every case below exists because an independent diff review found the gap. They
# are the cheapest part of this change and the part most likely to save the next
# person, so each one names what it would catch.


def test_every_fusion_caller_also_counts_the_accuracy_class() -> None:
    """Structural guard: no entry point may feed the fusion without counting.

    The first implementation put the tally inside ``update_device_cache`` only.
    Two of the three entry points (the poll loop and the manual locate) run the
    fusion THEMSELVES and abort on a rejection before ever reaching that call,
    so the distribution silently lost exactly the coarse fixes it exists to
    measure - while the test suite stayed green, because it only rebuilt the
    third path. This guard fails when a fourth caller appears without a tally.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "custom_components" / "googlefindmy"
    callers: dict[str, int] = {}
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        # Calls, not the definition itself.
        hits = len(re.findall(r"self\._apply_weighted_location_fusion\(", text))
        if hits:
            callers[str(path.relative_to(root))] = hits

    assert callers, "no fusion caller found - the guard would be vacuous"
    for rel, _hits in callers.items():
        text = (root / rel).read_text(encoding="utf-8")
        assert "count_accuracy_class(" in text, (
            f"{rel} feeds the fusion but never counts the accuracy class; "
            "the distribution would miss every fix arriving on that path"
        )


def test_sentinel_accuracy_is_not_counted_as_the_finest_class() -> None:
    """Android's 0.0 sentinel must not be filed under ``<10``.

    ``accuracy_bucket(0.0)`` returns ``<10`` by itself - correct for the
    diagnostics view, wrong for this distribution, because the same value counts
    as accuracy-LESS everywhere else in the gate. Filing it as the finest class
    would bias the measurement towards precision, i.e. against the very thing it
    is meant to detect.
    """
    coord = _cache_coord()
    coord.count_accuracy_class({"accuracy": 0.0})
    coord.count_accuracy_class({"accuracy": -1.0})
    coord.count_accuracy_class({"accuracy": "n/a"})
    coord.count_accuracy_class({})
    assert _bucket_calls(coord) == []
    # The neighbouring valid value is counted, so the guard is not vacuous.
    coord.count_accuracy_class({"accuracy": 5.0})
    assert _bucket_calls(coord) == [ACCURACY_BUCKET_STATS["<10"]]


def test_an_already_counted_payload_is_not_counted_twice() -> None:
    """The tally hangs on ``_accuracy_counted``, not on ``_fusion_preapplied``.

    Deriving it from the fusion marker coupled "was fused" to "was counted", and
    the push path is exactly where those come apart: its accuracy is substituted
    before the coordinator sees the payload, so it must count upstream and say
    so. Both directions are pinned here, because only the second one shows the
    two markers really are independent now.
    """
    counted = _cache_coord()
    CacheOperations.update_device_cache(
        counted,
        "dev",
        {
            "latitude": HOME[0],
            "longitude": HOME[1],
            "accuracy": 25.0,
            "last_seen": _now(),
            "_fusion_preapplied": True,
            "_accuracy_counted": True,
        },
    )
    assert _bucket_calls(counted) == []

    # Fused but NOT counted: still tallied, which the old derived rule could not do.
    uncounted = _cache_coord()
    CacheOperations.update_device_cache(
        uncounted,
        "dev",
        {
            "latitude": HOME[0],
            "longitude": HOME[1],
            "accuracy": 25.0,
            "last_seen": _now(),
            "_fusion_preapplied": True,
        },
    )
    assert _bucket_calls(uncounted) == [ACCURACY_BUCKET_STATS["10-50"]]


def test_the_counted_marker_never_reaches_the_commit_stage() -> None:
    """The marker must be POPPED, not read, so it cannot travel any further.

    Asserted at the commit stage, not on the cached row and not on the payload
    that was handed in. Both of those were tried and both were vacuums: the spec
    double never writes the row, and ``update_device_cache`` works on a shallow
    COPY, so the caller's dict keeps its marker no matter what. Only the stage
    that receives the working copy can see whether the marker survived. Same
    discipline as ``_report_hint``: an internal marker must not reach entity
    state.
    """
    coord = _cache_coord()
    CacheOperations.update_device_cache(
        coord,
        "dev",
        {
            "latitude": HOME[0],
            "longitude": HOME[1],
            "accuracy": 25.0,
            "last_seen": _now(),
            "_accuracy_counted": True,
        },
    )
    # ``_merge_with_existing_cache_row`` is the last stage before the commit and
    # the first one that still receives the working copy, so what it sees is
    # what the cached row would carry.
    calls = coord._merge_with_existing_cache_row.call_args_list
    assert calls, "the commit stage was never reached - the guard would be vacuous"
    rows = [
        a
        for call in calls
        for a in list(call.args) + list(call.kwargs.values())
        if isinstance(a, dict) and "latitude" in a
    ]
    assert rows, "no payload seen at the commit stage"
    for row in rows:
        assert "_accuracy_counted" not in row


def test_overlapping_coarse_fix_never_reaches_the_gate() -> None:
    """The whole placement argument, pinned.

    The gate sits in the clear-jump branch. Where the accuracy circles overlap
    it must NOT fire, because the inverse-square fusion already reduces a coarse
    fix to near-zero influence and keeps the better accuracy - so there is no
    displacement to prevent. If this ever starts rejecting, the gate has moved
    somewhere it does not belong.
    """
    existing = _existing(acc=20.0, age_s=300)
    coord = _coord(existing)
    # Same spot, so the circles overlap by a wide margin.
    payload = _incoming(acc=1600.0, lat=HOME[0], lon=HOME[1])
    assert _fuse(coord, payload) is True
    assert _rejects(coord) == 0
    # And the fusion did not degrade the accuracy to the coarse value.
    assert payload.get("accuracy", 20.0) <= 20.0


def test_round_trip_recovery_outranks_the_accuracy_gate() -> None:
    """Declared limit: a recovered return trip is published even when coarse.

    The recovery leaves the fusion above the accuracy gate. That is deliberate -
    it has an INDEPENDENT confirmation of the place (the fix landed near a
    remembered anchor), which is exactly what the gate lacks. Pinning it here so
    the behaviour is a decision on record rather than an accident, and so a
    later change to the ordering is visible.
    """
    now = _now()
    existing = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": 20.0,
        "last_seen": now - 1,
        "location_type": "sensor",
    }
    coord = _coord(existing)
    # Anchor at the RETURN target, i.e. where the coarse fix lands.
    coord._round_trip_anchors = {"dev": {"lat": FAR[0], "lon": FAR[1], "ts": now - 60}}
    # 5.5 km in 1 s is far above the 400 m/s cap, so the speed gate would reject;
    # the return-trip hatch accepts instead, and the accuracy gate never runs.
    payload = _incoming(acc=1600.0, age_s=0.0)
    assert _fuse(coord, payload) is True
    assert _rejects(coord) == 0
    assert payload.get("_round_trip_anchor_consume") is True


def test_purge_device_drops_the_coarse_fix() -> None:
    """A deleted device must not keep leaking its last coarse position.

    ``purge_device`` already clears the location cache, the last-good fix and the
    round-trip anchor for exactly this reason. The coarse fix is a position too,
    so a device re-added under the same id would otherwise inherit a
    ``coarse_latitude`` from before its deletion.
    """
    from custom_components.googlefindmy.coordinator.main import GoogleFindMyCoordinator

    # Same bare-coordinator harness as tests/test_plus_code_last_known.py's
    # ``test_purge_device_drops_last_good`` - the sibling leak this mirrors.
    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_location_data = {}
    coord._device_last_good_location = {}
    coord._round_trip_anchors = {}
    coord._device_update_history = {}
    coord._device_interval_history = {}
    coord._device_caps = {}
    coord._locate_inflight = set()
    coord._locate_cooldown_until = {}
    coord._sound_request_uuids = {}
    coord._sound_request_timestamps = {}
    coord._device_poll_cooldown_until = {}
    coord._present_device_ids = set()
    coord._present_last_seen = {}
    coord.data = []
    coord._is_on_hass_loop = lambda: True
    coord._ensure_device_name_cache = lambda: {}
    coord._refresh_subentry_index = lambda *_a, **_k: None
    coord._store_subentry_snapshots = lambda *_a, **_k: None
    coord.async_set_updated_data = lambda *_a, **_k: None

    coord._record_coarse_fix(
        "dev-1",
        {
            "latitude": FAR[0],
            "longitude": FAR[1],
            "accuracy": 1600.0,
            "last_seen": _now(),
        },
    )
    assert coord.get_coarse_fix("dev-1") is not None

    coord.purge_device("dev-1")

    assert coord.get_coarse_fix("dev-1") is None


def test_coarse_fix_expires_with_the_stale_threshold() -> None:
    """An old coarse fix must stop naming a city that is hours out of date.

    Same rule as the gate itself uses, no second time constant. Its two
    neighbours in the module are bounded the same way: the round-trip anchor has
    a TTL, the last-good fix a retention predicate.
    """
    display = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": 20.0,
        "last_seen": _now() - 60,
    }
    fresh = {
        "latitude": FAR[0],
        "longitude": FAR[1],
        "accuracy": 1600.0,
        "last_seen": _now() - 60,
    }
    stale = dict(fresh)
    stale["last_seen"] = _now() - (DEFAULT_STALE_THRESHOLD + 600)

    assert (
        "coarse_latitude"
        in _tracker_entity(fresh, display)._attr_extra_state_attributes
    )
    assert (
        "coarse_latitude"
        not in _tracker_entity(stale, display)._attr_extra_state_attributes
    )


def test_coarse_fix_with_a_future_timestamp_is_not_retained() -> None:
    """A corrupt future stamp must not be retained as a "very fresh" coarse fix.

    A rejected payload never reaches ``_is_significant_update``, where a stamp
    more than ``MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S`` ahead is normally dropped
    (``future_ts_drop_count``). Retained here, its age against the wall clock
    would be NEGATIVE, which the freshness test reads as very fresh - so the
    entity would show an invalid coarse position until wall time caught up.
    Found by an independent review of the pushed diff, not by this suite.
    """
    from custom_components.googlefindmy.coordinator.cache import (
        MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S,
    )

    coord = _coord(_existing(acc=20.0, age_s=300))
    payload = _incoming(acc=1600.0)
    payload["last_seen"] = _now() + MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S + 3600

    # The gate does not CLAIM this drop: it hands the payload on, because only
    # ``_is_significant_update`` sorts a corrupt stamp into
    # ``future_ts_drop_count`` (pinned in tests/test_coordinator_invalid_ts_split.py).
    # Counting it here as an accuracy rejection would silence that counter and
    # inflate this one with a drop that says nothing about the thresholds.
    assert _fuse(coord, payload) is True
    assert _rejects(coord) == 0
    assert coord.get_coarse_fix("dev") is None

    # A stamp inside the tolerance is retained, so the guard is not vacuous.
    coord2 = _coord(_existing(acc=20.0, age_s=300))
    ok = _incoming(acc=1600.0)
    ok["last_seen"] = _now() + 60
    assert _fuse(coord2, ok) is False
    assert coord2.get_coarse_fix("dev") is not None


def test_reader_rejects_a_negative_coarse_age() -> None:
    """Second line: a future-stamped fix that got in anyway is not displayed."""
    display = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": 20.0,
        "last_seen": _now() - 60,
    }
    future = {
        "latitude": FAR[0],
        "longitude": FAR[1],
        "accuracy": 1600.0,
        "last_seen": _now() + 3 * 3600,
    }
    attrs = _tracker_entity(future, display)._attr_extra_state_attributes
    assert "coarse_latitude" not in attrs


def test_substitute_zone_accuracy_writes_the_value_and_the_marker() -> None:
    """The rule lives in one helper because three sites need it identically."""
    from custom_components.googlefindmy.coordinator.helpers.cache import (
        substitute_zone_accuracy,
    )

    row: dict[str, Any] = {"accuracy": 1600.0}
    substitute_zone_accuracy(row, 400.0)
    assert row["accuracy"] == 400.0
    assert row["_accuracy_substituted"] is True


def test_the_push_fallback_write_strips_the_marker() -> None:
    """A coordinator without update_device_cache still must not cache it.

    ``_write_coordinator_payload`` falls back to writing ``_device_location_data``
    directly when the coordinator has no ``update_device_cache``. That path skips
    every marker pop the normal path performs, so the transient marker would end
    up in the cached row - and from there in the entity attributes.
    """
    from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA

    class _Bare:
        def __init__(self) -> None:
            self._device_location_data: dict[str, Any] = {}

    from types import SimpleNamespace

    class _Filter:
        @staticmethod
        def should_filter_detection(_dev: str, _name: str) -> tuple[bool, dict]:
            return False, {"latitude": HOME[0], "longitude": HOME[1], "radius": 400.0}

    receiver = FcmReceiverHA.__new__(FcmReceiverHA)
    coordinator = _Bare()
    # The payload comes from the real prepare step, so every marker that step
    # sets is in play - including ones added later.
    prep_coordinator = SimpleNamespace(
        count_accuracy_class=lambda _row: None,
        config_entry=SimpleNamespace(
            runtime_data=SimpleNamespace(google_home_filter=_Filter())
        ),
    )
    payload = FcmReceiverHA._prepare_coordinator_payload(
        receiver,
        prep_coordinator,
        ("acct", "device-id"),
        {"accuracy": 1600.0, "semantic_name": "Home", "latitude": FAR[0]},
    )
    assert payload is not None
    assert payload["_accuracy_counted"] is True
    assert payload["_accuracy_substituted"] is True

    FcmReceiverHA._write_coordinator_payload(
        receiver, coordinator, "device-id", payload
    )

    cached = coordinator._device_location_data["device-id"]
    assert cached["accuracy"] == 400.0

    # A coordinator that DOES carry the expiry surface gets it called: the
    # fallback is duck-typed, so both shapes have to be exercised, otherwise the
    # duck-typing is only asserted in the comment.
    expired: list[str] = []

    class _WithExpiry(_Bare):
        def _expire_coarse_fix(self, device_id: str, committed: Any) -> None:
            expired.append(device_id)

    FcmReceiverHA._write_coordinator_payload(
        receiver,
        _WithExpiry(),
        "device-id",
        {"accuracy": 400.0, "_accuracy_counted": True},
    )
    assert expired == ["device-id"]

    # Not "the two markers we know today": any transient key that survives here
    # becomes an entity attribute. Written as a property so the next marker is
    # covered by construction - the previous version named the keys and missed
    # ``_accuracy_counted`` when it was added one commit earlier.
    leaked = [key for key in cached if key.startswith("_")]
    assert not leaked, f"transient markers reached the cached row: {leaked}"


def test_a_substituted_home_radius_is_not_weighed_as_a_measurement() -> None:
    """The Google Home filter's decision must survive this gate.

    On all three inbound paths the filter replaces the coordinates with the home
    zone's and ``accuracy`` with that zone's RADIUS, before fusion. That number
    describes a zone, not a measurement of the device. Weighed against the
    cached precision it looks exactly like a coarse fix, so with a home zone of
    200 m or more this gate would refuse the deliberate move home and leave the
    tracker away. The substituting site marks the payload; only it can know.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    substituted = _incoming(acc=400.0)  # a 400 m home zone
    substituted["_accuracy_substituted"] = True

    assert _fuse(coord, substituted) is True
    assert _rejects(coord) == 0
    assert coord.get_coarse_fix("dev") is None

    # Non-vacuous: the identical payload without the marker IS rejected, so the
    # exemption is what lets it through, not the numbers.
    coord2 = _coord(_existing(acc=20.0, age_s=300))
    assert _fuse(coord2, _incoming(acc=400.0)) is False
    assert _rejects(coord2) == 1


def test_the_substitution_marker_never_reaches_the_cached_row() -> None:
    """A transient marker in the cache would be published as an attribute."""
    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_location_data = {
        "dev": {
            "latitude": HOME[0],
            "longitude": HOME[1],
            "accuracy": 20.0,
            "last_seen": _now() - 300,
            "status": "coordinate",
        }
    }
    coord._device_names = {}
    coord._device_update_history = {}
    coord.increment_stat = lambda *_a, **_k: None
    coord._apply_report_type_cooldown = lambda *_a, **_k: None
    coord._is_on_hass_loop = lambda: True
    coord._run_on_hass_loop = lambda *_a, **_k: None

    payload = {
        "latitude": FAR[0],
        "longitude": FAR[1],
        "accuracy": 400.0,
        "last_seen": _now(),
        "status": "coordinate",
        "_accuracy_substituted": True,
    }
    coord.update_device_cache("dev", payload)

    cached = coord._device_location_data["dev"]
    assert "_accuracy_substituted" not in cached
    # The payload did commit - otherwise the assertion above would hold for the
    # wrong reason (nothing written at all).
    assert cached["accuracy"] == 400.0


def test_the_new_mixin_declarations_refuse_to_be_used_unimplemented() -> None:
    """A declaration that silently returned None would be worse than a crash.

    ``_mixin_typing`` states what the mixins promise each other. The three
    entries this branch adds raise instead of returning a default, so a
    coordinator assembled without ``CacheOperations`` fails loudly rather than
    quietly never expiring a coarse fix and never recognising a replay.
    """
    from custom_components.googlefindmy.coordinator._mixin_typing import _MixinBase

    with pytest.raises(NotImplementedError):
        _MixinBase._expire_coarse_fix(object(), "dev", {})
    with pytest.raises(NotImplementedError):
        _MixinBase.is_replayed_report(object(), "dev", {})
    with pytest.raises(NotImplementedError):
        _MixinBase.claim_report_for_tally(object(), "dev", {})


def test_expiry_keeps_the_coarse_fix_when_the_commit_has_no_timestamp() -> None:
    """An unprovable claim must not cost the only coarse position we have.

    A committed row without a usable stamp cannot be shown to be newer than the
    retained coarse fix. Dropping it anyway would lose the city we would
    otherwise still know, which is the whole reason a coarse fix is kept.
    """
    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_coarse_fix = {
        "dev": {"latitude": FAR[0], "longitude": FAR[1], "last_seen": _now() - 300}
    }

    coord._expire_coarse_fix("dev", {"latitude": HOME[0], "longitude": HOME[1]})
    assert coord.get_coarse_fix("dev") is not None

    # Non-vacuous: with a usable stamp the same call does expire it.
    coord._expire_coarse_fix("dev", {"last_seen": _now()})
    assert coord.get_coarse_fix("dev") is None


def test_a_committed_fix_expires_the_retained_coarse_one() -> None:
    """Side information must not outlive the reason it exists.

    The coarse fix is kept for the case that nothing better exists. Once a
    position with an equal or newer stamp is published, it is obsolete by
    definition - but the reader only knows the coarse row's OWN age, so without
    a producer-side rule the tracker would show a city from the rejected report
    next to the fresh position for up to the whole stale threshold, and the
    diagnostics builder would carry it too.
    """
    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_location_data = {}
    coord._device_names = {}
    coord._device_update_history = {}
    coord._device_coarse_fix = {
        "dev": {
            "latitude": FAR[0],
            "longitude": FAR[1],
            "accuracy": 1600.0,
            "last_seen": _now() - 120,
        }
    }
    coord.increment_stat = lambda *_a, **_k: None
    coord._apply_report_type_cooldown = lambda *_a, **_k: None
    coord._is_on_hass_loop = lambda: True
    coord._run_on_hass_loop = lambda *_a, **_k: None

    # A newer precise fix commits.
    coord.update_device_cache(
        "dev",
        {
            "latitude": HOME[0],
            "longitude": HOME[1],
            "accuracy": 20.0,
            "last_seen": _now(),
            "status": "coordinate",
        },
    )
    assert coord.get_coarse_fix("dev") is None

    # Non-vacuous, and the boundary that matters: a committed fix OLDER than the
    # coarse one leaves it alone - it is still the newest thing known about that
    # direction.
    coord._device_coarse_fix["dev"] = {
        "latitude": FAR[0],
        "longitude": FAR[1],
        "accuracy": 1600.0,
        "last_seen": _now(),
    }
    coord._device_location_data = {}
    coord.update_device_cache(
        "dev",
        {
            "latitude": HOME[0],
            "longitude": HOME[1],
            "accuracy": 20.0,
            "last_seen": _now() - 600,
            "status": "coordinate",
        },
    )
    assert coord.get_coarse_fix("dev") is not None


def test_a_propagated_sibling_also_loses_its_coarse_fix() -> None:
    """Every commit site needs the rule, not just the one that was reported.

    Devices sharing an identity key receive the newer position through
    ``_propagate_location_to_shared_devices``, which writes
    ``_device_location_data`` itself instead of going through
    ``update_device_cache``. A sibling would otherwise keep showing an old city
    next to the propagated position, which is the same defect one commit site
    over. The two direct-write fallbacks (poll and push) carry the rule as well.
    """
    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_location_data = {
        "sibling": {
            "latitude": FAR[0],
            "longitude": FAR[1],
            "accuracy": 20.0,
            "last_seen": _now() - 600,
            # Hex: _normalize_identity_key runs bytes.fromhex over a string and
            # returns None for anything else, so a plain label would silently
            # abort the propagation and make this test pass for no reason.
            "identity_key": "a1b2c3",
        }
    }
    coord._identity_key_to_devices = {bytes.fromhex("a1b2c3"): {"source", "sibling"}}
    coord._device_coarse_fix = {
        "sibling": {
            "latitude": FAR[0],
            "longitude": FAR[1],
            "accuracy": 1600.0,
            "last_seen": _now() - 300,
        }
    }
    coord._device_last_good_location = {}
    coord.increment_stat = lambda *_a, **_k: None
    coord._round_trip_confirm_enabled = lambda: False

    coord._propagate_location_to_shared_devices(
        "source",
        {
            "latitude": HOME[0],
            "longitude": HOME[1],
            "accuracy": 20.0,
            "last_seen": _now(),
            "identity_key": "a1b2c3",
        },
    )

    assert coord._device_location_data["sibling"]["latitude"] == pytest.approx(
        HOME[0]
    ), "the propagation must have happened, or the assertion below is vacuous"
    assert coord.get_coarse_fix("sibling") is None


def test_a_future_dated_cached_fix_may_not_veto_anything() -> None:
    """The reference has to be trustworthy in both directions, not just one.

    The freshness test asked only whether the cached fix is too OLD. A row
    stamped in the future - clock skew on the reporting device, tolerated up to
    ``MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S`` elsewhere in this file - yields a
    NEGATIVE age and read as maximally fresh. It would then veto every coarse
    update until wall time caught up and the stale interval elapsed on top,
    pinning the tracker for hours. ``_record_coarse_fix`` already refuses a
    future stamp as an ordering authority; this is the same rule for the
    reference the gate compares against.
    """
    future_cached = _existing(acc=20.0, age_s=-1800)  # stamped 30 min ahead
    coord = _coord(future_cached)

    assert _fuse(coord, _incoming(acc=1600.0)) is True
    assert _rejects(coord) == 0

    # Non-vacuous: the same cached fix with a plausible stamp DOES veto.
    coord2 = _coord(_existing(acc=20.0, age_s=1800))
    assert _fuse(coord2, _incoming(acc=1600.0)) is False
    assert _rejects(coord2) == 1


def test_a_payload_without_a_timestamp_stays_this_gate_s_case() -> None:
    """A missing stamp is the one temporal class this gate must keep claiming.

    The gate hands the three classes ``_is_significant_update`` rejects on, so
    that gate can classify them. A missing or unparseable ``last_seen`` is NOT
    one of them: normalization returns None there and all three timestamp checks
    are skipped, so the payload is accepted. Handing it on would let a coarse fix
    through the very gate it was measured against - the distance-based merge
    could then put the coarse coordinates in place of fresh precise ones. Found
    by review of the previous commit, which had exactly that hole.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    payload = _incoming(acc=1600.0)
    payload.pop("last_seen")

    assert _fuse(coord, payload) is False
    assert _rejects(coord) == 1
    # Still not retained: no stamp means no age, and the reader needs one.
    assert coord.get_coarse_fix("dev") is None

    # The reason, pinned rather than asserted in prose: the significance gate
    # accepts a stampless payload, so nobody else would have stopped it.
    probe = MagicMock(spec=CacheOperations)
    probe._device_location_data = {}
    probe.increment_stat = MagicMock()
    stampless = {
        "latitude": FAR[0],
        "longitude": FAR[1],
        "accuracy": 1600.0,
    }
    assert CacheOperations._is_significant_update(probe, "dev", stampless) is True


def test_record_coarse_fix_refuses_an_implausible_stamp_on_its_own() -> None:
    """The retention guard must hold even though the gate no longer feeds it.

    Since the gate hands temporally invalid payloads on instead of claiming
    them, ``_record_coarse_fix`` is no longer reached with such a stamp through
    the fusion path - measured: removing its guard broke no test. That makes the
    guard unreachable, not unnecessary: it is the second caller's protection,
    and an unguarded invariant is the one that rots. Exercised directly here,
    for both bounds and for the published-row order.
    """
    now = _now()

    for label, stamp in (
        ("future", now + MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S + 3600),
        ("pre-Y2K", 100_000.0),
        ("missing", None),
    ):
        coord = _coord(_existing(acc=20.0, age_s=300))
        row = _incoming(acc=1600.0)
        if stamp is None:
            row.pop("last_seen")
        else:
            row["last_seen"] = stamp
        coord._record_coarse_fix("dev", row)
        assert coord.get_coarse_fix("dev") is None, label

    # Older than the published row: same refusal, different reason.
    coord = _coord(_existing(acc=20.0, age_s=60))
    delayed = _incoming(acc=1600.0)
    delayed["last_seen"] = now - 600
    coord._record_coarse_fix("dev", delayed)
    assert coord.get_coarse_fix("dev") is None

    # Non-vacuous: a plausible stamp IS retained by the same call.
    coord = _coord(_existing(acc=20.0, age_s=300))
    coord._record_coarse_fix("dev", _incoming(acc=1600.0))
    assert coord.get_coarse_fix("dev") is not None


def test_pre_y2k_timestamp_is_not_retained() -> None:
    """A corrupt pre-Y2K stamp must not become a coarse fix either.

    Same *source* as the future-stamp guard - the rejected payload never reaches
    ``_is_significant_update``, so both of its bounds have to be restated here -
    but a different damage. A future stamp reads as negative age, i.e. as
    extremely fresh; a 1970 stamp reads as roughly fifty-five years old, so the
    reader would discard it anyway. What this guard buys is that the store and
    the diagnostics never carry a row that is worthless by construction. Bounded
    by the same ``_Y2K_EPOCH_SECONDS`` constant, so "plausible" has one answer.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    payload = _incoming(acc=1600.0)
    payload["last_seen"] = 100_000.0  # 1970
    # Same division of labour as the future-stamp case: not retained, and not
    # booked as an accuracy rejection either - ``_is_significant_update`` drops
    # it into ``invalid_ts_drop_warn``.
    assert _fuse(coord, payload) is True
    assert _rejects(coord) == 0
    assert coord.get_coarse_fix("dev") is None

    # Non-vacuous: a plausible stamp of the same payload IS claimed and counted.
    coord2 = _coord(_existing(acc=20.0, age_s=300))
    assert _fuse(coord2, _incoming(acc=1600.0)) is False
    assert _rejects(coord2) == 1


def test_coarse_fix_never_moves_backwards_in_time() -> None:
    """An out-of-order report must not replace a newer coarse fix.

    ``_is_significant_update`` enforces forward order for the PUBLISHED row, but
    a payload this gate rejects never gets there. Without this guard a delayed
    crowd report would overwrite more recent side information with older data.
    """
    now = _now()
    coord = _coord(_existing(acc=20.0, age_s=300))

    recent = _incoming(acc=1600.0)
    recent["last_seen"] = now
    assert _fuse(coord, recent) is False
    kept = coord.get_coarse_fix("dev")
    assert kept is not None

    # Older than the retained one, but still NEWER than the published row, so
    # this case measures the store ordering and not the published-row guard.
    delayed = _incoming(acc=1600.0, lat=FAR[0] + 0.01)
    delayed["last_seen"] = now - 200
    assert _fuse(coord, delayed) is False
    still = coord.get_coarse_fix("dev")
    assert still is not None
    assert still["last_seen"] == kept["last_seen"]
    assert still["latitude"] == kept["latitude"]

    # A NEWER one does replace it, so the guard is not simply freezing the slot.
    newer = _incoming(acc=1600.0, lat=FAR[0] + 0.02)
    newer["last_seen"] = now + 60
    assert _fuse(coord, newer) is False
    assert coord.get_coarse_fix("dev")["latitude"] == FAR[0] + 0.02


def test_gate_stops_rejecting_once_the_cached_fix_goes_stale() -> None:
    """The self-healing property, pinned at its boundary.

    This is what separates the gate from the filter that was removed in 2025:
    it cannot freeze a tracker, because the moment the cached fix crosses
    ``stale_threshold`` the gate stops rejecting and the coarse fix wins. Any
    change that keeps the cached timestamp fresh across a rejection would
    destroy exactly this property - the threshold would never be reached.
    """
    payload = lambda: _incoming(acc=1600.0)  # noqa: E731
    assert _fuse(_coord(_existing(acc=20.0, age_s=300)), payload()) is False
    assert (
        _fuse(_coord(_existing(acc=20.0, age_s=DEFAULT_STALE_THRESHOLD - 1)), payload())
        is False
    )
    assert (
        _fuse(_coord(_existing(acc=20.0, age_s=DEFAULT_STALE_THRESHOLD + 1)), payload())
        is True
    )


# ------------------------------------------------- defensive branches, pinned

# Codecov flagged one uncovered line and five half-taken branches in this
# change. Each is a defensive guard, i.e. a branch that today's callers cannot
# reach - which is exactly the kind that rots unnoticed. They are pinned here
# rather than excluded, because "unreachable" is a claim about the callers, and
# the callers change.


def test_the_mixin_declaration_stays_a_declaration() -> None:
    """``_MixinBase.count_accuracy_class`` must raise, not silently do nothing.

    The stub exists so mypy can type the two mixins that call the method across
    class boundaries; the implementation lives in ``CacheOperations``. If a
    future edit gave the stub a body, an MRO mistake would stop being loud and
    the tally would vanish without a failure anywhere.
    """
    from custom_components.googlefindmy.coordinator._mixin_typing import _MixinBase

    with pytest.raises(NotImplementedError):
        _MixinBase.count_accuracy_class(object(), {"accuracy": 5.0})


def test_an_unmapped_bucket_counts_nothing_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A class without a counter must be dropped, never crash the write path.

    ``count_accuracy_class`` looks its key up in ``ACCURACY_BUCKET_STATS``. The
    two tables are kept in one module precisely so they cannot drift, but the
    lookup is inside the cache commit path: if they ever did drift, a ``None``
    bucket must fall through quietly rather than take a location update down
    with it. Forced here because no real accuracy value reaches this branch.
    """
    from custom_components.googlefindmy.coordinator import cache as cache_mod

    coord = _cache_coord()
    monkeypatch.setattr(cache_mod, "_accuracy_bucket_impl", lambda _v: None)
    coord.count_accuracy_class({"accuracy": 5.0})
    assert _bucket_calls(coord) == []


def test_diagnostics_omits_the_coarse_age_when_the_stamp_is_unusable() -> None:
    """A coarse fix without a numeric stamp yields ``None``, not a wrong age.

    The producer refuses to retain such a fix, so this is the reader's second
    line. It matters because ``coarse_fix_age_s`` is read by humans triaging an
    issue: a fabricated ``0`` would read as "just now".
    """
    from tests.helpers.main_coordinator_stub import MainCoordinatorStub

    coord = MainCoordinatorStub(config_entry=make_config_entry(entry_id="coarse-nots"))
    coord.data = [
        {
            "device_id": "dev",
            "accuracy": 20.0,
            "last_seen": _now() - 60,
            "is_own_report": False,
        }
    ]
    coord._device_location_data = {"dev": {"device_type": 1}}
    coord._present_last_seen = {}
    coord._device_coarse_fix = {
        "dev": {
            "latitude": FAR[0],
            "longitude": FAR[1],
            "accuracy": 1600.0,
            "last_seen": "not-a-number",
        }
    }

    entry = coord.build_per_device_diagnostics()[0]
    assert entry["coarse_fix_accuracy_bucket"] == "500-2000"
    assert entry["coarse_fix_age_s"] is None


def test_diagnostics_omits_the_age_of_a_future_dated_stamp() -> None:
    """A stamp in the future has no age, and reporting ``0`` would invent one.

    Intake accepts a drift of two hours, so a future-dated stamp really does reach both
    the snapshot and the coarse store. The device tracker already hides a coarse fix
    whose age is negative; clamping the same age to zero here would make the diagnostics
    dump claim the hidden fix happened just now, and those are the numbers the gate is
    evaluated with. Both wall-clock fields are checked, because the clamp was the same
    expression in both places.
    """
    from tests.helpers.main_coordinator_stub import MainCoordinatorStub

    coord = MainCoordinatorStub(
        config_entry=make_config_entry(entry_id="coarse-future")
    )
    coord.data = [
        {
            "device_id": "dev",
            "accuracy": 20.0,
            "last_seen": _now() + 600,
            "is_own_report": False,
        }
    ]
    coord._device_location_data = {"dev": {"device_type": 1}}
    coord._present_last_seen = {}
    coord._device_coarse_fix = {
        "dev": {
            "latitude": FAR[0],
            "longitude": FAR[1],
            "accuracy": 1600.0,
            "last_seen": _now() + 600,
        }
    }

    entry = coord.build_per_device_diagnostics()[0]
    assert entry["coarse_fix_accuracy_bucket"] == "500-2000"
    assert entry["coarse_fix_age_s"] is None
    assert entry["last_fix_age_s"] is None


def test_reader_publishes_only_the_coarse_fields_it_actually_has() -> None:
    """A partial coarse fix must not produce half-empty attributes.

    Each of the three reader guards is taken in both directions here. Without
    this, a fix missing its coordinates would still be truthy and the entity
    would publish ``coarse_accuracy`` alone - a radius around nothing.
    """
    display = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": 20.0,
        "last_seen": _now() - 60,
    }

    # Coordinates missing: no coarse_latitude/-longitude, but the rest survives.
    attrs = _tracker_entity(
        {"accuracy": 1600.0, "last_seen": _now() - 30}, display
    )._attr_extra_state_attributes
    assert "coarse_latitude" not in attrs
    assert "coarse_longitude" not in attrs
    assert attrs["coarse_accuracy"] == 1600.0
    assert "coarse_last_seen" in attrs

    # Only one half of the pair: still nothing, a lone coordinate is not a place.
    attrs = _tracker_entity(
        {"latitude": FAR[0], "accuracy": 1600.0, "last_seen": _now() - 30}, display
    )._attr_extra_state_attributes
    assert "coarse_latitude" not in attrs

    # Accuracy missing: the position is still worth showing.
    attrs = _tracker_entity(
        {"latitude": FAR[0], "longitude": FAR[1], "last_seen": _now() - 30}, display
    )._attr_extra_state_attributes
    assert attrs["coarse_latitude"] == FAR[0]
    assert "coarse_accuracy" not in attrs


def test_a_millisecond_stamp_is_stored_in_seconds() -> None:
    """The store must hold the normalized stamp, not the raw field.

    ``_record_coarse_fix`` judges the NORMALIZED value (millisecond-tolerant),
    but every reader computes the age with ``location_age_seconds``, which does
    a plain ``float()``. Storing the raw field would let a millisecond stamp
    pass the guard and then read as decades in the future: the entity would hide
    the fix entirely (negative age) and the diagnostics would clamp the age to
    ``0``, i.e. claim "just now" for a stamp they could not interpret.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))
    payload = _incoming(acc=1600.0)
    payload["last_seen"] = _now() * 1000.0  # milliseconds, as the seed path sends

    assert _fuse(coord, payload) is False
    stored = coord.get_coarse_fix("dev")
    assert stored is not None
    # Seconds, not milliseconds: an age a human would recognise, not ~55 years.
    assert abs(_now() - float(stored["last_seen"])) < 60


def test_a_future_stamp_does_not_block_later_coarse_fixes() -> None:
    """A retained future stamp must not win the ordering test against reality.

    The plausibility bound tolerates clock skew up to
    ``MAX_ACCEPTED_LOCATION_FUTURE_DRIFT_S``, so a stamp an hour ahead is
    retained. The reader discards it (negative age), so it is invisible - and
    without this carve-out it would still outrank every real fix for that whole
    hour, suppressing the coarse attributes exactly when they are wanted.
    """
    coord = _coord(_existing(acc=20.0, age_s=300))

    ahead = _incoming(acc=1600.0, lat=FAR[0] + 0.03)
    ahead["last_seen"] = _now() + 3600
    assert _fuse(coord, ahead) is False
    assert coord.get_coarse_fix("dev")["latitude"] == FAR[0] + 0.03

    honest = _incoming(acc=1600.0, lat=FAR[0] + 0.04)
    assert _fuse(coord, honest) is False
    assert coord.get_coarse_fix("dev")["latitude"] == FAR[0] + 0.04


# ------------------------------------------------- trusted anchors (Codex #1271)

# The gate originally sat only in the clear-jump branch, which the trusted-anchor
# branch returns before ever reaching. That exempted the single best cached
# reference the gate exists to protect. The placement rationale F3, which keeps
# the SPEED gate out of that branch, does not transfer: it rests on a semantic
# anchor having no kinematics, and this gate compares radii, not motion.


def _trusted(*, age_s: float = 300.0, acc: float = 50.0) -> dict[str, Any]:
    """A cached trusted anchor, i.e. a user-configured semantic location.

    ``50.0`` is ``DEFAULT_SEMANTIC_DETECTION_RADIUS``, the soft floor such an
    anchor gets in ``coordinator/main.py``; no ``accuracy_estimated`` flag, so
    ``is_reliable_fix`` accepts it.
    """
    fix = _existing(age_s=age_s, acc=acc)
    fix["location_type"] = "trusted"
    return fix


def test_gate_also_guards_a_fresh_trusted_anchor() -> None:
    """A 1600 m fix must not roll a fresh 50 m semantic anchor off its place.

    This is the false zone transition the whole change is about, in its worst
    form: the anchor is the most trustworthy position the integration has.
    """
    coord = _coord(_trusted())
    assert _fuse(coord, _incoming(acc=1600.0)) is False
    assert _rejects(coord) == 1
    assert coord.get_coarse_fix("dev") is not None


def test_trusted_anchor_snap_back_is_untouched() -> None:
    """An OVERLAPPING fix still snaps to the anchor and commits - unchanged.

    The gate must not reach into the overlap case: there the anchor wins by
    snap-back, not by rejection, and the payload has to commit so the device
    keeps reporting.
    """
    coord = _coord(_trusted(acc=200.0))
    # ~110 m from the anchor, so the circles overlap and this is not a jump.
    near = _incoming(acc=1600.0, lat=HOME[0] + 0.001, lon=HOME[1])
    assert _fuse(coord, near) is True
    assert _rejects(coord) == 0
    assert near["latitude"] == HOME[0]
    assert near["status"] == "Stationary (at Anchor)"


def test_a_stale_trusted_anchor_releases_the_coarse_fix() -> None:
    """Once the anchor ages out, the coarse fix wins - no freeze at an anchor.

    Same release condition as everywhere else in this gate. Without it the
    tracker could never leave a trusted anchor while only coarse fixes arrive,
    which would be the predecessor's failure mode with extra steps.
    """
    coord = _coord(_trusted(age_s=DEFAULT_STALE_THRESHOLD + 1))
    assert _fuse(coord, _incoming(acc=1600.0)) is True
    assert _rejects(coord) == 0


def test_a_precise_fix_still_leaves_a_trusted_anchor() -> None:
    """A good fix far from the anchor is published - the device did move.

    The gate is comparative: it only blocks a fix that is much WORSE than the
    anchor. A 20 m fix five kilometres away is not, so the tracker follows it.
    """
    coord = _coord(_trusted())
    assert _fuse(coord, _incoming(acc=20.0)) is True
    assert _rejects(coord) == 0


def test_a_report_older_than_the_published_row_is_not_retained() -> None:
    """A delayed coarse report must not pose as current side information.

    The store ordering next door only compares with an EARLIER coarse fix, so on
    the very first rejection there is nothing to compare against. Without this
    guard a report predating the published precise position would be shown as
    the current city until it aged out - and ``_is_significant_update``, which
    would have dropped it, is exactly what a rejected payload never reaches.
    """
    coord = _coord(_existing(acc=20.0, age_s=60))
    delayed = _incoming(acc=1600.0)
    delayed["last_seen"] = _now() - 600  # older than the published row

    assert _fuse(coord, delayed) is True
    # Not retained, and not booked: a regressed stamp is the significance
    # gate's case (``invalid_ts_drop_benign``), not this gate's.
    assert _rejects(coord) == 0
    assert coord.get_coarse_fix("dev") is None

    # A report NEWER than the published row is retained, so the guard is not
    # simply refusing everything.
    fresh = _incoming(acc=1600.0)
    assert _fuse(coord, fresh) is False
    assert coord.get_coarse_fix("dev") is not None


def test_the_tally_runs_before_any_accuracy_substitution() -> None:
    """Structural guard: nothing may rewrite ``accuracy`` before it is counted.

    Three sites overwrite the reported accuracy on the way in - the semantic
    mapping (an anchor radius), the Google-Home filter and the semantic-only
    preserve (both reuse the CACHED value via ``carry_reused_accuracy``).
    Counted after any of them, a response that reports no accuracy of its own
    enters the distribution as a freshly reported measurement and pulls it
    towards whatever happened to be cached - and this distribution is what the
    gate's thresholds will later be re-tuned against.

    A source-order check, and named as such: it cannot prove the values, only
    that no substitution precedes the tally in the two files that own an entry
    point. The behavioural half is
    ``test_sentinel_accuracy_is_not_counted_as_the_finest_class``.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "custom_components" / "googlefindmy"
    substitutions = (
        "carry_reused_accuracy(",
        "_apply_semantic_mapping(",
        '["accuracy"] = radius',
    )
    checked = 0
    for rel in (
        "coordinator/polling.py",
        "coordinator/locate.py",
        "Auth/fcm_receiver_ha.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        # Not "count_accuracy_class(": the push path resolves it through
        # ``getattr(coordinator, "count_accuracy_class", None)``, so the bare
        # name is the only spelling all three sites share.
        tally = text.index("count_accuracy_class")
        for needle in substitutions:
            first = text.find(needle)
            if first == -1:
                continue
            checked += 1
            assert tally < first, (
                f"{rel}: {needle} runs at {first} before the tally at {tally}; "
                "the distribution would count a substituted accuracy as reported"
            )
    assert checked >= 4, f"guard would be vacuous, only {checked} sites compared"


def test_every_zone_substitution_site_goes_through_the_helper() -> None:
    """Structural guard: the marker must not be forgotten at a fourth site.

    Two of the three sites sit deep inside ``_async_start_poll_cycle`` and
    ``async_locate_device``, whose success paths this suite does not drive
    (``tests/test_coordinator_locate_basics.py`` names the Google Home filter as
    out of scope), so their call lines carry no line coverage. What matters
    there is not the line but the invariant: a zone radius never reaches
    ``accuracy`` without the marker that tells the gate what it is. A raw
    assignment would silently restore the bug this change fixed.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "custom_components" / "googlefindmy"
    raw_forms = ('["accuracy"] = radius', '["accuracy"] = replacement_attrs')
    sites = 0
    for rel in (
        "coordinator/polling.py",
        "coordinator/locate.py",
        "Auth/fcm_receiver_ha.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        if "replacement_attrs" not in text:
            continue
        sites += 1
        assert "substitute_zone_accuracy(" in text, (
            f"{rel} consumes the Google Home filter's replacement but does not "
            "route the radius through substitute_zone_accuracy, so the accuracy "
            "gate would weigh a zone radius as a measurement"
        )
        for raw in raw_forms:
            assert raw not in text, (
                f"{rel} assigns the substituted radius directly ({raw}); the "
                "marker would be missing"
            )
    assert sites == 3, f"guard would be vacuous, only {sites} sites found"


def test_the_push_path_counts_before_the_filter_substitutes() -> None:
    """The push tally sees the REPORTED radius, not the filter's.

    The source-order guard above only proves the call sits ahead of the
    substitution; this proves it is reached and what it is handed. The Google
    Home filter replaces the accuracy with its configured radius before the
    coordinator is ever entered, so a class counted downstream would describe
    the filter's geometry.
    """
    from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA

    seen: list[Any] = []

    class _Coord:
        def count_accuracy_class(self, row: dict[str, Any]) -> None:
            seen.append(row.get("accuracy"))

    class _Filter:
        @staticmethod
        def should_filter_detection(_dev: str, _name: str) -> tuple[bool, dict]:
            return False, {"latitude": HOME[0], "longitude": HOME[1], "radius": 50.0}

    # The filter is resolved as coordinator.config_entry.runtime_data
    # .google_home_filter, so the double has to carry that whole chain.
    from types import SimpleNamespace

    receiver = FcmReceiverHA.__new__(FcmReceiverHA)
    coordinator = _Coord()
    coordinator.config_entry = SimpleNamespace(  # type: ignore[attr-defined]
        runtime_data=SimpleNamespace(google_home_filter=_Filter())
    )

    out = FcmReceiverHA._prepare_coordinator_payload(
        receiver,
        coordinator,
        ("acct", "device-id"),
        {"accuracy": 1600.0, "semantic_name": "Home", "latitude": FAR[0]},
    )

    assert out is not None
    assert seen == [1600.0], "the tally must see the reported radius"
    assert out["accuracy"] == 50.0, "the substitution must still happen"
    assert out["_accuracy_counted"] is True


def test_the_push_path_survives_a_coordinator_without_the_tally() -> None:
    """An older or partial coordinator must not break the push path."""
    from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA

    receiver = FcmReceiverHA.__new__(FcmReceiverHA)

    class _Bare:
        pass

    out = FcmReceiverHA._prepare_coordinator_payload(
        receiver, _Bare(), ("acct", "device-id"), {"accuracy": 1600.0}
    )
    assert out is not None
    assert "_accuracy_counted" not in out


# --------------------------------------------------- end-to-end substitution


def test_the_poll_cycle_substitutes_the_home_zone_and_marks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the real poll cycle through the Google Home branch.

    The two remaining call sites of ``substitute_zone_accuracy`` sit inside
    ``_async_start_poll_cycle`` and ``async_locate_device``; the structural guard
    above proves they use the helper, this proves the poll one is actually
    reached and what comes out of it. Harness mirrors
    ``tests/test_transient_owner_key_propagation.py``, which builds a real
    coordinator and runs one cycle.
    """
    import asyncio
    from unittest.mock import AsyncMock

    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    class _Cache:
        async def async_get_cached_value(self, _key: str) -> None:
            return None

        async def async_set_cached_value(self, _key: str, _value: Any) -> None:
            return None

    class _Hass:
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self.loop = loop
            self.data: dict[str, Any] = {}

        def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
            return self.loop.create_task(coro)

    class _Api:
        async def async_get_device_location(
            self, _dev_id: str, _dev_name: str
        ) -> dict[str, Any]:
            # A Google Home detection: semantic name, own far-away coordinates.
            return {
                "latitude": FAR[0],
                "longitude": FAR[1],
                "accuracy": 25.0,
                "last_seen": _now(),
                "semantic_name": "Living Room speaker",
                "status": "coordinate",
            }

    class _Filter:
        @staticmethod
        def should_filter_detection(_dev: str, _name: str) -> tuple[bool, dict]:
            return False, {
                "latitude": HOME[0],
                "longitude": HOME[1],
                "radius": 400.0,
            }

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator."
        "GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-home", "name": "Home Tag"}]
    hass = _Hass(loop)
    coordinator = GoogleFindMyCoordinator(hass, cache=_Cache())
    # The canonical factory, not an ad-hoc SimpleNamespace: tests/AGENTS.md
    # requires it, and tests/test_guard_config_entry_stub.py enforces it against
    # a frozen baseline. The harness this was modelled on predates that rule and
    # sits in the legacy allowlist, so copying it verbatim tripped the guard.
    coordinator.config_entry = make_config_entry(entry_id="entry-id")
    coordinator.api = _Api()
    home_filter = _Filter()
    coordinator._get_google_home_filter = lambda: home_filter
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = list(devices)
    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None
    coordinator.async_set_update_error = lambda _exc: None
    coordinator.async_set_updated_data = lambda _data: None

    # A precise cached fix far from home, so the substituted 400 m radius meets
    # every condition the accuracy gate rejects on - except the exemption.
    coordinator._device_location_data["dev-home"] = {
        "latitude": FAR[0],
        "longitude": FAR[1],
        "accuracy": 20.0,
        "last_seen": _now() - 300,
        "status": "coordinate",
    }

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)
        loop.close()

    cached = coordinator._device_location_data["dev-home"]
    assert cached["accuracy"] == 400.0, "the substitution must reach the cache"
    assert cached["latitude"] == pytest.approx(HOME[0])
    leaked = [key for key in cached if key.startswith("_")]
    assert not leaked, f"transient markers reached the cached row: {leaked}"


def test_a_retried_report_that_reaches_no_cache_is_counted_once() -> None:
    """The third reference, and the reason it cannot be derived from the caches.

    A delayed report older than the published row lands in NEITHER store: the
    published row keeps its newer timestamp, and the coarse store only sees fixes
    the accuracy gate rejected. Returned again by the next poll it matches
    nothing, so it would enter the distribution once per retry - the bias this
    whole line of fixes is about, one case further out.

    ``claim_report_for_tally`` therefore records what it lets through. One value
    per device, not a set: the case is one report coming back repeatedly.
    """
    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    published = _now()
    coord._device_location_data = {"dev": {"last_seen": published}}
    coord._device_coarse_fix = {}

    delayed = {"accuracy": 300.0, "last_seen": published - 600}

    assert coord.claim_report_for_tally("dev", delayed) is True
    # Same report, next poll: not claimed again.
    assert coord.claim_report_for_tally("dev", dict(delayed)) is False
    assert coord.claim_report_for_tally("dev", dict(delayed)) is False

    # A genuinely different report is still claimed, so the memory does not
    # simply block everything after the first.
    assert coord.claim_report_for_tally("dev", {"last_seen": published + 60}) is True

    # A report without a stamp is still identifiable by its position and radius,
    # and it has to be: retried, it used to be counted once per delivery, so the
    # distribution measured retry frequency. Reviewed and corrected - the earlier
    # expectation here was that it is claimed every time.
    assert coord.claim_report_for_tally("dev", {"accuracy": 50.0}) is True
    assert coord.claim_report_for_tally("dev", {"accuracy": 50.0}) is False
    # A different stampless report is a different measurement.
    assert coord.claim_report_for_tally("dev", {"accuracy": 800.0}) is True
    # Nothing at all to recognise it by: claimed every time, because refusing to
    # count a fix is the worse of the two errors.
    assert coord.claim_report_for_tally("dev", {}) is True
    assert coord.claim_report_for_tally("dev", {}) is True


def test_every_counting_site_claims_the_report_first() -> None:
    """Whoever counts a report must first claim it, measured rather than enumerated.

    The contract used to name three entry points. There is a fourth - the cache fallback
    that a device-list seed reaches - and it was found one review round at a time, which
    is what this test replaces: the set of counting sites is derived from the sources, so
    a fifth one arrives red instead of arriving silently.

    An unclaimed count is not a cosmetic issue. It is the whole defect this line of fixes
    is about: a report nobody can recognise gets added to the persisted histogram on every
    delivery, and the histogram is the evidence the accuracy gate is judged on.

    Deliberately coarse: the check is "the enclosing function mentions both names", not a
    dataflow proof. It cannot show that the claim guards the count, and the tests above do
    that for each path. What it can show is the absence of a site that never claims at
    all, which is exactly how the fourth one hid.
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "custom_components"
    assert package.is_dir(), f"package root not found: {package}"

    def mentioned(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                found.add(child.id)
            elif isinstance(child, ast.Attribute):
                found.add(child.attr)
            elif isinstance(child, ast.Constant) and isinstance(child.value, str):
                found.add(child.value)
        return found

    sites: dict[str, bool] = {}
    for source in sorted(package.rglob("*.py")):
        # A parse failure must not look like "no counting sites here": that is the
        # silent-green shape every finding in this file has had.
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name == "count_accuracy_class":
                continue
            names = mentioned(node)
            if "count_accuracy_class" in names:
                key = f"{source.relative_to(package)}::{node.name}"
                sites[key] = "claim_report_for_tally" in names

    assert sites, "no counting site found at all, so nothing was measured"
    unclaimed = sorted(key for key, claimed in sites.items() if not claimed)
    assert not unclaimed, (
        f"counting sites that never claim the report: {unclaimed} "
        f"(of {len(sites)} sites found)"
    )


def test_a_stampless_report_is_counted_once_however_often_it_arrives() -> None:
    """The one report that no store can hold, delivered again and again.

    A coarse payload without a parseable ``last_seen`` is rejected by the gate and
    retained nowhere: retention keys on the stamp, so there is nothing to key on. The
    published row cannot recognise it either. It is nevertheless tallied, because it
    carries an accuracy - and that combination is what made the persisted distribution
    count deliveries instead of fixes. Polling, manual locate and the push path all
    reach the same claim, so one bounded fingerprint per device closes it for all three.

    The declared limit is pinned in the same breath: two stampless reports that agree on
    position and radius are indistinguishable by construction, and alternating between
    two different ones defeats the single slot. Both are bounded and known, unlike an
    unbounded set of every report ever seen.
    """
    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coord._device_location_data = {}
    coord._device_coarse_fix = {}

    coarse = {"latitude": 49.9, "longitude": 10.9, "accuracy": 1600.0}
    assert coord.claim_report_for_tally("dev", dict(coarse)) is True
    assert coord.claim_report_for_tally("dev", dict(coarse)) is False
    assert coord.claim_report_for_tally("dev", dict(coarse)) is False

    # A move of the same coarse radius is a different fix and is counted.
    moved = dict(coarse, latitude=50.1)
    assert coord.claim_report_for_tally("dev", moved) is True

    # Another device keeps its own slot.
    assert coord.claim_report_for_tally("other", dict(coarse)) is True

    # A NaN never equals itself, so an identity built from raw floats would call every
    # delivery new. Each delivery builds its own NaN here, because a tuple comparison
    # short-circuits on identity: reusing one NaN object makes the two tuples compare
    # equal and the probe passes against a broken implementation.
    def _nan_fix() -> dict[str, float]:
        return {"latitude": float("nan"), "longitude": 10.9, "accuracy": 1600.0}

    assert coord.claim_report_for_tally("nan-dev", _nan_fix()) is True
    assert coord.claim_report_for_tally("nan-dev", _nan_fix()) is False


def test_the_replay_predicate_also_knows_the_retained_coarse_fix() -> None:
    """The reference the published row cannot provide.

    A fix the gate rejects leaves the published row untouched on purpose and is
    stored aside. Comparing against the published row alone therefore treats
    every later poll of that same report as new - and it does so for exactly the
    coarse fixes the distribution is collected for, which is the one place the
    bias could not be afforded.
    """
    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    coord = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    stamp = _now() - 300
    coord._device_location_data = {
        "dev": {"latitude": HOME[0], "longitude": HOME[1], "last_seen": stamp - 900}
    }
    coord._device_coarse_fix = {
        "dev": {"latitude": FAR[0], "longitude": FAR[1], "last_seen": stamp}
    }

    # Matches the retained coarse fix, not the published row.
    assert coord.is_replayed_report("dev", {"last_seen": stamp}) is True
    # Matches the published row.
    assert coord.is_replayed_report("dev", {"last_seen": stamp - 900}) is True
    # Matches neither: a genuinely new report.
    assert coord.is_replayed_report("dev", {"last_seen": _now()}) is False
    # No stamp is not a replay - it cannot be shown to be one.
    assert coord.is_replayed_report("dev", {}) is False


def test_the_push_path_does_not_count_a_replay() -> None:
    """Second counting site, same rule, driven through the real prepare step.

    A duplicate push delivery, or the same report arriving again after a poll,
    would otherwise add the same measurement to the persisted distribution once
    per delivery. The marker is set regardless, so the cache layer does not
    tally it either.
    """
    from types import SimpleNamespace

    from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA

    stamp = _now() - 300
    counted: list[Any] = []

    class _Coord:
        def __init__(self) -> None:
            self._device_location_data = {"device-id": {"last_seen": stamp}}
            self._device_coarse_fix: dict[str, Any] = {}
            self.config_entry = SimpleNamespace(
                runtime_data=SimpleNamespace(google_home_filter=None)
            )

        def count_accuracy_class(self, row: dict[str, Any]) -> None:
            counted.append(row.get("accuracy"))

        def is_replayed_report(self, device_id: str, row: dict[str, Any]) -> bool:
            return CacheOperations.is_replayed_report(self, device_id, row)

        def claim_report_for_tally(self, device_id: str, row: dict[str, Any]) -> bool:
            # Both real implementations, so the double exercises the production
            # rule rather than a restatement of it.
            return CacheOperations.claim_report_for_tally(self, device_id, row)

    receiver = FcmReceiverHA.__new__(FcmReceiverHA)
    out = FcmReceiverHA._prepare_coordinator_payload(
        receiver,
        _Coord(),
        ("acct", "device-id"),
        {"accuracy": 1600.0, "last_seen": stamp, "latitude": FAR[0]},
    )

    assert out is not None
    assert counted == [], f"a replayed push was counted: {counted}"
    assert out["_accuracy_counted"] is True, "the cache layer must not retry it"


def test_a_replayed_report_is_not_counted_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distribution must follow the reports, not the poll interval.

    A poll that returns the same report timestamp the cache already holds is a
    replay, not an incoming fix. Counted anyway, a device reporting once an hour
    and polled every five minutes would enter its accuracy class twelve times,
    and the distribution AP4a exists for - re-tuning the two thresholds against
    real data - would measure our polling frequency instead.

    The marker is still set, so ``update_device_cache`` does not tally it later:
    "not counted" is a decision, and it is made where the replay is known.
    """
    import asyncio
    from unittest.mock import AsyncMock

    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    class _Cache:
        async def async_get_cached_value(self, _key: str) -> None:
            return None

        async def async_set_cached_value(self, _key: str, _value: Any) -> None:
            return None

    class _Hass:
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self.loop = loop
            self.data: dict[str, Any] = {}

        def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
            return self.loop.create_task(coro)

    stamp = _now() - 300

    class _Api:
        async def async_get_device_location(
            self, _dev_id: str, _dev_name: str
        ) -> dict[str, Any]:
            # Same report timestamp as the cached row below: a replay.
            return {
                "latitude": HOME[0],
                "longitude": HOME[1],
                "accuracy": 25.0,
                "last_seen": stamp,
                "status": "coordinate",
            }

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator."
        "GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-replay", "name": "Replay Tag"}]
    coordinator = GoogleFindMyCoordinator(_Hass(loop), cache=_Cache())
    coordinator.config_entry = make_config_entry(entry_id="entry-id")
    coordinator.api = _Api()
    coordinator._get_google_home_filter = lambda: None
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = list(devices)
    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None
    coordinator.async_set_update_error = lambda _exc: None
    coordinator.async_set_updated_data = lambda _data: None
    coordinator._device_location_data["dev-replay"] = {
        "latitude": HOME[0],
        "longitude": HOME[1],
        "accuracy": 25.0,
        "last_seen": stamp,
        "status": "coordinate",
    }

    counted: list[Any] = []
    coordinator.count_accuracy_class = lambda row: counted.append(row.get("accuracy"))

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)
        loop.close()

    assert counted == [], f"a replay was counted: {counted}"


def test_the_poll_fallback_write_strips_every_transient_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The second direct-write fallback had the same leak as the push one.

    ``_async_start_poll_cycle`` writes ``_device_location_data`` itself when the
    coordinator's ``update_device_cache`` is not the real method (a test double).
    That branch popped a LIST of markers, and the list was correct until the next
    marker was added - ``_accuracy_counted`` reached the cache through it. Both
    fallbacks now share ``strip_transient_keys``, and this asserts the property
    rather than the names, so the next marker is covered by construction.
    """
    import asyncio
    from unittest.mock import AsyncMock

    from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator

    class _Cache:
        async def async_get_cached_value(self, _key: str) -> None:
            return None

        async def async_set_cached_value(self, _key: str, _value: Any) -> None:
            return None

    class _Hass:
        def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
            self.loop = loop
            self.data: dict[str, Any] = {}

        def async_create_task(self, coro: Any, *, name: str | None = None) -> Any:
            return self.loop.create_task(coro)

    class _Api:
        async def async_get_device_location(
            self, _dev_id: str, _dev_name: str
        ) -> dict[str, Any]:
            return {
                "latitude": FAR[0],
                "longitude": FAR[1],
                "accuracy": 25.0,
                "last_seen": _now(),
                "semantic_name": "Living Room speaker",
                "status": "coordinate",
            }

    class _Filter:
        @staticmethod
        def should_filter_detection(_dev: str, _name: str) -> tuple[bool, dict]:
            return False, {"latitude": HOME[0], "longitude": HOME[1], "radius": 400.0}

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator."
        "GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    loop = asyncio.new_event_loop()
    devices = [{"id": "dev-home", "name": "Home Tag"}]
    coordinator = GoogleFindMyCoordinator(_Hass(loop), cache=_Cache())
    coordinator.config_entry = make_config_entry(entry_id="entry-id")
    coordinator.api = _Api()
    home_filter = _Filter()
    coordinator._get_google_home_filter = lambda: home_filter
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._get_ignored_set = set
    coordinator._last_device_list = list(devices)
    coordinator.data = []
    coordinator.last_update_success = True
    coordinator.last_exception = None
    coordinator.async_set_update_error = lambda _exc: None
    coordinator.async_set_updated_data = lambda _data: None
    # THE point of this test: a double for update_device_cache selects the
    # direct-write branch, which is the one that leaked.
    coordinator.update_device_cache = lambda *_a, **_k: None

    try:
        loop.run_until_complete(
            coordinator._async_start_poll_cycle(devices, force=True)
        )
    finally:
        drain_loop(loop)
        loop.close()

    cached = coordinator._device_location_data["dev-home"]
    assert cached["accuracy"] == 400.0, "the fallback must have written the row"
    leaked = [key for key in cached if key.startswith("_")]
    assert not leaked, f"transient markers reached the cached row: {leaked}"
