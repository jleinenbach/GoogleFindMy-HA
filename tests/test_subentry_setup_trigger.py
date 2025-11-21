# tests/test_subentry_setup_trigger.py
from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterable
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.googlefindmy import (
    _SUBENTRY_SETUP_MAX_ATTEMPTS,
    ConfigEntrySubentryDefinition,
    ConfigEntrySubEntryManager,
    RuntimeData,
    _async_create_task,
    _async_setup_new_subentries,
    _async_setup_subentry,
    _async_unload_parent_entry,
)
from custom_components.googlefindmy.const import (
    DOMAIN,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
)
from custom_components.googlefindmy.entity import schedule_add_entities
from tests.helpers.homeassistant import (
    FakeConfigEntriesManager,
    FakeConfigEntry,
    FakeDeviceEntry,
    FakeDeviceRegistry,
    FakeEntityRegistry,
    FakeHass,
)


def _attach_runtime(entry: FakeConfigEntry) -> RuntimeData:
    """Attach a minimal runtime data container to ``entry``."""

    runtime_data = RuntimeData(
        coordinator=SimpleNamespace(),  # type: ignore[arg-type]
        token_cache=object(),  # type: ignore[arg-type]
        subentry_manager=SimpleNamespace(),  # type: ignore[arg-type]
        fcm_receiver=None,
    )
    entry.runtime_data = runtime_data
    return runtime_data


@pytest.mark.asyncio
async def test_async_setup_subentry_inherits_parent_runtime_data() -> None:
    """Legacy child entries should continue to inherit the parent runtime data."""

    hass = FakeHass(config_entries=FakeConfigEntriesManager())
    bucket = hass.data.setdefault(DOMAIN, {})
    entries_bucket = bucket.setdefault("entries", {})

    parent_entry_id = "parent-entry"
    coordinator = object()
    runtime_data = RuntimeData(
        coordinator=coordinator,  # type: ignore[arg-type]
        token_cache=object(),  # type: ignore[arg-type]
        subentry_manager=SimpleNamespace(),  # type: ignore[arg-type]
        fcm_receiver=None,
    )
    entries_bucket[parent_entry_id] = runtime_data

    forward_called = False

    async def forward(*_: object) -> None:
        nonlocal forward_called
        forward_called = True

    hass.config_entries.async_forward_entry_setups = forward  # type: ignore[attr-defined]

    child_entry = SimpleNamespace(
        entry_id="child-entry",
        data={"group_key": "child"},
        parent_entry_id=parent_entry_id,
        config_subentry_id="child-subentry",
        subentry_type="tracker",
        runtime_data=None,
    )

    assert await _async_setup_subentry(hass, child_entry) is True
    assert child_entry.runtime_data is runtime_data
    assert forward_called is False


@pytest.mark.asyncio
async def test_async_create_task_schedules_from_worker_thread() -> None:
    """Task scheduling should marshal worker threads back to the HA loop."""

    loop = asyncio.get_running_loop()
    hass = FakeHass(config_entries=FakeConfigEntriesManager([]))
    setattr(hass, "loop", loop)

    thread_names: list[str] = []

    def _recording_async_create_task(
        coro: Any, *, name: str | None = None
    ) -> asyncio.Task[Any]:
        thread_names.append(threading.current_thread().name)
        assert name == "googlefindmy.threadsafe_test"
        return loop.create_task(coro)

    setattr(hass, "async_create_task", _recording_async_create_task)

    async def _marker() -> None:
        thread_names.append(f"coro:{threading.current_thread().name}")

    task = await loop.run_in_executor(
        None,
        lambda: _async_create_task(
            hass,
            _marker(),
            name="googlefindmy.threadsafe_test",
        ),
    )

    await task

    main_thread_name = threading.main_thread().name
    assert thread_names[0] != main_thread_name
    assert thread_names[1] == f"coro:{main_thread_name}"


