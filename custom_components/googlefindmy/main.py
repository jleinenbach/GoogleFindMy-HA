#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import sys
import types
from pathlib import Path

_this_dir = Path(__file__).resolve().parent
_standalone = not (
    _this_dir.name == "googlefindmy" and _this_dir.parent.name == "custom_components"
)

if not _standalone:
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

        # Provide a __getattr__ so that `from custom_components.googlefindmy import X`
        # can lazily resolve names defined in __init__.py (e.g. get_proto_decoder)
        # without executing the full __init__.py (which has heavy HA imports).
        def _ccg_getattr(name: str) -> object:
            from importlib import import_module as _imp  # noqa: PLC0415
            from types import MappingProxyType  # noqa: PLC0415

            _PROTO_PATHS: dict[str, str] = {
                "Common_pb2": "custom_components.googlefindmy.ProtoDecoders.Common_pb2",
                "DeviceUpdate_pb2": "custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2",
                "LocationReportsUpload_pb2": (
                    "custom_components.googlefindmy.ProtoDecoders.LocationReportsUpload_pb2"
                ),
            }
            _proto_cache: dict[str, object] = {}

            def _import_proto(pname: str) -> object:
                mod = _proto_cache.get(pname)
                if mod is not None:
                    return mod
                mod = _imp(_PROTO_PATHS[pname])
                _proto_cache[pname] = mod
                return mod

            def get_proto_decoder(pname: str) -> object:
                return _import_proto(pname)

            def get_proto_decoders() -> object:
                for pn in _PROTO_PATHS:
                    _import_proto(pn)
                return MappingProxyType(_proto_cache)

            if name == "get_proto_decoder":
                _ccg.get_proto_decoder = get_proto_decoder  # type: ignore[attr-defined]
                return get_proto_decoder
            if name == "get_proto_decoders":
                _ccg.get_proto_decoders = get_proto_decoders  # type: ignore[attr-defined]
                return get_proto_decoders
            if name in _PROTO_PATHS:
                return _import_proto(name)
            raise AttributeError(f"module 'custom_components.googlefindmy' has no attribute {name!r}")

        _ccg.__getattr__ = _ccg_getattr  # type: ignore[attr-defined]
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


def _register_file_cache() -> None:
    """Create a file-backed TokenCache and register it for standalone CLI use.

    Reads/writes ``Auth/secrets.json`` (the upstream GoogleFindMyTools layout)
    so that ``_resolve_cli_cache`` in ``nbe_list_devices`` finds a registered
    cache and the CLI flow works without Home Assistant.
    """
    import json  # noqa: PLC0415

    from custom_components.googlefindmy.Auth.token_cache import (  # noqa: PLC0415
        _register_instance,
        _set_default_entry_id,
    )

    secrets_path = _this_dir / "Auth" / "secrets.json"

    # Build a minimal TokenCache-compatible object backed by a JSON file.
    # We cannot call TokenCache() because it requires a real HomeAssistant
    # instance, so we create a duck-typed replacement instead.

    class _FileCache:
        """Minimal file-backed cache compatible with the TokenCache interface."""

        entry_id: str = "standalone"

        def __init__(self, path: Path) -> None:
            self._path = path
            self._data: dict[str, object] = {}
            self._load_failed = False
            if path.is_file():
                try:
                    with open(path, encoding="utf-8") as fh:
                        raw = json.load(fh)
                    if isinstance(raw, dict):
                        self._data = raw
                except Exception:  # noqa: BLE001
                    import logging  # noqa: PLC0415

                    logging.getLogger(__name__).warning(
                        "Failed to load %s; credentials will not be persisted "
                        "until a successful write occurs.",
                        path,
                    )
                    self._load_failed = True

        def _save(self) -> None:
            if self._load_failed and not self._data:
                return  # Don't overwrite valid file with empty data
            self._load_failed = False
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2)

        # --- async interface expected by nbe_list_devices / nova_request ---

        async def get(self, name: str) -> object:
            return self._data.get(name)

        async def set(self, name: str, value: object) -> None:
            if value is None:
                self._data.pop(name, None)
            else:
                self._data[name] = value
            self._save()

        async def async_get_cached_value(self, name: str) -> object:
            return self._data.get(name)

        async def async_set_cached_value(self, name: str, value: object) -> None:
            await self.set(name, value)

        async def async_get_cached_value_or_set(self, name: str, generator):  # type: ignore[no-untyped-def]
            existing = self._data.get(name)
            if existing is not None:
                return existing
            import asyncio  # noqa: PLC0415

            candidate = generator()
            if asyncio.iscoroutine(candidate):
                candidate = await candidate
            await self.set(name, candidate)
            return candidate

        async def get_or_set(self, name: str, generator):  # type: ignore[no-untyped-def]
            return await self.async_get_cached_value_or_set(name, generator)

        def sync_get(self, name: str) -> object:
            return self._data.get(name)

        async def all(self) -> dict[str, object]:
            return dict(self._data)

        def sync_set(self, name: str, value: object) -> None:
            if value is None:
                self._data.pop(name, None)
            else:
                self._data[name] = value
            self._save()

        async def flush(self) -> None:
            self._save()

        async def close(self) -> None:
            self._save()

    file_cache = _FileCache(secrets_path)

    # Register as a TokenCache instance so _resolve_cli_cache finds it.
    # mypy: _FileCache is duck-typed, not a real TokenCache subclass.
    _register_instance("standalone", file_cache)  # type: ignore[arg-type]
    _set_default_entry_id("standalone", force=True)


if __name__ == "__main__":
    if _standalone:
        _register_file_cache()
    list_devices()
