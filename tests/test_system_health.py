# tests/test_system_health.py

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import system_health
from custom_components.googlefindmy.const import (
    CONF_GOOGLE_EMAIL,
    DATA_SECRET_BUNDLE,
    DOMAIN,
)

class _FakeConfigEntry:
    """Minimal ConfigEntry stub for system health tests."""

    def __init__(self) -> None:
        from homeassistant.config_entries import ConfigEntryState

        self.entry_id = "entry-test"
        self.domain = DOMAIN
        self.title = "Google Find My (user@example.com)"
        self.data = {
            CONF_GOOGLE_EMAIL: "user@example.com",
            DATA_SECRET_BUNDLE: {"username": "user@example.com"},
        }
        self.options: dict[str, object] = {}
        self.runtime_data = None
        self.disabled_by = None
        self.state = ConfigEntryState.LOADED


class _FakeConfigEntriesManager:
    """Expose the integration config entry to the system health handler."""

    def __init__(self, entry: _FakeConfigEntry) -> None:
        self._entries = [entry]

    def async_entries(self, domain: str | None = None):  # type: ignore[override]
        if domain is None or domain == DOMAIN:
            return list(self._entries)
        return []


class _FakeCoordinator:
    """Provide the minimal surface used by system health."""

    def __init__(self) -> None:
        self.data = [{"id": "device-1"}, {"id": "device-2"}]
        self.last_update_success_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self.stats = {"background_updates": 3, "polled_updates": 2}
        self.fcm_status = SimpleNamespace(
            state="connected", reason=None, changed_at=123.0
        )
        self.is_auth_error_active = False


class _FakeFcmReceiver:
    """Shared FCM receiver stub with deterministic telemetry."""

    def __init__(self) -> None:
        self.is_ready = True
        self.start_count = 2
        self.pcs = {"entry-test": object()}
        self.last_start_monotonic = 10.0
        self.last_stop_monotonic = 5.0


class _FakeHass:
    """Home Assistant core stub exposing data for the handler."""

    def __init__(self, coordinator: _FakeCoordinator) -> None:
        entry = _FakeConfigEntry()
        self.config_entries = _FakeConfigEntriesManager(entry)
        self.data = {
            DOMAIN: {
                "entries": {entry.entry_id: SimpleNamespace(coordinator=coordinator)},
                "fcm_receiver": _FakeFcmReceiver(),
            }
        }


@pytest.mark.asyncio
async def test_async_register_uses_registration_object() -> None:
    """Ensure the integration uses the provided registration helper when available."""

    class _Registration:
        def __init__(self) -> None:
            self.calls: list[tuple[object, object | None]] = []

        def async_register_info(
            self,
            handler: object,
            manage_url: object | None = None,
        ) -> None:
            self.calls.append((handler, manage_url))

    registration = _Registration()
    hass = object()

    await system_health.async_register(hass, registration)

    assert registration.calls == [
        (system_health.async_get_system_health_info, None)
    ]


@pytest.mark.asyncio
async def test_async_register_registers_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure the integration registers its system health callback when legacy API is used."""

    calls: list[tuple[object, str, object]] = []

    def _capture_register(hass: object, domain: str, handler: object) -> None:
        calls.append((hass, domain, handler))

    monkeypatch.setattr(
        system_health,
        "system_health_component",
        SimpleNamespace(async_register_info=_capture_register),
    )

    hass = object()
    await system_health.async_register(hass)

    assert calls == [(hass, DOMAIN, system_health.async_get_system_health_info)]


@pytest.mark.asyncio
async def test_async_get_system_health_info_redacts_email() -> None:
    """System health info must expose account data without PII."""

    coordinator = _FakeCoordinator()
    hass = _FakeHass(coordinator)

    info = await system_health.async_get_system_health_info(hass)  # type: ignore[arg-type]

    assert info["loaded_entries"] == 1
    assert info["fcm"]["available"] is True
    assert info["fcm"]["client_count"] == 1

    payload = info["entries"][0]
    assert payload["entry_id"] == "entry-test"
    assert payload["devices_loaded"] == 2
    assert (
        payload["last_successful_update"]
        == coordinator.last_update_success_time.isoformat()
    )
    assert payload["fcm_status"]["changed_at"] == "1970-01-01T00:02:03Z"

    account_hash = payload.get("account_hash")
    assert isinstance(account_hash, str)
    assert account_hash.startswith("sha256:")
    assert "user@example.com" not in account_hash
    assert "user@example.com" not in repr(info)
