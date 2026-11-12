# tests/test_switch_automatic_polling.py
"""Per-tracker switch ``automatic_location_polling`` (#242).

The switch is the user-facing side of the coordinator option
``automatic_polling_disabled``. These tests hold the platform to its contract:
one CONFIG switch per visible tracker on the tracker's device, the platform is
loaded for existing installations, toggling writes the options without
reloading the entry, the other per-tracker entities stay unchanged, recovery
recreates a missing switch, unloading works without trackers, and switching off
does not change the availability of the protection mode sensor.
"""

from __future__ import annotations

import asyncio
import logging
import time
import types
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity import EntityCategory

from custom_components.googlefindmy import EntityRecoveryManager, button, switch
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
    OPT_ENABLE_STATS_ENTITIES,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
)
from custom_components.googlefindmy.coordinator import GoogleFindMyCoordinator
from custom_components.googlefindmy.coordinator.subentry import SubentryMetadata
from tests.helpers.config_entries_stub import make_config_entry
from tests.test_button_subentry_setup import _make_add_entities, _make_hass

_OPT_DISABLED = "automatic_polling_disabled"
_SUBENTRY_ID = "tracker-subentry"
_DEVICES = [
    {"id": "dev-a", "name": "Keys"},
    {"id": "dev-b", "name": "Backpack"},
]


