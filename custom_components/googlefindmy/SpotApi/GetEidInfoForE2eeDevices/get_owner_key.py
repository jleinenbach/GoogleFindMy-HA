# custom_components/googlefindmy/SpotApi/GetEidInfoForE2eeDevices/get_owner_key.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""Owner key retrieval and normalization for Google Find My Device.

This module provides an asynchronous API to obtain the per-user *owner key*
that is required to decrypt location payloads.

Multi-account design (entry-scoped):
- Callers **must** provide the entry-scoped TokenCache. All reads/writes are
  performed strictly against that cache; global facades have been removed.
- Keys are stored per user under ``owner_key_<username>``. A legacy non-scoped
  ``owner_key`` is migrated *within the same cache scope* (if present).
- Username resolution uses the same entry-scoped cache.

Normalization & validation:
- Owner keys are normalized to **hex strings** in storage.
- Decoded keys must be exactly 32 bytes (256 bit).
- Potentially blocking calls (network/crypto/CPU-bound) are offloaded to an executor.

Injection points (optional):
- ``eid_info_getter``: async callable that returns EID info (for tests/flows).
- ``shared_key_getter``: async callable that returns the shared key bytes.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import math
import re
import time
from binascii import Error as BinasciiError
from binascii import unhexlify
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Final, Literal, NamedTuple, TypeGuard

from cryptography.exceptions import InvalidTag
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.googlefindmy._reauth_reason import ReauthReasonCode
from custom_components.googlefindmy.Auth.aas_token_retrieval import (
    _mask_email_for_logs,
)
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.Auth.username_provider import (
    async_get_username,
    username_string,
)
from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import (
    decrypt_owner_key,
)
from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import (
    async_get_shared_key,
)
from custom_components.googlefindmy.KeyBackup.vault_web_api import (
    OWNER_KEY_VERSIONS_FIELD,
    VaultEntry,
    from_bundle_field,
)
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    SpotApiEmptyResponseError,
    async_get_eid_info,
)
from custom_components.googlefindmy.SpotApi.spot_request import SpotAuthPermanentError
from custom_components.googlefindmy.typing_utils import (
    run_in_executor as _run_in_executor,
)

_LOGGER = logging.getLogger(__name__)

# Cache key base for owner keys. We migrate from legacy "owner_key" to per-user keys.
_OWNER_KEY_CACHE_PREFIX = "owner_key"
_OWNER_KEY_LENGTH = 32


class OwnerKeyInfo(NamedTuple):
    """Owner key material with version metadata."""

    key: bytes
    version: int | None


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
    return bytes(unhexlify(t))


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
        decoded = base64.urlsafe_b64decode(v_padded)
    except (ValueError, TypeError):
        decoded = base64.b64decode(v_padded)
    return bytes(decoded)


# ---------------------------------------------------------------------------
# Core retrieval (entry-scoped when a TokenCache is supplied)
# ---------------------------------------------------------------------------


