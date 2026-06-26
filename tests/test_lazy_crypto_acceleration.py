# tests/test_lazy_crypto_acceleration.py
"""Tests for the ECDSA acceleration visibility helper (no crypto behavior change).

Covers ``get_ecdsa_acceleration_info`` (backend detection via ``find_spec`` and
installed-version reporting via ``importlib.metadata.version``) plus the
``_dist_version_or_none`` helper, including the reachable ``PackageNotFoundError``
branch. All assertions are deterministic and environment-independent through
monkeypatching; mutation counter-checks are documented inline.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

from custom_components.googlefindmy.FMDNCrypto import _lazy_crypto


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    """Reset the lru_cache around every case (the SSOT is process-cached)."""
    _lazy_crypto.get_ecdsa_acceleration_info.cache_clear()
    yield
    _lazy_crypto.get_ecdsa_acceleration_info.cache_clear()


def _patch_find_spec(monkeypatch: pytest.MonkeyPatch, present: set[str]) -> None:
    """Make ``find_spec`` report only the modules in ``present`` as importable."""

    def fake_find_spec(name: str) -> object | None:
        return object() if name in present else None

    monkeypatch.setattr(_lazy_crypto, "find_spec", fake_find_spec)


def test_backend_gmpy2_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, {"gmpy2", "gmpy"})
    info = _lazy_crypto.get_ecdsa_acceleration_info()
    assert info["ecdsa_acceleration"] == "gmpy2"
    # Mutation counter-check: swapping the gmpy2/gmpy order would read "gmpy" here.


def test_backend_gmpy_when_only_gmpy(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, {"gmpy"})
    info = _lazy_crypto.get_ecdsa_acceleration_info()
    assert info["ecdsa_acceleration"] == "gmpy"


def test_backend_pure_python_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, set())
    info = _lazy_crypto.get_ecdsa_acceleration_info()
    assert info["ecdsa_acceleration"] == "pure-python"


def test_detection_is_side_effect_free(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, set())
    _lazy_crypto.get_ecdsa_acceleration_info()
    assert "gmpy2" not in sys.modules
    assert "gmpy" not in sys.modules


def test_versions_reported_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, {"gmpy2"})

    def fake_version(name: str) -> str:
        return {"gmpy2": "9.9.9-test", "gmpy": "8.8.8-test", "ecdsa": "0.19.1"}[name]

    monkeypatch.setattr(_lazy_crypto, "version", fake_version)
    info = _lazy_crypto.get_ecdsa_acceleration_info()
    assert info["gmpy2_version"] == "9.9.9-test"
    assert info["ecdsa_version"] == "0.19.1"


def test_missing_distribution_yields_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, set())

    def fake_version(name: str) -> str:
        raise _lazy_crypto.PackageNotFoundError(name)

    monkeypatch.setattr(_lazy_crypto, "version", fake_version)
    info = _lazy_crypto.get_ecdsa_acceleration_info()
    assert info["gmpy2_version"] is None
    assert info["gmpy_version"] is None
    assert info["ecdsa_version"] is None
    # Exercises the `except PackageNotFoundError: return None` branch of
    # _dist_version_or_none. Mutation counter-check: removing that except (or
    # returning a literal there) makes this assertion red.


def test_keys_are_deterministic_and_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_find_spec(monkeypatch, set())
    info = _lazy_crypto.get_ecdsa_acceleration_info()
    assert set(info) == {
        "ecdsa_acceleration",
        "gmpy2_version",
        "gmpy_version",
        "ecdsa_version",
    }
