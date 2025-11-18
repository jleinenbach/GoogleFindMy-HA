from __future__ import annotations

"""Typed runtime shims for importing ecdsa primitives without stubs."""

from importlib import import_module
from typing import Any, Protocol, cast


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

    Using ``import_module`` sidesteps mypy's missing-stub warnings while keeping
    the returned object fully typed via the Protocol above.
    """

    return cast(CurveParametersProtocol, getattr(import_module("ecdsa"), "SECP160r1"))


def load_curve_fp_class() -> type[CurveFpProtocol]:
    """Return the ``CurveFp`` class from ``ecdsa.ellipticcurve``."""

    return cast(
        type[CurveFpProtocol], getattr(import_module("ecdsa.ellipticcurve"), "CurveFp")
    )


def load_point_class() -> type[Any]:
    """Return the ``Point`` class from ``ecdsa.ellipticcurve``."""

    return cast(type[Any], getattr(import_module("ecdsa.ellipticcurve"), "Point"))
