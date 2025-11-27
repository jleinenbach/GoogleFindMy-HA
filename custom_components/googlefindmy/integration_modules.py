"""Shared integration module constants and lazy import helpers.

Centralizes import targets used by config flow and runtime helpers so import
paths remain consistent if the package layout changes.
"""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

INTEGRATION_PKG = "custom_components.googlefindmy"
INTEGRATION_API_MODULE = f"{INTEGRATION_PKG}.api"


def import_integration_package() -> ModuleType:
    """Return the primary integration package module."""

    return import_module(INTEGRATION_PKG)


def import_integration_api_module() -> ModuleType:
    """Return the lazily loaded integration API module."""

    return import_module(INTEGRATION_API_MODULE)
