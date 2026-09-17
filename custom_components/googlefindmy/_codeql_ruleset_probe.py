"""Temporary probe for the `main` ruleset: introduces two CodeQL error-level
findings ON PURPOSE (py/insecure-temporary-file, py/weak-crypto-key) to measure
that the `code_scanning` rule blocks the merge. Never merge this file.
"""

from __future__ import annotations

import tempfile

from cryptography.hazmat.primitives.asymmetric import rsa


def probe_weak_key() -> rsa.RSAPrivateKey:
    """Return a deliberately weak RSA key (CodeQL py/weak-crypto-key)."""
    return rsa.generate_private_key(public_exponent=65537, key_size=1024)  # nosec B505


def probe_insecure_tmp() -> str:
    """Return a racy temp path (CodeQL py/insecure-temporary-file)."""
    return tempfile.mktemp()  # noqa: S306  # nosec B306
