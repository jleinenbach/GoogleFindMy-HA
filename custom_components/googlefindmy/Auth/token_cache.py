# custom_components/googlefindmy/Auth/token_cache.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Async, in-memory cache with an HA-native Store backend, scoped per config entry.

This module provides a TokenCache class that encapsulates all state (Store, data, locks)
and offers a purely asynchronous API for safe use within the Home Assistant event loop.
It includes a backward-compatibility facade for legacy modules, designed for a phased
deprecation. The facade ensures single-entry setups continue to work while providing
hard failures for ambiguous multi-entry scenarios.

Key properties:
- Entry-scoped storage file: each ConfigEntry uses its own Store key.
- Deferred, atomic writes using Home Assistant's `Store` helper.
- JSON snapshot validation before persisting to disk.
- Merge migration from legacy Auth/secrets.json (best-effort, once per process).
- Flush on HA STOP and on config entry unload; `close()` prevents further writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Mapping, TypedDict, cast
from collections.abc import Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


CacheState = dict[str, Any]


class CacheData(TypedDict, total=False):
    """Type-safe, JSON-serializable structure for data persisted in the Store.

    `total=False` allows sparse dictionaries where some keys may not be present.
    """

    oauth_token: str
    username: str
    fcm_credentials: dict[str, Any] | str | None
    # Other keys historically stored in secrets.json are implicitly allowed.


