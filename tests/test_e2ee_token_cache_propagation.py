"""Regression tests for TokenCache propagation in E2EE helpers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _stub_homeassistant() -> None:
    """Install lightweight stubs for Home Assistant modules required at import time."""

    ha_pkg = sys.modules.setdefault("homeassistant", ModuleType("homeassistant"))
    ha_pkg.__path__ = getattr(ha_pkg, "__path__", [])  # mark as package

    config_entries = ModuleType("homeassistant.config_entries")

    class ConfigEntry:  # minimal placeholder
        pass

    class ConfigEntryState:  # minimal enum-like placeholder
        LOADED = "loaded"
        NOT_LOADED = "not_loaded"

    class ConfigEntryAuthFailed(Exception):
        pass

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigEntryState = ConfigEntryState
    config_entries.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    sys.modules["homeassistant.config_entries"] = config_entries

    const_module = ModuleType("homeassistant.const")

    class Platform:  # enum-like stub covering platforms used in __init__
        DEVICE_TRACKER = "device_tracker"
        BUTTON = "button"
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

    const_module.EVENT_HOMEASSISTANT_STARTED = "start"
    const_module.EVENT_HOMEASSISTANT_STOP = "stop"
    const_module.Platform = Platform
    sys.modules["homeassistant.const"] = const_module

    core_module = ModuleType("homeassistant.core")

    class CoreState:  # minimal CoreState stub
        running = "running"

    class HomeAssistant:  # minimal HomeAssistant placeholder
        state = CoreState.running

    core_module.CoreState = CoreState
    core_module.HomeAssistant = HomeAssistant
    core_module.callback = lambda func: func
    sys.modules["homeassistant.core"] = core_module

    exceptions_module = ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryNotReady(HomeAssistantError):
        pass

    exceptions_module.HomeAssistantError = HomeAssistantError
    exceptions_module.ConfigEntryNotReady = ConfigEntryNotReady
    exceptions_module.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    sys.modules["homeassistant.exceptions"] = exceptions_module

    helpers_pkg = sys.modules.setdefault(
        "homeassistant.helpers", ModuleType("homeassistant.helpers")
    )
    helpers_pkg.__path__ = getattr(helpers_pkg, "__path__", [])

    for sub in ("device_registry", "entity_registry", "issue_registry", "update_coordinator"):
        module_name = f"homeassistant.helpers.{sub}"
        module = ModuleType(module_name)
        sys.modules[module_name] = module
        setattr(helpers_pkg, sub, module)

    storage_module = ModuleType("homeassistant.helpers.storage")

    class Store:  # minimal async Store stub
        def __init__(self, *args, **kwargs) -> None:
            self._data: dict[str, object] | None = None

        async def async_load(self) -> dict[str, object] | None:
            return self._data

        def async_delay_save(self, *_args, **_kwargs) -> None:
            return None

    storage_module.Store = Store
    sys.modules["homeassistant.helpers.storage"] = storage_module
    setattr(helpers_pkg, "storage", storage_module)

    class UpdateFailed(Exception):
        pass

    sys.modules["homeassistant.helpers.update_coordinator"].UpdateFailed = UpdateFailed

    components_pkg = sys.modules.setdefault(
        "homeassistant.components", ModuleType("homeassistant.components")
    )
    components_pkg.__path__ = getattr(components_pkg, "__path__", [])

    recorder_module = ModuleType("homeassistant.components.recorder")
    recorder_module.get_instance = lambda *args, **kwargs: None
    recorder_module.history = ModuleType("homeassistant.components.recorder.history")
    sys.modules["homeassistant.components.recorder"] = recorder_module
    setattr(components_pkg, "recorder", recorder_module)


_stub_homeassistant()

components_pkg = sys.modules.setdefault(
    "custom_components", ModuleType("custom_components")
)
components_pkg.__path__ = [str(ROOT / "custom_components")]

gf_pkg = sys.modules.setdefault(
    "custom_components.googlefindmy", ModuleType("custom_components.googlefindmy")
)
gf_pkg.__path__ = [str(ROOT / "custom_components/googlefindmy")]
setattr(components_pkg, "googlefindmy", gf_pkg)


def test_async_retrieve_identity_key_threads_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure owner-key retrieval receives the entry TokenCache."""

    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
        decrypt_locations,
    )

    owner_calls: dict[str, object] = {}

    async def fake_async_get_owner_key(*, cache, **kwargs):  # type: ignore[no-untyped-def]
        owner_calls["owner_cache"] = cache
        return b"\x01" * 32

    async def fake_async_get_eid_info(*, cache):  # type: ignore[no-untyped-def]
        owner_calls["eid_cache"] = cache
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(ownerKeyVersion=1)
        )

    monkeypatch.setattr(
        decrypt_locations, "async_get_owner_key", fake_async_get_owner_key
    )
    monkeypatch.setattr(
        decrypt_locations, "async_get_eid_info", fake_async_get_eid_info
    )
    monkeypatch.setattr(decrypt_locations, "decrypt_eik", lambda *_: b"\x02" * 32)
    monkeypatch.setattr(decrypt_locations, "flip_bits", lambda data, _: data)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda _: False)

    class DummyEncrypted:
        encryptedIdentityKey = b"payload"
        ownerKeyVersion = 1

    class DummyRegistration:
        encryptedUserSecrets = DummyEncrypted()
        fastPairModelId = None

    cache = object()
    result = asyncio.run(
        decrypt_locations.async_retrieve_identity_key(
            DummyRegistration(), cache=cache
        )
    )

    assert result == b"\x02" * 32
    assert owner_calls["owner_cache"] is cache
    # Success path does not consult async_get_eid_info; ensure no unexpected call
    assert "eid_cache" not in owner_calls


