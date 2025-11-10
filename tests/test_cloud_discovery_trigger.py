# tests/test_cloud_discovery_trigger.py
"""Tests for the cloud discovery trigger helper."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING
from collections.abc import Awaitable
from unittest.mock import AsyncMock

import importlib

import pytest

from custom_components.googlefindmy import config_flow, discovery
from custom_components.googlefindmy.const import DOMAIN
from tests.helpers.config_flow import config_entries_flow_stub

integration = importlib.import_module("custom_components.googlefindmy")

if TYPE_CHECKING:
    import pytest


def _make_hass() -> SimpleNamespace:
    """Return a minimal hass stub suitable for discovery tests."""

    flow = config_entries_flow_stub(
        result={
            "type": config_flow.data_entry_flow.FlowResultType.ABORT,
            "reason": "unknown",
        }
    )
    config_entries = SimpleNamespace(flow=flow)
    return SimpleNamespace(data={}, config_entries=config_entries)


def test_trigger_cloud_discovery_uses_helper(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The helper should prefer async_create_discovery_flow when available."""

    hass = _make_hass()
    captured: list[tuple] = []

    async def _helper(*args, **kwargs):
        captured.append((args, kwargs))
        return None

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> bool:
        return await integration._trigger_cloud_discovery(
            hass,
            email="User@Example.com",
            token="aas_et/TOKEN",
            secrets_bundle={"aas_token": "aas_et/TOKEN"},
        )

    caplog.set_level(logging.INFO, "custom_components.googlefindmy.discovery")
    assert asyncio.run(_exercise()) is True
    assert hass.config_entries.flow.async_init.await_count == 0
    assert len(captured) == 1

    args, kwargs = captured[0]
    call_hass, domain = args
    context = kwargs.get("context", {})
    data = kwargs.get("data", {})
    discovery_key = kwargs.get("discovery_key")

    assert call_hass is hass

    assert domain == DOMAIN
    assert context["source"] == config_flow.SOURCE_DISCOVERY
    assert data["email"] == "user@example.com"
    assert data["token"] == "aas_et/TOKEN"
    assert data["secrets_bundle"] == {"aas_token": "aas_et/TOKEN"}
    assert data["discovery_ns"] == f"{DOMAIN}.cloud_scan"
    assert data["discovery_stable_key"] == "email:user@example.com"
    assert discovery_key is not None

    runtime = integration._cloud_discovery_runtime(hass)
    assert runtime["results"], "discovery payload should be recorded"

    assert any(
        "use***@example.com" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), "trigger log should redact identifiers"


def test_trigger_cloud_discovery_sanitizes_context_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Custom discovery triggers should not leak into the context source."""

    hass = _make_hass()
    captured: list[tuple] = []

    async def _helper(*args, **kwargs):
        captured.append((args, kwargs))
        return None

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> bool:
        return await integration._trigger_cloud_discovery(
            hass,
            email="User@Example.com",
            token="aas_et/TOKEN",
            secrets_bundle={"aas_token": "aas_et/TOKEN"},
            source="cloud_scanner",
        )

    assert asyncio.run(_exercise()) is True
    assert len(captured) == 1

    _, kwargs = captured[0]
    context = kwargs.get("context", {})
    data = kwargs.get("data", {})

    assert context["source"] == config_flow.SOURCE_DISCOVERY
    assert data["discovery_source"] == "cloud_scanner"


def test_trigger_cloud_discovery_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing helper should fall back to config_entries.flow.async_init."""

    hass = _make_hass()

    async def _helper(*args, **kwargs):
        raise AttributeError("missing helper")

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> bool:
        return await integration._trigger_cloud_discovery(
            hass,
            email="fallback@example.com",
            token=None,
            secrets_bundle=None,
        )

    caplog.set_level(logging.INFO, "custom_components.googlefindmy.discovery")

    assert asyncio.run(_exercise()) is True
    hass.config_entries.flow.async_init.assert_awaited_once()
    _, kwargs = hass.config_entries.flow.async_init.call_args
    assert kwargs["context"]["source"] == config_flow.SOURCE_DISCOVERY
    assert kwargs["data"]["email"] == "fallback@example.com"
    assert kwargs["data"]["discovery_ns"] == f"{DOMAIN}.cloud_scan"
    assert any(
        "fal***@example.com" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), "fallback trigger log should redact identifiers"


