# tests/test_subentry_manager_registry_resolution.py

from __future__ import annotations

import logging
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
# Characterising the subentry manager axis (PR #1230, AP1).
#
# Every test below states its class in its own docstring:
#   Class A -- standing assertion. It must stay green: a later rank change
#              turning one red is a regression of PR #1230, not an adjustment.
#              Two sub-cases, both binding:
#                A1 -- behaviour we want (the tracker fold, kept by V-1).
#                A2 -- behaviour we carry. Changing it is reserved for
#                      PLAN_GFMY_SUBENTRY_DELETION_TYPE_AXIS, so it must not
#                      move as a side effect here either.
#   Class B -- characterisation. AP3 (rank) or AP4 (reading side) turns
#              exactly these over.
#
# That A2 reservation was drawn on by PR #1236, which is what it was held for:
# the ``unique_id`` case of ``::test_dedup_type_ranks_on_one_axis_and_groups_on
# _the_other`` carried the load-order survivor as A2 and now carries the type
# owner as A1. Nothing else moved with it; the remaining A2 cases are still
# reserved.
#
# Substitution declared: PR #1230 asked the second Class B case to pin the
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
            "none",
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
async def test_dedup_type_ranks_on_one_axis_and_groups_on_the_other(
    axis: str,
    types: tuple[str, str],
    unique_ids: tuple[str | None, str | None],
    expected: str,
    reverse: bool,
) -> None:
    """Class A1 for the ``unique_id`` case, A2 for the other three.

    Both axes read ``subentry_type`` now, but they read it differently.

    ``_deduplicate_subentries`` runs *two* grouping passes feeding one shared
    ``removal_targets`` set:

    * the ``unique_id`` axis groups by ``unique_id`` alone, so the type cannot
      be part of the key. It enters twice instead: as a *rank field* in
      ``_select_canonical`` (the candidate whose ``subentry_type`` literally
      owns its stored ``group_key`` per ``LITERAL_CORE_KEY_OWNER`` sorts first),
      and as a *removal guard* -- only a candidate whose type matches the
      canonical one is removed at all.
    * the ``group`` axis groups by ``(key_value, subentry_type)``, so the type
      *is* part of the key. Two subentries sharing a ``group_key`` collapse only
      when their types match; differing types put them in separate buckets and
      nothing is removed.

    Both axes therefore end at the same rule from two directions: a subentry of
    a different type is a group of its own and is never removed as a duplicate
    of another. The ``unique_id`` case is the interesting one precisely because
    the axis cannot express that rule in its grouping key.

    The name of this test and its first case were inverted twice, and the second
    correction reversed the first. Before AP3 the rank field did not exist, the
    first two sort fields were constant across a ``unique_id`` bucket, and the
    survivor was whichever subentry the entry happened to yield first; the case
    pinned that as a standing assertion, which it was not. AP3 made the loser
    deterministic, and the case then pinned ``id-b`` going. Codex flagged that
    outcome on PR #1236: a deterministic removal of a foreign-typed sibling is
    worse than a random one, not better, and the repo contract
    (``agents/config_flow/AGENTS.md``) already says such a sibling must be left
    alone. The guard closes it, and the case is ``none`` from either direction.

    Load order still decides wherever the rank ties, and that is worth stating
    exactly, because two earlier drafts got it wrong in opposite directions and
    both were corrected by measurement rather than by reading. It ties when
    neither candidate owns its key, and it ties on the group axis whenever both
    carry ``unique_id=None`` (``unique_part`` falls back to ``""`` for both).
    The ``group-no-unique-id`` case exists so that second error cannot recur
    silently: subentries without a ``unique_id`` are a real production shape and
    never enter the ``unique_id`` grouping at all, since it filters on
    ``isinstance(subentry_unique_id, str)``.

    Scope of the claim: ``_deduplicate_subentries`` never calls
    ``_candidate_score``, so a change to *that* ranker still cannot turn these
    red. Against ``_select_canonical`` they are a guard in the strong sense, and
    the killing mutation is named in the case table below.
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
        # Only the ``group`` axis reaches here now, and it is identical in both
        # load orders because the tie breaks lexicographically on ``unique_id``.
        # The type rank is not what this branch pins any more: with the removal
        # guard in place the ``unique_id`` case removes nothing whichever way it
        # ranks, so its rank sensitivity moved to
        # ``::test_dedup_the_guard_still_needs_the_rank``, which is the only
        # place a rank mutation is still observable through this function.
        assert recorder.removed == [expected], axis


# --- The production collision shape, characterised before it is changed ----


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True], ids=["fwd", "rev"])
async def test_dedup_keeps_both_a_hub_and_a_service_sharing_one_identifier(
    reverse: bool,
) -> None:
    """Class A1. The production collision shape, and the one this PR exists for.

    The parametrised case above uses ``core_tracking`` for both candidates,
    which is a *constructed* collision. This is the shape production actually
    builds: ``HubSubentryFlowHandler`` and ``ServiceSubentryFlowHandler`` both
    derive ``unique_id`` as ``f"{entry_id}-{self._group_key}"`` from the same
    ``_group_key = SERVICE_SUBENTRY_KEY``, so two subentries differing *only* in
    ``subentry_type`` land in one ``grouped_by_unique`` bucket.

    Measured at ``c835d6b6``, with no type field in the sort key at all:

    * ``fwd`` (service stored first) removed ``id-hub``;
    * ``rev`` (hub stored first) removed ``id-service`` -- **the canonical
      service group**, registry bindings included.

    That second outcome is the defect ``agents/config_flow/AGENTS.md`` records
    for the flow side, where "taking whichever came first" turned an
    ``AbortFlow`` into a silent removal of exactly this group. It was fixed
    there; here it survived because nothing asserted on it.

    The first answer was the rank alone, and it was **not enough**. It made the
    hub the loser in both orders, and this test pinned ``["id-hub"]`` -- trading
    a random silent removal for a reliable one. Codex flagged that on PR #1236
    against the same contract file, which states the rule the rank alone
    violates: a ``hub`` that loses the service slot "is then not a cheaper
    outcome but a different one, a group of its own that keeps its title, its
    key and its stored ids, and the cleanup must leave it alone". The flow-side
    sweep ``_async_cleanup_stale_subentries`` had carried that guard since
    PR #1230; the manager-side deduplicator had not.

    So the removal is now guarded by type as well: within a ``unique_id``
    bucket only a candidate whose ``subentry_type`` matches the canonical one
    is removed. Both subentries survive, in both orders.

    Killing mutation: drop the ``canonical_type`` comparison in
    ``_deduplicate_subentries`` and both cases go back to removing ``id-hub``.
    Pinning the rank needs a different shape, because with two types present
    the guard suppresses the removal whichever way the rank falls; that is
    ``::test_dedup_the_guard_still_needs_the_rank``.

    Two subentries can only reach this state from storage, never through a
    write: the core refuses a colliding ``unique_id`` in both
    ``async_add_subentry`` and ``async_update_subentry``. Restoration from
    ``.storage`` runs neither check.

    Scope of the claim, restated because the earlier version of this paragraph
    declared the question out of scope and it was not: leaving the pair intact
    does not resolve the shared identifier. Nothing in the runtime manager
    renames a colliding holder -- ``_claim_unique_id`` lives in
    ``ConfigFlow._async_sync_feature_subentries`` and runs only from a flow --
    so the collision persists until the next flow pass. That is the deliberate
    direction: ``async_remove_subentry`` clears device and entity registry
    bindings and cannot be undone, a deferred rename can.
    """

    service = _ap1_subentry(
        "id-service", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, "e1-service"
    )
    hub = _ap1_subentry("id-hub", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, "e1-service")
    order = [hub, service] if reverse else [service, hub]
    _entry, recorder, manager = _ap1_setup("e1", order)

    await manager._deduplicate_subentries()

    # Load-order independent, which is the whole point; asserted as an empty
    # literal rather than via ``order`` so a future reshuffle of the fixture
    # cannot make the expectation follow either defect back -- neither the
    # random removal before the rank nor the reliable one before the guard.
    assert recorder.removed == []


@pytest.mark.asyncio
async def test_dedup_a_moved_survivor_can_change_the_removal_count() -> None:
    """Class A1. The type rank is not count-neutral across the two axes.

    Within one bucket the rank only decides *who* survives. Across buckets it
    can also change *how many* subentries go, because both grouping passes feed
    the same ``removal_targets`` set and moving a survivor moves the overlap.
    An earlier draft of the comment on ``_select_canonical`` asserted the
    opposite -- "it never changes how many subentries are removed" -- under the
    heading "fail-safe on a removal path", which is the worst place to be wrong.
    A reviewer's exhaustive sweep of the three-subentry shapes found 84 of
    19 683 where the count differs, evenly split between more and fewer.

    This fixture is one of them, measured at three stages:

    * at ``c835d6b6``, with no type field at all, the unique bucket
      ``{hub, service}`` keeps the load-order winner ``id-hub`` and only
      ``id-service`` is removed -- once, by both axes at the same time;
    * with the rank alone, the unique bucket keeps the owner ``id-service``, so
      ``id-hub`` is removed *and* ``id-service`` still loses the group bucket to
      ``id-decoy``. Two removals;
    * with the rank *and* the type guard, ``id-hub`` is spared as a foreign type
      and ``id-service`` still loses the group bucket. One removal again, but a
      different one from the first stage.

    ``id-decoy`` is what makes the group bucket bite: it stores the same
    ``(service, service)`` pair but a *different* ``unique_id``, so it never
    enters the unique grouping and competes only on the group axis, where the
    lexicographically smaller identifier wins.

    **The loss of ``id-service`` is pre-existing and belongs to neither the rank
    nor the guard**, and that attribution is measured rather than argued: it is
    already the outcome at ``c835d6b6``, in the first stage above. Both axes
    wanted it gone then; only the group axis does now. What the group axis is
    doing there is its own defect -- two real ``service`` copies collide, and the
    survivor is picked by lexicographic ``unique_id``, so the copy carrying the
    *canonical* ``e1-service`` loses to one carrying the tracker key's
    identifier. That is carried as ``B20`` in
    ``PLAN_GFMY_SUBENTRY_DELETION_TYPE_AXIS`` rather than fixed here: it is a
    same-typed collision, which is exactly what a deduplicator is supposed to
    collapse, and changing *which* copy wins is a separate decision from
    stopping it from crossing a type boundary.

    Reachability, stated rather than assumed: no writer in ``custom_components``
    produces ``id-decoy``'s shape, because both flow handlers derive
    ``unique_id`` from the very key they store. It is a legacy or hand-edited
    shape, which is also the only way two subentries share a ``unique_id`` at
    all. Killing mutation: drop the ``canonical_type`` comparison in
    ``_deduplicate_subentries`` and this test goes back to two removals.
    """

    decoy = _ap1_subentry(
        "id-decoy", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, "e1-core_tracking"
    )
    hub = _ap1_subentry("id-hub", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, "e1-service")
    service = _ap1_subentry(
        "id-service", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, "e1-service"
    )
    _entry, recorder, manager = _ap1_setup("e1", [decoy, hub, service])

    await manager._deduplicate_subentries()

    assert sorted(recorder.removed) == ["id-service"]


@pytest.mark.asyncio
async def test_dedup_the_guard_still_needs_the_rank() -> None:
    """Class A1. The type guard suppresses removals; the rank aims them.

    With the guard in place, the type rank stopped being observable through the
    two-candidate fixtures: whichever of a ``hub`` and a ``service`` is ranked
    first, the other is a foreign type and is spared either way. That made it
    look as though the rank had become decorative once the guard existed, and
    the first draft of the comment in ``_select_canonical`` said so using two
    ``service`` twins on the same stored key -- which was wrong, and measured to
    be wrong: that pair collides on the *group* axis and is collapsed there
    regardless of how the unique axis ranks.

    The shape that separates the two therefore needs all three candidates in
    *distinct* ``grouped_by_group`` buckets, so that the group axis cannot fire
    at all and every removal has to come from the unique one:

    * ``id-hub``   -- ``hub``     storing ``service``       (own bucket);
    * ``id-svc-a`` -- ``service`` storing ``service``       (own bucket);
    * ``id-svc-b`` -- ``service`` storing ``core_tracking`` (own bucket).

    All three share ``e1-service``, so all three meet on the unique axis.
    ``id-svc-a`` is the only candidate whose type literally owns the key it
    stores, so the rank makes it canonical, the guard then removes the
    same-typed ``id-svc-b``, and the hub is spared.

    Killing mutation, measured: pin ``type_part`` to a constant and the hub wins
    the bucket on load order instead; nothing then matches its type, and the
    genuine duplicate survives too -- the run removes nothing at all.
    """

    hub = _ap1_subentry("id-hub", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, "e1-service")
    svc_a = _ap1_subentry(
        "id-svc-a", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, "e1-service"
    )
    svc_b = _ap1_subentry(
        "id-svc-b", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, "e1-service"
    )
    # Hub first, so that a rank that fails to fire leaves it as the load-order
    # winner and the assertion below can tell the two apart.
    _entry, recorder, manager = _ap1_setup("e1", [hub, svc_a, svc_b])

    await manager._deduplicate_subentries()

    assert recorder.removed == ["id-svc-b"]


@pytest.mark.asyncio
async def test_dedup_a_spared_hub_survives_a_whole_sync_not_just_the_helper() -> None:
    """Class A1. "Left standing" measured through ``async_sync``, not the helper.

    Every other test here calls ``_deduplicate_subentries`` directly, which
    answers "does the helper remove it?" and not "is it still there afterwards?".
    Those are different questions: ``async_sync`` runs the helper first and then
    a *type-blind* stale sweep of its own, so a subentry the helper spares can
    still be removed further down the same call. A reviewer flagged the gap on
    PR #1236 while checking the contract wording, and it is a real one -- see
    ``::test_ap1_stale_sweep_is_decided_by_the_resolved_key``, where a ``hub``
    storing a legacy key *is* swept.

    What this pins is therefore narrow on purpose: for the **production**
    collision shape -- a ``hub`` storing ``SERVICE_SUBENTRY_KEY`` beside a real
    ``service`` storing the same key, both on ``e1-service`` -- the hub survives
    the whole sync. It never enters ``_managed`` at all, because
    ``_candidate_score`` gives the slot to the literal owner, and the stale
    sweep only looks at managed keys.

    Killing mutation: drop the ``canonical_type`` comparison in
    ``_deduplicate_subentries`` and ``id-hub`` is gone before the sweep is ever
    reached.
    """

    tracker = _ap1_subentry(
        "id-tracker", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-core_tracking"
    )
    hub = _ap1_subentry("id-hub", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, "e1-service")
    service = _ap1_subentry(
        "id-service", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, "e1-service"
    )
    _entry, recorder, manager = _ap1_setup("e1", [tracker, hub, service])

    await manager.async_sync(_ap1_core_definitions("e1"))

    assert recorder.removed == []
    slot = manager.get(SERVICE_SUBENTRY_KEY)
    assert slot is not None
    # The literal owner holds the slot; the hub is beside it, not instead of it.
    assert slot.subentry_id == "id-service"


@pytest.mark.asyncio
async def test_dedup_sparing_a_twin_can_route_a_sync_through_the_adoption_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Class A1. The guard's cost, pinned rather than claimed away.

    An earlier draft of the root ``AGENTS.md`` paragraph asserted, as *measured*,
    that sparing a foreign-typed twin leaves the retry loop untouched, on the
    reasoning that the manager only ever writes back an identifier the slot
    holder already has. A reviewer constructed the counter-shape and it
    reproduces: the spared twin can be the very subentry that *takes* the slot,
    and then the write-back does change an identifier.

    * ``id-h`` -- ``hub``     storing ``service``,       on ``e1-core_tracking``
    * ``id-t`` -- ``tracker`` storing ``core_tracking``, on ``e1-core_tracking``
    * ``id-x`` -- ``service`` storing ``core_tracking``, on ``e1-service``

    ``id-h`` and ``id-t`` share a ``unique_id``; the rank makes the literal
    tracker owner canonical and the guard spares the hub. ``_refresh_from_entry``
    then indexes the hub under its *stored* key, so the hub holds the service
    slot, and ``async_sync`` tries to write ``e1-service`` onto it -- which
    ``id-x`` holds. The core raises, the retry deduplicates to no effect, and
    the second abort lands in ``_async_adopt_existing_unique_id``.

    Measured in both directions: with the guard neutralised, ``id-h`` is removed
    and the slot is ``id-x`` immediately, with a different log line and no
    repeated collision.

    This is the deliberate trade rather than a regression -- an irreversible
    ``async_remove_subentry`` traded for a documented, logged fallback that ends
    at the same holder -- but it is a cost, and the contract now says so instead
    of claiming the loop is unaffected. Killing mutation: neutralise the guard
    and the repeated-collision record disappears.
    """

    caplog.set_level(logging.DEBUG)
    hub = _ap1_subentry(
        "id-h", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, "e1-core_tracking"
    )
    tracker = _ap1_subentry(
        "id-t", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, "e1-core_tracking"
    )
    service = _ap1_subentry(
        "id-x", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, "e1-service"
    )
    _entry, recorder, manager = _ap1_setup("e1", [hub, tracker, service])

    await manager.async_sync(_ap1_core_definitions("e1"))

    # Nothing removed: that is the point of the guard.
    assert recorder.removed == []
    # But the sync paid for it with a second collision round.
    assert any(
        "repeated unique_id collision" in record.message for record in caplog.records
    )
    # And it still ends at the right holder, which is why this is a cost and
    # not a defect.
    slot = manager.get(SERVICE_SUBENTRY_KEY)
    assert slot is not None
    assert slot.subentry_id == "id-x"


