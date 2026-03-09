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

        # Keys that are hidden in memory but preserved in the backing file.
        # This mirrors HA's two-layer persistence (volatile TokenCache +
        # persistent entry.data) for standalone mode where secrets.json is
        # the only store.  The AAS master token must survive runtime
        # invalidation so it can be recovered on the next process start.
        _SOFT_INVALIDATE_KEYS: frozenset[str] = frozenset({"aas_token"})

        def __init__(self, path: Path) -> None:
            self._path = path
            self._data: dict[str, object] = {}
            self._soft_invalidated: set[str] = set()
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
            if name in self._soft_invalidated:
                return None
            return self._data.get(name)

        async def set(self, name: str, value: object) -> None:
            if value is None:
                if name in self._SOFT_INVALIDATE_KEYS and name in self._data:
                    # Soft-invalidate: hide from in-memory reads but keep in
                    # the backing file so the value survives process restarts.
                    self._soft_invalidated.add(name)
                    return  # intentionally skip _save()
                self._data.pop(name, None)
            else:
                self._data[name] = value
                self._soft_invalidated.discard(name)
            self._save()

        async def async_get_cached_value(self, name: str) -> object:
            if name in self._soft_invalidated:
                return None
            return self._data.get(name)

        async def async_set_cached_value(self, name: str, value: object) -> None:
            await self.set(name, value)

        async def async_get_cached_value_or_set(self, name: str, generator):  # type: ignore[no-untyped-def]
            existing = await self.get(name)
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

    # Clear stale derived tokens — they were generated from the old OAuth
    # token and will be regenerated from the new one. The _FileCache
    # auto-saves on every set() call, so fresh AAS/ADM tokens produced
    # during the first successful Nova API request are persisted back
    # to secrets.json automatically.
    for key in list(data.keys()):
        if key.startswith(
            (
                "adm_token_",
                "adm_token_issued_at_",
                "aas_token_issued_at_",
                "adm_probe_",
            )
        ):
            del data[key]
    data.pop("aas_token", None)

    # 3) Persist to secrets.json
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    with open(secrets_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

    print("\nCredentials saved. Continuing...\n")


async def _ensure_aas_token(cache: object) -> None:
    """Eagerly exchange the OAuth cookie for an AAS master token.

    The OAuth cookie obtained by ``_ensure_authenticated()`` is **single-use**
    and has a very short TTL.  If we open Chrome again (e.g. for the shared
    key vault flow) before exchanging it, the cookie is invalidated and the
    AAS exchange fails with ``BadAuthentication``.

    This function must be called **immediately** after ``_ensure_authenticated()``
    and **before** any subsequent Chrome sessions.

    If no AAS token is available and the exchange fails (e.g. because the
    OAuth cookie is stale from a previous session), the process exits with
    a clear re-authentication prompt instead of continuing only to fail
    later during the first API call.
    """
    # If an AAS token is already cached, nothing to do.
    existing = await cache.get("aas_token")  # type: ignore[attr-defined]
    if isinstance(existing, str) and existing.strip():
        return
    # If there's no OAuth token either, skip (auth wasn't needed).
    oauth = await cache.get("oauth_token")  # type: ignore[attr-defined]
    if not isinstance(oauth, str) or not oauth.strip():
        return
    try:
        from custom_components.googlefindmy.Auth.aas_token_retrieval import (  # noqa: PLC0415
            async_get_aas_token,
        )

        await async_get_aas_token(cache=cache)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        # The OAuth cookie is single-use and already consumed/expired.
        # Without an AAS token the CLI cannot make any API calls, so
        # continuing would just produce the same error later.  Exit
        # immediately with a clear message.
        print(
            "\nError: Could not obtain an AAS authentication token.\n"
            "The OAuth cookie in secrets.json is stale (single-use, already consumed).\n"
            "\n"
            "Please re-authenticate:\n"
            "  python main.py --reauth\n",
            file=sys.stderr,
        )
        sys.exit(1)


async def _ensure_shared_key(cache: object) -> None:
    """Obtain the shared key from Google's vault if not already cached.

    Called eagerly during CLI startup (before any location requests) to avoid
    triggering the browser flow lazily during decryption, which can conflict
    with ChromeDriver file locks on Windows.
    """
    existing = await cache.get("shared_key")  # type: ignore[attr-defined]
    if existing:
        return
    # Check user-scoped legacy key too
    username = await cache.get("username")  # type: ignore[attr-defined]
    if isinstance(username, str) and username:
        legacy = await cache.get(f"shared_key_{username}")  # type: ignore[attr-defined]
        if legacy:
            return
    print("Retrieving encryption key from Google Key Backup vault...\n")
    from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import (  # noqa: PLC0415
        async_get_shared_key,
    )

    await async_get_shared_key(
        cache=cache,  # type: ignore[arg-type]
        username=username if isinstance(username, str) else None,
    )
    print("Encryption key saved.\n")


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
    # Must provide attributes that fcm_receiver_ha._write_coordinator_payload expects.
    _standalone_loc_data: dict[str, dict[str, object]] = {}
    coordinator_stub = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id="standalone"),
        cache=cache,
        is_device_present=lambda _cid: True,
        get_device_display_name=lambda _cid: None,
        _device_location_data=_standalone_loc_data,
        update_device_cache=lambda device_id, payload, **_kw: _standalone_loc_data.__setitem__(
            device_id, payload
        ),
        increment_stat=lambda _name: None,
        push_updated=lambda _ids: None,
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


