# custom_components/googlefindmy/KeyBackup/shared_key_retrieval.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Shared key retrieval for Google Find My Device (async-first, entry-scoped capable).

This module provides an asynchronous API to obtain the 32-byte *shared key*
used to decrypt E2EE payloads.

Multi-account / entry-scoped design:
- When an entry-scoped `TokenCache` is supplied, **all** reads/writes are done
  strictly against that cache (no global fallback).
- In entry-scoped mode, the canonical cache key is `"shared_key"` (per entry).
- In global (legacy) mode, a per-user key `"shared_key_<username>"` is used,
  with one-time migration from legacy `"shared_key"` if present.

Normalization & validation:
- Values are stored as lowercase **hex strings**.
- On read, base64/base64url/PEM-like values are accepted once and normalized.
- Decoded key must be exactly 32 bytes (256 bit).

Retrieval strategy (when not cached):
1) Derive from FCM credentials (non-interactive, HA-friendly).
2) As a last resort (CLI only), run the interactive shared-key flow.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from binascii import Error as BinasciiError, unhexlify
from typing import Any, Optional

from custom_components.googlefindmy.Auth.token_cache import (
    TokenCache,
    async_get_cached_value,
    async_get_cached_value_or_set,
    async_set_cached_value,
)
from custom_components.googlefindmy.Auth.username_provider import (
    async_get_username,
    username_string,
)

_LOGGER = logging.getLogger(__name__)

_CACHE_KEY_BASE = "shared_key"  # canonical per-entry key in entry-scoped mode


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _decode_hex_32(s: str) -> bytes:
    """Decode a string as hex and ensure it is exactly 32 bytes.

    Accepts optional "0x" prefix and ignores whitespace. Pads odd lengths.

    Raises:
        ValueError: if decoding fails or the length is not 32 bytes.
    """
    t = (s or "").strip().lower()
    if t.startswith("0x"):
        t = t[2:]
    t = re.sub(r"\s+", "", t)
    # quick sanity
    if not re.fullmatch(r"[0-9a-f]*", t):
        raise ValueError("shared_key contains non-hex characters")
    if len(t) % 2:
        t = "0" + t
    try:
        b = unhexlify(t)
    except (BinasciiError, TypeError) as exc:
        raise ValueError("shared_key is not valid hex") from exc
    if len(b) != 32:
        raise ValueError(f"shared_key has invalid length {len(b)} bytes (expected 32)")
    return b


def _decode_base64_like_32(s: str) -> bytes:
    """Decode a base64/base64url/PEM-like string and ensure length 32 bytes.

    - Removes PEM-style headers/footers and whitespace
    - Adds padding as required
    - Tries urlsafe base64 first, then standard base64

    Raises:
        ValueError: if decoding fails or length != 32 bytes.
    """
    v = re.sub(r"-{5}BEGIN[^-]+-{5}|-{5}END[^-]+-{5}", "", s or "")
    v = re.sub(r"\s+", "", v)
    pad = (-len(v)) % 4
    v_padded = v + ("=" * pad)
    try:
        b = base64.urlsafe_b64decode(v_padded)
    except (ValueError, TypeError):
        try:
            b = base64.b64decode(v_padded)
        except (ValueError, TypeError) as exc:
            raise ValueError("shared_key is not valid base64/base64url") from exc
    if len(b) != 32:
        raise ValueError(f"shared_key (base64) has invalid length {len(b)} bytes (expected 32)")
    return b