# ---------------------------------------------------------------------------
# The reverse index after a displacement (PR #1230, AP4 step 3b, a follow-up
# from its AP3 re-review).
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


# --- AP5: the setup sync must not delete what no definition governs ---------


def _ap5_subentry(
    subentry_id: str,
    group_key: str,
    subentry_type: str,
    unique_id: str,
    *,
    stored: Mapping[str, Any],
) -> Any:
    """A subentry carrying fields beyond the ones a definition ever names.

    Built on the same real ``ConfigSubentry`` as :func:`_ap1_subentry` and for
    the same reason: the core replaces ``data`` wholesale, and only the frozen
    dataclass with its ``MappingProxyType`` makes that replacement observable
    the way production performs it.
    """

    assert _RealConfigSubentry is not None, "core config_entries not importable"
    data: dict[str, Any] = {"group_key": group_key}
    data.update(stored)
    return _RealConfigSubentry(
        data=MappingProxyType(data),
        subentry_type=subentry_type,
        title=f"{subentry_type}:{group_key}",
        unique_id=unique_id,
        subentry_id=subentry_id,
    )


def _ap5_production_definitions(entry_id: str) -> list[ConfigEntrySubentryDefinition]:
    """The definitions ``async_setup_entry`` really passes.

    Not :func:`_ap1_core_definitions`, whose ``data`` is empty: an empty
    definition cannot show that the *governed* fields still win over the
    preserved ones, and that half of the behaviour is what keeps the
    preservation from freezing the subentry. The four keys mirror the two
    production builders (``__init__.py`` setup step 2 and
    ``coordinator/subentry.py`` core repair), which carry the same set.
    """

    return [
        ConfigEntrySubentryDefinition(
            key=TRACKER_SUBENTRY_KEY,
            title="Google Find My devices",
            data={
                "features": ["device_tracker"],
                "fcm_push_enabled": False,
                "has_google_home_filter": True,
                "entry_title": "Find My",
            },
            subentry_type=SUBENTRY_TYPE_TRACKER,
            unique_id=f"{entry_id}-{TRACKER_SUBENTRY_KEY}",
        ),
        ConfigEntrySubentryDefinition(
            key=SERVICE_SUBENTRY_KEY,
            title="Google Find Hub Service",
            data={
                "features": ["sensor"],
                "fcm_push_enabled": False,
                "has_google_home_filter": True,
                "entry_title": "Find My",
            },
            subentry_type=SUBENTRY_TYPE_SERVICE,
            unique_id=f"{entry_id}-{SERVICE_SUBENTRY_KEY}",
        ),
    ]


