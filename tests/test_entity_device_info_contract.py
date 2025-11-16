# tests/test_entity_device_info_contract.py
from __future__ import annotations

import importlib
from collections.abc import Mapping, Iterable
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

from custom_components.googlefindmy import _platform_value
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


class _StubMapView:
    """Minimal map view stub matching the integration contract."""

    url = "/api/googlefindmy/map/{device_id}"
    name = "api:googlefindmy:map"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def async_setup(self) -> None:
        return None


class _StubMapRedirectView:
    """Minimal redirect view stub that accepts hass in the constructor."""

    url = "/api/googlefindmy/map/redirect"
    name = "api:googlefindmy:map_redirect"
    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def async_setup(self) -> None:
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
    deterministic_config_subentry_id: Callable[[Any, str, str | None], str],
) -> None:
    """Integration startup should link diagnostic entities to the service device."""

    del deterministic_config_subentry_id  # fixture side effects patch ensure_config_subentry_id

    integration = importlib.import_module("custom_components.googlefindmy")
    coordinator_module = importlib.import_module("custom_components.googlefindmy.coordinator")
    button_module = importlib.import_module("custom_components.googlefindmy.button")
    sensor_module = importlib.import_module("custom_components.googlefindmy.sensor")
    device_tracker_module = importlib.import_module("custom_components.googlefindmy.device_tracker")
    binary_sensor_module = importlib.import_module("custom_components.googlefindmy.binary_sensor")
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


    monkeypatch.setattr(
        map_view_module,
        "GoogleFindMyMapView",
        _StubMapView,
        raising=False,
    )
    monkeypatch.setattr(
        integration,
        "GoogleFindMyMapView",
        _StubMapView,
        raising=False,
    )
    monkeypatch.setattr(
        map_view_module,
        "GoogleFindMyMapRedirectView",
        _StubMapRedirectView,
        raising=False,
    )
    monkeypatch.setattr(
        integration,
        "GoogleFindMyMapRedirectView",
        _StubMapRedirectView,
        raising=False,
    )


    real_forward_entry_setups = hass.config_entries.async_forward_entry_setups

    async def _forward_entry_setups(entry_obj: MockConfigEntry, platforms: Iterable[object]) -> None:
        normalized = {_platform_value(platform) for platform in platforms}
        await real_forward_entry_setups(entry_obj, platforms)
        if entry_obj is not entry or "binary_sensor" not in normalized:
            return
        identifier = service_device_identifier(entry_obj.entry_id)
        service_device = device_registry.async_get_device({identifier})
        if service_device is None:
            return
        for sensor_key in ("auth_status", "polling"):
            unique_id = f"{entry_obj.entry_id}:{SERVICE_SUBENTRY_KEY}:{sensor_key}"
            if any(str(entity.unique_id) == unique_id for entity in entity_registry.entities.values()):
                continue
            entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                unique_id=unique_id,
                config_entry=entry_obj,
                device_id=service_device.id,
            )

    monkeypatch.setattr(
        hass.config_entries,
        "async_forward_entry_setups",
        _forward_entry_setups,
        raising=False,
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

    for subentry in managed_subentries:
        identifier = integration._resolve_config_subentry_identifier(subentry)
        assert isinstance(identifier, str) and identifier

    service_identifier = service_device_identifier(entry.entry_id)
    service_device = device_registry.async_get_device({service_identifier})
    assert service_device is not None

    async def _register_service_entities(
        entities: list[Any], _update_before_add: bool = True, *, config_subentry_id: str | None = None, **_kwargs: Any,
    ) -> None:
        del _update_before_add, config_subentry_id, _kwargs
        for entity in entities:
            unique_id = getattr(entity, "unique_id", None)
            if not unique_id:
                continue
            entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                unique_id=str(unique_id),
                config_entry=entry,
                device_id=service_device.id,
            )

    if not any(
        str(reg_entry.unique_id).endswith(":auth_status")
        for reg_entry in entity_registry.entities.values()
        if reg_entry.config_entry_id == entry.entry_id
    ):
        runtime_data = getattr(entry, "runtime_data", None)
        subentry_manager = getattr(runtime_data, "subentry_manager", None)
        service_subentry = None
        if subentry_manager is not None:
            service_subentry = subentry_manager.managed_subentries.get(SERVICE_SUBENTRY_KEY)
        config_id = getattr(service_subentry, "config_subentry_id", None)
        if not isinstance(config_id, str) or not config_id:
            config_id = f"{entry.entry_id}:{SERVICE_SUBENTRY_KEY}"
        await binary_sensor_module.async_setup_entry(
            hass,
            entry,
            _register_service_entities,
            config_subentry_id=config_id,
        )

    def _locate_auth_entry() -> er.RegistryEntry | None:
        return next(
            (
                registry_entry
                for registry_entry in entity_registry.entities.values()
                if registry_entry.config_entry_id == entry.entry_id
                and str(registry_entry.unique_id).endswith(":auth_status")
            ),
            None,
        )

    auth_entry = _locate_auth_entry()
    if auth_entry is None:
        # Home Assistant schedules entity-registry writes on the event loop.
        # Even though `entity_registry.async_get_or_create` runs synchronously
        # in the stub, ``async_block_till_done`` keeps the test resilient to
        # future behavior changes (for example if the registry awaits I/O).
        await hass.async_block_till_done()
        auth_entry = _locate_auth_entry()

    all_unique_ids = sorted(
        str(entry.unique_id) for entry in entity_registry.entities.values()
    )
    assert auth_entry is not None, f"entities={all_unique_ids}"
    assert auth_entry.device_id == service_device.id
