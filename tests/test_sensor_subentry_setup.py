
import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.googlefindmy import sensor
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
)


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
        async_listen=lambda *_args, **_kwargs: (lambda: None),
        async_listen_once=lambda *_args, **_kwargs: (lambda: None),
    )
    hass.async_create_task = lambda coro, *, name=None: loop.create_task(coro, name=name)
    hass.async_run_hass_job = lambda job, *args: getattr(job, "target", lambda *_: None)(
        *args
    )
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
    assert {
        entity.unique_id for entity, _ in added
    } == {
        f"{DOMAIN}_{entry.entry_id}_{service_subentry.subentry_id}_background_updates",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_last_seen",
    }


@pytest.mark.asyncio
async def test_dispatcher_adds_new_tracker_subentries(stub_coordinator_factory: Any) -> None:
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

    async_dispatcher_send(
        hass, f"googlefindmy_subentry_setup_{entry.entry_id}", new_subentry.subentry_id
    )
    await asyncio.gather(*pending)

    async_dispatcher_send(
        hass, f"googlefindmy_subentry_setup_{entry.entry_id}", new_subentry.subentry_id
    )
    await asyncio.gather(*pending)

    configs = [config for _, config in added]
    assert configs.count(tracker_subentry.subentry_id) == 1
    assert configs.count(new_subentry.subentry_id) == 1
    assert configs.count(service_subentry.subentry_id) == 1
    assert len({entity.unique_id for entity, _ in added}) == 3
    assert entry._unload_callbacks, "dispatcher listener should be cleaned up on unload"
