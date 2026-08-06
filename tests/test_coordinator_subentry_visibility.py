# tests/test_coordinator_subentry_visibility.py
"""Coordinator subentry visibility regression tests."""

from __future__ import annotations

import logging
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import device_registry as dr

from custom_components.googlefindmy.const import (
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_SUBENTRY_KEY,
)
from custom_components.googlefindmy.coordinator import (
    GoogleFindMyCoordinator,
    SubentryMetadata,
)
from tests.helpers.config_entries_stub import make_config_entry


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return deterministic config_subentry identifiers for fixtures."""

    return f"{entry_id}-{key}-subentry"


class _StubDeviceEntry:
    """Minimal device entry stub exposing registry metadata."""

    def __init__(
        self,
        *,
        device_id: str,
        identifiers: set[tuple[str, str]],
        name: str | None = None,
    ) -> None:
        self.id = device_id
        self.identifiers = identifiers
        self.name = name
        self.name_by_user = None
        self.disabled_by = None


class _StubDeviceRegistry:
    """Stub registry returning known device entries by ID."""

    def __init__(self, entries: dict[str, _StubDeviceEntry]) -> None:
        self._entries = entries

    def async_get(self, device_id: str) -> _StubDeviceEntry | None:
        return self._entries.get(device_id)


class _ManagerStub:
    """Capture subentry manager updates for verification."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def update_visible_device_ids(
        self, subentry_key: str, device_ids: tuple[str, ...]
    ) -> None:
        self.calls.append((subentry_key, device_ids))


def test_visibility_accepts_namespaced_device_id() -> None:
    """Visibility checks must accept namespaced registry identifiers."""

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator._subentry_metadata = {
        TRACKER_SUBENTRY_KEY: SubentryMetadata(
            key=TRACKER_SUBENTRY_KEY,
            config_subentry_id="subentry-1",
            features=(),
            title=None,
            poll_intervals=MappingProxyType({}),
            filters=MappingProxyType({}),
            feature_flags=MappingProxyType({}),
            visible_device_ids=("parent123:device-abc",),
            enabled_device_ids=(),
        )
    }

    assert coordinator.is_device_visible_in_subentry(TRACKER_SUBENTRY_KEY, "device-abc")
    assert coordinator.is_device_visible_in_subentry(
        TRACKER_SUBENTRY_KEY, "parent123:device-abc"
    )
    assert not coordinator.is_device_visible_in_subentry(
        TRACKER_SUBENTRY_KEY, "other-device"
    )


def test_refresh_normalizes_registry_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registry identifiers in subentry settings must resolve to canonical IDs."""

    entry_id = "entry-test"
    canonical_id = "tracker-1"
    registry_id = "device-entry-test-1"

    registry = _StubDeviceRegistry(
        {
            registry_id: _StubDeviceEntry(
                device_id=registry_id,
                identifiers={(DOMAIN, f"{entry_id}:{canonical_id}")},
                name="Tracker One",
            )
        }
    )
    monkeypatch.setattr(dr, "async_get", lambda hass: registry)

    subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "visible_device_ids": [registry_id],
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Core",
        unique_id=f"{entry_id}-core",
        subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
    )
    entry = SimpleNamespace(
        entry_id=entry_id,
        title="Google Find My",
        data={},
        options={},
        subentries={subentry.subentry_id: subentry},
        runtime_data=None,
    )

    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = [{"id": canonical_id, "name": "Tracker One"}]
    coordinator._enabled_poll_device_ids = {canonical_id}
    coordinator.allow_history_fallback = False
    coordinator.device_poll_delay = 30
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = TRACKER_SUBENTRY_KEY
    coordinator._subentry_manager = None
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )

    coordinator._refresh_subentry_index()

    service_metadata = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_metadata is not None
    assert service_metadata.visible_device_ids == ()
    assert service_metadata.config_subentry_id == _stable_subentry_id(
        entry_id, SERVICE_SUBENTRY_KEY
    )

    metadata = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert metadata is not None
    assert metadata.visible_device_ids == (registry_id, canonical_id)
    assert metadata.enabled_device_ids == (canonical_id,)

    assert coordinator.is_device_visible_in_subentry(TRACKER_SUBENTRY_KEY, canonical_id)
    assert coordinator.is_device_visible_in_subentry(TRACKER_SUBENTRY_KEY, registry_id)


def test_default_subentry_prefers_tracker_and_skips_service_manager_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tracker subentry should be default and service updates must not emit manager calls."""

    entry_id = "entry-default"
    registry = _StubDeviceRegistry({})
    monkeypatch.setattr(dr, "async_get", lambda hass: registry)

    service_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id=f"{entry_id}-service",
        subentry_id=_stable_subentry_id(entry_id, SERVICE_SUBENTRY_KEY),
    )
    tracker_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "visible_device_ids": ["device-1"],
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Trackers",
        unique_id=f"{entry_id}-trackers",
        subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
    )

    entry = SimpleNamespace(
        entry_id=entry_id,
        title="Google Find My",
        data={},
        options={},
        subentries={
            service_subentry.subentry_id: service_subentry,
            tracker_subentry.subentry_id: tracker_subentry,
        },
        runtime_data=None,
    )

    loop_stub = SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None)
    hass_stub = SimpleNamespace(loop=loop_stub, data={DOMAIN: {}})
    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = [{"id": "device-1", "name": "Tracker One"}]
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator.allow_history_fallback = False
    coordinator.device_poll_delay = 30
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = SERVICE_SUBENTRY_KEY
    coordinator._subentry_manager = _ManagerStub()
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )

    coordinator._refresh_subentry_index()

    assert coordinator._default_subentry_key() == TRACKER_SUBENTRY_KEY
    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.visible_device_ids == ()
    assert service_meta.config_subentry_id == _stable_subentry_id(
        entry_id, SERVICE_SUBENTRY_KEY
    )
    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.visible_device_ids == ("device-1",)

    manager_stub = coordinator._subentry_manager
    assert isinstance(manager_stub, _ManagerStub)
    assert manager_stub.calls
    assert all(key != SERVICE_SUBENTRY_KEY for key, _ in manager_stub.calls)


def _coordinator_with_tracker_allowlist(
    entry_id: str, stored_visible: list[str]
) -> tuple[GoogleFindMyCoordinator, object]:
    """Return a coordinator whose tracker subentry carries a stored allow-list."""

    subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "visible_device_ids": list(stored_visible),
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Core",
        unique_id=f"{entry_id}-core",
        subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
    )
    entry = make_config_entry(
        entry_id=entry_id,
        title="Google Find My",
        subentries={subentry.subentry_id: subentry},
    )
    hass_stub = SimpleNamespace(
        loop=SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None),
        data={DOMAIN: {}},
    )

    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.allow_history_fallback = False
    coordinator.device_poll_delay = 30
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = None
    coordinator._subentry_manager = None
    coordinator._pending_subentry_repair = None
    coordinator._skip_repair_during_reload_refresh = False
    coordinator._reload_repair_skip_pending_release = False
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )
    return coordinator, entry


def test_a_device_the_account_gained_joins_the_tracker_allowlist() -> None:
    """A tracker added after the initial sync must not stay invisible.

    The stored ``visible_device_ids`` is a device-to-subentry assignment, and the
    initial config-flow sync is the only writer that ever filled it. A device the
    account gained afterwards is in no list at all, so the grouping helper routes
    it to the default (tracker) key and its entity is built -- but the metadata
    would keep reporting it as neither visible nor enabled, which is the state the
    silent-add path leaves behind.
    """

    old_id, new_id = "tracker-old", "tracker-new"
    coordinator, _entry = _coordinator_with_tracker_allowlist("entry-grew", [old_id])
    coordinator.data = [{"id": old_id, "name": "Old"}, {"id": new_id, "name": "New"}]
    coordinator._enabled_poll_device_ids = {old_id, new_id}

    coordinator._refresh_subentry_index(
        [{"id": old_id, "name": "Old"}, {"id": new_id, "name": "New"}]
    )

    metadata = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert metadata is not None
    assert new_id in metadata.visible_device_ids, (
        "a device assigned to no subentry belongs to the tracker, and the stored "
        "list has to say so"
    )
    assert new_id in metadata.enabled_device_ids


def test_a_device_moved_to_the_service_subentry_is_left_alone() -> None:
    """The merge must not undo a user's move and persist the device elsewhere.

    The service subentry was a selectable target in the repair-move, repair-delete
    and visibility steps until ``e8114585``, and its *metadata* visible ids are
    forced to empty. Going by the metadata would therefore call such a device
    unassigned, pull it into the tracker, and the manager write-back would make
    that permanent. The subentry here stores the **canonical** service key, which
    is what separates this case from
    ``test_devices_parked_on_a_mis_keyed_service_twin_are_reclaimed``: there the
    ids are write-back residue and are deliberately reclaimed. Both cases are
    pinned, so neither fix can quietly take the other's ground.
    """

    tracker_id, moved_id = "tracker-own", "tracker-moved-to-service"
    coordinator, entry = _coordinator_with_tracker_allowlist(
        "entry-service-move", [tracker_id]
    )
    service_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": SERVICE_SUBENTRY_KEY,
                "visible_device_ids": [moved_id],
            }
        ),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id="entry-service-move-service",
        subentry_id=_stable_subentry_id("entry-service-move", SERVICE_SUBENTRY_KEY),
    )
    entry.subentries[service_subentry.subentry_id] = service_subentry
    manager = _ManagerStub()
    coordinator._subentry_manager = manager
    devices = [
        {"id": tracker_id, "name": "Own"},
        {"id": moved_id, "name": "Moved"},
    ]
    coordinator.data = devices
    coordinator._enabled_poll_device_ids = {tracker_id, moved_id}

    coordinator._refresh_subentry_index(devices)

    metadata = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert metadata is not None
    assert moved_id not in metadata.visible_device_ids, (
        "a device the user moved to the service subentry is assigned, so the "
        "tracker merge must not claim it"
    )
    tracker_writes = [ids for key, ids in manager.calls if key == TRACKER_SUBENTRY_KEY]
    assert all(moved_id not in ids for ids in tracker_writes), (
        "and the write-back must not make the undone move permanent"
    )


def test_a_device_assigned_to_another_subentry_is_left_alone() -> None:
    """The merge must not pull a user's subentry assignment back to the tracker."""

    tracker_id, foreign_id = "tracker-own", "tracker-elsewhere"
    coordinator, entry = _coordinator_with_tracker_allowlist(
        "entry-split", [tracker_id]
    )
    foreign = ConfigSubentry(
        data=MappingProxyType(
            {"group_key": "extra_group", "visible_device_ids": [foreign_id]}
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Extra",
        unique_id="entry-split-extra",
        subentry_id="entry-split-extra-subentry",
    )
    entry.subentries[foreign.subentry_id] = foreign
    coordinator.data = [
        {"id": tracker_id, "name": "Own"},
        {"id": foreign_id, "name": "Elsewhere"},
    ]
    coordinator._enabled_poll_device_ids = {tracker_id, foreign_id}

    coordinator._refresh_subentry_index(
        [{"id": tracker_id, "name": "Own"}, {"id": foreign_id, "name": "Elsewhere"}]
    )

    metadata = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert metadata is not None
    assert foreign_id not in metadata.visible_device_ids, (
        "a device that already belongs to another subentry is assigned, so the "
        "tracker merge must not claim it"
    )


def test_the_merge_converges_and_stops_writing() -> None:
    """Once the gained device is stored, a second refresh must write nothing new."""

    old_id, new_id = "tracker-old", "tracker-new"
    coordinator, _entry = _coordinator_with_tracker_allowlist(
        "entry-converge", [old_id, new_id]
    )
    # A complete pair of core subentries: a missing one would send the refresh
    # into the repair path, which is not what this test is about.
    service_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": SERVICE_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id="entry-converge-service",
        subentry_id=_stable_subentry_id("entry-converge", SERVICE_SUBENTRY_KEY),
    )
    _entry.subentries[service_subentry.subentry_id] = service_subentry
    manager = _ManagerStub()
    coordinator._subentry_manager = manager
    devices = [{"id": old_id, "name": "Old"}, {"id": new_id, "name": "New"}]
    coordinator.data = devices
    coordinator._enabled_poll_device_ids = {old_id, new_id}

    coordinator._refresh_subentry_index(devices)

    metadata = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert metadata is not None
    assert metadata.visible_device_ids == tuple(sorted((old_id, new_id)))
    tracker_writes = [ids for key, ids in manager.calls if key == TRACKER_SUBENTRY_KEY]
    assert all(set(ids) == {old_id, new_id} for ids in tracker_writes), (
        "with both ids already stored the merge adds nothing, so the write-back "
        "cannot drift"
    )