async def _retrieve_owner_key(
    username: str,
    *,
    cache: TokenCache,
    eid_info_getter: Callable[[], Awaitable[Any]] | None = None,
    shared_key_getter: Callable[[], Awaitable[bytes]] | None = None,
) -> dict[str, Any]:
    """Retrieve and decrypt the owner key for `username`, returning cache payload.

    Steps:
        1. Call SPOT GetEidInfoForE2eeDevices (may raise SpotApiEmptyResponseError).
        2. Obtain shared key (bytes).
        3. Decrypt the encrypted owner key to raw bytes.
        4. Return a mapping containing lowercase hex key string and owner key version.

    The retrieval of EID info and shared key can be provided via async callables;
    when omitted, synchronous helpers are executed in a thread pool.

    Raises:
        SpotApiEmptyResponseError: If SPOT returns an empty/trailers-only body.
        RuntimeError: If required fields/keys are missing or invalid.
    """
    if eid_info_getter is not None:
        eid_info = await eid_info_getter()
    else:
        try:
            eid_info = await async_get_eid_info(cache=cache)
        except SpotAuthPermanentError as exc:
            reauth_exc = ConfigEntryAuthFailed(
                "Owner key retrieval failed: Google session invalid; re-authentication required."
            )
            reauth_exc.reauth_code = ReauthReasonCode.OWNER_KEY_PERMANENT
            raise reauth_exc from exc
        except SpotApiEmptyResponseError:
            _LOGGER.error(
                "Owner key retrieval failed: SPOT returned an empty/trailers-only "
                "response for GetEidInfoForE2eeDevices; the exact gRPC status is "
                "logged for diagnosis."
            )
            raise

    if shared_key_getter is not None:
        shared_key: Any = await shared_key_getter()
    else:
        shared_key = await async_get_shared_key(cache=cache, username=username)

    if not isinstance(shared_key, (bytes, bytearray)) or not shared_key:
        raise RuntimeError("Shared key is missing or empty; cannot decrypt owner key")

    metadata = getattr(eid_info, "encryptedOwnerKeyAndMetadata", None)
    if metadata is None:
        # R9b: log WHICH eid_info fields the server populated (names only, never
        # values/bytes) so a partial response is diagnosable beyond "Missing X".
        _LOGGER.debug(
            "eid_info field shape on missing metadata: populated=%s",
            [
                name
                for name in ("encryptedOwnerKeyAndMetadata",)
                if getattr(eid_info, name, None)
            ],
        )
        raise RuntimeError("Missing 'encryptedOwnerKeyAndMetadata' in eid_info")

    encrypted_owner_key = getattr(metadata, "encryptedOwnerKey", b"")
    if (
        not isinstance(encrypted_owner_key, (bytes, bytearray))
        or len(encrypted_owner_key) == 0
    ):
        # R9b: list the metadata field NAMES the server populated (never values /
        # decrypted bytes) so the missing-owner-key shape is visible at DEBUG.
        _LOGGER.debug(
            "eid_info.encryptedOwnerKeyAndMetadata field shape on missing "
            "encryptedOwnerKey: populated=%s",
            [
                name
                for name in (
                    "encryptedOwnerKey",
                    "ownerKeyVersion",
                    "securityDomain",
                )
                if getattr(metadata, name, None)
            ],
        )
        raise RuntimeError(
            "Missing or empty 'encryptedOwnerKey' in eid_info.encryptedOwnerKeyAndMetadata"
        )

    try:
        owner_key: Any = await _run_in_executor(
            decrypt_owner_key, shared_key, encrypted_owner_key
        )
    except InvalidTag:
        # Propagate InvalidTag unchanged. The caller (decrypt_locations) maps it to
        # SharedKeyMismatchError to distinguish "shared key wrong/stale" from
        # "shared key missing" (RuntimeError). Wrapping it into RuntimeError here
        # would erase that discriminator and collapse both root causes into one
        # opaque message, which is exactly what hid the real cause from users.
        raise
    owner_key_version = getattr(metadata, "ownerKeyVersion", None)

    if not isinstance(owner_key, (bytes, bytearray)) or len(owner_key) == 0:
        # R9b: log the decrypted owner-key shape (type + length only, NEVER the
        # bytes/value) so an empty/short result is diagnosable.
        _LOGGER.debug(
            "Decrypted owner_key shape on empty/invalid result: type=%s, len=%s",
            type(owner_key).__name__,
            len(owner_key) if isinstance(owner_key, (bytes, bytearray)) else "n/a",
        )
        raise RuntimeError("Decrypted owner_key is empty or invalid type")

    _LOGGER.info(
        "Retrieved owner key (version=%s, len=%s) for user=%s",
        owner_key_version,
        len(owner_key),
        _mask_email_for_logs(username),
    )

    return {
        "key": bytes(owner_key).hex(),
        "version": owner_key_version if isinstance(owner_key_version, int) else None,
    }