async def _run_in_executor(func, *args):
    """Run a blocking callable in a thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


# -----------------------------------------------------------------------------
# Retrieval strategies (cache-aware)
# -----------------------------------------------------------------------------

async def _derive_from_fcm_credentials(*, cache: TokenCache | None) -> str:
    """Try deriving the shared key from FCM credentials (non-interactive path).

    The FCM credential layout typically contains a private key in base64/base64url form.
    We derive deterministic 32-byte material by using the **last 32 bytes** of the DER
    payload, preserving prior behavior while avoiding interactive flows.

    Returns:
        str: lowercase hex string of 32 bytes.

    Raises:
        RuntimeError: if credentials are not present/invalid or too short.
    """
    if cache is not None:
        creds: Any = await cache.get("fcm_credentials")
    else:
        creds = await async_get_cached_value("fcm_credentials")

    if isinstance(creds, str):
        try:
            creds = json.loads(creds)
        except (json.JSONDecodeError, TypeError):
            creds = {}
    if not isinstance(creds, dict):
        raise RuntimeError("No FCM credentials available in cache")

    private_b64: Optional[str] = None
    keys_obj = creds.get("keys")
    if isinstance(keys_obj, dict):
        priv = keys_obj.get("private")
        if isinstance(priv, str) and priv.strip():
            private_b64 = priv.strip()

    if not private_b64:
        raise RuntimeError("FCM credentials have no private key to derive from")

    # Normalize PEM-ish inputs and whitespace; add padding
    v = re.sub(r"-{5}BEGIN[^-]+-{5}|-{5}END[^-]+-{5}", "", private_b64)
    v = re.sub(r"\s+", "", v)
    v_padded = v + ("=" * ((-len(v)) % 4))

    try:
        der = base64.urlsafe_b64decode(v_padded)
    except (ValueError, TypeError):
        try:
            der = base64.b64decode(v_padded)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(f"FCM private key is not valid base64/base64url: {exc}") from exc

    if len(der) < 32:
        raise RuntimeError(f"FCM private key too short ({len(der)} bytes); cannot derive shared key")

    shared = der[-32:]
    return shared.hex()


async def _interactive_flow_hex() -> str:
    """Run the interactive shared-key flow (CLI only) and return a hex string.

    This opens a browser and requires a TTY; **not suitable for Home Assistant**.
    We keep it as a last-resort fallback for developer CLI usage.
    """
    from custom_components.googlefindmy.KeyBackup.shared_key_flow import (  # lazy import
        request_shared_key_flow,
    )

    # Run potentially interactive/GUI logic in executor
    result = await _run_in_executor(request_shared_key_flow)

    # Normalize the result to hex
    if isinstance(result, (bytes, bytearray)):
        b = bytes(result)
        if len(b) != 32:
            raise RuntimeError(f"Interactive shared key has invalid length {len(b)} (expected 32)")
        return b.hex()

    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Interactive shared key flow returned empty/invalid result")

    s = result.strip()
    # Try hex first, then base64-like
    try:
        return _decode_hex_32(s).hex()
    except ValueError:
        return _decode_base64_like_32(s).hex()


async def _retrieve_shared_key_hex(*, cache: TokenCache | None) -> str:
    """Strategy chain to obtain a hex-encoded shared key (32 bytes).

    Order:
        1) Try deriving from FCM credentials (non-interactive, HA-friendly).
        2) If allowed (CLI/TTY), run the interactive flow in an executor.

    Returns:
        str: lowercase hex string of the 32-byte key.

    Raises:
        RuntimeError: if neither strategy can provide a valid key.
    """
    # 1) Non-interactive derivation (preferred for HA)
    try:
        return await _derive_from_fcm_credentials(cache=cache)
    except Exception as err:
        _LOGGER.debug("FCM-derivation for shared key not available: %s", err)

    # 2) Interactive flow (only if we seem to be in a CLI/TTY)
    try:
        import sys

        if sys.stdin and sys.stdin.isatty():
            _LOGGER.info("Falling back to interactive shared key flow (CLI mode detected)")
            return await _interactive_flow_hex()
        raise RuntimeError("Interactive flow not available in non-interactive environment")
    except Exception as err:
        raise RuntimeError(f"Failed to retrieve shared key: {err}") from err


# -----------------------------------------------------------------------------
# Cache orchestration (entry-scoped vs global legacy)
# -----------------------------------------------------------------------------

def _user_scoped_key(username: str) -> str:
    return f"{_CACHE_KEY_BASE}_{username}"


async def _get_or_generate_shared_key_hex(
    *,
    cache: TokenCache | None,
    username: Optional[str],
) -> str:
    """Return the shared key hex string with proper scoping & one-time migration.

    Entry-scoped mode (cache is provided):
        - Primary key: "shared_key" (per entry).
        - If missing and a user-scoped legacy key exists in this cache (rare), migrate it.
        - Otherwise generate via strategy chain and store under "shared_key".

    Global legacy mode (no cache provided):
        - Primary key: "shared_key_<username>" (per-user).
        - If missing, migrate from legacy "shared_key" to user-scoped key if present.
        - Otherwise generate via strategy chain and store under "shared_key_<username>".
    """
    if cache is not None:
        # Primary key in entry-scoped mode
        existing = await cache.get(_CACHE_KEY_BASE)
        if isinstance(existing, str) and existing.strip():
            return existing

        # Optional: migrate from user-scoped legacy key within the same cache (defensive)
        if isinstance(username, str) and username:
            legacy_user_key = await cache.get(_user_scoped_key(username))
            if isinstance(legacy_user_key, str) and legacy_user_key.strip():
                await cache.set(_CACHE_KEY_BASE, legacy_user_key)
                _LOGGER.debug("Migrated legacy user-scoped shared_key to entry-scoped key")
                return legacy_user_key

        # Generate fresh and persist
        return await cache.get_or_set(_CACHE_KEY_BASE, lambda: _retrieve_shared_key_hex(cache=cache))

    # --- Global legacy mode below ---
    # Need a username to scope keys safely
    if not isinstance(username, str) or not username:
        # Resolve from global cache (best-effort)
        resolved = await async_get_cached_value(username_string)
        username = str(resolved) if isinstance(resolved, str) and resolved else await async_get_username()

    if not isinstance(username, str) or not username:
        raise RuntimeError("Username is not configured; cannot resolve shared_key in global mode.")

    user_key = _user_scoped_key(username)

    # Per-user primary key
    existing = await async_get_cached_value(user_key)
    if isinstance(existing, str) and existing.strip():
        return existing

    # One-time migration from legacy global "shared_key"
    legacy = await async_get_cached_value(_CACHE_KEY_BASE)
    if isinstance(legacy, str) and legacy.strip():
        await async_set_cached_value(user_key, legacy)
        _LOGGER.debug("Migrated legacy global shared_key to per-user key: %s", user_key)
        return legacy

    # Generate and store under per-user key
    return await async_get_cached_value_or_set(user_key, lambda: _retrieve_shared_key_hex(cache=None))


# -----------------------------------------------------------------------------
# Public API (async-first, entry-scoped capable)
# -----------------------------------------------------------------------------

async def async_get_shared_key(
    *,
    cache: TokenCache | None = None,
    username: Optional[str] = None,
) -> bytes:
    """Return the 32-byte shared key (entry-scoped capable).

    Behavior:
        - Entry-scoped mode (preferred in HA): use per-entry key "shared_key".
        - Global legacy mode: use per-user key "shared_key_<username>" with migration.
        - Normalizes base64/base64url/PEM-like stored values to hex on first read.
        - Enforces a strict 32-byte length.

    Returns:
        bytes: a 32-byte key.

    Raises:
        RuntimeError: if a valid key cannot be obtained or normalized.
    """
    hex_value = await _get_or_generate_shared_key_hex(cache=cache, username=username)

    # Validate and return as bytes; self-heal non-hex to hex
    try:
        return _decode_hex_32(hex_value)
    except ValueError:
        # Try base64-like and normalize
        b = _decode_base64_like_32(hex_value)
        if cache is not None:
            await cache.set(_CACHE_KEY_BASE, b.hex())
        else:
            # Determine where to write in global mode
            user = username
            if not isinstance(user, str) or not user:
                # Try resolve for normalization path
                resolved = await async_get_cached_value(username_string)
                user = str(resolved) if isinstance(resolved, str) and resolved else await async_get_username()
            if isinstance(user, str) and user:
                await async_set_cached_value(_user_scoped_key(user), b.hex())
            else:
                # Fallback to legacy slot if user resolution failed
                await async_set_cached_value(_CACHE_KEY_BASE, b.hex())
        _LOGGER.info("Normalized cached shared_key to hex")
        return b


# -----------------------------------------------------------------------------
# Legacy sync facade (CLI compatibility)
# -----------------------------------------------------------------------------

def get_shared_key() -> bytes:  # pragma: no cover
    """Sync facade for CLI tools (NOT for Home Assistant event loop).

    - If called from within a running event loop, raises RuntimeError immediately.
    - Otherwise, runs the async API via `asyncio.run()` and returns the bytes.

    Returns:
        bytes: a 32-byte shared key.

    Raises:
        RuntimeError: if called inside an event loop.
    """
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            raise RuntimeError(
                "Sync get_shared_key() called from within the event loop. "
                "Use `await async_get_shared_key()` instead."
            )
    except RuntimeError:
        # No running loop -> OK for CLI usage
        pass
    return asyncio.run(async_get_shared_key())