@pytest.mark.asyncio
async def test_async_create_discovery_flow_handles_missing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attribute errors should fall back to a graceful abort."""

    hass = _make_hass()

    async def _helper(*args, **kwargs):
        raise AttributeError("missing helper attribute")

    monkeypatch.setattr(
        config_flow.config_entries,
        "async_create_discovery_flow",
        _helper,
        raising=False,
    )
    monkeypatch.setattr(config_flow, "_fallback_discovery_flow_helper", None)

    result = await config_flow.async_create_discovery_flow(
        hass,
        DOMAIN,
        context=None,
        data={},
    )

    assert result == {
        "type": config_flow.data_entry_flow.FlowResultType.ABORT,
        "reason": "unknown",
    }


@pytest.mark.asyncio
async def test_async_create_discovery_flow_treats_none_as_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Returning None should be treated as an already-in-progress abort."""

    hass = _make_hass()

    async def _helper(*args, **kwargs):
        return None

    monkeypatch.setattr(
        config_flow.config_entries,
        "async_create_discovery_flow",
        _helper,
        raising=False,
    )

    result = await config_flow.async_create_discovery_flow(
        hass,
        DOMAIN,
        context={"source": "discovery"},
        data={},
    )

    assert result == {
        "type": config_flow.data_entry_flow.FlowResultType.ABORT,
        "reason": "already_in_progress",
    }


def test_trigger_cloud_discovery_deduplicates(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Multiple discoveries with the same stable key should deduplicate flows."""

    hass = _make_hass()
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy")
    caplog.set_level(logging.DEBUG, "custom_components.googlefindmy.discovery")

    gate = asyncio.Event()
    calls: list[dict] = []

    async def _helper(*args, **kwargs):
        calls.append(kwargs.get("data") or args[3])
        await gate.wait()

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    async def _exercise() -> None:
        task = asyncio.create_task(
            integration._trigger_cloud_discovery(
                hass,
                email="dedup@example.com",
                token="aas_et/DUP",
            )
        )
        await asyncio.sleep(0)

        skipped = await integration._trigger_cloud_discovery(
            hass,
            email="dedup@example.com",
            token="aas_et/DUP",
        )
        assert skipped is False
        assert any(
            "ded***@example.com" in record.getMessage() for record in caplog.records
        )
        assert all("aas_et/DUP" not in record.getMessage() for record in caplog.records)
        assert len(calls) == 1

        gate.set()
        assert await task is True

        gate.clear()
        gate.set()
        again = await integration._trigger_cloud_discovery(
            hass,
            email="dedup@example.com",
            token="aas_et/DUP",
        )
        assert again is True
        assert len(calls) == 2

    asyncio.run(_exercise())


def test_results_append_triggers_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Appending to the results list should schedule a discovery flow."""

    hass = _make_hass()
    scheduled: list[Awaitable[bool]] = []

    def _async_create_task(coro):  # type: ignore[no-untyped-def]
        scheduled.append(coro)
        return coro

    hass.async_create_task = _async_create_task  # type: ignore[attr-defined]

    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(config_flow, "async_create_discovery_flow", helper)

    runtime = integration._cloud_discovery_runtime(hass)
    results = runtime["results"]
    results.append({"email": "append@example.com", "token": "aas_et/APP"})
    assert scheduled, "append should schedule a discovery coroutine"

    async def _drain() -> None:
        for task in scheduled:
            await task

    asyncio.run(_drain())
    helper.assert_awaited_once()
    assert hass.config_entries.flow.async_init.await_count == 0


def test_results_append_deduplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    """Appending duplicate payloads should only launch one flow at a time."""

    hass = _make_hass()
    calls: list[dict] = []

    gate_holder = [asyncio.Event()]

    async def _helper(*args, **kwargs):
        calls.append(kwargs.get("data") or args[3])
        await gate_holder[0].wait()
        return None

    monkeypatch.setattr(config_flow, "async_create_discovery_flow", _helper)

    scheduled: list[asyncio.Task] = []

    def _async_create_task(coro):  # type: ignore[no-untyped-def]
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    hass.async_create_task = _async_create_task  # type: ignore[attr-defined]

    stable_key = discovery._cloud_discovery_stable_key(
        "dedup@example.com",
        "aas_et/DUP",
        {"oauth_token": "aas_et/DUP"},
    )
    payload = discovery._assemble_cloud_discovery_payload(
        email="dedup@example.com",
        token="aas_et/DUP",
        secrets_bundle={"oauth_token": "aas_et/DUP"},
        discovery_ns=discovery.CLOUD_DISCOVERY_NAMESPACE,
        discovery_stable_key=stable_key,
        title=None,
        source=None,
    )

    async def _exercise() -> None:
        results = integration._cloud_discovery_runtime(hass)["results"]

        results.append(payload)
        results.append(payload)

        await asyncio.sleep(0)
        assert len(calls) == 1

        gate_holder[0].set()
        await asyncio.sleep(0)

        gate_holder[0] = asyncio.Event()

        results.append(payload)
        await asyncio.sleep(0)
        assert len(calls) == 2

        gate_holder[0].set()
        await asyncio.sleep(0)

        for task in scheduled:
            await task

    asyncio.run(_exercise())
