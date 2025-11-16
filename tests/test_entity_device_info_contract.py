# tests/test_entity_device_info_contract.py
from __future__ import annotations

import importlib
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any, Callable

from unittest.mock import AsyncMock

import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er

try:
    from pytest_homeassistant_custom_component.common import MockConfigEntry
except ModuleNotFoundError:  # pragma: no cover - environment guard
    pytest.fail(
        "pytest-homeassistant-custom-component must be installed. "
        "Install it alongside homeassistant before running the integration contract tests.",
        pytrace=False,
    )

from custom_components.googlefindmy.const import (
    DOMAIN,
    OPT_ENABLE_STATS_ENTITIES,
    SERVICE_FEATURE_PLATFORMS,
    SERVICE_SUBENTRY_KEY,
    TRACKER_FEATURE_PLATFORMS,
    TRACKER_SUBENTRY_KEY,
    service_device_identifier,
)

pytest_plugins = ("pytest_homeassistant_custom_component",)



@pytest.fixture(autouse=True)
def use_real_homeassistant_modules() -> Any:
    """Temporarily replace the stubbed Home Assistant modules with the real ones."""

    import sys

    saved_modules = {
        name: module for name, module in sys.modules.items() if name.startswith("homeassistant")
    }
    for name in list(sys.modules):
        if name.startswith("homeassistant"):
            del sys.modules[name]

    import homeassistant  # noqa: F401  # ensure the real package is loaded
    from homeassistant.helpers import aiohttp_client as _aiohttp_client

    if not hasattr(_aiohttp_client, '_async_make_resolver'):
        async def _async_make_resolver(*args: Any, **kwargs: Any) -> None:  # pragma: no cover - plugin shim
            return None

        _aiohttp_client._async_make_resolver = _async_make_resolver  # type: ignore[attr-defined]

    yield

    for name in list(sys.modules):
        if name.startswith("homeassistant"):
            del sys.modules[name]
    sys.modules.update(saved_modules)


class _DummyTokenCache:
    """In-memory token cache stub for integration setup."""

    def __init__(self) -> None:
        self._store: dict[str, Any] = {}

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        self._store[key] = value

    async def async_get_cached_value(self, key: str) -> Any:
        return self._store.get(key)

    async def flush(self) -> None:  # pragma: no cover - exercised implicitly
        return None


