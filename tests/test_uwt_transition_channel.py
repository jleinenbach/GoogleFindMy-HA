# tests/test_uwt_transition_channel.py
"""Measurement channel for unwanted-tracking protection mode changes.

The resolver records each change of a device's protection mode it sees per
(config entry, device) and keeps a fixed-size history per config entry. The
history feeds the ``uwt_transitions`` diagnostics block; the
``FMDN_UWT_TRANSITION`` log line is pinned further down. ``poll_age_s`` is
the age, at the observation, of the last location request the integration
sent for the device, read only from the coordinator of the match's own entry.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.googlefindmy import eid_resolver
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.eid_resolver import (
    FMDN_FRAME_TYPE,
    MODERN_FRAME_TYPE,
    EIDMatch,
    EidVariant,
    GoogleFindMyEIDResolver,
    ObservationClock,
    UwtSource,
    _FlagsReading,
)
from tests.test_ble_battery_sensor import _make_resolver

_RESOLVER_LOGGER = "custom_components.googlefindmy.eid_resolver"
_WALL0 = 1_800_000_000.0
_READING = _FlagsReading(uwt_bit=True, origin="framed", decoded=0x05)


def _match(entry: str = "entry-a", canonical: str = "dev-1") -> EIDMatch:
    return EIDMatch(
        device_id=f"reg-{canonical}",
        config_entry_id=entry,
        canonical_id=canonical,
        time_offset=0,
        is_reversed=False,
    )


def _clock(mono: float) -> ObservationClock:
    return ObservationClock(monotonic=mono, wall=_WALL0 + mono)


class _Coordinator:
    """Coordinator stand-in that answers ``locate_requested_mono``."""

    def __init__(self, stamps: dict[str, float] | None = None) -> None:
        self.stamps = dict(stamps or {})

    def locate_requested_mono(self, device_id: str) -> float | None:
        return self.stamps.get(device_id)


def _resolver(entries: dict[str, object] | None = None) -> GoogleFindMyEIDResolver:
    resolver = _make_resolver()
    resolver.hass.data[DOMAIN] = {"entries": dict(entries or {})}
    return resolver


def _observe(
    resolver: GoogleFindMyEIDResolver,
    mode: bool | None,
    mono: float,
    *,
    matches: list[EIDMatch] | None = None,
    source: UwtSource | None = None,
    reading: _FlagsReading = _READING,
    frame_type: int | None = MODERN_FRAME_TYPE,
) -> None:
    if source is None:
        source = UwtSource.UNKNOWN if mode is None else UwtSource.FLAGS_BIT
    resolver._note_uwt_observation(
        matches if matches is not None else [_match()],
        mode=mode,
        source=source,
        reading=reading,
        frame_type=frame_type,
        payload_len=29,
        clock=_clock(mono),
    )


def _events(resolver: GoogleFindMyEIDResolver, entry: str = "entry-a") -> list:
    return list(resolver.uwt_transition_snapshot(entry).events)


# ---------------------------------------------------------------------------
# What counts as a change
# ---------------------------------------------------------------------------


def test_first_known_mode_is_recorded_with_old_unknown() -> None:
    resolver = _resolver()
    _observe(resolver, True, 10.0)
    (event,) = _events(resolver)
    assert (event.kind, event.old, event.new) == ("change", "unknown", "on")
    assert (event.source, event.flags, event.frame) == ("flags_bit", "framed", "0x41")
    assert event.payload_len == 29
    assert event.observed == "2027-01-15T08:00:10Z"
    assert event.seq == 1


def test_an_observation_without_frame_type_is_labelled_none() -> None:
    """A bare EID carries no frame type: ``frame=none``, ``flags=absent``."""
    resolver = _resolver()
    _observe(
        resolver,
        True,
        10.0,
        source=UwtSource.FLAGS_BIT,
        reading=_FlagsReading(True, "framed", 0x01),
        frame_type=None,
    )
    (event,) = _events(resolver)
    assert event.frame == "none"


def test_the_same_mode_again_records_nothing() -> None:
    resolver = _resolver()
    _observe(resolver, True, 10.0)
    _observe(resolver, True, 20.0)
    assert len(_events(resolver)) == 1


def test_a_change_is_recorded_with_its_previous_mode() -> None:
    resolver = _resolver()
    _observe(resolver, False, 10.0)
    _observe(resolver, True, 70.0)
    events = _events(resolver)
    assert [(e.old, e.new, e.seq) for e in events] == [
        ("unknown", "off", 1),
        ("off", "on", 2),
    ]


@pytest.mark.parametrize("source", [UwtSource.UNKNOWN, UwtSource.CONFLICT])
def test_an_observation_without_mode_records_nothing(
    source: UwtSource, caplog: pytest.LogCaptureFixture
) -> None:
    """No change, and the reason is named once per reason.

    A conflict between frame type and flags bit is its own source, not
    "unknown"; the DEBUG line is written when the reason changes, not once
    per advertisement.
    """
    resolver = _resolver()
    _observe(resolver, True, 10.0)
    with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
        _observe(resolver, None, 20.0, source=source)
        _observe(resolver, None, 30.0, source=source)
    assert len(_events(resolver)) == 1
    skipped = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("uwt_observation_skipped ")
    ]
    assert skipped == [
        f"uwt_observation_skipped device=reg-dev-1 canonical=dev-1 "
        f"source={source.value}"
    ]


def test_a_known_mode_after_a_skip_resets_the_skip_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _resolver()
    with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
        _observe(resolver, None, 10.0, source=UwtSource.CONFLICT)
        _observe(resolver, True, 20.0)
        _observe(resolver, None, 30.0, source=UwtSource.CONFLICT)
    skipped = [
        r
        for r in caplog.records
        if r.getMessage().startswith("uwt_observation_skipped")
    ]
    assert len(skipped) == 2


def test_an_older_observation_records_nothing_a_newer_one_does() -> None:
    """History replay may deliver sightings out of order."""
    resolver = _resolver()
    _observe(resolver, False, 100.0)
    _observe(resolver, True, 50.0)
    assert [e.new for e in _events(resolver)] == ["off"]
    _observe(resolver, True, 150.0)
    assert [e.new for e in _events(resolver)] == ["off", "on"]


def test_prev_seen_age_is_measured_to_the_last_sighting_of_the_old_mode() -> None:
    resolver = _resolver()
    _observe(resolver, False, 100.0)
    _observe(resolver, False, 160.0)
    _observe(resolver, True, 200.0)
    first, second = _events(resolver)
    assert first.prev_seen_age_s is None
    assert second.prev_seen_age_s == 40


# ---------------------------------------------------------------------------
# Separation per entry and per device
# ---------------------------------------------------------------------------


def test_one_canonical_id_in_two_entries_keeps_two_histories() -> None:
    """A tracker shared between two accounts: one track and history each."""
    resolver = _resolver()
    _observe(resolver, False, 10.0, matches=[_match("entry-a"), _match("entry-b")])
    _observe(resolver, True, 20.0, matches=[_match("entry-a")])
    assert [(e.old, e.new) for e in _events(resolver, "entry-a")] == [
        ("unknown", "off"),
        ("off", "on"),
    ]
    assert [(e.old, e.new) for e in _events(resolver, "entry-b")] == [
        ("unknown", "off")
    ]


def test_the_history_is_bounded_per_entry() -> None:
    resolver = _resolver()
    for step in range(201):
        _observe(resolver, step % 2 == 0, float(step))
    _observe(resolver, True, 1.0, matches=[_match("entry-b")])
    a = resolver.uwt_transition_snapshot("entry-a")
    b = resolver.uwt_transition_snapshot("entry-b")
    assert (len(a.events), a.dropped, a.capacity) == (200, 1, 200)
    assert a.events[0].seq == 2
    assert (len(b.events), b.dropped) == (1, 0)


def test_an_entry_without_history_has_an_empty_snapshot() -> None:
    snapshot = _resolver().uwt_transition_snapshot("entry-none")
    assert snapshot.events == ()
    assert (snapshot.dropped, snapshot.info_lines_per_device_hour) == (0, 12)


def test_snapshot_on_a_resolver_built_without_init_is_empty() -> None:
    bare = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    assert bare.uwt_transition_snapshot("entry-a").events == ()


# ---------------------------------------------------------------------------
# poll_age_s
# ---------------------------------------------------------------------------


def test_poll_age_comes_from_the_own_entry_only() -> None:
    resolver = _resolver(
        {
            "entry-a": SimpleNamespace(coordinator=_Coordinator()),
            "entry-b": SimpleNamespace(coordinator=_Coordinator({"dev-1": 5.0})),
        }
    )
    _observe(resolver, True, 65.0, matches=[_match("entry-a"), _match("entry-b")])
    assert _events(resolver, "entry-a")[0].poll_age_s is None
    assert _events(resolver, "entry-b")[0].poll_age_s == 60


def test_poll_age_reads_a_bare_coordinator_as_well() -> None:
    resolver = _resolver({"entry-a": _Coordinator({"dev-1": 1.5})})
    _observe(resolver, True, 10.0)
    assert _events(resolver)[0].poll_age_s == 8


def test_a_request_after_the_observation_gives_no_poll_age(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The advertisement clock may lag ``time.monotonic``."""
    resolver = _resolver({"entry-a": _Coordinator({"dev-1": 12.0})})
    with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
        _observe(resolver, True, 10.0)
    assert _events(resolver)[0].poll_age_s is None
    assert any(
        r.getMessage()
        == "uwt_poll_age_unavailable device=reg-dev-1 reason=poll_after_observation"
        for r in caplog.records
    )


