# tests/test_config_flow_lazy_registry_imports.py
"""Guard the flow-only import boundary of ``config_flow`` against the API graph.

Codex finding on PR #1274: a module-scope ``from .coordinator.helpers.registry
import ...`` in ``config_flow.py`` runs ``coordinator/__init__.py``, which eagerly
imports the API and its crypto and network dependencies. Home Assistant imports
``config_flow`` on flow-only paths, so an unrelated import failure in that graph
would keep the configuration UI from loading. The ownership helpers are imported
inside the one function that uses them instead (``agents/config_flow/AGENTS.md``,
"Device ownership in flow code").

Scope note: a file-local guard in the style of
``test_location_request_lazy_imports.py``. It does not assert that the API graph
stays out of ``sys.modules`` process-wide, because the test session imports it
through other modules long before this file runs.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_OWNERSHIP_HELPERS = (
    "OwnershipIntent",
    "detect_device_registry_capabilities",
    "execute_ownership_plan",
    "plan_device_ownership",
    "resolve_device_by_identifiers",
)

_CONFIG_FLOW_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "googlefindmy"
    / "config_flow.py"
)


@pytest.mark.parametrize("name", _OWNERSHIP_HELPERS)
def test_ownership_helper_is_not_bound_at_module_scope(name: str) -> None:
    """A module-scope import would expose the helper as a module attribute."""

    module = importlib.import_module("custom_components.googlefindmy.config_flow")
    assert not hasattr(module, name), (
        f"{name} is bound at module scope in config_flow; import it inside "
        "_ensure_service_device_binding so the flow-only import path stays "
        "free of coordinator/__init__.py"
    )


def test_config_flow_has_no_module_scope_import_of_coordinator_or_api() -> None:
    """Static form of the same contract, independent of what the session has loaded."""

    tree = ast.parse(_CONFIG_FLOW_PATH.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            head = node.module.split(".")[0]
            if node.level and head in {"coordinator", "api"}:
                offenders.append(
                    f"line {node.lineno}: from {'.' * node.level}{node.module}"
                )
    assert offenders == [], offenders


def test_the_static_guard_sees_a_module_scope_import() -> None:
    """Positive control: the AST walk above is not vacuous."""

    tree = ast.parse("from .coordinator.helpers.registry import OwnershipIntent\n")
    node = tree.body[0]
    assert isinstance(node, ast.ImportFrom)
    assert node.level == 1
    assert node.module is not None
    assert node.module.split(".")[0] == "coordinator"