def test_a_service_twin_does_not_occupy_the_tracker_key_in_the_index() -> None:
    """A service subentry storing a tracker key must not displace the tracker.

    Early migrations left service subentries carrying ``core_tracking`` in
    their stored ``group_key``. The runtime index used to key purely off that
    stored value, so the service twin took the tracker's slot: the tracker's
    metadata was overwritten, the service was described by a synthesised
    placeholder pointing at a subentry that does not exist, and the visible
    ids were written back through the tracker key onto the service twin.
    """

    entry_id = "entry-service-twin"
    tracker_id = _stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY)
    # Deliberately NOT the id the synthesised service placeholder would carry,
    # so a placeholder cannot masquerade as the real subentry below.
    service_id = f"{entry_id}-legacy-service"

    tracker_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "visible_device_ids": ["device-1"],
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Trackers",
        unique_id=f"{entry_id}-trackers",
        subentry_id=tracker_id,
    )
    service_subentry = ConfigSubentry(
        data=MappingProxyType({"group_key": TRACKER_SUBENTRY_KEY}),
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id=f"{entry_id}-service",
        subentry_id=service_id,
    )

    # The service twin is inserted last on purpose: keyed by the stored value
    # it would be the surviving writer for ``core_tracking``.
    entry = make_config_entry(
        entry_id=entry_id,
        title="Google Find My",
        subentries={
            tracker_subentry.subentry_id: tracker_subentry,
            service_subentry.subentry_id: service_subentry,
        },
    )

    hass_stub = SimpleNamespace(
        loop=SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None),
        data={DOMAIN: {}},
    )
    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    coordinator.data = [{"id": "device-1", "name": "Tracker One"}]
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator.allow_history_fallback = False
    coordinator.device_poll_delay = 30
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = None
    coordinator._subentry_manager = _ManagerStub()
    coordinator._pending_subentry_repair = None
    coordinator._skip_repair_during_reload_refresh = False
    coordinator._reload_repair_skip_pending_release = False
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )

    coordinator._refresh_subentry_index(skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.config_subentry_id == tracker_id, (
        "the tracker key must still describe the tracker subentry, not the "
        "service twin that stored the same key"
    )
    assert tracker_meta.visible_device_ids == ("device-1",)

    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.config_subentry_id == service_id, (
        "the service key must describe the real service subentry rather than a "
        "synthesised placeholder"
    )
    assert service_meta.visible_device_ids == ()

    manager_stub = coordinator._subentry_manager
    assert isinstance(manager_stub, _ManagerStub)
    assert manager_stub.calls == [(TRACKER_SUBENTRY_KEY, ("device-1",))]


def _service_twin_coordinator(
    entry_id: str,
    *,
    service_specs: list[tuple[str, str | None, list[str]]],
    subentry_type: str = SUBENTRY_TYPE_SERVICE,
) -> tuple[GoogleFindMyCoordinator, object, _ManagerStub]:
    """Return a coordinator with one real tracker plus the given twins.

    ``service_specs`` holds ``(subentry_id, stored_group_key, visible ids)``
    for each twin; a ``stored_group_key`` of ``None`` stores no ``group_key``
    at all, which is the shape that falls back to the subentry id.
    ``subentry_type`` selects the twin's type, so the same fixture serves the
    ``service`` and the ``hub`` axis.
    """

    tracker_subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "visible_device_ids": ["device-1"],
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Trackers",
        unique_id=f"{entry_id}-trackers",
        subentry_id=_stable_subentry_id(entry_id, TRACKER_SUBENTRY_KEY),
    )
    subentries = {tracker_subentry.subentry_id: tracker_subentry}
    for subentry_id, stored_key, visible in service_specs:
        payload: dict[str, object] = {}
        if stored_key is not None:
            payload["group_key"] = stored_key
        if visible:
            payload["visible_device_ids"] = list(visible)
        service_subentry = ConfigSubentry(
            data=MappingProxyType(payload),
            subentry_type=subentry_type,
            title="Service",
            unique_id=f"{entry_id}-{subentry_id}",
            subentry_id=subentry_id,
        )
        subentries[service_subentry.subentry_id] = service_subentry

    entry = make_config_entry(
        entry_id=entry_id, title="Google Find My", subentries=subentries
    )
    return _coordinator_over(entry)


def _coordinator_over(
    entry: object,
) -> tuple[GoogleFindMyCoordinator, object, _ManagerStub]:
    """Wire a coordinator around ``entry`` with two known devices.

    Extracted from ``_service_twin_coordinator`` so the AP4 fixtures below can
    place arbitrary subentry shapes without copying the wiring; that fixture
    always builds one canonical tracker plus twins of a single type, which is
    the wrong shape for the mixed-type collisions the tracker key needs.
    """

    hass_stub = SimpleNamespace(
        loop=SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None),
        data={DOMAIN: {}},
    )
    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)  # type: ignore[attr-defined]
    coordinator.data = [
        {"id": "device-1", "name": "Tracker One"},
        {"id": "device-2", "name": "Tracker Two"},
    ]
    coordinator._enabled_poll_device_ids = {"device-1", "device-2"}
    coordinator.allow_history_fallback = False
    coordinator.device_poll_delay = 30
    coordinator.min_poll_interval = 60
    coordinator.location_poll_interval = 120
    coordinator._subentry_metadata = {}
    coordinator._subentry_snapshots = {}
    coordinator._feature_to_subentry = {}
    coordinator._default_subentry_key_value = None
    manager_stub = _ManagerStub()
    coordinator._subentry_manager = manager_stub
    coordinator._pending_subentry_repair = None
    coordinator._skip_repair_during_reload_refresh = False
    coordinator._reload_repair_skip_pending_release = False
    coordinator._warned_bad_identifier_devices = set()
    coordinator._diag = SimpleNamespace(
        add_warning=lambda **kwargs: None,
        remove_warning=lambda *args, **kwargs: None,
    )
    return coordinator, entry, manager_stub


def test_devices_parked_on_a_mis_keyed_service_twin_are_reclaimed() -> None:
    """Folding the twin must not strand the ids it accumulated.

    The twin only holds those ids because it attracted the write-back: both
    resolvers in ``config_flow.py`` used to steer it there on the stored key
    alone, while ``_accepts_device_assignment`` kept it out of every choice
    list a user can pick from. Neither does so any more -- the feature sync
    reads ``_canonical_core_key_of`` and ``_BaseSubentryFlow._resolve_existing``
    reads ``_may_answer_for`` -- and the ids a release before those fixes wrote
    are exactly the residue meant here. That the supply is shut is why this
    stays: the stored ids do not disappear with it.
    Counting them as assigned would keep the unassigned-device merge away from
    them while the service branch forces the visible ids to empty, leaving the
    devices in no group at all.
    """

    entry_id = "entry-reclaim"
    coordinator, _entry, _manager = _service_twin_coordinator(
        entry_id,
        service_specs=[("legacy-service-id", TRACKER_SUBENTRY_KEY, ["device-2"])],
    )

    coordinator._refresh_subentry_index(skip_repair=True)

    visible_anywhere = {
        device_id
        for meta in coordinator._subentry_metadata.values()
        for device_id in meta.visible_device_ids
    }
    assert "device-2" in visible_anywhere, (
        "a device parked on the mis-keyed twin must not disappear from every "
        "group when that twin is folded onto the service key"
    )
    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.visible_device_ids == ("device-1", "device-2")
    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.visible_device_ids == ()


def test_folding_a_mis_keyed_twin_writes_nothing_back_to_it() -> None:
    """The reclaim is a re-homing, not a deletion.

    Reading the re-homing as "the assignment is undone" is the misreading this
    test exists to foreclose. That the device reappears under the tracker is pinned
    by ``test_devices_parked_on_a_mis_keyed_service_twin_are_reclaimed``. What
    that neighbour does not say is the half that makes the re-homing
    reversible: the fold only exempts the ids from the in-memory assignment
    bookkeeping, and the stored subentry keeps them, so a later migration can
    still see what the twin held.

    This is a forward guard rather than a regression test for a fixed bug.
    Its reach was measured, not assumed, and it is narrower than "any write
    added to ``_refresh_subentry_index``": the mutation it kills is one that
    reaches the *stored* mapping, either by mutating the shared list in place
    (``dict(...)`` copies shallowly) or by writing through the subentry
    attribute. It does **not** kill a residue cleanup routed through
    ``manager.update_visible_device_ids``; dropping the service-key ``continue``
    in the manager loop leaves every test here green because the loop never
    receives that key: ``manager_visible`` is only filled under
    ``group_key != SERVICE_SUBENTRY_KEY`` (and under the tracker key), so the
    ``continue`` guards a branch that no current caller reaches. A call that
    *did* fire would additionally be invisible here, since the manager is a
    recorder in this fixture. That channel is guarded on the production side
    instead, by the type check in ``update_visible_device_ids`` itself.

    Only the ``service`` type is exercised. The fold treats both non-device
    types alike, and the ``hub`` axis is covered by
    ``test_a_hub_typed_subentry_never_bears_devices`` (which asserts against
    the manager calls, not against storage).
    """

    entry_id = "entry-nowrite"
    twin_id = "legacy-twin-id"
    coordinator, entry, _manager = _service_twin_coordinator(
        entry_id,
        service_specs=[(twin_id, TRACKER_SUBENTRY_KEY, ["device-2"])],
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )

    coordinator._refresh_subentry_index(skip_repair=True)

    twin = entry.subentries[twin_id]
    assert list(twin.data.get("visible_device_ids") or []) == ["device-2"], (
        "nothing in the fold touches the stored ids, neither in the in-memory copy "
        "nor on disk"
    )


@pytest.mark.parametrize("canonical_first", [True, False])
def test_the_canonically_keyed_service_subentry_wins_regardless_of_order(
    canonical_first: bool,
) -> None:
    """Two service-typed subentries fold onto one key, so the winner is fixed.

    The repair path creates a canonically keyed service subentry while a
    mis-keyed twin is still on disk. Without a tie-break the surviving
    description would depend on the order ``entry.subentries`` yields, and the
    service device could be bound to the leftover.
    """

    entry_id = "entry-two-services"
    canonical_id = f"{entry_id}-real-service"
    legacy_id = f"{entry_id}-legacy-service"
    specs = [
        (canonical_id, SERVICE_SUBENTRY_KEY, []),
        (legacy_id, "owner@example.com", []),
    ]
    if not canonical_first:
        specs.reverse()

    coordinator, _entry, _manager = _service_twin_coordinator(
        entry_id, service_specs=specs
    )

    coordinator._refresh_subentry_index(skip_repair=True)

    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.config_subentry_id == canonical_id, (
        "the subentry that already stored the canonical key must describe the "
        "service group, whichever order the entry yields"
    )