def _switch_uid(entry_id: str, dev_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_{_SUBENTRY_ID}_{dev_id}_automatic_location_polling"


def _make_entry() -> Any:
    """Config entry from the canonical factory (tests/AGENTS.md).

    ``subentries`` is an HA attribute outside the factory signature, so it is
    passed via ``**extra``; ``async_on_unload`` is attached per instance as the
    factory leaves methods out on purpose.
    """
    entry = make_config_entry(
        entry_id="entry-registry",
        data={
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
            OPT_ENABLE_STATS_ENTITIES: True,
        },
        options={OPT_ENABLE_STATS_ENTITIES: True},
        title="Registry Alignment",
        subentries={},
    )
    unload_callbacks: list[Callable[[], None]] = []
    entry._unload_callbacks = unload_callbacks
    entry.async_on_unload = unload_callbacks.append
    return entry


def _tracker_subentry(features: tuple[str, ...] = ("switch",)) -> ConfigSubentry:
    return ConfigSubentry(
        data={"group_key": TRACKER_SUBENTRY_KEY, "features": features},
        subentry_type="tracker",
        title="Tracker",
        subentry_id=_SUBENTRY_ID,
    )


def _coordinator(
    stub_coordinator_factory: Callable[..., type[Any]],
    hass: Any,
    entry: Any,
    *,
    devices: list[dict[str, Any]],
    hidden: frozenset[str] = frozenset(),
) -> Any:
    """Stub coordinator whose switch state lives in the entry options."""
    base = stub_coordinator_factory(data=devices)

    class _Coordinator(base):  # type: ignore[misc, valid-type]
        def is_device_visible_in_subentry(
            self, subentry_key: str, device_id: str
        ) -> bool:
            return device_id not in hidden

        def is_automatic_polling_enabled(self, device_id: str) -> bool:
            return device_id not in set(
                self.config_entry.options.get(_OPT_DISABLED, [])
            )

        def async_set_automatic_polling(self, device_id: str, enabled: bool) -> None:
            current = set(self.config_entry.options.get(_OPT_DISABLED, []))
            current = current - {device_id} if enabled else current | {device_id}
            self.config_entry.options = {
                **self.config_entry.options,
                _OPT_DISABLED: sorted(current),
            }

    coordinator = _Coordinator(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    return coordinator


async def _setup_switches(
    stub_coordinator_factory: Callable[..., type[Any]],
    *,
    devices: list[dict[str, Any]] = _DEVICES,
    hidden: frozenset[str] = frozenset(),
    recovery: bool = False,
    deferred: bool = False,
) -> tuple[Any, Any, Any, list[tuple[Any, str | None]], list[asyncio.Task[Any]]]:
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _make_entry()
    entry.subentries = {_SUBENTRY_ID: _tracker_subentry()}
    coordinator = _coordinator(
        stub_coordinator_factory, hass, entry, devices=devices, hidden=hidden
    )
    runtime = SimpleNamespace(coordinator=coordinator)
    if recovery:
        runtime.entity_recovery_manager = EntityRecoveryManager(
            hass, entry, coordinator
        )
    if deferred:
        # A subentry manager makes the setup wait for the dispatcher signal,
        # as for a parent entry in production.
        runtime.subentry_manager = SimpleNamespace(
            managed_subentries=dict(entry.subentries)
        )
    entry.runtime_data = runtime
    add_entities, added, pending = _make_add_entities(hass, loop)
    await switch.async_setup_entry(hass, entry, add_entities)
    await asyncio.gather(*pending)
    if deferred:
        async_dispatcher_send(
            hass, f"{DOMAIN}_subentry_setup_{entry.entry_id}", _SUBENTRY_ID
        )
        await asyncio.gather(*pending)
    return hass, entry, coordinator, added, pending


# ---------------------------------------------------------------------------
# (a) one CONFIG switch per tracker, on the tracker's device
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_config_switch_per_tracker_on_its_device(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """(a) N trackers give N switches, category CONFIG, the tracker's device."""
    hass, entry, coordinator, added, _ = await _setup_switches(stub_coordinator_factory)

    switches = [entity for entity, _ in added]
    assert {entity.unique_id for entity in switches} == {
        _switch_uid(entry.entry_id, dev["id"]) for dev in _DEVICES
    }
    assert {config for _, config in added} == {_SUBENTRY_ID}
    for entity in switches:
        assert entity.entity_description.entity_category is EntityCategory.CONFIG
        assert entity.entity_description.translation_key == (
            "automatic_location_polling"
        )
        locate = button.GoogleFindMyLocateButton(
            coordinator,
            dict(entity._device),
            None,
            subentry_key=TRACKER_SUBENTRY_KEY,
            subentry_identifier=_SUBENTRY_ID,
        )
        assert vars(entity.device_info) == vars(locate.device_info)
        assert entity.is_on is True


# ---------------------------------------------------------------------------
# (b) an existing entry without "switch" in its stored features loads it
# ---------------------------------------------------------------------------


def test_switch_is_a_tracker_feature_platform() -> None:
    """(b, static) ``switch`` is one of the canonical tracker platforms."""
    assert "switch" in TRACKER_FEATURE_PLATFORMS
    assert list(TRACKER_FEATURE_PLATFORMS) == sorted(TRACKER_FEATURE_PLATFORMS)


@pytest.mark.asyncio
async def test_existing_entry_without_switch_feature_loads_the_platform(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """(b) Stored features from before the update are rewritten at setup (A3).

    ``async_setup_entry`` syncs the tracker subentry definition from
    ``TRACKER_FEATURE_PLATFORMS``; the subentry stored by an older version
    therefore carries ``switch`` afterwards, and forwarding loads the platform.
    """
    from tests.test_hass_data_layout import _prepare_async_setup_entry_harness

    harness = _prepare_async_setup_entry_harness(
        monkeypatch, stub_coordinator_factory, asyncio.get_running_loop()
    )
    integration = harness.integration
    entry = harness.entry
    hass = harness.hass
    # Both subentries as an installation from before the update stored them.
    stored = (
        (
            "old-tracker",
            "tracker",
            TRACKER_SUBENTRY_KEY,
            ["button", "device_tracker", "sensor"],
        ),
        (
            "old-service",
            "service",
            SERVICE_SUBENTRY_KEY,
            list(SERVICE_FEATURE_PLATFORMS),
        ),
    )
    for subentry_id, subentry_type, key, features in stored:
        entry.subentries[subentry_id] = ConfigSubentry(
            data={"group_key": key, "features": features},
            subentry_type=subentry_type,
            title=key,
            subentry_id=subentry_id,
            unique_id=f"{entry.entry_id}-{key}",
        )
        hass.config_entries._registered_subentry_ids.add(subentry_id)
    harness.cache.values = {integration.username_string: "user@example.com"}

    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup_entry(hass, entry) is True
    while pending := [task for task in hass._tasks if not task.done()]:
        await asyncio.gather(*pending, return_exceptions=True)

    trackers = [
        sub
        for sub in entry.subentries.values()
        if getattr(sub, "subentry_type", None) == "tracker"
    ]
    assert [sub.subentry_id for sub in trackers] == ["old-tracker"]
    for sub in trackers:
        assert "switch" in sub.data["features"]

    forwarded = {
        str(getattr(platform, "value", platform))
        for _entry, platforms in hass.config_entries.forward_calls
        for platform in platforms
    }
    assert "switch" in forwarded


# ---------------------------------------------------------------------------
# (c) toggling writes the options through the real setup and never reloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_does_not_reload_entry(
    monkeypatch: pytest.MonkeyPatch,
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """(c) Off and on store the options; the update listener schedules no reload.

    The entry is set up through ``async_setup_entry``, which installs the real
    options update listener ``_async_refresh_watch_paths``. Home Assistant core
    calls the update listeners as tasks when ``async_update_entry`` changes the
    entry; the stub manager below does the same and the tasks are awaited.
    The coordinator's own ``async_set_automatic_polling`` does the writing.
    """
    from tests.test_hass_data_layout import _prepare_async_setup_entry_harness

    harness = _prepare_async_setup_entry_harness(
        monkeypatch, stub_coordinator_factory, asyncio.get_running_loop()
    )
    integration = harness.integration
    entry = harness.entry
    hass = harness.hass
    harness.cache.values = {integration.username_string: "user@example.com"}
    assert await integration.async_setup(hass, {}) is True
    assert await integration.async_setup_entry(hass, entry) is True
    listeners = list(entry._update_listeners)
    assert listeners, "async_setup_entry installs the options update listener"

    stored = hass.config_entries.async_update_entry
    listener_tasks: list[asyncio.Task[Any]] = []

    def _core_like_update(target: Any, **kwargs: Any) -> bool:
        before = (dict(target.options), dict(target.data))
        stored(target, **kwargs)
        changed = before != (dict(target.options), dict(target.data))
        if changed:
            for listener in listeners:
                listener_tasks.append(
                    asyncio.get_running_loop().create_task(listener(hass, target))
                )
        return changed

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _core_like_update)

    coordinator = entry.runtime_data.coordinator
    coordinator.hass = hass
    coordinator.config_entry = entry
    for name in (
        "async_set_automatic_polling",
        "is_automatic_polling_enabled",
        "_get_automatic_polling_disabled_set",
    ):
        setattr(
            coordinator,
            name,
            types.MethodType(getattr(GoogleFindMyCoordinator, name), coordinator),
        )
    entity = switch.GoogleFindMyAutomaticPollingSwitch(
        coordinator,
        {"id": "dev-a", "name": "Keys"},
        None,
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=_SUBENTRY_ID,
    )
    entity.hass = hass
    entity.async_write_ha_state = lambda: None

    await entity.async_turn_off()
    await asyncio.gather(*listener_tasks)
    assert entry.options[_OPT_DISABLED] == ["dev-a"]
    assert entity.is_on is False

    await entity.async_turn_on()
    await asyncio.gather(*listener_tasks)
    assert entry.options[_OPT_DISABLED] == []
    assert entity.is_on is True

    assert len(listener_tasks) == 2, "each change notifies the listener once"
    assert hass.config_entries.scheduled_reloads == [], (
        "toggling the switch must not reload the entry"
    )


# ---------------------------------------------------------------------------
# (d) the other per-tracker entities stay the same
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buttons_unchanged_by_the_switch_platform(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """(d) Button entities keep their number and unique ids (R6)."""
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _make_entry()
    entry.subentries = {
        _SUBENTRY_ID: _tracker_subentry(tuple(TRACKER_FEATURE_PLATFORMS))
    }
    coordinator = _coordinator(stub_coordinator_factory, hass, entry, devices=_DEVICES)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)

    add_buttons, buttons, pending = _make_add_entities(hass, loop)
    await button.async_setup_entry(hass, entry, add_buttons)
    await asyncio.gather(*pending)
    add_switches, switches, pending_switches = _make_add_entities(hass, loop)
    await switch.async_setup_entry(hass, entry, add_switches)
    await asyncio.gather(*pending_switches)

    expected_buttons = {
        f"{DOMAIN}_{entry.entry_id}_{_SUBENTRY_ID}_{dev['id']}_{action}"
        for dev in _DEVICES
        for action in ("play_sound", "stop_sound", "locate_device")
    }
    assert {entity.unique_id for entity, _ in buttons} == expected_buttons
    assert len(buttons) == 3 * len(_DEVICES)
    assert len(switches) == len(_DEVICES)
    assert not expected_buttons & {entity.unique_id for entity, _ in switches}


# ---------------------------------------------------------------------------
# (e) ignored and hidden trackers get no switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hidden_or_ignored_trackers_get_no_switch(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """(e) Invisible devices (ignored ones are invisible) get no switch.

    The coordinator leaves ignored devices out of ``visible_device_ids``, so
    ``is_device_visible_in_subentry`` is the single gate for both.
    """
    _, entry, _, added, _ = await _setup_switches(
        stub_coordinator_factory, hidden=frozenset({"dev-b"})
    )
    assert {entity.unique_id for entity, _ in added} == {
        _switch_uid(entry.entry_id, "dev-a")
    }


# ---------------------------------------------------------------------------
# (f) recovery recreates a missing switch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("deferred", [False, True], ids=["direct", "deferred"])
async def test_recovery_recreates_a_missing_switch(
    stub_coordinator_factory: Callable[..., type[Any]],
    deferred: bool,
) -> None:
    """(f) A switch missing from the entity registry is built and added again.

    ``deferred`` covers the production path of a parent entry with a subentry
    manager, where the tracker scope only exists after the dispatcher signal.
    """
    hass, entry, _, added, pending = await _setup_switches(
        stub_coordinator_factory, recovery=True, deferred=deferred
    )
    if deferred:
        assert {entity.unique_id for entity, _ in added} == {
            _switch_uid(entry.entry_id, dev["id"]) for dev in _DEVICES
        }
    manager = entry.runtime_data.entity_recovery_manager
    assert manager.expected_unique_ids_for_platform("switch") == {
        _switch_uid(entry.entry_id, dev["id"]) for dev in _DEVICES
    }

    present = _switch_uid(entry.entry_id, "dev-a")
    registry = SimpleNamespace(
        entities={
            "switch.keys": SimpleNamespace(
                domain="switch",
                platform=DOMAIN,
                unique_id=present,
                config_entry_id=entry.entry_id,
            )
        },
        async_entries_for_config_entry=lambda entry_id: [
            row for row in registry.entities.values() if row.config_entry_id == entry_id
        ],
    )
    hass.data["entity_registry"] = registry
    before = len(added)

    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)

    recovered = [entity.unique_id for entity, _ in added[before:]]
    assert recovered == [_switch_uid(entry.entry_id, "dev-b")]


# ---------------------------------------------------------------------------
# (g) unloading with the switch platform works without trackers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unload_with_switch_platform_and_no_tracker(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """(g) Setup without trackers adds nothing; unload runs every callback."""
    from homeassistant.const import Platform

    from custom_components.googlefindmy import PLATFORMS

    assert Platform.SWITCH in PLATFORMS
    _, entry, _, added, _ = await _setup_switches(stub_coordinator_factory, devices=[])
    assert added == []
    assert entry._unload_callbacks
    for unload in entry._unload_callbacks:
        unload()


# ---------------------------------------------------------------------------
# (h) switching off does not change the protection mode sensor's availability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uwt_sensor_availability_unchanged_by_switching_off(
    hass: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(h) Availability follows presence, not the switch (R-5)."""
    from tests.test_coordinator_automatic_polling import (
        _core_entry,
        _switch_coordinator,
    )
    from tests.test_uwt_mode_binary_sensor import _build_uwt_sensor

    entry = _core_entry(hass, {})
    coordinator = _switch_coordinator(monkeypatch, hass, entry)
    coordinator.last_update_success = True
    coordinator.is_device_visible_in_subentry = lambda _key, _dev: True
    coordinator._present_last_seen["dev-off"] = time.monotonic()
    sensor = _build_uwt_sensor(device_id="dev-off", coordinator=coordinator, hass=hass)

    before = sensor.available
    coordinator.async_set_automatic_polling("dev-off", False)
    assert coordinator.is_automatic_polling_enabled("dev-off") is False

    assert before is True
    assert sensor.available is before


def test_switch_entity_has_no_name_in_the_change_log() -> None:
    """The entity itself logs nothing; the coordinator writes the change line."""
    source = switch.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    body = text.split("class GoogleFindMyAutomaticPollingSwitch", 1)[1]
    assert "_LOGGER." not in body


# ---------------------------------------------------------------------------
# M0 positive proofs: behaviour of ``switch.py`` no earlier test pinned
# ---------------------------------------------------------------------------

_M0_SERVICE_ID = "service-subentry"
_M0_SECOND_ID = "second-subentry"
_M0_SIGNAL_LOGGER = "custom_components.googlefindmy"


def _m0_entry(subentries: dict[str, Any] | None, *, entry_id: str = "entry-m0") -> Any:
    """Config entry from the canonical factory (tests/AGENTS.md).

    ``subentries`` is an HA attribute outside the factory signature, so it is
    passed via ``**extra`` as the read-only mapping view the core hands out;
    ``None`` leaves the attribute out entirely (a pure test way, no core
    produces it). ``async_on_unload`` is attached per instance as the factory
    leaves methods out on purpose.
    """
    extra: dict[str, Any] = {}
    if subentries is not None:
        extra["subentries"] = types.MappingProxyType(dict(subentries))
    entry = make_config_entry(
        entry_id=entry_id,
        data={
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
            CONF_GOOGLE_EMAIL: "user@example.com",
        },
        **extra,
    )
    unload_callbacks: list[Callable[[], None]] = []
    entry._unload_callbacks = unload_callbacks
    entry.async_on_unload = unload_callbacks.append
    return entry


def _m0_uid(entry_id: str, identifier: str, dev_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_{identifier}_{dev_id}_automatic_location_polling"


def _m0_add_entities(
    hass: Any, loop: asyncio.AbstractEventLoop
) -> tuple[Any, list[tuple[Any, str | None]], list[asyncio.Task[Any]], list[int]]:
    """``_make_add_entities`` plus the size of every call.

    A scope that is added a second time hands an empty list to
    ``async_add_entities``; ``added`` alone cannot see that call.
    """
    inner, added, pending = _make_add_entities(hass, loop)
    calls: list[int] = []

    def _add(entities: Any, **kwargs: Any) -> None:
        entity_list = list(entities)
        calls.append(len(entity_list))
        inner(entity_list, **kwargs)

    return _add, added, pending, calls


def _m0_service_subentry() -> ConfigSubentry:
    return ConfigSubentry(
        data={"group_key": SERVICE_SUBENTRY_KEY, "features": ("binary_sensor",)},
        subentry_type="service",
        title="Service",
        subentry_id=_M0_SERVICE_ID,
    )


def _m0_tracker_subentry(
    subentry_id: str,
    *,
    group_key: str = TRACKER_SUBENTRY_KEY,
    features: tuple[str, ...] = ("switch",),
) -> ConfigSubentry:
    return ConfigSubentry(
        data={"group_key": group_key, "features": features},
        subentry_type="tracker",
        title="Tracker",
        subentry_id=subentry_id,
    )


def _m0_coordinator(
    stub_coordinator_factory: Callable[..., type[Any]],
    hass: Any,
    entry: Any,
    *,
    devices: list[dict[str, Any]] = _DEVICES,
    hidden: frozenset[str] = frozenset(),
    failing: frozenset[str] = frozenset(),
    snapshot_callback: Callable[[str | None, str | None], list[dict[str, Any]]]
    | None = None,
) -> Any:
    """Stub coordinator whose stable identifier differs from the key.

    In production the stable identifier of a scope is its config subentry id,
    not the group key; a stub answering the key for both would hide a fallback
    to the key. ``failing`` names devices whose visibility check raises, as a
    stub without a visibility index would.
    """
    base = stub_coordinator_factory(data=devices, snapshot_callback=snapshot_callback)

    class _Coordinator(base):  # type: ignore[misc, valid-type]
        def is_device_visible_in_subentry(
            self, subentry_key: str, device_id: str
        ) -> bool:
            if device_id in failing:
                raise RuntimeError("visibility index unavailable")
            return device_id not in hidden

        def stable_subentry_identifier(
            self, *, key: str | None = None, feature: str | None = None
        ) -> str:
            return (
                f"stable:{super().stable_subentry_identifier(key=key, feature=feature)}"
            )

    coordinator = _Coordinator(hass, cache=SimpleNamespace(entry_id=entry.entry_id))
    coordinator.config_entry = entry
    return coordinator


def _m0_registry(entry_id: str, present: set[str]) -> SimpleNamespace:
    """Entity registry double holding ``present`` switch unique ids."""
    rows = {
        f"switch.present_{index}": SimpleNamespace(
            domain="switch",
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


async def _m0_setup(
    hass: Any, entry: Any, add_entities: Any, *, config_subentry_id: str | None = None
) -> BaseException | None:
    """Run the platform setup and return the exception it raised, if any."""
    try:
        await switch.async_setup_entry(
            hass, entry, add_entities, config_subentry_id=config_subentry_id
        )
    except Exception as err:  # noqa: BLE001 - the test asserts on the outcome
        return err
    return None


def _m0_signal(entry: Any) -> str:
    return f"{DOMAIN}_subentry_setup_{entry.entry_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("way", ["empty", "absent"])
async def test_setup_without_subentries_uses_the_coordinator_fallback_scope(
    stub_coordinator_factory: Callable[..., type[Any]],
    way: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Without a subentry the switches live under the coordinator's tracker identifier.

    ``empty``: ``entry.subentries`` is an empty mapping, as before the first
    subentry sync. ``absent``: no ``subentries`` attribute at all; a pure test
    way, no core produces it. On both ways the platform warns once that no
    config subentry id is available and adds the switches anyway.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({} if way == "empty" else None)
    coordinator = _m0_coordinator(stub_coordinator_factory, hass, entry)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, calls = _m0_add_entities(hass, loop)

    with caplog.at_level(logging.DEBUG, logger=_M0_SIGNAL_LOGGER):
        error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert error is None
    fallback = coordinator.stable_subentry_identifier(key=TRACKER_SUBENTRY_KEY)
    assert fallback == f"stable:{TRACKER_SUBENTRY_KEY}"
    assert {(entity.unique_id, config) for entity, config in added} == {
        (_m0_uid(entry.entry_id, fallback, dev["id"]), fallback) for dev in _DEVICES
    }
    assert calls == [len(_DEVICES)]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [
        f"[{entry.entry_id}] switch platform deferred because config_subentry_id "
        "is unavailable; metadata may be stale or Home Assistant has not finished "
        "registering subentries yet."
    ]


@pytest.mark.asyncio
async def test_setup_hands_the_entry_to_a_coordinator_without_one(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A coordinator built without a config entry gets this entry at setup.

    The switch state is read from the entry's options through the coordinator,
    so the switches can only report their state once the entry is attached.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({_SUBENTRY_ID: _tracker_subentry()})
    coordinator = _coordinator(stub_coordinator_factory, hass, entry, devices=_DEVICES)
    coordinator.config_entry = None
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending = _make_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)

    assert error is None
    assert coordinator.config_entry is entry
    await asyncio.gather(*pending)
    assert [entity.is_on for entity, _ in added] == [True, True]


@pytest.mark.asyncio
async def test_signalled_subentry_type_falls_back_to_its_data(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A signalled subentry without ``subentry_type`` is typed by its ``data``.

    The dispatcher payload is ``ConfigSubentry | Any``. A payload without the
    attribute is a service when its data says so (under ``subentry_type`` or
    under ``type``) and gets no switch; it is untyped, hence processed, when
    the data carries no string type or is no mapping at all. A typed service
    subentry gets no switch either, at setup and when signalled.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({_M0_SERVICE_ID: _m0_service_subentry()})
    coordinator = _m0_coordinator(stub_coordinator_factory, hass, entry)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)
    assert error is None
    assert added == []

    for payload in (
        _m0_service_subentry(),
        SimpleNamespace(
            subentry_id="service-by-key", data={"subentry_type": "service"}
        ),
        SimpleNamespace(subentry_id="service-by-type", data={"type": "service"}),
    ):
        async_dispatcher_send(hass, _m0_signal(entry), payload)
        await asyncio.gather(*pending)
    assert added == []
    assert calls == []

    for payload in (
        SimpleNamespace(subentry_id="untyped-number", data={"type": 7}),
        SimpleNamespace(subentry_id="untyped-nodata"),
    ):
        async_dispatcher_send(hass, _m0_signal(entry), payload)
        await asyncio.gather(*pending)
    assert {(entity.unique_id, config) for entity, config in added} == {
        (_m0_uid(entry.entry_id, identifier, dev["id"]), identifier)
        for identifier in ("untyped-number", "untyped-nodata")
        for dev in _DEVICES
    }
    assert calls == [len(_DEVICES), len(_DEVICES)]


@pytest.mark.asyncio
async def test_repeated_and_foreign_signals_add_no_switch_twice(
    stub_coordinator_factory: Callable[..., type[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A known subentry signalled again adds nothing; a foreign id is skipped.

    The second signal with the tracker id is dropped before any scope is
    built. A signal with the service subentry's id string is no tracker id
    (service subentries are left out of the known tracker ids), so its scope
    is skipped with a debug line, while the tracker scope it collects again
    adds no switch a second time: ``async_add_entities`` sees one empty call.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry(
        {_SUBENTRY_ID: _tracker_subentry(), _M0_SERVICE_ID: _m0_service_subentry()}
    )
    coordinator = _m0_coordinator(stub_coordinator_factory, hass, entry)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, calls = _m0_add_entities(hass, loop)
    expected = {
        (_m0_uid(entry.entry_id, _SUBENTRY_ID, dev["id"]), _SUBENTRY_ID)
        for dev in _DEVICES
    }

    with caplog.at_level(logging.DEBUG, logger=_M0_SIGNAL_LOGGER):
        error = await _m0_setup(hass, entry, add_entities)
        await asyncio.gather(*pending)
        assert error is None
        assert {(entity.unique_id, config) for entity, config in added} == expected
        assert calls == [len(_DEVICES)]

        async_dispatcher_send(hass, _m0_signal(entry), _SUBENTRY_ID)
        await asyncio.gather(*pending)
        assert calls == [len(_DEVICES)]

        async_dispatcher_send(hass, _m0_signal(entry), _M0_SERVICE_ID)
        await asyncio.gather(*pending)

    assert {(entity.unique_id, config) for entity, config in added} == expected
    assert calls == [len(_DEVICES), 0]
    assert (
        f"Switch setup: skipping subentry '{_M0_SERVICE_ID}' because the "
        "config_subentry_id is unknown"
    ) in [r.getMessage() for r in caplog.records]


@pytest.mark.asyncio
async def test_recovery_before_the_signal_expects_and_builds_nothing(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Deferred setup: until the signal arrives, the recovery has no scope.

    It expects no unique id, builds nothing for any set of missing ids, and
    its add callback hands nothing to Home Assistant; after the signal it
    expects the switches of the scope. The registration is read from the
    manager because it is the only handle on the factory and the callback.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({_SUBENTRY_ID: _tracker_subentry()})
    coordinator = _m0_coordinator(stub_coordinator_factory, hass, entry)
    manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator,
        entity_recovery_manager=manager,
        # A subentry manager makes the setup wait for the dispatcher signal,
        # as for a parent entry in production.
        subentry_manager=SimpleNamespace(managed_subentries=dict(entry.subentries)),
    )
    add_entities, added, pending, calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)
    assert error is None
    assert added == []
    assert manager.expected_unique_ids_for_platform("switch") == set()

    registration = manager._platforms["switch"]
    probe = {
        _m0_uid(entry.entry_id, identifier, dev["id"])
        for identifier in (TRACKER_SUBENTRY_KEY, _SUBENTRY_ID)
        for dev in _DEVICES
    }
    assert registration.entity_factory(probe) == []
    assert registration.entity_factory(set()) == []
    stray = switch.GoogleFindMyAutomaticPollingSwitch(
        coordinator,
        dict(_DEVICES[0]),
        None,
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=_SUBENTRY_ID,
    )
    registration.add_entities([stray])
    hass.data["entity_registry"] = _m0_registry(entry.entry_id, set())
    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)
    assert added == []
    assert calls == [0]

    async_dispatcher_send(hass, _m0_signal(entry), _SUBENTRY_ID)
    await asyncio.gather(*pending)
    assert manager.expected_unique_ids_for_platform("switch") == {
        _m0_uid(entry.entry_id, _SUBENTRY_ID, dev["id"]) for dev in _DEVICES
    }


@pytest.mark.asyncio
async def test_recovery_expects_nothing_for_an_entry_without_id(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """With an empty ``entry_id`` the recovery expects and builds nothing.

    A pure test way: Home Assistant generates every entry id. The switches
    themselves are still set up; their unique id simply lacks the entry part.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({_SUBENTRY_ID: _tracker_subentry()}, entry_id="placeholder")
    entry.entry_id = ""
    coordinator = _m0_coordinator(stub_coordinator_factory, hass, entry)
    manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, entity_recovery_manager=manager
    )
    add_entities, added, pending, _calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert error is None
    assert {entity.unique_id for entity, _ in added} == {
        f"{DOMAIN}_{_SUBENTRY_ID}_{dev['id']}_automatic_location_polling"
        for dev in _DEVICES
    }
    assert manager.expected_unique_ids_for_platform("switch") == set()
    registration = manager._platforms["switch"]
    probe = {_m0_uid("", _SUBENTRY_ID, dev["id"]) for dev in _DEVICES} | {
        entity.unique_id for entity, _ in added
    }
    assert registration.entity_factory(probe) == []


@pytest.mark.asyncio
async def test_listener_adds_only_devices_new_to_the_scope(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """The coordinator listener adds a switch for a new device, none for known ones."""
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({_SUBENTRY_ID: _tracker_subentry()})
    coordinator = _m0_coordinator(
        stub_coordinator_factory, hass, entry, devices=[dict(dev) for dev in _DEVICES]
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)
    assert error is None
    assert {entity.unique_id for entity, _ in added} == {
        _m0_uid(entry.entry_id, _SUBENTRY_ID, dev["id"]) for dev in _DEVICES
    }
    assert calls == [len(_DEVICES)]
    # ``_listeners`` is the stub coordinator's record of ``async_add_listener``.
    assert coordinator._listeners, "setup registers the listener on the coordinator"

    for listener in list(coordinator._listeners):
        listener()
    await asyncio.gather(*pending)
    assert calls == [len(_DEVICES)]

    coordinator.data.append({"id": "dev-c", "name": "Bike"})
    for listener in list(coordinator._listeners):
        listener()
    await asyncio.gather(*pending)
    assert calls == [len(_DEVICES), 1]
    assert [
        (entity.unique_id, config) for entity, config in added[len(_DEVICES) :]
    ] == [(_m0_uid(entry.entry_id, _SUBENTRY_ID, "dev-c"), _SUBENTRY_ID)]


@pytest.mark.asyncio
async def test_recovery_keeps_the_first_tracker_scope_with_two_subentries(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """With two tracker subentries the recovery stays on the first scope.

    Both scopes get their switches at setup; the recovery expects and rebuilds
    the first scope's switches only and hands them to that scope's add
    callback, so a recovered switch carries the first subentry id.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry(
        {
            _SUBENTRY_ID: _tracker_subentry(),
            _M0_SECOND_ID: _m0_tracker_subentry(_M0_SECOND_ID),
        }
    )
    coordinator = _m0_coordinator(stub_coordinator_factory, hass, entry)
    manager = EntityRecoveryManager(hass, entry, coordinator)
    entry.runtime_data = SimpleNamespace(
        coordinator=coordinator, entity_recovery_manager=manager
    )
    add_entities, added, pending, _calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)
    assert error is None

    first = {_m0_uid(entry.entry_id, _SUBENTRY_ID, dev["id"]) for dev in _DEVICES}
    second = {_m0_uid(entry.entry_id, _M0_SECOND_ID, dev["id"]) for dev in _DEVICES}
    assert {(entity.unique_id, config) for entity, config in added} == {
        (unique_id, _SUBENTRY_ID) for unique_id in first
    } | {(unique_id, _M0_SECOND_ID) for unique_id in second}
    assert manager.expected_unique_ids_for_platform("switch") == first

    hass.data["entity_registry"] = _m0_registry(entry.entry_id, set())
    before = len(added)
    await manager.async_recover_missing_entities()
    await asyncio.gather(*pending)
    assert {(entity.unique_id, config) for entity, config in added[before:]} == {
        (unique_id, _SUBENTRY_ID) for unique_id in first
    }


def _m0_metadata(key: str, config_subentry_id: str, *features: str) -> SubentryMetadata:
    return SubentryMetadata(
        key=key,
        config_subentry_id=config_subentry_id,
        features=tuple(features),
        title=None,
        poll_intervals={},
        filters={},
        feature_flags={},
        visible_device_ids=(),
        enabled_device_ids=(),
    )


@pytest.mark.asyncio
async def test_coordinator_metadata_provides_the_tracker_scopes(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Without entry subentries the coordinator's subentry metadata names the scopes.

    Only metadata with the ``switch`` feature makes a scope; its identifier is
    the metadata's stable identifier (the config subentry id) and its key the
    metadata key, under which the snapshot is read.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({})
    by_key = {"fleet": [dict(_DEVICES[0])], "other": [dict(_DEVICES[1])]}
    coordinator = _m0_coordinator(
        stub_coordinator_factory,
        hass,
        entry,
        snapshot_callback=lambda key, _feature: list(by_key.get(key or "", [])),
    )
    # The coordinator's metadata index; a private attribute of the double.
    coordinator._subentry_metadata = {
        "fleet": _m0_metadata("fleet", "cfg-fleet", "switch"),
        "other": _m0_metadata("other", "cfg-other", "button"),
    }
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert error is None
    assert {(entity.unique_id, config) for entity, config in added} == {
        (_m0_uid(entry.entry_id, "cfg-fleet", _DEVICES[0]["id"]), "cfg-fleet")
    }
    assert calls == [1]


@pytest.mark.asyncio
async def test_subentry_without_id_or_data_leaves_the_fallback_scope(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A tracker subentry without id and without data yields no id and no scope.

    A pure test way: a core ``ConfigSubentry`` always carries both. The setup
    then falls back to the coordinator's tracker scope, as without subentries.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({"odd": SimpleNamespace(subentry_type="tracker", data=None)})
    coordinator = _m0_coordinator(stub_coordinator_factory, hass, entry)
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, _calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert error is None
    fallback = f"stable:{TRACKER_SUBENTRY_KEY}"
    assert {(entity.unique_id, config) for entity, config in added} == {
        (_m0_uid(entry.entry_id, fallback, dev["id"]), fallback) for dev in _DEVICES
    }


@pytest.mark.asyncio
async def test_tracker_subentry_without_the_switch_feature_gets_no_switch(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Only a tracker subentry whose features include ``switch`` makes a scope.

    The snapshot is read under the scope's group key. A subentry without the
    feature is still signalled, but its scope falls back to the default key,
    under which this coordinator has no device.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry(
        {
            "fleet-subentry": _m0_tracker_subentry("fleet-subentry", group_key="fleet"),
            "fleet-b-subentry": _m0_tracker_subentry(
                "fleet-b-subentry", group_key="fleet-b", features=("button",)
            ),
        }
    )
    by_key = {"fleet": [dict(_DEVICES[0])], "fleet-b": [dict(_DEVICES[1])]}
    coordinator = _m0_coordinator(
        stub_coordinator_factory,
        hass,
        entry,
        snapshot_callback=lambda key, _feature: list(by_key.get(key or "", [])),
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, _calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert error is None
    assert {(entity.unique_id, config) for entity, config in added} == {
        (_m0_uid(entry.entry_id, "fleet-subentry", _DEVICES[0]["id"]), "fleet-subentry")
    }


@pytest.mark.asyncio
async def test_availability_follows_visibility_and_a_failing_check_hides_nothing(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """``available`` is the coordinator's visibility; a check that raises hides nothing.

    Setup skips a hidden device and keeps a device whose visibility check
    raises; a switch built for a hidden device reports unavailable.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({_SUBENTRY_ID: _tracker_subentry()})
    devices = [
        {"id": "dev-a", "name": "Keys"},
        {"id": "dev-b", "name": "Backpack"},
        {"id": "dev-c", "name": "Bike"},
    ]
    coordinator = _m0_coordinator(
        stub_coordinator_factory,
        hass,
        entry,
        devices=devices,
        hidden=frozenset({"dev-b"}),
        failing=frozenset({"dev-c"}),
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, _calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert error is None
    assert {entity.unique_id for entity, _ in added} == {
        _m0_uid(entry.entry_id, _SUBENTRY_ID, dev_id) for dev_id in ("dev-a", "dev-c")
    }
    assert [entity.available for entity, _ in added] == [True, True]
    hidden = switch.GoogleFindMyAutomaticPollingSwitch(
        coordinator,
        dict(devices[1]),
        None,
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier=_SUBENTRY_ID,
    )
    assert hidden.available is False


@pytest.mark.asyncio
async def test_devices_without_a_string_id_get_no_switch(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """Snapshot rows without a non-empty string ``id`` are skipped, not built.

    A row with an id but without a name gets its label from the id.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({_SUBENTRY_ID: _tracker_subentry()})
    devices: list[dict[str, Any]] = [
        {"id": "dev-a", "name": "Keys"},
        {"name": "no id"},
        {"id": 5, "name": "number"},
        {"id": "", "name": "empty"},
        {"id": "dev-d"},
    ]
    coordinator = _m0_coordinator(
        stub_coordinator_factory, hass, entry, devices=devices
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert error is None
    assert {entity.unique_id for entity, _ in added} == {
        _m0_uid(entry.entry_id, _SUBENTRY_ID, dev_id) for dev_id in ("dev-a", "dev-d")
    }
    assert calls == [2]
    assert {entity.device_label() for entity, _ in added} == {"Keys", "dev-d"}


@pytest.mark.asyncio
async def test_device_with_only_blank_fields_gets_a_switch_without_a_name(
    stub_coordinator_factory: Callable[..., type[Any]],
) -> None:
    """A row whose ``id`` is whitespace passes the id check but yields no label.

    Whitespace is a non-empty string, so the switch is built; ``name``,
    ``device_id`` and ``id`` all strip to nothing, so no label is derived and
    the device info Home Assistant reads carries no name (the entity falls
    back to its default label). A derived label would appear there.
    """
    loop = asyncio.get_running_loop()
    hass = _make_hass(loop)
    entry = _m0_entry({_SUBENTRY_ID: _tracker_subentry()})
    devices: list[dict[str, Any]] = [
        {"id": "dev-a", "name": "Keys"},
        {"id": "   ", "name": " ", "device_id": "\t"},
    ]
    coordinator = _m0_coordinator(
        stub_coordinator_factory, hass, entry, devices=devices
    )
    entry.runtime_data = SimpleNamespace(coordinator=coordinator)
    add_entities, added, pending, calls = _m0_add_entities(hass, loop)

    error = await _m0_setup(hass, entry, add_entities)
    await asyncio.gather(*pending)

    assert error is None
    assert calls == [2]
    by_serial = {
        vars(entity.device_info)["serial_number"]: entity for entity, _ in added
    }
    assert set(by_serial) == {"dev-a", "   "}
    assert vars(by_serial["dev-a"].device_info)["name"] == "Keys"
    assert vars(by_serial["   "].device_info)["name"] is None
    assert by_serial["   "].device_label() == "Google Find My Device"
