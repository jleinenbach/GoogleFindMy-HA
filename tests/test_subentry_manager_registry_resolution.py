# tests/test_subentry_manager_registry_resolution.py

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, is_dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant import data_entry_flow

from custom_components.googlefindmy import (
    ConfigEntrySubentryDefinition,
    ConfigEntrySubEntryManager,
)
from custom_components.googlefindmy.const import (
    DOMAIN,
    LITERAL_CORE_KEY_OWNER,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.homeassistant import (
    DeferredRegistryConfigEntriesManager,
    FakeConfigEntriesManager,
    FakeConfigEntry,
    FakeHass,
)

try:
    # Attribute form on purpose: ``conftest.py`` installs a synthetic
    # ``homeassistant.config_entries`` in ``sys.modules``, so
    # ``from homeassistant.config_entries import ConfigSubentry`` binds the
    # *stub* -- a mutable, non-dataclass placeholder. Reaching the submodule
    # through the package attribute yields the genuine frozen dataclass, which
    # is the only shape that makes the frozen-write and ``MappingProxyType``
    # guarantees below testable at all (``tests/AGENTS.md``, point 10).
    import homeassistant.config_entries as _ha_config_entries

    _RealConfigSubentry: Any = _ha_config_entries.ConfigSubentry
except (ModuleNotFoundError, AttributeError):  # pragma: no cover - core absent
    _RealConfigSubentry = None


async def _build_runtime_manager(
    *,
    hass: FakeHass,
    parent_entry: FakeConfigEntry,
    resolved_child: SimpleNamespace,
    unique_id: str,
) -> ConfigEntrySubEntryManager:
    """Create and synchronize a runtime subentry manager for tests."""

    manager = ConfigEntrySubEntryManager(hass, parent_entry)
    definition = ConfigEntrySubentryDefinition(
        key=resolved_child.data["group_key"],
        title="Child",
        data={},
        unique_id=unique_id,
    )
    await manager.async_sync([definition])
    return manager


@pytest.mark.asyncio
async def test_managed_key_lookup_populates_subentry_id_cache() -> None:
    """Managed subentries should backfill the subentry-id cache when resolved later."""

    parent_entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    hass = FakeHass(config_entries=FakeConfigEntriesManager())
    runtime_manager = ConfigEntrySubEntryManager(hass, parent_entry)

    subentry_id = "child-subentry-id"
    runtime_manager._managed["child-group"] = SimpleNamespace(  # type: ignore[attr-defined]
        subentry_id=subentry_id,
        data={"group_key": "child-group"},
        subentry_type="tracker",
        title="Child",
        unique_id="unique-child",
    )

    assert subentry_id not in runtime_manager._managed_by_subentry_id

    resolved_key = runtime_manager._managed_key_for_subentry_id(subentry_id)

    assert resolved_key == "child-group"
    assert runtime_manager._managed_by_subentry_id[subentry_id] == "child-group"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["legacy", "dataclass"], ids=["legacy", "dataclass"])
async def test_async_sync_caches_resolved_registry_subentry(
    monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """Ensure async_sync stores the registry-backed child in managed state."""

    child_entry_id = "child-resolved-ulid"
    parent_entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)

    if shape == "legacy":
        resolved_child: Any = SimpleNamespace(
            entry_id=child_entry_id,
            subentry_id="child-subentry-id",
            unique_id="unique-child",
            data={"group_key": "child-group"},
            subentry_type="tracker",
            state=None,
        )
    # Asymmetry worth knowing about: unlike ``_ap1_subentry`` below, this
    # branch degrades *silently*. If the import ever binds the mutable stub
    # again, ``is_dataclass`` turns false and the ``dataclass`` parameter id
    # keeps running -- against the locally defined ``_FrozenSubentry`` in the
    # ``else`` branch. That fallback is genuinely frozen, so the case is not
    # vacuous, but it no longer measures core fidelity under a name that
    # promises it. ``_ap1_subentry`` is where a substituted stub is made loud.
    elif _RealConfigSubentry is not None and is_dataclass(_RealConfigSubentry):
        resolved_child = _RealConfigSubentry(
            data=MappingProxyType({"group_key": "child-group"}),
            subentry_id="child-subentry-id",
            subentry_type="tracker",
            title="Child",
            unique_id="unique-child",
        )
    else:

        @dataclass(frozen=True, kw_only=True)
        class _FrozenSubentry:
            data: Mapping[str, Any]
            subentry_type: str
            title: str
            unique_id: str | None
            subentry_id: str
            translation_key: str | None = None

            def __post_init__(self) -> None:
                object.__setattr__(self, "data", MappingProxyType(dict(self.data)))

        monkeypatch.setattr(
            "custom_components.googlefindmy.ConfigSubentry",
            _FrozenSubentry,
        )
        resolved_child = _FrozenSubentry(
            data={"group_key": "child-group"},
            subentry_id="child-subentry-id",
            subentry_type="tracker",
            title="Child",
            unique_id="unique-child",
        )

    manager = DeferredRegistryConfigEntriesManager(parent_entry, resolved_child)
    hass = FakeHass(config_entries=manager)

    runtime_manager = await _build_runtime_manager(
        hass=hass,
        parent_entry=parent_entry,
        resolved_child=resolved_child,
        unique_id=getattr(resolved_child, "unique_id", None) or "unique-child",
    )

    stored = runtime_manager.get("child-group")
    assert stored is not None
    if shape == "legacy":
        assert getattr(stored, "entry_id", None) == child_entry_id
    else:
        assert getattr(stored, "entry_id", None) is None
    assert getattr(stored, "subentry_id", None) == resolved_child.subentry_id
    assert stored is resolved_child
    assert stored is not manager.provisional_subentry

    managed_snapshot = runtime_manager.managed_subentries
    assert managed_snapshot["child-group"] is stored

    subentry_id = getattr(resolved_child, "subentry_id", None)
    if isinstance(subentry_id, str) and subentry_id:
        assert runtime_manager._managed_by_subentry_id.get(subentry_id) == "child-group"


