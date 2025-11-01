# tests/test_config_flow_subentry_sync.py
"""Tests validating config flow subentry creation and updates."""

from __future__ import annotations

# tests/test_config_flow_subentry_sync.py

import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AUTH_METHOD,
    DEFAULT_ENABLE_STATS_ENTITIES,
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    DOMAIN,
    OPT_DEVICE_POLL_DELAY,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_IGNORED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_OPTIONS_SCHEMA_VERSION,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_TRACKER,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
)
from homeassistant.config_entries import ConfigSubentry


def _stable_subentry_id(entry_id: str, key: str) -> str:
    """Return a deterministic config_subentry_id for the given entry/key pair."""

    return f"{entry_id}-{key}-subentry"


class _ConfigEntriesManagerStub:
    """Stub mimicking Home Assistant's config entries manager."""

    def __init__(self, entry: _EntryStub) -> None:
        self._entry = entry
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []
        self.entry_updates: list[dict[str, Any]] = []

    def async_entries(self, domain: str | None = None) -> list[Any]:
        if domain and domain != DOMAIN:
            return []
        return [self._entry]

    def async_get_entry(self, entry_id: str) -> _EntryStub | None:
        if entry_id == self._entry.entry_id:
            return self._entry
        return None

    def async_update_entry(self, entry: _EntryStub, **kwargs: Any) -> None:
        assert entry is self._entry
        payload = dict(kwargs)
        self.entry_updates.append(payload)
        if "data" in payload:
            entry.data = payload["data"]
        if "options" in payload:
            entry.options = payload["options"]
        if "version" in payload:
            entry.version = payload["version"]

    def async_create_subentry(
        self,
        entry: _EntryStub,
        *,
        data: dict[str, Any],
        title: str,
        unique_id: str | None,
        subentry_type: str,
    ) -> ConfigSubentry:
        assert entry is self._entry
        subentry = ConfigSubentry(
            data=MappingProxyType(dict(data)),
            subentry_type=subentry_type,
            title=title,
            unique_id=unique_id,
            subentry_id=_stable_subentry_id(entry.entry_id, data["group_key"]),
        )
        self._entry.subentries[subentry.subentry_id] = subentry
        self.created.append(
            {
                "data": dict(data),
                "title": title,
                "unique_id": unique_id,
                "subentry_type": subentry_type,
                "config_subentry_id": subentry.subentry_id,
                "object": subentry,
            }
        )
        return subentry

    def async_update_subentry(
        self,
        entry: _EntryStub,
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any],
        title: str | None = None,
        unique_id: str | None = None,
    ) -> None:
        assert entry is self._entry
        subentry.data = MappingProxyType(dict(data))
        if title is not None:
            subentry.title = title
        if unique_id is not None:
            subentry.unique_id = unique_id
        self.updated.append(
            {
                "data": dict(data),
                "title": title,
                "unique_id": unique_id,
                "config_subentry_id": subentry.subentry_id,
                "subentry": subentry,
            }
        )


class _HassStub:
    """Home Assistant stub exposing config entry helpers to the flow."""

    def __init__(self, entry: _EntryStub) -> None:
        self.config_entries = _ConfigEntriesManagerStub(entry)
        self.data: dict[str, Any] = {DOMAIN: {"entries": {entry.entry_id: entry}}}

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)


class _EntryStub:
    """Lightweight config entry stub with mutable subentries."""

    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.title = "Find My"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.subentries: dict[str, ConfigSubentry] = {}
        self.runtime_data = SimpleNamespace()
        self.version = 1


def _build_flow(entry: _EntryStub) -> config_flow.ConfigFlow:
    flow = config_flow.ConfigFlow()
    hass = _HassStub(entry)
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"entry_id": entry.entry_id}
    flow._auth_data = {
        DATA_AUTH_METHOD: "manual",
        CONF_OAUTH_TOKEN: "token",
        CONF_GOOGLE_EMAIL: "owner@example.com",
    }
    flow._available_devices = [("Device", "dev-1")]
    flow.unique_id = None  # type: ignore[attribute-defined-outside-init]
    flow._unique_id = None  # type: ignore[attr-defined]

    async def _set_unique_id(value: str | None) -> None:
        flow._unique_id = value  # type: ignore[attr-defined]

    flow.async_set_unique_id = _set_unique_id  # type: ignore[assignment]
    flow._abort_if_unique_id_configured = lambda: None  # type: ignore[attr-defined]
    return flow


