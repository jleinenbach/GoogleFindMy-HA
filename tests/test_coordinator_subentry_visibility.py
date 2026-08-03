# tests/test_coordinator_subentry_visibility.py
"""Coordinator subentry visibility regression tests."""

from __future__ import annotations

from types import MappingProxyType, SimpleNamespace

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

    The runtime manager joined as a third reader with
    ``PLAN_GFMY_ALIAS_TYPE_AXIS``; it is asserted here for the same reason as
    the other two, because a private copy in ``__init__.py`` would drift
    exactly as silently.
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
# AP4 -- the reading side's tracker axis (PLAN_GFMY_ALIAS_TYPE_AXIS).
#
# Two changes are pinned here, and they are one commit on purpose: step 3 gives
# the tracker key the same rank the service key already had, step 1 folds a
# ``tracker`` parked on the service key onto the tracker key. Step 1 alone
# would let the parked subentry compete for ``core_tracking`` while ``metadata``
# was still written without a rank, which trades one order-dependence for
# another instead of removing it.
# ---------------------------------------------------------------------------


def _ap4_subentry(
    subentry_id: str,
    stored_key: str | None,
    subentry_type: str,
    visible: list[str] | None = None,
) -> ConfigSubentry:
    """Build one subentry with a freely chosen (key, type) pair.

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
    if visible:
        payload["visible_device_ids"] = list(visible)
    return ConfigSubentry(
        data=MappingProxyType(payload),
        subentry_type=subentry_type,
        title=f"{subentry_type}:{stored_key}",
        unique_id=f"uid-{subentry_id}",
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
