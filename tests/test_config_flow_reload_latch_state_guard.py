# tests/test_config_flow_reload_latch_state_guard.py
"""The reload latch is not claimed for an entry that can never reach a setup.

Claiming the shared latch is a promise to reload, and its release points
(unload, setup, entry removal) all presuppose that a reload actually arrives.
Where it cannot, the promise must not be made: ``async_schedule_reload`` hands
the reload to a core-owned task it neither returns nor exposes, so nothing would
release the latch and every later reload of that entry would stand down with its
credentials ineffective.

Two properties carry these tests and are easy to break by accident:

* the gate is a **positive list of terminal states**, not the negation of an "is
  recoverable" property -- ``SETUP_IN_PROGRESS`` and ``UNLOAD_IN_PROGRESS`` are
  reported as non-recoverable too but heal within seconds, and dropping their
  reload would be the very damage the latch exists to prevent;
* the gate **fails open** on every doubt, because one reload too many is a
  nuisance while a missing one leaves written credentials ineffective.

Every double records what it was asked for, and every test whose verdict depends
on a lookup asserts on that recorder: a manager without ``async_get_entry`` makes
the gate fall through to "not hopeless" silently, so such a test could otherwise
be green while measuring nothing. The fail-open tests are weaker by nature -- they
assert the outcome the code would also produce without any gate at all -- which is
why they assert on the recorder too wherever a resolver was consulted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.googlefindmy import config_flow
from tests.helpers.config_entries_stub import make_config_entry


class _RecordingEntries:
    """Entry manager double recording resolutions and scheduled reloads."""

    def __init__(self, entry: Any | None, *, raises: bool = False) -> None:
        self._entry = entry
        self._raises = raises
        self.resolved: list[str] = []
        self.scheduled: list[str] = []

    def async_get_entry(self, entry_id: str) -> Any | None:
        """Resolve ``entry_id`` and record that the gate asked for it."""

        self.resolved.append(entry_id)
        if self._raises:
            raise RuntimeError("entry registry unavailable")
        return self._entry

    def async_schedule_reload(self, entry_id: str) -> None:
        """Record the reload the flow scheduled."""

        self.scheduled.append(entry_id)


class _EntriesWithoutResolver:
    """Entry manager double that cannot resolve entries at all."""

    def __init__(self) -> None:
        self.scheduled: list[str] = []

    def async_schedule_reload(self, entry_id: str) -> None:
        """Record the reload the flow scheduled."""

        self.scheduled.append(entry_id)


def _hass_with_latch(entries: Any) -> Any:
    """A hass double carrying a real ``data`` bucket, where the latch lives.

    Same shape as ``_reconfigure_flow_with_latch`` in
    ``tests/test_config_flow_reconfigure.py``: without ``data`` the latch helper
    falls back to "reload anyway" and nothing about the latch could be observed.
    """

    hass = SimpleNamespace()
    hass.data = {}
    hass.config_entries = entries
    return hass


def _latch_is_free(hass: Any, entry_id: str) -> bool:
    """Whether the shared reload latch of ``entry_id`` can be claimed again.

    Claims the latch as a side effect, so this belongs at the end of a test.
    """

    integration = config_flow.import_integration_package()
    return bool(integration.claim_pending_entry_reload(hass, entry_id))


# --- terminal states: no claim, no reload -----------------------------------


@pytest.mark.parametrize(
    ("test_id", "state"),
    [
        ("T-M1", "migration_error"),
        ("T-M2", "failed_unload"),
    ],
)
def test_a_terminal_entry_is_not_claimed_for_a_reload(test_id: str, state: str) -> None:
    """T-M1/T-M2: a reload that could only raise is not even promised."""

    entry = make_config_entry(entry_id=f"entry-{test_id.lower()}", state=state)
    entries = _RecordingEntries(entry)
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, entry.entry_id) is False
    assert entries.scheduled == [], "no reload may be scheduled for a terminal entry"
    assert entries.resolved == [entry.entry_id], (
        "the gate has to ask; a double without a resolver would let it fall "
        "through unnoticed"
    )
    assert _latch_is_free(hass, entry.entry_id), (
        "the latch was never claimed, so it is still there for the next writer"
    )


# --- transient states: the reload still happens -----------------------------


@pytest.mark.parametrize(
    ("test_id", "state"),
    [
        ("T-M3", "setup_in_progress"),
        ("T-M4", "unload_in_progress"),
    ],
)
def test_a_transient_entry_still_gets_its_reload(test_id: str, state: str) -> None:
    """T-M3/T-M4: non-recoverable is not the same as terminal.

    These two states report ``recoverable is False`` in Home Assistant, but they
    heal on their own: ``async_reload`` waits on ``entry.setup_lock``, which the
    running lifecycle operation holds. Standing down here would drop a legitimate
    reload. The test does not pin that self-healing -- it cannot, there is no core
    scheduler in a double -- it pins that these names stay out of the set.
    """

    entry = make_config_entry(entry_id=f"entry-{test_id.lower()}", state=state)
    entries = _RecordingEntries(entry)
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, entry.entry_id) is True
    assert entries.scheduled == [entry.entry_id]


# --- fail-open branches -----------------------------------------------------


def test_an_unresolvable_entry_still_schedules_its_reload() -> None:
    """T-M5: no entry, no information -- carry on rather than stand down."""

    entries = _RecordingEntries(None)
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, "entry-t-m5") is True
    assert entries.scheduled == ["entry-t-m5"]
    assert entries.resolved == ["entry-t-m5"]


def test_a_resolver_that_raises_still_schedules_its_reload() -> None:
    """T-M6: bookkeeping must never block a reload."""

    entries = _RecordingEntries(None, raises=True)
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, "entry-t-m6") is True
    assert entries.scheduled == ["entry-t-m6"]
    assert entries.resolved, "the gate did try to resolve before giving up"


def test_an_entry_without_a_state_still_schedules_its_reload() -> None:
    """T-M7: an entry object that does not carry a state tells us nothing."""

    entry = make_config_entry(entry_id="entry-t-m7")
    delattr(entry, "state")
    entries = _RecordingEntries(entry)
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, entry.entry_id) is True
    assert entries.scheduled == [entry.entry_id]
    assert entries.resolved == [entry.entry_id], (
        "the gate did look, and found nothing to judge"
    )


def test_an_unknown_future_state_still_schedules_its_reload() -> None:
    """T-M9: a state Home Assistant may add later keeps today's behaviour."""

    entry = make_config_entry(entry_id="entry-t-m9", state="quantum_error")
    entries = _RecordingEntries(entry)
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, entry.entry_id) is True
    assert entries.scheduled == [entry.entry_id]
    assert entries.resolved == [entry.entry_id], "the state was read, not skipped"


