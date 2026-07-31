# tests/test_options_flow_subentries.py

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.helpers import frame

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    OPT_CONTRIBUTOR_MODE,
    OPT_IGNORED_DEVICES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_flow import prepare_flow_hass_config_entries

pytestmark = pytest.mark.asyncio


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry identifiers for options tests."""

    return f"{entry_id}-{key}-subentry"


@dataclass
class _ManagerStub:
    """Minimal config_entries manager capturing subentry operations."""

    entry: _EntryStub

    def __post_init__(self) -> None:
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.removed: list[str] = []
        self.reloads: list[str] = []
        self.scheduled_reloads: list[str] = []
        self.setup_calls: list[str] = []

    def async_update_entry(self, entry: _EntryStub, *, data: dict[str, Any]) -> None:
        assert entry is self.entry
        entry.data = data

    def async_update_subentry(  # noqa: PLR0913
        self,
        entry: _EntryStub,
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any],
        title: str | None = None,
        unique_id: str | None = None,
        translation_key: str | None = None,
    ) -> None:
        assert entry is self.entry
        subentry.data = MappingProxyType(dict(data))
        if title is not None:
            subentry.title = title
        if unique_id is not None:
            subentry.unique_id = unique_id
        if translation_key is not None:
            subentry.translation_key = translation_key
        self.updated.append((subentry.subentry_id, dict(subentry.data)))

    def async_remove_subentry(self, entry: _EntryStub, subentry_id: str) -> bool:  # noqa: FBT001
        assert entry is self.entry
        entry.discard_subentry(subentry_id)
        self.removed.append(subentry_id)
        return True

    def async_schedule_reload(self, entry_id: str) -> None:
        """Record the call; deliberately do not chain into ``async_reload``.

        Synchronous on purpose: ``ConfigEntries.async_schedule_reload`` is a
        ``@callback`` returning ``None``, and a coroutine here would make the
        production call site look awaitable when it is not.

        Two core behaviours are left out knowingly, so nothing here is mistaken
        for a faithful replica: the core raises ``UnknownEntry`` for an entry id
        it does not know, and it hands the reload to ``async_create_task``. This
        recorder does neither, which is what lets an assertion tell the two call
        styles apart -- but it also means the failure branch of
        ``_schedule_claimed_reload`` cannot be reached from this file.

        Latent coupling, deliberately named here: ``_core_auto_schedules_subentries``
        only consults this attribute once the manager also exposes
        ``async_setup_subentry`` **and** ``async_get_subentries``. This stub has the
        second but not the first, so the probe still answers ``False``. Adding
        ``async_setup_subentry`` here later would flip that probe silently.
        """

        self.scheduled_reloads.append(entry_id)

    async def async_reload(self, entry_id: str) -> None:
        """Kept next to :meth:`async_schedule_reload` as a regression tripwire.

        A later fall back to the awaited variant would otherwise pass silently;
        with both recorders present the assertions can tell the two apart.
        """

        self.reloads.append(entry_id)

    def async_get_entry(self, entry_id: str) -> _EntryStub | None:
        if entry_id == self.entry.entry_id:
            return self.entry
        return None

    def async_get_subentries(self, entry_id: str) -> list[ConfigSubentry]:
        entry = self.async_get_entry(entry_id)
        if entry is None:
            return []
        return list(entry.subentries.values())

    async def async_setup(self, entry_id: str) -> bool:
        self.setup_calls.append(entry_id)
        return True


class _EntryStub:
    """Config entry stub exposing subentries and mutable options."""

    def __init__(self) -> None:
        self.entry_id = "entry-test"
        self.title = "Entry Title"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        # Home Assistant hands ``ConfigEntry.subentries`` out as a
        # ``MappingProxyType`` and never as a ``dict``
        # (``config_entries.py``: ``_setter(self, "subentries",
        # MappingProxyType(subentries))``). A stub exposing a plain ``dict``
        # here is not a harmless simplification: it makes an
        # ``isinstance(..., dict)`` guard in production pass in the test world
        # and skip every subentry in the real one, which is how the options
        # flow's subentry selection stayed dead for nine months while this file
        # was green. The mutable store below stays reachable for the two
        # writers that mirror the core's own -- ``add_subentry`` and
        # ``discard_subentry`` -- so what the flow gets to see is the read-only
        # view and nothing else.
        self._subentry_store: dict[str, ConfigSubentry] = {}
        self.subentries: Mapping[str, ConfigSubentry] = MappingProxyType(
            self._subentry_store
        )
        self.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(data=[]))
        # The three fields the reload gate in ``_schedule_claimed_reload``
        # reads. Without them every ``getattr(entry, ..., None)`` in
        # ``_entry_reload_is_hopeless`` falls back to ``None``, the gate can
        # only ever answer "not hopeless", and a test that claims to exercise
        # the gate would in fact exercise nothing (fail-open, so silently).
        # Defaults are the healthy case; a test that wants the gate to bite
        # assigns a terminal ``state`` (see the guard test at the end of this
        # file). ``ConfigEntryState`` is imported from the submodule because
        # that is the module world the production code compares against.
        self.state = ConfigEntryState.LOADED
        self.source = "user"
        self.disabled_by = None

    def add_subentry(
        self,
        *,
        key: str,
        title: str,
        visible_device_ids: list[str] | None = None,
        feature_flags: dict[str, Any] | None = None,
        subentry_type: str = SUBENTRY_TYPE_TRACKER,
        identity: str | None = None,
    ) -> ConfigSubentry:
        """Add a subentry to the stub.

        ``subentry_type`` is a parameter rather than a constant because the
        assignability predicate reads *both* axes: the stored ``group_key`` and
        the subentry type. A stub that could only produce ``tracker`` would let
        a key-only predicate pass every test while still missing a legacy
        subentry whose ``group_key`` drifted from its type -- the alias case
        ``agents/config_flow/AGENTS.md`` describes under
        ``Subentry alias handling``.

        ``identity`` decouples the Home Assistant identifiers from ``key`` and
        is what makes the *collision* half of that same alias case expressible.
        Without it this helper derives ``subentry_id`` from ``key`` and stores
        the result under that id, so two calls sharing a ``group_key`` silently
        overwrite one another and leave a single subentry behind -- the stub
        would quietly refuse to build the very shape
        ``tests/test_config_flow_subentry_sync.py`` pins for the manager.
        """

        payload = {
            "group_key": key,
            "feature_flags": feature_flags or {},
        }
        if visible_device_ids is not None:
            payload["visible_device_ids"] = list(visible_device_ids)
        slug = identity or key
        subentry = ConfigSubentry(
            data=payload,
            subentry_type=subentry_type,
            title=title,
            unique_id=f"{self.entry_id}-{slug}",
            subentry_id=_stable_subentry_id(self.entry_id, slug),
            translation_key=key,
        )
        self._subentry_store[subentry.subentry_id] = subentry
        return subentry

    def discard_subentry(self, subentry_id: str) -> None:
        """Remove a subentry through the store rather than through the view.

        Kept on the entry rather than done inline in ``_ManagerStub`` because
        ``self.subentries`` is a read-only view, and a double that could delete
        straight through it would be modelling an entry Home Assistant does not
        hand out.

        Two divergences from ``ConfigEntries.async_remove_subentry``, named so
        this is not mistaken for a replica. The core raises ``UnknownSubEntry``
        for an id it does not hold while this discards silently, and the core
        rebuilds the mapping into a fresh ``MappingProxyType`` while this
        mutates the shared store in place -- so a reference to
        ``entry.subentries`` taken before a removal stays live here and would be
        a stale snapshot against the core. Both are the lenient direction; a
        test that needs either behaviour cannot use this double.
        """

        self._subentry_store.pop(subentry_id, None)


class _HassStub:
    """Home Assistant stub exposing config entry helpers to the flow."""

    def __init__(self, entry: _EntryStub) -> None:
        self._entry = entry
        # Load-bearing since the repair steps schedule through
        # ``_schedule_claimed_reload``: the tests at the end of this file read
        # and pre-set the latch through it. The
        # reload latch lives in ``hass.data``: without this mapping
        # ``_domain_data`` raises ``AttributeError`` inside
        # ``_claim_entry_reload``, which swallows it and fails open with ``True``.
        # The stand-down branch would then be unreachable and its mutation probe
        # meaningless.
        self.data: dict[str, Any] = {}
        # ``tests/AGENTS.md`` point 8 asks for this and the file did not have it:
        # ``entry_reload_gate.entry_reload_is_hopeless`` ends on
        # ``domain in hass.config.components``, and where it cannot read that
        # container it fails **closed** and calls every entry hopeless. Tests for
        # a non-terminal but not-loaded entry would then measure the terminal
        # branch instead of their own.
        self.config = SimpleNamespace(components={config_flow.DOMAIN})

    @classmethod
    async def create(cls, entry: _EntryStub) -> _HassStub:
        hass = cls(entry)
        prepare_flow_hass_config_entries(
            hass,
            lambda: _ManagerStub(entry),
            frame_module=frame,
        )
        return hass

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)


async def _build_flow(entry: _EntryStub) -> config_flow.OptionsFlowHandler:
    flow = config_flow.OptionsFlowHandler()
    flow.hass = await _HassStub.create(entry)  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]
    return flow


def _offered_keys(result: dict[str, Any], field: str) -> set[str]:
    """Return the selectable keys a shown form offers for ``field``.

    Reads the ``vol.In`` container out of the rendered schema instead of
    calling the production helper that built it. That is deliberate: the point
    of these tests is what the *user* can pick, so a later rename or a second
    helper must not be able to keep them green while the form changes.
    """

    schema = result["data_schema"].schema
    for marker, validator in schema.items():
        if str(marker) != field:
            continue
        container = getattr(validator, "container", None)
        if container is None:  # pragma: no cover - schema shape changed
            raise AssertionError(f"field {field!r} is not a vol.In selector")
        return set(container)
    raise AssertionError(f"field {field!r} not present in the shown form")


async def test_subentry_options_are_gathered_from_the_real_read_only_mapping() -> None:
    """The ground every other test in this file stands on.

    Home Assistant exposes ``ConfigEntry.subentries`` as a ``MappingProxyType``,
    not as a ``dict``. That distinction is not cosmetic here: production reaches
    the subentries through an ``isinstance`` guard, and a guard narrowed to
    ``dict`` passes every test in this file while skipping every subentry a real
    user has. It did exactly that from 2025-10-27 until this commit, which is
    why the check runs against a genuine ``ConfigEntry`` rather than against the
    local stub -- a stub asserted against itself could only ever confirm its own
    shape.

    The identity assertion is the load-bearing one, and two of the assertions
    below are deliberately *not*: when the guard skips everything,
    ``_gather_subentry_options`` appends a synthetic option that carries
    ``TRACKER_SUBENTRY_KEY`` and is likewise exactly one, so neither
    ``option.key`` nor ``len(options)`` can tell the two paths apart. They are
    kept as context, not as evidence. What separates the paths is that the
    synthetic option has no backing subentry, so ``subentry_id`` is the cell
    that has to be checked, and the label -- the subentry's title rather than
    the entry's -- is checked next to it because it is what the user reads.
    Measured rather than reasoned: with the guard narrowed back to ``dict``,
    this test fails on the ``subentry_id`` line, not before it.

    Reaching the genuine class takes a detour that is worth naming, because it
    is the second reason the suite was blind here. ``tests/conftest.py`` puts a
    synthetic ``homeassistant.config_entries`` into ``sys.modules``, so the
    ``from homeassistant.config_entries import ...`` form at the top of this
    file resolves to the stub -- whose ``ConfigEntry`` is a bare placeholder
    without subentries. The parent package still carries the real submodule as
    an attribute, so the ``import ... as`` form below reaches it (measured: the
    two disagree, one reports the installed file, the other reports no file).
    Both imports are function-local as a consequence, which incidentally keeps
    the rest of this file independent of an optional plugin.
    """

    import homeassistant.config_entries as real_config_entries

    if not hasattr(real_config_entries, "ConfigSubentryData"):  # pragma: no cover
        pytest.fail(
            "The real homeassistant package must be importable for this test: "
            "it grounds the stub's subentry mapping in the core's own shape, "
            "and the suite's config_entries stub cannot stand in for it.",
            pytrace=False,
        )
    try:
        from pytest_homeassistant_custom_component.common import MockConfigEntry
    except ModuleNotFoundError:  # pragma: no cover - environment guard
        pytest.fail(
            "pytest-homeassistant-custom-component must be installed alongside "
            "homeassistant; see tests/AGENTS.md on the stub pairing.",
            pytrace=False,
        )

    real_entry = MockConfigEntry(
        domain=config_flow.DOMAIN,
        # Distinct from the subentry title below, so the fallback option (which
        # labels itself with the *entry* title) cannot be mistaken for the real
        # one.
        title="Account title",
        subentries_data=[
            real_config_entries.ConfigSubentryData(
                data={"group_key": TRACKER_SUBENTRY_KEY},
                subentry_type=SUBENTRY_TYPE_TRACKER,
                title="Subentry title",
                unique_id=None,
            )
        ],
    )

    # Asserted in both directions: ``Mapping`` alone would also hold for a
    # ``dict``, and it is the negative half that pins the core's actual shape.
    assert isinstance(real_entry.subentries, Mapping)
    assert not isinstance(real_entry.subentries, dict)

    # Built through this file's own helper and then pointed at the real entry:
    # the plumbing an options flow needs is beside the point here, the shape of
    # the entry it reads is the whole point.
    flow = await _build_flow(_EntryStub())
    flow.config_entry = real_entry  # type: ignore[attr-defined]

    options = flow._gather_subentry_options()

    assert len(options) == 1
    (option,) = options
    assert option.subentry_id == next(iter(real_entry.subentries))
    assert option.label == "Subentry title"
    assert option.key == TRACKER_SUBENTRY_KEY

    # The stub the rest of this file builds on carries the same shape, so those
    # tests exercise the production guard instead of a friendlier stand-in.
    assert not isinstance(_EntryStub().subentries, dict)


async def test_settings_updates_feature_flags_for_selected_subentry() -> None:
    """Settings step should persist feature flags to the chosen subentry."""

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    flow = await _build_flow(entry)

    result = await flow.async_step_settings(
        {
            "subentry": TRACKER_SUBENTRY_KEY,
            OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            OPT_CONTRIBUTOR_MODE: "high_traffic",
        }
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    _, payload = manager.updated[-1]
    assert payload["feature_flags"][OPT_MAP_VIEW_TOKEN_EXPIRATION] is True
    assert payload["feature_flags"][OPT_CONTRIBUTOR_MODE] == "high_traffic"


async def test_settings_schedules_the_reload_its_options_need() -> None:
    """The settings step asks for its own reload, through the owner latch.

    These options are read at setup time, so they only take effect after a
    reload. The step used to inherit one from ``OptionsFlowWithReload``; that
    base is gone (the core refuses it next to an update listener), so the
    reload has to be requested explicitly. Without this test the step would
    save its options and quietly do nothing until the next restart.

    The update listener is not a substitute: it reloads only for
    credential-relevant keys, and this form writes none.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    flow = await _build_flow(entry)

    result = await flow.async_step_settings(
        {
            "subentry": TRACKER_SUBENTRY_KEY,
            OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
        }
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == [entry.entry_id]


async def test_settings_stands_down_but_still_writes_the_options(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A foreign owner costs the reload, never the write.

    The settings form is the one step whose options would otherwise be lost:
    standing down must leave the entry unchanged in every other respect, so the
    reload that owner is bringing carries this write too. The log assertion is
    what makes the stand-down branch itself measured rather than merely
    executed -- without it, ignoring the return value would look the same.
    """

    caplog.set_level(logging.DEBUG, logger=config_flow._LOGGER.name)
    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    flow = await _build_flow(entry)
    _claim_latch(flow.hass, entry.entry_id)

    result = await flow.async_step_settings(
        {
            "subentry": TRACKER_SUBENTRY_KEY,
            OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
        }
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    _, payload = manager.updated[-1]
    assert payload["feature_flags"][OPT_MAP_VIEW_TOKEN_EXPIRATION] is True
    assert any(
        "were saved without scheduling a reload" in record.message
        for record in caplog.records
    )


async def _settled_settings_state(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Return the options, subentry data and title a settings submit leaves behind.

    Used to build the *unchanged* starting point for the two tests below. Taken
    from a throwaway entry with its own hass double on purpose: submitting twice
    against one double would leave the shared reload latch claimed from the first
    run, and the second run would then stand down as a foreign owner -- passing
    for the wrong reason and proving nothing about the change detection.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    flow = await _build_flow(entry)
    result = await flow.async_step_settings(dict(user_input))
    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    _, subentry_payload = manager.updated[-1]
    subentry = next(iter(entry.subentries.values()))
    return dict(result["data"]), dict(subentry_payload), subentry.title


async def test_settings_skips_the_reload_when_nothing_changed() -> None:
    """Confirming the form without an edit must not tear the entry down.

    This reproduces what the step inherited, it does not refine it: the core
    scheduled on ``async_update_entry(...) and automatic_reload``, and
    ``async_update_entry`` returns ``False`` for an unchanged payload
    (``OptionsFlowManager.async_finish_flow``, cores 2026.1.3 and 2026.2.3).
    An unconditional claim would make a no-op save remove every entity of the
    account and repeat the whole setup, and would hold the single-owner latch
    while doing it -- which is why the latch is asserted free at the end rather
    than only the recorders being empty.
    """

    user_input = {
        "subentry": TRACKER_SUBENTRY_KEY,
        OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
        OPT_CONTRIBUTOR_MODE: "high_traffic",
    }
    options, subentry_data, subentry_title = await _settled_settings_state(user_input)

    entry = _EntryStub()
    subentry = entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title=subentry_title)
    subentry.data = MappingProxyType(dict(subentry_data))
    entry.options = options
    flow = await _build_flow(entry)

    result = await flow.async_step_settings(dict(user_input))

    assert result["type"] == "create_entry"
    assert dict(result["data"]) == options
    manager = flow.hass.config_entries  # type: ignore[assignment]
    # Both sides (``tests/AGENTS.md`` rule 8): neither the scheduling nor the
    # awaited variant may fire.
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    # The latch was never taken, so a reload another owner brings is unaffected.
    assert _latch_is_free(flow.hass, entry.entry_id) is True


async def test_settings_schedules_the_reload_for_a_subentry_only_change() -> None:
    """A write that lands only on the subentry still owes a reload.

    The options comparison cannot see it: the payload is identical, but
    ``_async_update_feature_group_subentry`` synchronises a stale
    ``entry_title`` onto the subentry. Without the second arm of the condition
    the change would be written and never take effect, which is the exact
    damage class this PR exists to close.

    ``group_key`` would be the other candidate and is deliberately *not* used:
    dropping it also drops the subentry from ``_subentry_choice_map``, so the
    step would abort with ``invalid_subentry`` and the test would exercise the
    error path instead of the change detection (measured, not assumed).
    """

    user_input = {
        "subentry": TRACKER_SUBENTRY_KEY,
        OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
    }
    options, subentry_data, subentry_title = await _settled_settings_state(user_input)
    assert subentry_data["entry_title"] != "Stale title"
    subentry_data["entry_title"] = "Stale title"

    entry = _EntryStub()
    subentry = entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title=subentry_title)
    subentry.data = MappingProxyType(dict(subentry_data))
    entry.options = options
    flow = await _build_flow(entry)

    result = await flow.async_step_settings(dict(user_input))

    assert result["type"] == "create_entry"
    assert dict(result["data"]) == options
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated[-1][1]["entry_title"] == entry.title
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_settings_reloads_for_an_entry_without_any_subentry() -> None:
    """With no subentry to write to, the options arm has to carry the decision.

    An entry without subentries still offers a choice: `_gather_subentry_options`
    synthesises a `core_tracking` option whose `subentry` is ``None``, and
    `_async_update_feature_group_subentry` returns early for it. The subentry arm
    is therefore always ``False`` here, and a reload must still be scheduled
    because the options did change -- the early return must not be mistaken for
    "nothing happened".
    """

    entry = _EntryStub()
    assert entry.subentries == {}
    flow = await _build_flow(entry)

    result = await flow.async_step_settings(
        {"subentry": "core_tracking", OPT_MAP_VIEW_TOKEN_EXPIRATION: True}
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated == []
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_visibility_assigns_devices_to_target_subentry() -> None:
    """Visibility step should attach restored devices to the chosen subentry."""

    entry = _EntryStub()
    sub = entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.options = {
        OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}},
    }

    flow = await _build_flow(entry)
    result = await flow.async_step_visibility(
        {"subentry": TRACKER_SUBENTRY_KEY, "unignore_devices": ["dev-1"]}
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    updated_id, payload = manager.updated[-1]
    assert updated_id == sub.subentry_id
    assert payload["visible_device_ids"] == ("dev-1",)
    # Assert both sides (``tests/AGENTS.md`` rule 8): the step routes through
    # ``_schedule_claimed_reload`` like its two sibling assignment sites, so the
    # scheduling side has to be claimed and the direct side has to stay empty.
    # Unignoring without a reload leaves the device without any entity, because
    # the platform known-sets never drop an id -- see the comment at the call
    # site and the platform-level characterisation in
    # ``test_device_tracker_scanner.py``.
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_visibility_neither_offers_nor_accepts_the_service_subentry() -> None:
    """The service group is gone from the target list, and submitting it fails.

    This is the inversion of the characterisation recorded before the fix: back
    then the form offered the key *and* the sink wrote device ids into it. Both
    halves are asserted here for the same reason they were asserted then. A fix
    that only hid the option while leaving the submission path writable would
    flip the first assertion and keep the second, which is exactly the half-fix
    this pairing exists to catch.

    Why the write was a dead end: ``coordinator/subentry.py`` forces
    ``visible_device_ids`` back to ``()`` for the service key on every index
    refresh, and the next entry setup clears the stored value a second time --
    see ``test_service_subentry_visibility_is_cleared_by_the_setup_roundtrip``
    in ``test_options_flow_registry_updates.py``.

    Both reload directions are asserted (``tests/AGENTS.md`` rule 8): a refused
    submission writes nothing and therefore claims no reload either. Leaving
    that open would let a future change tear the entry down for an action it
    just rejected.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}

    flow = await _build_flow(entry)

    form = await flow.async_step_visibility()
    offered = _offered_keys(form, "subentry")
    assert SERVICE_SUBENTRY_KEY not in offered
    assert TRACKER_SUBENTRY_KEY in offered

    result = await flow.async_step_visibility(
        {"subentry": SERVICE_SUBENTRY_KEY, "unignore_devices": ["dev-1"]}
    )

    assert result["type"] == "form"
    assert result["errors"] == {"subentry": "invalid_subentry"}
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated == []
    assert manager.scheduled_reloads == []
    assert manager.reloads == []


async def test_visibility_still_assigns_and_reloads_for_a_device_group() -> None:
    """The narrowing must not cost the legitimate target its assignment.

    The companion to the test above: hiding the service group is only correct
    if the tracker group still works end to end. Both sides again -- the write
    lands, and the reload claim introduced by PR #1228 is still taken, because
    a device returning from the ignore list has no entity until a reload
    rebuilds the platform known-sets.
    """

    entry = _EntryStub()
    core = entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}

    flow = await _build_flow(entry)

    result = await flow.async_step_visibility(
        {"subentry": TRACKER_SUBENTRY_KEY, "unignore_devices": ["dev-1"]}
    )
    await asyncio.sleep(0)

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[core.subentry_id]["visible_device_ids"]) == ("dev-1",)
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_repairs_move_does_not_offer_the_service_subentry() -> None:
    """Second of the three assignment steps: the move target list is narrowed."""

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )

    flow = await _build_flow(entry)

    form = await flow.async_step_repairs_move()
    offered = _offered_keys(form, "target_subentry")
    assert SERVICE_SUBENTRY_KEY not in offered
    assert TRACKER_SUBENTRY_KEY in offered


