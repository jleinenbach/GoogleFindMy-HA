# tests/test_eid_resolver_scan_time.py
"""The resolver dates its state with the observation, not with the processing.

``resolve_eid``/``resolve_eid_all`` accept the advertisement time on the
monotonic clock (``observed_at``, what HA hands over as
``BluetoothServiceInfoBleak.time``). HA replays the last advertisement of
every known address when the callback is registered, up to 15 minutes old
for non-connectable sources, and restores that history across restarts. The
three state writers (battery state, scan info, lock confirmation) must
therefore stamp the sighting, not the moment of the replay.

Every case goes through the public entry point, not through the writers
directly: a direct call measures a writer, not whether the value reaches it.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import Iterator

import pytest

import custom_components.googlefindmy.eid_resolver as resolver_module
from custom_components.googlefindmy.eid_resolver import (
    EIDMatch,
    GoogleFindMyEIDResolver,
    GoogleFindMyEIDResolverProtocol,
)
from custom_components.googlefindmy.FMDNCrypto.eid_generator import (
    LEGACY_EID_LENGTH,
)
from tests.test_ble_battery_sensor import (
    _make_resolver,
    _match,
    _service_data_payload,
)

MONO_NOW = 50_000.0
WALL_NOW = 1_700_000_000.0
AGE = 600.0  # a replayed sighting, ten minutes old


@pytest.fixture
def frozen_clocks(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin both clocks so that the derived observation time is exact."""
    monkeypatch.setattr(time, "monotonic", lambda: MONO_NOW)
    monkeypatch.setattr(time, "time", lambda: WALL_NOW)
    yield


def _primed_resolver(device_id: str) -> tuple[GoogleFindMyEIDResolver, bytes]:
    """Resolver whose cache resolves one payload to ``device_id``.

    The payload carries a hashed-flags byte so that the battery writer runs
    as well; without it only the lock and scan-info writers would be measured.
    """
    resolver = _make_resolver()
    eid = bytes([0x5A]) * LEGACY_EID_LENGTH
    raw = _service_data_payload(eid, 0b00000_01_0)  # battery=1 (NORMAL)
    resolver._lookup[eid] = [_match(device_id)]
    resolver._lookup_metadata[eid] = {"flags_xor_mask": 0x00}
    return resolver, raw


@pytest.mark.usefixtures("frozen_clocks")
def test_scan_info_is_dated_with_the_advertisement() -> None:
    """T1: ``BLEScanInfo`` carries the observation on both clocks."""
    resolver, raw = _primed_resolver("dev-t1")

    match = resolver.resolve_eid(
        raw, ble_address="11:22:33:44:55:66", observed_at=MONO_NOW - AGE
    )

    assert match is not None  # positive control
    info = resolver.get_ble_scan_info("dev-t1")
    assert info is not None
    assert info.observed_at == MONO_NOW - AGE
    assert info.observed_at_wall == WALL_NOW - AGE


@pytest.mark.usefixtures("frozen_clocks")
def test_battery_state_is_dated_with_the_advertisement() -> None:
    """T2: ``BLEBatteryState.observed_at_wall`` is the sighting, not the replay.

    This is the value ``sensor.py``/``binary_sensor.py`` expose as
    ``last_ble_observation``.
    """
    resolver, raw = _primed_resolver("dev-t2")

    assert resolver.resolve_eid(raw, observed_at=MONO_NOW - AGE) is not None

    state = resolver.get_ble_battery_state("dev-t2")
    assert state is not None
    assert state.observed_at_wall == WALL_NOW - AGE


@pytest.mark.usefixtures("frozen_clocks")
def test_lock_confirmation_is_dated_with_the_advertisement() -> None:
    """T3: the lock confirmation is the sighting, truncated to whole seconds."""
    resolver, raw = _primed_resolver("dev-t3")

    assert resolver.resolve_eid(raw, observed_at=MONO_NOW - AGE) is not None

    assert resolver._last_lock_confirmation["dev-t3"] == int(WALL_NOW - AGE)
    # The lock the match creates carries the same sighting (persisted).
    lock = resolver._locks["dev-t3"]
    assert lock.created_at == int(WALL_NOW - AGE)
    assert lock.last_seen_at == int(WALL_NOW - AGE)


