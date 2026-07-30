# tests/test_entry_reload_gate.py
"""The state gate is a leaf module both reload claimants can reach.

`config_flow` and the tracker registry self-heal in `device_tracker` ask the
same two questions about an entry and the shared latch, so the answers live in
`entry_reload_gate` rather than in either caller. These tests pin the module
itself; the flow-level behaviour stays where it is, in
`tests/test_config_flow_reload_latch_state_guard.py`.

Three properties carry them and are easy to break by accident:

* the terminal set is a **positive list**, not the negation of an "is
  recoverable" property -- `SETUP_IN_PROGRESS` and `UNLOAD_IN_PROGRESS` are
  reported as non-recoverable too but heal within seconds;
* every doubt **fails open**, because one reload too many is a nuisance while a
  missing one leaves written credentials ineffective;
* the enum is bound through the **submodule** form, the same world the entries
  under test live in. The package-attribute form is a different object under the
  stub, and a comparison against it would silently never match.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.googlefindmy import entry_reload_gate
from tests.helpers.config_entries_stub import make_config_entry


class _RecordingEntries:
    """Entry manager double that records every lookup it was asked for."""

    def __init__(self, entry: Any = None, *, raises: bool = False) -> None:
        self._entry = entry
        self._raises = raises
        self.lookups: list[str] = []

    def async_get_entry(self, entry_id: str) -> Any:
        self.lookups.append(entry_id)
        if self._raises:
            raise RuntimeError("entry manager unavailable")
        return self._entry


def _hass(entries: Any = None) -> SimpleNamespace:
    return SimpleNamespace(config_entries=entries)


# --- the three hopeless cases -------------------------------------------------


@pytest.mark.parametrize("name", ["MIGRATION_ERROR", "FAILED_UNLOAD"])
def test_a_terminal_state_is_hopeless(name: str) -> None:
    """T-G1: a reload of a terminal entry can only raise."""

    entry = make_config_entry(state=getattr(ConfigEntryState, name))
    entries = _RecordingEntries(entry)

    assert entry_reload_gate.entry_reload_is_hopeless(_hass(entries), entry.entry_id)
    assert entries.lookups == [entry.entry_id]


@pytest.mark.parametrize("name", ["SETUP_IN_PROGRESS", "UNLOAD_IN_PROGRESS"])
def test_a_transient_state_is_not_hopeless(name: str) -> None:
    """T-G2: non-recoverable is not the same as terminal.

    Both states heal within seconds because `async_reload` waits on
    `entry.setup_lock`. Standing down for them would drop a legitimate reload
    together with the credentials it was meant to activate.
    """

    entry = make_config_entry(state=getattr(ConfigEntryState, name))
    entries = _RecordingEntries(entry)

    assert not entry_reload_gate.entry_reload_is_hopeless(
        _hass(entries), entry.entry_id
    )


def test_a_disabled_entry_is_hopeless() -> None:
    """T-G3: `async_reload` returns right after the unload, setup never runs.

    Invisible from the outside: the state stays recoverable and the result is
    truthy, so only the flag tells this apart from a reload that landed.
    """

    entry = make_config_entry(state=ConfigEntryState.LOADED, disabled_by="user")

    assert entry_reload_gate.entry_reload_is_hopeless(
        _hass(_RecordingEntries(entry)), entry.entry_id
    )


def test_an_ignored_entry_is_hopeless() -> None:
    """T-G4: `ConfigEntry.async_setup` bails out on its very first statement."""

    entry = make_config_entry(state=ConfigEntryState.LOADED, source="ignore")

    assert entry_reload_gate.entry_reload_is_hopeless(
        _hass(_RecordingEntries(entry)), entry.entry_id
    )


# --- fail-open and the supplied entry ----------------------------------------


def test_every_doubt_fails_open() -> None:
    """T-G5: six ways to know nothing, and none of them stands down.

    Each case asserts on the recorder wherever a resolver existed: without that
    a test could be green while the gate never looked at anything at all.
    """

    # 1. an empty entry id never reaches the manager
    empty_entries = _RecordingEntries(make_config_entry(state=ConfigEntryState.LOADED))
    assert not entry_reload_gate.entry_reload_is_hopeless(_hass(empty_entries), "")
    assert empty_entries.lookups == []

    # 2. no entry manager at all
    assert not entry_reload_gate.entry_reload_is_hopeless(_hass(None), "entry-1")

    # 3. a manager without the resolver
    assert not entry_reload_gate.entry_reload_is_hopeless(
        _hass(SimpleNamespace()), "entry-1"
    )

    # 4. a resolver that raises
    raising = _RecordingEntries(raises=True)
    assert not entry_reload_gate.entry_reload_is_hopeless(_hass(raising), "entry-1")
    assert raising.lookups == ["entry-1"]

    # 5. a resolver that answers None
    unknown = _RecordingEntries(None)
    assert not entry_reload_gate.entry_reload_is_hopeless(_hass(unknown), "entry-1")
    assert unknown.lookups == ["entry-1"]

    # 6. an entry without a state
    stateless = make_config_entry(state=None)
    assert not entry_reload_gate.entry_reload_is_hopeless(
        _hass(_RecordingEntries(stateless)), stateless.entry_id
    )


def test_an_unknown_future_state_is_not_hopeless() -> None:
    """A state nobody knows keeps today's behaviour instead of standing down."""

    entry = make_config_entry(state="a_state_that_does_not_exist_yet")

    assert not entry_reload_gate.entry_reload_is_hopeless(
        _hass(_RecordingEntries(entry)), entry.entry_id
    )


