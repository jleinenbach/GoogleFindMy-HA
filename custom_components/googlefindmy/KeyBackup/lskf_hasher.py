# custom_components/googlefindmy/KeyBackup/lskf_hasher.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
LSKF (Lock Screen Knowledge Factor) hashing for Google Find My Device key backup.

Overview
--------
The Lock Screen Knowledge Factor (LSKF) is typically a PIN, password, or pattern
used to unlock an Android device. This module derives cryptographic keys from the
LSKF using Scrypt, a memory-hard key derivation function designed to be resistant
to hardware brute-force attacks.

Key Backup Chain
----------------
The LSKF hash is the root of a key derivation chain:

    LSKF (PIN/Password)
         │
         ▼ Scrypt
    LSKF Hash (32 bytes)
         │
         ▼ SHA-256
    Recovery Key Decryption Key
         │
         ▼ AES-GCM
    Application Key → Security Domain Key → Shared Key → Owner Key → EIK

Scrypt Algorithm (RFC 7914)
---------------------------
Scrypt is designed to be memory-hard, making GPU/ASIC attacks expensive:

    Scrypt(P, S, N, r, p, dkLen) → DK

Where:
- P: Password (the LSKF)
- S: Salt (random bytes stored alongside encrypted data)
- N: CPU/memory cost parameter (must be power of 2)
- r: Block size parameter
- p: Parallelization parameter
- dkLen: Desired key length

Memory requirements: 128 × N × r bytes

Security Considerations
-----------------------
- Scrypt's memory-hardness makes it expensive to parallelize
- The salt prevents rainbow table attacks
- Parameters should be tuned for target hardware (higher N = more secure but slower)
"""

from __future__ import annotations

import hashlib
import time
from binascii import unhexlify
from concurrent.futures import ProcessPoolExecutor

import pyscrypt
from custom_components.googlefindmy.example_data_provider import get_example_data


def ascii_to_bytes(string: str) -> bytes:
    """Return the ASCII-encoded representation of ``string``."""

    return string.encode("ascii")


def get_lskf_hash(pin: str, salt: bytes) -> bytes:
    """Derive a 32-byte key from a PIN/password using Scrypt.

    Scrypt Algorithm Details (RFC 7914)
    -----------------------------------
    Scrypt is a password-based key derivation function specifically designed
    to make brute-force attacks expensive by requiring large amounts of memory.

    The algorithm works in two main phases:

    1. **ROMix Phase**: Fills a large memory buffer with pseudorandom data
       derived from the password. This phase requires sequential memory access,
       making it hard to parallelize.

    2. **BlockMix Phase**: Uses Salsa20/8 core to mix blocks of data. The
       number of iterations depends on the block size parameter (r).

    Parameters Used
    ---------------
    - N = 4096 (2^12): CPU/memory cost. Memory used = 128 × N × r = 4 MB
    - r = 8: Block size. Larger r increases memory per thread.
    - p = 1: Parallelization. Set to 1 for single-threaded operation.
    - dkLen = 32: Output key length (256 bits for AES-256 compatibility).

    Memory-Hardness Rationale
    -------------------------
    The memory requirement of 128 × N × r bytes (≈4 MB with current params):
    - Prevents GPU attacks (GPUs have limited per-thread memory)
    - Makes ASIC implementation expensive (requires significant RAM)
    - Each password guess requires this memory, limiting parallel guesses

    Time-Memory Tradeoff Resistance
    -------------------------------
    Scrypt's design makes time-memory tradeoffs unfavorable. Reducing memory
    by factor k increases computation by factor k², making shortcuts unprofitable.

    Args:
        pin: The user's PIN or password as a string.
        salt: Random salt bytes (typically 16-32 bytes).

    Returns:
        32-byte derived key suitable for AES-256 or further key derivation.

    Example:
        >>> salt = bytes.fromhex("0123456789abcdef0123456789abcdef")
        >>> key = get_lskf_hash("1234", salt)
        >>> len(key)
        32

    References:
        - RFC 7914: The scrypt Password-Based Key Derivation Function
        - https://www.tarsnap.com/scrypt.html (original paper)
    """
    # Parameters
    data_to_hash = ascii_to_bytes(pin)  # Convert the string to an ASCII byte array

    log_n_cost = 4096  # CPU/memory cost parameter
    block_size = 8  # Block size
    parallelization = 1  # Parallelization factor
    key_length = 32  # Length of the derived key in bytes

    # Perform Scrypt hashing
    hashed = pyscrypt.hash(
        password=data_to_hash,
        salt=salt,
        N=log_n_cost,
        r=block_size,
        p=parallelization,
        dkLen=key_length,
    )

    return hashed


def hash_pin(pin: str) -> tuple[str, str]:
    """Return the original ``pin`` together with its LSKF SHA-256 hash."""

    sample_pin_salt = unhexlify(get_example_data("sample_pin_salt"))

    hash_input = get_lskf_hash(pin, sample_pin_salt)
    if not isinstance(hash_input, bytes):  # Safety net for unexpected library changes.
        msg = "get_lskf_hash must return bytes"
        raise TypeError(msg)

    hash_object = hashlib.sha256(hash_input)
    hash_hex = hash_object.hexdigest()

    print(f"PIN: {pin}, Hash: {hash_hex}")
    return pin, hash_hex


if __name__ == "__main__":
    start_time = time.time()
    pins = [f"{i:04d}" for i in range(10000)]

    with ProcessPoolExecutor() as executor:
        results = list(executor.map(hash_pin, pins))

    for pin, hashed in results:
        print(f"PIN: {pin}, Hash: {hashed}")

    end_time = time.time()
    elapsed_time = end_time - start_time
    print(f"Time taken: {elapsed_time:.2f} seconds")
