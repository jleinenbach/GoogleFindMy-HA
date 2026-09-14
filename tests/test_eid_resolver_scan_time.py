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
    into the future.
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
    assert info.observed_at == MONO_NOW + 0.005


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
    assert internal.count("_last_lock_confirmation[") == 1
    assert "= int(clock.wall)" in internal.split("_last_lock_confirmation[")[1]

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