@pytest.mark.asyncio
async def test_async_setup_subentry_defers_when_parent_runtime_missing() -> None:
    """Modern subentry setup should defer until the parent runtime data bucket exists."""

    hass = FakeHass(config_entries=FakeConfigEntriesManager())

    subentry = SimpleNamespace(
        entry_id="child-entry",
        parent_entry_id="missing-parent",
        config_subentry_id="tracker-subentry",
        data={"features": TRACKER_FEATURE_PLATFORMS},
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )

    with pytest.raises(ConfigEntryNotReady):
        await _async_setup_subentry(hass, subentry)


@pytest.mark.asyncio
async def test_async_setup_subentry_defers_when_coordinator_missing() -> None:
    """Modern subentry setup should defer until the parent coordinator is ready."""

    hass = FakeHass(config_entries=FakeConfigEntriesManager())
    bucket = hass.data.setdefault(DOMAIN, {})
    entries_bucket = bucket.setdefault("entries", {})

    parent_entry_id = "parent-entry"
    entries_bucket[parent_entry_id] = SimpleNamespace(coordinator=None)

    subentry = SimpleNamespace(
        entry_id="child-entry",
        parent_entry_id=parent_entry_id,
        config_subentry_id="tracker-subentry",
        data={"features": TRACKER_FEATURE_PLATFORMS},
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )

    with pytest.raises(ConfigEntryNotReady):
        await _async_setup_subentry(hass, subentry)


@pytest.mark.asyncio
async def test_async_setup_legacy_subentry_attaches_bucket_runtime_data() -> None:
    """Legacy subentry setup should pull runtime data from the parent bucket."""

    hass = FakeHass(config_entries=FakeConfigEntriesManager())
    bucket = hass.data.setdefault(DOMAIN, {})
    entries_bucket = bucket.setdefault("entries", {})

    parent_entry_id = "parent-entry"
    coordinator = object()
    runtime_data = RuntimeData(
        coordinator=coordinator,  # type: ignore[arg-type]
        token_cache=object(),  # type: ignore[arg-type]
        subentry_manager=SimpleNamespace(),  # type: ignore[arg-type]
        fcm_receiver=None,
    )
    entries_bucket[parent_entry_id] = runtime_data

    child_entry = SimpleNamespace(
        entry_id="child-entry",
        data={"group_key": "child"},
        parent_entry_id=parent_entry_id,
        subentry_type="tracker",
        runtime_data=None,
    )

    assert await _async_setup_subentry(hass, child_entry) is True
    assert child_entry.runtime_data is runtime_data


@pytest.mark.asyncio
async def test_async_setup_subentry_errors_when_unregistered(caplog: pytest.LogCaptureFixture) -> None:
    """Modern subentry setup should fail loudly if the subentry is unknown."""

    async def _async_refresh() -> None:
        return None

    caplog.set_level(logging.ERROR)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    config_entries = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=config_entries)
    hass.async_create_task = asyncio.create_task
    _attach_runtime(parent_entry)

    bucket = hass.data.setdefault(DOMAIN, {})
    entries_bucket = bucket.setdefault("entries", {})
    runtime_data = RuntimeData(
        coordinator=SimpleNamespace(
            _refresh_subentry_index=lambda: None,
            async_request_refresh=_async_refresh,
        ),  # type: ignore[arg-type]
        token_cache=object(),  # type: ignore[arg-type]
        subentry_manager=SimpleNamespace(_refresh_from_entry=lambda: None),  # type: ignore[arg-type]
        fcm_receiver=None,
    )
    entries_bucket[parent_entry.entry_id] = runtime_data

    subentry = SimpleNamespace(
        entry_id="child-entry",
        parent_entry_id=parent_entry.entry_id,
        config_subentry_id="child-entry",
        data={"features": TRACKER_FEATURE_PLATFORMS},
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )

    with pytest.raises(ConfigEntryNotReady):
        await _async_setup_subentry(hass, subentry, subentry)

    assert "not registered under parent" in " ".join(caplog.messages)