@pytest.mark.usefixtures("frozen_clocks")
def test_observation_in_the_future_is_clamped_to_now() -> None:
    """T4: a timestamp ahead of ``time.monotonic`` is clamped, not projected.

    HA's own stamp (``CLOCK_MONOTONIC_COARSE``) never runs ahead of
    ``time.monotonic``, it lags by up to one tick. A value ahead of the
    resolver's reading comes from a caller with a different clock or from a
    skewed test clock; whatever its origin, the sighting must never be dated
    into the future, on either clock (R9 shows what a future monotonic
    stamp would do to the out-of-order guard).
    """
    resolver, raw = _primed_resolver("dev-t4")

    assert (
        resolver.resolve_eid(
            raw, ble_address="AA:BB:CC:DD:EE:FF", observed_at=MONO_NOW + 0.005
        )
        is not None
    )

    info = resolver.get_ble_scan_info("dev-t4")
    state = resolver.get_ble_battery_state("dev-t4")
    assert info is not None and state is not None
    assert info.observed_at_wall == WALL_NOW
    assert state.observed_at_wall == WALL_NOW
    assert info.observed_at == MONO_NOW


@pytest.mark.usefixtures("frozen_clocks")
def test_without_observed_at_the_observation_is_now() -> None:
    """T5: the pre-existing contract for callers that omit ``observed_at``.

    Third-party callers (Bermuda) resolve without knowing the advertisement
    time; for them the observation is the moment of the call, exactly as
    before the parameter existed.
    """
    resolver, raw = _primed_resolver("dev-t5")

    assert resolver.resolve_eid(raw, ble_address="AA:BB:CC:DD:EE:FF") is not None

    info = resolver.get_ble_scan_info("dev-t5")
    state = resolver.get_ble_battery_state("dev-t5")
    assert info is not None and state is not None
    assert info.observed_at == MONO_NOW
    assert info.observed_at_wall == WALL_NOW
    assert state.observed_at_wall == WALL_NOW
    assert resolver._last_lock_confirmation["dev-t5"] == int(WALL_NOW)


@pytest.mark.usefixtures("frozen_clocks")
def test_resolve_eid_all_dates_like_resolve_eid() -> None:
    """T6: the shared-device entry point applies the same observation time."""
    resolver, raw = _primed_resolver("dev-t6")

    matches = resolver.resolve_eid_all(
        raw, ble_address="AA:BB:CC:DD:EE:FF", observed_at=MONO_NOW - AGE
    )

    assert len(matches) == 1  # positive control
    info = resolver.get_ble_scan_info("dev-t6")
    state = resolver.get_ble_battery_state("dev-t6")
    assert info is not None and state is not None
    assert info.observed_at == MONO_NOW - AGE
    assert info.observed_at_wall == WALL_NOW - AGE
    assert state.observed_at_wall == WALL_NOW - AGE
    assert resolver._last_lock_confirmation["dev-t6"] == int(WALL_NOW - AGE)


def test_implementation_satisfies_the_protocol() -> None:
    """Protocol and implementation carry the same ``observed_at`` signature.

    The assignment is what ``mypy --strict`` checks structurally; the runtime
    signature comparison is the part a type checker alone would not run.
    """
    resolver = _make_resolver()
    _: GoogleFindMyEIDResolverProtocol = resolver
    for name in ("resolve_eid", "resolve_eid_all"):
        proto = inspect.signature(getattr(GoogleFindMyEIDResolverProtocol, name))
        impl = inspect.signature(getattr(GoogleFindMyEIDResolver, name))
        assert "observed_at" in proto.parameters
        assert proto.parameters["observed_at"].default is None
        assert proto.parameters["observed_at"].kind is inspect.Parameter.KEYWORD_ONLY
        assert impl.parameters["observed_at"] == proto.parameters["observed_at"]