async def _get_or_generate_user_owner_key_hex(
    username: str,
    *,
    cache: TokenCache,
    eid_info_getter: Callable[[], Awaitable[Any]] | None = None,
    shared_key_getter: Callable[[], Awaitable[bytes]] | None = None,
    force_refresh: bool = False,
) -> Any:
    """Return the per-user owner key payload, migrating and generating if needed.

    Cache behavior:
        - All operations are performed against the provided entry-scoped cache.
        - Legacy `owner_key` (non-scoped) is migrated *within the same cache scope*.
        - Cached values may be a string (legacy) or mapping with ``{"key": str, "version": int | None}``.
        - When ``force_refresh`` is True, skip cached values and fetch a fresh key from the server.
    """
    user_key_name = _user_cache_key(username)

    # 1) Per-user cache hit?
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    # Skip cache lookups entirely when a forced refresh is requested.
    if force_refresh:
        fresh = await _retrieve_owner_key(
            username,
            cache=cache,
            eid_info_getter=eid_info_getter,
            shared_key_getter=shared_key_getter,
        )
        await cache.set(user_key_name, fresh)
        return fresh

    user_key = await cache.get(user_key_name)
    if isinstance(user_key, dict) and user_key.get("key"):
        return user_key

    if isinstance(user_key, str) and user_key:
        return user_key

    # 2) Legacy migration (within the same cache scope only)
    legacy = await cache.get(_OWNER_KEY_CACHE_PREFIX)
    if isinstance(legacy, dict) and legacy.get("key"):
        await cache.set(user_key_name, legacy)
        _LOGGER.debug(
            "Migrated legacy 'owner_key' (entry-scoped) to user-scoped cache for %s",
            _mask_email_for_logs(username),
        )
        return legacy

    if isinstance(legacy, str) and legacy:
        await cache.set(user_key_name, legacy)
        _LOGGER.debug(
            "Migrated legacy 'owner_key' (entry-scoped) to user-scoped cache for %s",
            _mask_email_for_logs(username),
        )
        return legacy

    # 3) Generate fresh value and cache under the user-specific key.
    return await cache.get_or_set(
        user_key_name,
        lambda: _retrieve_owner_key(
            username,
            cache=cache,
            eid_info_getter=eid_info_getter,
            shared_key_getter=shared_key_getter,
        ),
    )


# ---------------------------------------------------------------------------
# Public API (async-first, entry-scoped capable)
# ---------------------------------------------------------------------------


