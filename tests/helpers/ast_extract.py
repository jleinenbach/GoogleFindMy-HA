# tests/helpers/ast_extract.py
"""Utilities for extracting callable objects from integration modules via AST."""

from __future__ import annotations

import ast
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

_ModulePath = Path | str


def compile_class_method_from_module(
    *,
    module_path: _ModulePath,
    class_name: str,
    method_name: str,
    global_overrides: Mapping[str, Any] | None = None,
) -> Any:
    """Compile a method from ``class_name`` in ``module_path`` into a standalone function.

    The helper loads the requested module's source, finds ``class_name`` and
    ``method_name`` in the AST, and compiles the function body in isolation. The
    resulting object behaves like the method defined in the original module once
    it is bound with :func:`types.MethodType`.

    Args:
        module_path: Path to the Python module that defines the target class.
        class_name: The name of the class containing the target method.
        method_name: The name of the method to extract.
        global_overrides: Optional mapping of global names injected into the
            execution environment so dependencies such as modules or constants are
            available when the compiled function runs.

    Returns:
        The standalone function object corresponding to ``method_name``.

    Raises:
        FileNotFoundError: If ``module_path`` does not exist.
        AssertionError: If the class or method cannot be found in the module.
    """

    module_path = Path(module_path).resolve()
    repo_root = Path(__file__).resolve().parents[2]
    if repo_root not in module_path.parents and module_path != repo_root:
        raise ValueError("module_path must point inside the repository tree")

    source = module_path.read_text(encoding="utf-8")
    module_ast = ast.parse(source, filename=str(module_path))

    for node in module_ast.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    func_module = ast.Module(
                        body=[deepcopy(item)],
                        type_ignores=[],
                    )
                    ast.fix_missing_locations(func_module)
                    namespace: dict[str, Any] = {}
                    safe_builtins = {
                        "__build_class__": __build_class__,
                        "__import__": __import__,
                        "len": len,
                        "range": range,
                        "min": min,
                        "max": max,
                        "sum": sum,
                        "any": any,
                        "all": all,
                        "isinstance": isinstance,
                        "issubclass": issubclass,
                        "object": object,
                        "str": str,
                        "int": int,
                        "float": float,
                        "bool": bool,
                        "dict": dict,
                        "list": list,
                        "tuple": tuple,
                        "set": set,
                        "frozenset": frozenset,
                        "enumerate": enumerate,
                        "zip": zip,
                        "print": print,
                    }

                    exec(
                        compile(func_module, str(module_path), "exec"),
                        {
                            "__builtins__": safe_builtins,
                            **(dict(global_overrides) if global_overrides else {}),
                        },
                        namespace,
                    )
                    return namespace[item.name]
            break

    raise AssertionError(
        f"Method {method_name!r} on class {class_name!r} not found in {module_path}"
    )
