# tests/test_config_flow_subentry_sync.py
"""Tests validating config flow subentry creation and updates."""

from __future__ import annotations

import asyncio
from types import MappingProxyType, SimpleNamespace
from typing import Any

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_AUTH_METHOD,
    DEFAULT_ENABLE_STATS_ENTITIES,
    DEFAULT_GOOGLE_HOME_FILTER_ENABLED,
    DOMAIN,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
)
from homeassistant.config_entries import ConfigSubentry


class _ConfigEntriesManagerStub:
    """Stub mimicking Home Assistant's config entries manager."""

    def __init__(self, entry: _EntryStub) -> None:
        self._entry = entry
        self.created: list[dict[str, Any]] = []
        self.updated: list[dict[str, Any]] = []

    def async_entries(self, domain: str | None = None) -> list[Any]:
        if domain and domain != DOMAIN:
            return []
        return [self._entry]

    def async_get_entry(self, entry_id: str) -> _EntryStub | None:
        if entry_id == self._entry.entry_id:
            return self._entry
        return None

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
        )
        self._entry.subentries[subentry.subentry_id] = subentry
        self.created.append(dict(data))
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
        self.updated.append(dict(data))


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


def test_device_selection_creates_feature_group_with_flags() -> None:
    """Sync helper should create a subentry with the expected feature flags."""

    entry = _EntryStub()
    flow = _build_flow(entry)
    context_map = flow._ensure_subentry_context()

    asyncio.run(
        flow._async_sync_feature_subentries(  # type: ignore[attr-defined]
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
    )
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.created, "subentry should be created"
    payload = manager.created[-1]
    assert payload["group_key"] == "core_tracking"
    assert payload["features"] == sorted(config_flow._CORE_FEATURE_PLATFORMS)
    assert all(isinstance(feature, str) for feature in payload["features"])
    assert all(feature == feature.lower() for feature in payload["features"])
    assert payload["has_google_home_filter"] is False
    flags = payload["feature_flags"]
    assert flags[OPT_MAP_VIEW_TOKEN_EXPIRATION] is False
    assert flags[OPT_GOOGLE_HOME_FILTER_ENABLED] is False
    assert flags[OPT_ENABLE_STATS_ENTITIES] is True


def test_device_selection_updates_existing_feature_group() -> None:
    """Sync helper should update an existing subentry with new feature flags."""

    entry = _EntryStub()
    existing = ConfigSubentry(
        data=MappingProxyType(
            {
                "group_key": "core_tracking",
                "feature_flags": {},
            }
        ),
        subentry_type="googlefindmy_feature_group",
        title="Google Find My devices",
        unique_id=f"{entry.entry_id}-core_tracking",
    )
    entry.subentries[existing.subentry_id] = existing

    flow = _build_flow(entry)
    context_map = flow._ensure_subentry_context()
    context_map["core_tracking"] = existing.subentry_id

    asyncio.run(
        flow._async_sync_feature_subentries(  # type: ignore[attr-defined]
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
    )
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated, "subentry should be updated"
    payload = manager.updated[-1]
    assert payload["group_key"] == "core_tracking"
    assert payload["has_google_home_filter"] is True
    flags = payload["feature_flags"]
    assert flags[OPT_MAP_VIEW_TOKEN_EXPIRATION] is True
    assert flags[OPT_GOOGLE_HOME_FILTER_ENABLED] is True
    assert flags[OPT_ENABLE_STATS_ENTITIES] is False