def test_update_visible_device_ids_refreshes_dataclass_subentry() -> None:
    """Ensure visibility updates retain dataclass-backed subentries in cache."""

    key = "child-group"
    subentry_id = "child-subentry-id"

    @dataclass(frozen=True, kw_only=True)
    class _FrozenSubentry:
        data: Mapping[str, Any]
        subentry_type: str
        title: str
        unique_id: str | None
        subentry_id: str
        translation_key: str | None = None

        def __post_init__(self) -> None:
            object.__setattr__(self, "data", MappingProxyType(dict(self.data)))

    entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    existing = _FrozenSubentry(
        data={"group_key": key},
        subentry_type="tracker",
        title="Child",
        unique_id="unique-child",
        subentry_id=subentry_id,
    )
    entry.subentries[subentry_id] = existing

    class _ConfigEntriesStub(FakeConfigEntriesManager):
        def __init__(self, managed_entry: FakeConfigEntry) -> None:
            super().__init__([managed_entry])
            self._entry = managed_entry
            self.payloads: list[dict[str, Any]] = []

        def async_update_subentry(
            self,
            entry_arg: FakeConfigEntry,
            subentry_arg: Any,
            *,
            data: dict[str, Any],
        ) -> _FrozenSubentry:
            assert entry_arg is self._entry
            self.payloads.append(dict(data))
            replacement = _FrozenSubentry(
                data=data,
                subentry_type=getattr(subentry_arg, "subentry_type", "tracker"),
                title=getattr(subentry_arg, "title", "Child"),
                unique_id=getattr(subentry_arg, "unique_id", None),
                subentry_id=subentry_id,
                translation_key=getattr(subentry_arg, "translation_key", None),
            )
            self._entry.subentries[subentry_id] = replacement
            return replacement

    hass = FakeHass(config_entries=_ConfigEntriesStub(entry))

    manager = ConfigEntrySubEntryManager(hass, entry)  # type: ignore[arg-type]
    managed_before = manager.get(key)
    assert managed_before is existing

    manager.update_visible_device_ids(key, ["device-2", "device-2", "device-1"])

    stored = manager.get(key)
    assert stored is not None
    assert stored is entry.subentries[subentry_id]
    assert stored is not existing
    assert getattr(stored, "entry_id", None) is None
    assert isinstance(stored.data.get("visible_device_ids"), list)
    assert stored.data["visible_device_ids"] == ["device-2", "device-1"]
    assert hass.config_entries.payloads[-1]["visible_device_ids"] == [
        "device-2",
        "device-1",
    ]
    assert manager._managed_by_subentry_id.get(subentry_id) == key


@dataclass(frozen=True, kw_only=True)
class _AliasSubentry:
    """Minimal subentry double for the alias-resolution tests."""

    data: Mapping[str, Any]
    subentry_type: str
    title: str
    unique_id: str | None
    subentry_id: str
    translation_key: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))


class _CapturingConfigEntries(FakeConfigEntriesManager):
    """Record which subentry a visibility write lands on."""

    def __init__(self, managed_entry: FakeConfigEntry) -> None:
        super().__init__([managed_entry])
        self._entry = managed_entry
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def async_update_subentry(
        self,
        entry_arg: FakeConfigEntry,
        subentry_arg: Any,
        *,
        data: dict[str, Any],
    ) -> _AliasSubentry:
        subentry_id = subentry_arg.subentry_id
        self.writes.append((subentry_id, dict(data)))
        replacement = _AliasSubentry(
            data=data,
            subentry_type=subentry_arg.subentry_type,
            title=subentry_arg.title,
            unique_id=subentry_arg.unique_id,
            subentry_id=subentry_id,
        )
        self._entry.subentries[subentry_id] = replacement
        return replacement


def _entry_with_twins(
    stored_key: str, *, service_first: bool = True
) -> FakeConfigEntry:
    """Return an entry whose service and tracker subentry share ``stored_key``.

    ``service_first`` fixes the order in which the entry yields them, because
    the defect these tests pin was order dependent.
    """

    entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    specs = [
        ("svc", "service", "Service"),
        ("trk", "tracker", "Trackers"),
    ]
    if not service_first:
        specs.reverse()
    for slug, subentry_type, title in specs:
        subentry = _AliasSubentry(
            data={"group_key": stored_key},
            subentry_type=subentry_type,
            title=title,
            unique_id=f"unique-{slug}",
            subentry_id=f"{slug}-subentry",
        )
        entry.subentries[subentry.subentry_id] = subentry
    return entry


@pytest.mark.parametrize("service_first", [True, False])
def test_a_service_twin_cannot_alias_the_tracker_key_to_itself(
    service_first: bool,
) -> None:
    """A core key names its own group and must never stand in for another.

    A service subentry left over from an early migration can still store
    ``core_tracking``. Its canonical identity is the service group, so the
    alias table used to learn ``core_tracking -> service`` and route every
    write meant for the tracker group onto the service twin.
    """

    entry = _entry_with_twins("core_tracking", service_first=service_first)
    hass = FakeHass(config_entries=_CapturingConfigEntries(entry))

    manager = ConfigEntrySubEntryManager(hass, entry)  # type: ignore[arg-type]

    # Effect first: the assertions below must fail on their own when the
    # guard is removed, so this test cannot be carried by its structural
    # check alone.
    manager.update_visible_device_ids("core_tracking", ["device-1"])

    written = hass.config_entries.writes
    assert [subentry_id for subentry_id, _ in written] == ["trk-subentry"], (
        "the device list belongs to the tracker group; the service twin must "
        "never receive it"
    )
    assert entry.subentries["svc-subentry"].data.get("visible_device_ids") is None

    resolved = manager.get("core_tracking")
    assert resolved is not None
    assert resolved.subentry_id == "trk-subentry"


@pytest.mark.parametrize("service_first", [True, False])
def test_a_key_claimed_by_two_subentries_resolves_to_nothing(
    service_first: bool,
) -> None:
    """An ambiguous stored key must miss rather than hit an arbitrary twin."""

    entry = _entry_with_twins("owner@example.com", service_first=service_first)
    hass = FakeHass(config_entries=_CapturingConfigEntries(entry))

    manager = ConfigEntrySubEntryManager(hass, entry)  # type: ignore[arg-type]

    manager.update_visible_device_ids("owner@example.com", ["device-1"])

    assert hass.config_entries.writes == [], (
        "with two subentries claiming the key there is no single right answer, "
        "so the write must not pick one"
    )
    assert manager.get("owner@example.com") is None