async def async_get_owner_key(  # noqa: PLR0912,PLR0915
    *,
    cache: TokenCache,
    username: str | None = None,
    eid_info_getter: Callable[[], Awaitable[Any]] | None = None,
    shared_key_getter: Callable[[], Awaitable[bytes]] | None = None,
    force_refresh: bool = False,
) -> OwnerKeyInfo:
    """Return the binary owner key (32 bytes) for the current user (entry-scoped capable).

    Behavior:
        - Uses a per-user cache key and migrates from legacy `owner_key` if found
          within the same cache scope.
        - Accepts and normalizes the cached key to a hex string.
        - Can decode base64/base64url/PEM-like content once and self-heal to hex.
        - Enforces a strict 32-byte key length.
        - When ``force_refresh`` is True, bypasses cached values and retrieves the owner key directly.
        - If `cache` is provided, username resolution will prefer the same cache.

    Returns:
        OwnerKeyInfo: the owner key (exactly 32 bytes) and version metadata if available.

    Raises:
        RuntimeError: if the key is missing/invalid or has incorrect length.
    """
    if cache is None:
        raise ValueError("TokenCache instance is required for multi-account safety.")

    # Resolve username (prefer entry-scoped cache).
    user: str | None
    if username is not None:
        user = username if isinstance(username, str) and username else None
    else:
        cached_username = await cache.get(username_string)
        user = (
            cached_username
            if isinstance(cached_username, str) and cached_username
            else None
        )
        if user is None:
            resolved_username = await async_get_username(cache=cache)
            user = (
                resolved_username
                if isinstance(resolved_username, str) and resolved_username
                else None
            )

    if user is None:
        raise RuntimeError("Username is not configured; cannot retrieve owner key.")

    try:
        raw_value = await _get_or_generate_user_owner_key_hex(
            user,
            cache=cache,
            eid_info_getter=eid_info_getter,
            shared_key_getter=shared_key_getter,
            force_refresh=force_refresh,
        )
    except SpotApiEmptyResponseError as exc:
        # E3-W: ground the wording in the observed state only; the
        # ConfigEntryAuthFailed type (and thus the reauth behavior) is kept
        # UNCHANGED deliberately, because whether an empty/trailers-only response
        # is transient or auth is not yet proven. The captured gRPC status (R9a)
        # makes the next real incident diagnosable before any reclassification.
        reauth_exc = ConfigEntryAuthFailed(
            "Owner key retrieval failed: SPOT returned an empty/trailers-only "
            "response; the exact gRPC status is logged for diagnosis."
        )
        reauth_exc.reauth_code = ReauthReasonCode.OWNER_KEY_EMPTY_RESPONSE
        raise reauth_exc from exc

    cache_version: int | None = None
    cache_key_value: str | None = None
    if isinstance(raw_value, dict):
        version_value = raw_value.get("version")
        if isinstance(version_value, int):
            cache_version = version_value
        elif isinstance(version_value, str) and version_value.isdigit():
            cache_version = int(version_value)

        key_candidate = raw_value.get("key")
        cache_key_value = key_candidate if isinstance(key_candidate, str) else None
    elif isinstance(raw_value, str):
        cache_key_value = raw_value

    if cache_key_value is None:
        await cache.set(_user_cache_key(user), None)
        await cache.set(_OWNER_KEY_CACHE_PREFIX, None)
        raise RuntimeError("Owner key is missing or invalid; please re-authenticate.")

    decoded_from_non_hex = False
    key_bytes: bytes
    try:
        key_bytes = _try_hex(cache_key_value)
    except (BinasciiError, TypeError):
        # 2) Fallback: try base64/base64url/PEM-like
        try:
            key_bytes = _try_base64_like(cache_key_value)
            decoded_from_non_hex = True
        except Exception as exc:
            _LOGGER.error(
                "Owner key for user '%s' is not valid hex or base64/base64url. "
                "Please store the key as a 64-char hex string (32 bytes). Error: %s",
                _mask_email_for_logs(user),
                exc,
            )
            # Clear per-user & legacy cache to prevent repeated failures on the same invalid data
            await cache.set(_user_cache_key(user), None)
            await cache.set(_OWNER_KEY_CACHE_PREFIX, None)
            raise RuntimeError(
                "Invalid owner_key format (expect 32-byte key in hex or base64)."
            ) from exc
    normalized_value = {"key": key_bytes.hex(), "version": cache_version}
    if decoded_from_non_hex:
        # Self-heal: normalize the cache to hex for future consistency
        _LOGGER.info(
            "Successfully decoded owner key from a non-hex format; normalizing cache to hex."
        )

    if decoded_from_non_hex or not isinstance(raw_value, dict):
        await cache.set(_user_cache_key(user), normalized_value)

    # 3) Final validation: the owner key must be exactly 32 bytes long
    if len(key_bytes) != _OWNER_KEY_LENGTH:
        _LOGGER.error(
            "Owner key for user '%s' has an invalid length: %d bytes (expected 32). "
            "Clear credentials and re-authenticate if this persists.",
            _mask_email_for_logs(user),
            len(key_bytes),
        )
        await cache.set(_user_cache_key(user), None)
        await cache.set(_OWNER_KEY_CACHE_PREFIX, None)
        raise RuntimeError("Owner key must be exactly 32 bytes long.")

    return OwnerKeyInfo(key=key_bytes, version=cache_version)


