import logging
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy.coordinator as coordinator_module


class _StubDevice:
    def __init__(self, identifier: str, registry_id: str | None = None) -> None:
        self.identifier = identifier
        self.id = registry_id or identifier
        self.custom_fields: dict[str, object] = {}
        self.disabled_by: str | None = None


class _StubDeviceRegistry:
    def __init__(self, devices: list[_StubDevice]) -> None:
        self._devices = devices

    def async_entries_for_config_entry(self, _entry_id: str) -> list[_StubDevice]:
        return list(self._devices)


class _StubHass:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def async_create_task(self, coro: Any):
        return coro


def _build_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> coordinator_module.GoogleFindMyCoordinator:
    registry = _StubDeviceRegistry([_StubDevice("device-1", registry_id="registry-1")])
    monkeypatch.setattr(coordinator_module.dr, "async_get", lambda hass: registry)

    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = _StubHass()
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1")
    coordinator._enabled_poll_device_ids = {"device-1"}
    coordinator._get_ignored_set = set
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    coordinator._device_location_data = {}
    coordinator._identity_key_to_devices = {}
    coordinator._propagating_location = False
    return coordinator


def test_decrypted_cache_identity_overrides_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = _build_coordinator(monkeypatch)
    coordinator._last_device_list = [
        {
            "id": "device-1",
            "encryptedIdentityKey": "aaaa",
            "owner_key_version": 1,
            "pairDate": 1_700_000_003,
        }
    ]
    coordinator._device_location_data = {
        "device-1": {
            "identity_key": b"\x01\x02",
            "encryptedIdentityKey": "bbbb",
            "owner_key_version": 2,
            "pair_date": 1_700_000_003,
        }
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.identity_key == b"\x01\x02"
    assert identity.encrypted_identity_key == bytes.fromhex("bbbb")
    assert identity.owner_key_version == 2


def test_poll_identity_used_when_cache_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _build_coordinator(monkeypatch)
    coordinator._last_device_list = [
        {
            "id": "device-1",
            "identityKey": "0c0d",
            "encryptedIdentityKey": "cccc",
            "owner_key_version": 4,
            "pairDate": 1_700_000_004,
        }
    ]
    coordinator._device_location_data = {
        "device-1": {
            "encryptedIdentityKey": "aaaa",
            "owner_key_version": 4,
            "pair_date": 1_700_000_004,
        }
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.identity_key == bytes.fromhex("0c0d")
    assert identity.encrypted_identity_key == bytes.fromhex("aaaa")
    assert identity.owner_key_version == 4


def test_cache_update_triggers_resolver_reset(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    coordinator = _build_coordinator(monkeypatch)
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *_args, **_kwargs: None
    coordinator._record_semantic_label = lambda *_args, **_kwargs: None
    coordinator._apply_semantic_mapping = lambda *_args, **_kwargs: None
    coordinator._apply_weighted_location_fusion = lambda *_args, **_kwargs: True
    coordinator._apply_report_type_cooldown = lambda *_args, **_kwargs: None
    coordinator.increment_stat = lambda *_args, **_kwargs: None
    coordinator._merge_with_existing_cache_row = lambda _dev_id, slot: slot
    coordinator._is_significant_update = lambda *_args, **_kwargs: True
    coordinator._ensure_device_name_cache = lambda: {}
    coordinator._device_location_data = {"device-1": {"identity_key": b"\x00\x00"}}

    reset_calls: list[str] = []
    refresh_calls: list[bool] = []
    coordinator._reset_resolver_offset = reset_calls.append
    coordinator._schedule_eid_resolver_refresh = lambda: refresh_calls.append(True)

    caplog.set_level(logging.INFO)
    coordinator.update_device_cache("device-1", {"identity_key": b"\x00\x01"})

    assert reset_calls == ["device-1"]
    assert refresh_calls == [True]
    assert any(
        "Identity key update detected" in record.message for record in caplog.records
    )
