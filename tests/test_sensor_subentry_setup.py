import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.googlefindmy import EntityRecoveryManager, sensor
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
)
from tests.helpers.config_entries_stub import make_config_entry


class _ConfigEntryStub:
    """Minimal config entry stub for sensor setup."""

    def __init__(self) -> None:
        self.entry_id = "entry-sensor"
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

    def _async_add_entities(entities: list[Any], **kwargs: Any) -> None:
        config_subentry_id = kwargs.get("config_subentry_id")
        for entity in entities:
            entity.hass = hass
            if not getattr(entity, "entity_id", None):
                entity.entity_id = f"sensor.{entity.unique_id}"
            added.append((entity, config_subentry_id))
            if hasattr(entity, "async_added_to_hass"):
                pending.append(loop.create_task(entity.async_added_to_hass()))

    return _async_add_entities, added, pending


@pytest.mark.asyncio
async def test_setup_iterates_sensor_subentries(stub_coordinator_factory: Any) -> None:
    """Initial setup should build sensors for each known subentry."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": "service", "features": ("sensor",)},
        subentry_type="service",
        title="Service",
        subentry_id="service-subentry",
    )
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("sensor",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {
        service_subentry.subentry_id: service_subentry,
        tracker_subentry.subentry_id: tracker_subentry,
    }

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    configs = {config for _, config in added}
    assert configs == {service_subentry.subentry_id, tracker_subentry.subentry_id}
    assert {entity.unique_id for entity, _ in added} == {
        f"{DOMAIN}_{entry.entry_id}_{service_subentry.subentry_id}_semantic_labels",
        f"{DOMAIN}_{entry.entry_id}_{service_subentry.subentry_id}_encryption_key_status",
        f"{DOMAIN}_{entry.entry_id}_{service_subentry.subentry_id}_background_updates",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_last_seen",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_plus_code",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_location_status",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_coarse_fix",
    }


@pytest.mark.asyncio
async def test_dispatcher_adds_new_tracker_subentries(
    stub_coordinator_factory: Any,
) -> None:
    """Dispatcher callbacks should attach sensors for newly added subentries."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": "service", "features": ("sensor",)},
        subentry_type="service",
        title="Service",
        subentry_id="service-subentry",
    )
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("sensor",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {
        service_subentry.subentry_id: service_subentry,
        tracker_subentry.subentry_id: tracker_subentry,
    }

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    new_subentry = ConfigSubentry(
        data={"group_key": "secondary", "features": ("sensor",)},
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
    # Each tracker device emits four sensors: last_seen, plus_code,
    # location_status and coarse_fix.
    assert configs.count(tracker_subentry.subentry_id) == 4
    assert configs.count(new_subentry.subentry_id) == 4
    # Service scope now emits three diagnostic sensors: semantic_labels,
    # encryption_key_status (AP-10) and the background_updates stat.
    assert configs.count(service_subentry.subentry_id) == 3
    assert len({entity.unique_id for entity, _ in added}) == 11
    assert entry._unload_callbacks, "dispatcher listener should be cleaned up on unload"


@pytest.mark.asyncio
async def test_dispatcher_deduplicates_existing_subentry_signals(
    stub_coordinator_factory: Any,
) -> None:
    """Repeated dispatcher signals should not rebuild sensors for the same subentry."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": "service", "features": ("sensor",)},
        subentry_type="service",
        title="Service",
        subentry_id="service-subentry",
    )
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("sensor",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {
        service_subentry.subentry_id: service_subentry,
        tracker_subentry.subentry_id: tracker_subentry,
    }

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    initial_count = len(added)
    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"

    async_dispatcher_send(hass, signal, tracker_subentry.subentry_id)
    async_dispatcher_send(hass, signal, service_subentry.subentry_id)
    await asyncio.gather(*pending)

    async_dispatcher_send(hass, signal, tracker_subentry.subentry_id)
    async_dispatcher_send(hass, signal, service_subentry.subentry_id)
    await asyncio.gather(*pending)

    # Seven entities: service semantic_labels + encryption_key_status (AP-10) +
    # background_updates stat, plus the tracker last_seen, plus_code,
    # location_status and coarse_fix sensors.
    assert len(added) == initial_count == 7
    assert {config for _, config in added} == {
        service_subentry.subentry_id,
        tracker_subentry.subentry_id,
    }


@pytest.mark.asyncio
async def test_hidden_tracker_sensors_skip_invisible_devices(
    stub_coordinator_factory: Any,
) -> None:
    """Visibility metadata should hide tracker sensors for invisible devices."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("sensor",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {tracker_subentry.subentry_id: tracker_subentry}

    visible_ids = {"visible-device"}

    def _is_visible(self: Any, subentry_key: str, device_id: str) -> bool:  # noqa: ANN001
        assert subentry_key in {tracker_subentry.subentry_id, "tracker"}
        return device_id in visible_ids

    coordinator_cls = stub_coordinator_factory(
        data=[
            {"id": "visible-device", "name": "Visible"},
            {"id": "hidden-device", "name": "Hidden"},
        ],
        methods={"is_device_visible_in_subentry": _is_visible},
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert {entity.unique_id for entity, _ in added} == {
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_last_seen",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_plus_code",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_location_status",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_coarse_fix",
        f"{DOMAIN}_{entry.entry_id}_service_background_updates",
        f"{DOMAIN}_{entry.entry_id}_service_semantic_labels",
        f"{DOMAIN}_{entry.entry_id}_service_encryption_key_status",
    }
    config_by_unique_id = {entity.unique_id: config for entity, config in added}
    assert (
        config_by_unique_id[
            f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_last_seen"
        ]
        == tracker_subentry.subentry_id
    )
    assert config_by_unique_id[
        f"{DOMAIN}_{entry.entry_id}_service_background_updates"
    ] in {None, "service"}


@pytest.mark.asyncio
async def test_recovery_skips_hidden_tracker_sensors(
    stub_coordinator_factory: Any,
) -> None:
    """Sensor recovery should ignore hidden tracker devices."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("sensor",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id="tracker-subentry",
    )
    entry.subentries = {tracker_subentry.subentry_id: tracker_subentry}

    visible_ids = {"visible-device"}

    def _is_visible(self: Any, subentry_key: str, device_id: str) -> bool:  # noqa: ANN001
        assert subentry_key in {tracker_subentry.subentry_id, "tracker"}
        return device_id in visible_ids

    coordinator_cls = stub_coordinator_factory(
        data=[
            {"id": "visible-device", "name": "Visible"},
            {"id": "hidden-device", "name": "Hidden"},
        ],
        methods={"is_device_visible_in_subentry": _is_visible},
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry

    recovery_manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, entity_recovery_manager=recovery_manager
    )

    add_entities, added, pending = _make_add_entities(hass, loop)

    await sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    registration = recovery_manager._platforms.get(str(Platform.SENSOR))
    assert registration is not None

    visible_unique_id = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_last_seen"
    visible_plus_code_unique_id = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_plus_code"
    hidden_unique_id = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_hidden-device_last_seen"
    hidden_plus_code_unique_id = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_hidden-device_plus_code"
    visible_status_unique_id = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_location_status"
    hidden_status_unique_id = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_hidden-device_location_status"
    visible_coarse_unique_id = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_coarse_fix"
    hidden_coarse_unique_id = f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_hidden-device_coarse_fix"
    service_unique_id = f"{DOMAIN}_{entry.entry_id}_service_background_updates"

    assert registration.expected_unique_ids() == {
        visible_unique_id,
        visible_plus_code_unique_id,
        visible_status_unique_id,
        visible_coarse_unique_id,
        service_unique_id,
    }

    missing = {
        visible_unique_id,
        visible_plus_code_unique_id,
        hidden_unique_id,
        hidden_plus_code_unique_id,
        visible_status_unique_id,
        hidden_status_unique_id,
        visible_coarse_unique_id,
        hidden_coarse_unique_id,
        service_unique_id,
    }
    recovered = registration.entity_factory(missing)

    assert {entity.unique_id for entity in recovered} == {
        visible_unique_id,
        visible_plus_code_unique_id,
        visible_status_unique_id,
        visible_coarse_unique_id,
        service_unique_id,
    }

    config_by_unique_id = {entity.unique_id: config for entity, config in added}
    assert config_by_unique_id.get(visible_unique_id) == tracker_subentry.subentry_id
    if service_unique_id in config_by_unique_id:
        assert config_by_unique_id[service_unique_id] in {None, "service"}


@pytest.mark.asyncio
async def test_sensor_setup_ignores_mismatched_subentry_type(
    stub_coordinator_factory: Any,
) -> None:
    """Tracker sensors should not attach to service-only subentries and vice versa."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": "service", "features": ("sensor",)},
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

    await sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"
    async_dispatcher_send(hass, signal, service_subentry)
    await asyncio.gather(*pending)

    assert added
    assert all(config == service_subentry.subentry_id for _, config in added)
    assert not any(
        "last_seen" in getattr(entity, "unique_id", "") for entity, _ in added
    )


# ---------------------------------------------------------------------------
# The recovery covers the service and the tracker scope on every way
# ---------------------------------------------------------------------------

_RECOVERY_SERVICE_ID = "service-subentry"
_RECOVERY_TRACKER_ID = "tracker-subentry"
_RECOVERY_TRACKER_KEY = "tracker"
_RECOVERY_STAT_KEYS = ("background_updates", "polled_updates", "timeouts")
_RECOVERY_TRACKER_KINDS = ("last_seen", "plus_code", "location_status", "coarse_fix")


def _recovery_make_entry(subentries: dict[str, Any] | None) -> Any:
    """Config entry from the canonical factory (tests/AGENTS.md).

    ``subentries`` is an HA attribute outside the factory signature, so it is
    passed via ``**extra`` as the read-only mapping view the core hands out;
    the mutable store behind it is kept on ``_subentry_store`` so a test can
    register a subentry the way the core does before it signals. ``None``
    leaves the attribute out entirely (W4, a pure test way). ``async_on_unload``
    is attached per instance as the factory leaves methods out on purpose.
    """
    store: dict[str, Any] = dict(subentries or {})
    extra: dict[str, Any] = {}
    if subentries is not None:
        extra["subentries"] = MappingProxyType(store)
    entry = make_config_entry(
        entry_id="entry-sensor",
        data={
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
        },
        **extra,
    )
    entry._subentry_store = store
    unload_callbacks: list[Any] = []
    entry._unload_callbacks = unload_callbacks
    entry.async_on_unload = unload_callbacks.append
    return entry


def _recovery_service_subentry() -> ConfigSubentry:
    return ConfigSubentry(
        data={"group_key": "service", "features": ("sensor",)},
        subentry_type="service",
        title="Service",
        subentry_id=_RECOVERY_SERVICE_ID,
    )


def _recovery_tracker_subentry() -> ConfigSubentry:
    return ConfigSubentry(
        data={"group_key": _RECOVERY_TRACKER_KEY, "features": ("sensor",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id=_RECOVERY_TRACKER_ID,
    )


def _recovery_registry(entry_id: str, present: set[str]) -> SimpleNamespace:
    """Entity registry double holding ``present`` sensor unique ids."""
    rows = {
        f"sensor.present_{index}": SimpleNamespace(
            domain="sensor",
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


def _recovery_stat_ids(unique_ids: set[str]) -> set[str]:
    """Return the statistics sensor ids among ``unique_ids``."""
    return {
        unique_id
        for unique_id in unique_ids
        if any(unique_id.endswith(f"_{key}") for key in _RECOVERY_STAT_KEYS)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("way", "identifier"),
    [
        ("W1", _RECOVERY_SERVICE_ID),
        ("W2", _RECOVERY_SERVICE_ID),
        ("W3", "service"),
        ("W4", "service"),
        ("W5", _RECOVERY_SERVICE_ID),
    ],
    ids=["W1", "W2", "W3", "W4", "W5"],
)
async def test_recovery_expected_ids_follow_the_service_scope(
    stub_coordinator_factory: Any, way: str, identifier: str
) -> None:
    """On every way a service scope arises, the recovery expects its stat ids.

    W1: the subentry manager lists the service subentry.
    W2: ``entry.subentries`` lists it, no manager.
    W3: ``entry.subentries`` is empty, the id arrives as ``config_subentry_id``;
        the scope then carries the default identifier ``service``.
    W4: no ``subentries`` attribute at all (not a mapping), id as in W3; a
        pure test way, no core produces it.
    W5: only a tracker subentry exists at setup; the service subentry is
        registered and signalled afterwards, as ``_async_setup_new_subentries``
        does for a subentry created after the platform loaded.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    service_subentry = _recovery_service_subentry()
    config_subentry_id: str | None = None
    managed: dict[str, Any] | None = None
    if way == "W1":
        entry = _recovery_make_entry({_RECOVERY_SERVICE_ID: service_subentry})
        managed = {_RECOVERY_SERVICE_ID: service_subentry}
    elif way == "W2":
        entry = _recovery_make_entry({_RECOVERY_SERVICE_ID: service_subentry})
    elif way == "W3":
        entry = _recovery_make_entry({})
        config_subentry_id = _RECOVERY_SERVICE_ID
    elif way == "W4":
        entry = _recovery_make_entry(None)
        config_subentry_id = _RECOVERY_SERVICE_ID
    else:
        entry = _recovery_make_entry(
            {_RECOVERY_TRACKER_ID: _recovery_tracker_subentry()}
        )

    coordinator_cls = stub_coordinator_factory(
        stats={key: 1 for key in _RECOVERY_STAT_KEYS}
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry

    manager = EntityRecoveryManager(hass, entry, coordinator)
    runtime = SimpleNamespace(coordinator=coordinator, entity_recovery_manager=manager)
    if managed is not None:
        runtime.subentry_manager = SimpleNamespace(managed_subentries=managed)
    entry.runtime_data = runtime

    add_entities, added, pending = _make_add_entities(hass, loop)

    await sensor.async_setup_entry(
        hass, entry, add_entities, config_subentry_id=config_subentry_id
    )
    await asyncio.gather(*pending)

    scope_ids = {
        f"{DOMAIN}_{entry.entry_id}_{identifier}_{key}" for key in _RECOVERY_STAT_KEYS
    }

    if way == "W5":
        assert not scope_ids & {entity.unique_id for entity, _ in added}, (
            "the service subentry's scope must not exist before the signal"
        )
        # The core registers the subentry, then the integration signals it
        # (``__init__.py``, ``_async_setup_new_subentries``).
        entry._subentry_store[_RECOVERY_SERVICE_ID] = service_subentry
        async_dispatcher_send(
            hass, f"{DOMAIN}_subentry_setup_{entry.entry_id}", service_subentry
        )
        await asyncio.gather(*pending)

    # Setup observation: the platform exposes the scope's stat sensors.
    added_stats = _recovery_stat_ids({entity.unique_id for entity, _ in added})
    assert scope_ids <= added_stats

    # The recovery expects the scope's stat ids, and nothing the setup did
    # not create. (On W5 the setup also synthesised a ``service`` scope before
    # the subentry existed; its ids may stay in the expected set.)
    expected_stats = _recovery_stat_ids(
        manager.expected_unique_ids_for_platform("sensor")
    )
    assert scope_ids <= expected_stats
    assert expected_stats <= added_stats

    # Reconstruction: one stat sensor of the scope is present, the two others
    # are rebuilt and handed to the recorded add_entities with the scope's
    # config subentry id.
    present = f"{DOMAIN}_{entry.entry_id}_{identifier}_background_updates"
    hass.data["entity_registry"] = _recovery_registry(entry.entry_id, {present})
    before = len(added)

    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)

    recovered = {(entity.unique_id, config) for entity, config in added[before:]}
    assert {
        (unique_id, _RECOVERY_SERVICE_ID) for unique_id in scope_ids - {present}
    } <= recovered
    assert present not in {unique_id for unique_id, _ in recovered}


@pytest.mark.asyncio
async def test_recovery_expected_ids_follow_the_tracker_scope_from_the_signal(
    stub_coordinator_factory: Any,
) -> None:
    """W5t: the tracker subentry arrives by signal after a service-only setup.

    The recovery must then expect the four sensors of each visible device of
    that scope, and none of a hidden device.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _recovery_make_entry({_RECOVERY_SERVICE_ID: _recovery_service_subentry()})
    tracker_subentry = _recovery_tracker_subentry()

    visible_ids = {"visible-device"}

    def _is_visible(self: Any, subentry_key: str, device_id: str) -> bool:  # noqa: ANN001
        if subentry_key in {_RECOVERY_TRACKER_KEY, _RECOVERY_TRACKER_ID}:
            return device_id in visible_ids
        return True

    coordinator_cls = stub_coordinator_factory(
        data=[
            {"id": "visible-device", "name": "Visible"},
            {"id": "hidden-device", "name": "Hidden"},
        ],
        methods={"is_device_visible_in_subentry": _is_visible},
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry

    manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, entity_recovery_manager=manager
    )

    add_entities, added, pending = _make_add_entities(hass, loop)

    await sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    def _tracker_uid(device_id: str, kind: str) -> str:
        return f"{DOMAIN}_{entry.entry_id}_{_RECOVERY_TRACKER_ID}_{device_id}_{kind}"

    tracker_ids = {
        _tracker_uid("visible-device", kind) for kind in _RECOVERY_TRACKER_KINDS
    }
    hidden_ids = {
        _tracker_uid("hidden-device", kind) for kind in _RECOVERY_TRACKER_KINDS
    }

    assert not tracker_ids & {entity.unique_id for entity, _ in added}, (
        "no tracker scope may exist before the signal"
    )
    # The core registers the subentry, then the integration signals it.
    entry._subentry_store[_RECOVERY_TRACKER_ID] = tracker_subentry
    async_dispatcher_send(
        hass, f"{DOMAIN}_subentry_setup_{entry.entry_id}", tracker_subentry
    )
    await asyncio.gather(*pending)

    # Setup observation: the platform exposes the visible device's sensors.
    added_ids = {entity.unique_id for entity, _ in added}
    assert tracker_ids <= added_ids
    assert not hidden_ids & added_ids

    # The recovery expects the scope's visible sensors and none of the hidden.
    expected = manager.expected_unique_ids_for_platform("sensor")
    assert tracker_ids <= expected
    assert not hidden_ids & expected

    # Reconstruction: last_seen is present, the three others are rebuilt and
    # handed to the recorded add_entities with the scope's subentry id.
    present = _tracker_uid("visible-device", "last_seen")
    hass.data["entity_registry"] = _recovery_registry(entry.entry_id, {present})
    before = len(added)

    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)

    recovered = {(entity.unique_id, config) for entity, config in added[before:]}
    assert {
        (unique_id, _RECOVERY_TRACKER_ID) for unique_id in tracker_ids - {present}
    } <= recovered
    assert not hidden_ids & {unique_id for unique_id, _ in recovered}
