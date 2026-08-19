# tests/test_browser_deps_boundary.py
"""The browser packages are a CLI concern and must stay out of Home Assistant.

Two properties are asserted here, both mechanically:

1. `manifest.json` does not make Home Assistant install Selenium or
   undetected-chromedriver into every setup.
2. No module Home Assistant loads on its own reaches them, so (1) cannot break
   the integration.

Property (2) is what makes (1) safe, which is why they live in one file.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from custom_components.googlefindmy import browser_deps

PACKAGE_ROOT = Path(browser_deps.__file__).parent
BROWSER_PACKAGES = {"selenium", "undetected_chromedriver", "webdriver_manager"}


def _module_level_imports(path: Path) -> list[tuple[str, int]]:
    """Return (module, relative level) for imports executed at load time."""

    found: list[tuple[str, int]] = []

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, ast.Import):
                found.extend((alias.name, 0) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                found.append((base, node.level))
                # `from . import diagnostics` binds a submodule; without the
                # aliases the crawl would silently skip whole subtrees.
                for alias in node.names:
                    if alias.name != "*":
                        found.append(
                            (f"{base}.{alias.name}" if base else alias.name, node.level)
                        )
            elif isinstance(node, ast.If):
                if "TYPE_CHECKING" in ast.dump(node.test):
                    continue  # not executed at run time
                walk(node.body)
                walk(node.orelse)
            elif isinstance(node, (ast.Try, ast.With)):
                walk(node.body)
                for handler in getattr(node, "handlers", []):
                    walk(handler.body)
                walk(getattr(node, "orelse", []))
                walk(getattr(node, "finalbody", []))
            # function and class bodies are not walked: they do not run on import

    walk(ast.parse(path.read_text(encoding="utf-8")).body)
    return found


def _dynamic_edges(path: Path) -> set[str]:
    """Return literal targets of `importlib.import_module(...)` in one module."""

    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name not in {"import_module", "__import__"}:
            continue
        found.update(
            arg.value
            for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        )
    return found


def _reachable(entry: str) -> tuple[set[str], set[str], set[str]]:
    """Crawl module-level imports transitively; report dynamic edges separately."""

    prefix = "custom_components.googlefindmy"
    seen: set[str] = set()
    external: set[str] = set()
    dynamic: set[str] = set()
    queue = [Path(entry)]
    while queue:
        rel = queue.pop()
        key = rel.as_posix()
        if key in seen:
            continue
        seen.add(key)
        dynamic |= _dynamic_edges(PACKAGE_ROOT / rel)
        for module, level in _module_level_imports(PACKAGE_ROOT / rel):
            if level:
                base = rel.parent
                for _ in range(level - 1):
                    base = base.parent
                parts = list(base.parts) + (module.split(".") if module else [])
            elif module == prefix or module.startswith(prefix + "."):
                parts = [p for p in module[len(prefix) :].split(".") if p]
            else:
                if module:
                    external.add(module.split(".")[0])
                continue
            candidate = PACKAGE_ROOT.joinpath(*parts)
            if candidate.with_suffix(".py").is_file():
                queue.append(candidate.with_suffix(".py").relative_to(PACKAGE_ROOT))
            elif (candidate / "__init__.py").is_file():
                queue.append((candidate / "__init__.py").relative_to(PACKAGE_ROOT))
    return seen, external, dynamic


def test_manifest_does_not_ship_browser_packages() -> None:
    manifest = json.loads((PACKAGE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    requirements = " ".join(manifest["requirements"]).lower()

    assert "selenium" not in requirements
    assert "chromedriver" not in requirements


@pytest.mark.parametrize("entry", ["__init__.py", "config_flow.py", "eid_resolver.py"])
def test_home_assistant_entry_points_reach_no_browser_package(entry: str) -> None:
    _, external, _dynamic = _reachable(entry)

    assert not (external & BROWSER_PACKAGES), (
        f"{entry} now loads a browser package at import time; "
        "either the import is misplaced or manifest.json has to carry it again"
    )


@pytest.mark.parametrize("entry", ["chrome_driver.py", "Auth/auth_flow.py"])
def test_positive_control_the_crawler_can_see_browser_packages(entry: str) -> None:
    """Without this, the assertions above would be a vacuum finding."""

    _, external, _dynamic = _reachable(entry)

    assert external & BROWSER_PACKAGES


def test_the_only_dynamic_route_to_the_browser_is_the_guarded_one() -> None:
    """A static crawl alone would overstate the result.

    `KeyBackup/shared_key_retrieval.py` -> `_interactive_flow_hex` reaches the
    browser flow through `importlib.import_module`, which no import graph sees.
    That edge is real and it sits behind the terminal guard in
    `_retrieve_shared_key_hex`. This test pins that it stays the *only* one, and
    that the branch reports the missing packages instead of an import traceback.
    """

    _, _, dynamic = _reachable("__init__.py")
    browser_routes = {
        target
        for target in dynamic
        if target.split(".")[0] in BROWSER_PACKAGES
        or target.endswith("shared_key_flow")
        or target.endswith("auth_flow")
        or target.endswith("chrome_driver")
    }

    assert browser_routes == {
        "custom_components.googlefindmy.KeyBackup.shared_key_flow"
    }

    source = (PACKAGE_ROOT / "KeyBackup" / "shared_key_retrieval.py").read_text(
        encoding="utf-8"
    )
    assert "MISSING_BROWSER_PACKAGES_HINT" in source
    assert "is_tty = sys.stdin and sys.stdin.isatty()" in source


def test_the_hint_names_the_install_command() -> None:
    assert "pip install selenium undetected-chromedriver" in (
        browser_deps.MISSING_BROWSER_PACKAGES_HINT
    )
    wrapped = browser_deps.missing_browser_dependency(ImportError("no module"))
    assert "pip install" in str(wrapped)
    assert "no module" in str(wrapped)
