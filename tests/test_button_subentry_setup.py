# tests/test_button_subentry_setup.py
"""Regression tests for button setup across tracker subentries."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.googlefindmy import EntityRecoveryManager, button
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
)


class _ConfigEntryStub:
    """Minimal config entry stub for button setup."""

    def __init__(self) -> None:
        self.entry_id = "entry-button"
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
            added.append((entity, config_subentry_id))
            if hasattr(entity, "async_added_to_hass"):
                pending.append(loop.create_task(entity.async_added_to_hass()))

    return _async_add_entities, added, pending


@pytest.mark.asyncio
async def test_setup_iterates_tracker_subentries(stub_coordinator_factory: Any) -> None:
    """Initial setup should build button entities for each known subentry."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("button",)},
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

    await button.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert {config for _, config in added} == {tracker_subentry.subentry_id}
    assert {
        entity.unique_id for entity, _ in added
    } == {
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_play_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_stop_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_locate_device",
    }


@pytest.mark.asyncio
async def test_dispatcher_adds_new_tracker_subentries(stub_coordinator_factory: Any) -> None:
    """Dispatcher callbacks should attach buttons for newly added subentries."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("button",)},
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

    await button.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    new_subentry = ConfigSubentry(
        data={"group_key": "secondary", "features": ("button",)},
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
    assert configs.count(tracker_subentry.subentry_id) == 3
    assert configs.count(new_subentry.subentry_id) == 3
    assert len({entity.unique_id for entity, _ in added}) == 6
    assert entry._unload_callbacks, "dispatcher listener should be cleaned up on unload"


@pytest.mark.asyncio
async def test_hidden_devices_skipped_by_visibility(stub_coordinator_factory: Any) -> None:
    """Visibility metadata should prevent hidden trackers from exposing buttons."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("button",)},
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

    await button.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    visible_unique_ids = {
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_play_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_stop_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_locate_device",
    }

    assert {entity.unique_id for entity, _ in added} == visible_unique_ids
    assert all(config == tracker_subentry.subentry_id for _, config in added)


@pytest.mark.asyncio
async def test_recovery_skips_hidden_buttons(stub_coordinator_factory: Any) -> None:
    """Recovery manager should ignore hidden tracker buttons."""

    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    entry = _ConfigEntryStub()
    tracker_subentry = ConfigSubentry(
        data={"group_key": "tracker", "features": ("button",)},
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

    await button.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    registration = recovery_manager._platforms.get(str(Platform.BUTTON))
    assert registration is not None

    visible_unique_ids = {
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_play_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_stop_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_visible-device_locate_device",
    }

    hidden_unique_ids = {
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_hidden-device_play_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_hidden-device_stop_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_hidden-device_locate_device",
    }

    assert registration.expected_unique_ids() == visible_unique_ids

    missing = visible_unique_ids | hidden_unique_ids
    recovered = registration.entity_factory(missing)

    assert {entity.unique_id for entity in recovered} == visible_unique_ids
    assert all(config == tracker_subentry.subentry_id for _, config in added)
