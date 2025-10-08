# custom_components/googlefindmy/SpotApi/GetEidInfoForE2eeDevices/get_owner_key.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Owner key retrieval and normalization for Google Find My Device (async-first).

This module provides an asynchronous API to obtain the per-user *owner key* that is
required to decrypt location payloads. It migrates legacy global cache layout to a
per-user storage model, uses the HA-native async token cache, and offloads any
potentially blocking calls (network/crypto/CPU-bound) to an executor.

Key properties:
- **Per-user cache**: keys are stored under `owner_key_<username>`.
- **Legacy migration**: a legacy `owner_key` (global) is migrated once if present.
- **Normalization**: owner keys are normalized to **hex strings** in storage.
- **Validation**: decoded keys must be exactly 32 bytes (256 bit).
- **Async-only**: the public API is `async_get_owner_key()`; the sync wrapper is disabled.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from binascii import Error as BinasciiError, unhexlify
from typing import Any

from custom_components.googlefindmy.Auth.token_cache import (
    async_get_cached_value,
    async_get_cached_value_or_set,
    async_set_cached_value,
)
from custom_components.googlefindmy.Auth.username_provider import async_get_username
from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import decrypt_owner_key
from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import get_shared_key
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
    get_eid_info,
)

_LOGGER = logging.getLogger(__name__)

# Cache key base for owner keys. We migrate from legacy "owner_key" to per-user keys.
_OWNER_KEY_CACHE_PREFIX = "owner_key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_cache_key(username: str) -> str:
    """Return the per-user cache key for the owner key hex string."""
    return f"{_OWNER_KEY_CACHE_PREFIX}_{username}"


def _try_hex(s: str) -> bytes:
    """Try to decode a string as hexadecimal.

    Accepts optional "0x" prefix and ignores whitespace. Pads odd-length strings.
    Raises:
        BinasciiError: if the string contains non-hex characters.
    """
    t = (s or "").strip().lower()
    if t.startswith("0x"):
        t = t[2:]
    # Allow whitespace in user-pasted values
    t = re.sub(r"\s+", "", t)
    if not re.fullmatch(r"[0-9a-f]*", t):
        raise BinasciiError("String contains non-hexadecimal characters.")
    if len(t) % 2:
        t = "0" + t  # Prepend a zero to make it even-length
    return unhexlify(t)


def _try_base64_like(s: str) -> bytes:
    """Try to decode a string as base64/base64url/PEM-like content.

    - Removes PEM-style headers/footers.
    - Strips whitespace.
    - Adds missing padding for base64/base64url.

    Returns:
        bytes: decoded bytes
    Raises:
        ValueError | TypeError: if decoding fails for both urlsafe and standard base64.
    """
    v = re.sub(r"-{5}BEGIN[^-]+-{5}|-{5}END[^-]+-{5}", "", s or "")
    v = re.sub(r"\s+", "", v)
    pad = (-len(v)) % 4
    v_padded = v + ("=" * pad)
    try:
        return base64.urlsafe_b64decode(v_padded)
    except (ValueError, TypeError):
        return base64.b64decode(v_padded)