@pytest.mark.asyncio
async def test_async_setup_new_subentries_retries_unknown_and_reschedules(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UnknownEntry should clear the tracker and auto-schedule a retry."""

    caplog.set_level(logging.WARNING)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-entry",
        unique_id="child-entry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )
    parent_entry.subentries[subentry.subentry_id] = subentry

    config_entries = FakeConfigEntriesManager([parent_entry])
    config_entries.set_transient_unknown_entry(subentry.entry_id, setup_failures=1)
    hass = FakeHass(config_entries=config_entries)

    runtime_data = _attach_runtime(parent_entry)

    scheduled_callbacks: list[Callable[[Any], None]] = []

    class _Handle:
        def __init__(self) -> None:
            self._cancelled = False

        def cancel(self) -> None:
            self._cancelled = True

    def _fake_async_call_later(
        _hass: FakeHass, _delay: float, callback: Callable[[Any], None]
    ) -> _Handle:
        scheduled_callbacks.append(callback)
        return _Handle()

    monkeypatch.setattr(
        "custom_components.googlefindmy.async_call_later", _fake_async_call_later
    )

    tasks: list[asyncio.Task[Any]] = []

    def _capture_task(
        coro: Any, *, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    hass.async_create_task = _capture_task

    await _async_setup_new_subentries(
        hass,
        parent_entry,
        [subentry],
    )

    assert runtime_data.subentry_retry_attempts == {}
    assert runtime_data.subentry_retry_handles == {}
    assert scheduled_callbacks == []
    assert config_entries.setup_calls == [subentry.entry_id]


@pytest.mark.asyncio
async def test_async_setup_new_subentries_stops_after_retry_limit(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retries should stop after the configured attempt limit."""

    caplog.set_level(logging.WARNING)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-entry",
        unique_id="child-entry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )
    parent_entry.subentries[subentry.subentry_id] = subentry

    config_entries = FakeConfigEntriesManager([parent_entry])
    config_entries.set_transient_unknown_entry(
        subentry.entry_id, setup_failures=_SUBENTRY_SETUP_MAX_ATTEMPTS + 2
    )
    hass = FakeHass(config_entries=config_entries)

    runtime_data = _attach_runtime(parent_entry)

    scheduled_callbacks: list[Callable[[Any], None]] = []

    def _fake_async_call_later(
        _hass: FakeHass, _delay: float, callback: Callable[[Any], None]
    ) -> SimpleNamespace:
        scheduled_callbacks.append(callback)
        return SimpleNamespace(cancel=lambda: None)

    monkeypatch.setattr(
        "custom_components.googlefindmy.async_call_later", _fake_async_call_later
    )

    tasks: list[asyncio.Task[Any]] = []

    def _capture_task(
        coro: Any, *, name: str | None = None
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    hass.async_create_task = _capture_task

    await _async_setup_new_subentries(hass, parent_entry, [subentry])

    assert scheduled_callbacks == []
    assert config_entries.setup_calls == [subentry.entry_id]
    assert runtime_data.subentry_retry_attempts == {}
    assert runtime_data.subentry_retry_handles == {}


@pytest.mark.asyncio
async def test_subentry_manager_retries_unknown_entry_without_enforcement() -> None:
    """ConfigEntrySubEntryManager should swallow UnknownEntry and retry later."""

    tracker_key = "tracker"
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    class _RetryingConfigEntriesManager(FakeConfigEntriesManager):
        def __init__(self) -> None:
            super().__init__([parent_entry])

        def async_add_subentry(self, entry: FakeConfigEntry, subentry: Any) -> Any:
            subentry_id = getattr(subentry, "subentry_id", None)
            if not isinstance(subentry_id, str) or not subentry_id:
                subentry_id = f"{entry.entry_id}:{tracker_key}"
                setattr(subentry, "subentry_id", subentry_id)
            entry.subentries[subentry_id] = subentry
            entry._registered_subentry_ids.add(subentry_id)
            self.set_transient_unknown_entry(subentry_id, setup_failures=1)
            return subentry

    config_entries = _RetryingConfigEntriesManager()
    hass = FakeHass(config_entries=config_entries)
    hass.async_create_task = asyncio.create_task
    _attach_runtime(parent_entry)
    manager = ConfigEntrySubEntryManager(hass, parent_entry)

    definition = ConfigEntrySubentryDefinition(
        key=tracker_key,
        title="Tracker devices",
        data={"group_key": tracker_key},
    )

    await manager.async_sync([definition])
    assert tracker_key in manager._managed
    assert len(config_entries.setup_calls) == 1
    scheduled_subentry_id = config_entries.setup_calls[0]
    assert isinstance(scheduled_subentry_id, str) and scheduled_subentry_id

    await _async_setup_new_subentries(
        hass,
        parent_entry,
        list(manager._managed.values()),
    )

    assert config_entries.setup_calls == [
        scheduled_subentry_id,
        scheduled_subentry_id,
    ]


@pytest.mark.asyncio
async def test_async_setup_new_subentries_enforces_registered_subentries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Subentry scheduling should warn when the parent entry lacks the subentry."""

    caplog.set_level(logging.WARNING)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    config_entries = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=config_entries)
    _attach_runtime(parent_entry)

    orphan_subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-entry",
        unique_id="child-entry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )

    await _async_setup_new_subentries(
        hass,
        parent_entry,
        [orphan_subentry],
        enforce_registration=True,
    )

    assert config_entries.setup_calls == [orphan_subentry.entry_id]


@pytest.mark.asyncio
async def test_async_setup_new_subentries_warns_but_schedules_when_unregistered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing parent membership should not block scheduling when enforcement is off."""

    caplog.set_level(logging.WARNING)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    config_entries = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=config_entries)

    orphan_subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-entry",
        unique_id="child-entry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )

    await _async_setup_new_subentries(hass, parent_entry, [orphan_subentry])

    assert orphan_subentry.entry_id in config_entries.setup_calls


@pytest.mark.asyncio
async def test_async_setup_new_subentries_requires_registered_subentries(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing registry visibility should warn even when enforcement is enabled."""

    caplog.set_level(logging.WARNING)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    config_entries = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=config_entries)

    orphan_subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-entry",
        unique_id="child-entry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )
    parent_entry.subentries[orphan_subentry.subentry_id] = orphan_subentry

    monkeypatch.setattr(
        "custom_components.googlefindmy._registered_subentry_ids", lambda *_: set()
    )

    await _async_setup_new_subentries(
        hass,
        parent_entry,
        [orphan_subentry],
        enforce_registration=True,
    )

    assert orphan_subentry.entry_id in config_entries.setup_calls


@pytest.mark.asyncio
async def test_async_setup_new_subentries_requires_fallback_parent_membership(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Fallback identifiers should not block scheduling valid parent subentries."""

    caplog.set_level(logging.WARNING)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    parent_entry.subentries["child-subentry"] = {"subentry_id": "child-subentry"}

    config_entries = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=config_entries)
    _attach_runtime(parent_entry)

    mixed_identifier_subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-subentry",
        unique_id="child-subentry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )

    await _async_setup_new_subentries(hass, parent_entry, [mixed_identifier_subentry])

    assert "child-subentry" in config_entries.setup_calls
    assert not any(
        "child-entry" in message and "skipping" not in message
        for message in caplog.messages
    )


@pytest.mark.asyncio
async def test_async_setup_new_subentries_requires_registration_when_not_enforced(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing registered subentries should warn without blocking scheduling."""

    caplog.set_level(logging.WARNING)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    registered_subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-entry",
        unique_id="child-entry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )
    parent_entry.subentries[registered_subentry.subentry_id] = registered_subentry

    config_entries = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=config_entries)
    _attach_runtime(parent_entry)

    monkeypatch.setattr(
        "custom_components.googlefindmy._registered_subentry_ids",
        lambda _hass, _entry: set(),
    )

    await _async_setup_new_subentries(hass, parent_entry, [registered_subentry])

    assert registered_subentry.entry_id in config_entries.setup_calls


@pytest.mark.asyncio
async def test_async_setup_new_subentries_requires_registration_when_enforced() -> None:
    """Subentry scheduling should proceed when the registry exposes the subentry."""

    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    registered_subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-entry",
        unique_id="child-entry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )
    parent_entry.subentries[registered_subentry.subentry_id] = registered_subentry

    config_entries = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=config_entries)
    _attach_runtime(parent_entry)

    await _async_setup_new_subentries(
        hass,
        parent_entry,
        [registered_subentry],
        enforce_registration=True,
    )

    assert registered_subentry.entry_id in config_entries.setup_calls


@pytest.mark.asyncio
async def test_async_setup_new_subentries_links_entities_and_devices(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Schedule setup for registered subentries and propagate config_subentry_id."""

    caplog.set_level(logging.DEBUG)
    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    registered_subentry = SimpleNamespace(
        entry_id="child-entry",
        subentry_id="child-entry",
        unique_id="child-entry",
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )
    parent_entry.subentries[registered_subentry.subentry_id] = registered_subentry

    config_entries = FakeConfigEntriesManager([parent_entry])
    _attach_runtime(parent_entry)
    hass = SimpleNamespace(
        config_entries=config_entries,
        data={},
        async_create_task=lambda coro, name=None: asyncio.create_task(coro),
    )

    device_registry = FakeDeviceRegistry(
        [
            FakeDeviceEntry(
                id="device-id",
                identifiers={(DOMAIN, "device-id")},
                config_entries={parent_entry.entry_id},
            )
        ]
    )
    entity_registry = FakeEntityRegistry()

    async def _async_add_entities(
        entities: Iterable[Any],
        update_before_add: bool = True,
        *,
        config_subentry_id: str | None = None,
    ) -> None:
        del update_before_add
        for entity in entities:
            entity_registry.entities[entity.entity_id] = SimpleNamespace(
                entity_id=entity.entity_id,
                config_entry_id=parent_entry.entry_id,
                config_subentry_id=config_subentry_id,
            )
            device_registry.async_update_device(
                entity.device_info.id,
                config_subentry_id=config_subentry_id,
            )

    class _StubEntity:
        def __init__(self, entity_id: str) -> None:
            self.entity_id = entity_id
            self._device_info = SimpleNamespace(
                id="device-id",
                identifiers={(DOMAIN, "device-id")},
                config_entries={parent_entry.entry_id},
            )

        @property
        def device_info(self) -> Any:
            return self._device_info

    await _async_setup_new_subentries(
        hass,
        parent_entry,
        [registered_subentry],
        enforce_registration=True,
    )

    schedule_add_entities(
        hass,
        _async_add_entities,
        entities=[_StubEntity("sensor.child")],
        update_before_add=True,
        config_subentry_id=registered_subentry.subentry_id,
        log_owner="Subentry schedule test",
        logger=logging.getLogger(__name__),
    )
    await asyncio.sleep(0)

    entity_entry = entity_registry.entities.get("sensor.child")
    device_entry = device_registry.devices.get("device-id")

    assert registered_subentry.entry_id in config_entries.setup_calls
    assert entity_entry is not None
    assert entity_entry.config_subentry_id == registered_subentry.subentry_id
    assert device_entry is not None
    assert device_entry.config_subentry_id == registered_subentry.subentry_id
    assert any(
        "Forwarded setup for config subentry" in message
        for message in caplog.messages
    )


@pytest.mark.asyncio
async def test_async_unload_entry_cancels_retry_handles() -> None:
    """Scheduled retry handles should be cancelled during parent unload."""

    parent_entry = FakeConfigEntry(entry_id="parent-entry")
    runtime_data = _attach_runtime(parent_entry)
    runtime_data.subentry_retry_attempts[parent_entry.entry_id] = {"child-entry": 2}

    class _Handle:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    handle = _Handle()
    runtime_data.subentry_retry_handles[parent_entry.entry_id] = handle

    config_entries = FakeConfigEntriesManager([parent_entry])
    config_entries.async_unload_platforms = lambda entry, platforms: True  # type: ignore[attr-defined]
    config_entries.async_forward_entry_unload = (  # type: ignore[attr-defined]
        lambda entry, platform, **kwargs: True
    )
    hass = FakeHass(config_entries=config_entries)

    assert await _async_unload_parent_entry(hass, parent_entry) is True
    assert handle.cancelled is True
    assert runtime_data.subentry_retry_handles == {}
    assert runtime_data.subentry_retry_attempts == {}
