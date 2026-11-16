# custom_components/googlefindmy/phone_identity.py
"""Row id of an Android phone that Google lists without a canonical ID.

Google lists such a phone with its Android ID (``PhoneInformation`` field 1) but no
canonical ID. The Android ID is personal data and must not become a row key,
attribute, ``unique_id`` or log argument, so the row id is a keyed hash of it:
``PHONE_ID_PREFIX`` plus the first ``PHONE_ID_HEX_LEN`` hex characters of
HMAC-SHA-256 over the decimal Android ID. The key is 32 random bytes per config
entry, kept in that entry's token cache (stored as hex). An unkeyed hash would not
do: the Android ID has 62 bits of a known shape, and its hash would be a stable
public handle for the phone.

The same phone therefore has different ids in two accounts, and the same id after a
restart. When the key is lost, the phone gets a new id (a new entity).

This module logs nothing about the key or the Android ID.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Any, Protocol

from .const import PHONE_ID_HEX_LEN, PHONE_ID_KEY_CACHE_NAME, PHONE_ID_PREFIX

_LOGGER = logging.getLogger(__name__)

PHONE_ID_KEY_BYTES = 32


class _KeyCache(Protocol):
    """The part of the entry-scoped token cache this module uses."""

    async def async_get_cached_value(self, key: str) -> Any:
        """Return the cached value of ``key``, or ``None``."""

    async def async_set_cached_value(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``."""


def _decode_stored_key(value: Any) -> bytes | None:
    """Return the stored key, or ``None`` when the value is not 64 hex characters."""
    if not isinstance(value, str) or len(value) != 2 * PHONE_ID_KEY_BYTES:
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


async def async_get_or_create_phone_id_key(cache: _KeyCache) -> bytes:
    """Load this entry's HMAC key from ``cache``, creating and storing it if absent.

    A stored value that is not a 32-byte hex string is replaced by a new key; the
    phones of this entry then get new ids once. Errors of the cache propagate; the
    caller decides (without a key the decoder emits no phone rows).
    """
    stored = await cache.async_get_cached_value(PHONE_ID_KEY_CACHE_NAME)
    key = _decode_stored_key(stored)
    if key is not None:
        return key
    if stored is not None:
        _LOGGER.warning(
            "The stored phone identity key was unreadable and was replaced; phones "
            "listed without a canonical ID get new entities"
        )
    key = secrets.token_bytes(PHONE_ID_KEY_BYTES)
    await cache.async_set_cached_value(PHONE_ID_KEY_CACHE_NAME, key.hex())
    return key


def phone_id_from_android_id(key: bytes, android_id: int) -> str:
    """Return the row id for ``android_id`` under this entry's ``key``."""
    if len(key) != PHONE_ID_KEY_BYTES:
        raise ValueError("phone identity key must be 32 bytes")
    digest = hmac.new(key, str(int(android_id)).encode("ascii"), hashlib.sha256)
    return PHONE_ID_PREFIX + digest.hexdigest()[:PHONE_ID_HEX_LEN]


def is_phone_id(device_id: object) -> bool:
    """Tell whether ``device_id`` is a phone row id (see ``PHONE_ID_PREFIX``)."""
    return isinstance(device_id, str) and device_id.startswith(PHONE_ID_PREFIX)
