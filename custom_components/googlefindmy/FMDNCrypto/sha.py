# custom_components/googlefindmy/FMDNCrypto/sha.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
SHA-256 and HMAC-SHA-256 utilities for FMDN cryptographic operations.

Domain Separation Pattern
-------------------------
Cryptographic domain separation prevents key/hash collisions across different
protocol operations. By appending a unique operation byte before hashing:

    hash_input = identity_key || operation_byte

Each operation type produces a unique hash even with identical keys. This is
a fundamental security pattern used throughout FMDN to ensure that keys
derived for one purpose cannot be confused with keys for another purpose.

Operation Values (FMDN Protocol)
--------------------------------
Different operation bytes are used for different protocol functions:
- 0x01: Owner verification
- 0x02: Location report signing
- Other values: Reserved for future protocol extensions

HMAC-SHA-256 (RFC 2104)
-----------------------
HMAC provides authenticated message integrity:

    HMAC(K, m) = H((K' ⊕ opad) || H((K' ⊕ ipad) || m))

Where:
- K' is K padded/hashed to block size (64 bytes for SHA-256)
- ipad = 0x36 repeated 64 times (inner padding)
- opad = 0x5C repeated 64 times (outer padding)
- H is the underlying hash function (SHA-256)

The nested hashing structure ensures that the MAC is secure even if the
underlying hash has certain weaknesses.
"""

import hashlib
import hmac


def calculate_truncated_sha256(identity_key: bytes, operation: int) -> bytes:
    """Compute a truncated SHA-256 hash for domain-separated identity operations.

    Mathematical Construction
    -------------------------
    The hash is computed as:

        H = SHA-256(identity_key || operation_byte)
        result = H[0:8]  (first 64 bits)

    Where || denotes byte concatenation.

    Truncation Rationale
    --------------------
    The 8-byte (64-bit) truncation provides:
    - Sufficient collision resistance for the protocol's security requirements
    - Compact representation for bandwidth-constrained beacon transmissions
    - Birthday attack resistance of 2^32 operations (acceptable for this use case)

    Domain Separation
    -----------------
    The operation byte ensures different protocol operations produce different
    hashes even with identical identity keys:

        operation=0x01: H₁ = SHA-256(key || 0x01)  → owner verification
        operation=0x02: H₂ = SHA-256(key || 0x02)  → location signing

    This prevents cross-protocol attacks where a hash computed for one purpose
    could be replayed in a different context.

    Args:
        identity_key: The identity key material (typically 20 or 32 bytes).
        operation: Single-byte operation identifier for domain separation.

    Returns:
        First 8 bytes of the SHA-256 hash (64-bit truncated hash).

    Example:
        >>> identity_key = bytes.fromhex("0123456789abcdef0123456789abcdef01234567")
        >>> truncated = calculate_truncated_sha256(identity_key, operation=0x01)
        >>> len(truncated)
        8
    """
    identity_key_bytes = identity_key
    data = identity_key_bytes + bytes([operation])

    sha256_hash = hashlib.sha256(data).digest()
    truncated_hash = sha256_hash[:8]

    return truncated_hash


def calculate_hmac_sha256(key: bytes | bytearray, message: bytes | bytearray) -> str:
    """Compute HMAC-SHA-256 and return the hexadecimal digest.

    HMAC Construction (RFC 2104)
    ----------------------------
    HMAC-SHA-256 uses a nested hash construction:

        HMAC(K, m) = SHA-256((K' ⊕ opad) || SHA-256((K' ⊕ ipad) || m))

    Security Properties
    -------------------
    1. **Unforgeability**: Without knowing K, an attacker cannot produce a
       valid MAC for any message (even after seeing MACs for other messages).

    2. **Key Extraction Resistance**: The MAC output does not reveal
       information about the secret key K.

    3. **Collision Resistance**: Finding two messages with the same MAC
       requires approximately 2^128 operations.

    Use Cases in FMDN
    -----------------
    - Authentication of location reports
    - Verification of ownership claims
    - Integrity protection for encrypted payloads

    Args:
        key: Secret key material (any length; will be hashed if > 64 bytes).
        message: Message bytes to authenticate.

    Returns:
        Hexadecimal string of the 32-byte (256-bit) HMAC digest.

    Example:
        >>> key = b"secret_key"
        >>> message = b"data to authenticate"
        >>> mac = calculate_hmac_sha256(key, message)
        >>> len(mac)  # 64 hex chars = 32 bytes
        64
    """
    hmac_obj = hmac.new(key, message, hashlib.sha256)
    return hmac_obj.hexdigest()