def test_a_supplied_entry_is_judged_without_asking_the_manager() -> None:
    """T-G6: the caller that already holds the entry needs no entry manager.

    That is not an optimisation. The tracker registry self-heal holds its
    `ConfigEntry` and its platform double carries no manager at all, so a gate
    that insisted on resolving would fail open there and gate nothing.
    """

    entry = make_config_entry(state=ConfigEntryState.FAILED_UNLOAD)
    entries = _RecordingEntries(None)

    assert entry_reload_gate.entry_reload_is_hopeless(
        _hass(entries), entry.entry_id, entry
    )
    assert entries.lookups == [], "the held entry makes the lookup unnecessary"
    assert entry_reload_gate.entry_reload_is_hopeless(
        _hass(None), entry.entry_id, entry
    )


# --- the set itself -----------------------------------------------------------


def test_every_terminal_state_name_resolved() -> None:
    """T-G7: the set did not silently run empty, and its members really match.

    Resolving the names is fail-open by design: an upstream rename shrinks the
    set rather than breaking the flow, and without this test that shrinking
    would go unnoticed. The second half is the load-bearing one -- two symbols
    are worthless if the entries the code actually sees never equal them.
    """

    assert len(entry_reload_gate.TERMINAL_ENTRY_RELOAD_STATES) == 2, (
        "a name that no longer resolves drops out of the set; the gate then "
        "quietly stops guarding"
    )

    for name in ("MIGRATION_ERROR", "FAILED_UNLOAD"):
        value = getattr(ConfigEntryState, name)
        entry = make_config_entry(state=value)
        assert entry_reload_gate.entry_reload_is_hopeless(
            _hass(_RecordingEntries(entry)), entry.entry_id
        ), f"an entry whose state is {value!r} is not recognised as terminal"

    for name in ("SETUP_IN_PROGRESS", "UNLOAD_IN_PROGRESS"):
        transient = getattr(ConfigEntryState, name)
        assert transient not in entry_reload_gate.TERMINAL_ENTRY_RELOAD_STATES, (
            f"{name} is resolvable on the same symbol and is left out on purpose"
        )


def test_the_enum_binding_survives_the_module_boundary() -> None:
    """The module binds the same `ConfigEntryState` object the tests see.

    Before the move, set and comparison sat in one file behind one import, so
    this held by construction. Now they are one import away from every caller,
    and a switch to `config_entries.ConfigEntryState` would compare against a
    different object under the stub, where nothing would ever match again.

    Scope, stated honestly: this pins the module's **binding**, not its use. A
    change that leaves the import in place (the annotation needs it) and only
    rewrites the two comparison sites would keep this test green. What catches
    that is the behavioural half -- `test_a_terminal_state_is_hopeless` and
    `test_every_terminal_state_name_resolved` feed real entries back through the
    predicate, and `test_a_setup_that_never_reached_our_hook_leaves_the_claim`
    does the same for the `MIGRATION_ERROR` comparison in the classifier. This
    test is the cheap tripwire in front of them, not their replacement.
    """

    assert entry_reload_gate.ConfigEntryState is ConfigEntryState
    for name in ("MIGRATION_ERROR", "FAILED_UNLOAD"):
        assert (
            getattr(ConfigEntryState, name)
            in entry_reload_gate.TERMINAL_ENTRY_RELOAD_STATES
        ), (
            f"{name} resolved on this side but not into the set; the two "
            "bindings have drifted apart"
        )


# --- the falsy-result classifier ----------------------------------------------


def test_a_loaded_component_means_a_lifecycle_hook_released_the_latch() -> None:
    """A falsy reload with the domain still loaded is not a dead end.

    Releasing there would discard a claim someone else may have taken since our
    own hook handed it back, which is the very double reload the latch prevents.
    """

    entry = make_config_entry(domain="googlefindmy", state=ConfigEntryState.SETUP_ERROR)
    hass = SimpleNamespace(
        config_entries=_RecordingEntries(entry),
        config=SimpleNamespace(components={"googlefindmy"}),
    )

    assert not entry_reload_gate.falsy_reload_left_the_latch_behind(
        hass, entry.entry_id
    )


def test_an_unloadable_component_leaves_the_claim_standing() -> None:
    """The reload took the short circuit, so neither of our hooks ran."""

    entry = make_config_entry(domain="googlefindmy", state=ConfigEntryState.NOT_LOADED)
    hass = SimpleNamespace(
        config_entries=_RecordingEntries(entry),
        config=SimpleNamespace(components=set()),
    )

    assert entry_reload_gate.falsy_reload_left_the_latch_behind(hass, entry.entry_id)


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"source": "ignore"}, "async_setup returns on its first statement"),
        (
            {"state": ConfigEntryState.MIGRATION_ERROR},
            "async_setup bails before our hook",
        ),
    ],
)
def test_a_setup_that_never_reached_our_hook_leaves_the_claim(
    kwargs: dict[str, Any], reason: str
) -> None:
    """Both exits keep the domain among the loaded components.

    The component check alone would therefore misread them as released.
    """

    entry = make_config_entry(domain="googlefindmy", **kwargs)
    hass = SimpleNamespace(
        config_entries=_RecordingEntries(entry),
        config=SimpleNamespace(components={"googlefindmy"}),
    )

    assert entry_reload_gate.falsy_reload_left_the_latch_behind(hass, entry.entry_id), (
        reason
    )