@pytest.mark.asyncio
async def test_device_selection_creates_feature_groups_with_flags() -> None:
    """Sync helper should create service and tracker subentries with expected flags."""

    entry = _EntryStub()
    flow = _build_flow(entry)
    context_map = flow._ensure_subentry_context()

    await flow._async_sync_feature_subentries(  # type: ignore[attr-defined]
        entry,
        options_payload={
            OPT_MAP_VIEW_TOKEN_EXPIRATION: False,
            OPT_GOOGLE_HOME_FILTER_ENABLED: False,
            OPT_ENABLE_STATS_ENTITIES: True,
        },
        defaults={
            OPT_GOOGLE_HOME_FILTER_ENABLED: DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
            OPT_ENABLE_STATS_ENTITIES: DEFAULT_ENABLE_STATS_ENTITIES,
        },
        context_map=context_map,
    )
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert len(manager.created) == 2, "both service and tracker subentries should be created"

    def _record_for(key: str) -> dict[str, Any]:
        for record in manager.created:
            if record["data"]["group_key"] == key:
                return record
        raise AssertionError(f"Subentry with key {key} not created")

    service_record = _record_for(SERVICE_SUBENTRY_KEY)
    tracker_record = _record_for(TRACKER_SUBENTRY_KEY)

    service_payload = service_record["data"]
    tracker_payload = tracker_record["data"]

    assert service_record["subentry_type"] == SUBENTRY_TYPE_SERVICE
    assert tracker_record["subentry_type"] == SUBENTRY_TYPE_TRACKER
    assert service_record["unique_id"] == f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"

    assert service_payload["features"] == sorted(SERVICE_FEATURE_PLATFORMS)
    assert "visible_device_ids" not in service_payload

    assert tracker_payload["features"] == sorted(TRACKER_FEATURE_PLATFORMS)
    assert all(isinstance(feature, str) for feature in tracker_payload["features"])
    assert all(feature == feature.lower() for feature in tracker_payload["features"])
    assert tracker_payload["visible_device_ids"] == ["dev-1"]

    assert tracker_payload["has_google_home_filter"] is False
    flags = tracker_payload["feature_flags"]
    assert flags[OPT_MAP_VIEW_TOKEN_EXPIRATION] is False
    assert flags[OPT_GOOGLE_HOME_FILTER_ENABLED] is False
    assert flags[OPT_ENABLE_STATS_ENTITIES] is True


@pytest.mark.asyncio
async def test_device_selection_updates_existing_feature_group() -> None:
    """Sync helper should update an existing subentry with new feature flags."""

    entry = _EntryStub()
    existing = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": TRACKER_SUBENTRY_KEY,
                "feature_flags": {},
            }
        ),
        subentry_type=SUBENTRY_TYPE_TRACKER,
        title="Google Find My devices",
        unique_id=f"{entry.entry_id}-{TRACKER_SUBENTRY_KEY}",
        subentry_id=_stable_subentry_id(entry.entry_id, TRACKER_SUBENTRY_KEY),
    )
    entry.subentries[existing.subentry_id] = existing

    flow = _build_flow(entry)
    context_map = flow._ensure_subentry_context()
    context_map[TRACKER_SUBENTRY_KEY] = existing.subentry_id

    await flow._async_sync_feature_subentries(  # type: ignore[attr-defined]
        entry,
        options_payload={
            OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
            OPT_GOOGLE_HOME_FILTER_ENABLED: True,
            OPT_ENABLE_STATS_ENTITIES: False,
        },
        defaults={
            OPT_GOOGLE_HOME_FILTER_ENABLED: DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
            OPT_ENABLE_STATS_ENTITIES: DEFAULT_ENABLE_STATS_ENTITIES,
        },
        context_map=context_map,
    )
    manager = flow.hass.config_entries  # type: ignore[assignment]
    # Service subentry should have been created alongside updating the tracker
    created_service = next(
        record for record in manager.created if record["subentry_type"] == SUBENTRY_TYPE_SERVICE
    )
    assert created_service["data"]["group_key"] == SERVICE_SUBENTRY_KEY
    assert created_service["unique_id"] == f"{entry.entry_id}-{SERVICE_SUBENTRY_KEY}"

    assert manager.updated, "tracker subentry should be updated"
    payload = manager.updated[-1]["data"]
    assert payload["group_key"] == TRACKER_SUBENTRY_KEY
    assert payload["has_google_home_filter"] is True
    flags = payload["feature_flags"]
    assert flags[OPT_MAP_VIEW_TOKEN_EXPIRATION] is True
    assert flags[OPT_GOOGLE_HOME_FILTER_ENABLED] is True
    assert flags[OPT_ENABLE_STATS_ENTITIES] is False


