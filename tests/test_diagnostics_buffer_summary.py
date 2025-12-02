# tests/test_diagnostics_buffer_summary.py
"""Diagnostics buffer summaries surface sanitized coordinator payloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import diagnostics
from custom_components.googlefindmy.const import (
    CONF_OAUTH_TOKEN,
    DOMAIN,
    OPT_DEVICE_POLL_DELAY,
    OPT_ENABLE_STATS_ENTITIES,
    OPT_GOOGLE_HOME_FILTER_ENABLED,
    OPT_GOOGLE_HOME_FILTER_KEYWORDS,
    OPT_IGNORED_DEVICES,
    OPT_LOCATION_POLL_INTERVAL,
    OPT_MIN_ACCURACY_THRESHOLD,
    OPT_MOVEMENT_THRESHOLD,
)
from tests.helpers import drain_loop


class _StubDiagnosticsBuffer:
    """Diagnostics buffer stub exposing redaction-sensitive payloads."""

    _WARNING_DETAIL = "warning detail " * 20
    _ERROR_DETAIL = "error detail " * 20

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": {"warnings": 2, "errors": 1},
            "warnings": [
                {
                    "code": "warn-device",
                    "device_id": "device-123",
                    "device_name": "Living Room Phone",
                    "detail": self._WARNING_DETAIL,
                }
            ],
            "errors": [
                {
                    "code": "err-device",
                    "device_id": "device-987",
                    "device_name": "Bedroom Tablet",
                    "detail": self._ERROR_DETAIL,
                }
            ],
        }


class _StubCoordinator:
    """Coordinator stub exposing a diagnostics buffer."""

    def __init__(self) -> None:
        self._diag = _StubDiagnosticsBuffer()
        self._device_names: dict[str, str] = {}
        self._device_location_data: dict[str, object] = {}
        self._last_poll_mono: float | None = None
        self.stats: dict[str, int] = {}
        self.performance_metrics: dict[str, float] = {}
        self.recent_errors: list[object] = []
        self._enabled_poll_device_ids: set[str] = set()
        self._present_device_ids: set[str] = set()
        self._is_polling = True

    def attach_subentry_manager(
        self, manager: object, *, is_reload: bool = False
    ) -> None:
        self.subentry_manager = manager
        self._attached_is_reload = is_reload


class _StubEntry:
    """Minimal config entry stub referencing the coordinator."""

    def __init__(
        self,
        coordinator: _StubCoordinator,
        *,
        data: dict[str, object] | None = None,
        options: dict[str, object] | None = None,
    ) -> None:
        self.entry_id = "entry-id"
        self.version = 1
        self.domain = DOMAIN
        self.data = data or {}
        self.options = options or {}
        self.runtime_data = SimpleNamespace(coordinator=coordinator)


class _StubHass:
    """Home Assistant stub providing coordinator access."""

    def __init__(self, entry: _StubEntry, coordinator: _StubCoordinator) -> None:
        self.data = {
            DOMAIN: {
                "entries": {entry.entry_id: SimpleNamespace(coordinator=coordinator)}
            }
        }


def _redact(data, keys):  # pragma: no cover - deterministic helper in tests
    if isinstance(data, dict):
        return {
            key: _redact(value, keys) for key, value in data.items() if key not in keys
        }
    if isinstance(data, list):
        return [_redact(item, keys) for item in data]
    return data


def _run(coro):
    """Execute an async coroutine within an isolated event loop."""

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        drain_loop(loop)


def test_async_get_config_entry_diagnostics_includes_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostics include sanitized buffer summaries with redacted IDs."""

    coordinator = _StubCoordinator()
    entry = _StubEntry(coordinator)
    hass = _StubHass(entry, coordinator)

    async def _fake_get_integration(_hass, _domain):
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(
        diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={})
    )
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )
    monkeypatch.setattr(diagnostics, "async_redact_data", _redact)
    monkeypatch.setattr(diagnostics, "GoogleFindMyCoordinator", _StubCoordinator)

    payload = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    coordinator_block = payload.get("coordinator")
    assert coordinator_block is not None

    diag_payload = coordinator_block.get("diagnostics_buffer")
    assert diag_payload is not None

    summary = diag_payload.get("summary")
    assert summary == {"warnings": 2, "errors": 1}

    warnings_preview = diag_payload.get("warnings_preview")
    assert isinstance(warnings_preview, list)
    assert len(warnings_preview) == 1
    first_warning = warnings_preview[0]
    assert "device_id" not in first_warning
    assert "device_name" not in first_warning
    assert first_warning["detail"].endswith("…")
    assert len(first_warning["detail"]) <= 160

    errors_preview = diag_payload.get("errors_preview")
    assert isinstance(errors_preview, list)
    assert len(errors_preview) == 1
    first_error = errors_preview[0]
    assert "device_id" not in first_error
    assert "device_name" not in first_error
    assert first_error["detail"].endswith("…")
    assert len(first_error["detail"]) <= 160