async def async_fetch_owner_key_uncached(
    *, cache: TokenCache, username: str | None
) -> OwnerKeyInfo:
    """Fetch the owner key from Spot without reading or writing the owner-key cache.

    For the login tool's vault self-test, which needs the owner key of *this*
    run rather than a cached one. The shared key is read through
    ``async_get_shared_key`` like in ``_retrieve_owner_key``; nothing under the
    ``owner_key`` prefix is touched.

    Raises:
        RuntimeError: Spot returned no usable owner key, or it is not 32 bytes.
        InvalidTag: the shared key does not open the owner key.
    """
    payload = await _retrieve_owner_key(username or "", cache=cache)
    key_hex = payload.get("key")
    if not isinstance(key_hex, str):
        raise RuntimeError("Owner key payload carries no key.")
    key_bytes = bytes.fromhex(key_hex)
    if len(key_bytes) != _OWNER_KEY_LENGTH:
        raise RuntimeError("Owner key must be exactly 32 bytes long.")
    version = payload.get("version")
    return OwnerKeyInfo(
        key=key_bytes, version=version if isinstance(version, int) else None
    )


# ---------------------------------------------------------------------------
# Owner-key versions from the vault bundle field (issue #246)
# ---------------------------------------------------------------------------

VaultLookupStatus = Literal[
    "found", "field_missing", "version_missing", "undecryptable"
]


@dataclass(frozen=True, slots=True)
class VaultOwnerKeyLookup:
    """Outcome of a vault lookup; ``key`` is the unwrapped owner key or ``None``.

    ``repr()`` and ``str()`` name only ``status`` and ``versions``, never bytes.
    """

    key: bytes | None = field(repr=False)
    status: VaultLookupStatus
    versions: tuple[int, ...]


def _lookup_failure(
    status: VaultLookupStatus, versions: tuple[int, ...] = ()
) -> VaultOwnerKeyLookup:
    return VaultOwnerKeyLookup(key=None, status=status, versions=versions)


async def _read_vault_field(
    cache: TokenCache,
) -> tuple[VaultEntry, ...] | VaultLookupStatus:
    """Read and parse the bundle field; a status string on any failure."""
    try:
        raw_field = await cache.get(OWNER_KEY_VERSIONS_FIELD)
    except Exception as exc:  # noqa: BLE001 - lookup is best-effort by contract
        _LOGGER.debug(
            "Vault owner-key lookup: field read failed (%s)", type(exc).__name__
        )
        return "field_missing"
    if raw_field is None:
        return "field_missing"
    try:
        entries = from_bundle_field(raw_field)
    except Exception as exc:  # noqa: BLE001 - lookup is best-effort by contract
        _LOGGER.debug(
            "Vault owner-key lookup: field parse failed (%s)", type(exc).__name__
        )
        entries = None
    return "undecryptable" if entries is None else entries


async def _read_wrapping_key(cache: TokenCache) -> bytes | VaultLookupStatus:
    """Read the shared key like ``decrypt_locations``; a status string on failure."""
    try:
        cached_user = await cache.get(username_string)
        wrapping_key = await async_get_shared_key(
            cache=cache,
            username=cached_user if isinstance(cached_user, str) else None,
        )
    except Exception as exc:  # noqa: BLE001 - lookup is best-effort by contract
        _LOGGER.debug(
            "Vault owner-key lookup: shared key read failed (%s)", type(exc).__name__
        )
        return "field_missing"
    if not isinstance(wrapping_key, (bytes, bytearray)) or not wrapping_key:
        return "undecryptable"
    return bytes(wrapping_key)


async def _unwrap_vault_blob(wrapping_key: bytes, blob: bytes) -> bytes | None:
    """Unwrap a vault blob in the executor; ``None`` unless exactly 32 bytes."""
    try:
        unwrapped: Any = await _run_in_executor(decrypt_owner_key, wrapping_key, blob)
    except Exception as exc:  # noqa: BLE001 - lookup is best-effort by contract
        _LOGGER.debug("Vault owner-key lookup: unwrap failed (%s)", type(exc).__name__)
        return None
    if (
        not isinstance(unwrapped, (bytes, bytearray))
        or len(unwrapped) != _OWNER_KEY_LENGTH
    ):
        return None
    return bytes(unwrapped)


