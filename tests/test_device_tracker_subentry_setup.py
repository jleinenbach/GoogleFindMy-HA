import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.googlefindmy import EntityRecoveryManager, device_tracker
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    TRACKER_SUBENTRY_KEY,
)
from tests.helpers.config_entries_stub import make_config_entry


class _ConfigEntryStub:
    """Minimal config entry stub for device tracker setup."""

    def __init__(self) -> None:
        self.entry_id = "entry-tracker"
        self.data: dict[str, Any] = {
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
        }
        self.options: dict[str, Any] = {}
        self.runtime_data: Any | None = None
        self.subentries: dict[str, Any] = {}
        self._unload_callbacks: list[Any] = []

    def async_on_unload(self, callback: Any) -> None:
        self._unload_callbacks.append(callback)


def _make_hass(loop: asyncio.AbstractEventLoop) -> HomeAssistant:
    hass = HomeAssistant()
    hass.loop = loop
    hass.data = {"core.uuid": "test-instance"}
    hass.bus = SimpleNamespace(
        async_listen=lambda *_args, **_kwargs: lambda: None,
        async_listen_once=lambda *_args, **_kwargs: lambda: None,
    )
    hass.async_create_task = lambda coro, *, name=None: loop.create_task(
        coro, name=name
    )
    hass.async_run_hass_job = lambda job, *args: getattr(
        job, "target", lambda *_: None
    )(*args)
    hass.verify_event_loop_thread = lambda *_args, **_kwargs: None
    return hass


def _make_add_entities(hass: HomeAssistant, loop: asyncio.AbstractEventLoop):
    added: list[tuple[Any, str | None]] = []
    pending: list[asyncio.Task[Any]] = []
    entity_counter = [0]  # Mutable counter to track entity IDs

    def _async_add_entities(entities: list[Any], **kwargs: Any) -> None:
        config_subentry_id = kwargs.get("config_subentry_id")
        for entity in entities:
            entity.hass = hass
            # Simulate HA platform setting entity_id before async_added_to_hass
            if not hasattr(entity, "entity_id") or entity.entity_id is None:
                entity_counter[0] += 1
                entity.entity_id = f"device_tracker.stub_{entity_counter[0]}"
            added.append((entity, config_subentry_id))
            if hasattr(entity, "async_added_to_hass"):
                pending.append(loop.create_task(entity.async_added_to_hass()))

    return _async_add_entities, added, pending


