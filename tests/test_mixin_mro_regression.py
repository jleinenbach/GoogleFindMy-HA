# tests/test_mixin_mro_regression.py
"""Regression tests for MRO shadowing bug in _MixinBase (commit 90bf146).

Commit 90bf146 introduced _MixinBase with stub methods that raised
NotImplementedError for async_set_updated_data, async_request_refresh,
and async_set_update_error.  Because _MixinBase precedes
DataUpdateCoordinator in the C3 MRO of GoogleFindMyCoordinator, these
stubs shadowed the real implementations and broke all coordinator
updates at runtime.

These tests use AST/source analysis (no HA runtime imports needed) to
ensure:
1. _MixinBase does NOT define the three DataUpdateCoordinator methods
   at runtime (they must be guarded by TYPE_CHECKING).
2. GoogleFindMyCoordinator's inheritance order keeps _MixinBase before
   DataUpdateCoordinator, making the guard essential.
3. No _MixinBase method stub shadows any DataUpdateCoordinator method.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# Paths to the source files under test
_COORDINATOR_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "googlefindmy"
    / "coordinator"
)
_MIXIN_TYPING_PY = _COORDINATOR_DIR / "_mixin_typing.py"
_MAIN_PY = _COORDINATOR_DIR / "main.py"

# The three DataUpdateCoordinator methods that must NOT be defined at
# runtime in _MixinBase (otherwise they shadow the real implementations).
_DANGEROUS_METHODS = frozenset(
    {
        "async_set_updated_data",
        "async_request_refresh",
        "async_set_update_error",
    }
)


# ---- AST helpers -------------------------------------------------------


def _parse_file(path: Path) -> ast.Module:
    """Parse a Python source file into an AST."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _find_class_node(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    """Return the first top-level ClassDef with the given name, or None."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _get_runtime_method_names(class_node: ast.ClassDef) -> set[str]:
    """Return method names defined directly in the class body at RUNTIME.

    Methods inside ``if TYPE_CHECKING:`` blocks are excluded because they
    only exist during static analysis.
    """
    names: set[str] = set()
    for node in class_node.body:
        # Direct function/async-function definitions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        # Functions inside ``if`` blocks that are NOT TYPE_CHECKING
        elif isinstance(node, ast.If) and not _is_type_checking_guard(node):
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(child.name)
    return names


def _get_type_checking_method_names(class_node: ast.ClassDef) -> set[str]:
    """Return method names defined inside ``if TYPE_CHECKING:`` blocks."""
    names: set[str] = set()
    for node in class_node.body:
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            for child in ast.walk(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(child.name)
    return names


def _is_type_checking_guard(if_node: ast.If) -> bool:
    """Return True if the ``if`` tests ``TYPE_CHECKING``."""
    test = if_node.test
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    return False


def _methods_raising_not_implemented(class_node: ast.ClassDef) -> set[str]:
    """Return names of methods whose body contains ``raise NotImplementedError``."""
    names: set[str] = set()
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.Raise) and child.exc is not None:
                    exc = child.exc
                    if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                        names.add(node.name)
                    elif isinstance(exc, ast.Call):
                        func = exc.func
                        if isinstance(func, ast.Name) and func.id == "NotImplementedError":
                            names.add(node.name)
    return names


def _get_class_base_names(class_node: ast.ClassDef) -> list[str]:
    """Return the base class names (simple names only) from a ClassDef."""
    names: list[str] = []
    for base in class_node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Subscript):
            # e.g. DataUpdateCoordinator[list[dict[str, Any]]]
            if isinstance(base.value, ast.Name):
                names.append(base.value.id)
    return names


# ---- Fixtures ----------------------------------------------------------


@pytest.fixture(scope="module")
def mixin_typing_tree() -> ast.Module:
    """Parse _mixin_typing.py once per module."""
    return _parse_file(_MIXIN_TYPING_PY)


