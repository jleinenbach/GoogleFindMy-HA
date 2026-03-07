#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import sys
import types
from pathlib import Path

_this_dir = Path(__file__).resolve().parent

if _this_dir.name == "googlefindmy" and _this_dir.parent.name == "custom_components":
    # Running inside the HA repo structure – add repo root to sys.path
    _repo_root = str(_this_dir.parents[2])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
else:
    # Running standalone (files copied to a flat directory like GoogleFindMyTools/)
    # Create virtual package so `custom_components.googlefindmy.*` imports resolve here
    if "custom_components" not in sys.modules:
        _cc = types.ModuleType("custom_components")
        _cc.__path__ = []
        sys.modules["custom_components"] = _cc
    if "custom_components.googlefindmy" not in sys.modules:
        _ccg = types.ModuleType("custom_components.googlefindmy")
        _ccg.__path__ = [str(_this_dir)]
        sys.modules["custom_components.googlefindmy"] = _ccg

    # Provide lightweight stubs for homeassistant so HA-specific imports
    # in token_cache.py and exceptions.py don't fail at import time.
    # Only inject stubs when HA is genuinely unavailable (not just not-yet-imported).
    _ha_available = "homeassistant" in sys.modules
    if not _ha_available:
        from importlib.util import find_spec

        _ha_available = find_spec("homeassistant") is not None
    if not _ha_available:
        _ha = types.ModuleType("homeassistant")
        _ha.__path__ = []
        sys.modules["homeassistant"] = _ha

        _ha_core = types.ModuleType("homeassistant.core")
        _ha_core.HomeAssistant = type("HomeAssistant", (), {})  # type: ignore[attr-defined]
        sys.modules["homeassistant.core"] = _ha_core

        _ha_helpers = types.ModuleType("homeassistant.helpers")
        _ha_helpers.__path__ = []
        sys.modules["homeassistant.helpers"] = _ha_helpers

        _ha_storage = types.ModuleType("homeassistant.helpers.storage")
        _ha_storage.Store = type("Store", (), {})  # type: ignore[attr-defined]
        sys.modules["homeassistant.helpers.storage"] = _ha_storage

        _ha_exceptions = types.ModuleType("homeassistant.exceptions")
        _ha_exceptions.HomeAssistantError = type("HomeAssistantError", (Exception,), {})  # type: ignore[attr-defined]
        sys.modules["homeassistant.exceptions"] = _ha_exceptions

from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import list_devices  # noqa: E402

if __name__ == "__main__":
    list_devices()
