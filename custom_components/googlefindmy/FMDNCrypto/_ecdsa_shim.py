"""Typed runtime shims for importing ecdsa primitives without stubs."""

from __future__ import annotations

from typing import Any, Protocol, cast

import ecdsa  # type: ignore[import-untyped]


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
    """Load the SECP160r1 curve parameters with runtime imports.

    The eager module-level import keeps the returned object fully typed while
    matching the access pattern used by the other helpers below.
    """

    return cast(CurveParametersProtocol, ecdsa.SECP160r1)


def load_curve_fp_class() -> type[CurveFpProtocol]:
    """Return the ``CurveFp`` class from ``ecdsa.ellipticcurve``."""

    return cast(type[CurveFpProtocol], ecdsa.ellipticcurve.CurveFp)


def load_point_class() -> type[Any]:
    """Return the ``Point`` class from ``ecdsa.ellipticcurve``."""

    return cast(type[Any], ecdsa.ellipticcurve.Point)
