# tests/helpers/homeassistant_stub.py
"""Shared Home Assistant stub helpers for the test suite."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import ModuleType
from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:  # pragma: no cover - imported for type checking only
    from pytest import MonkeyPatch

__all__ = [
    "install_homeassistant_core_callback_stub",
    "install_homeassistant_network_stub",
]

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


def install_homeassistant_network_stub(
    monkeypatch: MonkeyPatch | None = None,
    *,
    get_url_result: str | None = "https://example.local",
    get_url_error: Exception | type[Exception] | None = None,
) -> ModuleType:
    """Install a stubbed ``homeassistant.helpers.network`` module.

    The helper exposes ``get_url`` with controllable behaviour and installs a
    fallback ``NoURLAvailableError`` when Home Assistant's network helpers are
    unavailable. Use it in tests that need to exercise URL resolution without
    pulling in Home Assistant's full HTTP stack.
    """

    module_name = "homeassistant.helpers.network"
    resolved_module = sys.modules.get(module_name)
    if resolved_module is None:
        resolved_module = ModuleType(module_name)

    if not hasattr(resolved_module, "NoURLAvailableError"):
        resolved_module.NoURLAvailableError = type(  # type: ignore[attr-defined]
            "NoURLAvailableError",
            (Exception,),
            {},
        )

    def _get_url(*_args: Any, **_kwargs: Any) -> str | None:
        if get_url_error is not None:
            if isinstance(get_url_error, Exception):
                raise get_url_error
            raise get_url_error()
        return get_url_result

    if monkeypatch is not None:
        monkeypatch.setitem(sys.modules, module_name, resolved_module)
        monkeypatch.setattr(resolved_module, "get_url", _get_url, raising=False)
    else:  # pragma: no cover - convenience fallback
        sys.modules[module_name] = resolved_module
        setattr(resolved_module, "get_url", _get_url)

    return resolved_module