def test_an_empty_entry_id_still_schedules_its_reload() -> None:
    """T-M12: without an id there is nothing to look up and nothing to judge."""

    entries = _RecordingEntries(make_config_entry(state="failed_unload"))
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, "") is True
    assert entries.resolved == [], "an empty id must not reach the resolver"
    assert entries.scheduled == [""]


def test_a_manager_without_a_resolver_still_schedules_its_reload() -> None:
    """T-M13: an entry manager that cannot resolve is not evidence of anything."""

    entries = _EntriesWithoutResolver()
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, "entry-t-m13") is True
    assert entries.scheduled == ["entry-t-m13"]


# --- comparison form and set contents ---------------------------------------


def test_a_string_valued_state_is_compared_correctly() -> None:
    """T-M8: the test stub stores states as strings, the core as enum members.

    The production code compares against the symbols imported from
    ``homeassistant.config_entries``, which resolves to the same world the test
    sees. A comparison against ``state.name`` would raise on this string double
    instead of quietly answering wrong -- which is why the ``try`` around the
    lookup is deliberately narrow and does not wrap the comparison.
    """

    terminal = make_config_entry(entry_id="entry-t-m8-terminal", state="failed_unload")
    healthy = make_config_entry(entry_id="entry-t-m8-loaded", state="loaded")

    assert isinstance(terminal.state, str)
    assert config_flow._entry_reload_is_hopeless(
        _hass_with_latch(_RecordingEntries(terminal)), terminal.entry_id
    )
    assert not config_flow._entry_reload_is_hopeless(
        _hass_with_latch(_RecordingEntries(healthy)), healthy.entry_id
    )


