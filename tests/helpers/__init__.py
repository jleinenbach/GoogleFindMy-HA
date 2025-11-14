# tests/helpers/__init__.py
"""Helper utilities for Google Find My integration tests."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from .ast_extract import compile_class_method_from_module
from .asyncio import drain_loop

__all__ = [
    "compile_class_method_from_module",
    "drain_loop",
    "ConfigEntriesFlowManagerStub",
    "attach_config_entries_flow_manager",
    "config_entries_flow_stub",
    "prepare_flow_hass_config_entries",
    "set_config_flow_unique_id",
    "FakeConfigEntriesManager",
    "FakeConfigEntry",
    "FakeDeviceEntry",
    "FakeDeviceRegistry",
    "device_registry_async_entries_for_config_entry",
    "FakeEntityRegistry",
    "FakeHass",
    "FakeServiceRegistry",
    "service_device_stub",
    "config_entry_with_subentries",
    "resolve_config_entry_lookup",
    "install_homeassistant_core_callback_stub",
]

_EXPORT_MAP = {
    "ConfigEntriesFlowManagerStub": ".config_flow",
    "attach_config_entries_flow_manager": ".config_flow",
    "config_entries_flow_stub": ".config_flow",
    "prepare_flow_hass_config_entries": ".config_flow",
    "set_config_flow_unique_id": ".config_flow",
    "FakeConfigEntriesManager": ".homeassistant",
    "FakeConfigEntry": ".homeassistant",
    "FakeDeviceEntry": ".homeassistant",
    "FakeDeviceRegistry": ".homeassistant",
    "device_registry_async_entries_for_config_entry": ".homeassistant",
    "FakeEntityRegistry": ".homeassistant",
    "FakeHass": ".homeassistant",
    "FakeServiceRegistry": ".homeassistant",
    "service_device_stub": ".homeassistant",
    "config_entry_with_subentries": ".homeassistant",
    "resolve_config_entry_lookup": ".homeassistant",
    "install_homeassistant_core_callback_stub": ".homeassistant_stub",
}


def __getattr__(name: str) -> Any:
    """Lazily import config flow helpers to honor Home Assistant stubs."""

    module_name = _EXPORT_MAP.get(name)
    if module_name is not None:
        module = import_module(module_name, __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    """Return attributes available on the helpers package."""

    return sorted(set(globals()) | set(_EXPORT_MAP))