def test_state_writers_read_no_clock_of_their_own() -> None:
    """Structural pin: the writers consume the observation, they do not date it.

    Before the fix, ``_update_ble_battery`` and ``_record_ble_scan_info`` each
    read ``time.time()``/``time.monotonic()`` on their own, and
    ``_resolve_eid_internal`` read ``time.time()`` for the lock stamp it hands
    to ``_update_match_state``. Only the derivation helper may read a clock.
    The pins are on source text and therefore sensitive to a comment that
    quotes ``time.time()``; keep such comments out of these bodies.
    """
    clock_read = re.compile(r"time\.(time|monotonic)\(\)")
    for writer in (
        GoogleFindMyEIDResolver._update_ble_battery,
        GoogleFindMyEIDResolver._record_ble_scan_info,
    ):
        assert not clock_read.search(inspect.getsource(writer)), writer.__name__

    internal = inspect.getsource(GoogleFindMyEIDResolver._resolve_eid_internal)
    # The lock stamp comes from the clock; the only remaining clock read in
    # this body is the heuristic *search* parameter, which is not a stamp.
    assert "now = int(clock.wall)" in internal
    assert not re.search(r"\bnow = int\(time\.time\(\)\)", internal)
    # The heuristic path confirms through the shared helper (which enforces
    # the newest-sighting rule) and hands it the observation.
    assert internal.count("_confirm_lock(") == 1
    assert "_confirm_lock(heuristic_match.device_id, int(clock.wall))" in internal
    assert "_last_lock_confirmation[" not in internal

    assert clock_read.search(inspect.getsource(resolver_module._observation_clock))


