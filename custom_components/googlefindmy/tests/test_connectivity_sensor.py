from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.binary_sensor import GoogleFindMyConnectivitySensor


def _build_sensor(coordinator: Any) -> GoogleFindMyConnectivitySensor:
    sensor = GoogleFindMyConnectivitySensor.__new__(GoogleFindMyConnectivitySensor)
    sensor._subentry_identifier = "service"
    sensor._subentry_key = "service"
    sensor._attr_unique_id = "entry:service:connectivity"
    sensor.coordinator = coordinator
    sensor.hass = SimpleNamespace()
    return sensor


def test_connectivity_sensor_tracks_push_and_attributes() -> None:
    api = SimpleNamespace()
    api.is_push_ready = lambda: True
    api.fcm = SimpleNamespace(
        get_last_connected_wall_time=lambda entry_id: 1_700_000_000.0,
    )

    coordinator = SimpleNamespace(
        api=api,
        consecutive_timeouts=2,
        last_poll_result="failed",
        is_fcm_connected=True,
        fcm_status=SimpleNamespace(changed_at=1_650_000_000.0),
        config_entry=SimpleNamespace(entry_id="entry"),
    )

    sensor = _build_sensor(coordinator)

    assert sensor.is_on is True
    attributes = sensor.extra_state_attributes
    assert attributes is not None
    assert attributes.get("last_poll_result") == "failed"
    assert attributes.get("consecutive_timeouts") == coordinator.consecutive_timeouts
    assert attributes.get("fcm_connected_at") is not None

    api.is_push_ready = lambda: False
    assert sensor.is_on is False


@pytest.mark.asyncio
async def test_health_changes_notify_coordinators(monkeypatch: pytest.MonkeyPatch) -> None:
    receiver = FcmReceiverHA()

    async def _noop_start(entry_id: str, cache: Any) -> None:
        return None

    monkeypatch.setattr(receiver, "_start_supervisor_for_entry", _noop_start)

    notifications: list[bool] = []

    class _Coordinator(SimpleNamespace):
        def async_update_listeners(self) -> None:  # noqa: D401
            """Record listener notifications."""

            notifications.append(True)

    coordinator = _Coordinator(config_entry=SimpleNamespace(entry_id="entry-a"))
    receiver.register_coordinator(coordinator)

    receiver._update_entry_health("entry-a", True)
    receiver._update_entry_health("entry-a", False)

    assert notifications == [True, True]
