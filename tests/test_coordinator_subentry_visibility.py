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
    hass_stub = SimpleNamespace(
        loop=SimpleNamespace(call_soon_threadsafe=lambda *args, **kwargs: None),
        data={DOMAIN: {}},
    )
    coordinator = GoogleFindMyCoordinator.__new__(GoogleFindMyCoordinator)
    coordinator.hass = hass_stub  # type: ignore[assignment]
    coordinator.config_entry = entry  # type: ignore[attr-defined]
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
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

    The twin only holds those ids because it attracts the write-back: the
    remaining key-only resolver in ``config_flow.py``
    (``_BaseSubentryFlow._resolve_existing``) steers it there without consulting
    the type, while ``_accepts_device_assignment`` keeps it out of every choice
    list a user can pick from. The feature sync no longer does so, and the ids
    a release before that fix wrote are exactly the residue meant here.
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

    Reading the drop as "the assignment is undone" is the misreading this test
    exists to foreclose. That the device reappears under the tracker is pinned
    by ``test_devices_parked_on_a_mis_keyed_service_twin_are_reclaimed``. What
    that neighbour does not say is the half that makes the drop reversible:
    the ids leave the *in-memory* view only, and the stored subentry keeps
    them, so a later migration can still see what the twin held.

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
        "the drop works on the in-memory copy; the stored subentry keeps its ids"
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
