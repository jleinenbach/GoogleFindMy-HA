# custom_components/googlefindmy/KeyBackup/shared_key_retrieval.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Shared key retrieval for Google Find My Device (async-first).

This module provides an asynchronous API to obtain the 32-byte *shared key*
used to decrypt E2EE payloads. The value is cached (per config entry via the
integration's async token cache) as a **hex string** under the key "shared_key".

Key properties:
- **Async-first**: `async_get_shared_key()` is the primary API.
- **Executor offloading**: any blocking/interactive flows are executed in a thread.
- **Normalization**: the cached value is normalized to a lowercase hex string.
- **Validation**: decoded key must be exactly 32 bytes (256 bit).
- **Fallbacks**:
  1) Read from cache (hex expected, base64 accepted once and auto-normalized).
  2) Derive from FCM credentials (private key), if available (non-interactive).
  3) As a last resort (CLI only), run the interactive shared-key flow.

The legacy sync facade `get_shared_key()` is kept for import compatibility for
CLI scripts, but it is **not** safe to call from the Home Assistant event loop.
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
    async_get_cached_value,
    async_get_cached_value_or_set,
    async_set_cached_value,
)

_LOGGER = logging.getLogger(__name__)

_CACHE_KEY = "shared_key"  # stored as lowercase hex string


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
# Retrieval strategies
# -----------------------------------------------------------------------------


async def _derive_from_fcm_credentials() -> str:
    """Try deriving the shared key from FCM credentials (non-interactive path).

    The FCM credential layout typically contains a private key in base64/base64url form.
    We derive deterministic 32-byte material by using the **last 32 bytes** of the DER
    payload, preserving the original behavior while avoiding interactive flows.

    Returns:
        str: lowercase hex string of 32 bytes.

    Raises:
        RuntimeError: if credentials are not present/invalid or too short.
    """
    creds: Any = await async_get_cached_value("fcm_credentials")
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


async def _retrieve_shared_key_hex() -> str:
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
        return await _derive_from_fcm_credentials()
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
# Public API (async-first)
# -----------------------------------------------------------------------------


async def async_get_shared_key() -> bytes:
    """Return the 32-byte shared key.

    Behavior:
        - Reads from the async token cache key "shared_key" (hex).
        - If absent, derives it (prefer FCM credentials) or runs the interactive flow
          when a TTY is detected (CLI only), then stores a normalized hex string.
        - Normalizes base64/base64url or PEM-like stored values to hex on first read.
        - Enforces a strict 32-byte length.

    Returns:
        bytes: a 32-byte key.

    Raises:
        RuntimeError: if a valid key cannot be obtained or normalized.
    """
    raw = await async_get_cached_value(_CACHE_KEY)
    if isinstance(raw, str) and raw.strip():
        s = raw.strip()
        # Try hex fast-path
        try:
            return _decode_hex_32(s)
        except ValueError:
            # Accept base64/base64url, then self-heal to hex
            b = _decode_base64_like_32(s)
            await async_set_cached_value(_CACHE_KEY, b.hex())
            _LOGGER.info("Normalized cached shared_key (base64) to hex")
            return b

    # Cache miss -> compute via strategy chain, then persist
    hex_value = await async_get_cached_value_or_set(_CACHE_KEY, _retrieve_shared_key_hex)

    # Validate persisted value and return as bytes
    try:
        return _decode_hex_32(hex_value)
    except ValueError as exc:
        # Clear invalid cache to avoid repeated failures
        await async_set_cached_value(_CACHE_KEY, None)
        raise RuntimeError(f"Persisted shared_key is invalid: {exc}") from exc


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
