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
- Callers **must** supply the entry-scoped `TokenCache`. All reads/writes are
  performed against that cache; global facades have been removed to avoid
  cross-account leakage.
- The canonical cache key is `"shared_key"` (per entry).
- For backwards compatibility within the same entry, the helper still migrates
  previously stored user-scoped keys (e.g. `"shared_key_<username>"`).

Normalization & validation:
- Values are stored as lowercase **hex strings**.
- On read, base64/base64url/PEM-like values are accepted once and normalized.
- Decoded key must be exactly 32 bytes (256 bit).

Retrieval strategy (when not cached):
- In the command-line tool: interactive browser flow via ``shared_key_flow.py``
  (the only authoritative source — retrieves the real vault key from Google's Key
  Backup service). The command-line entry point marks its own process; a
  terminal alone is not enough (see ``_cli_process``).
- In non-interactive / HA mode: the key must be pre-populated from the secrets
  bundle during ``async_setup_entry()``.  If missing, a descriptive error is raised.
"""

from __future__ import annotations

import base64
import importlib
import logging
import os
import re
import sys
from binascii import Error as BinasciiError
from binascii import unhexlify
from collections.abc import Awaitable, Callable
from typing import cast

from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.typing_utils import (
    run_in_executor as _run_in_executor,
)

_LOGGER = logging.getLogger(__name__)

SHARED_KEY_LEN = 32

_CACHE_KEY_BASE = "shared_key"  # canonical per-entry key in entry-scoped mode


class SharedKeyUnavailableError(RuntimeError):
    """The shared key is genuinely absent and cannot be obtained here.

    Raised when an entry has no usable cached shared key and retrieval is
    impossible in the current context (e.g. non-interactive / HA mode, where the
    key must be pre-populated from the secrets bundle). This is a *genuine
    credential defect*, not a transient miss: the user must re-import a complete
    secrets bundle.

    It is a ``RuntimeError`` subclass so the existing owner-key handlers that
    catch ``(InvalidTag, RuntimeError)`` keep catching it. Downstream classifiers
    (see ``decrypt_locations._classify_owner_key_failure``) key on this *type*
    rather than the message wording, so the absent-key case escalates to reauth
    robustly even if the human-readable message changes.
    """


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
    if len(b) != SHARED_KEY_LEN:
        raise ValueError(
            f"shared_key has invalid length {len(b)} bytes (expected {SHARED_KEY_LEN})"
        )
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
    if len(b) != SHARED_KEY_LEN:
        raise ValueError(
            f"shared_key (base64) has invalid length {len(b)} bytes (expected {SHARED_KEY_LEN})"
        )
    return b


# -----------------------------------------------------------------------------
# Retrieval (cache-aware)
# -----------------------------------------------------------------------------


async def _interactive_flow_hex() -> str:
    """Run the interactive shared-key flow (CLI only) and return a hex string.

    This opens a browser and requires a TTY; **not suitable for Home Assistant**.
    We keep it as a last-resort fallback for developer CLI usage.
    """
    shared_key_flow = importlib.import_module(
        "custom_components.googlefindmy.KeyBackup.shared_key_flow"
    )
    request_shared_key_flow = shared_key_flow.request_shared_key_flow

    # Run potentially interactive/GUI logic in executor
    result = await _run_in_executor(request_shared_key_flow)

    # Normalize the result to hex
    if isinstance(result, (bytes, bytearray)):
        b = bytes(result)
        if len(b) != SHARED_KEY_LEN:
            raise RuntimeError(
                f"Interactive shared key has invalid length {len(b)} (expected {SHARED_KEY_LEN})"
            )
        return b.hex()

    if not isinstance(result, str) or not result.strip():
        raise RuntimeError("Interactive shared key flow returned empty/invalid result")

    s = result.strip()
    # Try hex first, then base64-like
    try:
        return _decode_hex_32(s).hex()
    except ValueError:
        return _decode_base64_like_32(s).hex()


# Guard: only attempt the browser flow once per process to avoid opening
# multiple Chrome windows on retry paths (e.g. blind refresh in
# async_retrieve_identity_key).
_browser_flow_attempted = False


# Set by the command-line entry point on its own process (``main.py`` does it
# before anything else). Home Assistant never sets it, which is the whole point:
# the previous guard asked "is a terminal attached", and a foreground Home
# Assistant answers yes.
_ENV_CLI_PROCESS = "GOOGLEFINDMY_CLI_PROCESS"

# Same opt-in `Auth/auth_flow.py` already uses for consoles that carry a user but
# report no tty (IDE run windows).
_ENV_ASSUME_INTERACTIVE = "GOOGLEFINDMY_ASSUME_INTERACTIVE"


def _cli_process() -> bool:
    """Return True when this process identified itself as the CLI tool."""

    return os.environ.get(_ENV_CLI_PROCESS) == "1"


def _attended_session() -> bool:
    """Return True when somebody can answer the browser prompt.

    The browser flow waits for a human to sign in. A command-line run is a
    necessary condition, not a sufficient one: `main.py` sets its marker
    unconditionally, so an unattended run with redirected stdin (cron, a
    container entrypoint) would otherwise open Chrome and wait forever.
    """

    if os.environ.get(_ENV_ASSUME_INTERACTIVE) == "1":
        return True
    return bool(sys.stdin and sys.stdin.isatty())


async def _retrieve_shared_key_hex() -> str:
    """Obtain a hex-encoded shared key (32 bytes) via the interactive browser flow.

    The interactive flow opens Chrome, navigates to Google's Key Backup vault,
    and extracts the authoritative shared key via JavaScript interception.
    This is the **only** way to obtain the correct shared key.

    In non-interactive (HA) mode, the shared key must be pre-populated from
    the secrets bundle during ``async_setup_entry()``.  This function is only
    reached when the cache has no shared key.

    A module-level guard prevents the browser from being opened more than once
    per process lifetime, avoiding the "5 browser windows" problem when multiple
    retry paths each trigger shared key retrieval.

    Returns:
        str: lowercase hex string of the 32-byte key.

    Raises:
        RuntimeError: if the key cannot be obtained.
    """
    global _browser_flow_attempted  # noqa: PLW0603

    is_attended = _attended_session()
    is_cli = _cli_process()

    if is_attended and not is_cli:
        # A terminal is not a command line. Home Assistant started in the
        # foreground of a terminal answers `isatty()` with True, and would then
        # have opened a browser from inside the Home Assistant process. Say what
        # is missing rather than failing silently: someone running an unforeseen
        # command-line wrapper can set the marker themselves.
        raise SharedKeyUnavailableError(
            "Refusing to open the interactive browser flow: a terminal is "
            "attached, but this process is not the Google Find My command-line "
            f"tool. Run `python main.py`, or set {_ENV_CLI_PROCESS}=1 if you are "
            "driving the extraction from your own wrapper."
        )

    if is_cli and not is_attended:
        # The marker says "command-line tool", the session says "nobody here".
        # Opening Chrome now would hang until the timeout with no way to answer.
        raise SharedKeyUnavailableError(
            "Refusing to open the interactive browser flow: this is the command-line "
            "tool, but no terminal is attached. Run it from an interactive terminal, "
            f"or set {_ENV_ASSUME_INTERACTIVE}=1 if you are at a console that reports "
            "no tty."
        )

    if is_cli:
        if _browser_flow_attempted:
            raise RuntimeError(
                "Shared key browser flow already attempted in this session. "
                "Restart with --reauth to try again."
            )
        _browser_flow_attempted = True
        try:
            _LOGGER.info(
                "Retrieving shared key via interactive browser flow (CLI mode)"
            )
            return await _interactive_flow_hex()
        except Exception as err:
            _LOGGER.warning("Interactive shared key flow failed: %s", err)
            raise RuntimeError(
                "Shared key retrieval failed. "
                "Ensure Chrome/Chromium is installed for the browser-based flow."
            ) from err

    raise SharedKeyUnavailableError(
        "Shared key not available in non-interactive environment. "
        "Provide the key via secrets bundle or run the CLI with --reauth."
    )


# -----------------------------------------------------------------------------
# Cache orchestration (entry-scoped vs global legacy)
# -----------------------------------------------------------------------------


def _user_scoped_key(username: str) -> str:
    return f"{_CACHE_KEY_BASE}_{username}"


async def _get_or_generate_shared_key_hex(
    *,
    cache: TokenCache,
    username: str | None,
    force_refresh: bool = False,
) -> str:
    """Return the shared key hex string with proper scoping & one-time migration."""
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    if force_refresh:
        _LOGGER.info("Force-refreshing shared_key (clearing cached value)")
        try:
            await cache.set(_CACHE_KEY_BASE, None)
            if isinstance(username, str) and username:
                await cache.set(_user_scoped_key(username), None)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Failed to clear cached shared_key during force refresh")

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
    async def _generate() -> str:
        return await _retrieve_shared_key_hex()

    generator = cast(
        Callable[[], Awaitable[str] | str],
        _generate,
    )

    generated: str = await cache.get_or_set(
        _CACHE_KEY_BASE,
        generator,
    )

    if not isinstance(generated, str):
        raise RuntimeError("Shared key generator returned non-string value")

    return generated


# -----------------------------------------------------------------------------
# Public API (async-first, entry-scoped capable)
# -----------------------------------------------------------------------------


async def async_get_shared_key(
    *,
    cache: TokenCache,
    username: str | None = None,
    force_refresh: bool = False,
) -> bytes:
    """Return the 32-byte shared key (entry-scoped capable).

    Behavior:
        - Entry-scoped mode (preferred in HA): use per-entry key "shared_key".
        - Global legacy mode: use per-user key "shared_key_<username>" with migration.
        - Normalizes base64/base64url/PEM-like stored values to hex on first read.
        - Enforces a strict 32-byte length.
        - When ``force_refresh`` is True, cached values are cleared before
          retrieval.  In HA (non-interactive) mode this raises RuntimeError,
          which triggers the reauth flow.

    Returns:
        bytes: a 32-byte key.

    Raises:
        RuntimeError: if a valid key cannot be obtained or normalized.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    hex_value = await _get_or_generate_shared_key_hex(
        cache=cache, username=username, force_refresh=force_refresh
    )

    # Validate and return as bytes; self-heal non-hex to hex
    try:
        return _decode_hex_32(hex_value)
    except ValueError:
        # Try base64-like and normalize
        b = _decode_base64_like_32(hex_value)
        await cache.set(_CACHE_KEY_BASE, b.hex())
        _LOGGER.info("Normalized cached shared_key to hex")
        return b
