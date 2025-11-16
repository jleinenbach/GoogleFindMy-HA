from __future__ import annotations

from types import SimpleNamespace

import pytest

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.googlefindmy import (
    RuntimeData,
    _async_ensure_subentries_are_setup,
    _async_setup_subentry,
    _platform_value,
    SUBENTRY_FORWARD_HELPER_LOG_KEY,
)
from custom_components.googlefindmy.const import (
    DOMAIN,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
)

from tests.helpers.homeassistant import FakeConfigEntriesManager, FakeConfigEntry, FakeHass


def _platform_names(platforms: tuple[object, ...]) -> tuple[str, ...]:
    """Return normalized platform names for assertions."""

    names = [_platform_value(platform) for platform in platforms]
    return tuple(names)


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

    forward_calls: list[tuple[object, tuple[Platform, ...]]] = []

    async def forward(entry: SimpleNamespace, platforms: list[Platform], *, config_subentry_id: str | None = None) -> None:
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
    assert forward_calls


@pytest.mark.asyncio
async def test_async_setup_subentry_forwards_platforms_via_plural_helper() -> None:
    """Dataclass subentries should forward their required platforms once."""

    hass = FakeHass(config_entries=FakeConfigEntriesManager())
    entry = FakeConfigEntry(entry_id="parent", domain=DOMAIN)

    subentry = SimpleNamespace(
        config_subentry_id="service-subentry",
        data={"features": SERVICE_FEATURE_PLATFORMS},
        subentry_type=SUBENTRY_TYPE_SERVICE,
    )

    calls: list[tuple[FakeConfigEntry, tuple[object, ...]]] = []

    async def forward(entry_obj: FakeConfigEntry, platforms: list[Platform]) -> None:
        calls.append((entry_obj, tuple(platforms)))

    hass.config_entries.async_forward_entry_setups = forward  # type: ignore[attr-defined]

    result = await _async_setup_subentry(hass, entry, subentry)

    assert result is True
    assert calls == [(entry, tuple(SERVICE_FEATURE_PLATFORMS))]


