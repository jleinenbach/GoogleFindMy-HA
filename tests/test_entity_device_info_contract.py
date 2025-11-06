# tests/test_entity_device_info_contract.py

from __future__ import annotations

from types import SimpleNamespace
from typing import Callable

import pytest

from custom_components.googlefindmy.const import (
    SERVICE_SUBENTRY_KEY,
    TRACKER_SUBENTRY_KEY,
    service_device_identifier,
)
from custom_components.googlefindmy.entity import (
    GoogleFindMyDeviceEntity,
    GoogleFindMyEntity,
)


class _CoordinatorStub:
    """Lightweight coordinator stub satisfying CoordinatorEntity requirements."""

    def __init__(self, entry_id: str) -> None:
        self.config_entry = SimpleNamespace(
            entry_id=entry_id,
            data={},
            options={},
            title="Google Find My",
        )
        self.hass: SimpleNamespace | None = None
        self._listeners: list[Callable[[], None]] = []

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: None


class _ServiceEntity(GoogleFindMyEntity):
    """Concrete subclass exposing the coordinator update hook."""

    def _handle_coordinator_update(self) -> None:  # pragma: no cover - stub
        return None


class _TrackerEntity(GoogleFindMyDeviceEntity):
    """Concrete device entity used to exercise ``device_info``."""

    def _handle_coordinator_update(self) -> None:  # pragma: no cover - stub
        return None


@pytest.fixture
def _patch_get_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure map URL helpers return a deterministic value during tests."""

    monkeypatch.setattr(
        "custom_components.googlefindmy.entity.get_url",
        lambda hass, **_: "https://example.invalid",
    )


def _build_hass_stub() -> SimpleNamespace:
    """Return a Home Assistant stub with deterministic core UUID."""

    return SimpleNamespace(data={"core.uuid": "test-ha"})


def test_service_device_info_excludes_config_entry_id(
    _patch_get_url: None,
) -> None:
    """Service-level entities must not expose ``config_entry_id`` in DeviceInfo."""

    coordinator = _CoordinatorStub("entry-service")
    hass = _build_hass_stub()
    coordinator.hass = hass

    entity = _ServiceEntity(
        coordinator,
        subentry_key=SERVICE_SUBENTRY_KEY,
        subentry_identifier="service-subentry",
    )
    entity.hass = hass

    info = entity.service_device_info(include_subentry_identifier=True)

    assert service_device_identifier("entry-service") in info.identifiers
    assert getattr(info, "config_entry_id", None) is None
    assert getattr(info, "via_device", None) is None


def test_device_entity_device_info_excludes_config_entry_id(
    _patch_get_url: None,
) -> None:
    """Per-device entities must omit ``config_entry_id`` from DeviceInfo payloads."""

    coordinator = _CoordinatorStub("entry-device")
    hass = _build_hass_stub()
    coordinator.hass = hass

    entity = _TrackerEntity(
        coordinator,
        {"id": "tracker-1", "name": "Keys"},
        subentry_key=TRACKER_SUBENTRY_KEY,
        subentry_identifier="tracker-subentry",
    )
    entity.hass = hass

    info = entity.device_info

    identifier_values = {value for _, value in info.identifiers}
    assert any(value.startswith("entry-device") for value in identifier_values)
    assert getattr(info, "config_entry_id", None) is None
    assert getattr(info, "via_device", None) is None
