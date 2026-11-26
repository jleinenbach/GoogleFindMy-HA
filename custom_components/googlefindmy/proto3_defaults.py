# custom_components/googlefindmy/proto3_defaults.py
"""Recognize proto3 default values that stand for an unset field.

proto3 scalar and bytes fields have no presence: an unset int32 reads as 0 and
an unset bytes field reads as b"". A check such as ``value is not None`` takes
that default for a real value. This module is the single definition of such
defaults for the keys that carry time anchors, the owner-key version and key
blobs, so producers and the location cache agree on what "no value" means.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

# Epoch-second anchors; readers already treat values <= 0 as invalid.
_ANCHOR_KEYS: Final[frozenset[str]] = frozenset(
    {
        "pair_date",
        "pairDate",
        "secrets_creation_date",
        "secretsCreationDate",
        "encrypted_user_secrets_creation_date",
        "encryptedUserSecretsCreationDate",
    }
)
# Owner-key versions start at 1 (``_is_version`` in get_owner_key.py).
_VERSION_KEYS: Final[frozenset[str]] = frozenset(
    {"owner_key_version", "ownerKeyVersion"}
)
_NUMBER_KEYS: Final[frozenset[str]] = _ANCHOR_KEYS | _VERSION_KEYS
_BLOB_KEYS: Final[frozenset[str]] = frozenset(
    {
        "identity_key",
        "identityKey",
        "eik",
        "encrypted_identity_key",
        "encryptedIdentityKey",
        "encrypted_account_key",
        "encryptedAccountKey",
    }
)


def is_unset_number(value: object) -> bool:
    """Return True for an int (not bool) <= 0, the default of an unset field."""
    return isinstance(value, int) and not isinstance(value, bool) and value <= 0


def is_unset_blob(value: object) -> bool:
    """Return True for b"" or "", the default of an unset bytes field."""
    return isinstance(value, (bytes, str)) and len(value) == 0


def proto3_number_or_none(value: object) -> int | None:
    """Return ``value`` if it is a set int field, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or is_unset_number(value):
        return None
    return value


def is_proto3_default(key: str, value: object) -> bool:
    """Return True if ``value`` is the proto3 default for the field behind ``key``."""
    if key in _NUMBER_KEYS:
        return is_unset_number(value)
    if key in _BLOB_KEYS:
        return is_unset_blob(value)
    return False


def drop_proto3_defaults(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``mapping`` without entries that hold a proto3 default."""
    return {
        key: value
        for key, value in mapping.items()
        if not is_proto3_default(key, value)
    }
