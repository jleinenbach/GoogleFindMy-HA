#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Standalone CLI entry point for GoogleFindMy functionality.

Usage:
    python -m custom_components.googlefindmy.main [--reauth] [--entry ENTRY_ID] [--debug]
    python custom_components/googlefindmy/main.py [--reauth] [--entry ENTRY_ID] [--debug]

Flags:
    --reauth   Force re-authentication by clearing cached tokens and running the
               Chrome login flow.  Honored in any directory layout; it always
               forces a fresh token bootstrap.
    --entry    Target config entry ID.  Required when secrets.json contains caches
               for multiple HA config entries.  May also be set via the
               ``GOOGLEFINDMY_ENTRY_ID`` environment variable.
    --debug    Enable verbose DEBUG logging for the bootstrap and FCM flow.  The
               package configures no logging otherwise, so bootstrap/FCM diagnostics
               are invisible without this flag.  May also be enabled via the
               ``GOOGLEFINDMY_DEBUG`` environment variable.

Run as a script, the module always runs the full token-bootstrap pipeline and
writes ``secrets.json``, regardless of flat vs. repo directory layout.  When a
usable cache already exists, ``_ensure_authenticated`` returns early so no Chrome
login is launched; ``--reauth`` forces a fresh login.  ``_standalone`` only
governs packaging (virtual package vs. real package), which is the one thing the
directory name legitimately determines.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-time typing block
    import argparse
    from collections.abc import Mapping, Sequence

_this_dir = Path(__file__).resolve().parent
_standalone = not (
    _this_dir.name == "googlefindmy" and _this_dir.parent.name == "custom_components"
)

