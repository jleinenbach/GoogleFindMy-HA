# tests/test_binary_sensor_icons.py
"""Unit tests ensuring diagnostic binary sensor icons match auth state."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

if "homeassistant.components.binary_sensor" not in sys.modules:
    binary_sensor_module = ModuleType("homeassistant.components.binary_sensor")

    class BinarySensorEntity:  # noqa: D401 - stub for BinarySensorEntity base class
        """Minimal stand-in for Home Assistant's BinarySensorEntity."""

        def __init__(self, *args, **kwargs) -> None:
            return None

    class BinarySensorEntityDescription:
        """Lightweight entity description capturing keyword arguments."""

        def __init__(self, key: str, **kwargs) -> None:  # noqa: D401 - stub signature
            self.key = key
            for name, value in kwargs.items():
                setattr(self, name, value)

    class BinarySensorDeviceClass:
        """Enum-style container for binary sensor device classes."""

        PROBLEM = "problem"

    binary_sensor_module.BinarySensorEntity = BinarySensorEntity
    binary_sensor_module.BinarySensorEntityDescription = BinarySensorEntityDescription
    binary_sensor_module.BinarySensorDeviceClass = BinarySensorDeviceClass
    sys.modules["homeassistant.components.binary_sensor"] = binary_sensor_module

if "homeassistant.helpers.entity" not in sys.modules:
    entity_module = ModuleType("homeassistant.helpers.entity")

    class DeviceInfo:  # noqa: D401 - stub for DeviceInfo dataclass
        def __init__(self, **kwargs) -> None:
            for name, value in kwargs.items():
                setattr(self, name, value)

    class EntityCategory:
        """Enum-style placeholder for entity categories."""

        DIAGNOSTIC = "diagnostic"

    entity_module.DeviceInfo = DeviceInfo
    entity_module.EntityCategory = EntityCategory
    sys.modules["homeassistant.helpers.entity"] = entity_module

if "homeassistant.helpers.entity_platform" not in sys.modules:
    entity_platform_module = ModuleType("homeassistant.helpers.entity_platform")

    class AddEntitiesCallback:  # noqa: D401 - stub callable for platform setup
        def __call__(self, entities, update_before_add: bool = False) -> None:
            return None

    entity_platform_module.AddEntitiesCallback = AddEntitiesCallback
    sys.modules["homeassistant.helpers.entity_platform"] = entity_platform_module

device_registry_module = sys.modules.get("homeassistant.helpers.device_registry")
if device_registry_module is None:
    device_registry_module = ModuleType("homeassistant.helpers.device_registry")
    sys.modules["homeassistant.helpers.device_registry"] = device_registry_module

if not hasattr(device_registry_module, "DeviceEntryType"):
    class DeviceEntryType:  # noqa: D401 - stub enum container
        SERVICE = "service"

    device_registry_module.DeviceEntryType = DeviceEntryType

core_module = sys.modules.get("homeassistant.core")
if core_module is not None and not hasattr(core_module, "Event"):

    class Event:  # noqa: D401 - stub for Home Assistant Event objects
        def __init__(self, event_type: str, data: dict | None = None) -> None:
            self.event_type = event_type
            self.data = data or {}

    core_module.Event = Event

update_module = sys.modules.get("homeassistant.helpers.update_coordinator")
if update_module is not None and not hasattr(update_module, "CoordinatorEntity"):

    class CoordinatorEntity:
        """Minimal CoordinatorEntity stub retaining the coordinator reference."""

        def __init__(self, coordinator) -> None:  # noqa: D401 - stub signature
            self.coordinator = coordinator

        def async_write_ha_state(self) -> None:  # pragma: no cover - stub behaviour
            return None

        def __class_getitem__(cls, _item):  # noqa: D401 - support generic subscription
            return cls

    update_module.CoordinatorEntity = CoordinatorEntity


from custom_components.googlefindmy.binary_sensor import (  # noqa: E402 - import after stubs
    GoogleFindMyAuthStatusSensor,
    GoogleFindMyPollingSensor,
)
from custom_components.googlefindmy.const import (  # noqa: E402 - import after stubs
    DOMAIN,
    SERVICE_SUBENTRY_KEY,
    service_device_identifier,
)