@pytest.mark.parametrize(
    "stored_key", [TRACKER_SUBENTRY_KEY, "owner@example.com", None]
)
@pytest.mark.parametrize("hub_first", [True, False])
def test_a_hub_typed_subentry_never_bears_devices(
    stored_key: str | None, hub_first: bool
) -> None:
    """A ``hub``-typed subentry must not hold or attract device ids.

    ``HubSubentryFlowHandler`` sets ``_group_key = SERVICE_SUBENTRY_KEY`` and
    the service feature platforms, so a hub *is* the service group under a
    second entry point, and the options flow already refuses it as an
    assignment target (``_NON_DEVICE_SUBENTRY_TYPES``). The runtime index used
    to fold only ``service``, so a hub left over from an early migration kept a
    device-bearing slot and had visible ids written back onto it.

    The three key shapes take different routes, and the damage differs with
    them: ``core_tracking`` collides with the real tracker and *additionally*
    overwrote its metadata, while an email-style key and a missing key (which
    falls back to the subentry id) open a group of their own and only attracted
    the write-back. The order axis therefore carries weight for
    ``core_tracking`` alone; for the other two shapes both orders exercise the
    same path, and they are parametrised for uniformity, not as evidence.
    """

    entry_id = f"entry-hub-{stored_key or 'nokey'}-{int(hub_first)}"
    hub_id = f"{entry_id}-legacy-hub"
    coordinator, entry, manager = _service_twin_coordinator(
        entry_id,
        service_specs=[(hub_id, stored_key, ["device-2"])],
        subentry_type=SUBENTRY_TYPE_HUB,
    )
    if hub_first:
        # ``entry.subentries`` yields insertion order, and the collapse used to
        # depend on it: rebuild the mapping with the hub in front.
        subentries = entry.subentries
        reordered = {hub_id: subentries[hub_id]}
        reordered.update(
            {key: value for key, value in subentries.items() if key != hub_id}
        )
        subentries.clear()
        subentries.update(reordered)

    coordinator._refresh_subentry_index(skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.config_subentry_id == _stable_subentry_id(
        entry_id, TRACKER_SUBENTRY_KEY
    ), "the tracker key must keep describing the real tracker, not the hub"

    for key, ids in manager.calls:
        if not ids:
            continue
        owner = coordinator.get_subentry_metadata(key=key)
        assert getattr(owner, "config_subentry_id", None) != hub_id, (
            f"device ids were written back through key {key!r}, which describes "
            "the hub subentry"
        )

    assert "device-2" in tracker_meta.visible_device_ids, (
        "and the ids the hub had accumulated must be reclaimed by the tracker "
        "rather than stranded in no group at all"
    )


@pytest.mark.parametrize("hub_first", [True, False])
def test_a_literal_service_subentry_outranks_a_hub_storing_the_same_key(
    hub_first: bool,
) -> None:
    """A preserved hub must not take the service slot from the real service.

    ``HubSubentryFlowHandler._group_key`` is ``SERVICE_SUBENTRY_KEY``, so a hub
    stores that key *literally*, not by folding onto it. The exact-key tie-break
    below the fold therefore does not separate the two, and whichever the entry
    yielded last used to describe the service group. That is not cosmetic: the
    registry coordinator heals the service device linkage from this metadata,
    while the entity platforms select on ``subentry_type == "service"``
    literally (``known_ids_for_subentry_type``), so a hub in the slot rebinds
    the device away from the subentry the platforms actually use.

    The shape is one the config flow now deliberately preserves rather than
    deletes: ``_async_cleanup_stale_subentries`` skips a hub that lost the
    service slot instead of sweeping it, which is what makes the collision
    reachable at runtime in the first place. Both iteration orders are asserted
    because the defect was order-dependent.
    """

    entry_id = f"entry-service-plus-hub-{int(hub_first)}"
    service_id = f"{entry_id}-real-service"
    hub_id = f"{entry_id}-preserved-hub"
    coordinator, entry, _manager = _service_twin_coordinator(
        entry_id, service_specs=[(service_id, SERVICE_SUBENTRY_KEY, [])]
    )
    hub_subentry = ConfigSubentry(
        # The hub carries ids on purpose: this is the one shape
        # ``test_a_hub_typed_subentry_never_bears_devices`` cannot cover,
        # because all of its ``stored_key`` values *fold* (and folding exempts
        # the ids from the assignment bookkeeping so the merge reclaims them), whereas the canonical key does
        # not fold. What happens to them is asserted below.
        data=MappingProxyType(
            {
                "group_key": SERVICE_SUBENTRY_KEY,
                "visible_device_ids": ["device-2"],
            }
        ),
        subentry_type=SUBENTRY_TYPE_HUB,
        title="Google Find Hub Service",
        unique_id=f"{entry_id}-hub",
        subentry_id=hub_id,
    )

    # ``entry.subentries`` yields insertion order; rebuild it so both orders
    # are exercised, mirroring the neighbouring hub test.
    subentries = entry.subentries
    ordered: dict[str, ConfigSubentry] = {}
    if hub_first:
        ordered[hub_id] = hub_subentry
    ordered.update(subentries)
    if not hub_first:
        ordered[hub_id] = hub_subentry
    subentries.clear()
    subentries.update(ordered)

    coordinator._refresh_subentry_index(skip_repair=True)

    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.config_subentry_id == service_id, (
        "the subentry whose type literally owns the service key must describe "
        "the service group, whichever order the entry yields; a hub merely "
        "shares the key"
    )

    # The loser's ids are a named limit, not an assertion of correctness, and
    # this assertion does *not* discriminate the rank: it holds whichever
    # subentry wins the slot (measured, by neutralising the owner field). It
    # falls only for a fold mutation, which already kills
    # ``test_a_device_moved_to_the_service_subentry_is_left_alone``. It is kept
    # anyway, at the site where the shape is constructed, because that test
    # constructs a different one and nothing else records here that the ids
    # reach neither group: they count into ``stored_assigned_ids``, so the
    # unassigned-device merge treats them as already assigned, while the service
    # branch keeps the metadata empty. Measured identical at ``bf3a36aa``, so
    # the rank neither causes nor fixes it; only *which* subentry holds the slot
    # changed. Carried as ``U-31`` in the remainder register of
    # ``agents/config_flow/AGENTS.md``, owned by
    # ``PLAN_GFMY_VISIBILITY_ASSIGNMENT_BOOKKEEPING``; closing it there has to
    # come past here.
    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert "device-2" not in tracker_meta.visible_device_ids, (
        "ids stored on the *canonical* service key are not reclaimed, unlike "
        "the mis-keyed ones the fold exempts from the assignment bookkeeping; "
        "changing that is a decision of its own, because it cannot be told "
        "apart from a move the user made"
    )


@pytest.mark.parametrize("higher_id_first", [True, False])
def test_two_equally_ranked_service_subentries_resolve_by_identifier(
    higher_id_first: bool,
) -> None:
    """Among equals the identifier decides, so the slot stops depending on order.

    Two ``service``-typed subentries can both store the canonical key -- a
    second repair pass is enough -- and then neither the exact-key nor the
    literal-owner component of the rank separates them. Without the third
    component the surviving description would again be whichever the entry
    happened to yield last, which is the very defect the rank replaces.

    The identifier's *value* is arbitrary; only its stability matters, so this
    pins that both orders agree, not which of the two is preferable. The
    fixture deliberately names the lower identifier ``a-`` and the higher
    ``z-`` so the expectation cannot be read out of the insertion order.

    Only the ``True`` case carries evidence, and saying so keeps the
    parametrisation from overstating itself: with the lower identifier
    inserted first it already holds the slot as the incumbent, so
    neutralising the identifier field leaves that case green. The ``False``
    case is coverage of the symmetric path, not a second measurement.
    """

    entry_id = f"entry-two-equal-services-{int(higher_id_first)}"
    lower_id = f"{entry_id}-a-first-repair"
    higher_id = f"{entry_id}-z-second-repair"
    specs = [
        (lower_id, SERVICE_SUBENTRY_KEY, []),
        (higher_id, SERVICE_SUBENTRY_KEY, []),
    ]
    if higher_id_first:
        specs.reverse()

    coordinator, _entry, _manager = _service_twin_coordinator(
        entry_id, service_specs=specs
    )

    coordinator._refresh_subentry_index(skip_repair=True)

    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.config_subentry_id == lower_id, (
        "two candidates of equal rank must resolve to the same subentry in "
        "either order; the lowest identifier is the arbitrary but stable choice"
    )


@pytest.mark.parametrize("identifier_less_first", [True, False])
def test_a_subentry_without_a_usable_identifier_loses_the_service_slot(
    identifier_less_first: bool,
) -> None:
    """A missing identifier must rank last, not first.

    The identifier the rank sees is the *sanitised and provisional-filtered*
    one, not ``subentry.subentry_id``: an empty or non-string id becomes
    ``None`` (``sanitize_subentry_identifier``), and so does a
    ``-provisional`` id that does not match the entry's stable one
    (``filter_provisional_identifier``). Ordering such a candidate by
    ``subentry_id or ""`` would sort it *below* every real identifier, so it
    would win the slot deterministically. Measured, the resulting metadata does
    not carry ``None``: the stable-id block near the end of
    ``_refresh_subentry_index`` substitutes a synthesised
    ``{entry_id}-service-subentry`` placeholder. That substitution is what makes
    the defect quiet -- the group ends up described by a stand-in while the real
    subentry, the one the registry bindings hang off, is passed over -- and it
    is worse than the order-dependence the rank replaces, because it is
    deterministic rather than occasional.

    Only the missing-identifier half is asserted here. The provisional half
    reaches the same code path through the same variable, and no production
    site in ``custom_components/`` was found that creates a ``-provisional``
    subentry id, so it is deliberately left unpinned rather than pinned
    against a shape that may not exist.
    """

    entry_id = f"entry-idless-service-{int(identifier_less_first)}"
    real_id = f"{entry_id}-real-service"
    specs = [
        (real_id, SERVICE_SUBENTRY_KEY, []),
        ("   ", SERVICE_SUBENTRY_KEY, []),
    ]
    if identifier_less_first:
        specs.reverse()

    coordinator, _entry, _manager = _service_twin_coordinator(
        entry_id, service_specs=specs
    )

    coordinator._refresh_subentry_index(skip_repair=True)

    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert service_meta is not None
    assert service_meta.config_subentry_id == real_id, (
        "a subentry whose identifier sanitises to None must rank last; "
        "otherwise it takes the slot and the group loses its identifier"
    )


def test_the_literal_owner_table_is_one_object_shared_by_all_ranking_sides() -> None:
    """Pin the shared owner table that the three ranking sides depend on.

    The reading side's owner field reads
    ``LITERAL_CORE_KEY_OWNER.get(SERVICE_SUBENTRY_KEY)``, and for the service
    key that lookup is *value-identical* to the literal ``"service"``: the key
    constant and the type constant happen to carry the same string. A behaviour
    test therefore cannot tell the shared table apart from a hard-coded literal,
    which is exactly what makes the table's purpose untestable through the
    index alone. This pins the two properties the move to ``const.py`` was made
    for: both mappings are the *same object*, and each key maps to the type
    that literally owns it. The tracker row is the one that carries real
    information, since ``TRACKER_SUBENTRY_KEY`` and ``SUBENTRY_TYPE_TRACKER``
    differ in value.

    What this does *not* pin, said plainly rather than left to be discovered:
    replacing the lookup at the ranking site with the literal ``"service"``
    still passes, because the two constants are value-equal today. This test
    is the tripwire for the day they stop being, not a guard against a
    hard-coded literal. Killing mutations: making the flow hold a private
    ``dict`` copy, and mapping the tracker key to the wrong owner.

    The runtime manager joined as a third reader with PR #1230; it is asserted
    here for the same reason as the other two, because a private copy in
    ``__init__.py`` would drift exactly as silently.
    """

    from custom_components import googlefindmy as _integration
    from custom_components.googlefindmy import config_flow as _config_flow
    from custom_components.googlefindmy import const as _const
    from custom_components.googlefindmy.coordinator import subentry as _subentry

    assert _config_flow._LITERAL_CORE_KEY_OWNER is _const.LITERAL_CORE_KEY_OWNER, (
        "the config flow must rank against the shared table, not a private copy"
    )
    assert _subentry.LITERAL_CORE_KEY_OWNER is _const.LITERAL_CORE_KEY_OWNER, (
        "the runtime index must rank against the shared table, not a private copy"
    )
    assert _integration.LITERAL_CORE_KEY_OWNER is _const.LITERAL_CORE_KEY_OWNER, (
        "the runtime manager must rank against the shared table, not a private copy"
    )
    assert _const.LITERAL_CORE_KEY_OWNER[SERVICE_SUBENTRY_KEY] == SUBENTRY_TYPE_SERVICE
    assert _const.LITERAL_CORE_KEY_OWNER[TRACKER_SUBENTRY_KEY] == SUBENTRY_TYPE_TRACKER


# ---------------------------------------------------------------------------
# The reading side's tracker axis (PR #1230, AP4).
#
# Two changes are pinned here, and they are one commit on purpose: step 3 gives
# the tracker key the same rank the service key already had, step 1 folds a
# ``tracker`` parked on the service key onto the tracker key. Step 1 alone
# would let the parked subentry compete for ``core_tracking`` while ``metadata``
# was still written without a rank, which trades one order-dependence for
# another instead of removing it.
# ---------------------------------------------------------------------------


class _UnsetUniqueId:
    """Nominal type for the "argument not given" sentinel.

    A bare ``object()`` would widen the parameter to ``str | None | object``,
    which accepts anything; a one-member class keeps the union closed.
    """


_UNSET_UNIQUE_ID = _UnsetUniqueId()


def _ap4_subentry(
    subentry_id: str,
    stored_key: str | None,
    subentry_type: str,
    visible: list[str] | None = None,
    unique_id: str | None | _UnsetUniqueId = _UNSET_UNIQUE_ID,
) -> ConfigSubentry:
    """Build one subentry with a freely chosen (key, type) pair.

    ``unique_id`` defaults to a derived ``uid-<subentry_id>``, which is the
    shape every caller wanted while the manager still carried its
    ``unique_match`` field, and which makes that removed field uniformly ``0``
    (no caller's ``entry_id`` is a substring of it). Passing ``None`` explicitly is therefore not a cosmetic
    variation but the one input that separated the manager from the other two
    rankers, and the sentinel keeps that choice distinguishable from "not
    specified": the field's declared type is ``str | None`` on both the core
    class and this suite's stub, so ``None`` is a value the parameter must be
    able to carry rather than a way of omitting it.

    ``visible`` is written whenever it is not ``None``, so ``[]`` stores an
    *explicitly empty* allow-list and ``None`` stores no key at all. The
    distinction is not academic: it is the very one the reading side used to
    lose (``runtime_patterns/AGENTS.md`` remainder ``U-26``, since closed), so
    a helper that tested truthiness here would have made the empty case
    unrepresentable and the conflation untestable. It matters more now, not
    less: the two shapes carry opposite meanings rather than the same one.

    ``_service_twin_coordinator`` cannot serve these cases: it always adds a
    canonical tracker and gives every twin the *same* type, while the tracker
    key's collisions are precisely about mixed types on one key.

    Import form, chosen rather than inherited (``tests/AGENTS.md``, point 10):
    the module-level ``from homeassistant.config_entries import ConfigSubentry``
    reaches the ``conftest`` stub, and that is fine *here* because nothing
    below draws its force from a core guarantee -- the reading side touches
    these doubles only through ``getattr`` and ``.get()``. Where a double does
    need the genuine frozen dataclass, the resolution is asserted at the
    construction site instead (see ``_ap1_subentry`` in
    ``tests/test_subentry_manager_registry_resolution.py``).
    """

    payload: dict[str, object] = {}
    if stored_key is not None:
        payload["group_key"] = stored_key
    if visible is not None:
        payload["visible_device_ids"] = list(visible)
    resolved_unique_id = (
        f"uid-{subentry_id}" if unique_id is _UNSET_UNIQUE_ID else unique_id
    )
    return ConfigSubentry(
        data=MappingProxyType(payload),
        subentry_type=subentry_type,
        title=f"{subentry_type}:{stored_key}",
        unique_id=cast("str | None", resolved_unique_id),
        subentry_id=subentry_id,
    )


def _ap4_coordinator(
    entry_id: str, subentries: list[ConfigSubentry]
) -> tuple[GoogleFindMyCoordinator, object, _ManagerStub]:
    """Coordinator over exactly ``subentries``, in the given iteration order."""

    entry = make_config_entry(
        entry_id=entry_id,
        title="Google Find My",
        subentries={s.subentry_id: s for s in subentries},
    )
    return _coordinator_over(entry)


@pytest.mark.parametrize("parked_first", [True, False])
def test_ap4_a_parked_tracker_reaches_exactly_one_group(parked_first: bool) -> None:
    """A tracker parked on the service key must describe devices somewhere.

    Class A. The config flow deliberately preserves this shape rather than
    sweeping it (``::test_a_tracker_parked_on_the_service_key_is_not_swept_up``),
    so it arrives here. Before this commit the reading side keyed it by its
    *stored* key, and the outcome was measured, not assumed: the service branch
    forces the metadata ids to ``()`` while ``stored_assigned_ids`` still counts
    them, so the unassigned-device merge did not reclaim them either and the
    devices were described by **no** group at all.

    Both orders are asserted because the fold sits above the rank and the rank
    is what decides which subentry keeps the slot afterwards.
    """

    entry_id = f"e-ap4-parked-{int(parked_first)}"
    canonical = _ap4_subentry(
        "id-canonical", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-1"]
    )
    parked = _ap4_subentry(
        "id-parked", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-2"]
    )
    order = [parked, canonical] if parked_first else [canonical, parked]
    coordinator, _entry, _manager = _ap4_coordinator(entry_id, order)

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert tracker_meta is not None and service_meta is not None
    assert "device-2" in tracker_meta.visible_device_ids, (
        "the devices a parked tracker holds must be described by the tracker "
        "group; keying it by its stored key left them in no group at all"
    )
    assert service_meta.config_subentry_id != "id-parked", (
        "and the parked subentry must have left the service key entirely; "
        "asserting on its ids instead would be tautological, because the "
        "service branch forces them to () for whoever holds the slot"
    )


@pytest.mark.parametrize("higher_id_first", [True, False])
def test_ap4_the_tracker_slot_no_longer_depends_on_iteration_order(
    higher_id_first: bool,
) -> None:
    """Two canonical tracker groups must resolve by rank, not by load order.

    Class A, and the case the contract named as the remaining gap until this
    commit removed the sentence: up to ``12199494`` it read "the tracker key
    has no such rank ... two ``tracker``-typed subentries storing
    ``core_tracking`` still resolve by iteration order". Both candidates are
    equal on every contract field here, so the tie-break decides, and the point
    of the tie-break is that the value is arbitrary while stability is not.
    """

    entry_id = f"e-ap4-tie-{int(higher_id_first)}"
    low = _ap4_subentry("id-a", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER)
    high = _ap4_subentry("id-b", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER)
    order = [high, low] if higher_id_first else [low, high]
    coordinator, _entry, _manager = _ap4_coordinator(entry_id, order)

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.config_subentry_id == "id-a", (
        "among equals the lowest identifier decides, in both iteration orders"
    )


@pytest.mark.parametrize("parked_first", [True, False])
def test_ap4_an_exact_tracker_key_outranks_a_parked_twin(parked_first: bool) -> None:
    """The subentry that stores the tracker key beats one folded onto it.

    Class A. The parked twin carries the *lower* identifier on purpose: without
    the exact-key field the tie-break would hand it the slot, so this assertion
    discriminates that field rather than restating the tie-break.
    """

    entry_id = f"e-ap4-exact-{int(parked_first)}"
    canonical = _ap4_subentry(
        "id-z-canonical", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-1"]
    )
    parked = _ap4_subentry(
        "id-a-parked", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-2"]
    )
    order = [parked, canonical] if parked_first else [canonical, parked]
    coordinator, _entry, _manager = _ap4_coordinator(entry_id, order)

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.config_subentry_id == "id-z-canonical", (
        "an exact stored-key match beats a folded twin, even when the twin's "
        "identifier sorts lower"
    )


@pytest.mark.parametrize("foreign_first", [True, False])
def test_ap4_the_literal_tracker_owner_outranks_a_foreign_type(
    foreign_first: bool,
) -> None:
    """A type that does not own the tracker key loses it.

    Class A. The reachable shape is a *future or unknown* type, named as such
    rather than dressed up: ``service`` and ``hub`` both fold onto the service
    key before they get here, so the only other way onto this key is a type
    this release does not know. ``_async_cleanup_stale_subentries`` skips such
    a subentry deliberately (removal is irreversible), so it survives to reach
    this rank. Its identifier sorts lower, so the tie-break alone would give it
    the slot.
    """

    entry_id = f"e-ap4-owner-{int(foreign_first)}"
    canonical = _ap4_subentry(
        "id-z-tracker", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-1"]
    )
    foreign = _ap4_subentry(
        "id-a-future", TRACKER_SUBENTRY_KEY, "future_type", ["device-2"]
    )
    order = [foreign, canonical] if foreign_first else [canonical, foreign]
    coordinator, _entry, _manager = _ap4_coordinator(entry_id, order)

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.config_subentry_id == "id-z-tracker", (
        "the type that literally owns the tracker key beats one that merely "
        "stores it, whichever order the entry yields"
    )


def test_ap4_a_displaced_tracker_leaves_no_stale_manager_write() -> None:
    """The winner's manager write must replace the loser's, not queue behind it.

    Class A, and the assertion that carries the generalised no-cleanup
    argument. For the service key the argument was "``manager_visible`` is
    never filled for it"; for the tracker key that is false, so the rank block
    relies on the weaker but sufficient claim that every structure it writes is
    keyed by *group* and therefore overwritten. This is the assertion that
    holds the claim up: replacing the assignment with a ``setdefault`` leaves
    the loser's ids in the write-back.

    Two shapes were tried and the fixture is the second, because the first did
    not discriminate: with a *parked* loser the fold exempts its stored ids from the assignment
    bookkeeping, the unassigned-device merge fires and recomputes
    ``manager_visible[TRACKER_SUBENTRY_KEY]`` from the merged metadata, so the
    write matched the metadata whatever the loop had done. Here both
    candidates store explicit, disjoint ids, every device is therefore
    assigned, the merge does not fire, and the write comes straight from the
    loop.

    The order is fixed rather than parametrised: only loser-first exercises an
    overwrite at all.
    """

    entry_id = "e-ap4-displaced"
    winner = _ap4_subentry(
        "id-a-winner", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-1"]
    )
    loser = _ap4_subentry(
        "id-b-loser", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-2"]
    )
    coordinator, _entry, manager = _ap4_coordinator(entry_id, [loser, winner])

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_writes = [ids for key, ids in manager.calls if key == TRACKER_SUBENTRY_KEY]
    assert tracker_writes == [("device-1",)], (
        "the write-back must carry the ids of the subentry that won the slot; "
        "the loser's entry is overwritten rather than cleaned up, which is the "
        "whole no-cleanup argument for a key the manager *is* written for"
    )
    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_writes[0] == tracker_meta.visible_device_ids, (
        "and what is written back is what the winner describes"
    )


@pytest.mark.parametrize("missing_first", [True, False])
def test_ap8_the_manager_and_the_index_agree_when_one_identifier_is_missing(
    missing_first: bool,
) -> None:
    """A stored ``unique_id=None`` must not split the manager from the index.

    This is the shape the manager's removed ``unique_match`` field decided on
    its own. Both candidates are literal ``tracker`` subentries storing
    ``core_tracking``, so the three shared fields tie and the contract's last
    criterion -- the lowest ``subentry_id`` -- is what must decide. Before that
    removal the manager scored them ``(1,1,1,0,0)`` against ``(1,1,1,0,1)`` and took
    the *higher* identifier in both iteration orders, while this side and
    ``config_flow.py::_resolve_existing`` took the lower one.

    Why the divergence is not cosmetic: ``_refresh_subentry_index`` fills
    ``manager_visible[TRACKER_SUBENTRY_KEY]`` from the subentry *it* chose, and
    ``update_visible_device_ids`` resolves that key through the *manager's*
    index. Disagreeing sides mean one group's device assignments are written
    onto the other subentry, with no removal and no log line.

    Why the input is reachable, which is the half the removed field's comment
    got wrong: it argued the pair needs a ``unique_id`` that lacks the entry
    id and that no writer produces one. That is a statement about a wrongly
    *shaped* identifier and says nothing about ``None``, which needs no writer
    -- the core loads ``subentry_data.get("unique_id")``, so a stored subentry
    without the key is ``None`` on load, and ``config_flow.py``'s subentry
    write-backs pass an existing ``None`` straight through.

    Killing mutation: restoring ``unique_match`` to ``_candidate_score``'s
    tuple fails both parametrisations. Restoring ``entry_match`` alone does
    not, and that asymmetry is the measurement rather than a gap: the field is
    uniformly ``0`` because ``ConfigSubentry`` declares no ``entry_id``, so it
    was removed for being inert, not for being wrong, and an inert field has no
    killing mutation to have.

    What this test does **not** cover, stated because the pair it pins is
    exactly where the fourth ranker disagrees: ``_deduplicate_subentries``
    keeps ``id-z-with-uid`` and removes ``id-a-no-uid``, which
    ``async_sync`` reaches unconditionally. That is pre-existing (``B16``) and
    owned by ``PLAN_GFMY_SUBENTRY_DELETION_TYPE_AXIS``; the assertion below is
    about the three rankers naming one holder, not about that holder surviving
    a sync.
    """

    from custom_components.googlefindmy import ConfigEntrySubEntryManager
    from tests.helpers.homeassistant import FakeHass

    entry_id = f"e-ap8-uid-{int(missing_first)}"
    # The *lower* identifier is the one without a ``unique_id``: that is the
    # pairing under which the removed field inverted the tie-break. Giving the
    # higher one an identifier containing ``entry_id`` is what made
    # ``unique_match`` discriminate at all.
    missing_uid = _ap4_subentry(
        "id-a-no-uid",
        TRACKER_SUBENTRY_KEY,
        SUBENTRY_TYPE_TRACKER,
        ["device-1"],
        unique_id=None,
    )
    with_uid = _ap4_subentry(
        "id-z-with-uid",
        TRACKER_SUBENTRY_KEY,
        SUBENTRY_TYPE_TRACKER,
        ["device-2"],
        unique_id=f"{entry_id}-{TRACKER_SUBENTRY_KEY}",
    )
    order = [missing_uid, with_uid] if missing_first else [with_uid, missing_uid]
    coordinator, entry, _manager = _ap4_coordinator(entry_id, order)

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)
    index_choice = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert index_choice is not None

    runtime_manager = ConfigEntrySubEntryManager(FakeHass(config_entries=None), entry)
    runtime_manager._refresh_from_entry()
    managed = runtime_manager._managed.get(TRACKER_SUBENTRY_KEY)
    assert managed is not None

    assert index_choice.config_subentry_id == managed.subentry_id, (
        "a stored unique_id=None must not make the manager and the index name "
        "different holders of the tracker slot"
    )
    assert managed.subentry_id == "id-a-no-uid", (
        "and the holder they both name must be the lowest identifier, which is "
        "the contract's last criterion once the three shared fields tie"
    )


