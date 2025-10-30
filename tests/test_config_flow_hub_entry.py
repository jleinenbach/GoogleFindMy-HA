# tests/test_config_flow_hub_entry.py
"""Regression coverage for the Add Hub entry point and hub subentry flow."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import config_flow
from custom_components.googlefindmy.const import (
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    SUBENTRY_TYPE_SERVICE,
    SUBENTRY_TYPE_HUB,
    SUBENTRY_TYPE_TRACKER,
)


def test_supported_subentry_types_include_hub() -> None:
    """Config flow must advertise the hub subentry handler."""

    mapping = config_flow.ConfigFlow.async_get_supported_subentry_types(  # type: ignore[arg-type]
        SimpleNamespace()
    )

    assert SUBENTRY_TYPE_HUB in mapping
    handler_cls = mapping[SUBENTRY_TYPE_HUB]
    assert handler_cls is config_flow.HubSubentryFlowHandler
    assert handler_cls._group_key == SERVICE_SUBENTRY_KEY  # type: ignore[attr-defined]
    assert handler_cls._subentry_type == SUBENTRY_TYPE_HUB  # type: ignore[attr-defined]
    assert handler_cls._features == SERVICE_FEATURE_PLATFORMS  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_hub_flow_logs_and_returns_user_step(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Add Hub entry point should log and present the standard user form."""

    caplog.set_level(logging.INFO)

    flow = config_flow.ConfigFlow()
    flow.hass = SimpleNamespace()  # type: ignore[assignment]
    flow.context = {"source": "hub", "entry_id": "entry-123"}
    flow.unique_id = None  # type: ignore[attr-defined]

    result = await flow.async_step_hub()
    if inspect.isawaitable(result):
        result = await result

    assert result["type"] == "form"
    assert any(
        "Add Hub flow requested" in record.getMessage() for record in caplog.records
    ), "Expected Add Hub request to be logged"


@pytest.mark.asyncio
async def test_hub_flow_errors_when_handler_missing(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing hub handler should raise immediately with a clear error."""

    caplog.set_level(logging.ERROR)

    flow = config_flow.ConfigFlow()
    flow.hass = SimpleNamespace()  # type: ignore[assignment]
    flow.context = {"source": "hub", "entry_id": "entry-999"}
    flow.unique_id = None  # type: ignore[attr-defined]

    def _without_hub(cls, entry):  # type: ignore[unused-argument]
        return {
            SUBENTRY_TYPE_SERVICE: config_flow.ServiceSubentryFlowHandler,
            SUBENTRY_TYPE_TRACKER: config_flow.TrackerSubentryFlowHandler,
        }

    monkeypatch.setattr(
        config_flow.ConfigFlow,
        "async_get_supported_subentry_types",
        classmethod(_without_hub),
        raising=False,
    )

    with pytest.raises(config_flow.HomeAssistantErrorBase, match="Hub subentry handler"):
        await flow.async_step_hub()

    assert any(
        "no hub subentry handler is registered" in record.getMessage()
        for record in caplog.records
    ), "Expected the guard to log a descriptive error"


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