@pytest.mark.parametrize("service_position", [0, 1, 2])
def test_an_ambiguous_key_stays_ambiguous_for_every_later_claimant(
    service_position: int,
) -> None:
    """A third subentry with the same key must not re-establish the alias.

    Dropping the conflicting mapping alone would leave the key free again, so
    whichever subentry is iterated afterwards would re-register it and the
    resolution would once more depend on iteration order. The ambiguity is
    therefore remembered for the rest of the pass, whatever position the
    service twin holds.
    """

    stored_key = "owner@example.com"
    entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    specs = [
        ("trk1", "tracker", "Alpha trackers"),
        ("trk2", "tracker", "Beta trackers"),
    ]
    specs.insert(service_position, ("svc", "service", "Service"))
    for slug, subentry_type, title in specs:
        subentry = _AliasSubentry(
            data={"group_key": stored_key},
            subentry_type=subentry_type,
            title=title,
            unique_id=f"unique-{slug}",
            subentry_id=f"{slug}-subentry",
        )
        entry.subentries[subentry.subentry_id] = subentry

    hass = FakeHass(config_entries=_CapturingConfigEntries(entry))
    manager = ConfigEntrySubEntryManager(hass, entry)  # type: ignore[arg-type]

    manager.update_visible_device_ids(stored_key, ["device-1"])

    assert hass.config_entries.writes == []
    assert manager.get(stored_key) is None


def test_an_ambiguous_key_is_not_answered_by_a_subentry_of_a_retired_type() -> None:
    """Ambiguity must survive a subentry whose own identity is that key.

    A subentry type that a later release stopped offering still sits in
    ``.storage`` after an upgrade. Its canonical identity falls back to the
    stored key itself, so the key exists in the managed mapping even though no
    alias points at it. Dropping the conflicting alias alone would therefore
    still hand that leftover out for a key two other subentries are fighting
    over.
    """

    stored_key = "owner@example.com"
    entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    for slug, subentry_type, title in (
        ("svc", "service", "Service"),
        ("trk", "tracker", "Trackers"),
        ("old", "retired-group", "Retired group"),
    ):
        subentry = _AliasSubentry(
            data={"group_key": stored_key},
            subentry_type=subentry_type,
            title=title,
            unique_id=f"unique-{slug}",
            subentry_id=f"{slug}-subentry",
        )
        entry.subentries[subentry.subentry_id] = subentry

    hass = FakeHass(config_entries=_CapturingConfigEntries(entry))
    manager = ConfigEntrySubEntryManager(hass, entry)  # type: ignore[arg-type]

    # Precondition of this test: the retired type really does claim the key as
    # its own canonical identity, so a missing guard has something to hit.
    assert manager._managed.get(stored_key) is entry.subentries["old-subentry"]

    manager.update_visible_device_ids(stored_key, ["device-1"])

    assert hass.config_entries.writes == []
    assert manager.get(stored_key) is None


@pytest.mark.parametrize("service_first", [True, False])
def test_a_subentry_that_owns_its_stored_key_contests_a_foreign_alias(
    service_first: bool,
) -> None:
    """A self-claiming subentry must be able to make a key ambiguous.

    A ``hub``-typed subentry canonicalises onto its own stored key, so it
    registers no redirection. Counting only redirections would leave such a
    key uncontested and let a service twin capture it, which is the same
    hijack the core-key guard closes for ``core_tracking``.
    """

    stored_key = "owner@example.com"
    entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    specs = [("hub", "hub", "Hub"), ("svc", "service", "Service")]
    if service_first:
        specs.reverse()
    for slug, subentry_type, title in specs:
        subentry = _AliasSubentry(
            data={"group_key": stored_key},
            subentry_type=subentry_type,
            title=title,
            unique_id=f"unique-{slug}",
            subentry_id=f"{slug}-subentry",
        )
        entry.subentries[subentry.subentry_id] = subentry

    hass = FakeHass(config_entries=_CapturingConfigEntries(entry))
    manager = ConfigEntrySubEntryManager(hass, entry)  # type: ignore[arg-type]

    manager.update_visible_device_ids(stored_key, ["device-1"])

    assert hass.config_entries.writes == [], (
        "the service twin must not receive device ids through a key the hub "
        "subentry owns"
    )
    assert manager.get(stored_key) is None


def test_the_canonical_service_key_is_not_captured_by_a_tracker_twin() -> None:
    """The reserved-key guard covers the service key, not only the tracker one."""

    entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    for slug, subentry_type, title in (
        ("trk", "tracker", "Trackers"),
        ("svc", "service", "Service"),
    ):
        subentry = _AliasSubentry(
            data={"group_key": "service"},
            subentry_type=subentry_type,
            title=title,
            unique_id=f"unique-{slug}",
            subentry_id=f"{slug}-subentry",
        )
        entry.subentries[subentry.subentry_id] = subentry

    hass = FakeHass(config_entries=_CapturingConfigEntries(entry))
    manager = ConfigEntrySubEntryManager(hass, entry)  # type: ignore[arg-type]

    resolved = manager.get("service")
    assert resolved is not None, (
        "a tracker twin storing the service key must not turn the service "
        "group into an unresolvable one"
    )
    assert resolved.subentry_id == "svc-subentry"


@pytest.mark.parametrize("subentry_type", ["service", "hub"], ids=["service", "hub"])
@pytest.mark.parametrize(
    "stored_key", ["core_tracking", "owner@example.com", "service"]
)
def test_update_visible_device_ids_refuses_a_non_device_bearing_type(
    subentry_type: str, stored_key: str
) -> None:
    """The manager must not store device ids on a service or hub subentry.

    ``config_flow._accepts_device_assignment`` keeps the options flow from
    offering such a group as an assignment target, but it lives in another
    module and cannot see this call. Without a check here the invariant rested
    entirely on every caller resolving to the right key, so a single caller
    passing a key that resolves to a service or hub subentry wrote devices onto
    the one group that must not hold any.

    Deciding on the resolved subentry rather than on the key is what makes this
    total, and the three stored keys reach it by three different routes. One
    cell is deliberately *not* carried by this guard: a ``service``-typed
    subentry storing ``core_tracking`` is canonicalised onto ``service``, and
    the reserved-key rule stops ``core_tracking`` from aliasing anywhere, so
    the lookup finds nothing and the method returns one line earlier. The
    precondition below is what tells the two apart, so a cell cannot pass by
    never reaching the guard at all.
    """

    entry = FakeConfigEntry(entry_id="parent-entry", domain=DOMAIN)
    subentry = _AliasSubentry(
        data={"group_key": stored_key},
        subentry_type=subentry_type,
        title="Non-device group",
        unique_id="unique-non-device",
        subentry_id="non-device-subentry",
    )
    entry.subentries[subentry.subentry_id] = subentry

    hass = FakeHass(config_entries=_CapturingConfigEntries(entry))
    manager = ConfigEntrySubEntryManager(hass, entry)  # type: ignore[arg-type]

    resolved = manager._resolve_key(stored_key)
    reaches_the_guard = (
        resolved is not None and manager._managed.get(resolved) is subentry
    )
    carried_by_the_reserved_key_rule = (
        subentry_type == "service" and stored_key == TRACKER_SUBENTRY_KEY
    )
    assert reaches_the_guard is not carried_by_the_reserved_key_rule, (
        "precondition: every cell except the reserved-key one must actually "
        "reach the guard, otherwise a passing assertion proves nothing"
    )

    manager.update_visible_device_ids(stored_key, ["device-1"])

    assert hass.config_entries.writes == [], (
        f"a {subentry_type!r} subentry must never receive visible_device_ids, "
        f"whatever key it stores (here {stored_key!r})"
    )
    stored = entry.subentries[subentry.subentry_id]
    assert "visible_device_ids" not in stored.data, (
        "and the stored payload must be left untouched, not merely the write log"
    )