@pytest.mark.parametrize("parked_first", [True, False])
def test_ap4_the_manager_and_the_index_agree_on_the_tracker_slot(
    parked_first: bool,
) -> None:
    """Both rankers must name the same subentry for ``core_tracking``.

    Class A, and the reason step 3 is not cosmetic.
    ``manager_visible[TRACKER_SUBENTRY_KEY]`` drives
    ``update_visible_device_ids``, which resolves the key through the
    *manager's* index. If the two sides rank differently, the manager describes
    one subentry and the write-back lands on the other, and entities change
    group without a single subentry being removed and without a log line.

    The canonical subentry carries the higher identifier deliberately: with the
    exact-key field neutralised on either side, that side falls back to the
    tie-break and picks the other subentry, so the assertion discriminates the
    field instead of restating a shared tie-break.
    """

    from custom_components.googlefindmy import ConfigEntrySubEntryManager
    from tests.helpers.homeassistant import FakeHass

    entry_id = f"e-ap4-agree-{int(parked_first)}"
    canonical = _ap4_subentry(
        "id-z-canonical", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-1"]
    )
    parked = _ap4_subentry(
        "id-a-parked", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-2"]
    )
    order = [parked, canonical] if parked_first else [canonical, parked]
    coordinator, entry, _manager = _ap4_coordinator(entry_id, order)

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)
    index_choice = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert index_choice is not None

    runtime_manager = ConfigEntrySubEntryManager(FakeHass(config_entries=None), entry)
    runtime_manager._refresh_from_entry()
    managed = runtime_manager._managed.get(TRACKER_SUBENTRY_KEY)
    assert managed is not None

    assert index_choice.config_subentry_id == managed.subentry_id, (
        "the index and the manager must select the same subentry for the "
        "tracker slot, or the visibility write-back rebinds entities silently"
    )