@pytest.mark.asyncio
async def test_integration_device_info_uses_service_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    stub_coordinator_factory: Callable[..., type[Any]],
    credentialed_config_entry_data: Callable[..., dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    enable_custom_integrations: None,
) -> None:
    """Integration startup should link diagnostic entities to the service device."""

    integration = importlib.import_module("custom_components.googlefindmy")
    coordinator_module = importlib.import_module("custom_components.googlefindmy.coordinator")
    button_module = importlib.import_module("custom_components.googlefindmy.button")
    map_view_module = importlib.import_module("custom_components.googlefindmy.map_view")

    monkeypatch.setattr(integration, "async_setup", AsyncMock(return_value=True))
    monkeypatch.setattr(integration, "CONFIG_SCHEMA", lambda config: {})

    config_flow_module = importlib.import_module("custom_components.googlefindmy.config_flow")
    config_entries_module = importlib.import_module("homeassistant.config_entries")
    if config_entries_module.HANDLERS.get(DOMAIN) is None:
        config_entries_module.HANDLERS.register(DOMAIN)(config_flow_module.ConfigFlow)

    async def _skip_migration(self: Any, hass_obj: HomeAssistant) -> bool:
        return True

    monkeypatch.setattr(
        config_entries_module.ConfigEntry,
        "async_migrate",
        _skip_migration,
        raising=False,
    )
    monkeypatch.setattr(
        MockConfigEntry,
        "async_migrate",
        _skip_migration,
        raising=False,
    )

    cache = _DummyTokenCache()
    monkeypatch.setattr(integration.TokenCache, "create", AsyncMock(return_value=cache))
    monkeypatch.setattr(integration, "_register_instance", lambda *_: None)
    monkeypatch.setattr(integration, "_unregister_instance", lambda *_: cache)

    async_defaults: dict[str, AsyncMock] = {
        "_async_soft_migrate_data_to_options": AsyncMock(return_value=None),
        "_async_migrate_unique_ids": AsyncMock(return_value=None),
        "_async_relink_button_devices": AsyncMock(return_value=None),
        "_async_relink_subentry_entities": AsyncMock(return_value=None),
        "_async_save_secrets_data": AsyncMock(return_value=None),
        "_async_seed_manual_credentials": AsyncMock(return_value=None),
        "_async_normalize_device_names": AsyncMock(return_value=None),
        "_async_release_shared_fcm": AsyncMock(return_value=None),
        "_async_self_heal_duplicate_entities": AsyncMock(return_value=None),
        "_ensure_post_migration_consistency": AsyncMock(return_value=(True, "user@example.com")),
    }
    for attribute, mock in async_defaults.items():
        monkeypatch.setattr(integration, attribute, mock, raising=False)

    dummy_fcm = SimpleNamespace(
        register_coordinator=lambda *_: None,
        unregister_coordinator=lambda *_: None,
        _start_listening=AsyncMock(return_value=None),
        request_stop=lambda: None,
    )
    monkeypatch.setattr(
        integration,
        "_async_acquire_shared_fcm",
        AsyncMock(return_value=dummy_fcm),
    )

    coordinator_cls = stub_coordinator_factory(
        data=[{"id": "tracker-1", "name": "Keys"}],
        stats={"background_updates": 2},
        service_subentry_key=SERVICE_SUBENTRY_KEY,
        subentry_key=TRACKER_SUBENTRY_KEY,
    )
    monkeypatch.setattr(coordinator_module, "GoogleFindMyCoordinator", coordinator_cls)
    monkeypatch.setattr(integration, "GoogleFindMyCoordinator", coordinator_cls)
    monkeypatch.setattr(button_module, "GoogleFindMyCoordinator", coordinator_cls)
    monkeypatch.setattr(map_view_module, "GoogleFindMyCoordinator", coordinator_cls, raising=False)

    if not hasattr(hass, "http") or hass.http is None:
        hass.http = SimpleNamespace(register_view=lambda *_: None)  # type: ignore[assignment]
    else:
        monkeypatch.setattr(hass.http, "register_view", lambda *_: None)

    http_module = importlib.import_module("homeassistant.components.http")
    monkeypatch.setattr(http_module, "async_setup", AsyncMock(return_value=True))
    if hasattr(http_module, "async_setup_entry"):
        monkeypatch.setattr(http_module, "async_setup_entry", AsyncMock(return_value=True))

    forward_calls: list[tuple[str | None, tuple[object, ...]]] = []

    def _normalize_platform_names(platforms: tuple[object, ...]) -> set[str]:
        names: set[str] = set()
        for platform in platforms:
            value = getattr(platform, "value", platform)
            if not isinstance(value, str):
                value = str(value)
            names.add(value)
        return names

    async def _capture_forward_entry_setups(
        entry_obj: MockConfigEntry,
        platforms: list[object],
        *,
        config_subentry_id: str | None = None,
        **_kwargs: Any,
    ) -> bool:
        platforms_tuple = tuple(platforms)
        platform_names = _normalize_platform_names(platforms_tuple)

        if config_subentry_id:
            identifier = service_device_identifier(entry_obj.entry_id)
            service_device = device_registry.async_get_or_create(
                config_entry_id=entry_obj.entry_id,
                identifiers={identifier},
                name="Google Find My Service",
            )
            if SERVICE_SUBENTRY_KEY in config_subentry_id:
                entity_registry.async_get_or_create(
                    "binary_sensor",
                    DOMAIN,
                    unique_id=f"{entry_obj.entry_id}:{SERVICE_SUBENTRY_KEY}:auth_status",
                    config_entry=entry_obj,
                    device_id=service_device.id,
                )
        else:
            if "binary_sensor" in platform_names:
                identifier = service_device_identifier(entry_obj.entry_id)
                service_device = device_registry.async_get_or_create(
                    config_entry_id=entry_obj.entry_id,
                    identifiers={identifier},
                    name="Google Find My Service",
                )
                entity_registry.async_get_or_create(
                    "binary_sensor",
                    DOMAIN,
                    unique_id=f"{entry_obj.entry_id}:{SERVICE_SUBENTRY_KEY}:auth_status",
                    config_entry=entry_obj,
                    device_id=service_device.id,
        )
        return True

    async def _capture_forward_entry_setups_bound(
        _manager: Any,
        entry_obj: MockConfigEntry,
        platforms: list[object],
        *,
        config_subentry_id: str | None = None,
        **_kwargs: Any,
    ) -> bool:
        return await _capture_forward_entry_setups(
            entry_obj,
            platforms,
            config_subentry_id=config_subentry_id,
            **_kwargs,
        )

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        _capture_forward_entry_setups,
        raising=False,
    )
    monkeypatch.setattr(
        config_entries_module.ConfigEntries,
        "async_forward_entry_setups",
        _capture_forward_entry_setups_bound,
        raising=False,
    )

    original_invoke = integration._invoke_with_optional_keyword

    def _record_forward_calls(
        callback: Callable[..., Any],
        args: tuple[Any, ...],
        keyword: str,
        value: Any,
    ) -> Any:
        if keyword == "config_subentry_id":
            platforms_arg: tuple[object, ...] = ()
            if len(args) >= 2 and isinstance(args[1], list):
                platforms_arg = tuple(args[1])
            forward_calls.append((value, platforms_arg))
        return original_invoke(callback, args, keyword, value)

    monkeypatch.setattr(
        integration,
        "_invoke_with_optional_keyword",
        _record_forward_calls,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id="gfm-parent-entry",
        unique_id="gfm-parent-entry",
        data=credentialed_config_entry_data(),
        options={OPT_ENABLE_STATS_ENTITIES: True},
        title="Integration Contract",
    )
    entry.add_to_hass(hass)

    try:
        setup_ok = await hass.config_entries.async_setup(entry.entry_id)
    except ConfigEntryNotReady as err:  # pragma: no cover - regression guard
        pytest.fail(f"Integration setup raised ConfigEntryNotReady: {err}")

    assert setup_ok is True
    await hass.async_block_till_done()

    runtime_data = getattr(entry, "runtime_data", None)
    assert runtime_data is not None
    subentry_manager = getattr(runtime_data, "subentry_manager", None)
    assert subentry_manager is not None

    managed_subentries = tuple(subentry_manager.managed_subentries.values())
    assert managed_subentries

    forwarded_by_identifier: dict[str, set[str]] = {}
    for forwarded_identifier, forwarded_platforms in forward_calls:
        assert isinstance(forwarded_identifier, str) and forwarded_identifier
        platform_names = _normalize_platform_names(forwarded_platforms)
        forwarded_by_identifier[forwarded_identifier] = platform_names
        assert len(forwarded_platforms) == len(platform_names)

    expected_platforms: dict[str, set[str]] = {}
    for subentry in managed_subentries:
        identifier = integration._resolve_config_subentry_identifier(subentry)
        assert isinstance(identifier, str) and identifier
        raw_data = getattr(subentry, "data", {}) if isinstance(subentry, object) else {}
        data = raw_data if isinstance(raw_data, Mapping) else {}
        group_key = data.get("group_key")
        if group_key == TRACKER_SUBENTRY_KEY:
            expected_platforms[identifier] = set(TRACKER_FEATURE_PLATFORMS)
            continue
        if group_key == SERVICE_SUBENTRY_KEY:
            expected_platforms[identifier] = set(SERVICE_FEATURE_PLATFORMS)
            continue

        features = data.get("features")
        if isinstance(features, (list, tuple, set)):
            expected_platforms[identifier] = {str(item) for item in features}
        else:
            expected_platforms[identifier] = set()

    assert forwarded_by_identifier == expected_platforms

    service_identifier = service_device_identifier(entry.entry_id)
    service_device = device_registry.async_get_device({service_identifier})
    assert service_device is not None

    auth_entry = next(
        (
            registry_entry
            for registry_entry in entity_registry.entities.values()
            if registry_entry.config_entry_id == entry.entry_id
            and str(registry_entry.unique_id).endswith(":auth_status")
        ),
        None,
    )
    assert auth_entry is not None
    assert auth_entry.device_id == service_device.id
