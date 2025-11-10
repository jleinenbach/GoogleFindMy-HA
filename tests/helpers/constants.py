# tests/helpers/constants.py
"""Shared constants helpers for Google Find My tests."""

from __future__ import annotations

from functools import lru_cache
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

__all__ = ["load_googlefindmy_const_module", "get_googlefindmy_constant"]


def _const_module_path() -> Path:
    """Return the filesystem path to the integration's const module."""

    return (
        Path(__file__)
        .resolve()
        .parents[2]
        / "custom_components"
        / "googlefindmy"
        / "const.py"
    )


@lru_cache(maxsize=1)
def load_googlefindmy_const_module() -> ModuleType:
    """Load and cache the integration's const module without Home Assistant."""

    module_path = _const_module_path()
    spec = spec_from_file_location("tests._googlefindmy_const", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load googlefindmy const module")

    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_googlefindmy_constant(name: str) -> object:
    """Return a constant exported by the integration's const module."""

    module = load_googlefindmy_const_module()
    try:
        return getattr(module, name)
    except AttributeError as exc:  # pragma: no cover - defensive guard
        raise AttributeError(f"Unknown googlefindmy constant: {name}") from exc