async def test_repairs_delete_narrows_the_fallback_but_not_the_deletion() -> None:
    """Third assignment step, and the one place where the two lists differ.

    What may be *deleted* is a different question from what may *receive*
    devices. The service group stays removable -- the core repair path recreates
    a missing one, and taking that away would be an unrelated product decision
    (veto point ``V-3-T`` of the plan) -- while it must not be offered as the
    fallback that inherits the deleted group's devices.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.add_subentry(key="second", title="Second")
    entry.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )

    flow = await _build_flow(entry)

    form = await flow.async_step_repairs_delete()
    assert SERVICE_SUBENTRY_KEY not in _offered_keys(form, "fallback_subentry")
    assert SERVICE_SUBENTRY_KEY in _offered_keys(form, "delete_subentry")


async def test_the_three_non_assigning_steps_keep_the_service_subentry() -> None:
    """The narrowing stops at the three steps that assign devices.

    ``_subentry_choice_map`` fed all six steps before the split. Filtering
    inside it would have been the smaller diff and the larger mistake:
    ``async_step_settings`` and ``async_step_credentials`` legitimately target
    the service group, and ``async_step_repairs`` only asks whether *any*
    subentry exists, so a shared filter could send an entry whose sole subentry
    is the service group into ``repairs_no_subentries``. All three are checked
    here, the last one in its worst case. The fourth remaining caller, the
    ``removable_choices`` of ``async_step_repairs_delete``, has its own test.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )
    flow = await _build_flow(entry)

    settings_form = await flow.async_step_settings()
    assert SERVICE_SUBENTRY_KEY in _offered_keys(settings_form, "subentry")

    credentials_form = await flow.async_step_credentials()
    assert SERVICE_SUBENTRY_KEY in _offered_keys(credentials_form, "subentry")

    service_only = _EntryStub()
    service_only.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )
    service_only_flow = await _build_flow(service_only)

    menu = await service_only_flow.async_step_repairs()
    assert menu["type"] == "menu"