async def async_get_vault_owner_key(
    *, cache: TokenCache, version: int
) -> VaultOwnerKeyLookup:
    """Unwrap the owner key of ``version`` from the bundle field; never raises.

    Reads the field ``owner_key_versions`` and the shared key from the entry
    cache and decrypts the vault entry in the executor, as ``_retrieve_owner_key``
    does for the Spot owner key. The unwrapped key is never persisted.

    Error contract: any exception while reading the cache (field, username or
    shared key, including a cache without ``get``) gives ``field_missing``; an
    absent field gives ``field_missing``; a field in a foreign shape gives
    ``undecryptable``; a field without ``version`` gives ``version_missing``; a
    missing shared key, a failing decryption or a result that is not exactly 32
    bytes gives ``undecryptable``. Only the exception class name is logged, at
    DEBUG. ``asyncio.CancelledError`` is not an ``Exception`` and propagates.
    """
    entries = await _read_vault_field(cache)
    if isinstance(entries, str):
        return _lookup_failure(entries)
    versions = tuple(entry.version for entry in entries)
    entry = next((item for item in entries if item.version == version), None)
    if entry is None:
        return _lookup_failure("version_missing", versions)
    wrapping_key = await _read_wrapping_key(cache)
    if isinstance(wrapping_key, str):
        return _lookup_failure(wrapping_key, versions)
    unwrapped = await _unwrap_vault_blob(wrapping_key, entry.key)
    if unwrapped is None:
        return _lookup_failure("undecryptable", versions)
    return VaultOwnerKeyLookup(key=unwrapped, status="found", versions=versions)


# Minimum spacing of owner-key Spot refreshes outside a poll cycle (push path,
# CLI). Equal to DEFAULT_LOCATION_POLL_INTERVAL, kept local so this module does
# not depend on const.py.
OWNER_KEY_SPOT_REFRESH_MIN_INTERVAL_S: Final = 300

# Test seam for the refresh throttle.
_monotonic: Callable[[], float] = time.monotonic


@dataclass(slots=True)
class _OwnerKeyRefreshState:
    """Per-account throttle and version-gap register."""

    cycle_seq: int = 0
    last_claim_cycle: int = 0
    last_claim_at: float = -math.inf
    resolver_last_claim_at: float = -math.inf
    refresh_confirmed: bool = False
    open_gap_versions: set[int] = field(default_factory=set)
    resolved_versions: set[int] = field(default_factory=set)


_owner_key_refresh_state: dict[str, _OwnerKeyRefreshState] = {}