@pytest.mark.parametrize(
    "bucket",
    [None, {"entries": None}, {"entries": {}}, {"entries": {"entry-a": object()}}],
    ids=["no-bucket", "no-entries", "entry-missing", "no-clock"],
)
def test_poll_age_without_a_usable_coordinator_is_none(bucket: object) -> None:
    resolver = _make_resolver()
    if bucket is not None:
        resolver.hass.data[DOMAIN] = bucket
    _observe(resolver, True, 10.0)
    assert _events(resolver)[0].poll_age_s is None


def test_a_non_numeric_stamp_is_ignored() -> None:
    class _Odd:
        def locate_requested_mono(self, device_id: str) -> object:
            return True

    resolver = _resolver({"entry-a": _Odd()})
    _observe(resolver, True, 10.0)
    assert _events(resolver)[0].poll_age_s is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def _identity(entry: str, canonical: str, registry: str | None = None) -> object:
    return SimpleNamespace(
        config_entry_id=entry, canonical_id=canonical, registry_id=registry
    )


@pytest.mark.asyncio
async def test_refresh_drops_gone_devices_and_unloaded_entries() -> None:
    resolver = _resolver()
    _observe(resolver, True, 10.0, matches=[_match("entry-a", "dev-1")])
    _observe(resolver, True, 10.0, matches=[_match("entry-a", "dev-2")])
    _observe(resolver, True, 10.0, matches=[_match("entry-b", "dev-3")])

    async def _refresh_cache() -> None:
        resolver._cached_identities = [
            _identity("entry-a", "entry-a:dev-1"),
            _identity("", "dev-9"),
        ]

    with patch.object(
        GoogleFindMyEIDResolver, "_refresh_cache", new=lambda self: _refresh_cache()
    ):
        await resolver.async_refresh()

    assert set(resolver._uwt_track) == {("entry-a", "dev-1")}
    assert set(resolver._uwt_events) == {"entry-a"}