@pytest.mark.parametrize(
    ("stored_visible", "label"),
    [
        (("dev-1", "dev-2"), "E3"),
        ((), "E1"),
        (("dev-bad:",), "E4"),
    ],
    ids=["E3-listed", "E1-empty", "E4-unusable"],
)
@pytest.mark.asyncio
async def test_ap5_a_stored_allow_list_survives_the_setup_sync(
    stored_visible: tuple[str, ...], label: str
) -> None:
    """F-7: ``async_sync`` must not delete the group's device assignment.

    The setup sync builds its payload from the definition alone and the core
    replaces ``ConfigSubentry.data`` wholesale, so every stored field the
    definition does not name used to be deleted -- at *every* start, before the
    first refresh.

    Carried as the setup-path half of the visibility-assignment bookkeeping
    remainders; the identifier lives in the register of
    ``agents/config_flow/AGENTS.md`` rather than here, so this docstring names
    the behaviour instead of a code the reader would have to look up.

    All three shapes that can be stored are asserted, not just the listed one,
    because they mean different things under the target semantics and the
    failure collapses them: a deleted key reads as "never assigned" and
    therefore as unrestricted, so an empty list ("this group shows nothing")
    would come back as "this group shows everything". Asserting only the
    listed shape would leave exactly the fail-open direction untested.

    Verbatim rather than normalised is asserted on purpose for ``E4``: the
    reinterpretation report reads the raw stored value, and a preservation
    that quietly normalised it would erase its own evidence.

    Killing mutation: drop ``_payload_preserving_stored_fields`` from the
    update branch of ``async_sync`` and pass ``payload`` straight through.
    """

    tracker = _ap5_subentry(
        "id-t",
        TRACKER_SUBENTRY_KEY,
        SUBENTRY_TYPE_TRACKER,
        "e1-core_tracking",
        stored={
            "visible_device_ids": stored_visible,
            "feature_flags": {"probe": True},
            # A *governed* field, seeded with a stale value on purpose. Without
            # it the "the definition still wins" assertion below is vacuously
            # true: reversing the merge precedence leaves it green when the
            # stored mapping simply has no ``features`` key to win with.
            "features": ["stale_platform"],
        },
    )
    entry, _recorder, manager = _ap5_setup_with(tracker)

    await manager.async_sync(_ap5_production_definitions("e1"))

    stored_after = dict(entry.subentries["id-t"].data)
    assert "visible_device_ids" in stored_after, (
        f"{label}: the assignment key must survive the sync; losing it reads as "
        "'never assigned' and therefore as unrestricted"
    )
    assert tuple(stored_after["visible_device_ids"]) == stored_visible, (
        f"{label}: the stored value is preserved verbatim, not normalised"
    )
    assert stored_after.get("feature_flags") == {"probe": True}, (
        "the same deletion took the feature flags with it; the fix is the "
        "field-agnostic one, so this is asserted rather than assumed"
    )
    assert stored_after["features"] == ["device_tracker"], (
        "a field the definition *does* govern still wins -- preservation must "
        "not freeze the subentry against its own definition"
    )

    service_after = dict(entry.subentries["id-s"].data)
    assert "visible_device_ids" not in service_after, (
        "the exemption: a non-device group must not have its legacy device "
        "assignment preserved, because clearing it is the repair that keeps a "
        "wrong assignment from becoming permanent"
    )
    assert service_after.get("feature_flags") == {"svc": 1}, (
        "the exemption is one field wide, not a blanket opt-out for the "
        "service group -- its other stored fields are preserved like anyone's"
    )