@pytest.mark.usefixtures("frozen_clocks")
def test_heuristic_match_confirms_the_lock_as_of_the_sighting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heuristic path stamps the lock confirmation with the observation.

    The heuristic *search* keeps running against now (which EID would be
    current), but the confirmation it records is a stamp like on the cache
    hit path and must carry the sighting.
    """
    resolver, raw = _primed_resolver("dev-cached")
    unrelated = _service_data_payload(bytes([0x3C]) * LEGACY_EID_LENGTH, 0)
    seen: list[int] = []

    def fake_heuristic(
        _self: GoogleFindMyEIDResolver, candidates: list[bytes], *, now_unix: int
    ) -> EIDMatch:
        seen.append(now_unix)
        return _match("dev-heur")

    # The resolver is a slots dataclass; patch the class, not the instance.
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_heuristic_resolve", fake_heuristic)

    match = resolver.resolve_eid(unrelated, observed_at=MONO_NOW - AGE)

    assert match is not None and match.device_id == "dev-heur"  # positive control
    assert seen == [int(WALL_NOW)]  # the search parameter stays "now"
    assert resolver._last_lock_confirmation["dev-heur"] == int(WALL_NOW - AGE)


@pytest.mark.usefixtures("frozen_clocks")
def test_negative_observed_at_is_a_sighting_before_boot() -> None:
    """A negative monotonic timestamp is real input, not an error.

    ``habluetooth`` restores the advertisement history across restarts as
    ``unix_time - (time.time() - time.monotonic())``, which is negative for
    a sighting that predates the current boot. The age is then larger than
    the uptime, and the wall-clock time is still the original sighting.
    """
    resolver, raw = _primed_resolver("dev-neg")

    assert (
        resolver.resolve_eid(raw, ble_address="AA:BB:CC:DD:EE:FF", observed_at=-30.0)
        is not None
    )

    info = resolver.get_ble_scan_info("dev-neg")
    assert info is not None
    assert info.observed_at == -30.0
    assert info.observed_at_wall == WALL_NOW - (MONO_NOW + 30.0)


@pytest.mark.usefixtures("frozen_clocks")
def test_direct_scan_info_call_without_clock_is_dated_now() -> None:
    """Direct callers of the scan-info writer get the "now" fallback."""
    resolver = _make_resolver()

    resolver._record_ble_scan_info([_match("dev-direct")], "AA:BB:CC:DD:EE:FF")

    info = resolver.get_ble_scan_info("dev-direct")
    assert info is not None
    assert info.observed_at == MONO_NOW
    assert info.observed_at_wall == WALL_NOW


# --- Out-of-order delivery ---------------------------------------------------
#
# HA before 2026.8 replays the advertisement history in dict order (Core
# `manager.py`, `history.values()`; 2026.8 sorts by `time`). A tracker rotates
# its address, so the history can hold several of its sightings and an older
# one can be delivered after a newer one. The state on record must then stay
# with the newer sighting: `last_ble_observation` must not move backwards and
# the stored address must not fall back to a rotated-out one.

NEWER = MONO_NOW - 100.0  # the sighting that arrives first
OLDER = MONO_NOW - AGE  # the sighting that arrives second, but is older


def _primed_pair(device_id: str) -> tuple[GoogleFindMyEIDResolver, bytes, bytes]:
    """Resolver with two payloads for one device: battery NORMAL and LOW.

    The second payload resolves with ``time_offset=-1`` so that a rolled-back
    drift is observable on the lock.
    """
    resolver, raw_normal = _primed_resolver(device_id)
    eid_low = bytes([0x6B]) * LEGACY_EID_LENGTH
    raw_low = _service_data_payload(eid_low, 0b00000_10_0)  # battery=2 (LOW)
    resolver._lookup[eid_low] = [
        EIDMatch(
            device_id=device_id,
            config_entry_id="entry-1",
            canonical_id=device_id,
            time_offset=-1,
            is_reversed=False,
        )
    ]
    resolver._lookup_metadata[eid_low] = {"flags_xor_mask": 0x00}
    return resolver, raw_normal, raw_low


@pytest.mark.usefixtures("frozen_clocks")
def test_older_sighting_after_newer_keeps_scan_info() -> None:
    """R1: the stored address and scan time stay with the newer sighting."""
    resolver, raw_newer, raw_older = _primed_pair("dev-r1")

    assert resolver.resolve_eid(raw_newer, ble_address="AA:AA", observed_at=NEWER)
    assert resolver.resolve_eid(raw_older, ble_address="BB:BB", observed_at=OLDER)

    info = resolver.get_ble_scan_info("dev-r1")
    assert info is not None
    assert info.ble_address == "AA:AA"
    assert info.observed_at == NEWER
    assert info.observed_at_wall == WALL_NOW - 100.0


@pytest.mark.usefixtures("frozen_clocks")
def test_older_sighting_after_newer_keeps_battery_state() -> None:
    """R2: ``last_ble_observation`` and the battery level do not move back."""
    resolver, raw_newer, raw_older = _primed_pair("dev-r2")

    assert resolver.resolve_eid(raw_newer, observed_at=NEWER)
    assert resolver.resolve_eid(raw_older, observed_at=OLDER)

    state = resolver.get_ble_battery_state("dev-r2")
    assert state is not None
    assert state.battery_level == 1  # NORMAL from the newer sighting, not LOW
    assert state.observed_at_wall == WALL_NOW - 100.0


@pytest.mark.usefixtures("frozen_clocks")
def test_older_sighting_after_newer_keeps_lock_stamps() -> None:
    """R3: confirmation, ``last_seen_at`` and drift stay with the newer sighting."""
    resolver, raw_newer, raw_older = _primed_pair("dev-r3")

    assert resolver.resolve_eid(raw_newer, observed_at=NEWER)
    assert resolver.resolve_eid(raw_older, observed_at=OLDER)

    assert resolver._last_lock_confirmation["dev-r3"] == int(WALL_NOW - 100.0)
    lock = resolver._locks["dev-r3"]
    assert lock.last_seen_at == int(WALL_NOW - 100.0)
    assert lock.drift_offset == 0  # not the -1 of the older sighting


@pytest.mark.usefixtures("frozen_clocks")
def test_newer_sighting_after_older_updates_everything() -> None:
    """R4: the guard is one-directional; in-order delivery still updates.

    Positive control for R1 to R3: the same two sightings in the natural
    order end on the newer one, on all three writers, including the drift.
    """
    resolver, raw_newer, raw_older = _primed_pair("dev-r4")

    assert resolver.resolve_eid(raw_older, ble_address="BB:BB", observed_at=OLDER)
    assert resolver.resolve_eid(raw_newer, ble_address="AA:AA", observed_at=NEWER)

    info = resolver.get_ble_scan_info("dev-r4")
    state = resolver.get_ble_battery_state("dev-r4")
    assert info is not None and state is not None
    assert (info.ble_address, info.observed_at) == ("AA:AA", NEWER)
    assert (state.battery_level, state.observed_at_wall) == (1, WALL_NOW - 100.0)
    lock = resolver._locks["dev-r4"]
    assert lock.last_seen_at == int(WALL_NOW - 100.0)
    assert lock.drift_offset == 0  # the newer sighting's offset replaced -1


@pytest.mark.usefixtures("frozen_clocks")
def test_same_time_sighting_is_applied() -> None:
    """R5: only a strictly older sighting is ignored.

    Two proxies can hand over the same advertisement with the same stamp;
    the second one is not older and is applied (the boundary of the guard).
    """
    resolver, raw_newer, raw_older = _primed_pair("dev-r5")

    assert resolver.resolve_eid(raw_newer, ble_address="AA:AA", observed_at=NEWER)
    assert resolver.resolve_eid(raw_older, ble_address="BB:BB", observed_at=NEWER)

    info = resolver.get_ble_scan_info("dev-r5")
    state = resolver.get_ble_battery_state("dev-r5")
    assert info is not None and state is not None
    assert info.ble_address == "BB:BB"
    assert state.battery_level == 2
    assert resolver._locks["dev-r5"].drift_offset == -1  # lock boundary, too


@pytest.mark.usefixtures("frozen_clocks")
def test_heuristic_confirmation_does_not_move_backwards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R6: the heuristic path shares the newest-sighting rule for the stamp."""
    resolver, _raw = _primed_resolver("dev-cached")
    unrelated = _service_data_payload(bytes([0x3C]) * LEGACY_EID_LENGTH, 0)

    def fake_heuristic(
        _self: GoogleFindMyEIDResolver, candidates: list[bytes], *, now_unix: int
    ) -> EIDMatch:
        return _match("dev-heur")

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_heuristic_resolve", fake_heuristic)

    assert resolver.resolve_eid(unrelated, observed_at=NEWER)
    assert resolver.resolve_eid(unrelated, observed_at=OLDER)

    assert resolver._last_lock_confirmation["dev-heur"] == int(WALL_NOW - 100.0)


