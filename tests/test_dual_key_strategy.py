"""Ensure multiple identity key candidates are indexed for a single device."""

from __future__ import annotations

from types import SimpleNamespace

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


def _build_coordinator(monkeypatch):
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
    coordinator._last_device_list = []
    coordinator._device_location_data = {}
    return coordinator


def test_multiple_candidates_expand_identities(monkeypatch):
    coordinator = _build_coordinator(monkeypatch)
    coordinator._device_location_data = {
        "device-1": {
            "identity_key_candidates": [b"a" * 32, b"b" * 32],
            "encrypted_identity_key": b"enc",
            "owner_key_version": 7,
            "pair_date": 1_700_000_005,
            "identity_key": b"a" * 32,
        }
    }

    identities = coordinator.get_active_device_identities()

    assert len(identities) == 2
    keys = {identity.identity_key for identity in identities}
    assert keys == {b"a" * 32, b"b" * 32}
    for identity in identities:
        assert identity.canonical_id == "device-1"
        assert identity.encrypted_identity_key == b"enc"
        assert identity.owner_key_version == 7
