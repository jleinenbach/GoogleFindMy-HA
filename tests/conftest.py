# tests/conftest.py
"""Test configuration and environment stubs for integration tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from datetime import datetime, timezone

# Ensure the package root is importable without installing the package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
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

    class ConfigFlow:
        """Minimal stub matching the ConfigFlow API used in tests."""

        VERSION = 1

        def __init_subclass__(cls, **kwargs):  # type: ignore[override]
            super().__init_subclass__()

        def __init__(self) -> None:
            self.context: dict[str, object] = {}
            self.hass = None

        async def async_show_form(
            self, *args, **kwargs
        ):  # pragma: no cover - defensive
            return {"type": "form"}

        async def async_show_menu(
            self, *args, **kwargs
        ):  # pragma: no cover - defensive
            return {"type": "menu"}

        async def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def add_suggested_values_to_schema(self, schema, suggested):  # noqa: D401 - stub
            return schema

    class OptionsFlow:
        """Minimal OptionsFlow stub for imports."""

        async def async_show_form(
            self, *args, **kwargs
        ):  # pragma: no cover - defensive
            return {"type": "form"}

        async def async_create_entry(
            self, *, title: str, data
        ):  # pragma: no cover - defensive
            return {"type": "create_entry", "title": title, "data": data}

    class OptionsFlowWithReload(OptionsFlow):
        """Placeholder inheriting OptionsFlow behaviour."""

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigEntryState = ConfigEntryState
    config_entries.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow
    config_entries.OptionsFlowWithReload = OptionsFlowWithReload
    sys.modules["homeassistant.config_entries"] = config_entries

    vol_module = ModuleType("voluptuous")

    class _Schema:
        def __init__(self, schema):
            self.schema = schema

        def __call__(self, value):  # pragma: no cover - defensive
            return value

    class _Marker:
        def __init__(self, key):
            self.key = key
            self.schema = {key}

        def __hash__(self) -> int:  # pragma: no cover - defensive
            return hash(self.key)

        def __eq__(self, other: object) -> bool:  # pragma: no cover - defensive
            if isinstance(other, _Marker):
                return self.key == other.key
            return self.key == other

    def _identity(value):
        return value

    vol_module.Schema = _Schema
    vol_module.Optional = lambda key, default=None: _Marker(key)
    vol_module.Required = lambda key, description=None: _Marker(key)
    vol_module.Any = lambda *items, **kwargs: _identity
    vol_module.All = lambda *validators, **kwargs: _identity
    vol_module.In = lambda items: _identity
    vol_module.Range = lambda **kwargs: _identity
    vol_module.Coerce = lambda typ: _identity

    sys.modules["voluptuous"] = vol_module

    data_entry_flow = ModuleType("homeassistant.data_entry_flow")
    data_entry_flow.FlowResult = dict  # type: ignore[assignment]
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow

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

    loader_module = ModuleType("homeassistant.loader")

    async def _async_get_integration(_hass, _domain):  # pragma: no cover - stub
        return SimpleNamespace(name="stub", version="0.0.0")

    loader_module.async_get_integration = _async_get_integration
    sys.modules["homeassistant.loader"] = loader_module
    setattr(ha_pkg, "loader", loader_module)

    core_module = ModuleType("homeassistant.core")

    class CoreState:  # minimal CoreState stub
        running = "running"

    class ServiceCall:  # pragma: no cover - stub for service handlers
        def __init__(self, data=None):
            self.data = data or {}

    class HomeAssistant:  # minimal HomeAssistant placeholder
        state = CoreState.running

    core_module.CoreState = CoreState
    core_module.HomeAssistant = HomeAssistant
    core_module.ServiceCall = ServiceCall
    core_module.callback = lambda func: func
    sys.modules["homeassistant.core"] = core_module

    exceptions_module = ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        pass

    class ConfigEntryNotReady(HomeAssistantError):
        pass

    class ServiceValidationError(HomeAssistantError):
        """Stubbed ServiceValidationError carrying translation context."""

        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args)
            self.translation_domain = kwargs.get("translation_domain")
            self.translation_key = kwargs.get("translation_key")
            self.translation_placeholders = kwargs.get("translation_placeholders")

    exceptions_module.HomeAssistantError = HomeAssistantError
    exceptions_module.ConfigEntryNotReady = ConfigEntryNotReady
    exceptions_module.ServiceValidationError = ServiceValidationError
    exceptions_module.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    sys.modules["homeassistant.exceptions"] = exceptions_module

    helpers_pkg = sys.modules.setdefault(
        "homeassistant.helpers", ModuleType("homeassistant.helpers")
    )
    helpers_pkg.__path__ = getattr(helpers_pkg, "__path__", [])

    for sub in (
        "device_registry",
        "entity_registry",
        "issue_registry",
        "update_coordinator",
    ):
        module_name = f"homeassistant.helpers.{sub}"
        module = ModuleType(module_name)
        sys.modules[module_name] = module
        setattr(helpers_pkg, sub, module)

    issue_registry_module = sys.modules["homeassistant.helpers.issue_registry"]
    issue_registry_module.IssueSeverity = SimpleNamespace(ERROR="error")

    def _noop(*args, **kwargs) -> None:  # pragma: no cover - stubbed helper
        return None

    issue_registry_module.async_delete_issue = _noop
    issue_registry_module.async_create_issue = _noop

    device_registry_module = sys.modules["homeassistant.helpers.device_registry"]
    device_registry_module.EVENT_DEVICE_REGISTRY_UPDATED = "device_registry_updated"
    device_registry_module.async_get = lambda _hass=None: SimpleNamespace(
        async_get=lambda _id: None
    )

    cv_module = ModuleType("homeassistant.helpers.config_validation")

    def _multi_select(choices):  # pragma: no cover - defensive
        return lambda value: value

    cv_module.multi_select = _multi_select
    sys.modules["homeassistant.helpers.config_validation"] = cv_module
    setattr(helpers_pkg, "config_validation", cv_module)

    aiohttp_client_module = ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client_module.async_get_clientsession = lambda hass: None
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client_module
    setattr(helpers_pkg, "aiohttp_client", aiohttp_client_module)

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

    update_coordinator_module = sys.modules["homeassistant.helpers.update_coordinator"]
    update_coordinator_module.UpdateFailed = UpdateFailed

    from typing import Generic, TypeVar

    _T = TypeVar("_T")

    class DataUpdateCoordinator(Generic[_T]):
        """Minimal stub for DataUpdateCoordinator supporting subclassing."""

        def __init__(
            self, hass=None, logger=None, name: str | None = None, update_interval=None
        ):
            self.hass = hass
            self.logger = logger
            self.name = name or "coordinator"
            self.update_interval = update_interval

        async def async_request_refresh(
            self,
        ) -> None:  # pragma: no cover - stubbed behaviour
            return None

        async def async_config_entry_first_refresh(
            self,
        ) -> None:  # pragma: no cover - stubbed behaviour
            return None

    update_coordinator_module.DataUpdateCoordinator = DataUpdateCoordinator

    event_module = ModuleType("homeassistant.helpers.event")

    async def _async_call_later(
        *_args, **_kwargs
    ):  # pragma: no cover - stubbed behaviour
        return None

    event_module.async_call_later = _async_call_later
    sys.modules["homeassistant.helpers.event"] = event_module
    setattr(helpers_pkg, "event", event_module)

    network_module = ModuleType("homeassistant.helpers.network")
    network_module.get_url = lambda *args, **kwargs: "https://example.local"
    sys.modules["homeassistant.helpers.network"] = network_module
    setattr(helpers_pkg, "network", network_module)

    entity_registry_module = sys.modules["homeassistant.helpers.entity_registry"]

    def _async_get_entity_registry(_hass=None):  # pragma: no cover - stub behaviour
        return SimpleNamespace(async_get=lambda _entity_id: None)

    entity_registry_module.async_get = _async_get_entity_registry

    util_pkg = sys.modules.setdefault(
        "homeassistant.util", ModuleType("homeassistant.util")
    )
    dt_module = ModuleType("homeassistant.util.dt")
    dt_module.UTC = timezone.utc
    dt_module.utcnow = lambda: datetime.now(timezone.utc)
    dt_module.now = dt_module.utcnow
    dt_module.as_local = lambda dt: dt
    sys.modules["homeassistant.util.dt"] = dt_module
    setattr(util_pkg, "dt", dt_module)

    components_pkg = sys.modules.setdefault(
        "homeassistant.components", ModuleType("homeassistant.components")
    )
    components_pkg.__path__ = getattr(components_pkg, "__path__", [])

    http_module = ModuleType("homeassistant.components.http")

    class HomeAssistantView:  # pragma: no cover - stub for imports
        requires_auth = False

        async def get(self, *_args, **_kwargs):
            return None

    http_module.HomeAssistantView = HomeAssistantView
    sys.modules["homeassistant.components.http"] = http_module
    setattr(components_pkg, "http", http_module)

    diagnostics_module = ModuleType("homeassistant.components.diagnostics")

    def _async_redact_data(data, _keys):  # pragma: no cover - stub behaviour
        return data

    diagnostics_module.async_redact_data = _async_redact_data
    sys.modules["homeassistant.components.diagnostics"] = diagnostics_module
    setattr(components_pkg, "diagnostics", diagnostics_module)

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