@pytest.mark.usefixtures("frozen_clocks")
def test_older_sighting_after_newer_keeps_known_offset() -> None:
    """R7: ``_known_offsets`` mirrors the lock drift and follows the same rule.

    It is restored from ``lock.drift_offset`` on load, so the two must carry
    the same sighting. Needs a ``timestamp_basis`` in the metadata, otherwise
    the offset is never recorded and the guard would be measured in a vacuum.
    """
    resolver, raw_newer, raw_older = _primed_pair("dev-r7")
    for metadata in resolver._lookup_metadata.values():
        metadata["timestamp_basis"] = "unix"

    assert resolver.resolve_eid(raw_newer, observed_at=NEWER)
    assert resolver._known_offsets[("dev-r7", "unix")] == 0  # positive control
    assert resolver.resolve_eid(raw_older, observed_at=OLDER)

    assert resolver._known_offsets[("dev-r7", "unix")] == 0  # not the older -1


@pytest.mark.usefixtures("frozen_clocks")
def test_lock_keeps_the_newer_sighting_within_one_second() -> None:
    """R8: the lock is ordered on the monotonic clock, not on its second stamp.

    Lock stamps are whole seconds (persisted schema). Within this process
    the writer still orders sightings on the monotonic clock, so two
    sightings in the same wall second keep the newer one on the lock as well,
    like battery state and scan info. The remaining limit is pinned by R12.
    """
    resolver, raw_newer, raw_older = _primed_pair("dev-r8")
    # Both sightings fall into the same wall second (x.8 and x.3).
    same_second_newer = NEWER - 0.2
    same_second_older = NEWER - 0.7

    assert resolver.resolve_eid(
        raw_newer, ble_address="AA:AA", observed_at=same_second_newer
    )
    assert resolver.resolve_eid(
        raw_older, ble_address="BB:BB", observed_at=same_second_older
    )

    info = resolver.get_ble_scan_info("dev-r8")
    state = resolver.get_ble_battery_state("dev-r8")
    assert info is not None and state is not None
    assert info.ble_address == "AA:AA"
    assert state.battery_level == 1
    assert resolver._locks["dev-r8"].drift_offset == 0  # the newer sighting


@pytest.mark.usefixtures("frozen_clocks")
def test_future_sighting_does_not_freeze_the_out_of_order_guard() -> None:
    """R9: a sighting from ahead of the local clock does not block later ones.

    Found by Codex on the PR: with the wall clock clamped but the monotonic
    stamp kept, the scan-info guard would compare every later, correctly
    dated sighting against a future value and reject it as older, freezing
    the stored address until the local clock caught up (indefinitely for a
    caller with a different clock origin). Both clocks are clamped, so a
    sighting dated *now* still replaces it.
    """
    resolver, raw_first, raw_second = _primed_pair("dev-r9")

    assert resolver.resolve_eid(
        raw_first, ble_address="AA:AA", observed_at=MONO_NOW + 3600.0
    )
    assert resolver.resolve_eid(raw_second, ble_address="BB:BB", observed_at=MONO_NOW)

    info = resolver.get_ble_scan_info("dev-r9")
    state = resolver.get_ble_battery_state("dev-r9")
    assert info is not None and state is not None
    assert info.ble_address == "BB:BB"
    assert info.observed_at == MONO_NOW
    assert info.observed_at_wall == WALL_NOW
    assert state.battery_level == 2  # the wall-clock writer took it as well


