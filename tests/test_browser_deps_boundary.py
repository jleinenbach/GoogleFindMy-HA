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
import textwrap
from pathlib import Path

import pytest

from custom_components.googlefindmy import browser_deps

PACKAGE_ROOT = Path(browser_deps.__file__).parent
BROWSER_PACKAGES = {"selenium", "undetected_chromedriver", "webdriver_manager"}


def _type_checking_polarity(test: ast.expr) -> bool | None:
    """Return what a plain TYPE_CHECKING guard evaluates to at run time.

    ``True``  for ``if TYPE_CHECKING:``      — the body is type-only.
    ``False`` for ``if not TYPE_CHECKING:``  — the body is the runtime branch.
    ``None``  for anything else              — walk both branches.
    """

    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _type_checking_polarity(test.operand)
        if inner is not None:
            return not inner
    return None


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
                # `if TYPE_CHECKING:` does not run, but its `else:` does — and
                # that runtime-alias pattern is used in `__init__.py` itself.
                # Skipping the whole statement would hide an import there.
                guard = _type_checking_polarity(node.test)
                if guard is True:
                    walk(node.orelse)
                elif guard is False:
                    walk(node.body)
                else:
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


def _ha_entry_points() -> list[str]:
    """Every module Home Assistant loads on its own, not just the obvious two.

    The platform modules are never imported from `__init__.py`: Home Assistant
    loads them itself from the `PLATFORMS` list via `async_forward_entry_setups`.
    A browser import in one of them would run at setup time while this test
    stayed green, which is exactly the failure the manifest change would cause.
    The list is read from the source instead of being written out here, so a new
    platform cannot silently fall out of the crawl.
    """

    tree = ast.parse((PACKAGE_ROOT / "__init__.py").read_text(encoding="utf-8"))
    platforms: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not (isinstance(target, ast.Name) and target.id == "PLATFORMS"):
            continue
        for element in getattr(node.value, "elts", []):
            if isinstance(element, ast.Attribute):
                platforms.append(element.attr.lower())

    assert platforms, "PLATFORMS could not be read; the crawl would be incomplete"

    # Modules Home Assistant discovers by file name, not by import.
    by_convention = [
        "__init__.py",
        "config_flow.py",
        "diagnostics.py",
        "repairs.py",
        "system_health.py",
        "eid_resolver.py",
    ]
    entries = [f"{name}.py" for name in platforms] + by_convention
    return [name for name in entries if (PACKAGE_ROOT / name).is_file()]


def test_manifest_does_not_ship_browser_packages() -> None:
    manifest = json.loads((PACKAGE_ROOT / "manifest.json").read_text(encoding="utf-8"))
    requirements = " ".join(manifest["requirements"]).lower()

    assert "selenium" not in requirements
    assert "chromedriver" not in requirements


@pytest.mark.parametrize("entry", _ha_entry_points())
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


def test_the_crawler_walks_the_runtime_branch_of_a_type_checking_guard(
    tmp_path: Path,
) -> None:
    """`if TYPE_CHECKING: ... else: ...` is used in `__init__.py` itself.

    Dropping both branches would make an import in the branch that actually
    executes invisible, and the boundary test would stay green while the
    manifest no longer installs that dependency.
    """

    module = tmp_path / "guarded.py"
    module.write_text(
        textwrap.dedent(
            """\
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                import type_only_package
            else:
                import runtime_package
            if not TYPE_CHECKING:
                import other_runtime_package
            """
        ),
        encoding="utf-8",
    )

    names = {name for name, _ in _module_level_imports(module)}

    assert "runtime_package" in names
    assert "other_runtime_package" in names
    assert "type_only_package" not in names


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


def test_the_lazy_driver_import_carries_the_install_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selenium fails loudly at import; undetected-chromedriver does not.

    `chrome_driver._load_uc` imports it lazily and falls back to a stub, so an
    environment with only Selenium installed gets past every import guard. The
    stub is therefore where the hint has to appear, not in a preflight: an eager
    import would also defeat the distutils fallback the stub exists for.
    """

    import importlib

    from custom_components.googlefindmy import chrome_driver

    real_import_module = importlib.import_module

    def _hide_uc(name: str, *args: object, **kwargs: object) -> object:
        if name == "undetected_chromedriver":
            raise ImportError("No module named 'undetected_chromedriver'")
        return real_import_module(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(chrome_driver.importlib, "import_module", _hide_uc)

    stub = chrome_driver._load_uc()

    # Call it the way the production strategies call it. A stub that only
    # accepts `options` raises TypeError here, the strategy chain swallows it
    # as a generic driver failure, and the hint never reaches the user.
    for kwargs in (
        {"options": object()},
        {"options": object(), "version_main": 131},
        {"options": object(), "version_main": None},
        {
            "options": object(),
            "version_main": 131,
            "browser_executable_path": "/usr/bin/chromium",
        },
    ):
        with pytest.raises(RuntimeError) as excinfo:
            stub.Chrome(**kwargs)

        assert browser_deps.INSTALL_COMMAND in str(excinfo.value)

def test_the_first_run_cli_exits_cleanly_when_the_driver_is_missing() -> None:
    """The `ImportError` guard cannot see it: the driver loads later, lazily.

    Without a boundary at the call, the stub's hinted `RuntimeError` travels
    through the driver strategy chain and reaches the user as a traceback.
    """

    from custom_components.googlefindmy import main

    def _flow() -> tuple[str, str | None]:
        raise RuntimeError(
            f"Browser packages are missing.\n\n    {browser_deps.INSTALL_COMMAND}\n"
        )

    with pytest.raises(SystemExit) as excinfo:
        main._run_oauth_flow_or_exit(_flow)

    assert excinfo.value.code == 1

    def _unrelated() -> tuple[str, str | None]:
        raise RuntimeError("chrome crashed")

    # Anything else must keep its traceback: swallowing it would hide real bugs.
    with pytest.raises(RuntimeError, match="chrome crashed"):
        main._run_oauth_flow_or_exit(_unrelated)