@pytest.fixture(scope="module")
def mixin_base_node(mixin_typing_tree: ast.Module) -> ast.ClassDef:
    """Return the _MixinBase ClassDef AST node."""
    node = _find_class_node(mixin_typing_tree, "_MixinBase")
    assert node is not None, "_MixinBase class not found in _mixin_typing.py"
    return node


@pytest.fixture(scope="module")
def main_tree() -> ast.Module:
    """Parse main.py once per module."""
    return _parse_file(_MAIN_PY)


@pytest.fixture(scope="module")
def coordinator_node(main_tree: ast.Module) -> ast.ClassDef:
    """Return the GoogleFindMyCoordinator ClassDef AST node."""
    node = _find_class_node(main_tree, "GoogleFindMyCoordinator")
    assert node is not None, (
        "GoogleFindMyCoordinator class not found in main.py"
    )
    return node


# ---- Tests: _MixinBase runtime surface --------------------------------


class TestMixinBaseNoRuntimeStubs:
    """_MixinBase must NOT expose DataUpdateCoordinator methods at runtime."""

    @pytest.mark.parametrize("method_name", sorted(_DANGEROUS_METHODS))
    def test_dangerous_method_not_in_runtime_body(
        self, mixin_base_node: ast.ClassDef, method_name: str
    ) -> None:
        """The three methods must not be defined at runtime in _MixinBase.

        If they exist at runtime, they shadow DataUpdateCoordinator's
        real implementations due to MRO ordering.
        """
        runtime_methods = _get_runtime_method_names(mixin_base_node)
        assert method_name not in runtime_methods, (
            f"REGRESSION: _MixinBase defines '{method_name}' at runtime "
            f"(outside TYPE_CHECKING guard).  This shadows the real "
            f"implementation in DataUpdateCoordinator due to MRO ordering.  "
            f"Move it inside 'if TYPE_CHECKING:'."
        )

    @pytest.mark.parametrize("method_name", sorted(_DANGEROUS_METHODS))
    def test_dangerous_method_in_type_checking_block(
        self, mixin_base_node: ast.ClassDef, method_name: str
    ) -> None:
        """The three methods should exist in a TYPE_CHECKING block for mypy."""
        tc_methods = _get_type_checking_method_names(mixin_base_node)
        assert method_name in tc_methods, (
            f"'{method_name}' is not inside an 'if TYPE_CHECKING:' block "
            f"in _MixinBase.  mypy needs these declarations for cross-mixin "
            f"type resolution."
        )


class TestNoRuntimeNotImplementedForDUCMethods:
    """No DataUpdateCoordinator method should raise NotImplementedError at runtime."""

    def test_no_runtime_not_implemented_stubs_for_duc_methods(
        self, mixin_base_node: ast.ClassDef
    ) -> None:
        """Ensure no runtime method in _MixinBase raises NotImplementedError
        for a DataUpdateCoordinator method name.
        """
        runtime_methods = _get_runtime_method_names(mixin_base_node)
        raising_methods = _methods_raising_not_implemented(mixin_base_node)
        dangerous_at_runtime = (
            runtime_methods & raising_methods & _DANGEROUS_METHODS
        )
        assert not dangerous_at_runtime, (
            f"REGRESSION: _MixinBase defines runtime stubs that raise "
            f"NotImplementedError for DataUpdateCoordinator methods: "
            f"{sorted(dangerous_at_runtime)}.  These will shadow the real "
            f"implementations at runtime."
        )


# ---- Tests: MRO structure (inheritance order) --------------------------