# ---------------------------------------------------------------------------
# AP1 (PLAN_GFMY_ALIAS_TYPE_AXIS): characterising the subentry manager axis.
#
# Every test below states its class in its own docstring:
#   Class A -- standing assertion. It must stay green while this plan runs;
#              AP3 turning one red is a FAIL of AP3, not an adjustment.
#              Two sub-cases, both binding:
#                A1 -- behaviour we want (the tracker fold, kept by V-1).
#                A2 -- behaviour we carry. Changing it is reserved for
#                      PLAN_GFMY_SUBENTRY_DELETION_TYPE_AXIS, so it must not
#                      move as a side effect here either.
#   Class B -- characterisation. AP3 (rank) or AP4 (reading side) turns
#              exactly these over.
#
# Substitution declared: the plan asks the second Class B case to pin the
# reading side (``_refresh_subentry_index`` and
# ``metadata[TRACKER_SUBENTRY_KEY].config_subentry_id``). It is pinned here on
# the manager side instead, because AP4 builds the coordinator fixture that
# case needs anyway; the reading-side twin moves into AP4 rather than being
# dropped.
# ---------------------------------------------------------------------------

_LEGACY_EMAIL_KEY = "user@example.com"
# Index of ``type_match`` in ``_candidate_score``'s tuple. A name, not a
# guard: inserting a field in front of it moves the assertion onto its
# neighbour exactly as a literal would. It is named so the *fix* is one line
# when that happens, and so a reader sees which field is meant.
_TYPE_FIELD = 1


def _ap1_subentry(
    subentry_id: str, group_key: str | None, subentry_type: str, unique_id: str
) -> Any:
    """Build a subentry double.

    A ``ConfigSubentry`` rather than an ad-hoc ``SimpleNamespace``: that is the
    type the production code type-checks against. Entry doubles do go through
    ``make_config_entry`` (``tests/AGENTS.md``, "Canonical config-entry stub");
    subentry doubles do not, because that factory builds a ConfigEntry -- the
    wrong object type here, which is why ``test_guard_config_entry_stub``
    exempts subentry markers.
    """

    # Measured, not assumed: every guarantee the doubles below claim (frozen
    # writes, ``MappingProxyType`` data) is vacuous against the mutable
    # ``conftest`` stand-in, and a stub silently substituted for the core class
    # would make those claims pass without testing anything. Assert the shape
    # here so the substitution fails loudly instead.
    assert _RealConfigSubentry is not None, "core config_entries not importable"
    assert is_dataclass(_RealConfigSubentry), (
        f"{_RealConfigSubentry.__module__}.{_RealConfigSubentry.__qualname__} is not a "
        "dataclass -- the conftest stub was bound instead of the core class"
    )
    assert _RealConfigSubentry.__dataclass_params__.frozen, (
        "core ConfigSubentry is expected to be frozen; the frozen-write "
        "guarantees asserted by the doubles below are vacuous otherwise"
    )
    data = {"group_key": group_key} if group_key is not None else {}
    return _RealConfigSubentry(
        # Fidelity, not a proven guard, and said plainly because the mutation
        # says so: the core declares ``data: MappingProxyType[str, Any]`` with
        # no ``__post_init__`` coercion, so this matches the real shape --
        # but swapping it for a plain dict kills no test here (measured).
        # The reason, stated as measured rather than as a sweeping rule: *no*
        # subentry path in production narrows ``data`` to ``dict``. They either
        # narrow to ``Mapping`` (``__init__.py:4294``) or duck-type straight
        # through ``.get()`` with no narrowing at all (``config_flow.py:1404``,
        # ``__init__.py:4772``), and both forms satisfy either. The six
        # ``isinstance(data, dict)`` sites in the integration read unrelated
        # payloads -- none of them a ``ConfigSubentry``. Keep the faithful
        # shape so a future ``dict``-narrowing guard fails here rather than
        # only in production, and treat that as the reason -- not a claim that
        # it is pinned today.
        data=MappingProxyType(data),
        subentry_type=subentry_type,
        title=f"{subentry_type}:{group_key}",
        unique_id=unique_id,
        subentry_id=subentry_id,
    )


def _ap1_entry(entry_id: str, subentries: list[Any]) -> Any:
    """Config entry double whose ``subentries`` mirrors the core type.

    The core stores ``MappingProxyType`` (``config_entries.py``: ``subentries:
    MappingProxyType[str, ConfigSubentry]``), so the double does too; a plain
    dict would let a test mutate what production cannot.
    """

    return make_config_entry(
        entry_id=entry_id,
        domain=DOMAIN,
        subentries=MappingProxyType({s.subentry_id: s for s in subentries}),
    )


