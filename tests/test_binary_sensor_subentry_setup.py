import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.googlefindmy import EntityRecoveryManager, binary_sensor
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
)


class _ConfigEntryStub:
    """Minimal config entry stub for binary sensor setup."""

    def __init__(self) -> None:
        self.entry_id = "entry-binary"
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
        async_listen=lambda *_args, **_kwargs: (lambda: None),
        async_listen_once=lambda *_args, **_kwargs: (lambda: None),
    )
    hass.async_create_task = lambda coro, *, name=None: loop.create_task(coro, name=name)
    return hass


def _make_add_entities(hass: HomeAssistant, loop: asyncio.AbstractEventLoop):
    added: list[tuple[Any, str | None]] = []
    pending: list[asyncio.Task[Any]] = []

    def _async_add_entities(entities: list[Any], **kwargs: Any) -> None:
        config_subentry_id = kwargs.get("config_subentry_id")
        for entity in entities:
            entity.hass = hass
            added.append((entity, config_subentry_id))
            if hasattr(entity, "async_added_to_hass"):
                pending.append(loop.create_task(entity.async_added_to_hass()))

    return _async_add_entities, added, pending


@pytest.mark.asyncio
async def test_setup_iterates_service_subentries(stub_coordinator_factory: Any) -> None:
    """Initial setup should build entities for each known service subentry."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("binary_sensor",)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
        subentry_id="service-subentry",
    )
    entry.subentries = {service_subentry.subentry_id: service_subentry}

    coordinator_cls = stub_coordinator_factory(
        metadata_for_feature={"binary_sensor": service_subentry.subentry_id}
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await binary_sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert {config for _, config in added} == {service_subentry.subentry_id}
    assert {
        entity.unique_id for entity, _ in added
    } == {
        f"{entry.entry_id}:{service_subentry.subentry_id}:polling",
        f"{entry.entry_id}:{service_subentry.subentry_id}:auth_status",
        f"{entry.entry_id}:{service_subentry.subentry_id}:connectivity",
    }


@pytest.mark.asyncio
async def test_dispatcher_adds_new_service_subentries(stub_coordinator_factory: Any) -> None:
    """Dispatcher callbacks should attach entities for newly added subentries."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("binary_sensor",)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        subentry_id="service-subentry",
    )
    entry.subentries = {service_subentry.subentry_id: service_subentry}

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await binary_sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    new_subentry = ConfigSubentry(
        data={"group_key": "secondary", "features": ("binary_sensor",)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
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
    assert configs.count(service_subentry.subentry_id) == 3
    assert configs.count(new_subentry.subentry_id) == 3
    assert len({entity.unique_id for entity, _ in added}) == 6
    assert entry._unload_callbacks, "dispatcher listener should be cleaned up on unload"


@pytest.mark.asyncio
async def test_dispatcher_deduplicates_existing_subentry_signals(
    stub_coordinator_factory: Any,
) -> None:
    """Repeated dispatcher signals should not build entities multiple times."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("binary_sensor",)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        unique_id=f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}",
        subentry_id="service-subentry",
    )
    entry.subentries = {service_subentry.subentry_id: service_subentry}

    coordinator_cls = stub_coordinator_factory(
        metadata_for_feature={"binary_sensor": service_subentry.subentry_id}
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_entities, added, pending = _make_add_entities(hass, loop)

    await binary_sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    initial_count = len(added)
    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"

    async_dispatcher_send(hass, signal, service_subentry.subentry_id)
    await asyncio.gather(*pending)

    async_dispatcher_send(hass, signal, service_subentry.subentry_id)
    await asyncio.gather(*pending)

    assert len(added) == initial_count == 3
    assert {config for _, config in added} == {service_subentry.subentry_id}


@pytest.mark.asyncio
async def test_binary_sensor_skips_tracker_subentry(
    stub_coordinator_factory: Any,
) -> None:
    """Service-only binary sensor platform ignores tracker subentries."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("binary_sensor",)},
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

    await binary_sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"
    async_dispatcher_send(hass, signal, tracker_subentry)
    await asyncio.gather(*pending)

    assert added == []


@pytest.mark.asyncio
async def test_recovery_registers_connectivity_sensor(
    stub_coordinator_factory: Any,
) -> None:
    """Recovery manager should include connectivity sensors for service subentries."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    service_subentry = ConfigSubentry(
        data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("binary_sensor",)},
        subentry_type=SUBENTRY_TYPE_SERVICE,
        title="Service",
        subentry_id="service-subentry",
    )
    entry.subentries = {service_subentry.subentry_id: service_subentry}

    coordinator_cls = stub_coordinator_factory()
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    recovery_manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, entity_recovery_manager=recovery_manager
    )

    add_entities, added, pending = _make_add_entities(hass, loop)

    await binary_sensor.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    registration = recovery_manager._platforms.get(str(Platform.BINARY_SENSOR))
    assert registration is not None

    expected = {
        f"{entry.entry_id}:{service_subentry.subentry_id}:polling",
        f"{entry.entry_id}:{service_subentry.subentry_id}:auth_status",
        f"{entry.entry_id}:{service_subentry.subentry_id}:connectivity",
    }

    assert registration.expected_unique_ids() == expected

    recovered = registration.entity_factory(set(expected))
    assert {entity.unique_id for entity in recovered} == expected
