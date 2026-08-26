# tests/helpers/constants.py
"""Shared constants helpers for Google Find My tests."""

from __future__ import annotations

import sys
from functools import lru_cache
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import ModuleType

__all__ = ["load_googlefindmy_const_module", "get_googlefindmy_constant"]


def _const_module_path() -> Path:
    """Return the filesystem path to the integration's const module."""

    return (
        Path(__file__).resolve().parents[2]
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
    # Register before exec_module, as the importlib recipe for "importing a source
    # file directly" prescribes. Skipping this half of the recipe worked only as
    # long as const.py contained nothing that resolves its own annotations at
    # class-creation time. @dataclass does: with `from __future__ import
    # annotations` every field annotation is a string, and dataclasses resolves it
    # via sys.modules[cls.__module__] to tell a KW_ONLY/ClassVar marker from a real
    # field. Unregistered, that lookup yields None and the module fails to import
    # with `AttributeError: 'NoneType' object has no attribute '__dict__'`, which
    # this conftest turns into a collection error for the entire test suite.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # Do not leave a half-initialised module behind for the next importer.
        sys.modules.pop(spec.name, None)
        raise
    return module


def get_googlefindmy_constant(name: str) -> object:
    """Return a constant exported by the integration's const module."""

    module = load_googlefindmy_const_module()
    try:
        return getattr(module, name)
    except AttributeError as exc:  # pragma: no cover - defensive guard
        raise AttributeError(f"Unknown googlefindmy constant: {name}") from exc
