"""Import smoke test: every module in the package can be imported.

This test catches errors that linters (ruff) and type checkers (mypy) miss:
- Broken relative imports after refactoring
- Circular import chains
- Protobuf descriptor pool conflicts (cf. upstream #144)
- Missing optional dependencies at module level
- Module-level NameErrors

The test uses pkgutil.walk_packages to discover ALL .py modules
recursively and attempts to import each one.  Runtime cost is ~2 s
because most modules are already cached after conftest.py runs.
"""

from __future__ import annotations

import importlib
import pkgutil

import pytest

import custom_components.googlefindmy as _pkg

# Discover every module in the package tree once at collection time.
_ALL_MODULES: list[str] = [
    mod_info.name
    for mod_info in pkgutil.walk_packages(_pkg.__path__, prefix=_pkg.__name__ + ".")
]


@pytest.mark.parametrize("module_name", _ALL_MODULES)
def test_module_importable(module_name: str) -> None:
    """Every module in custom_components.googlefindmy must be importable."""
    mod = importlib.import_module(module_name)
    assert mod is not None