def _scope_name(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def owner_key_scope(cache: object) -> str:
    """Return the account scope of ``cache``: entry id, namespace or object id.

    An empty or blank string counts as missing; never raises.
    """
    for attribute in ("entry_id", "namespace"):
        try:
            name = _scope_name(getattr(cache, attribute, None))
        except Exception:  # noqa: BLE001 - a raising property counts as missing
            name = None
        if name is not None:
            return name
    return f"id:{id(cache)}"


def _state_for(scope: str) -> _OwnerKeyRefreshState:
    state = _owner_key_refresh_state.get(scope)
    if state is None:
        state = _OwnerKeyRefreshState()
        _owner_key_refresh_state[scope] = state
    return state


def _is_version(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def begin_owner_key_poll_cycle(scope: str) -> None:
    """Mark the start of a poll cycle; its first refresh claim is granted."""
    _state_for(scope).cycle_seq += 1


def claim_owner_key_spot_refresh(cache: object) -> bool:
    """Grant a forced owner-key refresh: first claim of a poll cycle, or 300 s.

    A claim is granted when the scope's poll cycle has advanced since the last
    claim, or when 300 s have passed since it; a cycle longer than 300 s can
    therefore grant a second claim.

    Without a preceding ``begin_owner_key_poll_cycle`` (push path, CLI) only the
    clock counts; the first claim of a scope is always granted. A granted claim
    stays unconfirmed until ``confirm_owner_key_spot_refresh`` reports that the
    refresh completed.
    """
    state = _state_for(owner_key_scope(cache))
    now = _monotonic()
    if (
        state.cycle_seq != state.last_claim_cycle
        or now - state.last_claim_at >= OWNER_KEY_SPOT_REFRESH_MIN_INTERVAL_S
    ):
        state.last_claim_cycle = state.cycle_seq
        state.last_claim_at = now
        state.refresh_confirmed = False
        return True
    return False


def confirm_owner_key_spot_refresh(cache: object) -> None:
    """Record that the refresh granted by the last claim completed."""
    _state_for(owner_key_scope(cache)).refresh_confirmed = True


def owner_key_spot_refresh_confirmed(cache: object) -> bool:
    """Return True when the refresh of the current claim window completed.

    False while that refresh is still running or after it failed: a throttled
    caller then has no fresh answer from Spot and must not classify a gap.
    """
    state = _owner_key_refresh_state.get(owner_key_scope(cache))
    return state is not None and state.refresh_confirmed


def claim_owner_key_spot_refresh_by_clock(cache: object) -> bool:
    """Grant the EID resolver's own refresh claim, by the clock alone.

    Touches only ``resolver_last_claim_at``, never the poll-cycle claim.
    """
    state = _state_for(owner_key_scope(cache))
    now = _monotonic()
    if now - state.resolver_last_claim_at >= OWNER_KEY_SPOT_REFRESH_MIN_INTERVAL_S:
        state.resolver_last_claim_at = now
        return True
    return False


def record_owner_key_version_gap(cache: object, version: object) -> None:
    """Add ``version`` to the open gap versions of the cache's scope (idempotent)."""
    if _is_version(version):
        _state_for(owner_key_scope(cache)).open_gap_versions.add(version)


def record_owner_key_version_resolved(cache: object, version: object) -> None:
    """Move ``version`` from open to resolved; a no-op unless it is open."""
    if not _is_version(version):
        return
    state = _owner_key_refresh_state.get(owner_key_scope(cache))
    if state is not None and version in state.open_gap_versions:
        state.open_gap_versions.discard(version)
        state.resolved_versions.add(version)


def consume_resolved_owner_key_versions(scope: str) -> tuple[int, ...]:
    """Return the resolved versions of ``scope`` in order and clear only them."""
    state = _owner_key_refresh_state.get(scope)
    if state is None or not state.resolved_versions:
        return ()
    resolved = tuple(sorted(state.resolved_versions))
    state.resolved_versions.clear()
    return resolved


def open_owner_key_version_gaps(scope: str) -> tuple[int, ...]:
    """Return the open gap versions of ``scope`` in order (read only)."""
    state = _owner_key_refresh_state.get(scope)
    return tuple(sorted(state.open_gap_versions)) if state is not None else ()


def forget_owner_key_refresh_state(scope: str) -> None:
    """Drop the register of ``scope`` (entry unload or removal)."""
    _owner_key_refresh_state.pop(scope, None)


def reset_owner_key_refresh_state() -> None:
    """Clear the whole register (test seam, no production caller)."""
    _owner_key_refresh_state.clear()


# ---------------------------------------------------------------------------
# Legacy sync facade (CLI/offline only)
# ---------------------------------------------------------------------------


def get_owner_key(*, cache: TokenCache) -> OwnerKeyInfo:
    """Synchronous facade for CLI/offline usage; not allowed in the HA event loop.

    Raises:
        RuntimeError: If called from within a running event loop.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(async_get_owner_key(cache=cache))
    raise RuntimeError(
        "Sync get_owner_key() called from the event loop. "
        "Use `await async_get_owner_key()` instead."
    )