def test_diagnostics_merge_entry_data_and_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Diagnostics merge entry data defaults with option overrides."""

    coordinator = _StubCoordinator()
    entry = _StubEntry(
        coordinator,
        data={
            OPT_LOCATION_POLL_INTERVAL: 600,
            OPT_DEVICE_POLL_DELAY: 10,
            OPT_MOVEMENT_THRESHOLD: 75,
            OPT_ENABLE_STATS_ENTITIES: True,
            OPT_GOOGLE_HOME_FILTER_ENABLED: False,
            OPT_GOOGLE_HOME_FILTER_KEYWORDS: "legacy",
            OPT_IGNORED_DEVICES: ["legacy-id"],
            OPT_MIN_ACCURACY_THRESHOLD: 123,
            CONF_OAUTH_TOKEN: "secret-token",
        },
        options={
            OPT_LOCATION_POLL_INTERVAL: "45",  # coercion
            OPT_GOOGLE_HOME_FILTER_ENABLED: True,
            OPT_GOOGLE_HOME_FILTER_KEYWORDS: "one, two, three",
            OPT_IGNORED_DEVICES: {"dev1": {}, "dev2": {}},
            OPT_ENABLE_STATS_ENTITIES: False,
        },
    )
    hass = _StubHass(entry, coordinator)

    async def _fake_get_integration(_hass, _domain):
        return SimpleNamespace(name="Test Integration", version="1.2.3")

    monkeypatch.setattr(diagnostics, "async_get_integration", _fake_get_integration)
    monkeypatch.setattr(diagnostics.dr, "async_get", lambda _hass: SimpleNamespace(devices={}))
    monkeypatch.setattr(
        diagnostics.er, "async_get", lambda _hass: SimpleNamespace(entities={})
    )

    payload = _run(diagnostics.async_get_config_entry_diagnostics(hass, entry))

    effective_config = payload["effective_config"]
    assert effective_config[OPT_LOCATION_POLL_INTERVAL] == "45"
    assert effective_config[OPT_DEVICE_POLL_DELAY] == 10
    assert effective_config[OPT_MOVEMENT_THRESHOLD] == 75
    assert effective_config[OPT_GOOGLE_HOME_FILTER_KEYWORDS] == "one, two, three"
    assert effective_config[OPT_IGNORED_DEVICES] == {"dev1": {}, "dev2": {}}
    assert effective_config[CONF_OAUTH_TOKEN] == diagnostics.REDACTED

    config_summary = payload["config"]
    assert config_summary["location_poll_interval"] == 45
    assert config_summary["device_poll_delay"] == 10
    assert config_summary["min_accuracy_threshold"] == 123
    assert config_summary["movement_threshold"] == 75
    assert config_summary["google_home_filter_enabled"] is True
    assert config_summary["enable_stats_entities"] is False
    assert config_summary["google_home_filter_keywords_count"] == 3
    assert config_summary["ignored_devices_count"] == 2