def test_the_flow_reaches_the_same_verdict_for_every_terminal_state() -> None:
    """T-M11: the set's members really match the entries this flow sees.

    The set itself, its size and the deliberate absence of the transient states
    are pinned once, in ``tests/test_entry_reload_gate.py`` where the set lives.
    What has to be pinned *here* is the other half: a set of two symbols is
    worthless if the entries reaching this flow never equal them -- that is
    exactly how an earlier draft of this guard stayed green while its sibling
    tests were red. So each member is fed back through the flow's predicate on a
    real ``make_config_entry`` entry.
    """

    for name in ("MIGRATION_ERROR", "FAILED_UNLOAD"):
        value = getattr(ConfigEntryState, name)
        entry = make_config_entry(state=value)
        entries = _RecordingEntries(entry)
        assert config_flow._entry_reload_is_hopeless(
            _hass_with_latch(entries), entry.entry_id
        ), (
            f"an entry whose state is {value!r} is not recognised as terminal; "
            "the set is built in the gate module and has to resolve in the "
            "same world the entries live in"
        )


# --- order and the two invisible cases --------------------------------------


def test_the_terminal_check_runs_before_the_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-M10: a hopeless entry must not burn the latch on the way to the verdict."""

    claims: list[str] = []

    def _record_claim(hass: Any, entry_id: str) -> bool:
        claims.append(entry_id)
        return True

    monkeypatch.setattr(config_flow, "_claim_entry_reload", _record_claim)

    entry = make_config_entry(entry_id="entry-t-m10", state="failed_unload")
    entries = _RecordingEntries(entry)

    assert (
        config_flow._schedule_claimed_reload(_hass_with_latch(entries), entry.entry_id)
        is False
    )
    assert claims == [], "the state check has to run before the claim, not after"


def test_a_disabled_entry_is_not_claimed_for_a_reload() -> None:
    """T-M14: a disabled entry is unloaded and never set up again.

    Invisible from the outside: the state stays recoverable (``not_loaded``) and
    ``async_reload`` hands back a truthy unload result, so neither the state nor
    the return value distinguishes this from a reload that landed.
    """

    entry = make_config_entry(
        entry_id="entry-t-m14", state="not_loaded", disabled_by="user"
    )
    entries = _RecordingEntries(entry)
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, entry.entry_id) is False
    assert entries.scheduled == []
    assert entries.resolved == [entry.entry_id]
    assert _latch_is_free(hass, entry.entry_id)


def test_a_supplied_entry_is_judged_without_asking_the_manager() -> None:
    """T-M16: the caller may hand in the entry it already holds.

    That is not just saved work. The gate fails open when it cannot resolve, so
    a manager double *without* ``async_get_entry`` turns it into a silent no-op
    -- a test against such a double would be green while measuring nothing. A
    caller that passes its entry does not depend on whether a given manager
    happens to offer the lookup. ``PLAN_GFMY_RELOAD_CLAIM_DEAD_ENDS`` builds its
    own gate on exactly this path, so it must not go untested here.
    """

    entry = make_config_entry(entry_id="entry-t-m16", state="failed_unload")
    entries = _EntriesWithoutResolver()
    hass = _hass_with_latch(entries)

    assert config_flow._entry_reload_is_hopeless(hass, entry.entry_id, entry) is True
    assert not hasattr(entries, "async_get_entry"), (
        "the point of this test is a manager that could not have answered"
    )

    healthy = make_config_entry(entry_id="entry-t-m16-ok", state="loaded")
    assert (
        config_flow._entry_reload_is_hopeless(hass, healthy.entry_id, healthy) is False
    )


def test_an_ignored_entry_is_not_claimed_for_a_reload() -> None:
    """T-M15: ``ConfigEntry.async_setup`` bails out for a ``SOURCE_IGNORE`` entry.

    The same half of the same core condition as the disabled case
    (``if self.source == SOURCE_IGNORE or self.disabled_by: return``), and just
    as invisible. Whether our own paths can reach an ignored entry is *not*
    measured; the branch is set because it closes the other half of a condition
    we already rely on.
    """

    entry = make_config_entry(
        entry_id="entry-t-m15", state="not_loaded", source="ignore"
    )
    entries = _RecordingEntries(entry)
    hass = _hass_with_latch(entries)

    assert config_flow._schedule_claimed_reload(hass, entry.entry_id) is False
    assert entries.scheduled == []
    assert _latch_is_free(hass, entry.entry_id)
