# tests/test_diagnostics_buffer_summary.py
"""Diagnostics buffer summaries surface sanitized coordinator payloads."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import diagnostics
from custom_components.googlefindmy.const import DOMAIN


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


class _StubEntry:
    """Minimal config entry stub referencing the coordinator."""

    def __init__(self, coordinator: _StubCoordinator) -> None:
        self.entry_id = "entry-id"
        self.version = 1
        self.domain = DOMAIN
        self.data: dict[str, object] = {}
        self.options: dict[str, object] = {}
        self.runtime_data = coordinator


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
        asyncio.set_event_loop(None)
        loop.close()


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
