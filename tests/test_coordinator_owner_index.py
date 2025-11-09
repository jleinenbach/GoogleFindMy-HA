# tests/test_coordinator_owner_index.py
"""Owner-index maintenance and FCM fallback routing tests."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.coordinator import (
    FcmStatus,
    GoogleFindMyCoordinator,
)
from custom_components.googlefindmy.const import DOMAIN

from tests.test_coordinator_status import (
    _DummyAPI,
    _DummyCache,
    _DummyEntry,
    _DummyHass,
)

from tests.helpers import drain_loop


@pytest.fixture
def owner_index_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[GoogleFindMyCoordinator, _DummyAPI]:
    """Instantiate a coordinator with preseeded owner-index bucket."""

    api = _DummyAPI()

    def _factory(*_args, **_kwargs) -> _DummyAPI:
        return api

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyAPI",
        _factory,
    )
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    hass = _DummyHass(loop)
    hass.data.setdefault(DOMAIN, {})["device_owner_index"] = {}

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.GoogleFindMyCoordinator._async_load_stats",
        AsyncMock(return_value=None),
    )

    coordinator = GoogleFindMyCoordinator(hass, cache=_DummyCache())
    coordinator.config_entry = _DummyEntry()
    coordinator.async_set_updated_data = lambda _data: None
    coordinator._async_build_device_snapshot_with_fallbacks = AsyncMock(return_value=[])
    coordinator._async_start_poll_cycle = AsyncMock()
    coordinator._ensure_registry_for_devices = lambda *_args, **_kwargs: 0
    coordinator._schedule_short_retry = lambda *_args, **_kwargs: None
    coordinator._get_ignored_set = lambda: set()
    coordinator._is_fcm_ready_soft = lambda: True
    coordinator._set_fcm_status(FcmStatus.CONNECTED)

    try:
        yield coordinator, api
    finally:
        drain_loop(loop)


def test_owner_index_updates_with_refresh(
    owner_index_coordinator: tuple[GoogleFindMyCoordinator, _DummyAPI],
) -> None:
    """Coordinator refresh claims canonical ids and prunes stale entries."""

    coordinator, api = owner_index_coordinator
    owner_index = coordinator.hass.data[DOMAIN]["device_owner_index"]
    owner_index.update(
        {
            "other-entry-device": "other-entry",
            "stale-device": coordinator.config_entry.entry_id,
        }
    )

    api.device_list = [
        {"id": "device-a", "name": "Device A"},
        {"id": "device-b", "name": "Device B"},
    ]
    coordinator._get_ignored_set = lambda: {"device-b"}

    coordinator.hass.loop.run_until_complete(coordinator._async_update_data())

    assert owner_index["device-a"] == coordinator.config_entry.entry_id
    assert owner_index["device-b"] == coordinator.config_entry.entry_id
    assert owner_index["other-entry-device"] == "other-entry"
    assert "stale-device" not in owner_index


def test_fcm_owner_index_fallback_routes_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner-index mapping enables FCM routing when token context is missing."""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        receiver = FcmReceiverHA()
        hass = SimpleNamespace(
            data={DOMAIN: {"device_owner_index": {"device-z": "entry-target"}}}
        )
        receiver.attach_hass(hass)

        seen: list[set[str] | None] = []

        def _capture(entries: set[str] | None):
            seen.append(set(entries) if entries else entries)
            return []

        receiver._coordinators_for_entries = _capture  # type: ignore[assignment]
        monkeypatch.setattr(
            receiver,
            "_extract_canonic_id_from_response",
            lambda _hex: "device-z",
        )

        payload = base64.b64encode(b"payload").decode()
        receiver._on_notification(
            None,
            {"data": {"com.google.android.apps.adm.FCM_PAYLOAD": payload}},
            None,
            None,
        )

        assert seen == [{"entry-target"}]
    finally:
        drain_loop(loop)
