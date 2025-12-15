from types import SimpleNamespace

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

    def async_create_task(self, coro):
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
    coordinator._get_ignored_set = lambda: set()
    coordinator._extract_our_identifier = lambda device: getattr(
        device, "identifier", None
    )
    coordinator.data = []
    return coordinator


def test_cache_prioritized_over_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _build_coordinator(monkeypatch)
    coordinator._last_device_list = [
        {
            "id": "device-1",
            "identityKey": "1111",
            "encryptedIdentityKey": "aaaa",
            "owner_key_version": 1,
            "device_type": 1,
            "fastPairModelId": "poll-model",
            "pairDate": 1_700_000_001,
        }
    ]
    coordinator._device_location_data = {
        "device-1": {
            "identity_key": b"\x11\x11",
            "encryptedIdentityKey": "bbbb",
            "owner_key_version": 2,
            "device_type": 3,
            "fast_pair_model_id": "push-model",
            "pair_date": 1_700_000_001,
        }
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.encrypted_identity_key == bytes.fromhex("bbbb")
    assert identity.owner_key_version == 2
    assert identity.device_type == 3
    assert identity.fast_pair_model_id == "push-model"


def test_poll_used_when_cache_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = _build_coordinator(monkeypatch)
    coordinator._last_device_list = [
        {
            "id": "device-1",
            "encryptedIdentityKey": "cccc",
            "owner_key_version": 4,
            "device_type": 9,
            "fastPairModelId": "poll-fallback",
            "pairDate": 1_700_000_002,
            "identityKey": "dddd",
        }
    ]
    coordinator._device_location_data = {
        "device-1": {"pair_date": 1_700_000_002, "identity_key": b"\xdd\xdd"}
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 1
    identity = identities[0]
    assert identity.encrypted_identity_key == bytes.fromhex("cccc")
    assert identity.owner_key_version == 4
    assert identity.device_type == 9
    assert identity.fast_pair_model_id == "poll-fallback"