async def _run_in_executor(func, *args):
    """Run a blocking callable in a thread executor."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


# ---------------------------------------------------------------------------
# Core retrieval
# ---------------------------------------------------------------------------


async def _retrieve_owner_key(username: str) -> str:
    """Retrieve and decrypt the owner key for `username`, returning a hex string.

    Steps:
        1. Call SPOT GetEidInfoForE2eeDevices (may raise SpotApiEmptyResponseError).
        2. Obtain shared key (bytes).
        3. Decrypt the encrypted owner key to raw bytes.
        4. Return as lowercase hex string.

    Raises:
        SpotApiEmptyResponseError: If SPOT returns an empty/trailers-only body.
        RuntimeError: If required fields/keys are missing or invalid.
    """
    try:
        # Potentially performs I/O and parsing -> run in executor
        eid_info = await _run_in_executor(get_eid_info)
    except SpotApiEmptyResponseError:
        # Clear, actionable message; token invalidation/retry happens in spot_request.
        _LOGGER.error(
            "Owner key retrieval failed: SPOT returned empty/trailers-only body for "
            "GetEidInfoForE2eeDevices (likely auth/session issue). Please re-authenticate."
        )
        raise

    # May involve cache I/O/crypto initialization -> run in executor
    shared_key: Any = await _run_in_executor(get_shared_key)
    if not isinstance(shared_key, (bytes, bytearray)) or not shared_key:
        raise RuntimeError("Shared key is missing or empty; cannot decrypt owner key")

    # Guards for presence and non-empty encrypted owner key
    metadata = getattr(eid_info, "encryptedOwnerKeyAndMetadata", None)
    if metadata is None:
        raise RuntimeError("Missing 'encryptedOwnerKeyAndMetadata' in eid_info")

    encrypted_owner_key = getattr(metadata, "encryptedOwnerKey", b"")
    if not isinstance(encrypted_owner_key, (bytes, bytearray)) or len(encrypted_owner_key) == 0:
        raise RuntimeError("Missing or empty 'encryptedOwnerKey' in eid_info.encryptedOwnerKeyAndMetadata")

    # Crypto is CPU-bound -> run in executor
    owner_key: Any = await _run_in_executor(decrypt_owner_key, shared_key, encrypted_owner_key)
    owner_key_version = getattr(metadata, "ownerKeyVersion", None)

    if not isinstance(owner_key, (bytes, bytearray)) or len(owner_key) == 0:
        raise RuntimeError("Decrypted owner_key is empty or invalid type")

    _LOGGER.info(
        "Retrieved owner key (version=%s, len=%s) for user=%s",
        owner_key_version,
        len(owner_key),
        username,
    )

    return bytes(owner_key).hex()


async def _get_or_generate_user_owner_key_hex(username: str) -> str:
    """Return the per-user owner key (hex string), migrating and generating if needed.

    Order of operations:
        1) Try per-user cache key first.
        2) Migrate from legacy global `owner_key` once if present.
        3) Generate and cache via `async_get_cached_value_or_set` (per-user key).
    """
    user_key_name = _user_cache_key(username)

    # 1) Per-user cache hit?
    user_key = await async_get_cached_value(user_key_name)
    if isinstance(user_key, str) and user_key:
        return user_key

    # 2) Legacy migration path: move global 'owner_key' to user-scoped cache if present.
    legacy = await async_get_cached_value(_OWNER_KEY_CACHE_PREFIX)
    if isinstance(legacy, str) and legacy:
        await async_set_cached_value(user_key_name, legacy)
        # Do not clear legacy immediately; let subsequent runs be idempotent
        _LOGGER.debug("Migrated legacy 'owner_key' to user-scoped cache for %s", username)
        return legacy

    # 3) Generate fresh value and cache under the user-specific key.
    return await async_get_cached_value_or_set(user_key_name, lambda: _retrieve_owner_key(username))


# ---------------------------------------------------------------------------
# Public API (async-first)
# ---------------------------------------------------------------------------


async def async_get_owner_key() -> bytes:
    """Return the binary owner key (32 bytes) for the current user.

    Behavior:
        - Uses a per-user cache key and migrates from legacy `owner_key` if found.
        - Accepts and normalizes the cached key to a hex string.
        - Can decode base64/base64url/PEM-like content once and self-heal to hex.
        - Enforces a strict 32-byte key length.

    Returns:
        bytes: the owner key (exactly 32 bytes)

    Raises:
        RuntimeError: if the key is missing/invalid or has incorrect length.
    """
    username = await async_get_username()
    if not isinstance(username, str) or not username:
        raise RuntimeError("Username is not configured; cannot retrieve owner key.")

    raw_value = await _get_or_generate_user_owner_key_hex(username)

    # 1) Fast path: try hex (canonical format)
    key_bytes: bytes
    try:
        key_bytes = _try_hex(raw_value)
    except (BinasciiError, TypeError):
        # 2) Fallback: try base64/base64url/PEM-like
        try:
            key_bytes = _try_base64_like(raw_value)
        except Exception as exc:
            _LOGGER.error(
                "Owner key for user '%s' is not valid hex or base64/base64url. "
                "Please store the key as a 64-char hex string (32 bytes). Error: %s",
                username,
                exc,
            )
            # Clear per-user & legacy cache to prevent repeated failures on the same invalid data
            await async_set_cached_value(_user_cache_key(username), None)
            await async_set_cached_value(_OWNER_KEY_CACHE_PREFIX, None)
            raise RuntimeError("Invalid owner_key format (expect 32-byte key in hex or base64).") from exc
        else:
            # Self-heal: normalize the cache to hex for future consistency
            _LOGGER.info(
                "Successfully decoded owner key from a non-hex format; normalizing cache to hex."
            )
            await async_set_cached_value(_user_cache_key(username), key_bytes.hex())

    # 3) Final validation: the owner key must be exactly 32 bytes long
    if len(key_bytes) != 32:
        _LOGGER.error(
            "Owner key for user '%s' has an invalid length: %d bytes (expected 32). "
            "Clear credentials and re-authenticate if this persists.",
            username,
            len(key_bytes),
        )
        await async_set_cached_value(_user_cache_key(username), None)
        await async_set_cached_value(_OWNER_KEY_CACHE_PREFIX, None)
        raise RuntimeError("Owner key must be exactly 32 bytes long.")

    return key_bytes


# ---------------------------------------------------------------------------
# Legacy sync facade (disabled by design)
# ---------------------------------------------------------------------------


def get_owner_key() -> bytes:  # pragma: no cover - kept for import compatibility
    """Legacy sync facade — intentionally unsupported inside Home Assistant.

    This function exists only to preserve import compatibility for external/CLI scripts.
    It **must not** be used from within the HA event loop and intentionally raises to
    enforce the async-first contract. CLI users should run `async_get_owner_key()` via
    `asyncio.run(...)`.

    Raises:
        NotImplementedError: Always. Use `async_get_owner_key()` instead.
    """
    raise NotImplementedError("Use async_get_owner_key() instead.")