class TokenCache:
    """Async in-memory cache backed by HA Store, scoped per config entry.

    The cache delays disk writes via `Store.async_delay_save` to minimize I/O on
    flash storage while guaranteeing atomic persistence. Use `flush()` on shutdown.
    """

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize a TokenCache for a specific config entry.

        Args:
            hass: The Home Assistant instance.
            entry_id: The unique ID of the ConfigEntry.
        """
        if not isinstance(entry_id, str):
            raise TypeError("TokenCache requires entry_id to be a string.")

        normalized_entry_id = entry_id.strip()
        if not normalized_entry_id:
            raise ValueError("TokenCache requires a non-empty entry_id.")

        self._hass = hass
        self.entry_id = normalized_entry_id
        # Each entry gets its own storage file: f"{STORAGE_KEY}_{entry_id}"
        self._store: Store[CacheData] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}_{normalized_entry_id}"
        )
        self._data: CacheState = {}
        self._write_lock = asyncio.Lock()
        self._per_key_locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    # ------------------------------- Factory ---------------------------------

    @classmethod
    async def create(
        cls, hass: HomeAssistant, entry_id: str, legacy_path: str | None = None
    ) -> TokenCache:
        """Create a TokenCache, load Store data, and (optionally) migrate legacy file.

        Args:
            hass: The Home Assistant instance.
            entry_id: The ConfigEntry ID used for scoping the Store file.
            legacy_path: Optional path to an old `secrets.json` for a one-time migration.

        Returns:
            An initialized TokenCache instance.
        """
        instance = cls(hass, entry_id)

        data = await instance._store.async_load()
        if isinstance(data, dict):
            instance._data = instance._coerce_cache_state(data)
            _LOGGER.debug(
                "googlefindmy: Cache loaded from Store for entry '%s' (%d keys).",
                instance.entry_id,
                len(instance._data),
            )

        if legacy_path:
            await instance._migrate_legacy_file(legacy_path)

        return instance

    # ------------------------------ Migration --------------------------------

    async def _migrate_legacy_file(self, legacy_path: str) -> None:
        """Migrate an old JSON file to the Store (merge) and remove it once, process-wide."""
        global _LEGACY_MIGRATION_DONE
        if _LEGACY_MIGRATION_DONE:
            return
        _LEGACY_MIGRATION_DONE = True

        def _read_legacy() -> Mapping[str, Any] | None:
            if not os.path.exists(legacy_path):
                return None
            try:
                with open(legacy_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return cast(Mapping[str, Any], data)
                return None
            except Exception:
                _LOGGER.exception("Failed to read legacy cache file at %s", legacy_path)
                return None

        legacy_data = await self._hass.async_add_executor_job(_read_legacy)
        if legacy_data is None:
            return

        normalized_legacy = self._coerce_cache_state(legacy_data)

        async with self._write_lock:
            # Merge strategy: keys already in Store override legacy keys.
            merged: CacheState = {**normalized_legacy, **self._data}
            if "fcm_credentials" in merged:
                merged["fcm_credentials"] = self._normalize_fcm(
                    merged["fcm_credentials"]
                )

            if merged != self._data:
                self._data = merged
                if self._is_valid_snapshot():
                    self._store.async_delay_save(self._snapshot, 1.0)
                _LOGGER.info("googlefindmy: Merged legacy cache into the Store.")

        def _remove_legacy() -> None:
            try:
                os.remove(legacy_path)
            except OSError:
                _LOGGER.warning(
                    "Failed to remove legacy cache file after migration: %s",
                    legacy_path,
                )

        await self._hass.async_add_executor_job(_remove_legacy)

    # ------------------------------- Get/Set ---------------------------------

    async def get(self, name: str) -> Any:
        """Return a value from the in-memory cache (non-blocking)."""
        return self._data.get(name)

    async def set(self, name: str, value: Any | None) -> None:
        """Set a value, normalize it, validate snapshot, and schedule a deferred save.

        Raises:
            RuntimeError: If called after `close()`.
        """
        if self._closed:
            raise RuntimeError("TokenCache is closed; writes are disallowed.")

        normalized = self._normalize_on_write(name, value)

        async with self._write_lock:
            if self._data.get(name) == normalized:
                return  # No change, no I/O

            if normalized is None:
                if self._data.pop(name, None) is not None:
                    # Clean up per-key lock on removal to avoid unbounded growth.
                    self._per_key_locks.pop(name, None)
            else:
                if not self._is_jsonable(normalized):
                    _LOGGER.error(
                        "Value for key '%s' is not JSON-serializable; skipping save.",
                        name,
                    )
                    return
                self._data[name] = normalized

        # Only schedule a save if the snapshot is valid JSON.
        if self._is_valid_snapshot():
            self._store.async_delay_save(self._snapshot, 1.2)
        else:
            _LOGGER.error(
                "Aborting deferred save due to non-JSON-serializable snapshot."
            )

    async def get_or_set(
        self, name: str, generator: Callable[[], Awaitable[Any] | Any]
    ) -> Any:
        """Return existing value or compute/store it, avoiding thundering herds via per-key lock."""
        if (existing := self._data.get(name)) is not None:
            return existing

        lock = self._per_key_locks.setdefault(name, asyncio.Lock())
        async with lock:
            if (existing := self._data.get(name)) is not None:
                return existing

            new_value = generator()
            if asyncio.iscoroutine(new_value):
                new_value = await new_value

            await self.set(name, new_value)
            return new_value

    async def all(self) -> CacheData:
        """Return a shallow copy of the entire cache snapshot."""
        return self._snapshot()

    # ---------- Coordinator/API compatibility aliases (Protocol-friendly) ----------

    async def async_get_cached_value(self, name: str) -> Any:
        """Alias for compatibility with CacheProtocol used by coordinator/api."""
        return await self.get(name)

    async def async_set_cached_value(self, name: str, value: Any | None) -> None:
        """Alias for compatibility with CacheProtocol used by coordinator/api."""
        await self.set(name, value)

    async def async_get_cached_value_or_set(
        self, name: str, generator: Callable[[], Awaitable[Any] | Any]
    ) -> Any:
        """Alias for compatibility with potential CacheProtocol callers."""
        return await self.get_or_set(name, generator)

    # ------------------------------ Persistence ------------------------------

    async def flush(self) -> None:
        """Force an immediate save of any pending changes to disk."""
        if not self._closed and self._is_valid_snapshot():
            await self._store.async_save(self._snapshot())

    async def close(self) -> None:
        """Mark the cache as closed and perform a final flush; block further writes."""
        if self._closed:
            return
        self._closed = True
        await self.flush()

    # ------------------------------ Utilities --------------------------------

    @staticmethod
    def _is_jsonable(value: Any) -> bool:
        try:
            json.dumps(value)
            return True
        except TypeError:
            return False

    def _is_valid_snapshot(self) -> bool:
        for key, val in self._data.items():
            if not self._is_jsonable(val):
                _LOGGER.error(
                    "Snapshot contains non-JSON-serializable value for key '%s'", key
                )
                return False
        return True

    @staticmethod
    def _normalize_on_write(name: str, value: Any) -> Any:
        if name == "fcm_credentials":
            return TokenCache._normalize_fcm(value)
        return value

    @staticmethod
    def _normalize_fcm(value: Any) -> dict[str, Any] | str | None:
        if value is None:
            return None

        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
            if isinstance(parsed, dict):
                return cast(dict[str, Any], parsed)
            return value

        if isinstance(value, dict):
            return cast(dict[str, Any], value)

        return cast(dict[str, Any] | str | None, value)

    def _snapshot(self) -> CacheData:
        """Return a serializable snapshot of the cache for Store persistence."""

        return cast(CacheData, dict(self._data))

    @staticmethod
    def _coerce_cache_state(raw: Mapping[Any, Any]) -> CacheState:
        """Coerce raw mappings from disk or legacy sources into CacheState."""

        coerced: CacheState = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                _LOGGER.debug("Skipping non-string cache key from persisted data: %r", key)
                continue
            coerced[key] = value

        if "fcm_credentials" in coerced:
            coerced["fcm_credentials"] = TokenCache._normalize_fcm(
                coerced["fcm_credentials"]
            )

        return coerced


# -------------------------- Global registry & facade --------------------------

_INSTANCES: dict[str, TokenCache] = {}
_DEFAULT_ENTRY_ID: str | None = None
_LEGACY_MIGRATION_DONE: bool = False


def _register_instance(entry_id: str, instance: TokenCache) -> None:
    """Register a TokenCache instance for a config entry ID (internal)."""
    cache_entry_id = getattr(instance, "entry_id", None)
    if isinstance(cache_entry_id, str):
        normalized = cache_entry_id.strip()
        if normalized and normalized != entry_id:
            _LOGGER.warning(
                "TokenCache entry_id mismatch: registry key '%s' vs instance '%s'. Using registry key.",
                entry_id,
                normalized,
            )
            try:
                setattr(instance, "entry_id", entry_id)
            except Exception as err:  # noqa: BLE001 - defensive logging only
                _LOGGER.debug(
                    "Failed to correct TokenCache entry_id to '%s': %s", entry_id, err
                )
        elif not normalized:
            try:
                setattr(instance, "entry_id", entry_id)
            except Exception as err:  # noqa: BLE001 - defensive logging only
                _LOGGER.debug(
                    "Failed to assign entry_id '%s' to TokenCache instance: %s",
                    entry_id,
                    err,
                )
    else:
        try:
            setattr(instance, "entry_id", entry_id)
        except Exception as err:  # noqa: BLE001 - defensive logging only
            _LOGGER.debug(
                "Failed to assign entry_id '%s' to TokenCache instance: %s",
                entry_id,
                err,
            )
    _INSTANCES[entry_id] = instance


def _unregister_instance(entry_id: str) -> TokenCache | None:
    """Unregister and return the TokenCache instance for a config entry ID (internal)."""
    global _DEFAULT_ENTRY_ID
    if _DEFAULT_ENTRY_ID == entry_id:
        _DEFAULT_ENTRY_ID = None
    return _INSTANCES.pop(entry_id, None)


def _set_default_entry_id(entry_id: str) -> None:
    """Set the default entry for facade calls (only for single-entry scenarios)."""
    global _DEFAULT_ENTRY_ID
    if len(_INSTANCES) > 1 and _DEFAULT_ENTRY_ID != entry_id:
        # Immediately disallow ambiguous facade usage in multi-entry setups.
        _DEFAULT_ENTRY_ID = None
        _LOGGER.warning(
            "Multiple config entries are active. Global cache calls are ambiguous and will fail."
        )
    else:
        _DEFAULT_ENTRY_ID = entry_id


def _get_default_cache() -> TokenCache:
    """Return the default cache instance or raise if ambiguous/missing."""
    if _DEFAULT_ENTRY_ID and (cache := _INSTANCES.get(_DEFAULT_ENTRY_ID)):
        return cache
    if not _INSTANCES:
        raise RuntimeError(
            "No TokenCache registered. Provide the entry-scoped TokenCache (for example, "
            "entry.runtime_data.token_cache)."
        )
    if len(_INSTANCES) == 1:
        return next(iter(_INSTANCES.values()))
    raise RuntimeError(
        "Multiple config entries active. Use an entry-id-specific cache or pass `entry.runtime_data`."
    )


def get_cache_for_entry(entry_id: str) -> TokenCache:
    """Return the registered TokenCache for a config entry ID."""

    if entry_id in _INSTANCES:
        return _INSTANCES[entry_id]
    raise KeyError(f"No TokenCache registered for entry_id '{entry_id}'.")


def get_registered_entry_ids() -> list[str]:
    """Return a list of registered entry IDs (for diagnostics/CLI helpers)."""

    return list(_INSTANCES)


# ------------------------------- Public facade --------------------------------


async def async_get_cached_value(name: str) -> Any:
    """Facade: return a value from the default cache."""
    cache = _get_default_cache()
    return await cache.get(name)


async def async_set_cached_value(name: str, value: Any | None) -> None:
    """Facade: set a value in the default cache."""
    cache = _get_default_cache()
    await cache.set(name, value)


async def async_get_cached_value_or_set(
    name: str, generator: Callable[[], Awaitable[Any] | Any]
) -> Any:
    """Facade: return a value or generate/store it."""
    cache = _get_default_cache()
    return await cache.get_or_set(name, generator)


async def async_get_all_cached_values() -> CacheData:
    """Facade: return a full snapshot from the default cache."""
    cache = _get_default_cache()
    return await cache.all()


def get_cached_value(name: str) -> Any:
    """Legacy sync facade. Must not be called from the event loop.

    Raises:
        RuntimeError: If called inside the event loop (use async variant instead).
    """
    try:
        asyncio.get_running_loop()  # raises if no running loop
    except RuntimeError:
        if not _INSTANCES:
            return None
        cache = _get_default_cache()
        return cache._data.get(name)
    else:
        raise RuntimeError(
            f"Sync `get_cached_value({name!r})` used inside event loop. "
            "Use `async_get_cached_value` instead."
        )


def set_cached_value(name: str, value: Any | None) -> None:
    """Legacy sync facade. Must not be called from the event loop.

    Raises:
        RuntimeError: If called inside the event loop (use async variant instead).
    """
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            raise RuntimeError(
                f"Sync `set_cached_value({name!r})` used inside event loop. "
                "Use `async_set_cached_value` instead."
            )
    except RuntimeError:
        # No running loop; proceed synchronously
        pass

    if not _INSTANCES:
        _LOGGER.warning("Cache not initialized; cannot set '%s'", name)
        return

    cache = _get_default_cache()
    # Direct write to in-memory dict for sync context (CLI/tests). No disk write here.
    if value is None:
        cache._data.pop(name, None)
    else:
        cache._data[name] = value


def get_cached_value_or_set(name: str, generator: Callable[[], Any]) -> Any:
    """Legacy sync 'get or set' facade (CLI/tests only).

    IMPORTANT:
        - Must NOT be called from inside the Home Assistant event loop.
        - Avoids I/O; writes only to in-memory cache for the current process.

    Behavior:
        If the key exists, returns it.
        Otherwise, computes value via `generator()`, stores it in-memory, and returns it.
    """
    # Prevent usage in the event loop
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            raise RuntimeError(
                f"Sync `get_cached_value_or_set({name!r})` used inside event loop. "
                "Use `async_get_cached_value_or_set` instead."
            )
    except RuntimeError:
        # No running loop -> safe to proceed
        pass

    if not _INSTANCES:
        _LOGGER.warning(
            "Cache not initialized; computing '%s' without storing persistently", name
        )
        return generator()

    cache = _get_default_cache()
    if name in cache._data:
        return cache._data[name]

    value = generator()
    cache._data[name] = value
    return value


__all__ = [
    "CacheData",
    "TokenCache",
    "async_get_cached_value",
    "async_set_cached_value",
    "async_get_cached_value_or_set",
    "async_get_all_cached_values",
    "get_cached_value",
    "set_cached_value",
    "get_cached_value_or_set",
    "_register_instance",
    "_unregister_instance",
    "_set_default_entry_id",
]
