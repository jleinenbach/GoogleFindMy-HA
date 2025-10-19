# tests/conftest.py
"""Test configuration and environment stubs for integration tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

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

        async def async_show_form(self, *args, **kwargs):  # pragma: no cover - defensive
            return {"type": "form"}

        async def async_show_menu(self, *args, **kwargs):  # pragma: no cover - defensive
            return {"type": "menu"}

        async def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def add_suggested_values_to_schema(self, schema, suggested):  # noqa: D401 - stub
            return schema

    class OptionsFlow:
        """Minimal OptionsFlow stub for imports."""

        async def async_show_form(self, *args, **kwargs):  # pragma: no cover - defensive
            return {"type": "form"}

        async def async_create_entry(self, *, title: str, data):  # pragma: no cover - defensive
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