def test_supported_subentry_types_include_hub_handler() -> None:
    """Config flow must expose the hub handler alongside service and tracker types."""

    entry = _EntryStub()
    mapping = config_flow.ConfigFlow.async_get_supported_subentry_types(entry)

    service_factory = mapping[SUBENTRY_TYPE_SERVICE]
    tracker_factory = mapping[SUBENTRY_TYPE_TRACKER]
    hub_factory = mapping["hub"]

    assert callable(service_factory)
    assert callable(tracker_factory)
    assert callable(hub_factory)

    assert isinstance(service_factory(), config_flow.ServiceSubentryFlowHandler)
    assert isinstance(tracker_factory(), config_flow.TrackerSubentryFlowHandler)
    assert isinstance(hub_factory(), config_flow.HubSubentryFlowHandler)


@pytest.mark.asyncio
async def test_async_step_migrate_creates_subentries_and_moves_options() -> None:
    """Migration flow should consolidate options and sync feature subentries."""

    entry = _EntryStub()
    entry.version = 0
    entry.data = {
        CONF_GOOGLE_EMAIL: "Legacy@Example.com",
        CONF_OAUTH_TOKEN: "token",
        DATA_AUTH_METHOD: "secrets_json",
        OPT_LOCATION_POLL_INTERVAL: 900,
        OPT_DEVICE_POLL_DELAY: 8,
    }
    entry.options = {
        OPT_MIN_ACCURACY_THRESHOLD: 75,
        OPT_OPTIONS_SCHEMA_VERSION: 1,
    }

    flow = config_flow.ConfigFlow()
    hass = _HassStub(entry)
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {}
    flow.unique_id = None  # type: ignore[attribute-defined-outside-init]
    flow._unique_id = None  # type: ignore[attr-defined]
    flow._available_devices = []  # type: ignore[attr-defined]
    flow.config_entry = entry  # type: ignore[assignment]

    result = await flow.async_step_migrate(entry)

    assert result["type"] == "form"
    assert entry.version == config_flow.ConfigFlow.VERSION
    assert OPT_LOCATION_POLL_INTERVAL not in entry.data
    assert OPT_DEVICE_POLL_DELAY not in entry.data
    assert entry.data[CONF_GOOGLE_EMAIL] == "legacy@example.com"
    assert entry.data[CONF_OAUTH_TOKEN] == "token"

    options = entry.options
    assert options[OPT_LOCATION_POLL_INTERVAL] == 900
    assert options[OPT_DEVICE_POLL_DELAY] == 8
    assert options[OPT_MIN_ACCURACY_THRESHOLD] == 75
    assert options[OPT_OPTIONS_SCHEMA_VERSION] == 2
    assert options[OPT_IGNORED_DEVICES] == {}

    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert len(manager.created) == 2
    assert any(
        record["data"]["group_key"] == SERVICE_SUBENTRY_KEY for record in manager.created
    )
    assert any(
        record["data"]["group_key"] == TRACKER_SUBENTRY_KEY for record in manager.created
    )
    assert manager.entry_updates and manager.entry_updates[-1].get(
        "version"
    ) == config_flow.ConfigFlow.VERSION

    placeholders = flow.context.get("title_placeholders", {})
    assert placeholders.get("email") == "legacy@example.com"

    confirm = await flow.async_step_migrate_complete({})
    assert confirm["type"] == "abort"
    assert confirm["reason"] == "migration_successful"