def test_ap4_a_parked_tracker_alone_leaves_the_service_group_synthesised() -> None:
    """The fold changes *who describes* the service group, and that is pinned.

    Class A, and the half of step 1 that is not about devices. Where a parked
    tracker is the only holder of the service key, folding it onto the tracker
    key leaves nothing on the service key, so the index falls back to its
    synthesised placeholder. Before the fold the group was described by the
    parked subentry itself.

    That difference is not cosmetic downstream: ``registry.py``'s
    ``_is_real_service_subentry`` rejects the placeholder, because
    ``extract_service_subentry_ids`` still collects the parked subentry through
    its stored ``group_key``, so the set is non-empty and lacks the
    placeholder. The service device then keeps its base identifier but loses
    the ``<entry>:<subentry>:service`` one. Both shapes are unattractive -- the
    old one bound the service device to a *tracker* subentry -- so this pins
    the change rather than declaring either side correct.

    Killing mutation: making the ``elif`` branch of the fold unreachable
    restores ``id-parked`` as the service holder.
    """

    entry_id = "e-ap4-parked-alone"
    parked = _ap4_subentry(
        "id-parked", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-2"]
    )
    coordinator, _entry, _manager = _ap4_coordinator(entry_id, [parked])

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert service_meta is not None and tracker_meta is not None
    assert service_meta.config_subentry_id == f"{entry_id}-service-subentry", (
        "with the parked tracker folded away, the service group must be "
        "described by the synthesised placeholder, not by the tracker subentry"
    )
    assert tracker_meta.config_subentry_id == "id-parked", (
        "and the parked subentry must hold the tracker slot it was folded onto"
    )


@pytest.mark.parametrize("parked_first", [True, False])
def test_ap4_a_parked_tracker_leaves_the_service_pool(parked_first: bool) -> None:
    """With a real ``service`` subentry present, the parked one still leaves.

    Class A. The rank comment claims the parked shape "leaves for
    ``TRACKER_SUBENTRY_KEY`` before this rank sees it, measured in both
    iteration orders"; until this test that measurement lived only in the plan
    notes, which the tree cannot check.

    Honest note on sharpness, measured rather than asserted: **neither**
    assertion has a killing mutation in this state, and an earlier draft of
    this docstring claimed the first one had. Making the fold unreachable
    leaves this test green -- with a real ``service`` subentry present the
    parked one merely loses the rank, its metadata entry is overwritten, and
    the unassigned-device merge still collects ``device-2`` into the tracker
    group. The shape that *does* discriminate is the parked subentry alone,
    pinned by the test above. This one is therefore a regression anchor for
    the shape the rank comment describes, not a proof of the fold.
    """

    entry_id = f"e-ap4-pool-{int(parked_first)}"
    real_service = _ap4_subentry(
        "id-real-service", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, None
    )
    parked = _ap4_subentry(
        "id-parked", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-2"]
    )
    order = [parked, real_service] if parked_first else [real_service, parked]
    coordinator, _entry, _manager = _ap4_coordinator(entry_id, order)

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert tracker_meta is not None and service_meta is not None
    assert "device-2" in tracker_meta.visible_device_ids, (
        "the parked subentry's devices must land in the tracker group in "
        "either iteration order"
    )
    assert service_meta.config_subentry_id == "id-real-service", (
        "and the real service subentry keeps the slot it never lost"
    )


def _ap4_coordinator_with_a_foreign_group(
    entry_id: str, subentries: list[ConfigSubentry]
) -> tuple[GoogleFindMyCoordinator, object, _ManagerStub]:
    """``_ap4_coordinator`` plus a third device, for the ownership cases below.

    ``_coordinator_over`` wires exactly two devices, which is one too few to
    tell "this group sees everything" apart from "this group sees its own ids
    plus the unassigned ones": with two devices and one foreign owner there is
    no device left over to be unassigned, so both readings agree. The third
    device is what makes the two answers differ.
    """

    coordinator, entry, manager = _ap4_coordinator(entry_id, subentries)
    coordinator.data = [
        {"id": "device-1", "name": "Tracker One"},
        {"id": "device-2", "name": "Tracker Two"},
        {"id": "device-3", "name": "Tracker Three"},
    ]
    coordinator._enabled_poll_device_ids = {"device-1", "device-2", "device-3"}
    return coordinator, entry, manager