if not _standalone:
    # Running inside the HA repo structure – add repo root to sys.path
    _repo_root = str(_this_dir.parents[1])
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
else:  # pragma: no cover - standalone CLI stub-injection shim, structurally
    # unreachable when imported as an installed HA integration (_standalone is
    # False under the test suite / HA runtime). Not fachlich testable; excluded
    # from the coverage denominator per PLAN_GFMY_COV_W0_MESSSCOPE AP-W0.3 (E5).
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
            ) -> importlib.machinery.ModuleSpec | None:
                if (
                    fullname == "homeassistant" or fullname.startswith("homeassistant.")
                ) and fullname not in sys.modules:
                    return importlib.machinery.ModuleSpec(
                        fullname,
                        self,  # type: ignore[arg-type]
                        is_package=True,
                    )
                return None

            # Loader protocol -------------------------------------------------
            def create_module(
                self, spec: importlib.machinery.ModuleSpec
            ) -> types.ModuleType:
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
        setattr(
            _ha_exceptions, "HomeAssistantError", _ha_exceptions._HomeAssistantError
        )
        sys.modules["homeassistant.exceptions"] = _ha_exceptions


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write ``data`` as JSON to ``path`` with 0600 permissions.

    The standalone ``secrets.json`` carries OAuth/AAS tokens, so an interrupted
    write (process abort, full disk) must never leave a truncated or empty file
    behind. The data is first written to a temporary file in the *same*
    directory (``os.replace`` is only atomic within a single filesystem), then
    flushed and ``fsync``-ed, then renamed onto the target. On any failure the
    temporary artifact is removed so no orphan ``*.tmp`` lingers next to the
    real file. ``tempfile.mkstemp`` creates the temp file with 0600, which the
    rename preserves; the explicit ``os.chmod`` keeps the mode correct even if
    the target already existed with looser permissions.
    """
    import json  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
        os.chmod(path, 0o600)
    except BaseException:
        # Clean up the temp artifact on any failure (errors *and* interrupts);
        # the rename either fully succeeded or never happened, so removing the
        # leftover temp keeps the directory free of orphans without touching
        # the existing target file.
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _register_file_cache(entry_id: str = "") -> object:
    """Create a file-backed TokenCache and register it for standalone CLI use.

    Reads/writes ``Auth/secrets.json`` (the upstream GoogleFindMyTools layout)
    so that ``_resolve_cli_cache`` in ``nbe_list_devices`` finds a registered
    cache and the CLI flow works without Home Assistant.

    Args:
        entry_id: Registry key under which the cache is registered.  An empty
            string (the default) preserves the legacy single-entry behaviour;
            a non-empty value must match exactly what is later passed to
            ``_async_cli_main`` / ``_resolve_cli_cache`` (either via the
            ``--entry`` CLI flag or the ``GOOGLEFINDMY_ENTRY_ID`` env var),
            otherwise the cache lookup raises "Unknown entry_id ...".

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

        entry_id: str = ""

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
            _atomic_write_json(self._path, self._data)

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
    _register_instance(entry_id, file_cache)  # type: ignore[arg-type]
    _set_default_entry_id(entry_id, force=True)
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
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).warning(
                "Failed to load %s; treating as missing credentials and "
                "starting authentication flow.",
                secrets_path,
                exc_info=True,
            )

    has_user = isinstance(data.get("username"), str) and data["username"]
    has_token = (isinstance(data.get("oauth_token"), str) and data["oauth_token"]) or (
        isinstance(data.get("aas_token"), str) and data["aas_token"]
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
    _atomic_write_json(secrets_path, data)

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


async def _ensure_owner_key(cache: object) -> None:
    """Eagerly fetch the owner key so the generated bundle is complete.

    Mirrors :func:`_ensure_shared_key`: called once during CLI startup after the
    shared key is available. The owner-key fetch is non-interactive (a SPOT API
    call plus a decrypt with the already-retrieved shared key, no browser), so
    it is cheap to perform eagerly. Functionally the shared key alone suffices
    for the HA integration (the owner key is derivable at runtime); fetching it
    here is a robustness/completeness step so a single CLI run yields a bundle
    that carries both keys.

    Beyond the runtime-scoped ``owner_key_{username}`` cache entry (which
    ``async_get_owner_key`` writes itself), this also stores the owner key as a
    top-level ``owner_key`` hex string. That top-level field is the only form
    the paste/seeding import reads (``__init__._async_save_secrets_data``), so
    writing it keeps the generated ``secrets.json`` paste-compatible.
    """
    existing = await cache.get("owner_key")  # type: ignore[attr-defined]
    if existing:
        return
    username = await cache.get("username")  # type: ignore[attr-defined]
    if isinstance(username, str) and username:
        scoped = await cache.get(f"owner_key_{username}")  # type: ignore[attr-defined]
        if scoped:
            return
    print("Retrieving owner key...\n")
    from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_owner_key import (  # noqa: PLC0415
        async_get_owner_key,
    )

    info = await async_get_owner_key(cache=cache)  # type: ignore[arg-type]
    # Persist the top-level paste-compatible hex form (bundle-inherently
    # version-less, matching externally produced Google bundles).
    await cache.set("owner_key", info.key.hex())  # type: ignore[attr-defined]
    print("Owner key saved.\n")


async def _ensure_vault_keys(cache: object) -> None:
    """Eagerly obtain the vault keys with a per-key criticality boundary.

    The two eager retrievals have opposite criticality contracts, so each gets
    its own error boundary instead of a single shared ``try`` that flattens both
    to "all or nothing":

    * **Shared key** (``_ensure_shared_key``) is *essential* and browser/login
      bound. Failure here is fatal: there is no usable bundle without it, so we
      print an actionable sign-in hint and exit with status 1.
    * **Owner key** (``_ensure_owner_key``) is *optional* and non-interactive (a
      SPOT API call plus a decrypt, no browser). The runtime re-derives it
      lazily on first use (``decrypt_locations``/``eid_resolver`` call
      ``async_get_owner_key`` with a ``force_refresh`` fallback), so a failure
      here still leaves a usable ``secrets.json``. We warn and continue rather
      than aborting the whole run.

    Both blocks catch only ``Exception``; ``KeyboardInterrupt`` (a
    ``BaseException``) still propagates so a user-initiated abort is honoured.

    No tokens are deleted on failure: the AAS/oauth tokens are durable by design
    (``_FileCache._SOFT_INVALIDATE_KEYS``); removing them would force an
    expensive full OAuth login on the next run.
    """
    # Shared key: essential and browser-bound. Failure is fatal.
    try:
        await _ensure_shared_key(cache)
    except Exception:  # noqa: BLE001
        print(
            "\nError: vault key retrieval failed.\n"
            "Could not obtain the encryption key from Google's vault.\n"
            "\n"
            "Please re-run the tool and complete the Google sign-in when the "
            "browser opens.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    # Owner key: optional and non-interactive. The runtime re-derives it lazily,
    # so a failure here is non-fatal -- warn and keep the usable secrets.json.
    try:
        await _ensure_owner_key(cache)
    except Exception as exc:  # noqa: BLE001
        print(
            f"\nNote: the owner key could not be pre-fetched "
            f"({type(exc).__name__}).\n"
            "This is non-fatal: the shared key alone is sufficient for the Home "
            "Assistant integration, and the owner key is fetched automatically "
            "on first use. The generated secrets.json is ready to use.\n",
            file=sys.stderr,
        )


async def _clear_stale_adm_token(cache: object) -> None:
    """Clear cached ADM token so the first API request generates a fresh one.

    Unlike Home Assistant (which has a coordinator polling regularly to keep
    tokens fresh), the CLI process exits between uses.  The ADM token stored
    in secrets.json from the previous session has most likely expired (~1 h
    lifetime).  Clearing it here avoids a guaranteed 401 + lengthy retry
    cascade on the very first request.
    """
    username_raw = await cache.get("username")  # type: ignore[attr-defined]
    if not isinstance(username_raw, str) or not username_raw.strip():
        return
    user = username_raw.strip().lower()
    for key in (f"adm_token_{user}", f"adm_token_issued_at_{user}"):
        await cache.set(key, None)  # type: ignore[attr-defined]


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
    await fcm.async_initialize(entry_id="", cache=cache)  # type: ignore[arg-type]

    # Minimal coordinator stub so _select_manual_locate_entry finds an entry.
    # Must provide attributes that fcm_receiver_ha._write_coordinator_payload expects.
    _standalone_loc_data: dict[str, dict[str, object]] = {}
    coordinator_stub = SimpleNamespace(
        config_entry=SimpleNamespace(entry_id=""),
        cache=cache,
        is_device_present=lambda _cid: True,
        get_device_display_name=lambda _cid: None,
        _device_location_data=_standalone_loc_data,
        update_device_cache=lambda device_id, payload, **_kw: (
            _standalone_loc_data.__setitem__(device_id, payload)
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
    except ImportError:
        import logging  # noqa: PLC0415

        # api.py provider hook is optional; the location_request hook
        # registered above is sufficient for the standalone CLI path.
        logging.getLogger(__name__).debug(
            "custom_components.googlefindmy.api not importable; "
            "FCM provider only registered via location_request."
        )
    except Exception:  # noqa: BLE001
        import logging  # noqa: PLC0415

        logging.getLogger(__name__).warning(
            "Failed to register FCM provider via api.py; "
            "falling back to location_request registration.",
            exc_info=True,
        )

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

    print(
        f"Cleared {len(keys_to_remove)} cached token(s). Re-authentication required.\n"
    )


def _resolve_effective_entry_id(cli_entry: str | None, env_entry: str | None) -> str:
    """Resolve the effective entry id from CLI flag and env var.

    Priority: ``--entry`` > ``GOOGLEFINDMY_ENTRY_ID`` > "" (default).
    Returns the stripped value; whitespace-only input collapses to "".

    Extracted from the ``__main__`` block so the resolution rule is unit-
    testable without spawning a subprocess.
    """
    if cli_entry is not None:
        return cli_entry.strip()
    if env_entry:
        return env_entry.strip()
    return ""


async def _run_cli_bootstrap(file_cache: object, entry_id: str | None) -> None:
    """Run the standalone token bootstrap, then hand off to the interactive CLI.

    A single aiohttp session is shared across the whole flow and managed by
    ``async with`` so it is closed deterministically on *every* exit path,
    including the ``sys.exit(1)`` that ``_ensure_vault_keys`` raises on a fatal
    shared-key failure: ``__aexit__`` runs for ``BaseException`` (``SystemExit``,
    ``KeyboardInterrupt``) too.  Previously the session was created before the
    ``try`` and closed only in a ``finally``, so any ``sys.exit`` from the eager
    key-retrieval steps leaked it, producing 'Unclosed client session' warnings
    and the WinError-6 teardown noise on Windows.

    Extracted from the ``__main__`` block so the session lifecycle is testable
    without spawning a subprocess (mirrors ``_resolve_effective_entry_id``).
    """
    import contextlib  # noqa: PLC0415

    import aiohttp  # noqa: PLC0415

    from custom_components.googlefindmy.NovaApi.ListDevices.nbe_list_devices import (  # noqa: PLC0415
        _async_cli_main,
    )

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=16, enable_cleanup_closed=True)
    ) as session:
        # Exchange the single-use OAuth cookie for an AAS master token BEFORE
        # opening Chrome again for the shared key flow.  The OAuth cookie has a
        # very short TTL and is invalidated once a new Chrome session is opened,
        # so the exchange must happen immediately.
        await _ensure_aas_token(file_cache)
        # Eagerly obtain both vault keys (shared + owner); a vault failure exits
        # cleanly without deleting the durable AAS/oauth tokens.
        await _ensure_vault_keys(file_cache)
        await _clear_stale_adm_token(file_cache)
        fcm = await _setup_fcm_receiver(file_cache)
        try:
            await _async_cli_main(entry_id=entry_id, session=session)
        finally:
            with contextlib.suppress(Exception):
                await fcm.async_stop()


def _build_cli_parser() -> argparse.ArgumentParser:
    """Build the standalone CLI argument parser.

    Extracted from the ``__main__`` block so the flag surface (and therefore the
    ``--help`` output) is unit-testable without spawning a subprocess.
    """
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description="Google Find My Device CLI")
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Force re-authentication by clearing cached tokens and running Chrome login",
    )
    parser.add_argument(
        "--entry",
        default=None,
        help=(
            "Target config entry ID. Alternative to GOOGLEFINDMY_ENTRY_ID env var. "
            "Required when secrets.json contains caches for multiple HA config entries."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Enable verbose DEBUG logging for the bootstrap and FCM flow. "
            "Also enabled via the GOOGLEFINDMY_DEBUG environment variable."
        ),
    )
    return parser


