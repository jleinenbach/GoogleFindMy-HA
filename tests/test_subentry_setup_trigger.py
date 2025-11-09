# tests/test_subentry_setup_trigger.py
"""Regression coverage for subentry setup helpers."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.googlefindmy import (
    MAX_SUBENTRY_REGISTRATION_ATTEMPTS,
    RuntimeData,
    _async_ensure_subentries_are_setup,
    _async_setup_subentry,
    _domain_data,
    _ensure_entries_bucket,
)
from custom_components.googlefindmy.const import DOMAIN
from custom_components.googlefindmy.entity import resolve_coordinator

from tests.helpers.homeassistant import (
    FakeConfigEntriesManager,
    FakeHass,
    config_entry_with_runtime_managed_subentries,
)


@pytest.mark.asyncio
async def test_async_setup_subentry_inherits_parent_runtime_data() -> None:
    """Child setup should reuse the parent's runtime data bucket."""

    hass = FakeHass(config_entries=FakeConfigEntriesManager())
    bucket = _domain_data(hass)
    entries_bucket = _ensure_entries_bucket(bucket)

    parent_entry_id = "parent-entry"
    coordinator = object()
    runtime_data = RuntimeData(
        coordinator=coordinator,  # type: ignore[arg-type]
        token_cache=object(),  # type: ignore[arg-type]
        subentry_manager=SimpleNamespace(),  # type: ignore[arg-type]
        fcm_receiver=None,
        google_home_filter=None,
    )
    entries_bucket[parent_entry_id] = runtime_data

    forward_calls: list[tuple[object, tuple[object, ...]]] = []

    async def forward(entry: SimpleNamespace, platforms: list[object]) -> None:
        forward_calls.append((entry, tuple(platforms)))
        assert entry.runtime_data is runtime_data

    hass.config_entries.async_forward_entry_setups = forward  # type: ignore[attr-defined]

    child_entry = SimpleNamespace(
        entry_id="child-entry",
        data={"group_key": "child"},
        parent_entry_id=parent_entry_id,
        subentry_type="tracker",
        runtime_data=None,
    )

    assert await _async_setup_subentry(hass, child_entry) is True
    assert child_entry.runtime_data is runtime_data
    assert resolve_coordinator(child_entry) is coordinator
    assert forward_calls, "Expected platform forwarding during setup"


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_schedules_all_children() -> None:
    """All discovered subentries should be scheduled for setup."""

    pending_subentry = SimpleNamespace(
        entry_id="child-pending",
        subentry_id="child-pending",
    )
    active_subentry = SimpleNamespace(
        entry_id="child-active",
        subentry_id="child-active",
    )
    disabled_subentry = SimpleNamespace(
        entry_id="child-disabled",
        subentry_id="child-disabled",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries=[pending_subentry, active_subentry, disabled_subentry],
    )

    manager = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=manager)

    await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.setup_calls == [
        pending_subentry.entry_id,
        active_subentry.entry_id,
        disabled_subentry.entry_id,
    ]


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_warns_and_raises_on_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log a warning and raise ConfigEntryNotReady when subentry setup fails."""

    successful_subentry = SimpleNamespace(
        entry_id="child-success",
        subentry_id="child-success",
    )
    failing_subentry = SimpleNamespace(
        entry_id="child-failure",
        subentry_id="child-failure",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries=[successful_subentry, failing_subentry],
    )

    manager = FakeConfigEntriesManager([parent_entry])

    async def failing_setup(entry_id: str) -> bool:
        manager.setup_calls.append(entry_id)
        return entry_id != failing_subentry.entry_id

    manager.async_setup = failing_setup  # type: ignore[assignment]
    hass = FakeHass(config_entries=manager)

    with caplog.at_level(logging.WARNING), pytest.raises(ConfigEntryNotReady):
        await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.setup_calls == [
        successful_subentry.entry_id,
        failing_subentry.entry_id,
    ]
    assert any(
        "setup returned False" in record.getMessage()
        and failing_subentry.entry_id in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_id_value", [None, "", object()])
async def test_async_ensure_subentries_are_setup_falls_back_to_subentry_id(
    entry_id_value: object,
) -> None:
    """Fresh subentries resolve identifiers when entry_id is missing or invalid."""

    pending_subentry = SimpleNamespace(
        entry_id=entry_id_value,
        subentry_id="child-created",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries={pending_subentry.subentry_id: pending_subentry},
    )

    manager = FakeConfigEntriesManager([parent_entry])
    hass = FakeHass(config_entries=manager)

    await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.setup_calls == [pending_subentry.subentry_id]


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_retries_missing_child() -> None:
    """Late-registered subentries should be retried until setup succeeds."""

    pending_subentry = SimpleNamespace(
        entry_id="child-retry",
        subentry_id="child-retry",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries=[pending_subentry],
    )

    manager = FakeConfigEntriesManager([parent_entry])
    manager.set_transient_unknown_entry(
        pending_subentry.entry_id,
        lookup_misses=2,
        setup_failures=1,
    )
    hass = FakeHass(config_entries=manager)

    await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.lookup_attempts[pending_subentry.entry_id] >= 3
    assert manager.setup_calls == [pending_subentry.entry_id, pending_subentry.entry_id]


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_raises_when_child_never_registers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Raise ConfigEntryNotReady when a subentry exhausts registration retries."""

    missing_subentry = SimpleNamespace(
        entry_id="child-missing",
        subentry_id="child-missing",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries=[missing_subentry],
    )

    manager = FakeConfigEntriesManager([parent_entry])
    manager.set_transient_unknown_entry(
        missing_subentry.entry_id,
        lookup_misses=MAX_SUBENTRY_REGISTRATION_ATTEMPTS + 1,
    )
    hass = FakeHass(config_entries=manager)

    with caplog.at_level(logging.WARNING), pytest.raises(ConfigEntryNotReady) as exc:
        await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert missing_subentry.entry_id not in manager.setup_calls
    assert str(MAX_SUBENTRY_REGISTRATION_ATTEMPTS) in str(exc.value)
    assert any(
        "not registered" in record.getMessage()
        and missing_subentry.entry_id in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_preserves_first_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Propagate the first captured exception even when retries exhaust."""

    failing_subentry = SimpleNamespace(
        entry_id="child-failure",
        subentry_id="child-failure",
    )
    missing_subentry = SimpleNamespace(
        entry_id="child-missing",
        subentry_id="child-missing",
    )

    parent_entry = config_entry_with_runtime_managed_subentries(
        entry_id="parent",
        domain=DOMAIN,
        subentries=[failing_subentry, missing_subentry],
    )

    manager = FakeConfigEntriesManager([parent_entry])
    manager.set_transient_unknown_entry(
        missing_subentry.entry_id,
        lookup_misses=MAX_SUBENTRY_REGISTRATION_ATTEMPTS + 1,
    )

    async def raising_setup(entry_id: str) -> bool:
        manager.setup_calls.append(entry_id)
        if entry_id == failing_subentry.entry_id:
            raise RuntimeError("boom")
        return True

    manager.async_setup = raising_setup  # type: ignore[assignment]
    hass = FakeHass(config_entries=manager)

    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
        await _async_ensure_subentries_are_setup(hass, parent_entry)

    assert manager.setup_calls == [failing_subentry.entry_id]
    assert any(
        "not registered" in record.getMessage()
        and missing_subentry.entry_id in record.getMessage()
        for record in caplog.records
    )
