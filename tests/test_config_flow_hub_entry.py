# tests/test_config_flow_hub_entry.py
from __future__ import annotations

# Tests covering hub subentry registration, delegation, and legacy-core fallbacks.

import inspect
import logging
from types import SimpleNamespace
from typing import Any, Callable, Protocol

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_TRACKER,
)
from homeassistant.config_entries import ConfigEntry


class _SubentrySupportToggle(Protocol):
    """Protocol covering the shared fixture interface for subentry toggles."""

    def as_modern(self) -> object | None:
        """Restore native subentry support."""

    def as_legacy(self) -> type[object]:
        """Simulate legacy cores lacking subentry support."""


@pytest.mark.parametrize(
    ("simulate_legacy_core", "expects_hub"),
    [
        (False, True),
        (True, False),
    ],
)
def test_supported_subentry_types_gate_hub_registration(
    subentry_support: _SubentrySupportToggle,
    simulate_legacy_core: bool,
    expects_hub: bool,
) -> None:
    """Config flow should only expose hub subentries when supported."""

    if simulate_legacy_core:
        subentry_support.as_legacy()
    else:
        subentry_support.as_modern()

    mapping = config_flow.ConfigFlow.async_get_supported_subentry_types(  # type: ignore[arg-type]
        SimpleNamespace()
    )

    assert SUBENTRY_TYPE_SERVICE in mapping
    assert SUBENTRY_TYPE_TRACKER in mapping
    assert (SUBENTRY_TYPE_HUB in mapping) is expects_hub

    if expects_hub:
        handler_factory = mapping[SUBENTRY_TYPE_HUB]
        assert callable(handler_factory)
        handler = handler_factory()
        assert isinstance(handler, config_flow.HubSubentryFlowHandler)
        assert handler._group_key == SERVICE_SUBENTRY_KEY  # type: ignore[attr-defined]
        assert handler._subentry_type == SUBENTRY_TYPE_HUB  # type: ignore[attr-defined]
        assert handler._features == SERVICE_FEATURE_PLATFORMS  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_hub_flow_invokes_subentry_handler(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Add Hub flows should delegate to the hub subentry handler."""

    caplog.set_level(logging.INFO)

    entry = SimpleNamespace(entry_id="entry-123", data={}, options={}, subentries={})

    class _ConfigEntriesManager:
        def __init__(self) -> None:
            self.lookups: list[str] = []

        def async_get_entry(self, entry_id: str) -> SimpleNamespace | None:
            self.lookups.append(entry_id)
            if entry_id == entry.entry_id:
                return entry
            return None

    hass = SimpleNamespace(config_entries=_ConfigEntriesManager())

    sentinel: dict[str, object] = {"type": "create_entry", "data": {}}
    calls: list[tuple[str | None, dict[str, Any] | None]] = []

    async def _fake_async_step_user(
        self: config_flow.HubSubentryFlowHandler,
        user_input: dict[str, Any] | None = None,
    ) -> dict[str, object]:
        calls.append((getattr(self.config_entry, "entry_id", None), user_input))
        return sentinel

    monkeypatch.setattr(
        config_flow.HubSubentryFlowHandler,
        "async_step_user",
        _fake_async_step_user,
        raising=False,
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"source": "hub", "entry_id": entry.entry_id}
    flow.config_entry = entry  # type: ignore[assignment]

    result = await flow.async_step_hub()
    if inspect.isawaitable(result):
        result = await result

    assert result is sentinel
    assert calls == [(entry.entry_id, None)]
    assert any(
        "provisioning hub subentry" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_hub_flow_aborts_without_entry_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Add Hub flows without entry context should abort."""

    hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda _: None))

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"source": "hub", "entry_id": "missing"}

    result = await flow.async_step_hub()
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "unknown"


@pytest.mark.asyncio
async def test_hub_flow_aborts_when_hub_unsupported(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cores without hub subentry support should abort with not_supported."""

    caplog.set_level(logging.ERROR)

    entry = SimpleNamespace(entry_id="entry-legacy", data={}, options={}, subentries={})

    class _ConfigEntriesManager:
        def __init__(self) -> None:
            self.entry = entry

        def async_get_entry(self, entry_id: str) -> SimpleNamespace | None:
            if entry_id == entry.entry_id:
                return self.entry
            return None

    hass = SimpleNamespace(config_entries=_ConfigEntriesManager())

    def _no_hub(_: ConfigEntry) -> dict[str, Callable[[], config_flow.ConfigSubentryFlow]]:
        return {
            config_flow.SUBENTRY_TYPE_SERVICE: lambda: config_flow.ServiceSubentryFlowHandler(entry),
            config_flow.SUBENTRY_TYPE_TRACKER: lambda: config_flow.TrackerSubentryFlowHandler(entry),
        }

    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "async_get_supported_subentry_types",
        staticmethod(_no_hub),
        raising=False,
    )

    flow = config_flow.ConfigFlow()
    flow.hass = hass  # type: ignore[assignment]
    flow.context = {"source": "hub", "entry_id": entry.entry_id}
    flow.config_entry = entry  # type: ignore[assignment]

    result = await flow.async_step_hub()
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "abort"
    assert result["reason"] == "not_supported"
    assert any(
        "hub subentry type not supported" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_hub_subentry_flow_logs_and_delegates(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hub subentry handler should log and delegate to the base flow implementation."""

    caplog.set_level(logging.INFO)

    sentinel: dict[str, object] = {"type": "create_entry", "data": {}}

    async def _fake_async_step_user(self, user_input=None):  # type: ignore[unused-argument]
        return sentinel

    monkeypatch.setattr(
        config_flow._BaseSubentryFlow,  # type: ignore[attr-defined]
        "async_step_user",
        _fake_async_step_user,
        raising=False,
    )

    handler = object.__new__(config_flow.HubSubentryFlowHandler)
    handler.config_entry = SimpleNamespace(entry_id="entry-1")  # type: ignore[attr-defined]
    handler.subentry = None  # type: ignore[attr-defined]
    handler.hass = SimpleNamespace()  # type: ignore[assignment]

    result = await config_flow.HubSubentryFlowHandler.async_step_user(handler, None)
    if inspect.isawaitable(result):
        result = await result

    assert result is sentinel
    assert any(
        "Hub subentry flow requested" in record.getMessage() for record in caplog.records
    ), "Expected hub subentry flow to log when invoked"