async def test_the_sink_refuses_a_service_target_on_a_direct_call() -> None:
    """The guard sits at the data-flow chokepoint, not only in the forms.

    The forms can no longer produce this call, so any occurrence is a caller
    bug. It is still worth pinning: ``_async_assign_devices_to_subentry`` is
    the single writer able to break an invariant that three other production
    sites already keep, and a fourth caller added later would inherit the
    protection without knowing about it.

    Standing down rather than stripping the ids elsewhere is asserted too: the
    tracker group keeps what it legitimately holds.
    """

    entry = _EntryStub()
    core = entry.add_subentry(
        key=TRACKER_SUBENTRY_KEY, title="Core", visible_device_ids=["dev-1"]
    )
    entry.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )
    flow = await _build_flow(entry)

    changed = await flow._async_assign_devices_to_subentry(
        entry,  # type: ignore[arg-type]
        SERVICE_SUBENTRY_KEY,
        ["dev-1"],
    )

    assert SERVICE_SUBENTRY_KEY not in changed
    assert changed == set()
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated == []
    # The device stays where it legitimately was.
    assert tuple(core.data["visible_device_ids"]) == ("dev-1",)


async def test_a_legacy_subentry_typed_service_is_excluded_by_its_type() -> None:
    """The key alone does not carry the proof, so the type is read as well.

    ``ConfigEntrySubEntryManager._refresh_from_entry`` derives the canonical key
    primarily from ``subentry_type`` and keeps a diverging stored ``group_key``
    as an alias, which ``agents/config_flow/AGENTS.md`` requires under
    ``Subentry alias handling``. A predicate comparing only the key would let
    this subentry through in all three steps.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.add_subentry(key="second", title="Second")
    entry.add_subentry(
        key="owner@example.com",
        title="Legacy Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}

    flow = await _build_flow(entry)

    visibility = await flow.async_step_visibility()
    move = await flow.async_step_repairs_move()
    delete = await flow.async_step_repairs_delete()

    assert "owner@example.com" not in _offered_keys(visibility, "subentry")
    assert "owner@example.com" not in _offered_keys(move, "target_subentry")
    assert "owner@example.com" not in _offered_keys(delete, "fallback_subentry")


@pytest.mark.parametrize(
    ("service_title", "tracker_title", "service_sorts_first"),
    [
        ("Alpha service", "Zulu trackers", True),
        ("Zulu service", "Alpha trackers", False),
    ],
    ids=["service-sorts-first", "tracker-sorts-first"],
)
async def test_a_group_key_shared_by_two_types_still_lands_on_the_tracker(
    service_title: str, tracker_title: str, service_sorts_first: bool
) -> None:
    """The two orderings fail differently, so both are exercised.

    ``Subentry alias handling`` lets one legacy label sit on a service and a
    tracker subentry at once, and ``_gather_subentry_options`` sorts by label,
    so the alphabet decided which subentry a key resolved to. Resolving the
    first holder while writing to every holder meant: service first -> the
    guard judged the wrong twin and refused, so the move reported success
    without moving anything; tracker first -> the guard passed and the write
    fanned out onto the service group, storing exactly the ids the invariant
    forbids.

    Both assertions are needed. Only checking the return value misses the
    fan-out, only checking the service payload misses the silent refusal.
    """

    entry = _EntryStub()
    service = entry.add_subentry(
        key="owner@example.com",
        title=service_title,
        subentry_type=SUBENTRY_TYPE_SERVICE,
        identity="legacy-service",
    )
    tracker = entry.add_subentry(
        key="owner@example.com",
        title=tracker_title,
        visible_device_ids=[],
        identity="legacy-tracker",
    )
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}
    flow = await _build_flow(entry)

    offered = _offered_keys(await flow.async_step_visibility(), "subentry")
    options = flow._gather_subentry_options()
    target_key = next(option.key for option in options if option.subentry is tracker)

    # The ordering this case claims to exercise is pinned against a value from
    # the parameter list, not derived from the titles. Deriving it would only
    # restate the sort and would still pass if both parameter sets happened to
    # order the same way -- which is exactly how the first version of this test
    # left the second half of the asymmetry uncovered while staying green.
    assert (options[0].subentry is service) is service_sorts_first
    assert target_key in offered

    changed = await flow._async_assign_devices_to_subentry(
        entry,  # type: ignore[arg-type]
        target_key,
        ["dev-1"],
    )

    assert changed == {target_key}
    assert tuple(tracker.data.get("visible_device_ids", ())) == ("dev-1",)
    assert tuple(service.data.get("visible_device_ids", ())) == ()


async def test_the_option_key_identifies_exactly_one_subentry() -> None:
    """The property the two choice maps and the deletion step depend on.

    Both ``_subentry_choice_map`` and ``_device_target_choice_map`` collapse the
    key into a ``dict``, and ``async_step_repairs_delete`` resolves both the
    deletion target and the devices it hands on through that mapping. A key
    carried by two subentries therefore hides one of them from every form and
    can delete the other. Asserted as an invariant over the whole list rather
    than against a fixed expectation, so a future shape inherits the guarantee.

    Three holders rather than two, and one of them a service group, so the
    disambiguation is not read as a two-element swap. The last two subentries
    build the chain that defeats a rewrite touching only the duplicates: the
    fourth stores the ``subentry_id`` a duplicate would move to, and the fifth
    stores the ``subentry_id`` of the fourth. Any rewrite bounded by a fixed
    number of passes hands out a fresh duplicate here, which is why the
    production rule moves *every* option once one duplicate exists.

    Both identifiers are derived from ``entry.entry_id`` rather than written
    out. A literal would stop matching a real ``subentry_id`` the moment that
    attribute changed, the collision would not arise, and the invariant below
    would hold trivially while covering nothing.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.add_subentry(key="shared", title="First", identity="first")
    entry.add_subentry(key="shared", title="Second", identity="second")
    entry.add_subentry(
        key="shared",
        title="Legacy service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
        identity="third",
    )
    entry.add_subentry(
        key=_stable_subentry_id(entry.entry_id, "first"),
        title="Collides with a replacement",
        identity="fourth",
    )
    entry.add_subentry(
        key=_stable_subentry_id(entry.entry_id, "fourth"),
        title="Collides one step further out",
        identity="fifth",
    )
    flow = await _build_flow(entry)

    options = flow._gather_subentry_options()

    assert len(options) == 6
    assert len({option.key for option in options}) == len(options)
    # All-or-nothing: once one duplicate exists every option carries its own
    # ``subentry_id``, including the healthy tracker group. Pinned so the
    # difference to the narrower rule stays visible, and asserted through the
    # subentries rather than against written-out ids.
    assert {option.key for option in options} == set(entry.subentries)
    assert TRACKER_SUBENTRY_KEY not in {option.key for option in options}


