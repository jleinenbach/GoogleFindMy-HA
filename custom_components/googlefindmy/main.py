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

        class _LazyGoogleFindMyModule(types.ModuleType):
            """Virtual package that lazily resolves proto-decoder helpers."""

            _PROTO_PATHS: dict[str, str] = {
                "Common_pb2": "custom_components.googlefindmy.ProtoDecoders.Common_pb2",
                "DeviceUpdate_pb2": "custom_components.googlefindmy.ProtoDecoders.DeviceUpdate_pb2",
                "LocationReportsUpload_pb2": (
                    "custom_components.googlefindmy.ProtoDecoders.LocationReportsUpload_pb2"
                ),
            }
            _proto_cache: dict[str, object] = {}

            @classmethod
            def _import_proto(cls, pname: str) -> object:
                mod = cls._proto_cache.get(pname)
                if mod is not None:
                    return mod
                from importlib import import_module as _imp  # noqa: PLC0415

                mod = _imp(cls._PROTO_PATHS[pname])
                cls._proto_cache[pname] = mod
                return mod

            def __getattr__(self, name: str) -> object:
                from types import MappingProxyType  # noqa: PLC0415

                if name == "get_proto_decoder":
                    func = self._import_proto
                    setattr(self, name, func)
                    return func
                if name == "get_proto_decoders":

                    def _get_all() -> object:
                        for pn in self._PROTO_PATHS:
                            self._import_proto(pn)
                        return MappingProxyType(self._proto_cache)

                    setattr(self, name, _get_all)
                    return _get_all
                if name in self._PROTO_PATHS:
                    return self._import_proto(name)
                raise AttributeError(
                    f"module 'custom_components.googlefindmy' has no attribute {name!r}"
                )

        _ccg = _LazyGoogleFindMyModule("custom_components.googlefindmy")
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
        import importlib.abc  # noqa: PLC0415
        import importlib.machinery  # noqa: PLC0415

        class _DummyMeta(type):
            """Metaclass so dummy classes double as pass-through decorators."""

            def __call__(cls, *args, **kwargs):  # type: ignore[override]
                # @some_ha_decorator usage: return the function unchanged
                if len(args) == 1 and callable(args[0]) and not kwargs:
                    return args[0]
                obj = cls.__new__(cls)
                obj.__init__(*args, **kwargs)
                return obj

        class _Dummy(metaclass=_DummyMeta):
            """Versatile stand-in for any HA symbol (class, decorator, constant)."""

            def __init__(self, *args, **kwargs):  # noqa: ARG002
                pass

            def __init_subclass__(cls, **kwargs):
                super().__init_subclass__(**kwargs)

        class _StubHAModule(types.ModuleType):
            """Stub module – missing attributes become ``_Dummy`` subclasses."""

            def __getattr__(self, name: str) -> object:
                if name.startswith("_"):
                    raise AttributeError(name)
                cls = type(name, (_Dummy,), {})
                setattr(self, name, cls)
                return cls

        class _StubHAExceptions(types.ModuleType):
            """Stub for ``homeassistant.exceptions`` – generates Exception subclasses."""

            _HomeAssistantError: type = type("HomeAssistantError", (Exception,), {})

            def __getattr__(self, name: str) -> object:
                if name.startswith("_"):
                    raise AttributeError(name)
                cls = type(name, (self._HomeAssistantError,), {})
                setattr(self, name, cls)
                return cls

        class _HAStubFinder(importlib.abc.MetaPathFinder):
            """Import hook: auto-create stub modules for any ``homeassistant.*``."""

            def find_spec(  # type: ignore[override]
                self,
                fullname: str,
                path: object,
                target: object = None,
            ) -> "importlib.machinery.ModuleSpec | None":
                if (
                    fullname == "homeassistant"
                    or fullname.startswith("homeassistant.")
                ) and fullname not in sys.modules:
                    return importlib.machinery.ModuleSpec(
                        fullname, self, is_package=True,  # type: ignore[arg-type]
                    )
                return None

            # Loader protocol -------------------------------------------------
            def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
                mod = _StubHAModule(spec.name)
                mod.__path__ = []  # type: ignore[attr-defined]
                return mod

            def exec_module(self, module: types.ModuleType) -> None:  # noqa: ARG002
                pass

        sys.meta_path.insert(0, _HAStubFinder())

        # Pre-populate specific submodules with concrete stubs so the
        # critical import chain (token_cache, exceptions, get_owner_key)
        # gets the exact types it needs.
        _ha_core = _StubHAModule("homeassistant.core")
        _ha_core.__path__ = []  # type: ignore[attr-defined]
        _ha_core.HomeAssistant = type("HomeAssistant", (), {})  # type: ignore[attr-defined]
        sys.modules["homeassistant.core"] = _ha_core

        _ha_storage = _StubHAModule("homeassistant.helpers.storage")
        _ha_storage.__path__ = []  # type: ignore[attr-defined]
        _ha_storage.Store = type("Store", (), {})  # type: ignore[attr-defined]
        sys.modules["homeassistant.helpers.storage"] = _ha_storage

        _ha_exceptions = _StubHAExceptions("homeassistant.exceptions")
        _ha_exceptions.HomeAssistantError = _ha_exceptions._HomeAssistantError  # type: ignore[attr-defined]
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

        def sync_pop(self, name: str, default: object = None) -> object:
            val = self._data.pop(name, default)
            if val is not default:
                self._save()
            return val

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
