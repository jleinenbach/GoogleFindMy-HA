# custom_components/googlefindmy/Auth/token_cache.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import json
import os
import asyncio
import threading
from typing import Dict, Optional, Any

try:
    import aiofiles  # optional; kept for compatibility, not required by the locked path
except ImportError:
    aiofiles = None

SECRETS_FILE = "secrets.json"

# --- Helper: normalize FCM credentials (stringified JSON -> dict) ----------------
def _normalize_fcm_credentials(value: Any) -> Any:
    """Normalize 'fcm_credentials' to a dict if it's a JSON string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value

# --- Concurrency primitives ------------------------------------------------------
# 1) Per-process write lock for file + memory updates (also safe from sync paths).
_write_lock = threading.RLock()
# 2) Per-key async locks to deduplicate generator work (avoid thundering herd).
_async_key_locks: Dict[str, asyncio.Lock] = {}

def _get_async_key_lock(name: str) -> asyncio.Lock:
    """Return a stable per-key asyncio.Lock (created lazily)."""
    lock = _async_key_locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _async_key_locks[name] = lock
    return lock

# --- Internal helpers (single source of truth = in-memory cache) -----------------
def _get_secrets_file() -> str:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, SECRETS_FILE)

def _read_secrets_file_sync() -> Dict[str, Any]:
    """Read file contents atomically under the write lock and return a dict."""
    with _write_lock:
        secrets_file = _get_secrets_file()
        if not os.path.exists(secrets_file):
            return {}
        try:
            with open(secrets_file, "r") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

def _set_cached_value_sync(name: str, value: Optional[Any]) -> None:
    """Update memory and write the full cache to disk atomically."""
    with _write_lock:
        # Update memory first
        if value is None:
            _memory_cache.pop(name, None)
        else:
            _memory_cache[name] = value

        # Persist entire memory snapshot to avoid overwriting concurrent keys
        secrets_file = _get_secrets_file()
        with open(secrets_file, "w") as f:
            json.dump(_memory_cache, f)

# --- Public API (sync) -----------------------------------------------------------
def get_cached_value_or_set(name: str, generator) -> Any:
    """Return cached value or compute+store synchronously."""
    existing_value = get_cached_value(name)
    if existing_value is not None:
        return existing_value

    value = generator() if callable(generator) else generator
    set_cached_value(name, value)
    return value

def get_cached_value(name: str) -> Any:
    """Sync getter with memory fast-path and atomic file backfill."""
    # Memory fast path
    val = get_from_memory_cache(name)
    if val is not None:
        if name == "fcm_credentials":
            val = _normalize_fcm_credentials(val)
            _memory_cache[name] = val
        return val

    # Read from disk (under lock), then backfill memory
    data = _read_secrets_file_sync()
    if data:
        # Normalize FCM creds in the loaded snapshot
        if "fcm_credentials" in data:
            data["fcm_credentials"] = _normalize_fcm_credentials(data["fcm_credentials"])
        set_memory_cache(data)
        return data.get(name)
    return None

def set_cached_value(name: str, value: Optional[Any]) -> None:
    """Sync setter (atomic write via process-wide lock)."""
    _set_cached_value_sync(name, value)

# --- Public API (async) ----------------------------------------------------------
async def async_get_cached_value_or_set(name: str, generator):
    """Return cached value or compute+store with per-key async lock."""
    existing_value = await async_get_cached_value(name)
    if existing_value is not None:
        return existing_value

    lock = _get_async_key_lock(name)
    async with lock:
        # Re-check after acquiring the lock
        existing_value = await async_get_cached_value(name)
        if existing_value is not None:
            return existing_value

        value = await generator() if asyncio.iscoroutinefunction(generator) else generator()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _set_cached_value_sync, name, value)
        return value

async def async_get_cached_value(name: str):
    """Async getter with memory fast-path and executor-backed file read."""
    # Memory fast path
    val = get_from_memory_cache(name)
    if val is not None:
        if name == "fcm_credentials":
            val = _normalize_fcm_credentials(val)
            _memory_cache[name] = val
        return val

    # Consistent read via the same sync helper (under lock) in executor
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _read_secrets_file_sync)
    if data:
        if "fcm_credentials" in data:
            data["fcm_credentials"] = _normalize_fcm_credentials(data["fcm_credentials"])
        set_memory_cache(data)
        return data.get(name)
    return None

async def async_set_cached_value(name: str, value: Optional[Any]):
    """Async setter using the sync writer under executor (atomic write)."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _set_cached_value_sync, name, value)

# --- Bulk helpers / diagnostics --------------------------------------------------
def save_oauth_token(token: str):
    """Save OAuth token to cache."""
    set_cached_value("oauth_token", token)

async def async_save_oauth_token(token: str):
    """Save OAuth token to cache (async version)."""
    await async_set_cached_value("oauth_token", token)

def load_oauth_token():
    """Load OAuth token from cache."""
    return get_cached_value("oauth_token")

async def async_load_oauth_token():
    """Load OAuth token from cache (async version)."""
    return await async_get_cached_value("oauth_token")

def get_all_cached_values() -> Dict[str, Any]:
    """
    Return a full snapshot from disk (source of truth) and refresh memory.
    This avoids returning a partial in-memory view.
    """
    data = _read_secrets_file_sync()
    if data and "fcm_credentials" in data:
        data["fcm_credentials"] = _normalize_fcm_credentials(data["fcm_credentials"])
    set_memory_cache(data)
    return data

async def async_get_all_cached_values() -> Dict[str, Any]:
    """Async variant of get_all_cached_values()."""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _read_secrets_file_sync)
    if data and "fcm_credentials" in data:
        data["fcm_credentials"] = _normalize_fcm_credentials(data["fcm_credentials"])
    set_memory_cache(data)
    return data

# --- Global in-memory cache (single source of truth for this process) -----------
_memory_cache: Dict[str, Any] = {}

def set_memory_cache(data: dict):
    """Set the in-memory cache for Home Assistant (avoids file I/O in event loop)."""
    global _memory_cache
    with _write_lock:
        _memory_cache = data.copy()

def get_from_memory_cache(key: str):
    """Get a value from the in-memory cache."""
    return _memory_cache.get(key)

async def async_load_cache_from_file():
    """Load the entire cache from file into memory (async)."""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, _read_secrets_file_sync)
    if data and "fcm_credentials" in data:
        data["fcm_credentials"] = _normalize_fcm_credentials(data["fcm_credentials"])
    set_memory_cache(data)
    return data