class TestMROStructure:
    """GoogleFindMyCoordinator's base order must be verified."""

    def test_mixin_bases_precede_data_update_coordinator(
        self, coordinator_node: ast.ClassDef
    ) -> None:
        """All mixin bases must come before DataUpdateCoordinator in the
        class definition, which means _MixinBase (their common parent)
        will precede DataUpdateCoordinator in the MRO.
        """
        bases = _get_class_base_names(coordinator_node)
        assert "DataUpdateCoordinator" in bases, (
            "GoogleFindMyCoordinator does not inherit from "
            "DataUpdateCoordinator."
        )
        duc_idx = bases.index("DataUpdateCoordinator")
        # All mixin Operations classes should come before DUC
        mixin_names = [b for b in bases if b.endswith("Operations")]
        for mixin in mixin_names:
            mixin_idx = bases.index(mixin)
            assert mixin_idx < duc_idx, (
                f"{mixin} (index {mixin_idx}) must come before "
                f"DataUpdateCoordinator (index {duc_idx}) in the base list."
            )

    def test_coordinator_has_expected_mixin_bases(
        self, coordinator_node: ast.ClassDef
    ) -> None:
        """GoogleFindMyCoordinator must inherit from the expected mixins."""
        bases = _get_class_base_names(coordinator_node)
        expected_mixins = {
            "RegistryOperations",
            "SubentryOperations",
            "PollingOperations",
            "IdentityOperations",
            "LocateOperations",
            "CacheOperations",
        }
        actual_mixins = {b for b in bases if b.endswith("Operations")}
        missing = expected_mixins - actual_mixins
        assert not missing, (
            f"GoogleFindMyCoordinator is missing expected mixin bases: "
            f"{sorted(missing)}"
        )


# ---- Tests: Comprehensive audit of all _MixinBase stubs ---------------


class TestMixinBaseAudit:
    """Audit _MixinBase to detect any future shadowing regressions."""

    def test_runtime_stubs_do_not_shadow_known_duc_interface(
        self, mixin_base_node: ast.ClassDef
    ) -> None:
        """No runtime method in _MixinBase should have a name matching any
        known DataUpdateCoordinator public/protected method.

        This is a broader check than _DANGEROUS_METHODS — it catches
        future additions that might also shadow DUC methods.
        """
        # Known DataUpdateCoordinator methods (from HA source, may grow)
        duc_known_methods = {
            "async_set_updated_data",
            "async_request_refresh",
            "async_set_update_error",
            "async_config_entry_first_refresh",
            "async_refresh",
            "_async_refresh",
            "_async_update_data",
            "_async_update_listeners",
            "async_add_listener",
            "async_remove_listener",
        }
        runtime_methods = _get_runtime_method_names(mixin_base_node)
        shadowed = runtime_methods & duc_known_methods
        assert not shadowed, (
            f"_MixinBase defines runtime methods that match "
            f"DataUpdateCoordinator's interface: {sorted(shadowed)}.  "
            f"These will shadow the real implementations.  Move them "
            f"inside 'if TYPE_CHECKING:' or remove them."
        )

    def test_all_mixin_operations_inherit_from_mixin_base(self) -> None:
        """Each Operations mixin class must inherit from _MixinBase.

        If a mixin doesn't inherit from _MixinBase, the MRO changes and
        the TYPE_CHECKING guard may no longer be sufficient.  This test
        documents the expected inheritance structure.
        """
        mixin_files = {
            "polling.py": "PollingOperations",
            "cache.py": "CacheOperations",
            "registry.py": "RegistryOperations",
            "subentry.py": "SubentryOperations",
            "identity.py": "IdentityOperations",
            "locate.py": "LocateOperations",
        }
        for filename, class_name in mixin_files.items():
            path = _COORDINATOR_DIR / filename
            assert path.exists(), f"Missing file: {path}"
            tree = _parse_file(path)
            cls_node = _find_class_node(tree, class_name)
            assert cls_node is not None, (
                f"Class {class_name} not found in {filename}"
            )
            bases = _get_class_base_names(cls_node)
            assert "_MixinBase" in bases, (
                f"{class_name} in {filename} does not inherit from "
                f"_MixinBase.  All Operations mixins must inherit from "
                f"_MixinBase for consistent MRO behaviour."
            )
