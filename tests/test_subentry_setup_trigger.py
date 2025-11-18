# tests/test_subentry_setup_trigger.py
from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.googlefindmy import (
    RuntimeData,
    _async_setup_new_subentries,
    _async_setup_subentry,
    _subentry_setup_tracker,
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
async def test_async_setup_new_subentries_retries_unknown_and_clears_tracker(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """UnknownEntry should clear the tracker so setup can retry once registered."""

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

    with pytest.raises(ConfigEntryNotReady):
        await _async_setup_new_subentries(
            hass,
            parent_entry,
            [subentry],
            enforce_registration=True,
        )

    tracker = _subentry_setup_tracker(hass, parent_entry)
    assert subentry.entry_id not in tracker

    await _async_setup_new_subentries(
        hass,
        parent_entry,
        [subentry],
        enforce_registration=True,
    )

    assert config_entries.setup_calls == [subentry.entry_id, subentry.entry_id]
    assert any("not yet registered" in message for message in caplog.messages)


@pytest.mark.asyncio
async def test_async_setup_new_subentries_enforces_registered_subentries(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Subentry scheduling should warn when the parent entry lacks the subentry."""

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

    await _async_setup_new_subentries(
        hass,
        parent_entry,
        [orphan_subentry],
        enforce_registration=True,
    )

    assert config_entries.setup_calls == []
    assert any(
        "Subentry child-entry not registered for entry" in message
        for message in caplog.messages
    )


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
    assert any(
        "Subentry child-entry not registered for entry parent-entry" in message
        for message in caplog.messages
    )


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
    assert "No registered config subentries visible" in " ".join(caplog.messages)


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

    monkeypatch.setattr(
        "custom_components.googlefindmy._registered_subentry_ids",
        lambda _hass, _entry: set(),
    )

    await _async_setup_new_subentries(hass, parent_entry, [registered_subentry])

    assert registered_subentry.entry_id in config_entries.setup_calls
    assert any(
        "No registered config subentries visible" in message
        for message in caplog.messages
    )


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
    assert any("Scheduled setup for config subentry" in message for message in caplog.messages)