def test_wall_clock_step_back_does_not_freeze_the_battery_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R10: a backward wall-clock correction does not make live sightings "older".

    Found by Codex on the PR: the battery writer ordered sightings by the
    wall clock. When the wall clock is stepped backwards while HA runs (NTP
    or a manual correction), a later advertisement gets a smaller wall
    stamp although it is newer on the monotonic clock, and a wall-clock
    guard would reject every battery/UWT update until wall time caught up.
    The guard orders by the monotonic stamp, so the later sighting wins and
    its wall stamp is the corrected one.
    """
    resolver, raw_first, raw_second = _primed_pair("dev-r10")
    monkeypatch.setattr(time, "monotonic", lambda: MONO_NOW)

    monkeypatch.setattr(time, "time", lambda: WALL_NOW)
    assert resolver.resolve_eid(raw_first, observed_at=MONO_NOW - 100.0)
    first = resolver.get_ble_battery_state("dev-r10")
    assert first is not None and first.battery_level == 1  # positive control

    step = 3600.0  # the wall clock is corrected back by one hour
    monkeypatch.setattr(time, "time", lambda: WALL_NOW - step)
    assert resolver.resolve_eid(raw_second, observed_at=MONO_NOW - 50.0)

    state = resolver.get_ble_battery_state("dev-r10")
    assert state is not None
    assert state.battery_level == 2  # LOW, from the later sighting
    assert state.observed_at_monotonic == MONO_NOW - 50.0
    assert state.observed_at_wall == WALL_NOW - step - 50.0


def test_wall_clock_step_back_does_not_freeze_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R11: the lock writer is ordered on the monotonic clock as well.

    Same class as R10 on the lock guard: with the wall stamp as the
    reference, a backward wall-clock correction would hold every live
    sighting as older than the lock for the size of the step, freezing drift
    learning and the ``_known_offsets`` mirror. Ordered on the monotonic
    clock, the later sighting updates drift and ``last_seen_at`` (which then
    carries the corrected wall second).
    """
    resolver, raw_first, raw_second = _primed_pair("dev-r11")
    monkeypatch.setattr(time, "monotonic", lambda: MONO_NOW)

    monkeypatch.setattr(time, "time", lambda: WALL_NOW)
    assert resolver.resolve_eid(raw_first, observed_at=MONO_NOW - 100.0)
    assert resolver._locks["dev-r11"].drift_offset == 0

    step = 3600.0
    monkeypatch.setattr(time, "time", lambda: WALL_NOW - step)
    assert resolver.resolve_eid(raw_second, observed_at=MONO_NOW - 50.0)

    lock = resolver._locks["dev-r11"]
    assert lock.drift_offset == -1  # learned from the later sighting
    assert lock.last_seen_at == int(WALL_NOW - step - 50.0)
    assert resolver._last_lock_confirmation["dev-r11"] == int(WALL_NOW - 100.0)


@pytest.mark.usefixtures("frozen_clocks")
def test_first_sighting_after_restart_is_ordered_by_the_persisted_second() -> None:
    """R12: declared limit. Across a restart only the wall second is on record.

    A restored lock carries ``last_seen_at`` in whole seconds and no
    monotonic stamp (the monotonic clock does not survive a restart), so the
    first sighting after a restart is compared on the second: a sighting in
    the same second as the persisted one is applied in delivery order, one
    in an earlier second is held as older. Pinned so that the limit is a
    decision on record, not a surprise.
    """
    resolver, raw_first, raw_second = _primed_pair("dev-r12")
    assert resolver.resolve_eid(raw_first, observed_at=NEWER + 0.8)  # x.8
    persisted = resolver._locks["dev-r12"]
    assert persisted.last_seen_at == int(WALL_NOW - 100.0)  # second x
    # "Restart": the in-process order is gone, the persisted second remains.
    resolver._lock_last_seen_monotonic.clear()

    # An earlier second: held as older, drift stays.
    assert resolver.resolve_eid(raw_second, observed_at=NEWER - 1.0)
    assert resolver._locks["dev-r12"].drift_offset == 0

    # The same second, but older (x.3): applied in delivery order, the limit.
    assert resolver.resolve_eid(raw_second, observed_at=NEWER + 0.3)
    assert resolver._locks["dev-r12"].drift_offset == -1
