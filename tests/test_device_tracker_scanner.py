# tests/test_device_tracker_scanner.py
"""Tests for the device tracker cloud scanner integration hooks."""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping

import pytest

from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    CONF_OAUTH_TOKEN,
    DATA_SECRET_BUNDLE,
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
)
from custom_components.googlefindmy.discovery import (
    CLOUD_DISCOVERY_NAMESPACE,
    _cloud_discovery_stable_key,
)


def test_scanner_triggers_cloud_discovery(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The tracker scanner should invoke cloud discovery for new trackers."""

    device_tracker = importlib.import_module("custom_components.googlefindmy.device_tracker")
    triggered_calls: list[Mapping[str, Any]] = []
    scheduled: list[asyncio.Task[Any]] = []

    async def _fake_trigger(
        hass,
        *,
        email,
        token,
        secrets_bundle,
        discovery_ns,
        discovery_stable_key,
        source,
    ) -> bool:  # type: ignore[no-untyped-def]
        triggered_calls.append(
            {
                "email": email,
                "token": token,
                "secrets_bundle": secrets_bundle,
                "discovery_ns": discovery_ns,
                "discovery_stable_key": discovery_stable_key,
                "source": source,
            }
        )
        return True

    monkeypatch.setattr(device_tracker, "_trigger_cloud_discovery", _fake_trigger)

    def _async_create_task(
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        scheduled.append(task)
        return task

    hass = SimpleNamespace(async_create_task=_async_create_task)

    class _StubCoordinator(device_tracker.GoogleFindMyCoordinator):
        def __init__(self, devices: Iterable[dict[str, Any]]) -> None:
            self._devices = list(devices)
            self._listeners: list[Callable[[], None]] = []
            self.hass = hass
            self.config_entry = None
            self._bootstrap_consumed = False

        def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
            self._listeners.append(listener)
            return lambda: None

        def stable_subentry_identifier(
            self,
            *,
            key: str | None = None,
            feature: str | None = None,
        ) -> str:
            assert key is not None
            return f"{key}-identifier"

        def get_subentry_snapshot(
            self,
            key: str | None = None,
            *,
            feature: str | None = None,
        ) -> list[dict[str, Any]]:
            if not self._bootstrap_consumed:
                self._bootstrap_consumed = True
                return []
            return list(self._devices)

        def get_subentry_metadata(
            self,
            *,
            key: str | None = None,
            feature: str | None = None,
        ) -> Any:
            if key is not None:
                resolved = key
            elif feature in {"button", "device_tracker", "sensor"}:
                resolved = TRACKER_SUBENTRY_KEY
            elif feature == "binary_sensor":
                resolved = SERVICE_SUBENTRY_KEY
            else:
                resolved = TRACKER_SUBENTRY_KEY
            return SimpleNamespace(key=resolved)

    class _StubConfigEntry:
        def __init__(self, coordinator: _StubCoordinator) -> None:
            self.runtime_data = coordinator
            self.entry_id = "entry-123"
            self.data: dict[str, Any] = {
                CONF_GOOGLE_EMAIL: "Owner@Example.Com",
                CONF_OAUTH_TOKEN: "aas_et/ACCOUNT",
                DATA_SECRET_BUNDLE: {"Email": "Owner@Example.Com"},
            }
            self.options: dict[str, Any] = {}
            self._callbacks: list[Callable[[], None]] = []

        def async_on_unload(self, callback: Callable[[], None]) -> None:
            self._callbacks.append(callback)

    coordinator = _StubCoordinator([
        {"id": "tracker-1", "name": "Tracker"},
    ])
    entry = _StubConfigEntry(coordinator)
    coordinator.config_entry = entry

    added: list[list[Any]] = []

    def _capture_entities(entities: Iterable[Any], update_before_add: bool = False) -> None:
        added.append(list(entities))
        assert update_before_add is True

    caplog.set_level(logging.INFO, "custom_components.googlefindmy.device_tracker")

    async def _exercise() -> None:
        await device_tracker.async_setup_entry(hass, entry, _capture_entities)

        assert added and len(added[0]) == 1

        for task in scheduled:
            await task

    asyncio.run(_exercise())

    identifier = coordinator.stable_subentry_identifier(key=TRACKER_SUBENTRY_KEY)
    tracker_entity = added[0][0]
    assert tracker_entity.subentry_key == TRACKER_SUBENTRY_KEY
    assert identifier in tracker_entity.unique_id

    assert triggered_calls, "scanner should schedule cloud discovery"
    call = triggered_calls[0]
    assert call["email"] == "owner@example.com"
    assert call["token"] == "aas_et/ACCOUNT"
    assert call["secrets_bundle"] == {"Email": "Owner@Example.Com"}
    assert call["discovery_ns"] == f"{CLOUD_DISCOVERY_NAMESPACE}.entry-123"
    expected_key = _cloud_discovery_stable_key(
        "owner@example.com",
        "aas_et/ACCOUNT",
        {"Email": "Owner@Example.Com"},
    )
    assert call["discovery_stable_key"] == expected_key
    assert call["source"] == "cloud_scanner"

    assert any(
        "own***@example.com" in record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO
    ), "scanner should log redacted identifiers"

    assert any(callback for callback in entry._callbacks)