class _CoreLikeSubentryEntries(FakeConfigEntriesManager):
    """Config-entries manager that mirrors the core subentry contract.

    Subclasses ``FakeConfigEntriesManager`` as ``tests/AGENTS.md`` ("Custom
    config entries manager subclasses") requires; the base class carries no
    subentry mutators at all, so they are added here.

    Read out of ``homeassistant/config_entries.py`` rather than assumed:

    * ``async_update_subentry`` runs the unique-id collision check *only* when a
      ``unique_id`` is passed and differs from the stored one, raising
      ``AbortFlow("already_configured")``; it returns ``False`` when nothing
      changed, which is what steers the re-indexing branch in production.
    * it writes through ``object.__setattr__``, because ``ConfigSubentry`` is a
      frozen dataclass in the core. Assigning attributes directly would only
      work against the mutable test stub -- the trap ``tests/AGENTS.md`` names.
    * ``async_remove_subentry`` clears the device *and* the entity registry
      binding, so every removal is recorded here.
    * removal replaces ``entry.subentries`` with a fresh ``MappingProxyType``
      instead of mutating in place, exactly as ``_async_update_entry`` does.
      An update does not touch ``entry.subentries`` at all -- the core writes
      through to the subentry object and saves, so the double must not either.

    The unique-id collision branch is kept although no test currently reaches
    it: production actively catches ``AbortFlow("already_configured")`` from
    this call and recovers, so a double that cannot raise it would silently
    exclude a live production path. That is a different case from
    ``async_create_subentry``, which is deliberately absent because the core
    has no such method at all (production probes it via ``getattr`` and takes a
    non-core branch when present).

    One deliberate deviation: the core raises ``UnknownSubEntry`` for an unknown
    id, this double returns ``False``. No test drives that path, and importing
    the exception at module level would defeat the guarded import above.
    """

    def __init__(self, entry: Any) -> None:
        super().__init__([entry])
        self.removed: list[str] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []

    @staticmethod
    def _replace(entry: Any, mapping: dict[str, Any]) -> None:
        entry.subentries = MappingProxyType(mapping)

    @staticmethod
    def _raise_if_unique_id_exists(entry: Any, unique_id: str | None) -> None:
        if unique_id is None:
            return
        for existing in entry.subentries.values():
            if getattr(existing, "unique_id", None) == unique_id:
                raise data_entry_flow.AbortFlow("already_configured")

    def async_remove_subentry(self, entry: Any, subentry_id: str) -> bool:
        mapping = dict(entry.subentries)
        if subentry_id not in mapping:
            return False
        mapping.pop(subentry_id)
        self.removed.append(subentry_id)
        self._replace(entry, mapping)
        return True

    def async_update_subentry(self, entry: Any, subentry: Any, **kwargs: Any) -> bool:
        setter = object.__setattr__
        sentinel = object()
        changed = False

        unique_id = kwargs.get("unique_id", sentinel)
        if unique_id is not sentinel and subentry.unique_id != unique_id:
            self._raise_if_unique_id_exists(entry, unique_id)
            setter(subentry, "unique_id", unique_id)
            changed = True

        title = kwargs.get("title", sentinel)
        if title is not sentinel and subentry.title != title:
            setter(subentry, "title", title)
            changed = True

        data = kwargs.get("data", sentinel)
        if data is not sentinel and dict(subentry.data) != dict(data):
            setter(subentry, "data", MappingProxyType(dict(data)))
            changed = True

        self.updated.append((subentry.subentry_id, dict(kwargs)))
        return changed


def _ap1_setup(entry_id: str, subentries: list[Any]) -> tuple[Any, Any, Any]:
    """Return ``(entry, recorder, manager)`` wired the way production is."""

    entry = _ap1_entry(entry_id, subentries)
    recorder = _CoreLikeSubentryEntries(entry)
    manager = ConfigEntrySubEntryManager(FakeHass(config_entries=recorder), entry)
    return entry, recorder, manager


def _ap1_core_definitions(entry_id: str) -> list[ConfigEntrySubentryDefinition]:
    return [
        ConfigEntrySubentryDefinition(
            key=TRACKER_SUBENTRY_KEY,
            title="Devices",
            data={},
            subentry_type=SUBENTRY_TYPE_TRACKER,
            unique_id=f"{entry_id}-{TRACKER_SUBENTRY_KEY}",
        ),
        ConfigEntrySubentryDefinition(
            key=SERVICE_SUBENTRY_KEY,
            title="Service",
            data={},
            subentry_type=SUBENTRY_TYPE_SERVICE,
            unique_id=f"{entry_id}-{SERVICE_SUBENTRY_KEY}",
        ),
    ]


# --- Class A: standing assertions -----------------------------------------


def test_ap1_a_tracker_type_folds_legacy_email_key_onto_core_tracking() -> None:
    """Class A1 (standing assertion, behaviour we want).

    A ``tracker``-typed subentry that still stores a legacy e-mail group key is
    reachable under ``core_tracking`` and does not appear under the stored key
    in the managed mapping (``__init__.py``, ``_refresh_from_entry``, tracker
    fold). It stays *resolvable* by the stored key through the alias table --
    what is asserted here is the mapping, not resolvability.

    V-1 decided the fold stays: it is the shield that keeps such a group inside
    ``desired`` and therefore out of the stale sweep. Turning it over is a
    decision, not a refactoring. Complements
    ``test_config_flow_subentry_sync.py`` rather than replacing it.
    """

    legacy = _ap1_subentry("id-legacy", _LEGACY_EMAIL_KEY, SUBENTRY_TYPE_TRACKER, "u-1")
    _entry, _recorder, manager = _ap1_setup("e1", [legacy])

    assert manager.get(TRACKER_SUBENTRY_KEY) is legacy
    assert _LEGACY_EMAIL_KEY not in manager.managed_subentries


def test_ap1_a_hub_type_keeps_stored_core_tracking_key() -> None:
    """Class A2 (standing assertion, behaviour we carry).

    A ``hub``-typed subentry storing ``core_tracking`` keeps that key; its type
    does not pull it onto ``service``. The contract states this in
    ``coordinator/subentry.py`` (the comment above the index fold:
    ``_refresh_from_entry`` "canonicalises ``service`` and ``tracker`` by type
    but leaves ``hub`` on its stored key"). Note the scope: that holds for the
    *manager*. The very same file folds ``hub`` onto ``SERVICE_SUBENTRY_KEY``
    for its own index a few lines below, via ``NON_DEVICE_SUBENTRY_TYPES`` --
    the asymmetry is deliberate and is what this test pins on the manager side.

    Discriminating power sits in the second assertion; the first only states
    the key is present at all.
    """

    hub = _ap1_subentry("id-hub", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, "u-2")
    _entry, _recorder, manager = _ap1_setup("e1", [hub])

    assert TRACKER_SUBENTRY_KEY in manager.managed_subentries
    assert SERVICE_SUBENTRY_KEY not in manager.managed_subentries


