#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from binascii import unhexlify, Error as BinasciiError
import base64
import re
import logging

from custom_components.googlefindmy.Auth.token_cache import (
    get_cached_value_or_set,
    get_cached_value,
    set_cached_value,
)
from custom_components.googlefindmy.Auth.username_provider import get_username
from custom_components.googlefindmy.KeyBackup.cloud_key_decryptor import decrypt_owner_key
from custom_components.googlefindmy.KeyBackup.shared_key_retrieval import get_shared_key
from custom_components.googlefindmy.SpotApi.GetEidInfoForE2eeDevices.get_eid_info_request import (
    get_eid_info,
    SpotApiEmptyResponseError,
)

_LOGGER = logging.getLogger(__name__)

# Cache key base for owner keys. We migrate from legacy "owner_key" to per-user keys.
_OWNER_KEY_CACHE_PREFIX = "owner_key"


def _user_cache_key(username: str) -> str:
    """Compute per-user cache key for the owner key."""
    return f"{_OWNER_KEY_CACHE_PREFIX}_{username}"


def _retrieve_owner_key(username: str) -> str:
    """
    Retrieve and decrypt the owner key for the given account, and return it as hex string.

    Steps:
    - Call GetEidInfoForE2eeDevices (may raise SpotApiEmptyResponseError if trailers-only/empty).
    - Fetch shared_key (must be non-empty bytes).
    - Decrypt encrypted owner key.
    - Return hex string; do not cache here (caller manages cache).
    """
    try:
        eid_info = get_eid_info()
    except SpotApiEmptyResponseError:
        # Clear, actionable message; actual token invalidation/retry happens in spot_request.
        _LOGGER.error(
            "Owner key retrieval failed: SPOT returned empty/trailers-only body "
            "for GetEidInfoForE2eeDevices (likely auth/session issue). Please re-authenticate."
        )
        raise

    shared_key = get_shared_key()
    if not isinstance(shared_key, (bytes, bytearray)) or not shared_key:
        raise RuntimeError("Shared key is missing or empty; cannot decrypt owner key")

    # Guards for presence and non-empty encrypted owner key
    metadata = getattr(eid_info, "encryptedOwnerKeyAndMetadata", None)
    if metadata is None:
        raise RuntimeError("Missing 'encryptedOwnerKeyAndMetadata' in eid_info")

    encrypted_owner_key = getattr(metadata, "encryptedOwnerKey", b"")
    if not isinstance(encrypted_owner_key, (bytes, bytearray)) or len(encrypted_owner_key) == 0:
        raise RuntimeError("Missing or empty 'encryptedOwnerKey' in eid_info.encryptedOwnerKeyAndMetadata")

    owner_key = decrypt_owner_key(shared_key, encrypted_owner_key)
    owner_key_version = getattr(metadata, "ownerKeyVersion", None)

    if not isinstance(owner_key, (bytes, bytearray)) or len(owner_key) == 0:
        raise RuntimeError("Decrypted owner_key is empty or invalid type")

    _LOGGER.info(
        "Retrieved owner key (version=%s, len=%s) for user=%s",
        owner_key_version,
        len(owner_key),
        username,
    )

    return owner_key.hex()


def _get_or_generate_user_owner_key_hex(username: str) -> str:
    """
    Get user-scoped owner key from cache; migrate from legacy key if present;
    otherwise generate and cache.
    """
    user_key = get_cached_value(_user_cache_key(username))
    if user_key:
        return user_key

    # Legacy migration path: move global 'owner_key' to user-scoped cache if present.
    legacy = get_cached_value(_OWNER_KEY_CACHE_PREFIX)
    if legacy:
        set_cached_value(_user_cache_key(username), legacy)
        _LOGGER.debug("Migrated legacy 'owner_key' to user-scoped cache for %s", username)
        return legacy

    # Generate fresh value and cache under the user-specific key.
    return get_cached_value_or_set(_user_cache_key(username), lambda: _retrieve_owner_key(username))


def get_owner_key() -> bytes:
    """
    Return the binary owner key (bytes) for the current user.

    - Uses a per-user cache key, migrating from the legacy 'owner_key'.
    - Resiliently decodes hex, base64, base64url, and PEM-like formats.
    - Normalizes the cached key to a hex string after a successful non-hex decode.
    - Enforces a 32-byte key length with a clear error message.
    """
    username = get_username()
    raw_value = _get_or_generate_user_owner_key_hex(username)

    def _try_hex(s: str) -> bytes:
        """Attempt to decode a string as hexadecimal."""
        t = s.strip().lower()
        if t.startswith("0x"):
            t = t[2:]
        # Allow whitespace in user-entered values.
        t = re.sub(r"\s+", "", t)
        if not re.fullmatch(r"[0-9a-f]+", t or "00"):
            raise BinasciiError("String contains non-hexadecimal characters.")
        if len(t) % 2:
            t = "0" + t  # Prepend a zero for odd-length strings.
        return unhexlify(t)

    def _try_base64_like(s: str) -> bytes:
        """Attempt to decode a string from various base64-like formats."""
        # Remove PEM-style headers/footers if the user pasted them.
        s = re.sub(r"-{5}BEGIN[^-]+-{5}|-{5}END[^-]+-{5}", "", s)
        s = re.sub(r"\s+", "", s)
        # Add required padding.
        pad = (-len(s)) % 4
        s_padded = s + ("=" * pad)
        try:
            # First, try url-safe base64 which handles '-' and '_'.
            return base64.urlsafe_b64decode(s_padded)
        except (ValueError, TypeError):
            # Fall back to standard base64.
            return base64.b64decode(s_padded)

    # 1) Fast path: attempt to decode as hex, as is the standard format.
    try:
        key_bytes = _try_hex(raw_value)
    except (BinasciiError, TypeError):
        # 2) Fallback: attempt to decode as base64/base64url/PEM-like.
        try:
            key_bytes = _try_base64_like(raw_value)
        except Exception as exc:
            _LOGGER.error(
                "Owner key for user '%s' is not valid hex or base64/base64url. "
                "Please store the key as a 64-char hex string (32 bytes). Error: %s",
                username,
                exc,
            )
            # Clear cache to prevent repeated failures on the same invalid data.
            set_cached_value(_user_cache_key(username), None)
            set_cached_value(_OWNER_KEY_CACHE_PREFIX, None)
            raise RuntimeError(
                "Invalid owner_key format (expect 32-byte key in hex or base64)."
            ) from exc
        else:
            # Self-heal: normalize the cache to hex for future consistency.
            _LOGGER.info(
                "Successfully decoded owner key from a non-hex format; normalizing cache to hex."
            )
            set_cached_value(_user_cache_key(username), key_bytes.hex())

    # 3) Final validation: the owner key must be exactly 32 bytes long.
    if len(key_bytes) != 32:
        _LOGGER.error(
            "Owner key for user '%s' has an invalid length: %d bytes (expected 32). "
            "Clear credentials and re-authenticate if this persists.",
            username,
            len(key_bytes),
        )
        # Clear cache to prevent repeated failures on the same invalid data.
        set_cached_value(_user_cache_key(username), None)
        set_cached_value(_OWNER_KEY_CACHE_PREFIX, None)
        raise RuntimeError("Owner key must be exactly 32 bytes long.")

    return key_bytes


if __name__ == "__main__":
    print(get_owner_key())
