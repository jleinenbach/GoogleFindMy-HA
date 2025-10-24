# tests/test_options_flow_subentries.py
"""Tests covering subentry selection and repair flows in the options handler."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from types import MappingProxyType, SimpleNamespace

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    OPT_CONTRIBUTOR_MODE,
    OPT_IGNORED_DEVICES,
    OPT_MAP_VIEW_TOKEN_EXPIRATION,
)
from homeassistant.config_entries import ConfigSubentry


@dataclass
class _ManagerStub:
    """Minimal config_entries manager capturing subentry operations."""

    entry: "_EntryStub"

    def __post_init__(self) -> None:
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.removed: list[str] = []

    def async_update_entry(self, entry: "_EntryStub", *, data: dict[str, Any]) -> None:
        assert entry is self.entry
        entry.data = data

    def async_update_subentry(
        self,
        entry: "_EntryStub",
        subentry: ConfigSubentry,
        *,
        data: dict[str, Any],
        title: str | None = None,
        unique_id: str | None = None,
    ) -> None:
        assert entry is self.entry
        subentry.data = MappingProxyType(dict(data))
        if title is not None:
            subentry.title = title
        if unique_id is not None:
            subentry.unique_id = unique_id
        self.updated.append((subentry.subentry_id, dict(subentry.data)))

    def async_remove_subentry(self, entry: "_EntryStub", subentry_id: str) -> bool:  # noqa: FBT001
        assert entry is self.entry
        entry.subentries.pop(subentry_id, None)
        self.removed.append(subentry_id)
        return True


class _EntryStub:
    """Config entry stub exposing subentries and mutable options."""

    def __init__(self) -> None:
        self.entry_id = "entry-test"
        self.title = "Entry Title"
        self.data: dict[str, Any] = {}
        self.options: dict[str, Any] = {}
        self.subentries: dict[str, ConfigSubentry] = {}
        self.runtime_data = SimpleNamespace(coordinator=SimpleNamespace(data=[]))

    def add_subentry(
        self,
        *,
        key: str,
        title: str,
        visible_device_ids: list[str] | None = None,
        feature_flags: dict[str, Any] | None = None,
    ) -> ConfigSubentry:
        payload = {
            "group_key": key,
            "feature_flags": feature_flags or {},
        }
        if visible_device_ids is not None:
            payload["visible_device_ids"] = list(visible_device_ids)
        subentry = ConfigSubentry(
            data=payload,
            subentry_type="googlefindmy_feature_group",
            title=title,
            unique_id=f"{self.entry_id}-{key}",
        )
        self.subentries[subentry.subentry_id] = subentry
        return subentry


class _HassStub:
    """Home Assistant stub exposing config entry helpers to the flow."""

    def __init__(self, entry: _EntryStub) -> None:
        self.config_entries = _ManagerStub(entry)

    def async_create_task(self, coro: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coro)


def _build_flow(entry: _EntryStub) -> config_flow.OptionsFlowHandler:
    flow = config_flow.OptionsFlowHandler()
    flow.hass = _HassStub(entry)  # type: ignore[assignment]
    flow.config_entry = entry  # type: ignore[attr-defined]
    return flow


def test_settings_updates_feature_flags_for_selected_subentry() -> None:
    """Settings step should persist feature flags to the chosen subentry."""

    entry = _EntryStub()
    entry.add_subentry(key="core_tracking", title="Core")
    flow = _build_flow(entry)

    result = asyncio.run(
        flow.async_step_settings(
            {
                "subentry": "core_tracking",
                OPT_MAP_VIEW_TOKEN_EXPIRATION: True,
                OPT_CONTRIBUTOR_MODE: "high_traffic",
            }
        )
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    _, payload = manager.updated[-1]
    assert payload["feature_flags"][OPT_MAP_VIEW_TOKEN_EXPIRATION] is True
    assert payload["feature_flags"][OPT_CONTRIBUTOR_MODE] == "high_traffic"


def test_visibility_assigns_devices_to_target_subentry() -> None:
    """Visibility step should attach restored devices to the chosen subentry."""

    entry = _EntryStub()
    sub = entry.add_subentry(key="core_tracking", title="Core")
    entry.options = {
        OPT_IGNORED_DEVICES: {"dev-1": {"name": "Device 1"}},
    }

    flow = _build_flow(entry)
    result = asyncio.run(
        flow.async_step_visibility(
            {"subentry": "core_tracking", "unignore_devices": ["dev-1"]}
        )
    )

    assert result["type"] == "create_entry"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    updated_id, payload = manager.updated[-1]
    assert updated_id == sub.subentry_id
    assert payload["visible_device_ids"] == ("dev-1",)


def test_repairs_move_assigns_devices_to_selected_subentry() -> None:
    """Repair move step should remove devices from other subentries."""

    entry = _EntryStub()
    target = entry.add_subentry(key="target", title="Target", visible_device_ids=[])
    other = entry.add_subentry(key="other", title="Other", visible_device_ids=["dev-2"])
    entry.runtime_data.coordinator.data = [
        {"device_id": "dev-1", "name": "Device 1"},
        {"device_id": "dev-2", "name": "Device 2"},
    ]

    flow = _build_flow(entry)
    result = asyncio.run(
        flow.async_step_repairs_move(
            {"target_subentry": "target", "device_ids": ["dev-1", "dev-2"]}
        )
    )

    assert result["type"] == "abort"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert manager.updated
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[target.subentry_id]["visible_device_ids"]) == (
        "dev-1",
        "dev-2",
    )
    assert tuple(updated[other.subentry_id]["visible_device_ids"]) == ()


def test_repairs_delete_moves_devices_and_removes_subentry() -> None:
    """Deleting a subentry moves devices to fallback and removes the source."""

    entry = _EntryStub()
    removable = entry.add_subentry(
        key="remove", title="Remove", visible_device_ids=["dev-1", "dev-2"]
    )
    fallback = entry.add_subentry(key="keep", title="Keep", visible_device_ids=[])

    flow = _build_flow(entry)
    result = asyncio.run(
        flow.async_step_repairs_delete(
            {"delete_subentry": "remove", "fallback_subentry": "keep"}
        )
    )

    assert result["type"] == "abort"
    manager = flow.hass.config_entries  # type: ignore[assignment]
    assert removable.subentry_id in manager.removed
    updated = {sid: payload for sid, payload in manager.updated}
    assert tuple(updated[fallback.subentry_id]["visible_device_ids"]) == (
        "dev-1",
        "dev-2",
    )