def test_async_retrieve_identity_key_error_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even on failure the TokenCache must be propagated to diagnostics."""

    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
        decrypt_locations,
    )

    caches: dict[str, object] = {}

    async def fake_async_get_owner_key(*, cache, **kwargs):  # type: ignore[no-untyped-def]
        caches["owner_cache"] = cache
        return b"\x01" * 32

    async def fake_async_get_eid_info(*, cache):  # type: ignore[no-untyped-def]
        caches["eid_cache"] = cache
        return SimpleNamespace(
            encryptedOwnerKeyAndMetadata=SimpleNamespace(ownerKeyVersion=2)
        )

    monkeypatch.setattr(
        decrypt_locations, "async_get_owner_key", fake_async_get_owner_key
    )
    monkeypatch.setattr(
        decrypt_locations, "async_get_eid_info", fake_async_get_eid_info
    )

    def _raise_decrypt(*_: object) -> bytes:
        raise ValueError("boom")

    monkeypatch.setattr(decrypt_locations, "decrypt_eik", _raise_decrypt)
    monkeypatch.setattr(decrypt_locations, "flip_bits", lambda data, _: data)
    monkeypatch.setattr(decrypt_locations, "is_mcu_tracker", lambda _: False)

    class DummyEncrypted:
        encryptedIdentityKey = b"payload"
        ownerKeyVersion = 1

    class DummyRegistration:
        encryptedUserSecrets = DummyEncrypted()
        fastPairModelId = None

    cache = object()
    with pytest.raises(decrypt_locations.StaleOwnerKeyError):
        asyncio.run(
            decrypt_locations.async_retrieve_identity_key(
                DummyRegistration(), cache=cache
            )
        )

    assert caches["owner_cache"] is cache
    assert caches["eid_cache"] is cache


def test_async_get_eid_info_threads_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """`async_get_eid_info` must forward the cache to the Spot helper."""

    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices import (
        get_eid_info_request as module,
    )

    captured: dict[str, object] = {}

    async def fake_async_spot_request(scope, payload, *, cache):  # type: ignore[no-untyped-def]
        captured["scope"] = scope
        captured["payload"] = payload
        captured["cache"] = cache
        return b"response"

    class DummyResponse:
        def __init__(self) -> None:
            self.payload: bytes | None = None

        def ParseFromString(self, data: bytes) -> None:
            self.payload = data

    monkeypatch.setattr(
        "custom_components.googlefindmy.SpotApi.spot_request.async_spot_request",
        fake_async_spot_request,
    )
    monkeypatch.setattr(module, "_build_request_bytes", lambda: b"request")
    monkeypatch.setattr(
        module.DeviceUpdate_pb2,
        "GetEidInfoForE2eeDevicesResponse",
        DummyResponse,
    )

    cache = object()
    response = asyncio.run(module.async_get_eid_info(cache=cache))

    assert captured["scope"] == "GetEidInfoForE2eeDevices"
    assert captured["payload"] == b"request"
    assert captured["cache"] is cache
    assert isinstance(response, DummyResponse)
    assert response.payload == b"response"


def test_sync_decrypt_location_response_forwards_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The synchronous facade must forward the TokenCache to the async helper."""

    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
        decrypt_locations,
    )

    captured: dict[str, object] = {}

    async def fake_async_decrypt(device_update, *, cache):  # type: ignore[no-untyped-def]
        captured["device_update"] = device_update
        captured["cache"] = cache
        return ["ok"]

    monkeypatch.setattr(
        decrypt_locations,
        "async_decrypt_location_response_locations",
        fake_async_decrypt,
    )

    cache = object()
    device_update = object()

    result = decrypt_locations.decrypt_location_response_locations(
        device_update, cache=cache  # type: ignore[arg-type]
    )

    assert result == ["ok"]
    assert captured["device_update"] is device_update
    assert captured["cache"] is cache


def test_fcm_background_decode_uses_entry_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background FCM decoding must supply the entry TokenCache."""

    from custom_components.googlefindmy.Auth import fcm_receiver_ha

    receiver = fcm_receiver_ha.FcmReceiverHA()
    cache = object()
    receiver._entry_caches["entry"] = cache

    captured: dict[str, object] = {}

    def fake_parse(hex_string: str) -> str:
        captured["hex"] = hex_string
        return "parsed"

    def fake_sync_decrypt(device_update, *, cache):  # type: ignore[no-untyped-def]
        captured["device_update"] = device_update
        captured["cache"] = cache
        return [{"latitude": 1.0}]

    monkeypatch.setattr(
        "custom_components.googlefindmy.ProtoDecoders.decoder.parse_device_update_protobuf",
        fake_parse,
    )
    monkeypatch.setattr(
        "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations.decrypt_location_response_locations",
        fake_sync_decrypt,
    )

    result = receiver._decode_background_location("entry", "payload")

    assert result == {"latitude": 1.0}
    assert captured["hex"] == "payload"
    assert captured["device_update"] == "parsed"
    assert captured["cache"] is cache