def _clear_stale_tokens_for_reauth() -> None:
    """Clear cached tokens from secrets.json to force re-authentication.

    Called when ``--reauth`` is passed on the CLI. Removes ``oauth_token``,
    ``aas_token``, and all derived ``adm_token_*`` / timestamp keys so that
    ``_ensure_authenticated()`` triggers the Chrome login flow.
    """
    import json  # noqa: PLC0415

    secrets_path = _this_dir / "Auth" / "secrets.json"
    if not secrets_path.is_file():
        return

    try:
        with open(secrets_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return
    except Exception:  # noqa: BLE001
        return

    keys_to_remove = [
        k
        for k in data
        if k.startswith(
            (
                "oauth_token",
                "aas_token",
                "adm_token_",
                "adm_token_issued_at_",
                "aas_token_issued_at_",
                "adm_probe_",
                "owner_key",
                "shared_key",
            )
        )
    ]
    if not keys_to_remove:
        return

    for k in keys_to_remove:
        del data[k]

    with open(secrets_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)

    print(f"Cleared {len(keys_to_remove)} cached token(s). Re-authentication required.\n")


if __name__ == "__main__":
    import argparse  # noqa: PLC0415
    import asyncio  # noqa: PLC0415

    from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import (  # noqa: E402, PLC0415
        _async_cli_main,
    )

    _cli_parser = argparse.ArgumentParser(description="Google Find My Device CLI")
    _cli_parser.add_argument(
        "--reauth",
        action="store_true",
        help="Force re-authentication by clearing cached tokens and running Chrome login",
    )
    _cli_args = _cli_parser.parse_args()

    if _standalone:
        if _cli_args.reauth:
            _clear_stale_tokens_for_reauth()
        _ensure_authenticated()
        _file_cache = _register_file_cache()

        async def _cli_main() -> None:
            import aiohttp  # noqa: PLC0415

            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=16, enable_cleanup_closed=True)
            )
            # Exchange the single-use OAuth cookie for an AAS master token
            # BEFORE opening Chrome again for the shared key flow.  The OAuth
            # cookie has a very short TTL and is invalidated once a new Chrome
            # session is opened, so the exchange must happen immediately.
            await _ensure_aas_token(_file_cache)
            await _ensure_shared_key(_file_cache)
            fcm = await _setup_fcm_receiver(_file_cache)
            try:
                await _async_cli_main(session=session)
            finally:
                with __import__("contextlib").suppress(Exception):
                    await fcm.async_stop()
                await session.close()

        try:
            asyncio.run(_cli_main())
        except KeyboardInterrupt:
            print("\nExiting.")
    else:
        list_devices()
