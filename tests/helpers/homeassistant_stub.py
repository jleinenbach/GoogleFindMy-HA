# tests/helpers/homeassistant_stub.py
"""Shared Home Assistant stub helpers for the test suite."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any, Callable, TypeVar

if TYPE_CHECKING:  # pragma: no cover - imported for type checking only
    from pytest import MonkeyPatch

__all__ = ["install_homeassistant_core_callback_stub"]

_CallbackT = TypeVar("_CallbackT", bound=Callable[..., Any])


def _identity_callback(func: _CallbackT) -> _CallbackT:
    """Return ``func`` unchanged, mirroring Home Assistant's decorator."""

    return func


def install_homeassistant_core_callback_stub(
    monkeypatch: MonkeyPatch | None = None,
    *,
    module: ModuleType | None = None,
    overwrite: bool = False,
) -> ModuleType:
    """Ensure :mod:`homeassistant.core` exposes the ``callback`` decorator.

    Parameters
    ----------
    monkeypatch:
        Optional pytest monkeypatch fixture. When provided, the helper registers
        the stubbed module and attribute through ``monkeypatch`` so pytest rolls
        the changes back after the test finishes.
    module:
        Existing module instance to install the stub on. When omitted, the
        helper retrieves (or creates) ``homeassistant.core`` from
        :data:`sys.modules`.
    overwrite:
        Replace any existing ``callback`` attribute even if one is already
        present on the resolved module.

    Returns
    -------
    ModuleType
        The module hosting the stubbed ``callback`` decorator.
    """

    module_name = "homeassistant.core"
    resolved_module = module or sys.modules.get(module_name)
    if resolved_module is None:
        resolved_module = ModuleType(module_name)

    if monkeypatch is not None:
        monkeypatch.setitem(sys.modules, module_name, resolved_module)
    else:
        sys.modules[module_name] = resolved_module

    has_callback = hasattr(resolved_module, "callback")
    if overwrite or not has_callback:
        if monkeypatch is not None:
            monkeypatch.setattr(
                resolved_module,
                "callback",
                _identity_callback,
                raising=False,
            )
        else:
            setattr(resolved_module, "callback", _identity_callback)

    return resolved_module
