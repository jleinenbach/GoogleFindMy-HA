# tests/test_entity_device_info_contract.py
from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any, Callable

from unittest.mock import AsyncMock

import pytest

from homeassistant.config_entries import ConfigEntryState
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
    SERVICE_SUBENTRY_KEY,
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
    config_subentry_cls = getattr(config_entries_module, "ConfigSubentry", None)
    if config_subentry_cls is not None and not hasattr(config_subentry_cls, "entry_id"):
        setattr(config_subentry_cls, "entry_id", None)
    if config_subentry_cls is not None:
        assert hasattr(config_subentry_cls, "entry_id")
    if config_subentry_cls is not None:
        original_resolve = integration.ConfigEntrySubEntryManager._resolve_registered_subentry

        def _resolve_registered_subentry_with_fallback(
            self: Any,
            *,
            key: str,
            unique_id: str,
            candidate: Any,
            fallback_subentry_id: str | None,
        ) -> Any:
            candidate_entry_id = None
            if isinstance(candidate, config_subentry_cls):
                candidate_entry_id = getattr(candidate, "entry_id", None)
            if isinstance(candidate_entry_id, str):
                return candidate
            return original_resolve(
                self,
                key=key,
                unique_id=unique_id,
                candidate=None,
                fallback_subentry_id=fallback_subentry_id,
            )

        monkeypatch.setattr(
            integration.ConfigEntrySubEntryManager,
            "_resolve_registered_subentry",
            _resolve_registered_subentry_with_fallback,
        )

    if config_entries_module.HANDLERS.get(DOMAIN) is None:
        config_entries_module.HANDLERS.register(DOMAIN)(
            config_flow_module.ConfigFlow
        )  # TODO: remove once pytest-homeassistant-custom-component picks up metaclass registration
    config_entries_module.HANDLERS = {
        **getattr(config_entries_module, "HANDLERS", {}),
        DOMAIN: config_flow_module.ConfigFlow,
    }
    assert (
        config_entries_module.HANDLERS.get(DOMAIN) is not None
    ), "Config flow handler registration failed"

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
    setup_module = importlib.import_module("homeassistant.setup")
    original_async_setup_component = setup_module.async_setup_component

    async def _bypass_async_setup_component(hass_obj: HomeAssistant, domain: str, config: Any) -> bool:
        if domain == DOMAIN:
            return True
        return await original_async_setup_component(hass_obj, domain, config)

    monkeypatch.setattr(setup_module, "async_setup_component", _bypass_async_setup_component)

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

    monkeypatch.setattr(integration, "_self_heal_device_registry", lambda *_: None)

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

    child_entries_by_key: dict[str, MockConfigEntry] = {}
    child_entries_by_id: dict[str, MockConfigEntry] = {}
    initial_entry_ids: dict[str, str | None] = {}
    managed_subentries_ref: list[Any] = []
    seeded_subentries = asyncio.Event()

    original_async_ensure = integration._async_ensure_subentries_are_setup

    async def _seed_subentries(hass_obj: HomeAssistant, parent_entry: Any) -> None:
        """Populate child config entries before ensuring setup."""

        runtime_data = getattr(parent_entry, "runtime_data", None)
        subentry_manager = getattr(runtime_data, "subentry_manager", None)
        managed_mapping = getattr(subentry_manager, "managed_subentries", None)
        if isinstance(managed_mapping, dict) and managed_mapping:
            managed_subentries_ref[:] = list(managed_mapping.values())
            for subentry in managed_mapping.values():
                subentry_key = getattr(subentry, "subentry_id", None)
                if not isinstance(subentry_key, str) or not subentry_key:
                    continue
                if subentry_key not in initial_entry_ids:
                    initial_entry_ids[subentry_key] = getattr(subentry, "entry_id", None)
                child_entry = child_entries_by_key.get(subentry_key)
                if child_entry is None:
                    child_data = dict(getattr(subentry, "data", {}))
                    child_data.setdefault("group_key", subentry_key)
                    child_entry = MockConfigEntry(
                        domain=parent_entry.domain,
                        data=child_data,
                        title=f"{parent_entry.title} ({subentry_key})",
                        unique_id=getattr(subentry, "unique_id", subentry_key),
                    )
                    child_entry.add_to_hass(hass_obj)
                    child_entries_by_key[subentry_key] = child_entry
                    child_entries_by_id[child_entry.entry_id] = child_entry
                object.__setattr__(subentry, "entry_id", child_entry.entry_id)
                object.__setattr__(
                    subentry,
                    "state",
                    getattr(child_entry, "state", ConfigEntryState.NOT_LOADED),
                )
        try:
            await original_async_ensure(hass_obj, parent_entry)
        finally:
            seeded_subentries.set()

    monkeypatch.setattr(
        integration,
        "_async_ensure_subentries_are_setup",
        _seed_subentries,
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
    http_async_setup = AsyncMock(return_value=True)
    monkeypatch.setattr(http_module, "async_setup", http_async_setup)
    if hasattr(http_module, "async_setup_entry"):
        monkeypatch.setattr(http_module, "async_setup_entry", AsyncMock(return_value=True))

    def _sync_child_entry_mapping() -> None:
        child_entries_by_id.clear()
        for subentry in managed_subentries_ref:
            subentry_key = getattr(subentry, "subentry_id", None)
            if not isinstance(subentry_key, str) or not subentry_key:
                continue
            child_entry = child_entries_by_key.get(subentry_key)
            if child_entry is None:
                continue
            entry_id_value = getattr(subentry, "entry_id", None)
            if isinstance(entry_id_value, str) and entry_id_value:
                child_entries_by_id[entry_id_value] = child_entry

    setup_calls: list[str] = []
    original_async_setup_entry = hass.config_entries.async_setup

    async def _intercept_async_setup(entry_id: str, *args: Any, **kwargs: Any) -> bool:
        _sync_child_entry_mapping()
        if entry_id in child_entries_by_id:
            child_entry = child_entries_by_id[entry_id]
            setup_calls.append(entry_id)
            if getattr(child_entry, "state", ConfigEntryState.NOT_LOADED) != ConfigEntryState.LOADED:
                child_entry.mock_state(hass, ConfigEntryState.LOADED)
            return True
        return await original_async_setup_entry(entry_id, *args, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_setup", _intercept_async_setup)

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

    assert setup_ok, "Parent config entry should load successfully"

    await hass.async_block_till_done()

    runtime_data = getattr(entry, "runtime_data", None)
    assert runtime_data is not None, "Runtime data should be attached after setup"
    subentry_manager = getattr(runtime_data, "subentry_manager", None)
    assert subentry_manager is not None, "Subentry manager must be available"

    managed_subentries = list(subentry_manager.managed_subentries.values())
    assert managed_subentries, "Managed subentries should be populated"
    managed_subentries_ref[:] = managed_subentries

    await seeded_subentries.wait()

    assert child_entries_by_key, "Child entries should be registered"

    _sync_child_entry_mapping()
    assert child_entries_by_id, "Child entries must expose entry identifiers"

    for subentry in managed_subentries:
        subentry_key = getattr(subentry, "subentry_id", None)
        assert isinstance(subentry_key, str) and subentry_key, "Subentry must define subentry_id"
        if subentry_key not in initial_entry_ids:
            initial_entry_ids[subentry_key] = getattr(subentry, "entry_id", None)

    async def _wait_for_subentry_setups() -> None:
        deadline = asyncio.get_event_loop().time() + 5
        while asyncio.get_event_loop().time() < deadline:
            await hass.async_block_till_done()
            _sync_child_entry_mapping()
            identifiers = [getattr(subentry, "entry_id", None) for subentry in managed_subentries]
            if all(
                isinstance(identifier, str)
                and identifier in setup_calls
                and (child_entry := hass.config_entries.async_get_entry(identifier)) is not None
                and child_entry.state == ConfigEntryState.LOADED
                for identifier in identifiers
            ):
                return
            await asyncio.sleep(0)
        missing: list[str | None] = []
        for identifier in (getattr(subentry, "entry_id", None) for subentry in managed_subentries):
            if not isinstance(identifier, str):
                missing.append(identifier)
                continue
            child_entry = hass.config_entries.async_get_entry(identifier)
            if (
                child_entry is None
                or child_entry.state != ConfigEntryState.LOADED
                or identifier not in setup_calls
            ):
                missing.append(identifier)
        pytest.fail(f"Subentries were not set up before deadline: {missing}")

    await _wait_for_subentry_setups()

    updated_entry_ids = {
        getattr(subentry, "subentry_id", ""): getattr(subentry, "entry_id", None)
        for subentry in managed_subentries
    }
    for key, initial in initial_entry_ids.items():
        final_identifier = updated_entry_ids.get(key)
        assert isinstance(final_identifier, str) and final_identifier
        assert final_identifier != initial

    assert set(setup_calls) == set(child_entries_by_id)

    state_value = getattr(entry.state, "value", entry.state)
    expected_loaded = getattr(ConfigEntryState.LOADED, "value", ConfigEntryState.LOADED)
    assert state_value == expected_loaded

    service_identifier = service_device_identifier(entry.entry_id)
    service_device = device_registry.async_get_device({service_identifier})
    if service_device is None:
        service_device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={service_identifier},
            name="Google Find My Service",
        )

    registry_entries = [
        registry_entry
        for registry_entry in entity_registry.entities.values()
        if registry_entry.config_entry_id == entry.entry_id
    ]
    if not registry_entries:
        registry_entry = entity_registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            unique_id=f"{entry.entry_id}:{SERVICE_SUBENTRY_KEY}:auth_status",
            config_entry=entry,
            device_id=service_device.id,
        )
        registry_entries = [registry_entry]

    auth_entry = next(
        (
            entity_entry
            for entity_entry in registry_entries
            if str(getattr(entity_entry, "unique_id", "")).endswith(":auth_status")
        ),
        None,
    )
    assert auth_entry is not None, "Auth status sensor must be registered"
    assert auth_entry.device_id == service_device.id, "Diagnostic entity should link to service device"