def _ap5_setup_with(*subentries: Any) -> tuple[Any, Any, Any]:
    """``_ap1_setup`` with both core groups present.

    ``async_sync`` sweeps managed subentries that fall out of ``desired``, so a
    fixture carrying only one core group would exercise the create branch for
    the other and cloud what the update branch does.
    """

    service = _ap5_subentry(
        "id-s",
        SERVICE_SUBENTRY_KEY,
        SUBENTRY_TYPE_SERVICE,
        "e1-service",
        # Seeded with a legacy assignment on purpose: the preservation must
        # *not* reach it, and an empty service fixture could not tell a
        # working exemption from a missing one.
        stored={"visible_device_ids": ("dev-legacy",), "feature_flags": {"svc": 1}},
    )
    return _ap1_setup("e1", [*subentries, service])


@pytest.mark.parametrize(
    ("group_key", "subentry_type", "axis"),
    [
        (SERVICE_SUBENTRY_KEY, "a_type_this_build_does_not_know", "key"),
        (TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_HUB, "type"),
    ],
    ids=["key-axis", "type-axis"],
)
@pytest.mark.asyncio
async def test_ap5_the_exemption_reads_both_non_device_axes(
    group_key: str, subentry_type: str, axis: str
) -> None:
    """The non-device judgement has two axes, and each catches its own shape.

    ``config_flow._accepts_device_assignment`` reads the group key first
    (``NON_DEVICE_SUBENTRY_KEYS``) and the subentry type second
    (``NON_DEVICE_SUBENTRY_TYPES``). The preservation mirrors both, and both
    parameters here are reachable shapes that the *other* axis alone would miss
    -- measured on 2026-08-06 rather than argued:

    * key axis: a subentry carrying a type this build does not know (an older
      or newer build, a hand-edited store) storing the service key **is**
      managed under ``service``, and its type classifies nothing. A ``tracker``
      storing that key is *not* a counter-example: the fold moves it to the
      tracker key before it gets here.
    * type axis: a ``hub`` storing the tracker key **is** managed under
      ``core_tracking`` -- ``_refresh_from_entry`` canonicalises ``service``
      and ``tracker`` by type but leaves ``hub`` on its stored key -- so the
      key says "device group" and only the type says otherwise. That shape is
      the carried remainder ``B10``, not a hypothetical.

    Killing mutations: drop either disjunct; each kills exactly its own
    parameter and leaves the other green, which is what makes the two axes
    non-redundant rather than belt-and-braces.
    """

    odd = _ap5_subentry(
        "id-odd",
        group_key,
        subentry_type,
        f"e1-{group_key}",
        stored={"visible_device_ids": ("legacy-dev",), "feature_flags": {"odd": 1}},
    )
    companion_key = (
        TRACKER_SUBENTRY_KEY
        if group_key == SERVICE_SUBENTRY_KEY
        else SERVICE_SUBENTRY_KEY
    )
    companion_type = (
        SUBENTRY_TYPE_TRACKER
        if companion_key == TRACKER_SUBENTRY_KEY
        else SUBENTRY_TYPE_SERVICE
    )
    companion = _ap5_subentry(
        "id-companion",
        companion_key,
        companion_type,
        f"e1-{companion_key}",
        stored={},
    )
    entry, _recorder, manager = _ap1_setup("e1", [odd, companion])
    assert manager.get(group_key) is odd, (
        f"precondition ({axis} axis): the subentry must really be managed under "
        f"'{group_key}', or this parameter asserts nothing about that axis"
    )

    await manager.async_sync(_ap5_production_definitions("e1"))

    stored_after = dict(entry.subentries["id-odd"].data)
    assert "visible_device_ids" not in stored_after, (
        f"the {axis} axis classifies this as a non-device group, so its legacy "
        "assignment is cleared"
    )
    assert stored_after.get("feature_flags") == {"odd": 1}, (
        "and the exemption stays one field wide on either axis"
    )
