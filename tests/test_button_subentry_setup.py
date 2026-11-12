# tests/test_button_subentry_setup.py
"""Regression tests for button setup across tracker subentries."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
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
from tests.helpers.config_entries_stub import make_config_entry


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
    assert {entity.unique_id for entity, _ in added} == {
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_play_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_stop_sound",
        f"{DOMAIN}_{entry.entry_id}_{tracker_subentry.subentry_id}_device-1_locate_device",
    }


@pytest.mark.asyncio
async def test_dispatcher_adds_new_tracker_subentries(
    stub_coordinator_factory: Any,
) -> None:
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

    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"

    async_dispatcher_send(hass, signal, new_subentry.subentry_id)
    await asyncio.gather(*pending)

    async_dispatcher_send(hass, signal, new_subentry.subentry_id)
    await asyncio.gather(*pending)

    configs = [config for _, config in added]
    assert configs.count(tracker_subentry.subentry_id) == 3
    assert configs.count(new_subentry.subentry_id) == 3
    assert len({entity.unique_id for entity, _ in added}) == 6
    assert entry._unload_callbacks, "dispatcher listener should be cleaned up on unload"


@pytest.mark.asyncio
async def test_dispatcher_deduplicates_existing_subentry_signals(
    stub_coordinator_factory: Any,
) -> None:
    """Repeated dispatcher signals should not build duplicate buttons."""

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

    initial_count = len(added)
    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"

    async_dispatcher_send(hass, signal, tracker_subentry.subentry_id)
    await asyncio.gather(*pending)

    async_dispatcher_send(hass, signal, tracker_subentry.subentry_id)
    await asyncio.gather(*pending)

    assert len(added) == initial_count == 3
    assert {config for _, config in added} == {tracker_subentry.subentry_id}


@pytest.mark.asyncio
async def test_hidden_devices_skipped_by_visibility(
    stub_coordinator_factory: Any,
) -> None:
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


@pytest.mark.asyncio
async def test_button_setup_ignores_non_tracker_subentry(
    stub_coordinator_factory: Any,
) -> None:
    """Button platform should skip service or unrelated subentry types."""

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

    await button.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    signal = f"{DOMAIN}_subentry_setup_{entry.entry_id}"
    async_dispatcher_send(hass, signal, service_subentry)
    await asyncio.gather(*pending)

    assert added == []


# ---------------------------------------------------------------------------
# PB-1: the recovery reads the tracker scope at call time
# ---------------------------------------------------------------------------

_PB1_SUBENTRY_ID = "tracker-subentry"
# A non-default group key, so a recovery that reads a fixed key instead of the
# scope's key (M-B3) is visible to the visibility double below.
_PB1_GROUP_KEY = "fleet"
_PB1_ACTIONS = ("play_sound", "stop_sound", "locate_device")


def _pb1_make_entry(subentries: dict[str, Any]) -> Any:
    """Config entry from the canonical factory (tests/AGENTS.md).

    ``subentries`` is an HA attribute outside the factory signature, so it is
    passed via ``**extra`` as the read-only mapping view the core hands out;
    ``async_on_unload`` is attached per instance as the factory leaves methods
    out on purpose.
    """
    entry = make_config_entry(
        entry_id="entry-button",
        data={
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
        },
        subentries=MappingProxyType(dict(subentries)),
    )
    unload_callbacks: list[Any] = []
    entry._unload_callbacks = unload_callbacks
    entry.async_on_unload = unload_callbacks.append
    return entry


def _pb1_uid(entry_id: str, device_id: str, action: str) -> str:
    return f"{DOMAIN}_{entry_id}_{_PB1_SUBENTRY_ID}_{device_id}_{action}"


def _pb1_registry(entry_id: str, present: set[str]) -> SimpleNamespace:
    """Entity registry double holding ``present`` button unique ids."""
    rows = {
        f"button.present_{index}": SimpleNamespace(
            domain="button",
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
@pytest.mark.parametrize("deferred", [False, True], ids=["direct", "deferred"])
async def test_recovery_recreates_missing_buttons(
    stub_coordinator_factory: Any, deferred: bool
) -> None:
    """The button recovery covers the tracker scope on both setup paths.

    ``direct``: no subentry manager, the scope is added during setup.
    ``deferred``: a subentry manager makes the setup wait for the dispatcher
    signal (``button.py``, "deferring subentry processing"), so the scope only
    exists after the signal. A hidden device must appear on neither path.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    tracker_subentry = ConfigSubentry(
        data={"group_key": _PB1_GROUP_KEY, "features": ("button",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id=_PB1_SUBENTRY_ID,
    )
    entry = _pb1_make_entry({_PB1_SUBENTRY_ID: tracker_subentry})

    visible_ids = {"visible-device"}

    def _is_visible(self: Any, subentry_key: str, device_id: str) -> bool:  # noqa: ANN001
        # Only the scope's own key decides; any other key answers "visible",
        # so a recovery that asks with a fixed key exposes the hidden device.
        if subentry_key in {_PB1_GROUP_KEY, _PB1_SUBENTRY_ID}:
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
    runtime = SimpleNamespace(coordinator=coordinator, entity_recovery_manager=manager)
    if deferred:
        # A subentry manager makes the setup wait for the dispatcher signal,
        # as for a parent entry in production.
        runtime.subentry_manager = SimpleNamespace(
            managed_subentries=dict(entry.subentries)
        )
    entry.runtime_data = runtime

    add_entities, added, pending = _make_add_entities(hass, loop)

    await button.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    if deferred:
        assert added == [], "deferred setup must add nothing before the signal"
        async_dispatcher_send(
            hass, f"{DOMAIN}_subentry_setup_{entry.entry_id}", _PB1_SUBENTRY_ID
        )
        await asyncio.gather(*pending)

    expected_visible = {
        _pb1_uid(entry.entry_id, "visible-device", action) for action in _PB1_ACTIONS
    }
    hidden = {
        _pb1_uid(entry.entry_id, "hidden-device", action) for action in _PB1_ACTIONS
    }

    # Setup observation: the platform itself exposes the visible buttons only.
    assert {entity.unique_id for entity, _ in added} == expected_visible

    # (a) The recovery expects exactly the visible buttons of the scope.
    assert manager.expected_unique_ids_for_platform("button") == expected_visible

    # (b) Reconstruction: one button is present, the two others are rebuilt
    # and handed to the recorded add_entities with the scope's subentry id.
    present = _pb1_uid(entry.entry_id, "visible-device", "play_sound")
    hass.data["entity_registry"] = _pb1_registry(entry.entry_id, {present})
    before = len(added)

    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)

    recovered = {(entity.unique_id, config) for entity, config in added[before:]}
    assert recovered == {
        (unique_id, _PB1_SUBENTRY_ID) for unique_id in expected_visible - {present}
    }
    assert not hidden & {unique_id for unique_id, _ in recovered}

    # (c) The registered factory builds only what is missing. The manager
    # filters its result by ``missing`` itself, so a factory that ignores
    # ``missing`` (M-B2) is invisible on the public path; the registration is
    # the only place where that property can be observed.
    registration = manager._platforms[str(Platform.BUTTON)]
    missing = expected_visible - {present}
    assert {entity.unique_id for entity in registration.entity_factory(missing)} == (
        missing
    )


@pytest.mark.asyncio
async def test_recovery_before_the_signal_expects_no_button(
    stub_coordinator_factory: Any,
) -> None:
    """Deferred setup: until the signal arrives the button recovery has no scope.

    It expects no unique id, builds nothing for any set of missing ids and
    recovers nothing; after the signal it expects the buttons of the scope.
    The registration is read from the manager because it is the only handle
    on the factory.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)

    tracker_subentry = ConfigSubentry(
        data={"group_key": _PB1_GROUP_KEY, "features": ("button",)},
        subentry_type="tracker",
        title="Tracker",
        subentry_id=_PB1_SUBENTRY_ID,
    )
    entry = _pb1_make_entry({_PB1_SUBENTRY_ID: tracker_subentry})
    coordinator_cls = stub_coordinator_factory(
        data=[{"id": "visible-device", "name": "Visible"}]
    )
    coordinator = coordinator_cls(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator,
        entity_recovery_manager=manager,
        # A subentry manager makes the setup wait for the dispatcher signal,
        # as for a parent entry in production.
        subentry_manager=SimpleNamespace(managed_subentries=dict(entry.subentries)),
    )
    add_entities, added, pending = _make_add_entities(hass, loop)

    await button.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert added == []
    assert manager.expected_unique_ids_for_platform("button") == set()
    registration = manager._platforms[str(Platform.BUTTON)]
    probe = {
        f"{DOMAIN}_{entry.entry_id}_{identifier}_visible-device_{action}"
        for identifier in ("tracker", _PB1_GROUP_KEY, _PB1_SUBENTRY_ID)
        for action in _PB1_ACTIONS
    }
    assert registration.entity_factory(probe) == []
    hass.data["entity_registry"] = _pb1_registry(entry.entry_id, set())
    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)
    assert added == []

    async_dispatcher_send(
        hass, f"{DOMAIN}_subentry_setup_{entry.entry_id}", _PB1_SUBENTRY_ID
    )
    await asyncio.gather(*pending)
    assert manager.expected_unique_ids_for_platform("button") == {
        _pb1_uid(entry.entry_id, "visible-device", action) for action in _PB1_ACTIONS
    }