def test_a_track_kept_by_registry_id_survives_the_purge() -> None:
    resolver = _resolver()
    match = EIDMatch("reg-x", "entry-a", "", 0, False)
    _observe(resolver, True, 10.0, matches=[match])
    resolver._cached_identities = [_identity("entry-a", "", "reg-x")]
    resolver._purge_uwt_state()
    assert set(resolver._uwt_track) == {("entry-a", "reg-x")}


def test_stop_clears_the_channel() -> None:
    resolver = _resolver()
    _observe(resolver, True, 10.0)
    resolver.stop()
    assert resolver._uwt_track == {}
    assert resolver.uwt_transition_snapshot("entry-a").events == ()


# ---------------------------------------------------------------------------
# Through resolve_eid
# ---------------------------------------------------------------------------

_HEADER = b"\x00" * 7


def _resolver_knowing(marker: int) -> GoogleFindMyEIDResolver:
    resolver = _resolver()
    eid = bytes([marker]) * 20
    resolver._lookup[eid] = [_match()]
    resolver._lookup_metadata[eid] = {
        "variant": EidVariant.LEGACY_SECP160R1_X20_BE.value,
        "rotation_timestamp": 7 * 1024,
        "timestamp_basis": "pair_date",
        "flags_xor_mask": 0x00,
    }
    return resolver


def _feed(resolver: GoogleFindMyEIDResolver, payload: bytes, mono: float) -> None:
    with (
        patch("time.time", return_value=_WALL0 + mono),
        patch("time.monotonic", return_value=mono),
    ):
        assert resolver.resolve_eid(payload, observed_at=mono)