def test_ap1_a_hub_type_with_email_key_stays_on_that_key() -> None:
    """Class A2 (standing assertion, behaviour we carry).

    A ``hub``-typed subentry with an e-mail group key is indexed under that very
    key -- no fold applies, so this is the sharp evidence that the stored key
    decides for a non-core type. It is also the precondition of the stale-sweep
    pins below: landing outside ``{core_tracking, service}`` is what puts a
    group in reach of the sweep.
    """

    hub = _ap1_subentry("id-hub", _LEGACY_EMAIL_KEY, SUBENTRY_TYPE_HUB, "u-3")
    _entry, _recorder, manager = _ap1_setup("e1", [hub])

    assert manager.managed_subentries.get(_LEGACY_EMAIL_KEY) is hub


# --- Class B, turned over by AP3: standing assertions now ------------------
#
# Both assertions below were characterisation at 5e964a1f: they recorded that
# the manager resolved a key collision by load order, and named the change that
# would end it. AP3 made that change, so they now assert the rule rather than
# the symptom. The pre-AP3 wording is in the history of this file; the anchor
# commit is 5e964a1f.


@pytest.mark.parametrize("reverse", [False, True], ids=["fwd", "rev"])
def test_ap3_candidate_score_reads_the_type_axis_on_key_collision(
    reverse: bool,
) -> None:
    """Class A (standing assertion; AP3 turned this over from Class B).

    Two candidates collide on ``core_tracking`` and differ only in
    ``subentry_type`` (their ``subentry_id`` and title differ too, but neither
    reaches the decision: the score reads only whether an id is *present*, and
    the identifier tie-break is not consulted once the scores differ). Before
    AP3 both produced the same score, the strict ``>`` fell through, and the
    winner was whichever the entry yielded first. Now the type that literally
    owns the key per ``LITERAL_CORE_KEY_OWNER`` outranks the one that does not,
    in either order.

    The ``hub`` here is deliberately given the *lower* ``subentry_id``
    (``id-a-hub`` < ``id-b-tracker``), so it would win the identifier tie-break
    if the type field were removed. That is what makes this an assertion about
    the type axis rather than about determinism in general: neutralising
    ``type_match`` does not merely make the outcome order-dependent, it flips
    the winner to the hub in *both* orders.
    """

    tracker = _ap1_subentry(
        "id-b-tracker", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-x"
    )
    hub = _ap1_subentry("id-a-hub", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, "e1-x")
    order = [hub, tracker] if reverse else [tracker, hub]
    _entry, _recorder, manager = _ap1_setup("e1", order)

    tracker_score = manager._candidate_score(tracker, key=TRACKER_SUBENTRY_KEY)
    hub_score = manager._candidate_score(hub, key=TRACKER_SUBENTRY_KEY)
    assert tracker_score > hub_score
    assert manager.get(TRACKER_SUBENTRY_KEY) is tracker


def test_ap3_candidate_score_type_axis_is_neutral_off_the_core_keys() -> None:
    """Class A (standing assertion).

    ``LITERAL_CORE_KEY_OWNER`` only names the two core keys. For any other key
    the lookup yields ``None``, so *no* candidate can match and the field is
    uniformly ``0``. Without that guard every non-core key would acquire a
    second ordering nobody chose.

    The third candidate carries ``subentry_type=None`` and is the only one that
    pins the guard rather than the field. An earlier version of this test
    claimed the guard was asserted and did not assert it: with two *typed*
    candidates, dropping ``canonical_owner is not None`` leaves both at ``0``
    anyway and the mutation survives (measured). Only ``None == None`` turns
    the missing table entry into a *match*, which would hand an untyped
    subentry a rank nobody granted it on every non-core key.

    That candidate is a ``SimpleNamespace`` rather than a ``ConfigSubentry``,
    deliberately and against the file's usual rule: the core declares
    ``subentry_type: str``, so the shape cannot be built from the real class,
    and ``_candidate_score`` reads every attribute through ``getattr`` without
    narrowing. The tree treats untyped subentries as a real if defensive shape
    (``agents/config_flow/AGENTS.md`` on the stale sweep,
    ``_canonical_core_key_of``), which is why the guard exists at all.
    """

    tracker = _ap1_subentry(
        "id-t", _LEGACY_EMAIL_KEY, SUBENTRY_TYPE_TRACKER, "e1-legacy"
    )
    hub = _ap1_subentry("id-h", _LEGACY_EMAIL_KEY, SUBENTRY_TYPE_HUB, "e1-legacy")
    untyped = SimpleNamespace(
        data=MappingProxyType({"group_key": _LEGACY_EMAIL_KEY}),
        subentry_type=None,
        title="untyped",
        unique_id="e1-legacy",
        subentry_id="id-u",
    )
    _entry, _recorder, manager = _ap1_setup("e1", [tracker])

    assert LITERAL_CORE_KEY_OWNER.get(_LEGACY_EMAIL_KEY) is None
    assert (
        manager._candidate_score(tracker, key=_LEGACY_EMAIL_KEY)[_TYPE_FIELD]
        == manager._candidate_score(hub, key=_LEGACY_EMAIL_KEY)[_TYPE_FIELD]
        == manager._candidate_score(untyped, key=_LEGACY_EMAIL_KEY)[_TYPE_FIELD]
        == 0
    )


@pytest.mark.parametrize("reverse", [False, True], ids=["fwd", "rev"])
def test_ap3_an_exact_stored_key_outranks_a_folded_twin(reverse: bool) -> None:
    """Class A (standing assertion).

    This pins ``exact_key``, the **first** field of the rank tuple (index 0):
    a subentry that stores ``core_tracking`` itself beats one that merely folds
    onto it, in either order. Both are ``tracker``-typed, so the type axis ties
    and cannot decide.

    This field is here because leaving it out was a measured regression rather
    than a hypothetical: without it the legacy twin below wins on the
    identifier tie-break (``id-legacy`` < ``id-tracker``) and takes the
    ``core_tracking`` slot. The failure this fixture then shows -- ``async_sync``
    raising for a unique id no subentry holds -- is the *loud* form of it, and it
    is loud because these fixture ids (``e1-t``, ``e1-legacy``) are not of the
    ``f"{entry_id}-{key}"`` shape every production writer uses. On disk the
    adoption finds an owner and writes the payload onto the wrong subentry
    silently; see ``_candidate_score``'s docstring for the writer list. The
    fixture keeps the identifier ordering on purpose -- the twin must be able to
    win the later fields -- so neutralising ``exact_key`` flips the outcome
    instead of merely making it order-dependent.
    """

    canonical = _ap1_subentry(
        "id-tracker", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-t"
    )
    legacy_twin = _ap1_subentry(
        "id-legacy", _LEGACY_EMAIL_KEY, SUBENTRY_TYPE_TRACKER, "e1-legacy"
    )
    order = [legacy_twin, canonical] if reverse else [canonical, legacy_twin]
    _entry, _recorder, manager = _ap1_setup("e1", order)

    assert manager._candidate_score(
        canonical, key=TRACKER_SUBENTRY_KEY
    ) > manager._candidate_score(legacy_twin, key=TRACKER_SUBENTRY_KEY)
    assert legacy_twin.subentry_id < canonical.subentry_id, (
        "the fixture must let the twin win the identifier tie-break, or "
        "neutralising exact_key would only make the outcome order-dependent"
    )
    slot = manager.get(TRACKER_SUBENTRY_KEY)
    assert slot is not None and slot.subentry_id == "id-tracker"