SERVICE_SUBENTRY_IDENTIFIER = "entry-id-service-subentry"


@pytest.mark.parametrize(
    ("event_state", "expected_icon"),
    [
        (True, "mdi:account-alert"),
        (False, "mdi:account-check"),
    ],
)
def test_auth_status_sensor_icon(event_state: bool, expected_icon: str) -> None:
    """Auth status sensor exposes state-specific icons for clarity."""

    coordinator = SimpleNamespace(api_status=None)
    entry = SimpleNamespace(entry_id="entry-id")
    sensor = GoogleFindMyAuthStatusSensor(
        coordinator,
        entry,
        subentry_key=SERVICE_SUBENTRY_KEY,
        subentry_identifier=SERVICE_SUBENTRY_IDENTIFIER,
    )

    # Force the fast-path state without requiring Home Assistant event bus.
    sensor._event_state = event_state

    assert sensor.subentry_key == SERVICE_SUBENTRY_KEY
    assert sensor.icon == expected_icon


def test_auth_status_sensor_attributes_include_nova_snapshots() -> None:
    """Auth status sensor exposes Nova API and FCM diagnostic fields."""

    coordinator = SimpleNamespace(
        api_status=SimpleNamespace(
            state="reauth_required",
            reason="Token expired",
            changed_at=1700000000.0,
        ),
        fcm_status=SimpleNamespace(
            state="connected",
            reason=None,
            changed_at=1700000100.5,
        ),
    )
    entry = SimpleNamespace(entry_id="entry-id")
    sensor = GoogleFindMyAuthStatusSensor(
        coordinator,
        entry,
        subentry_key=SERVICE_SUBENTRY_KEY,
        subentry_identifier=SERVICE_SUBENTRY_IDENTIFIER,
    )

    attrs = sensor.extra_state_attributes

    assert attrs is not None
    assert attrs["nova_api_status"] == "reauth_required"
    assert attrs["nova_api_status_reason"] == "Token expired"
    assert attrs["nova_api_status_changed_at"] == "2023-11-14T22:13:20Z"
    assert attrs["nova_fcm_status"] == "connected"
    assert "nova_fcm_status_reason" not in attrs
    assert attrs["nova_fcm_status_changed_at"] == "2023-11-14T22:15:00.500000Z"


def test_auth_status_sensor_attributes_return_none_when_unavailable() -> None:
    """Missing status snapshots result in no extra attributes."""

    coordinator = SimpleNamespace(api_status=None, fcm_status=None)
    entry = SimpleNamespace(entry_id="entry-id")
    sensor = GoogleFindMyAuthStatusSensor(
        coordinator,
        entry,
        subentry_key=SERVICE_SUBENTRY_KEY,
        subentry_identifier=SERVICE_SUBENTRY_IDENTIFIER,
    )

    assert sensor.subentry_key == SERVICE_SUBENTRY_KEY
    assert sensor.extra_state_attributes is None


def test_service_diagnostic_sensors_share_service_device_identifiers() -> None:
    """Service diagnostics attach the same identifier set to the hub device."""

    coordinator = SimpleNamespace(api_status=None, fcm_status=None)
    entry = SimpleNamespace(entry_id="entry-id")
    coordinator.config_entry = entry
    polling = GoogleFindMyPollingSensor(
        coordinator,
        entry,
        subentry_key=SERVICE_SUBENTRY_KEY,
        subentry_identifier=SERVICE_SUBENTRY_IDENTIFIER,
    )
    auth = GoogleFindMyAuthStatusSensor(
        coordinator,
        entry,
        subentry_key=SERVICE_SUBENTRY_KEY,
        subentry_identifier=SERVICE_SUBENTRY_IDENTIFIER,
    )

    assert polling.subentry_key == SERVICE_SUBENTRY_KEY
    assert auth.subentry_key == SERVICE_SUBENTRY_KEY
    expected_identifiers = {
        service_device_identifier("entry-id"),
        (DOMAIN, f"entry-id:{SERVICE_SUBENTRY_IDENTIFIER}:service"),
    }

    assert polling.device_info.identifiers == expected_identifiers
    assert auth.device_info.identifiers == expected_identifiers