@pytest.mark.asyncio
async def test_setup_iterates_tracker_subentries(stub_coordinator_factory: Any) -> None:
    """Initial setup should build tracker entities for each known subentry."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("device_tracker",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {tracker_subentry.subentry_id: tracker_subentry}

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await device_tracker.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert {config for _, config in added} == {tracker_subentry.subentry_id}
    # Should have 2 entities per device: main tracker + last location
    assert len(added) == 2
    unique_ids = {entity.unique_id for entity, _ in added}
    assert unique_ids == {
        f"{entry.entry_id}:{tracker_subentry.subentry_id}:device-1",
        f"{entry.entry_id}:{tracker_subentry.subentry_id}:device-1:last_location",
    }


@pytest.mark.asyncio
async def test_placeholder_scopes_preserve_multiple_subentries(
    stub_coordinator_factory: Any,
) -> None:
    """Tracker scopes should not collapse when identifiers are missing."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()

    meta_a = SimpleNamespace(features=("device_tracker",), config_subentry_id=None)
    meta_b = SimpleNamespace(features=("device_tracker",), config_subentry_id=None)

    def _stable_identifier(*_args: Any, **_kwargs: Any) -> None:
        return None

    def _snapshot_for_key(
        self: Any, key: str | None, _feature: str | None = None
    ) -> list[dict[str, Any]]:
        del self
        if not key:
            return []
        return [{"id": f"device-{key}", "name": f"Device {key}"}]

    coordinator_cls = stub_coordinator_factory(
        extra_attributes={"_subentry_metadata": {"alpha": meta_a, "beta": meta_b}},
        methods={
            "stable_subentry_identifier": _stable_identifier,
            "get_subentry_snapshot": _snapshot_for_key,
        },
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await device_tracker.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    unique_ids = {entity.unique_id for entity, _ in added}
    # Should have 4 entities: 2 main trackers + 2 last location trackers
    assert len(unique_ids) >= 4
    assert unique_ids.issuperset(
        {
            f"{entry.entry_id}:{TRACKER_SUBENTRY_KEY}:device-alpha",
            f"{entry.entry_id}:{TRACKER_SUBENTRY_KEY}:device-alpha:last_location",
            f"{entry.entry_id}:{TRACKER_SUBENTRY_KEY}:device-beta",
            f"{entry.entry_id}:{TRACKER_SUBENTRY_KEY}:device-beta:last_location",
        }
    )


@pytest.mark.asyncio
async def test_dispatcher_adds_new_tracker_subentries(
    stub_coordinator_factory: Any,
) -> None:
    """Dispatcher callbacks should attach trackers for new subentries without duplicates."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("device_tracker",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {tracker_subentry.subentry_id: tracker_subentry}

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await device_tracker.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    new_subentry = ConfigSubentry(
        data={"group_key": "secondary", "features": ("device_tracker",)},
        subentry_type="tracker",
        title="Secondary",
        subentry_id="secondary-subentry",
    )
    entry.subentries[new_subentry.subentry_id] = new_subentry

    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"
    async_dispatcher_send(hass, signal, new_subentry.subentry_id)
    await asyncio.gather(*pending)

    async_dispatcher_send(hass, signal, new_subentry.subentry_id)
    await asyncio.gather(*pending)

    configs = [config for _, config in added]
    # Each subentry creates 2 entities (main tracker + last location)
    assert configs.count(tracker_subentry.subentry_id) == 2
    assert configs.count(new_subentry.subentry_id) == 2
    # 2 devices x 2 entities each = 4 unique IDs
    assert len({entity.unique_id for entity, _ in added}) == 4
    assert entry._unload_callbacks, "dispatcher listener should be cleaned up on unload"


@pytest.mark.asyncio
async def test_dispatcher_deduplicates_existing_subentry_signals(
    stub_coordinator_factory: Any,
) -> None:
    """Repeated dispatcher signals should not rebuild tracker entities."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("device_tracker",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {tracker_subentry.subentry_id: tracker_subentry}

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await device_tracker.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    # Initial count should be 2 (main tracker + last location)
    initial_count = len(added)
    assert initial_count == 2
    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"

    async_dispatcher_send(hass, signal, tracker_subentry.subentry_id)
    await asyncio.gather(*pending)

    async_dispatcher_send(hass, signal, tracker_subentry.subentry_id)
    await asyncio.gather(*pending)

    # Should still be 2 entities (no duplicates from repeated signals)
    assert len(added) == initial_count == 2
    assert {config for _, config in added} == {tracker_subentry.subentry_id}


@pytest.mark.asyncio
async def test_device_tracker_ignores_non_tracker_subentry(
    stub_coordinator_factory: Any,
) -> None:
    """Dispatcher should ignore service or unknown subentry types."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": "service", "features": ("service",)},
        subentry_type="service",
        title="Service",
        subentry_id="service-subentry",
    )
    entry.subentries = {service_subentry.subentry_id: service_subentry}

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await device_tracker.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"
    async_dispatcher_send(hass, signal, service_subentry)
    await asyncio.gather(*pending)

    assert added == []


# ---------------------------------------------------------------------------
# PB-2s (W5t): the recovery covers a tracker scope that arrives by signal
# ---------------------------------------------------------------------------

_PB2S_SERVICE_ID = "service-subentry"
_PB2S_TRACKER_ID = "tracker-subentry"
_PB2S_TRACKER_KEY = "tracker"


def _pb2s_make_entry(subentries: dict[str, Any]) -> Any:
    """Config entry from the canonical factory (tests/AGENTS.md).

    ``subentries`` is an HA attribute outside the factory signature, so it is
    passed via ``**extra`` as the read-only mapping view the core hands out;
    the mutable store behind it is kept on ``_subentry_store`` so a test can
    register a subentry the way the core does before it signals.
    ``async_on_unload`` is attached per instance as the factory leaves methods
    out on purpose.
    """
    store: dict[str, Any] = dict(subentries)
    entry = make_config_entry(
        entry_id="entry-tracker",
        data={
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
        },
        subentries=MappingProxyType(store),
    )
    entry._subentry_store = store
    unload_callbacks: list[Any] = []
    entry._unload_callbacks = unload_callbacks
    entry.async_on_unload = unload_callbacks.append
    return entry


def _pb2s_registry(entry_id: str, present: set[str]) -> SimpleNamespace:
    """Entity registry double holding ``present`` device tracker unique ids."""
    rows = {
        f"device_tracker.present_{index}": SimpleNamespace(
            domain="device_tracker",
            platform=DOMAIN,
            unique_id=unique_id,
            config_entry_id=entry_id,
        )
        for index, unique_id in enumerate(sorted(present))
    }
    return SimpleNamespace(
        entities=rows,
        async_entries_for_config_entry=lambda config_entry_id: [
            row for row in rows.values() if row.config_entry_id == config_entry_id
        ],
    )


@pytest.mark.asyncio
async def test_recovery_expected_ids_follow_the_tracker_scope_from_the_signal(
    stub_coordinator_factory: Any,
) -> None:
    """W5t: the tracker subentry arrives by signal after a service-only setup.

    The recovery must then expect the main and the last-location tracker of
    each visible, enabled device of that scope, and none of a hidden device.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    service_subentry = ConfigSubentry(
        data={"group_key": "service", "features": ("sensor",)},
        subentry_type="service",
        title="Service",
        subentry_id=_PB2S_SERVICE_ID,
    )
    tracker_subentry = ConfigSubentry(
        data={"group_key": _PB2S_TRACKER_KEY, "features": ("device_tracker",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id=_PB2S_TRACKER_ID,
    )
    entry = _pb2s_make_entry({_PB2S_SERVICE_ID: service_subentry})

    visible_ids = ["visible-device"]

    def _is_visible(self: Any, subentry_key: str, device_id: str) -> bool:  # noqa: ANN001
        return device_id in visible_ids

    def _metadata(
        self: Any, *, key: str | None = None, feature: str | None = None
    ) -> Any:  # noqa: ANN001
        # The tracker recovery reads visibility and enablement from the
        # subentry metadata of the scope's key.
        return SimpleNamespace(
            key=key,
            config_subentry_id=_PB2S_TRACKER_ID,
            visible_device_ids=list(visible_ids),
            enabled_device_ids=list(visible_ids),
        )

    coordinator_cls = stub_coordinator_factory(
        data=[
            {"id": "visible-device", "name": "Visible"},
            {"id": "hidden-device", "name": "Hidden"},
        ],
        methods={
            "is_device_visible_in_subentry": _is_visible,
            "get_subentry_metadata": _metadata,
        },
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry

    manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, entity_recovery_manager=manager
    )

    add_entities, added, pending = _make_add_entities(hass, loop)

    await device_tracker.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert added == [], "a service-only setup adds no tracker"
    assert manager.expected_unique_ids_for_platform("device_tracker") == set()

    # The core registers the subentry, then the integration signals it
    # (``__init__.py``, ``_async_setup_new_subentries``).
    entry._subentry_store[_PB2S_TRACKER_ID] = tracker_subentry
    async_dispatcher_send(
        hass, f"{DOMAIN}_subentry_setup_{entry.entry_id}", tracker_subentry
    )
    await asyncio.gather(*pending)

    main_uid = f"{entry.entry_id}:{_PB2S_TRACKER_ID}:visible-device"
    last_uid = f"{entry.entry_id}:{_PB2S_TRACKER_ID}:visible-device:last_location"

    # Setup observation: the platform exposes the visible device's trackers.
    # (The setup builds every device of the coordinator snapshot; in
    # production the snapshot is already visibility-filtered, so the hidden
    # device is not asserted on here but on the recovery, which reads the
    # subentry metadata itself.)
    added_ids = {entity.unique_id for entity, _ in added}
    assert {main_uid, last_uid} <= added_ids

    # The recovery expects exactly the visible device's trackers; the hidden
    # device (in the snapshot, not in the metadata) contributes nothing.
    assert manager.expected_unique_ids_for_platform("device_tracker") == {
        main_uid,
        last_uid,
    }

    # Reconstruction: the main tracker is present, the last-location tracker
    # is rebuilt and handed to the recorded add_entities with the scope's id.
    hass.data["entity_registry"] = _pb2s_registry(entry.entry_id, {main_uid})
    before = len(added)

    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)

    recovered = {(entity.unique_id, config) for entity, config in added[before:]}
    assert recovered == {(last_uid, _PB2S_TRACKER_ID)}