@pytest.mark.parametrize("reverse", [False, True], ids=["fwd", "rev"])
def test_ap3_duplicate_tracker_group_resolves_by_identifier_not_load_order(
    reverse: bool,
) -> None:
    """Class A (standing assertion; AP3 turned this over from Class B).

    Two ``tracker``-typed subentries collide on ``core_tracking``. Their scores
    tie on every field including the type axis -- same type, same key -- so this
    is the case the identifier tie-break exists for. Before AP3 the strict ``>``
    fell through and ``_refresh_from_entry`` kept whichever came first, so the
    owning ``subentry_id`` differed between the two orders. Now the lowest
    ``subentry_id`` wins in both.

    Lowest rather than highest, and that direction is the load-bearing part.
    The source of the direction is ``config_flow.py::_resolve_existing``, whose
    ``min(...)`` is parametric in the key and therefore already applies to
    ``core_tracking``. The counterpart it converges on is
    ``coordinator/subentry.py``'s final rank field ``subentry_id or ""``. When
    this test was written that citation would have been wrong -- the field sat
    inside ``if group_key == SERVICE_SUBENTRY_KEY:`` and had no counterpart on
    this key -- and an earlier draft made exactly that claim and then
    contradicted itself four lines later. AP4 widened the block to both core
    keys, so the counterpart now exists.

    Scope, stated because it is narrower than the name suggests: this pins the
    *manager*. That both sides now name the same subentry is a separate
    assertion and lives where it can see both
    (``tests/test_coordinator_subentry_visibility.py::test_ap4_the_manager_and_the_index_agree_on_the_tracker_slot``).
    """

    first = _ap1_subentry(
        "id-first", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-dup-a"
    )
    second = _ap1_subentry(
        "id-second", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-dup-b"
    )
    order = [second, first] if reverse else [first, second]
    _entry, _recorder, manager = _ap1_setup("e1", order)

    assert manager._candidate_score(
        first, key=TRACKER_SUBENTRY_KEY
    ) == manager._candidate_score(second, key=TRACKER_SUBENTRY_KEY)
    assert manager.get(TRACKER_SUBENTRY_KEY) is first


# --- Stale-sweep pins: characterise, do not change -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "group_key,subentry_type,expect_removed",
    [
        (_LEGACY_EMAIL_KEY, SUBENTRY_TYPE_HUB, True),
        (None, SUBENTRY_TYPE_HUB, True),
        (SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, False),
        (_LEGACY_EMAIL_KEY, SUBENTRY_TYPE_TRACKER, False),
    ],
    ids=[
        "hub-email-removed",
        "hub-nokey-removed",
        "hub-service-kept",
        "tracker-email-kept",
    ],
)
async def test_ap1_stale_sweep_is_decided_by_the_resolved_key(
    group_key: str | None, subentry_type: str, expect_removed: bool
) -> None:
    """Class A2 (standing assertion, behaviour we carry).

    ``async_sync`` sweeps managed keys outside ``desired`` and removes them
    through ``async_remove``, which reaches the core and clears the device and
    entity registry bindings. What the sweep never reads is the
    ``subentry_type``; the outcome follows from the *resolved* key alone.

    The two removed cases reach the sweep and fail its first barrier: their
    resolved key (the stored e-mail key, or the bare type name when no key is
    stored) is outside ``desired``. The two kept cases never reach the sweep at
    all, and for a reason worth naming, because it is not a barrier: the later
    twin resolves onto a key the canonical subentry already holds and loses the
    collision in ``_refresh_from_entry`` (``preferred is existing`` short-cuts
    the indexing), so it is never managed. The tracker case additionally
    depends on the Class A1 fold putting it on ``core_tracking`` first.

    Scope of this pin, stated rather than implied: the first barrier (a key
    inside ``desired`` is skipped) is pinned below -- without it both core ids
    are in ``desired_ids`` and would be popped from the managed map. The
    *second* barrier (a key whose ``subentry_id`` already belongs to a desired
    key) is **not** pinned here and cannot be from this direction: the only
    writer of the managed map, ``_index_managed_subentry``, pops any earlier
    key holding the same ``subentry_id``, so no fixture built through
    ``async_sync`` can put one id under two keys. Whether that branch is
    reachable at all is carried as a follow-up finding in
    ``PLAN_GFMY_SUBENTRY_DELETION_TYPE_AXIS``.

    No guard is added here; changing this is reserved for that plan. The
    assertion is on the exact removal list, not on membership: a regression
    that additionally sweeps a canonical core group is the irreversible
    outcome this pin exists for, and ``async_remove`` swallows every exception
    into a debug log, so the test is the only visibility.
    """

    tracker = _ap1_subentry(
        "id-tracker", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-t"
    )
    service = _ap1_subentry(
        "id-service", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, "e1-s"
    )
    third = _ap1_subentry("id-third", group_key, subentry_type, "e1-third")
    _entry, recorder, manager = _ap1_setup("e1", [tracker, service, third])

    await manager.async_sync(_ap1_core_definitions("e1"))

    assert recorder.removed == (["id-third"] if expect_removed else [])

    # Both core groups survive the sweep under their canonical key and keep
    # their original subentry. The first half of this pins the first barrier:
    # drop the ``managed_key in desired`` skip and both ids match
    # ``desired_ids``, so both groups are popped from the managed map and
    # ``manager.get`` returns None. The second half pins the collision loss
    # described above: if the later twin took over the slot, the id here would
    # be ``id-third``.
    tracker_slot = manager.get(TRACKER_SUBENTRY_KEY)
    service_slot = manager.get(SERVICE_SUBENTRY_KEY)
    assert tracker_slot is not None
    assert tracker_slot.subentry_id == "id-tracker"
    assert service_slot is not None
    assert service_slot.subentry_id == "id-service"


