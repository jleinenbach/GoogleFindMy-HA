# custom_components/googlefindmy/FMDNCrypto/_lazy_crypto.py
"""Lazy-loading wrappers for heavy cryptography dependencies.

This module provides cached factory functions that defer the import of
expensive cryptography libraries (cryptography, ecdsa, Cryptodome) until
they are actually needed. This improves integration startup time by avoiding
loading crypto modules during Home Assistant initialization.

Usage:
    from ._lazy_crypto import get_aesgcm_class, get_aes_cipher

    # Instead of: from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    AESGCM = get_aesgcm_class()
    cipher = AESGCM(key)
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM


# -----------------------------------------------------------------------------
# cryptography library lazy loaders
# -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_aesgcm_class() -> type[AESGCM]:
    """Lazily load and cache the AESGCM class from cryptography."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

    return AESGCM


@lru_cache(maxsize=1)
def get_cipher_class() -> Any:
    """Lazily load and cache the Cipher class from cryptography."""
    from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: PLC0415

    return Cipher


@lru_cache(maxsize=1)
def get_algorithms_module() -> Any:
    """Lazily load and cache the algorithms module from cryptography."""
    from cryptography.hazmat.primitives.ciphers import algorithms  # noqa: PLC0415

    return algorithms


@lru_cache(maxsize=1)
def get_modes_module() -> Any:
    """Lazily load and cache the modes module from cryptography."""
    from cryptography.hazmat.primitives.ciphers import modes  # noqa: PLC0415

    return modes


@lru_cache(maxsize=1)
def get_ec_module() -> Any:
    """Lazily load and cache the ec module from cryptography."""
    from cryptography.hazmat.primitives.asymmetric import ec  # noqa: PLC0415

    return ec


@lru_cache(maxsize=1)
def get_p256_curve() -> Any:
    """Lazily load and cache the P-256 curve instance."""
    ec = get_ec_module()
    return ec.SECP256R1()


@lru_cache(maxsize=1)
def get_invalid_tag_exception() -> type[Exception]:
    """Lazily load and cache the InvalidTag exception class."""
    from cryptography.exceptions import InvalidTag  # noqa: PLC0415

    return InvalidTag


# -----------------------------------------------------------------------------
# ecdsa library lazy loaders
# -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_ecdsa_module() -> Any:
    """Lazily load and cache the ecdsa module."""
    import ecdsa  # noqa: PLC0415

    return ecdsa


@lru_cache(maxsize=1)
def get_secp160r1_curve() -> Any:
    """Lazily load and cache the SECP160r1 curve parameters."""
    ecdsa = get_ecdsa_module()
    return ecdsa.SECP160r1


@lru_cache(maxsize=1)
def get_curve_fp_class() -> type:
    """Lazily load and cache the CurveFp class from ecdsa."""
    ecdsa = get_ecdsa_module()
    return ecdsa.ellipticcurve.CurveFp  # type: ignore[no-any-return]


@lru_cache(maxsize=1)
def get_point_class() -> type:
    """Lazily load and cache the Point class from ecdsa."""
    ecdsa = get_ecdsa_module()
    return ecdsa.ellipticcurve.Point  # type: ignore[no-any-return]


# -----------------------------------------------------------------------------
# Cryptodome library lazy loaders
# -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_aes_class() -> Any:
    """Lazily load and cache the AES class from Cryptodome."""
    from Cryptodome.Cipher import AES  # noqa: PLC0415

    return AES


# -----------------------------------------------------------------------------
# cryptography HKDF lazy loaders
# -----------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_hashes_module() -> Any:
    """Lazily load and cache the hashes module from cryptography."""
    from cryptography.hazmat.primitives import hashes  # noqa: PLC0415

    return hashes


@lru_cache(maxsize=1)
def get_hkdf_class() -> Any:
    """Lazily load and cache the HKDF class from cryptography."""
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # noqa: PLC0415

    return HKDF