def _configure_cli_logging(*, debug_flag: bool, env: Mapping[str, str]) -> None:
    """Configure root logging for the standalone CLI.

    The package never calls ``logging.basicConfig``; without it the root logger's
    last-resort handler discards everything below WARNING, so every
    ``_LOGGER.debug``/``info`` in the bootstrap and FCM path is invisible and the
    first-run locate timeout cannot be diagnosed from a log.  DEBUG is enabled
    when ``--debug`` is passed or ``GOOGLEFINDMY_DEBUG`` is truthy; otherwise the
    default keeps normal runs at WARNING so the critical readiness warning still
    surfaces without extra noise.

    Must be called before the bootstrap so the auth and FCM steps emit under the
    configured level.
    """
    import logging  # noqa: PLC0415

    env_value = env.get("GOOGLEFINDMY_DEBUG", "").strip().lower()
    debug_enabled = debug_flag or env_value not in ("", "0", "false", "no", "off")
    logging.basicConfig(
        level=logging.DEBUG if debug_enabled else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _main(argv: Sequence[str] | None = None) -> None:
    """Standalone CLI entry point.

    Extracted from the ``__main__`` guard so flag parsing, logging setup, and the
    bootstrap call order are unit-testable (the guard itself cannot be imported).
    Logging is configured *before* ``_ensure_authenticated`` so the Chrome/FCM
    diagnostics are captured from the very first step.
    """
    import asyncio  # noqa: PLC0415

    args = _build_cli_parser().parse_args(argv)
    _configure_cli_logging(debug_flag=args.debug, env=os.environ)

    # Always run the token-bootstrap pipeline, regardless of flat vs. repo
    # directory layout.  When a usable cache already exists, _ensure_authenticated
    # returns early so no Chrome login is launched; --reauth forces a fresh login.
    # This replaces the earlier branch gates (first the _standalone directory name,
    # then a cache-signal check that was always empty for a fresh shell start),
    # both of which could dead-end a repo-layout start in an opaque
    # MissingTokenCacheError instead of bootstrapping.
    if args.reauth:
        _clear_stale_tokens_for_reauth()
    _ensure_authenticated()
    # Resolve the effective entry id once (CLI > env > "") and use the SAME
    # value for both registration and hand-off, so the registry key and the
    # lookup hint can never diverge.  Forwarding the raw args.entry instead
    # would re-trigger _async_cli_main's env fallback for an explicit
    # --entry "" (empty string is falsy), making _resolve_cli_cache reject the
    # env value as "Unknown entry_id" although the cache was registered under
    # "".  Codex reviews on 694f6883aa and 17669c7ff2.
    effective_entry_id = _resolve_effective_entry_id(
        args.entry, os.environ.get("GOOGLEFINDMY_ENTRY_ID")
    )
    file_cache = _register_file_cache(effective_entry_id)

    try:
        asyncio.run(_run_cli_bootstrap(file_cache, effective_entry_id))
    except KeyboardInterrupt:
        print("\nExiting.")


if __name__ == "__main__":
    _main()
