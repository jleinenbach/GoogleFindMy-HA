#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import sys
import types
from pathlib import Path
from typing import Any

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

            def __call__(cls, *args: object, **kwargs: object) -> object:
                # @some_ha_decorator usage: return the function unchanged
                if len(args) == 1 and callable(args[0]) and not kwargs:
                    return args[0]
                return super().__call__(*args, **kwargs)

        class _Dummy(metaclass=_DummyMeta):
            """Versatile stand-in for any HA symbol (class, decorator, constant)."""

            def __init__(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
                pass

            def __init_subclass__(cls, **kwargs: object) -> None:
                super().__init_subclass__(**kwargs)

        class _StubHAModule(types.ModuleType):
            """Stub module – missing attributes become ``_Dummy`` subclasses.

            Attribute lookup first checks ``sys.modules`` for a registered
            submodule so that ``from homeassistant import core`` (attribute-
            style access after the finder has already loaded the submodule)
            returns the real stub module rather than fabricating a dummy.
            """

            def __getattr__(self, name: str) -> object:
                if name.startswith("_"):
                    raise AttributeError(name)
                # If a submodule with this name exists, return it.
                fullname = f"{self.__name__}.{name}"
                if fullname in sys.modules:
                    return sys.modules[fullname]
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

            def find_spec(
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
                        fullname,
                        self,  # type: ignore[arg-type]
                        is_package=True,
                    )
                return None

            # Loader protocol -------------------------------------------------
            def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
                mod = _StubHAModule(spec.name)
                mod.__path__ = []
                return mod

            def exec_module(self, module: types.ModuleType) -> None:  # noqa: ARG002
                pass

        sys.meta_path.insert(0, _HAStubFinder())

        # Pre-populate specific submodules with concrete stubs so the
        # critical import chain (token_cache, exceptions, get_owner_key)
        # gets the exact types it needs.
        _ha_core = _StubHAModule("homeassistant.core")
        _ha_core.__path__ = []
        setattr(_ha_core, "HomeAssistant", type("HomeAssistant", (), {}))
        sys.modules["homeassistant.core"] = _ha_core

        _ha_storage = _StubHAModule("homeassistant.helpers.storage")
        _ha_storage.__path__ = []
        setattr(_ha_storage, "Store", type("Store", (), {}))
        sys.modules["homeassistant.helpers.storage"] = _ha_storage

        _ha_exceptions = _StubHAExceptions("homeassistant.exceptions")
        setattr(_ha_exceptions, "HomeAssistantError", _ha_exceptions._HomeAssistantError)
        sys.modules["homeassistant.exceptions"] = _ha_exceptions

from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import list_devices  # noqa: E402


def _register_file_cache() -> object:
    """Create a file-backed TokenCache and register it for standalone CLI use.

    Reads/writes ``Auth/secrets.json`` (the upstream GoogleFindMyTools layout)
    so that ``_resolve_cli_cache`` in ``nbe_list_devices`` finds a registered
    cache and the CLI flow works without Home Assistant.

    Returns the cache instance so callers can pass it to the FCM setup.
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
    return file_cache


def _ensure_authenticated() -> None:
    """Run the Chrome-based OAuth login when no credentials exist yet.

    This replicates the original GoogleFindMyTools first-run experience:
    Chrome opens for Google login, the user enters their e-mail, and both
    the OAuth token and username are persisted to ``Auth/secrets.json``.
    """
    import json  # noqa: PLC0415

    secrets_path = _this_dir / "Auth" / "secrets.json"
    data: dict[str, object] = {}
    if secrets_path.is_file():
        try:
            with open(secrets_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                data = raw
        except Exception:  # noqa: BLE001
            pass

    has_user = isinstance(data.get("username"), str) and data["username"]
    has_token = (
        (isinstance(data.get("oauth_token"), str) and data["oauth_token"])
        or (isinstance(data.get("aas_token"), str) and data["aas_token"])
    )
    if has_user and has_token:
        return  # Already authenticated

    print("No credentials found. Starting authentication flow...\n")

    # 1) Get the OAuth token via Chrome login
    from custom_components.googlefindmy.Auth.auth_flow import (  # noqa: PLC0415
        request_oauth_account_token_flow,
    )

    oauth_token, detected_email = request_oauth_account_token_flow()

    # 2) Set the Google account e-mail (needed for gpsoauth exchange).
    #    Prefer the email extracted from the Chrome session; fall back to
    #    a CLI prompt only when extraction failed.
    if not has_user:
        if detected_email:
            data["username"] = detected_email
        else:
            email = input("\nEnter your Google account email: ").strip()
            if not email:
                print("Error: email is required.", file=sys.stderr)
                sys.exit(1)
            data["username"] = email

    data["oauth_token"] = oauth_token

    # 3) Persist to secrets.json
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    with open(secrets_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

    print("\nCredentials saved. Continuing...\n")


async def _setup_fcm_receiver(cache: object) -> Any:
    """Initialize the FCM push-notification receiver for standalone CLI use.

    This is required because Google delivers location data exclusively via
    FCM push notifications, not HTTP responses.  Without a registered FCM
    receiver, location queries would fail with
    ``RuntimeError: FCM receiver provider has not been registered.``
    """
    from types import SimpleNamespace  # noqa: PLC0415

    from custom_components.googlefindmy.Auth.fcm_receiver_ha import (  # noqa: PLC0415
        FcmReceiverHA,
    )
    from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request import (  # noqa: PLC0415
        register_fcm_receiver_provider as loc_register,
    )

    fcm = FcmReceiverHA()
    await fcm.async_initialize(entry_id="standalone", cache=cache)  # type: ignore[arg-type]

    # Minimal coordinator stub so _select_manual_locate_entry finds an entry.
    coordinator_stub = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="standalone"),
        cache=cache,
        is_device_present=lambda _cid: True,
        get_device_display_name=lambda _cid: None,
    )
    fcm.register_coordinator(coordinator_stub)

    def _get_fcm(entry_id: str | None = None) -> FcmReceiverHA:  # noqa: ARG001
        return fcm

    loc_register(_get_fcm)

    # Also register in api.py if available (used by some code paths).
    try:
        from custom_components.googlefindmy.api import (  # noqa: PLC0415
            register_fcm_receiver_provider as api_register,
        )

        api_register(_get_fcm)
    except Exception:  # noqa: BLE001
        pass

    return fcm


def _try_seed_fcm_credentials(cache: object) -> None:
    """Import FCM credentials from a user-provided file if the cache is empty.

    If ``Auth/fcm_credentials.json`` exists and the cache does not yet contain
    ``fcm_credentials``, the file is read and injected into the cache.  This
    provides a workaround for environments where fresh GCM registration is
    unreliable: the user can export credentials from a working HA installation
    and place them in the ``Auth/`` directory.
    """
    import json  # noqa: PLC0415

    if hasattr(cache, "sync_get") and cache.sync_get("fcm_credentials"):  # type: ignore[union-attr]
        return  # already seeded
    creds_file = _this_dir / "Auth" / "fcm_credentials.json"
    if not creds_file.is_file():
        return
    try:
        with open(creds_file, encoding="utf-8") as fh:
            fcm_creds = json.load(fh)
        if isinstance(fcm_creds, dict) and hasattr(cache, "sync_set"):
            cache.sync_set("fcm_credentials", fcm_creds)  # type: ignore[union-attr]
            print("FCM credentials imported from fcm_credentials.json")
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    import asyncio  # noqa: PLC0415

    from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import (  # noqa: E402, PLC0415
        _async_cli_main,
    )

    if _standalone:
        _ensure_authenticated()
        _file_cache = _register_file_cache()
        _try_seed_fcm_credentials(_file_cache)

        async def _cli_main() -> None:
            fcm = await _setup_fcm_receiver(_file_cache)
            try:
                await _async_cli_main()
            finally:
                with __import__("contextlib").suppress(Exception):
                    await fcm.async_stop()

        try:
            asyncio.run(_cli_main())
        except KeyboardInterrupt:
            print("\nExiting.")
    else:
        list_devices()