async def test_a_subentry_without_a_stored_group_key_falls_back_to_its_id() -> None:
    """The other source of an option key, and the reason it is already unique.

    A subentry whose ``group_key`` is missing or blank takes its
    ``subentry_id`` as the key. That branch is what makes the disambiguation
    above a no-op for such a subentry, and it is why the rewrite can claim not
    to change what ``_async_update_feature_group_subentry`` would store.
    """

    entry = _EntryStub()
    blank = entry.add_subentry(key="   ", title="Blank", identity="blank")
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")

    flow = await _build_flow(entry)

    options = flow._gather_subentry_options()
    keys = {option.subentry_id: option.key for option in options}

    assert keys[blank.subentry_id] == blank.subentry_id
    assert len({option.key for option in options}) == len(options)


async def test_a_shared_group_key_leaves_both_groups_reachable_for_deletion() -> None:
    """What the injectivity buys at the consumer, not just as a property.

    ``async_step_repairs_delete`` resolves both the deletion target and the
    devices it hands to the fallback through the ``dict`` of
    ``_subentry_choice_map``. While two subentries shared one key that mapping
    held a single entry, so one group was invisible, the coupling
    ``fallback_key != target_key`` could never be satisfied and the whole step
    aborted on ``subentry_delete_invalid``: the user could not delete either
    group.
    """

    entry = _EntryStub()
    service = entry.add_subentry(
        key="owner@example.com",
        title="Zulu service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
        identity="legacy-service",
    )
    tracker = entry.add_subentry(
        key="owner@example.com",
        title="Alpha trackers",
        visible_device_ids=["dev-1"],
        identity="legacy-tracker",
    )
    flow = await _build_flow(entry)

    form = await flow.async_step_repairs_delete()
    keys = {
        option.subentry_id: option.key for option in flow._gather_subentry_options()
    }

    assert form["type"] == "form"
    assert _offered_keys(form, "delete_subentry") == {keys[service.subentry_id]}
    assert _offered_keys(form, "fallback_subentry") == {keys[tracker.subentry_id]}