def test_a_matched_advertisement_feeds_the_channel() -> None:
    resolver = _resolver_knowing(0x71)
    _feed(resolver, _HEADER + bytes([FMDN_FRAME_TYPE]) + b"\x71" * 20 + b"\x00", 10.0)
    _feed(resolver, _HEADER + bytes([MODERN_FRAME_TYPE]) + b"\x71" * 20, 20.0)
    events = _events(resolver)
    assert [(e.old, e.new, e.source, e.flags, e.frame) for e in events] == [
        ("unknown", "off", "flags_bit", "framed", "0x40"),
        ("off", "on", "frame_type", "absent", "0x41"),
    ]


def test_a_failing_channel_does_not_fail_the_match(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _resolver_knowing(0x71)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("channel broken")

    with (
        patch.object(GoogleFindMyEIDResolver, "_note_uwt_observation", new=_boom),
        caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER),
    ):
        _feed(resolver, _HEADER + bytes([FMDN_FRAME_TYPE]) + b"\x71" * 20, 10.0)
    assert resolver.last_confirmed_sighting("reg-dev-1") is not None
    assert any(
        r.getMessage() == "uwt_transition_failed error_type=RuntimeError"
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# FMDN_UWT_TRANSITION
# ---------------------------------------------------------------------------

_LINE = re.compile(
    r"^FMDN_UWT_TRANSITION kind=(change|resync) device=\S+ canonical=\S+ "
    r"old=(on|off|unknown) new=(on|off) source=(flags_bit|frame_type) "
    r"flags=(framed|guessed|absent) frame=(0x40|0x41|none) "
    r"decoded=(0x[0-9a-f]{2}|none) payload_len=\d+ poll_age_s=(\d+|none) "
    r"prev_seen_age_s=(\d+|none) observed=\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ "
    r"seq=\d+ suppressed=\d+$"
)


def _lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == _RESOLVER_LOGGER
        and r.getMessage().startswith("FMDN_UWT_TRANSITION ")
    ]


def test_the_line_has_the_documented_form(caplog: pytest.LogCaptureFixture) -> None:
    resolver = _resolver({"entry-a": _Coordinator({"dev-1": 4.0})})
    with caplog.at_level(logging.INFO, logger=_RESOLVER_LOGGER):
        _observe(resolver, False, 10.0, frame_type=FMDN_FRAME_TYPE)
        _observe(
            resolver,
            True,
            70.0,
            source=UwtSource.FRAME_TYPE,
            reading=_FlagsReading(None, "absent"),
        )
    (level1, first), (level2, second) = _lines(caplog)
    assert (level1, level2) == (logging.INFO, logging.INFO)
    assert _LINE.match(first) and _LINE.match(second)
    assert first == (
        "FMDN_UWT_TRANSITION kind=change device=reg-dev-1 canonical=dev-1 "
        "old=unknown new=off source=flags_bit flags=framed frame=0x40 "
        "decoded=0x05 payload_len=29 poll_age_s=6 prev_seen_age_s=none "
        "observed=2027-01-15T08:00:10Z seq=1 suppressed=0"
    )
    assert second == (
        "FMDN_UWT_TRANSITION kind=change device=reg-dev-1 canonical=dev-1 "
        "old=off new=on source=frame_type flags=absent frame=0x41 "
        "decoded=none payload_len=29 poll_age_s=66 prev_seen_age_s=60 "
        "observed=2027-01-15T08:01:10Z seq=2 suppressed=0"
    )


def test_two_entries_write_two_lines(caplog: pytest.LogCaptureFixture) -> None:
    resolver = _resolver()
    with caplog.at_level(logging.INFO, logger=_RESOLVER_LOGGER):
        _observe(resolver, True, 10.0, matches=[_match("entry-a"), _match("entry-b")])
    assert len(_lines(caplog)) == 2


def _flap(resolver: GoogleFindMyEIDResolver, count: int, start: float = 0.0) -> None:
    for step in range(count):
        _observe(resolver, step % 2 == 0, start + 60.0 * step)


def test_the_thirteenth_change_of_an_hour_goes_to_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _resolver()
    with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
        _flap(resolver, 13)
    levels = [level for level, _ in _lines(caplog)]
    assert levels == [logging.INFO] * 12 + [logging.DEBUG]
    assert _lines(caplog)[-1][1].endswith("seq=13 suppressed=1")
    assert len(_events(resolver)) == 13