# --- Deduplication pin: the negative control for AP3 -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["fwd", "rev"])
@pytest.mark.parametrize(
    ("axis", "types", "unique_ids", "expected"),
    [
        (
            "unique_id",
            (SUBENTRY_TYPE_TRACKER, SUBENTRY_TYPE_HUB),
            ("e1-shared-uid", "e1-shared-uid"),
            "load-order-loser",
        ),
        (
            "group",
            (SUBENTRY_TYPE_TRACKER, SUBENTRY_TYPE_TRACKER),
            ("e1-uid-a", "e1-uid-b"),
            "id-b",
        ),
        (
            "group-type-separates",
            (SUBENTRY_TYPE_TRACKER, SUBENTRY_TYPE_HUB),
            ("e1-uid-a", "e1-uid-b"),
            "none",
        ),
        (
            "group-no-unique-id",
            (SUBENTRY_TYPE_TRACKER, SUBENTRY_TYPE_TRACKER),
            (None, None),
            "load-order-loser",
        ),
    ],
    ids=["unique_id", "group", "group-type-separates", "group-no-unique-id"],
)
async def test_ap1_deduplicate_reads_type_only_on_the_group_axis(
    axis: str,
    types: tuple[str, str],
    unique_ids: tuple[str | None, str | None],
    expected: str,
    reverse: bool,
) -> None:
    """Class A2 (standing assertion, behaviour we carry).

    ``_deduplicate_subentries`` runs *two* grouping passes that feed one shared
    ``removal_targets`` set, and only one of them is type-blind:

    * the ``unique_id`` axis groups by ``unique_id`` alone. The survivor comes
      from ``_select_canonical``, whose sort key is
      ``(0 if unique_part else 1, unique_part, index, subentry_part)``; with the
      ``unique_id`` equal, the first two fields are constant and ``index`` --
      the load order -- decides who is removed. No type is read here.
    * the ``group`` axis groups by ``(key_value, subentry_type)``, so the type
      *is* part of the key. Two subentries sharing a ``group_key`` collapse only
      when their types match; differing types put them in separate buckets and
      nothing is removed.

    The two axes also differ in *which* field decides the survivor, and this is
    the part that keeps getting stated too broadly. Load order decides exactly
    when field 2 (``unique_part``) ties, and that happens in two situations,
    not one: always on the ``unique_id`` axis, because there the shared
    ``unique_id`` *is* the grouping key; and on the group axis whenever both
    subentries carry ``unique_id=None``, since ``unique_part`` then falls back
    to ``""`` for both. With distinct ``unique_id``s on the group axis the tie
    breaks lexicographically instead and the outcome is load-order
    *independent*: ``id-b`` (``e1-uid-b``) loses in both orders.

    Two drafts of this docstring got that wrong in opposite directions and both
    were corrected by a measurement rather than by reading -- first by claiming
    load order everywhere, then by tying it to the ``unique_id`` axis alone.
    The ``group-no-unique-id`` case exists so the second error cannot recur
    silently: subentries without a ``unique_id`` are a real production shape,
    they never enter the ``unique_id`` grouping at all (it filters on
    ``isinstance(subentry_unique_id, str)``), and they are load-order decided
    on the group axis.

    That second axis is also why the older, narrower claim ("no type is read")
    was wrong, and it is the axis AP3 will pull on: a rank change that also
    moves a subentry's *type* would silently regroup here. Four cases pin the
    distinction, each in both load orders.

    Scope of the claim, stated because it stays narrower than it looks:
    ``_deduplicate_subentries`` never calls ``_candidate_score``, so a pure rank
    change cannot turn these red by construction. They are a *negative control*
    proving AP3 leaves this removal path untouched, and they become a guard in
    the strong sense only once someone touches ``_select_canonical`` or unifies
    the two rankers. Either way AP3 requires them to stay green.
    """

    first = _ap1_subentry("id-a", TRACKER_SUBENTRY_KEY, types[0], unique_ids[0])
    second = _ap1_subentry("id-b", TRACKER_SUBENTRY_KEY, types[1], unique_ids[1])
    order = [second, first] if reverse else [first, second]
    _entry, recorder, manager = _ap1_setup("e1", order)

    await manager._deduplicate_subentries()

    if expected == "load-order-loser":
        assert recorder.removed == [order[1].subentry_id], axis
    elif expected == "none":
        assert recorder.removed == [], axis
    else:
        # Lexicographic on ``unique_id``; identical in both load orders.
        assert recorder.removed == [expected], axis


# ---------------------------------------------------------------------------
# AP4 step 3b -- the reverse index after a displacement
# (``PLAN_GFMY_ALIAS_TYPE_AXIS``, follow-up U-23 from the AP3 re-review).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reverse", [False, True], ids=["fwd", "rev"])
def test_ap4_the_reverse_index_drops_a_displaced_subentry(reverse: bool) -> None:
    """A subentry pushed off a managed key must lose its reverse-index entry.

    Class A. ``_managed`` is keyed by managed key and is therefore overwritten
    cleanly when a better candidate arrives; ``_managed_by_subentry_id`` is
    keyed by *identifier* and survives that overwrite, so the loser kept
    claiming a slot it no longer held.

    The harm is not bookkeeping hygiene, and it was measured rather than
    derived: ``_async_adopt_existing_unique_id`` asks
    ``_managed_key_for_subentry_id(owner_subentry_id)`` which key the owner
    previously held and pops it. Given the stale answer it pops the slot of the
    *rightful* holder, so ``core_tracking`` drops out of ``_managed`` entirely
    until the next refresh.

    Only one load order produces the displacement (the loser has to be indexed
    first), which is exactly why both are asserted: the defect was invisible in
    the other one.
    """

    low = _ap1_subentry("id-a", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-a")
    high = _ap1_subentry("id-b", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-b")
    order = [high, low] if reverse else [low, high]
    _entry, _recorder, manager = _ap1_setup("e1", order)

    manager._refresh_from_entry()

    managed = manager._managed.get(TRACKER_SUBENTRY_KEY)
    assert managed is not None and managed.subentry_id == "id-a", (
        "the tie-break picks the lowest identifier in both load orders"
    )
    assert manager._managed_key_for_subentry_id("id-b") is None, (
        "the displaced subentry must not keep claiming the key it lost; the "
        "adoption path reads this mapping to decide which slot to vacate"
    )
    assert manager._managed_key_for_subentry_id("id-a") == TRACKER_SUBENTRY_KEY, (
        "and the winner keeps its own entry"
    )