async def test_a_move_onto_a_shared_key_reports_success_only_when_it_moved() -> None:
    """The refusal and "nothing to do" must not look alike to the caller.

    ``async_step_repairs_move`` maps an empty return value onto
    ``subentry_move_success``. While the guard could refuse on behalf of a twin
    the user never picked, that abort told the user their device had moved when
    it had not.
    """

    entry = _EntryStub()
    entry.add_subentry(
        key="owner@example.com",
        title="Alpha service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
        identity="legacy-service",
    )
    tracker = entry.add_subentry(
        key="owner@example.com",
        title="Zulu trackers",
        visible_device_ids=[],
        identity="legacy-tracker",
    )
    entry.runtime_data.coordinator.data = [{"device_id": "dev-1", "name": "Device 1"}]
    flow = await _build_flow(entry)

    form = await flow.async_step_repairs_move()
    target_key = next(
        option.key
        for option in flow._gather_subentry_options()
        if option.subentry is tracker
    )

    assert target_key in _offered_keys(form, "target_subentry")

    result = await flow.async_step_repairs_move(
        {"target_subentry": target_key, "device_ids": ["dev-1"]}
    )
    await asyncio.sleep(0)

    assert result["reason"] == "subentry_move_success"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[tracker.subentry_id]["visible_device_ids"]) == ("dev-1",)


