"""Typed runtime shims for importing ecdsa primitives without stubs."""

from __future__ import annotations

from typing import Any, Protocol, cast

from ._lazy_crypto import get_curve_fp_class, get_point_class, get_secp160r1_curve


class CurveFpProtocol(Protocol):
    """Subset of ``ecdsa.ellipticcurve.CurveFp`` methods used by the helpers."""

    def p(self) -> int:
        """Return the curve prime."""

    def a(self) -> int:
        """Return the curve ``a`` constant."""

    def b(self) -> int:
        """Return the curve ``b`` constant."""


class CurveParametersProtocol(Protocol):
    """Shape of the SECP160r1 parameters required by the cryptor helpers."""

    curve: CurveFpProtocol
    generator: Any
    order: int


def load_curve() -> CurveParametersProtocol:
    """Load the SECP160r1 curve parameters with lazy imports.

    The import is deferred until first call to improve startup time.
    """

    return cast(CurveParametersProtocol, get_secp160r1_curve())


def load_curve_fp_class() -> type[CurveFpProtocol]:
    """Return the ``CurveFp`` class from ``ecdsa.ellipticcurve``."""

    return cast(type[CurveFpProtocol], get_curve_fp_class())


def load_point_class() -> type:
    """Return the ``Point`` class from ``ecdsa.ellipticcurve``."""

    return get_point_class()