def test_after_the_hour_a_resync_line_closes_the_throttled_stretch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Once the budget is free, the next sighting closes the throttled
    stretch with the end state."""
    resolver = _resolver()
    _flap(resolver, 13)
    caplog.clear()
    # The 13th change (index 12) showed "on"; one hour after the first INFO
    # line the budget is free again, and the next observation repeats "on".
    with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
        _observe(resolver, True, 3600.0)
        _observe(resolver, True, 3610.0)
    lines = _lines(caplog)
    assert len(lines) == 1
    level, line = lines[0]
    assert level == logging.INFO
    assert _LINE.match(line)
    assert " kind=resync " in line and " old=on new=on " in line
    assert line.endswith("seq=14 suppressed=1")
    assert _events(resolver)[-1].kind == "resync"


def test_no_resync_while_the_budget_is_still_used_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _resolver()
    _flap(resolver, 13)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
        _observe(resolver, True, 3500.0)
    assert _lines(caplog) == []


def test_a_change_after_the_hour_carries_the_suppressed_count(
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver = _resolver()
    _flap(resolver, 14)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
        _observe(resolver, True, 3700.0)
    ((level, line),) = _lines(caplog)
    assert level == logging.INFO
    assert " kind=change " in line and " old=off new=on " in line
    assert line.endswith("suppressed=2")


# ---------------------------------------------------------------------------
# Log hygiene (AGENTS.md section 5 (b), "Log hygiene for scanners")
# ---------------------------------------------------------------------------

_RESOLVER_SOURCE = Path(eid_resolver.__file__)
_SEMGREP_WORDS = re.compile(r"(?i)(api.key|secret|credential|token|password)")
_ALLOWED_ARGS = re.compile(
    r"^(_UWT_TRANSITION_FORMAT|level|suppressed|match\.(device_id|canonical_id)"
    r"|event\.[a-z_]+|.*decoded.*|.*event\.(poll_age_s|prev_seen_age_s).*)$"
)


def test_transition_line_carries_no_identifying_or_flagged_values() -> None:
    """The format string and the arguments of the transition line stay clean.

    The first source line of the format literal carries no word the Semgrep
    rule ``python-logger-credential-disclosure`` reacts to, and the arguments
    are only the class (b) identifiers of the match, fields of the recorded
    event (fixed labels, numbers, the UTC time), the decoded flags byte and
    the suppressed count: no name, entity id, address or coordinate.

    Retirement condition: the transition line is lowered to DEBUG or removed.
    """
    source = _RESOLVER_SOURCE.read_text()
    tree = ast.parse(source)
    lines = source.splitlines()
    (assign,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_UWT_TRANSITION_FORMAT"
            for t in node.targets
        )
    ]
    first_literal_line = lines[assign.value.lineno - 1]
    if "(" in first_literal_line and '"' not in first_literal_line:
        first_literal_line = lines[assign.value.lineno]
    assert "FMDN_UWT_TRANSITION" in first_literal_line
    assert not _SEMGREP_WORDS.search(first_literal_line)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and any(
            isinstance(arg, ast.Name) and arg.id == "_UWT_TRANSITION_FORMAT"
            for arg in node.args
        )
    ]
    assert len(calls) == 1
    for arg in calls[0].args:
        assert _ALLOWED_ARGS.match(ast.unparse(arg)), ast.unparse(arg)


def test_readme_quotes_the_line_the_code_writes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The README example starts like the line the code really writes.

    The line is rendered through the real code path, and its prefix up to the
    first variable field is looked up in a code block of the README section
    (AGENTS.md: quoted program output is covered by a test).

    Retirement condition: the README section is removed.
    """
    resolver = _resolver()
    with caplog.at_level(logging.INFO, logger=_RESOLVER_LOGGER):
        _observe(resolver, True, 10.0)
    ((_, line),) = _lines(caplog)
    prefix = line.split(" device=")[0].rsplit("=", 1)[0] + "="
    assert prefix == "FMDN_UWT_TRANSITION kind="

    readme = (_RESOLVER_SOURCE.parents[2] / "README.md").read_text()
    section = readme.split(
        "### Reporting unwanted tracking protection mode changes", 1
    )[1].split("\n## ", 1)[0]
    code_blocks = re.findall(r"```[a-z]*\n(.*?)```", section, flags=re.DOTALL)
    assert any(
        block_line.strip().startswith(prefix)
        for block in code_blocks
        for block_line in block.splitlines()
    )
    assert "custom_components.googlefindmy.eid_resolver: info" in section
