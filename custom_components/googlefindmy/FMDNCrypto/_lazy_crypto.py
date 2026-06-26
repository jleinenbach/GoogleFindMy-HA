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
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
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


# -----------------------------------------------------------------------------
# ECDSA big-integer backend visibility (diagnostics only, no behavior change)
# -----------------------------------------------------------------------------


def _dist_version_or_none(name: str) -> str | None:
    """Return the installed distribution version of ``name`` or ``None``.

    Reads dist-info metadata via ``importlib.metadata.version``; this does
    NOT import the module (side-effect-free) and raises
    ``PackageNotFoundError`` when the distribution is absent. The except
    branch is reachable and exercised by the test suite (it is the
    not-installed case), so it carries no ``# pragma: no cover``.
    """
    try:
        return version(name)
    except PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def get_ecdsa_acceleration_info() -> dict[str, str | None]:
    """Report which big-int backend python-ecdsa would use, plus installed versions.

    python-ecdsa speeds up the modular arithmetic of the legacy SECP160r1 EID
    path automatically when ``gmpy2`` (preferred) or ``gmpy`` is importable;
    otherwise it falls back to pure Python. This helper exposes that fact for
    diagnostics WITHOUT changing any crypto behavior.

    The returned keys are always present and deterministic:

        {
            "ecdsa_acceleration": "gmpy2" | "gmpy" | "pure-python",
            "gmpy2_version": str | None,
            "gmpy_version":  str | None,
            "ecdsa_version": str | None,
        }

    Honesty note (mirrors ``Auth/gpsoauth_loader._gpsoauth_available``):
    ``find_spec``/``importlib.metadata.version`` report import availability and
    installed distribution metadata, NOT real import success. A broken install
    (missing libgmp, ABI break) still yields a spec and a metadata version while
    python-ecdsa falls back to pure-python at runtime. The only claim made here
    is "importable / installed in version X" (what python-ecdsa *would* use),
    never "acceleration active". Logging the version is precisely what lets such
    a defect be attributed after the fact.

    Performance note (event loop): this function performs filesystem I/O
    (``find_spec`` plus a dist-info ``METADATA`` read via
    ``importlib.metadata.version``). The ``@lru_cache`` only defers that cost to
    the first caller, so event-loop code MUST materialize it via
    ``hass.async_add_executor_job(get_ecdsa_acceleration_info)`` rather than
    calling it synchronously. The function itself stays synchronous; the
    executor responsibility lies with the caller.

    Mutation note: callers MUST NOT mutate the cached dict; copy it first (the
    diagnostics consumer returns a shallow copy).
    """
    if find_spec("gmpy2") is not None:
        backend = "gmpy2"
    elif find_spec("gmpy") is not None:
        backend = "gmpy"
    else:
        backend = "pure-python"
    return {
        "ecdsa_acceleration": backend,
        "gmpy2_version": _dist_version_or_none("gmpy2"),
        "gmpy_version": _dist_version_or_none("gmpy"),
        "ecdsa_version": _dist_version_or_none("ecdsa"),
    }