async def test_an_entry_with_only_a_service_group_still_offers_a_target() -> None:
    """The empty arm of the narrowing is exercised through to its effect.

    Filtering an entry whose only subentry is a service group would leave
    ``vol.In`` with an empty mapping, which renders a form the user cannot
    submit. The fallback mirrors the one ``_gather_subentry_options`` already
    applies for an entry without any subentry (invariant LC-1: a set operation
    needs its clear counterpart spelled out).

    Both sides are asserted, because the offered key is a *synthesised* option
    with no backing subentry, so the assignment writes nothing. That is not a
    dead end: an id no subentry claims is merged into the tracker group by
    ``coordinator/subentry.py::_refresh_subentry_index``, which synthesises the
    tracker metadata when the entry has none. What the user asked for -- the
    device out of the ignore list and visible again -- therefore happens, and
    the un-ignore write plus the reload claim are what carry it.
    """

    entry = _EntryStub()
    entry.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}

    flow = await _build_flow(entry)

    form = await flow.async_step_visibility()
    offered = _offered_keys(form, "subentry")

    assert offered == {TRACKER_SUBENTRY_KEY}
    assert SERVICE_SUBENTRY_KEY not in offered

    result = await flow.async_step_visibility(
        {"subentry": TRACKER_SUBENTRY_KEY, "unignore_devices": ["dev-1"]}
    )
    await asyncio.sleep(0)

    assert result["type"] == "create_entry"
    assert result["data"][OPT_IGNORED_DEVICES] == {}
    manager = flow.hass.config_entries  # type: ignore[assignment]
    # No subentry write: the only real subentry is the service group, and it
    # must not receive the id.
    assert manager.updated == []
    assert manager.scheduled_reloads == [entry.entry_id]


async def test_the_synthesised_fallback_never_borrows_a_key_in_use() -> None:
    """The fallback must not take the identity of a group it filtered out.

    An entry whose only subentry is typed ``service`` while storing the legacy
    ``group_key`` ``core_tracking`` is removed from the target list by its
    type, and the fallback used to offer *that* key back. The sink resolves a
    key against the **unfiltered** set, so it found the real, non-assignable
    subentry and refused the write.

    Asserted on the identity, not on a literal: the point is that no real
    option holds the offered key, not which substitute the helper picked.
    """

    entry = _EntryStub()
    entry.add_subentry(
        key=TRACKER_SUBENTRY_KEY,
        title="Legacy service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
        visible_device_ids=["dev-1"],
    )
    flow = await _build_flow(entry)

    choices, option_map = flow._device_target_choice_map()
    (offered_key,) = choices
    stored_keys = {opt.key for opt in flow._gather_subentry_options()}

    assert option_map[offered_key].subentry is None
    assert offered_key not in stored_keys


async def test_the_fallback_key_search_is_total_across_several_holders() -> None:
    """One substitute is not enough, so the search walks until it is free.

    A second subentry may store the very substitute the first one is displaced
    to. A fixed number of attempts would hand out a borrowed key again, one
    step further out, which is the same defect the search removes.
    """

    entry = _EntryStub()
    for index, key in enumerate(
        (TRACKER_SUBENTRY_KEY, f"{TRACKER_SUBENTRY_KEY}_2", f"{TRACKER_SUBENTRY_KEY}_3")
    ):
        entry.add_subentry(
            key=key,
            title=f"Legacy service {index}",
            subentry_type=SUBENTRY_TYPE_SERVICE,
            identity=f"legacy-{index}",
        )
    flow = await _build_flow(entry)

    choices, _ = flow._device_target_choice_map()
    (offered_key,) = choices

    assert offered_key not in {opt.key for opt in flow._gather_subentry_options()}


async def test_repairs_move_refuses_an_entry_without_a_real_destination() -> None:
    """A move needs a destination, and saying otherwise was the defect.

    Without a backing subentry the assignment writes nothing, ``changed``
    stays empty, and the step used to abort on ``subentry_move_success`` --
    a success message for a move that could not happen. The synthesised
    fallback is therefore not a candidate here, mirroring the
    ``real_fallback_keys`` line ``async_step_repairs_delete`` already draws
    for its own fallback field. The abort string already exists, so no
    translation key is added.
    """

    entry = _EntryStub()
    legacy = entry.add_subentry(
        key=TRACKER_SUBENTRY_KEY,
        title="Legacy service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
        visible_device_ids=["dev-1"],
    )
    flow = await _build_flow(entry)

    result = await flow.async_step_repairs_move()

    assert result["type"] == "abort"
    assert result["reason"] == "repairs_no_subentries"
    # The devices stay where they were: no strip, no loss.
    assert tuple(legacy.data["visible_device_ids"]) == ("dev-1",)
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated == []


