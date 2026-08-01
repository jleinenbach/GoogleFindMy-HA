# tests/test_subentry_manager_registry_resolution.py

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, is_dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import (
    ConfigEntrySubentryDefinition,
    ConfigEntrySubEntryManager,
)
from custom_components.googlefindmy.const import DOMAIN, TRACKER_SUBENTRY_KEY
from tests.helpers.homeassistant import (
    DeferredRegistryConfigEntriesManager,
    FakeConfigEntriesManager,
    FakeConfigEntry,
    FakeHass,
)

try:
    from homeassistant.config_entries import ConfigSubentry as _RealConfigSubentry
except ModuleNotFoundError:  # pragma: no cover - optional core stubs
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