@pytest.mark.asyncio
async def test_async_setup_subentry_logs_when_forward_helper_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing forward helpers should be logged and skip setup."""

    hass = FakeHass(config_entries=FakeConfigEntriesManager())
    entry = FakeConfigEntry(entry_id="parent", domain=DOMAIN)

    subentry = SimpleNamespace(
        config_subentry_id="tracker-subentry",
        data={"features": TRACKER_FEATURE_PLATFORMS},
        subentry_type=SUBENTRY_TYPE_TRACKER,
    )

    hass.config_entries.async_forward_entry_setups = None  # type: ignore[attr-defined]

    with caplog.at_level("INFO"):
        result = await _async_setup_subentry(hass, entry, subentry)

    assert result is False
    assert "does not expose 'async_forward_entry_setups'" in caplog.text
    logged = hass.data[DOMAIN].get(SUBENTRY_FORWARD_HELPER_LOG_KEY)
    assert isinstance(logged, set) and entry.entry_id in logged


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_collects_all_sources() -> None:
    """Managed, entry, and metadata subentries should all be forwarded once."""

    tracker_subentry = SimpleNamespace(
        config_subentry_id="tracker-subentry",
        data={"features": TRACKER_FEATURE_PLATFORMS},
        subentry_type=SUBENTRY_TYPE_TRACKER,
        key=TRACKER_SUBENTRY_KEY,
    )
    service_metadata = SimpleNamespace(
        config_subentry_id="service-subentry",
        features=tuple(SERVICE_FEATURE_PLATFORMS),
        key=SERVICE_SUBENTRY_KEY,
    )

    managed = {TRACKER_SUBENTRY_KEY: tracker_subentry}
    subentry_manager = SimpleNamespace(managed_subentries=managed)
    coordinator = SimpleNamespace(_subentry_metadata={SERVICE_SUBENTRY_KEY: service_metadata})
    runtime_data = RuntimeData(
        coordinator=coordinator,  # type: ignore[arg-type]
        token_cache=object(),  # type: ignore[arg-type]
        subentry_manager=subentry_manager,  # type: ignore[arg-type]
        fcm_receiver=None,
    )

    entry = FakeConfigEntry(entry_id="parent", domain=DOMAIN)
    entry.runtime_data = runtime_data
    entry.subentries[TRACKER_SUBENTRY_KEY] = tracker_subentry

    hass = FakeHass(config_entries=FakeConfigEntriesManager([entry]))

    calls: list[tuple[FakeConfigEntry, tuple[Platform, ...]]] = []

    async def forward(entry_obj: FakeConfigEntry, platforms: list[Platform]) -> None:
        calls.append((entry_obj, tuple(platforms)))

    hass.config_entries.async_forward_entry_setups = forward  # type: ignore[attr-defined]

    await _async_ensure_subentries_are_setup(hass, entry)

    assert calls
    recorded_entry, recorded_platforms = calls[0]
    assert recorded_entry is entry
    expected_order: list[str] = []
    for name in (*TRACKER_FEATURE_PLATFORMS, *SERVICE_FEATURE_PLATFORMS):
        if name not in expected_order:
            expected_order.append(name)
    assert _platform_names(recorded_platforms) == tuple(expected_order)


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_requires_forward_helper(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing forward helpers should emit an error and abort setup."""

    tracker_subentry = SimpleNamespace(
        config_subentry_id="tracker-subentry",
        data={"features": TRACKER_FEATURE_PLATFORMS},
        subentry_type=SUBENTRY_TYPE_TRACKER,
        key=TRACKER_SUBENTRY_KEY,
    )

    subentry_manager = SimpleNamespace(
        managed_subentries={TRACKER_SUBENTRY_KEY: tracker_subentry}
    )
    runtime_data = RuntimeData(
        coordinator=SimpleNamespace(_subentry_metadata={}),  # type: ignore[arg-type]
        token_cache=object(),  # type: ignore[arg-type]
        subentry_manager=subentry_manager,  # type: ignore[arg-type]
        fcm_receiver=None,
    )

    entry = FakeConfigEntry(entry_id="parent", domain=DOMAIN)
    entry.runtime_data = runtime_data

    hass = FakeHass(config_entries=FakeConfigEntriesManager([entry]))
    hass.config_entries.async_forward_entry_setups = None  # type: ignore[attr-defined]

    with caplog.at_level("INFO"):
        await _async_ensure_subentries_are_setup(hass, entry)

    assert "does not expose 'async_forward_entry_setups'" in caplog.text
    logged = hass.data[DOMAIN].get(SUBENTRY_FORWARD_HELPER_LOG_KEY)
    assert isinstance(logged, set) and entry.entry_id in logged


@pytest.mark.asyncio
async def test_async_ensure_subentries_are_setup_logs_config_entry_not_ready(caplog: pytest.LogCaptureFixture) -> None:
    """ConfigEntryNotReady exceptions should be caught and logged without raising."""

    subentry = SimpleNamespace(
        config_subentry_id="tracker-subentry",
        data={"features": TRACKER_FEATURE_PLATFORMS},
        subentry_type=SUBENTRY_TYPE_TRACKER,
        key=TRACKER_SUBENTRY_KEY,
    )

    subentry_manager = SimpleNamespace(managed_subentries={TRACKER_SUBENTRY_KEY: subentry})
    runtime_data = RuntimeData(
        coordinator=SimpleNamespace(_subentry_metadata={}),  # type: ignore[arg-type]
        token_cache=object(),  # type: ignore[arg-type]
        subentry_manager=subentry_manager,  # type: ignore[arg-type]
        fcm_receiver=None,
    )

    entry = FakeConfigEntry(entry_id="parent", domain=DOMAIN)
    entry.runtime_data = runtime_data

    hass = FakeHass(config_entries=FakeConfigEntriesManager([entry]))

    async def forward(*_: object, **__kwargs: object) -> None:
        raise ConfigEntryNotReady("test")

    hass.config_entries.async_forward_entry_setups = forward  # type: ignore[attr-defined]

    with caplog.at_level("ERROR"):
        await _async_ensure_subentries_are_setup(hass, entry)

    assert "Aggregated subentry setup raised an unexpected error: test" in caplog.text
