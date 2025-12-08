# custom_components/googlefindmy/FMDNCrypto/key_derivation.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import logging

from custom_components.googlefindmy.FMDNCrypto.sha import calculate_truncated_sha256

_LOGGER = logging.getLogger(__name__)


class FMDNOwnerOperations:
    """Owner-only key derivations defined by the FHN Accessory spec.

    Per the FHN Accessory Specification v1.3 Authentication section, the
    ephemeral identity key is the root material for three owner operations:
    recovery, ring, and unwanted tracking protection. Each operation derives
    an 8-byte key from ``SHA256(EIK || suffix_byte)`` where the suffix encodes
    the operation being authorized.
    """

    def __init__(self) -> None:
        self.recovery_key: bytes | None = None
        self.ringing_key: bytes | None = None
        self.tracking_key: bytes | None = None

    def generate_keys(self, identity_key: bytes) -> None:
        """Derive owner operation keys per FHN Accessory Specification v1.3.

        FHN Accessory Specification v1.3 — Authentication section:

        * ``0x01`` suffix → recovery key = first 8 bytes of
          ``SHA256(ephemeral identity key || 0x01)``.
        * ``0x02`` suffix → ring key = first 8 bytes of
          ``SHA256(ephemeral identity key || 0x02)``.
        * ``0x03`` suffix → unwanted tracking protection key = first 8 bytes of
          ``SHA256(ephemeral identity key || 0x03)``.

        The suffix mapping and truncation above mirror the specification’s
        terminology so the derived keys align with the authenticated operations
        defined for owners.
        """
        if not isinstance(identity_key, (bytes, bytearray, memoryview)):
            msg = (
                "Identity key must be a bytes-like object, got "
                f"{type(identity_key).__name__}"
            )
            raise TypeError(msg)

        identity_key_bytes = bytes(identity_key)

        try:
            self.recovery_key = calculate_truncated_sha256(
                identity_key_bytes, 0x01
            )
            self.ringing_key = calculate_truncated_sha256(
                identity_key_bytes, 0x02
            )
            self.tracking_key = calculate_truncated_sha256(
                identity_key_bytes, 0x03
            )

        except Exception:  # noqa: BLE001 - log and propagate default state
            self.recovery_key = None
            self.ringing_key = None
            self.tracking_key = None
            _LOGGER.exception("Failed to derive owner operation keys")