@pytest.mark.parametrize("parked_first", [True, False])
def test_ap4_a_parked_tracker_that_wins_keeps_its_own_allow_list(
    parked_first: bool,
) -> None:
    """A folded winner must not swallow a device another group owns.

    Class A, and the regression this commit repairs. Folding a parked tracker
    onto the tracker key used to *remove* its ``visible_device_ids`` so the
    unassigned-device merge would reclaim them when the rank took the key away
    again. That removal did two things at once, and only one was wanted:
    downstream, ``allow_filter`` reads a missing list as "no restriction", not
    as "no assignment". A parked tracker that *keeps* the key was therefore
    handed the entire device index -- ``device-3`` included, although
    ``other_group`` owns it -- and ``manager.update_visible_device_ids``
    persisted that widened list, exposing the device through two subentries.

    The old comment defended the removal by claiming the winner's shape matched
    the pre-fold one ("alone, its ids were already joined by every unassigned
    device"). That holds only while no other group owns anything: the merge
    adds *unassigned* devices, an absent filter adds *every* device. Hence the
    third device and the foreign owner in this fixture.

    Killing mutation: restoring ``data.pop("visible_device_ids", None)`` in the
    ``elif`` branch of the fold puts ``device-3`` back into both assertions.
    """

    entry_id = f"e-ap4-winner-{int(parked_first)}"
    parked = _ap4_subentry(
        "id-parked", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-2"]
    )
    foreign = _ap4_subentry(
        "id-foreign", "other_group", SUBENTRY_TYPE_TRACKER, ["device-3"]
    )
    order = [parked, foreign] if parked_first else [foreign, parked]
    coordinator, _entry, manager = _ap4_coordinator_with_a_foreign_group(
        entry_id, order
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    foreign_meta = coordinator.get_subentry_metadata(key="other_group")
    assert tracker_meta is not None and foreign_meta is not None
    assert "device-3" not in tracker_meta.visible_device_ids, (
        "the folded winner must keep its own allow-list; without it the group "
        "sees the whole device index, including what another group owns"
    )
    assert tracker_meta.visible_device_ids == ("device-1", "device-2"), (
        "it sees its own id plus the one device no group claims -- which is "
        "what the fold's own justification promised"
    )
    assert foreign_meta.visible_device_ids == ("device-3",), (
        "and the owning group keeps its device"
    )
    persisted = dict(manager.calls)
    assert "device-3" not in persisted.get(TRACKER_SUBENTRY_KEY, ()), (
        "the manager write-back is where the widened list became durable, so "
        "the metadata assertion alone would understate the damage"
    )


@pytest.mark.parametrize("parked_first", [True, False])
def test_ap4_a_parked_tracker_that_loses_is_still_rehomed(parked_first: bool) -> None:
    """The other half: a folded loser's devices must still be reclaimed.

    Class A, and the reason the fold touches the bookkeeping at all. Keeping
    the allow-list (the test above) must not undo what the removal was *for*:
    a parked tracker that loses the core key has no metadata entry left, so its
    devices reach a group only if the unassigned-device merge counts them as
    unassigned. That is why the fold exempts them from ``stored_assigned_ids``
    rather than emptying ``data``.

    The foreign group is here for the same reason as above: without it a green
    result would also be produced by "the tracker sees everything".

    Killing mutation: setting ``ids_are_rehomable`` to ``False`` in the
    ``elif`` branch of the fold strands ``device-2`` in no group at all.
    """

    entry_id = f"e-ap4-loser-{int(parked_first)}"
    canonical = _ap4_subentry(
        "id-canonical", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-1"]
    )
    parked = _ap4_subentry(
        "id-parked", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-2"]
    )
    foreign = _ap4_subentry(
        "id-foreign", "other_group", SUBENTRY_TYPE_TRACKER, ["device-3"]
    )
    order = (
        [parked, canonical, foreign] if parked_first else [canonical, foreign, parked]
    )
    coordinator, _entry, _manager = _ap4_coordinator_with_a_foreign_group(
        entry_id, order
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    foreign_meta = coordinator.get_subentry_metadata(key="other_group")
    assert tracker_meta is not None and foreign_meta is not None
    assert tracker_meta.config_subentry_id == "id-canonical", (
        "the canonical tracker outranks the parked one, so this really is the "
        "loser case the assertion below is about"
    )
    assert "device-2" in tracker_meta.visible_device_ids, (
        "the loser's devices must be re-homed into the tracker group; the "
        "exemption from the assignment bookkeeping is what gets them there"
    )
    assert "device-3" not in tracker_meta.visible_device_ids, (
        "and re-homing must not reach into a group that owns its devices"
    )


@pytest.mark.parametrize("parked_first", [True, False])
def test_ap4_a_parked_tracker_with_an_empty_allow_list_stays_empty(
    parked_first: bool,
) -> None:
    """An *explicitly empty* allow-list must survive the fold as "owns nothing".

    Class A, and the second half of the regression the sibling test above
    repairs. Keeping a non-empty ``visible_device_ids`` through the fold is not
    enough, because the reading side used to normalise a list that ends up
    empty to ``None``, and ``allow_filter`` reads ``None`` as *no restriction*.
    A parked tracker whose stored list is ``()`` therefore arrived on the
    tracker key unfiltered and was handed the entire device index,
    ``device-3`` included,
    although ``other_group`` owns it -- and ``manager.update_visible_device_ids``
    makes that durable.

    The shape is reachable through ordinary use, not only through migration
    residue: ``_async_assign_devices_to_subentry`` writes
    ``tuple(sorted(...))`` back unconditionally, so moving the *last* device
    out of a group stores ``()`` for it. What the fold changed is where that
    lands. Before it, a parked tracker stayed on the service key, whose branch
    forces the visible ids to ``()`` and never fills ``manager_visible``, so the
    absent-filter reading was unreachable for it. The fold routes it to the
    tracker key, where neither guard applies.

    That is the boundary of this fix. The absent-filter reading itself is
    older and wider (``runtime_patterns/AGENTS.md`` remainder ``U-26``, owned by
    ``PLAN_GFMY_VISIBILITY_ASSIGNMENT_BOOKKEEPING``) and has since been closed
    for every group, so what this test pins is no longer an exception carved
    out for the fold but the general reading. It is kept as the fold's own
    guard: it is the only place asserting that a *folded* group reaches the
    normalisation with its stored list intact.

    Killing mutation: making the normalisation leave ``normalized_allowed`` at
    ``None`` when nothing survived -- i.e. restoring the collapse -- puts
    ``device-3`` back into both assertions. The arm-specific mutation this
    docstring named before the closure no longer exists, because the arm does
    not.
    """

    entry_id = f"e-ap4-empty-{int(parked_first)}"
    parked = _ap4_subentry("id-parked", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, [])
    foreign = _ap4_subentry(
        "id-foreign", "other_group", SUBENTRY_TYPE_TRACKER, ["device-3"]
    )
    order = [parked, foreign] if parked_first else [foreign, parked]
    coordinator, _entry, manager = _ap4_coordinator_with_a_foreign_group(
        entry_id, order
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    foreign_meta = coordinator.get_subentry_metadata(key="other_group")
    assert tracker_meta is not None and foreign_meta is not None
    assert "device-3" not in tracker_meta.visible_device_ids, (
        "an explicitly empty allow-list claims nothing, so the folded group "
        "must not be handed a device another group owns"
    )
    assert tracker_meta.visible_device_ids == ("device-1", "device-2"), (
        "it sees exactly the devices no group claims -- what the "
        "unassigned-device merge hands every tracker group, no more"
    )
    assert foreign_meta.visible_device_ids == ("device-3",), (
        "and the owning group keeps its device"
    )
    persisted = dict(manager.calls)
    assert "device-3" not in persisted.get(TRACKER_SUBENTRY_KEY, ()), (
        "the manager write-back is where the widened list became durable, so "
        "the metadata assertion alone would understate the damage"
    )


# --- AP3 characterisation: what each stored-key shape means today -----------
#
# Four shapes reach the normalisation in ``_refresh_subentry_index``, and the
# reading side answers them in only two distinct ways: unrestricted (E1, E2,
# E4) or filtered (E3) -- past tense throughout, see below. That collapse
# *was* the point: three shapes carrying
# three different statements shared one answer, and the table below pinned it
# per shape so the semantics change in
# ``PLAN_GFMY_VISIBILITY_ASSIGNMENT_BOOKKEEPING`` had to face each one
# separately instead of moving them as a block.
#
# That change has since landed, and the table now pins its result. Two of the
# four answers moved, and only those two: a *present* key -- empty, or emptied
# because none of its entries survived normalisation -- restricts to nothing
# instead of collapsing into "unrestricted". An absent key is still
# unrestricted, and a key that normalises to a non-empty set still filters to
# it, which is the property that keeps freshly created groups working.
_AP3_STORED_SHAPES: dict[str, list[str] | None] = {
    # present and empty -- "this group owns nothing"
    "E1": [],
    # absent -- never assigned
    "E2": None,
    # present, normalises to a non-empty set
    "E3": ["device-1"],
    # present and non-empty, yet every entry is discarded by the normalisation:
    # ``rsplit(":", 1)[-1]`` leaves the empty string for a bare prefix
    "E4": ["e-ap3:"],
}

# What the *tracker* group -- the default one -- sees per shape. Read this
# together with ``_AP3_ANSWER_FOR_A_NON_DEFAULT_GROUP`` below: the default
# group is fed by the unassigned-device merge, so a restricting filter does not
# leave it empty here. ``device-1`` and ``device-2`` belong to no group and
# return through that merge; ``device-3`` belongs to ``other_group`` and is the
# one the filter has to keep out.
_AP3_ANSWER_FOR_THE_DEFAULT_GROUP: dict[str, tuple[str, ...]] = {
    # restricted to nothing -- what remains is the merge's doing, not the
    # filter's
    "E1": ("device-1", "device-2"),
    # unrestricted: never assigned, so the whole index
    "E2": ("device-1", "device-2", "device-3"),
    # filtered to its own id; ``device-2`` joins through the merge
    "E3": ("device-1", "device-2"),
    # same as E1: present, but nothing survived normalisation
    "E4": ("device-1", "device-2"),
}

# The same shapes on a group the merge does *not* feed. This is where the
# change is visible without the merge cushioning it, and where "restricts to
# nothing" and "unrestricted" are furthest apart.
_AP3_ANSWER_FOR_A_NON_DEFAULT_GROUP: dict[str, tuple[str, ...]] = {
    "E1": (),
    "E2": ("device-1", "device-2", "device-3"),
    "E3": ("device-1",),
    "E4": (),
}


@pytest.mark.parametrize("shape", sorted(_AP3_STORED_SHAPES))
def test_ap3_a_stored_shape_decides_what_a_group_may_see(shape: str) -> None:
    """Pin, per stored shape, whether the allow-list still restricts anything.

    This started as the characterisation the semantics change measured against
    and now pins its outcome. It is deliberately built so that the *merge* is
    not what supplies the answer.
    The sibling guard in ``test_coordinator_visibility_availability.py`` wires
    a single device into the tracker group; that device returns through the
    unassigned-device merge whether or not a filter is applied, so the two
    mechanisms are indistinguishable there and the guard stays green under a
    change to the filter. Here ``device-3`` is owned by ``other_group`` through
    a non-empty stored list, and the merge only re-homes devices no group
    claims, so under the code as it stands an absent filter is the route by
    which ``device-3`` reaches the tracker view.

    That is a statement about this code, not a proof about every possible
    code: a mutation to the *merge* alone -- emptying the assigned-id set and
    narrowing the union -- also pushes ``device-3`` into the tracker view while
    the filter stays intact, and the fixture guard below does not notice,
    because ``other_group``'s own view is unaffected. What the fixture does
    buy is that no single change to the filter can be mistaken for the merge
    or the other way round.

    Only ``E2`` still ends up unrestricted, and that is intended: a group that
    was never assigned anything is unrestricted, which is how a freshly
    created group is meant to work. ``E1`` and ``E4`` used to share that answer
    for a different reason -- a list saying "nothing" and a list whose entries
    are all unusable both collapsed into the same absent filter -- and no
    longer do.

    Each shape is checked on *two* groups, because the default group is a poor
    place to read the change off. It is fed by the unassigned-device merge, so
    restricting its filter to nothing still leaves it the two devices no group
    claims; what changes there is that ``device-3`` stays out. The non-default
    group is the plain reading: it goes to the empty tuple under ``E1`` and
    ``E4``, and stays at the whole index under ``E2``. Both are asserted, so a
    later change cannot fix one reading while quietly breaking the other.

    Note the boundary against the fold: the parked-tracker tests above cover
    ``E1`` and a non-empty list on the *re-homable* arm, which used to be the
    only arm where an empty list was kept. That arm no longer exists as a
    special case -- keeping the empty set is now what every group gets -- and
    those tests are the fold's own guard rather than a statement about this
    shape.

    Killing mutations, one per shape, because a single mutation cannot cover
    all four. Which assertion each one trips was measured, not assumed, and
    the difference matters: a case killed through the *fixture guard* only
    says the separation broke, while a case killed through the shape
    assertion says this shape's answer really depends on the mutated code.

    * ``E1``/``E4`` -- restore the collapse, i.e. leave ``normalized_allowed``
      at ``None`` when nothing survived normalisation. Both fail on the default
      group's shape assertion (``device-3`` re-enters); ``E2`` and ``E3`` stay
      green, which is what makes this the mutation that separates the change
      from its neighbours.
    * ``E2`` -- treat an absent key as an empty set (the equivalence the plan
      measured and rejected). Only ``E2`` fails, on the default group's shape
      assertion: it loses ``device-3`` while ``device-1`` and ``device-2``
      stay, because the merge re-homes them. The non-default assertion would
      catch it too, and more starkly -- that group loses everything, which is
      the breakage that motivates the rejection -- but the default one is
      reached first. An earlier revision of this docstring claimed ``device-1``
      was lost on the default group, which a review disproved.
    * ``E3`` -- force ``allow_filter`` to ``None``. All four fail, and *which*
      assertion catches them was measured: ``E1``, ``E3`` and ``E4`` on the
      default group's shape assertion, ``E2`` on the fixture guard, because for
      ``E2`` the default group's answer is unrestricted either way and only
      ``other_group`` changes. A narrower mutation is not available here: every
      arm of the normalisation is shared by all groups, so nothing
      distinguishes them at that point.
    """

    entry_id = f"e-ap3-{shape.lower()}"
    canonical = _ap4_subentry(
        "id-canonical",
        TRACKER_SUBENTRY_KEY,
        SUBENTRY_TYPE_TRACKER,
        _AP3_STORED_SHAPES[shape],
    )
    plain = _ap4_subentry(
        "id-plain", "plain_group", SUBENTRY_TYPE_TRACKER, _AP3_STORED_SHAPES[shape]
    )
    foreign = _ap4_subentry(
        "id-foreign", "other_group", SUBENTRY_TYPE_TRACKER, ["device-3"]
    )
    coordinator, _entry, manager = _ap4_coordinator_with_a_foreign_group(
        entry_id, [canonical, plain, foreign]
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    plain_meta = coordinator.get_subentry_metadata(key="plain_group")
    foreign_meta = coordinator.get_subentry_metadata(key="other_group")
    assert tracker_meta is not None and foreign_meta is not None
    assert plain_meta is not None
    assert (
        tracker_meta.visible_device_ids == _AP3_ANSWER_FOR_THE_DEFAULT_GROUP[shape]
    ), (
        f"shape {shape} decides what the default group may see, and the answer "
        "has to change deliberately rather than as a side effect"
    )
    assert (
        plain_meta.visible_device_ids == _AP3_ANSWER_FOR_A_NON_DEFAULT_GROUP[shape]
    ), (
        f"shape {shape} on a group the merge does not feed -- this is where "
        "'restricts to nothing' and 'unrestricted' are furthest apart, and "
        "where a regression would otherwise hide behind the merge"
    )
    assert foreign_meta.visible_device_ids == ("device-3",), (
        "the owning group keeps its device under every shape; a change here "
        "would mean the fixture stopped separating the two mechanisms"
    )

    reaches_foreign_device = "device-3" in tracker_meta.visible_device_ids
    tracker_writes = [ids for key, ids in manager.calls if key == TRACKER_SUBENTRY_KEY]
    assert tracker_writes, (
        "the write-back has to have happened at all -- without this the "
        "assertion below is vacuously true for the restricted shapes, which "
        "is the one-sided guard tests/AGENTS.md point 8 warns about"
    )
    persisted = dict(manager.calls).get(TRACKER_SUBENTRY_KEY, ())
    assert ("device-3" in persisted) is reaches_foreign_device, (
        "and the write-back mirrors the view: that is how a widened list "
        "becomes durable instead of lasting one refresh"
    )


# --- AP4: the migration record the semantics change owes its users ----------


def _ap4_refresh_with_shape(
    entry_id: str,
    key: str,
    stored: list[str] | None,
    *,
    subentry_id: str = "id-under-test",
) -> GoogleFindMyCoordinator:
    """Run one refresh with a single group holding ``stored``, plus an owner."""

    under_test = _ap4_subentry(subentry_id, key, SUBENTRY_TYPE_TRACKER, stored)
    foreign = _ap4_subentry(
        "id-foreign", "other_group", SUBENTRY_TYPE_TRACKER, ["device-3"]
    )
    coordinator, _entry, _manager = _ap4_coordinator_with_a_foreign_group(
        entry_id, [under_test, foreign]
    )
    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)
    return coordinator


def _ap4_reinterpretation_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return the migration-record lines captured so far."""

    return [
        record.getMessage()
        for record in caplog.records
        if "selects no device" in record.getMessage()
    ]


def test_ap4_a_reinterpreted_allow_list_is_recorded_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The semantics change owes an operator a record, and this is it.

    ``agents/runtime_patterns/AGENTS.md`` names this log line as the migration
    reasoning that `U-26` was waiting for, which makes it a contract rather
    than a convenience: a group whose stored list is empty stops claiming any
    device, and where nothing else hands them back it shows none of them. If
    that happens silently the user reports devices as having disappeared.
    Because it is a contract, it is pinned here rather than left to survive by
    accident. The group under test is deliberately *not* the default one --
    that shape is covered by its own negative test below, because the merge
    hands it everything back and there is nothing to report.

    Four properties, each with its own killing mutation:

    * it is emitted at all -- deleting the call makes this red;
    * at ``WARNING`` -- and which assertion catches a level change was
      measured, not assumed: *lowering* it to ``info`` or ``debug`` trips the
      presence assertion first, because ``caplog`` is set to ``WARNING`` and
      never captures the record at all. The level assertion itself guards the
      other direction, against a record loud enough to look like a failure. A
      record an operator never sees is not a record, which is why the presence
      assertion is the one that matters here;
    * once per subentry -- removing the dedup set makes the second refresh
      emit again, which matters because refreshes are frequent;
    * it names the subentry -- an operator needs to find the group again.
    """

    caplog.set_level(logging.WARNING)
    coordinator = _ap4_refresh_with_shape("e-ap4-log", "plain_group", [])

    lines = _ap4_reinterpretation_lines(caplog)
    assert len(lines) == 1, (
        "exactly one migration record for the one affected subentry -- none "
        "means the change is silent, more than one means the dedup is gone"
    )
    assert "id-under-test" in lines[0], (
        "the record has to name the subentry, because that is what an "
        "operator uses to find the group and reassign its devices"
    )
    levels = {
        record.levelno
        for record in caplog.records
        if "selects no device" in record.getMessage()
    }
    assert levels == {logging.WARNING}, (
        "a visibility change the user did not ask for belongs above debug; at "
        "debug it is invisible in a default installation"
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)
    assert len(_ap4_reinterpretation_lines(caplog)) == 1, (
        "a second refresh must not repeat it -- refreshes run on every poll, "
        "so without the dedup this floods the log"
    )


def test_ap4_the_migration_record_leaks_neither_group_key_nor_device_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The record must not carry an email address or a device identifier.

    The repository contract says plainly: never log tokens, email addresses,
    precise coordinates, device IDs, or raw API payloads. Both forbidden shapes
    are reachable right here. A legacy per-account group stores its owner's
    address *as* its ``group_key`` (``agents/config_flow/AGENTS.md``, B13), and
    the stored allow-list holds namespaced device identifiers. An earlier
    revision of this record logged both, which is what this test exists to
    prevent from coming back.

    What survives is the subentry id -- an internal handle, not a device
    identifier -- and a count, which is enough to tell a deliberately emptied
    list (``0``) from one whose entries were all discarded (``> 0``).

    Killing mutation: putting ``group_key`` or ``raw_allowed`` back into the
    log call.
    """

    caplog.set_level(logging.WARNING)
    # Both entries are discarded by the normalisation: ``rsplit(":", 1)[-1]``
    # leaves the empty string for a bare prefix. The device name is still in
    # the *stored* value, which is exactly what must not reach the log.
    _ap4_refresh_with_shape(
        "e-ap4-leak", "owner@example.com", ["device-9:", "e-ap4-leak:"]
    )

    lines = _ap4_reinterpretation_lines(caplog)
    assert len(lines) == 1
    assert "owner@example.com" not in lines[0], (
        "a legacy group key is an email address, and the contract forbids logging one"
    )
    assert "device-9" not in lines[0], (
        "the stored list holds device identifiers, which the same rule forbids"
    )
    assert "of 2 entries" in lines[0], (
        "the count is what replaces the values: it separates a deliberately "
        "emptied list from one the normalisation discarded"
    )


@pytest.mark.parametrize(
    ("case", "key", "stored"),
    [
        ("folded", SERVICE_SUBENTRY_KEY, []),
        ("service", SERVICE_SUBENTRY_KEY, []),
    ],
)
def test_ap4_no_record_where_the_reading_did_not_change(
    caplog: pytest.LogCaptureFixture, case: str, key: str, stored: list[str]
) -> None:
    """Only groups whose reading actually changed get a record.

    Two shapes reach the same branch without having changed. A tracker parked
    on the service key is folded onto the tracker key, and for folded groups an
    empty list was already kept as an empty set before this change. A group on
    the canonical service key has its visible ids forced to ``()`` a few lines
    further down regardless of any filter.

    Telling an operator that either of them "now shows no devices instead of
    all of them" would be false, and this line is the migration record, so it
    has to be true. That is the whole point of the guard, and a review found it
    missing from the first revision.

    Killing mutation: dropping the ``not ids_are_rehomable and group_key !=
    SERVICE_SUBENTRY_KEY`` condition from the call site.
    """

    caplog.set_level(logging.WARNING)
    subentry_type = (
        SUBENTRY_TYPE_SERVICE if case == "service" else SUBENTRY_TYPE_TRACKER
    )
    under_test = _ap4_subentry("id-quiet", key, subentry_type, stored)
    foreign = _ap4_subentry(
        "id-foreign", "other_group", SUBENTRY_TYPE_TRACKER, ["device-3"]
    )
    coordinator, _entry, _manager = _ap4_coordinator_with_a_foreign_group(
        f"e-ap4-quiet-{case}", [under_test, foreign]
    )
    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    assert _ap4_reinterpretation_lines(caplog) == [], (
        f"the {case} shape reached the branch without its reading having "
        "changed, so a record claiming a change would be a false statement"
    )


def test_ap4_an_empty_allow_list_survives_an_empty_poll_as_empty() -> None:
    """A restricting filter keeps restricting when the device index is empty.

    Consequence of the change, asserted so it cannot flip back unnoticed. The
    continuity fallback (``previous_visible`` when ``device_index`` is empty)
    exists so a transient empty poll does not blank a group's view. It is
    filtered by ``allow_filter`` like everything else, so for a group whose
    stored list selects nothing it now yields nothing, where the collapsed
    ``None`` used to let the previous view through.

    That is the correct reading and not a regression to repair: the group owns
    nothing, and the devices it showed on the populated refresh reached it
    solely through the unassigned-device merge, which has nothing to hand over
    when there are no devices. It is asserted because the alternative reading
    -- "the fallback should win" -- is tempting and would quietly restore the
    fail-open behaviour for exactly the shape this change is about.

    No data is lost either way: the write-back compares the derived ``()``
    against the stored empty list, finds them equal and does not write.

    Killing mutation: restoring the collapse, which lets the previous view
    through the fallback and makes the second assertion red.
    """

    coordinator = _ap4_refresh_with_shape("e-ap4-poll", TRACKER_SUBENTRY_KEY, [])
    populated = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert populated is not None
    assert populated.visible_device_ids == ("device-1", "device-2"), (
        "with a device index the merge still hands over what no group claims; "
        "only the foreign-owned device stays out"
    )

    coordinator.data = []
    coordinator._refresh_subentry_index([], skip_repair=True)
    drained = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert drained is not None
    assert drained.visible_device_ids == (), (
        "and with no device index there is nothing unclaimed to hand over, so "
        "a group owning nothing sees nothing -- the continuity fallback must "
        "not be read as a licence to ignore the filter"
    )


def test_ap4_no_record_where_the_merge_hands_everything_back(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The default group in a single-group installation loses nothing.

    The third and most common shape that reaches the same branch without a
    change worth reporting, and the one a first revision of the guard missed.
    Where no other group claims anything, the unassigned-device merge hands the
    default group every device back, so the result is bit-for-bit what it was
    before the semantics changed: the filter excluded everything, and the merge
    put everything back.

    Telling that operator "this group now shows fewer devices" would be false,
    and it is the likeliest installation shape there is. The guard therefore
    does not enumerate exempt shapes -- an earlier revision tried that and had
    to grow a third case -- but measures what the group actually ends up
    seeing, after the merge, against the device index.

    Killing mutation: moving the record back into the normalisation branch, or
    dropping the ``>= set(device_index)`` comparison from the block that drains
    the collected candidates.
    """

    caplog.set_level(logging.WARNING)
    lone = _ap4_subentry("id-lone", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, [])
    coordinator, _entry, _manager = _ap4_coordinator_with_a_foreign_group(
        "e-ap4-lone", [lone]
    )
    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert meta is not None
    assert meta.visible_device_ids == ("device-1", "device-2", "device-3"), (
        "precondition: with nobody else claiming anything the merge hands the "
        "whole index back, so nothing was lost"
    )
    assert _ap4_reinterpretation_lines(caplog) == [], (
        "and where nothing was lost there is nothing to report -- a record "
        "here would tell the most common installation shape that its devices "
        "went missing when they did not"
    )


def test_ap4_no_record_for_a_subentry_that_lost_its_core_slot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A candidate that does not end up describing its group says nothing.

    Two tracker subentries storing the same core key are the reachable
    collision the rank exists for; only one of them ends up in the index. The
    loser's reading never becomes visible to anyone, so a record telling its
    operator that "this group now shows fewer devices" would be about a group
    the loser does not describe -- and the winner's own list is untouched.

    This is the third exemption, and unlike the first two it is not a shape but
    an outcome: it can only be decided after the rank has run, which is the
    other reason the record is drained at the end rather than emitted inline.

    Killing mutation: dropping the ``config_subentry_id != pending_id`` check
    from the block that drains the collected candidates.
    """

    caplog.set_level(logging.WARNING)
    # Which of the two takes the slot was measured, not assumed: the tie is
    # broken on the lower identifier, and neither the listing order nor the
    # stored list enters into it. The candidate -- the one whose list selects
    # nothing, and which would report if it counted -- therefore gets the
    # higher identifier so that it loses.
    winner = _ap4_subentry(
        "id-a-keeps-slot", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, ["device-1"]
    )
    loser = _ap4_subentry(
        "id-z-loses-slot", TRACKER_SUBENTRY_KEY, SUBENTRY_TYPE_TRACKER, []
    )
    foreign = _ap4_subentry(
        "id-foreign", "other_group", SUBENTRY_TYPE_TRACKER, ["device-3"]
    )
    coordinator, _entry, _manager = _ap4_coordinator_with_a_foreign_group(
        "e-ap4-slot", [winner, loser, foreign]
    )
    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert meta is not None
    assert meta.config_subentry_id == "id-a-keeps-slot", (
        "precondition: the rank has to have picked one of the two, otherwise "
        "this test measures something else"
    )
    assert _ap4_reinterpretation_lines(caplog) == [], (
        "the loser does not describe the group, so its reading is not what "
        "the operator sees and there is nothing to report about it"
    )


def test_ap6_a_synthesised_tracker_leaves_a_foreign_group_its_devices() -> None:
    """No tracker subentry stored: the synthesised one must still not take everything.

    ``runtime_patterns/AGENTS.md`` remainder ``U-25``. Every other reader of a
    device assignment had been taught that "no restriction known" is not the
    same as "no restriction"; this branch was the last one that still equated
    them, and it did so at the *widest* possible point: with no tracker
    subentry stored, it seeded the synthesised one from the whole device index.
    A group that owns ``device-3`` therefore shared it with the tracker, and
    the manager write-back persisted that widening, so one device was exposed
    through two subentries.

    The assertion on ``manager.calls`` is not decoration, and it is a
    *negative* one on purpose. The metadata alone is recomputed on every
    refresh and would repair itself; what made this a data defect rather than
    a display one is that the list was written back. Narrowing it was not
    enough: the write-back does not reach this group at all, because the
    manager canonicalises every ``tracker``-typed subentry onto
    ``TRACKER_SUBENTRY_KEY`` whatever key it stored, so the only subentry
    holding that slot here is ``other_group``'s. Measured on 2026-08-06
    against the real ``ConfigEntrySubEntryManager``: writing the narrowed list
    left that subentry storing ``['device-1', 'device-2']`` and having lost
    the ``device-3`` it owns. A synthesised group has nothing of its own to
    persist into, so it persists nothing.

    Killing mutations: restoring ``tuple(sorted(device_index.keys()))`` in the
    synthesis branch puts ``device-3`` back into the metadata; restoring the
    ``manager_visible[TRACKER_SUBENTRY_KEY]`` assignment in that branch brings
    the misdirected write back.
    """

    foreign = _ap4_subentry(
        "id-foreign", "other_group", SUBENTRY_TYPE_TRACKER, ["device-3"]
    )
    coordinator, _entry, manager = _ap4_coordinator_with_a_foreign_group(
        "e-ap6-foreign", [foreign]
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    foreign_meta = coordinator.get_subentry_metadata(key="other_group")
    assert tracker_meta is not None and foreign_meta is not None
    assert tracker_meta.visible_device_ids == ("device-1", "device-2"), (
        "the synthesised tracker takes what no other group claims, not the "
        "whole device index"
    )
    assert foreign_meta.visible_device_ids == ("device-3",), (
        "the owning group is untouched; if this changes the fixture stopped "
        "separating ownership from visibility and the finding above is moot"
    )
    assert [key for key, _ids in manager.calls if key == TRACKER_SUBENTRY_KEY] == [], (
        "a synthesised tracker persists nothing: the only subentry the "
        "manager files under this key belongs to another group, and writing "
        "there replaces that group's stored assignment with this list"
    )
    assert ("other_group", ("device-3",)) in manager.calls, (
        "the owning group's own write-back is untouched; without it the "
        "assertion above could pass by writing nothing at all"
    )


def test_ap6_a_synthesised_tracker_still_takes_every_unclaimed_device() -> None:
    """Negative control: without a foreign owner nothing narrows.

    The fix above must not be readable as "the synthesised tracker starts
    empty". An installation with no other group is the common case, and there
    the tracker is still the group every device belongs to.

    The fixture carries a service subentry rather than nothing at all, and
    that is load-bearing: an empty subentry list makes the reader append a
    ``core_tracking`` placeholder, which puts the tracker key into ``metadata``
    and skips the synthesised branch entirely. Written that way the case never
    reached the lines it claims to guard -- measured with ``sys.settrace`` on
    2026-08-06, hit count zero. The service group contributes no ownership, so
    the branch runs with an empty ``stored_assigned_ids``, which is the input
    this case is about.

    Its two assertions do different work, and only the second is sharp on its
    own. The visibility one is carried twice: with nothing owned, the
    unassigned-device merge hands the whole index back however the branch
    seeded it, so a mutation of the exclusion alone leaves it green. The
    write-back one is not, and that asymmetry is measured, not assumed --
    seeding the branch from the empty tuple routes the group through the merge
    instead, which *does* fill ``manager_visible``, and the assertion fails
    with ``['core_tracking'] != []``. The case that pins the exclusion itself
    is the ownership one below.
    """

    service = _ap4_subentry(
        "id-svc-alone", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, None
    )
    coordinator, _entry, manager = _ap4_coordinator_with_a_foreign_group(
        "e-ap6-alone", [service]
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    assert tracker_meta is not None
    assert tracker_meta.visible_device_ids == ("device-1", "device-2", "device-3"), (
        "with nobody else claiming anything the synthesised tracker still "
        "holds the whole index"
    )
    assert [key for key, _ids in manager.calls if key == TRACKER_SUBENTRY_KEY] == [], (
        "still nothing to persist into: whether the synthesised group ends up "
        "wide or narrow, no stored subentry carries its key"
    )


def test_ap6_a_device_moved_to_the_service_group_is_not_reclaimed() -> None:
    """The *stored* claim counts, not only the computed one.

    A device the user moved onto the service subentry keeps its entry in that
    subentry's stored allow-list, while the service metadata has its visible
    ids forced to empty a few lines earlier. Asking only the metadata would
    therefore call the device unassigned and hand it to the synthesised
    tracker -- quietly undoing the move. This is why the exclusion reads
    ``stored_assigned_ids`` and not the metadata views.

    Killing mutation: excluding what the metadata views name instead of what
    ``stored_assigned_ids`` records puts ``device-3`` back into the tracker,
    because the service metadata is empty by then -- which is exactly the
    undone move.
    """

    service = _ap4_subentry(
        "id-svc", SERVICE_SUBENTRY_KEY, SUBENTRY_TYPE_SERVICE, ["device-3"]
    )
    coordinator, _entry, _manager = _ap4_coordinator_with_a_foreign_group(
        "e-ap6-moved", [service]
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    service_meta = coordinator.get_subentry_metadata(key=SERVICE_SUBENTRY_KEY)
    assert tracker_meta is not None and service_meta is not None
    assert service_meta.visible_device_ids == (), (
        "the service group's computed view is empty by construction -- if it "
        "were not, the metadata source alone would already cover this case "
        "and the assertion below would prove nothing"
    )
    assert "device-3" not in tracker_meta.visible_device_ids, (
        "the stored claim survives the empty computed view; otherwise the "
        "move is undone on the next refresh"
    )
    assert tracker_meta.visible_device_ids == ("device-1", "device-2")


def test_ap6_a_group_that_only_sees_devices_does_not_own_them() -> None:
    """Seeing is not owning, and the exclusion must not confuse the two.

    A group that stored no allow-list is unrestricted, so its computed view is
    the whole device index -- while it owns nothing. Excluding what such a
    group *sees* would hand every device to it and leave the synthesised
    tracker empty: the same defect as ``U-25``, pointing the other way. The
    exclusion therefore reads the stored assignment only.

    This is also the control that separates the fix from doing nothing. Seeding
    the branch from the empty tuple and letting the unassigned-device merge
    fill it back in produces the identical result in every other case here,
    because that merge subtracts what any group already sees -- which is
    exactly the wider question that goes wrong in this one.

    Killing mutations, both of which pass every other test in this group:
    adding the metadata views to the exclusion, and replacing the whole branch
    with the empty tuple.
    """

    unrestricted = _ap4_subentry(
        "id-unrestricted", "other_group", SUBENTRY_TYPE_TRACKER, None
    )
    coordinator, _entry, manager = _ap4_coordinator_with_a_foreign_group(
        "e-ap6-sees", [unrestricted]
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    other_meta = coordinator.get_subentry_metadata(key="other_group")
    assert tracker_meta is not None and other_meta is not None
    assert other_meta.visible_device_ids == ("device-1", "device-2", "device-3"), (
        "the unrestricted group sees everything -- if it did not, this case "
        "would not tell ownership and visibility apart and would prove nothing"
    )
    assert tracker_meta.visible_device_ids == ("device-1", "device-2", "device-3"), (
        "nobody owns anything, so the synthesised tracker keeps the whole "
        "index; a group's mere view must not take devices away from it"
    )
    assert [key for key, _ids in manager.calls if key == TRACKER_SUBENTRY_KEY] == [], (
        "and it still persists nothing, the synthesised group having no "
        "subentry of its own to write into"
    )


def test_ap6_a_fully_owned_index_leaves_the_synthesised_tracker_empty() -> None:
    """The widest new outcome, stated rather than left to the reader.

    When every device is owned through a stored list, the exclusion leaves
    nothing, and the synthesised tracker comes out empty. That is the correct
    answer -- the group owns none of them -- and it is the outcome furthest
    from the previous behaviour, which handed it all three. It is spelled out
    here because an untested extreme is where a later reader assumes the
    branch cannot produce it.

    Nothing of this is persisted, for the reason the first case measures, so
    the empty tuple stays a view and never becomes a stored "shows nothing".
    That distinction matters under the semantics this branch of work
    introduced elsewhere: a *stored* empty list would mean the group shows no
    device, permanently.
    """

    owner = _ap4_subentry(
        "id-owner",
        "other_group",
        SUBENTRY_TYPE_TRACKER,
        ["device-1", "device-2", "device-3"],
    )
    coordinator, _entry, manager = _ap4_coordinator_with_a_foreign_group(
        "e-ap6-owned", [owner]
    )

    coordinator._refresh_subentry_index(coordinator.data, skip_repair=True)

    tracker_meta = coordinator.get_subentry_metadata(key=TRACKER_SUBENTRY_KEY)
    owner_meta = coordinator.get_subentry_metadata(key="other_group")
    assert tracker_meta is not None and owner_meta is not None
    assert owner_meta.visible_device_ids == ("device-1", "device-2", "device-3")
    assert tracker_meta.visible_device_ids == (), (
        "every device is owned elsewhere, so the synthesised group holds none"
    )
    assert tracker_meta.enabled_device_ids == (), (
        "and polls none of them either -- the enabled set is derived from the "
        "visible one, so a widened view here would widen polling too"
    )
    assert [key for key, _ids in manager.calls if key == TRACKER_SUBENTRY_KEY] == [], (
        "and above all it is not stored: a persisted empty list would mean "
        "'shows nothing' for good, on a subentry belonging to another group"
    )
