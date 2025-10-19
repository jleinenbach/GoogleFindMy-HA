# tests/conftest.py
"""Test configuration and environment stubs for integration tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType
from typing import Any

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

    class OptionsFlow:  # pragma: no cover - minimal stub for config flow imports
        pass

    class OptionsFlowWithReload(OptionsFlow):
        pass

    class ConfigFlow:  # minimal base class used by the integration
        def __init__(self) -> None:
            self.unique_id = None

        def __init_subclass__(cls, **kwargs: Any) -> None:
            return None

        async def async_set_unique_id(self, *_args, **_kwargs) -> None:
            return None

        def _abort_if_unique_id_configured(self) -> None:
            return None

        def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs: Any) -> dict[str, Any]:
            return {"type": "create_entry", **kwargs}

        def add_suggested_values_to_schema(self, schema: Any, _defaults: Any) -> Any:
            return schema

    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigEntryState = ConfigEntryState
    config_entries.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    config_entries.ConfigFlow = ConfigFlow
    config_entries.OptionsFlow = OptionsFlow
    config_entries.OptionsFlowWithReload = OptionsFlowWithReload
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

    config_validation = ModuleType("homeassistant.helpers.config_validation")

    def multi_select(_choices: Any) -> Any:
        def _validator(value: Any) -> Any:
            return value

        return _validator

    config_validation.multi_select = multi_select
    sys.modules["homeassistant.helpers.config_validation"] = config_validation
    setattr(helpers_pkg, "config_validation", config_validation)

    aiohttp_client = ModuleType("homeassistant.helpers.aiohttp_client")

    def async_get_clientsession(_hass: Any) -> Any:
        return None

    aiohttp_client.async_get_clientsession = async_get_clientsession
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
    setattr(helpers_pkg, "aiohttp_client", aiohttp_client)

    if "voluptuous" not in sys.modules:
        vol_module = ModuleType("voluptuous")

        class _Marker:
            def __init__(self, key, **_: Any) -> None:
                self.key = key
                self.schema = {key}

            def __hash__(self) -> int:  # pragma: no cover - minimal behaviour
                return hash(self.key)

            def __eq__(self, other: object) -> bool:  # pragma: no cover
                if isinstance(other, _Marker):
                    return self.key == other.key
                return self.key == other

            def __call__(self, value: Any) -> Any:
                return value

        class _Schema(dict):
            def __init__(self, data: Any) -> None:
                super().__init__(data if isinstance(data, dict) else {})
                self.schema = data if isinstance(data, dict) else {}

        def _ensure_callable(obj: Any) -> Any:
            return obj if callable(obj) else (lambda value: value)

        def Schema(data: Any) -> _Schema:
            return _Schema(data)

        def Required(key: Any, **kwargs: Any) -> _Marker:
            return _Marker(key, **kwargs)

        def Optional(key: Any, **kwargs: Any) -> _Marker:
            return _Marker(key, **kwargs)

        def All(*validators: Any) -> Any:
            funcs = [_ensure_callable(v) for v in validators]

            def _validator(value: Any) -> Any:
                for func in funcs:
                    value = func(value)
                return value

            return _validator

        def Coerce(target_type: Any) -> Any:
            def _coerce(value: Any) -> Any:
                return target_type(value)

            return _coerce

        def Range(min: Any | None = None, max: Any | None = None) -> Any:
            def _validator(value: Any) -> Any:
                if min is not None and value < min:
                    raise ValueError("value below minimum")
                if max is not None and value > max:
                    raise ValueError("value above maximum")
                return value

            return _validator

        def In(_options: Any) -> Any:
            def _validator(value: Any) -> Any:
                return value

            return _validator

        vol_module.Schema = Schema
        vol_module.Required = Required
        vol_module.Optional = Optional
        vol_module.All = All
        vol_module.Coerce = Coerce
        vol_module.Range = Range
        vol_module.In = In
        sys.modules["voluptuous"] = vol_module

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

    data_entry_flow_module = ModuleType("homeassistant.data_entry_flow")

    class FlowResultType:  # minimal enum-like stub
        FORM = "form"
        CREATE_ENTRY = "create_entry"

    class FlowResult(dict):
        pass

    data_entry_flow_module.FlowResultType = FlowResultType
    data_entry_flow_module.FlowResult = FlowResult
    sys.modules["homeassistant.data_entry_flow"] = data_entry_flow_module

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