async def test_the_sink_stands_down_when_no_option_carries_the_target() -> None:
    """Without a destination the loop must not run at all.

    Its ``else`` branch strips the ids from every group that holds them. With
    a real target that is the second half of a move; without one it is a loss
    -- and it would be reported as a success, because a strip fills
    ``changed``.
    """

    entry = _EntryStub()
    legacy = entry.add_subentry(
        key=TRACKER_SUBENTRY_KEY,
        title="Legacy service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
        visible_device_ids=["dev-1"],
    )
    flow = await _build_flow(entry)

    changed = await flow._async_assign_devices_to_subentry(
        entry,  # type: ignore[arg-type]
        "a-key-no-option-carries",
        ["dev-1"],
    )

    assert changed == set()
    assert tuple(legacy.data["visible_device_ids"]) == ("dev-1",)
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated == []


async def test_repairs_delete_stays_submittable_in_the_common_two_group_shape() -> None:
    """Narrowing the fallback must re-derive what is deletable, not just filter.

    The two lists are coupled through ``fallback_key != target_key``. In the
    shape the integration provisions for itself -- one tracker group and one
    service group -- narrowing only the fallback side left a form whose single
    fallback value was always the deletion target, so every submission failed
    on ``invalid_subentry`` with no way out. That is the same defect the step
    was narrowed to remove, one field higher up.

    The tracker group therefore drops out of the deletion list here: nothing
    else could inherit its devices. The service group stays, and deleting it
    works end to end.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    service = entry.add_subentry(
        key=SERVICE_SUBENTRY_KEY,
        title="Service",
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )

    flow = await _build_flow(entry)

    form = await flow.async_step_repairs_delete()
    assert form["type"] == "form"
    assert _offered_keys(form, "delete_subentry") == {SERVICE_SUBENTRY_KEY}
    assert _offered_keys(form, "fallback_subentry") == {TRACKER_SUBENTRY_KEY}

    result = await flow.async_step_repairs_delete(
        {
            "delete_subentry": SERVICE_SUBENTRY_KEY,
            "fallback_subentry": TRACKER_SUBENTRY_KEY,
        }
    )
    await asyncio.sleep(0)

    assert result["type"] == "abort"
    assert result["reason"] == "subentry_delete_success"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert service.subentry_id in manager.removed


async def test_repairs_move_assigns_devices_to_selected_subentry() -> None:
    """Repair move step should remove devices from other subentries."""

    entry = _EntryStub()
    target = entry.add_subentry(key="target", title="Target", visible_device_ids=[])
    other = entry.add_subentry(key="other", title="Other", visible_device_ids=["dev-2"])
    entry.runtime_data.coordinator.data = [
        {"device_id": "dev-1", "name": "Device 1"},
        {"device_id": "dev-2", "name": "Device 2"},
    ]

    flow = await _build_flow(entry)

    async def _invoke() -> dict[str, Any]:
        result = await flow.async_step_repairs_move(
            {"target_subentry": "target", "device_ids": ["dev-1", "dev-2"]}
        )
        await asyncio.sleep(0)
        return result

    result = await _invoke()

    assert result["type"] == "abort"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[target.subentry_id]["visible_device_ids"]) == (
        "dev-1",
        "dev-2",
    )
    assert tuple(updated[other.subentry_id]["visible_device_ids"]) == ()
    # Test D: exactly one reload, and it goes through the scheduling lever.
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_repairs_delete_moves_devices_and_removes_subentry() -> None:
    """Deleting a subentry moves devices to fallback and removes the source."""

    entry = _EntryStub()
    removable = entry.add_subentry(
        key="remove", title="Remove", visible_device_ids=["dev-1", "dev-2"]
    )
    fallback = entry.add_subentry(key="keep", title="Keep", visible_device_ids=[])

    flow = await _build_flow(entry)

    async def _invoke_delete() -> dict[str, Any]:
        result = await flow.async_step_repairs_delete(
            {"delete_subentry": "remove", "fallback_subentry": "keep"}
        )
        await asyncio.sleep(0)
        return result

    result = await _invoke_delete()

    assert result["type"] == "abort"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert removable.subentry_id in manager.removed
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[fallback.subentry_id]["visible_device_ids"]) == (
        "dev-1",
        "dev-2",
    )
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == []


async def test_the_scheduling_double_matches_the_core_call_shape() -> None:
    """The recorder has to be reachable and synchronous, or a typo hides here.

    Declared ``async`` only because this module carries a file-wide
    ``pytestmark = pytest.mark.asyncio``; the check itself needs no event loop.

    The repair tests now assert on ``scheduled_reloads``, so a misspelled method
    name no longer passes unnoticed. What stays worth pinning is the *shape*: an
    awaitable double would let the production call site look awaitable when it is
    not, and a recorder chaining into ``async_reload`` would blur the two routes.
    """

    entry = _EntryStub()
    manager = _ManagerStub(entry)

    schedule = getattr(manager, "async_schedule_reload", None)
    assert callable(schedule)
    assert not inspect.iscoroutinefunction(schedule), (
        "the core's async_schedule_reload is a @callback; an awaitable double "
        "would hide that the production call site does not await it"
    )

    schedule(entry.entry_id)
    assert manager.scheduled_reloads == [entry.entry_id]
    assert manager.reloads == [], (
        "the two recorders have to stay distinguishable; that is the whole "
        "point of keeping async_reload next to it"
    )


# --- AP5: the repair steps stand behind the single reload owner --------------


def _claim_latch(hass: Any, entry_id: str) -> None:
    """Take the shared latch on behalf of a foreign reload owner."""

    integration = config_flow.import_integration_package()
    assert integration.claim_pending_entry_reload(hass, entry_id) is True


def _latch_is_free(hass: Any, entry_id: str) -> bool:
    """Whether the shared reload latch of ``entry_id`` can be claimed again.

    Claims the latch as a side effect, so this belongs at the end of a test.
    """

    integration = config_flow.import_integration_package()
    return bool(integration.claim_pending_entry_reload(hass, entry_id))


async def test_repairs_move_stands_down_but_still_writes_the_assignment() -> None:
    """Test E: a foreign owner costs the reload, never the assignment."""

    entry = _EntryStub()
    target = entry.add_subentry(key="target", title="Target", visible_device_ids=[])
    entry.add_subentry(key="other", title="Other", visible_device_ids=["dev-1"])
    entry.runtime_data.coordinator.data = [{"device_id": "dev-1", "name": "Device 1"}]

    flow = await _build_flow(entry)
    _claim_latch(flow.hass, entry.entry_id)

    result = await flow.async_step_repairs_move(
        {"target_subentry": "target", "device_ids": ["dev-1"]}
    )
    await asyncio.sleep(0)

    assert result["type"] == "abort"
    assert result["reason"] == "subentry_move_success"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[target.subentry_id]["visible_device_ids"]) == ("dev-1",)


async def test_repairs_delete_stands_down_but_still_removes_the_subentry() -> None:
    """Test F: the removal happens before the claim and survives it."""

    entry = _EntryStub()
    removable = entry.add_subentry(
        key="remove", title="Remove", visible_device_ids=["dev-1"]
    )
    entry.add_subentry(key="keep", title="Keep", visible_device_ids=[])

    flow = await _build_flow(entry)
    _claim_latch(flow.hass, entry.entry_id)

    result = await flow.async_step_repairs_delete(
        {"delete_subentry": "remove", "fallback_subentry": "keep"}
    )
    await asyncio.sleep(0)

    assert result["type"] == "abort"
    assert result["reason"] == "subentry_delete_success"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    assert removable.subentry_id in manager.removed


async def test_a_move_that_changes_nothing_does_not_burn_the_latch() -> None:
    """Test G: an ineffective step schedules nothing and leaves the latch free.

    The second assertion is the one that matters. A claim pulled in front of the
    ``if not changed`` exit would satisfy the first (nothing is scheduled on that
    branch anyway) while leaving the latch set for the lifetime of the process,
    swallowing every later reload of this entry.
    """

    entry = _EntryStub()
    entry.add_subentry(key="target", title="Target", visible_device_ids=["dev-1"])
    entry.runtime_data.coordinator.data = [{"device_id": "dev-1", "name": "Device 1"}]

    flow = await _build_flow(entry)

    result = await flow.async_step_repairs_move(
        {"target_subentry": "target", "device_ids": ["dev-1"]}
    )
    await asyncio.sleep(0)

    assert result["type"] == "abort"
    assert result["reason"] == "subentry_move_success"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    assert _latch_is_free(flow.hass, entry.entry_id) is True


async def test_a_hopeless_entry_stops_the_repair_step_from_claiming_the_latch() -> None:
    """Test H: the state gate is reachable from this file, and it bites.

    Tests D to G above exercise the latch half of ``_schedule_claimed_reload``
    while the state gate in front of it can only ever answer "not hopeless",
    because ``_EntryStub`` now carries healthy values for the three fields it
    reads. That makes those tests honest but leaves the gate itself unmeasured
    from here -- and the gate is the reason the repair steps were routed through
    the single owner at all. This test closes that hole for the repair path: an
    entry in a terminal state must not have its latch claimed, because no setup
    would follow to hand it back, and the leaked latch would then swallow every
    later reload of this entry.

    Removing ``_entry_reload_is_hopeless`` from ``_schedule_claimed_reload``
    turns the first two assertions red; the third one guards against "fixing"
    that by skipping the write as well.
    """

    entry = _EntryStub()
    entry.state = ConfigEntryState.MIGRATION_ERROR
    entry.add_subentry(key="target", title="Target", visible_device_ids=[])
    entry.runtime_data.coordinator.data = [{"device_id": "dev-1", "name": "Device 1"}]

    flow = await _build_flow(entry)

    result = await flow.async_step_repairs_move(
        {"target_subentry": "target", "device_ids": ["dev-1"]}
    )
    await asyncio.sleep(0)

    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert _latch_is_free(flow.hass, entry.entry_id) is True
    # The step still did its job: standing down is about the reload, never
    # about the change the user asked for.
    assert result["type"] == "abort"
    assert result["reason"] == "subentry_move_success"
    assert "dev-1" in entry.subentries[next(iter(entry.subentries))].data.get(
        "visible_device_ids", []
    )


async def test_visibility_without_a_selection_does_not_schedule_a_reload() -> None:
    """Test I: closing the form without restoring anything must stay inert.

    The claim sits inside ``if to_restore:`` for this reason. Pulled in front of
    it, every visit to the visibility page that the user simply confirms would
    reload the entry, and a claim that no assignment justifies would still occupy
    the single owner for the duration of that reload.
    """

    entry = _EntryStub()
    entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}

    flow = await _build_flow(entry)
    result = await flow.async_step_visibility(
        {"subentry": TRACKER_SUBENTRY_KEY, "unignore_devices": []}
    )
    await asyncio.sleep(0)

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    assert manager.reloads == []
    assert _latch_is_free(flow.hass, entry.entry_id) is True


async def test_a_hopeless_entry_stops_the_visibility_step_from_claiming(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test J: the state gate is reachable from the visibility step too.

    Test H does this for the repair path. The visibility step took its claim
    later and would otherwise be the one assignment site whose gate nobody
    measures: a terminal entry has no setup coming that would hand the latch
    back, so a claim taken here would swallow every later reload of this entry.
    Two assertions guard against "fixing" a red gate by skipping the user's
    change as well -- standing down is about the reload, never about the
    assignment.

    The last one pins the warning. Here a stand-down costs the whole thing, not
    a half: the restored device has no entity to fall back on, so this branch
    has to say so in the log rather than return a silent success.
    """

    entry = _EntryStub()
    entry.state = ConfigEntryState.MIGRATION_ERROR
    sub = entry.add_subentry(key=TRACKER_SUBENTRY_KEY, title="Core")
    entry.options = {OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}}}

    flow = await _build_flow(entry)
    result = await flow.async_step_visibility(
        {"subentry": TRACKER_SUBENTRY_KEY, "unignore_devices": ["dev-1"]}
    )
    await asyncio.sleep(0)

    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.scheduled_reloads == []
    # Both sides (rule 8): without the second one a later rewrite that swaps
    # ``_schedule_claimed_reload`` for a direct ``async_reload`` would bypass the
    # state gate and still leave this test green.
    assert manager.reloads == []
    assert _latch_is_free(flow.hass, entry.entry_id) is True
    assert result["type"] == "create_entry"
    updated_id, payload = manager.updated[-1]
    assert updated_id == sub.subentry_id
    assert payload["visible_device_ids"] == ("dev-1",)
    assert any(
        record.levelname == "WARNING" and "no reload was scheduled" in record.message
        for record in caplog.records
    )


async def test_a_hub_typed_group_is_not_offered_as_a_device_target() -> None:
    """The hub type is excluded from assignment targets, not just the service one.

    ``_NON_DEVICE_SUBENTRY_TYPES`` is now shared with the runtime index rather
    than restated here, so a type dropped on one side would silently reappear
    as an assignable target on the other. This pins the hub half of that set:
    without it only ``service`` was covered by a test, and the coupling could
    regress unnoticed.
    """

    entry = _EntryStub()
    entry.add_subentry(
        key=TRACKER_SUBENTRY_KEY,
        title="Legacy hub",
        subentry_type=SUBENTRY_TYPE_HUB,
        visible_device_ids=["dev-1"],
    )
    flow = await _build_flow(entry)

    choices, option_map = flow._device_target_choice_map()
    (offered_key,) = choices

    assert option_map[offered_key].subentry is None, (
        "a hub-typed group must not be an assignment target, so the only "
        "offered key has to be the synthesised fallback"
    )
    assert offered_key not in {opt.key for opt in flow._gather_subentry_options()}
