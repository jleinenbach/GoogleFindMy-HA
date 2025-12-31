from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.googlefindmy.coordinator as coordinator_module
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
    decrypt_locations as decrypt_module,
)


class _StatusEnum:
    SEMANTIC = 1

    @staticmethod
    def Name(code: int) -> str:
        return "SEMANTIC" if code == _StatusEnum.SEMANTIC else str(code)


class _LocationContainer:
    def __init__(self, location: Any, timestamp: int) -> None:
        self.networkLocations = [location]
        self.networkLocationTimestamps = [timestamp]
        self.recentLocation = location
        self.recentLocationTimestamp = timestamp

    def HasField(self, _name: str) -> bool:  # noqa: N802
        return False


def _build_coordinator(monkeypatch: pytest.MonkeyPatch) -> coordinator_module.GoogleFindMyCoordinator:
    coordinator = coordinator_module.GoogleFindMyCoordinator.__new__(
        coordinator_module.GoogleFindMyCoordinator
    )
    coordinator.hass = SimpleNamespace(async_create_task=lambda coro: coro, data={})
    coordinator.config_entry = SimpleNamespace(entry_id="entry-1")
    coordinator._enabled_poll_device_ids = set()
    coordinator._get_ignored_set = lambda: set()
    coordinator.data = []
    coordinator._device_location_data = {}
    coordinator._record_semantic_label = lambda *_args, **_kwargs: None
    coordinator._apply_semantic_mapping = lambda *_args, **_kwargs: None
    coordinator._apply_weighted_location_fusion = lambda *_args, **_kwargs: True
    coordinator._apply_report_type_cooldown = lambda *_args, **_kwargs: None
    coordinator._is_on_hass_loop = lambda: True
    coordinator._run_on_hass_loop = lambda *_args, **_kwargs: None
    coordinator._track_device_interval = lambda *_args, **_kwargs: None
    coordinator._is_significant_update = lambda *_args, **_kwargs: True
    coordinator._merge_with_existing_cache_row = lambda _dev_id, slot: slot
    coordinator.increment_stat = lambda *_args, **_kwargs: None
    coordinator._ensure_device_name_cache = lambda: {}
    coordinator._reset_resolver_offset = lambda *_args, **_kwargs: None
    coordinator._schedule_eid_resolver_refresh = lambda *_args, **_kwargs: None
    return coordinator


@pytest.mark.asyncio
async def test_decrypted_key_reaches_coordinator_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # FIX: identity_key must be exactly 32 bytes for hex conversion
    identity_key = b"new_decrypted_key_source_truth!!"  # 32 bytes
    timestamp = int(time.time())

    semantic_location = SimpleNamespace(
        status=_StatusEnum.SEMANTIC,
        semanticLocation=SimpleNamespace(locationName="Semantic"),
    )
    locations = _LocationContainer(semantic_location, timestamp)

    device_update_protobuf = SimpleNamespace(
        deviceMetadata=SimpleNamespace(
            information=SimpleNamespace(
                deviceRegistration=SimpleNamespace(
                    encryptedUserSecrets=SimpleNamespace(
                        encryptedIdentityKey=b"enc_bytes",
                        ownerKeyVersion=1,
                    )
                ),
                locationInformation=SimpleNamespace(
                    reports=SimpleNamespace(recentLocationAndNetworkLocations=locations)
                ),
            )
        )
    )

    common_stub = SimpleNamespace(Status=_StatusEnum)
    device_update_stub = SimpleNamespace(Location=lambda: SimpleNamespace(ParseFromString=lambda *_a, **_kw: None))

    async def _stub_async_retrieve_identity_key(*_args: Any, **_kwargs: Any) -> list[bytes]:
        return [identity_key]

    monkeypatch.setattr(decrypt_module, "_get_common_pb2", lambda: common_stub)
    monkeypatch.setattr(decrypt_module, "_get_device_update_pb2", lambda: device_update_stub)
    monkeypatch.setattr(
        decrypt_module,
        "async_retrieve_identity_key",
        _stub_async_retrieve_identity_key,
    )
    monkeypatch.setattr(decrypt_module, "is_mcu_tracker", lambda *_args: False)

    decrypted_payloads = await decrypt_module.async_decrypt_location_response_locations(
        device_update_protobuf, cache=SimpleNamespace()
    )

    assert decrypted_payloads
    payload = decrypted_payloads[0]
    # FIX: identity_key is now returned as hex string
    assert payload["identity_key"] == identity_key.hex()

    coordinator = _build_coordinator(monkeypatch)
    coordinator.update_device_cache("device-1", payload)

    stored_data = coordinator.get_device_location_data("device-1")
    assert stored_data is not None
    assert stored_data["identity_key"] == identity_key.hex()
