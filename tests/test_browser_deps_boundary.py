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
from types import SimpleNamespace

import pytest

from custom_components.googlefindmy import browser_deps

PACKAGE_ROOT = Path(browser_deps.__file__).parent
BROWSER_PACKAGES = {"selenium", "undetected_chromedriver", "webdriver_manager"}
PACKAGE_PREFIX = "custom_components.googlefindmy"
# The one dynamic route to the browser that is meant to exist. It sits behind
# the command-line marker in `shared_key_retrieval._retrieve_shared_key_hex`;
# `test_the_only_dynamic_route_to_the_browser_is_the_guarded_one` pins that it
# stays the only one.
INTENTIONAL_DYNAMIC_BROWSER_ROUTE = f"{PACKAGE_PREFIX}.KeyBackup.shared_key_flow"


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
            elif isinstance(node, ast.ClassDef):
                # A class body *does* run at import time: Python executes it to
                # build the namespace before the class object exists. An import
                # placed there is a load-time import like any other, so skipping
                # it alongside function bodies would be a hole in this check.
                walk(node.body)
            # function bodies are not walked: they do not run on import

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

    prefix = PACKAGE_PREFIX
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


def test_the_crawler_walks_a_class_body(tmp_path: Path) -> None:
    """A class body runs at import time; a function body does not.

    Both were skipped together, which is right for one of them. An import
    inside a class body executes while the module loads, so it would break a
    Home Assistant platform at setup time while this test stayed green.
    """

    module = tmp_path / "classy.py"
    module.write_text(
        textwrap.dedent(
            """\
            class Holder:
                import class_body_package

                def method(self):
                    import function_body_package
            """
        ),
        encoding="utf-8",
    )

    names = {name for name, _ in _module_level_imports(module)}

    assert "class_body_package" in names
    assert "function_body_package" not in names


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

    # Collected over every module Home Assistant can reach, including the
    # fifteen it only reaches through an import written inside a function.
    # A module-level crawl from `__init__.py` alone does not see `discovery.py`
    # or `services.py`, so an `import_module("selenium")` added there used to
    # leave this test green.
    dynamic: set[str] = set()
    for entry in _ha_entry_points():
        for rel in _modules_reached_including_function_bodies(entry):
            dynamic |= _dynamic_edges(PACKAGE_ROOT / Path(rel))
    browser_routes = {
        target
        for target in dynamic
        if target.split(".")[0] in BROWSER_PACKAGES
        or target.endswith("shared_key_flow")
        or target.endswith("auth_flow")
        or target.endswith("chrome_driver")
    }

    assert browser_routes == {INTENTIONAL_DYNAMIC_BROWSER_ROUTE}

    source = (PACKAGE_ROOT / "KeyBackup" / "shared_key_retrieval.py").read_text(
        encoding="utf-8"
    )
    assert "MISSING_BROWSER_PACKAGES_HINT" in source
    # The guard in front of that route is the marker the command-line tool sets
    # for itself. #1254 replaced the terminal test that used to stand here: a
    # Home Assistant instance started in the foreground of a terminal answers
    # `isatty()` with True and is still not the CLI, so a session check alone
    # is a necessary condition, never a sufficient one.
    assert '_ENV_CLI_PROCESS = "GOOGLEFINDMY_CLI_PROCESS"' in source
    assert "is_cli = _cli_process()" in source


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


def test_the_translated_failure_still_reaches_the_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint does not survive the strategy chain, so text matching is not enough.

    `chrome_driver.py` ends in a generic "Failed to start ChromeDriver"
    message, and `shared_key_flow.request_shared_key_flow` turns any driver
    failure into `None`. Both wipe the install hint from the text, which is
    exactly the case the boundary exists for. Asking the packages survives
    both translations.
    """

    from custom_components.googlefindmy import main

    monkeypatch.setattr(browser_deps, "browser_packages_missing", lambda: True)

    def _translated() -> tuple[str, str | None]:
        raise RuntimeError(
            "Failed to start ChromeDriver after all attempts.\n"
            "Possible solutions:\n"
            "1. Make sure Google Chrome is installed and up-to-date"
        )

    with pytest.raises(SystemExit) as excinfo:
        main._run_oauth_flow_or_exit(_translated)

    assert excinfo.value.code == 1


def test_the_original_failure_is_reported_before_the_packages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not every failure on this path comes from loading the driver.

    `Auth/auth_flow.py` refuses an unattended run before `create_driver()` is
    reached. Leading with the install command would answer a question that
    user did not ask: they would install the packages and fail again for the
    same reason. The failure comes first, the packages second.
    """

    from custom_components.googlefindmy import main

    monkeypatch.setattr(browser_deps, "browser_packages_missing", lambda: True)

    def _unattended() -> tuple[str, str | None]:
        raise RuntimeError("The interactive Chrome login needs an attended terminal")

    with pytest.raises(SystemExit):
        main._run_oauth_flow_or_exit(_unattended)

    out = capsys.readouterr().out
    assert out.index("attended terminal") < out.index(browser_deps.INSTALL_COMMAND)


def test_a_real_driver_failure_keeps_its_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterpart: with the packages present, nothing is swallowed."""

    from custom_components.googlefindmy import main

    monkeypatch.setattr(browser_deps, "browser_packages_missing", lambda: False)

    def _crashed() -> tuple[str, str | None]:
        raise RuntimeError("chrome crashed")

    with pytest.raises(RuntimeError, match="chrome crashed"):
        main._run_oauth_flow_or_exit(_crashed)


def test_the_package_probe_reports_what_is_installed() -> None:
    """Positive control: the probe is capable of answering both ways.

    Selenium and undetected-chromedriver are installed in the test
    environment, so a probe that always returned True would make the two
    tests above vacuous.
    """

    assert browser_deps.browser_packages_missing() is False


def test_an_imported_stub_counts_as_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module in `sys.modules` is importable, whatever `find_spec` says.

    `tests/test_chrome_driver.py` puts a `SimpleNamespace` under
    `undetected_chromedriver` so the driver module can be imported without the
    real package. That object has no `__spec__`, and `find_spec` raises
    `ValueError` for it — so a probe that only asked `find_spec` reported a
    package that is demonstrably in use as missing, and the install hint
    started appearing on unrelated failures.
    """

    import sys

    monkeypatch.setitem(sys.modules, "undetected_chromedriver", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "selenium", SimpleNamespace())

    assert browser_deps.browser_packages_missing() is False


def test_an_installed_but_unimportable_package_survives_the_strategy_chain() -> None:
    """`find_spec` finds a package whose import fails; only the type tells them apart.

    `undetected_chromedriver` does not import where `distutils` has been
    removed from the standard library, which is the state of a modern GitHub
    runner. The package is installed, so a presence probe says "present", and
    the strategy chain rewrites the stub's message into its own generic one.
    The type is the only thing that crosses both.
    """

    from custom_components.googlefindmy import main

    def _broken() -> tuple[str, str | None]:
        raise browser_deps.BrowserPackagesUnusable(
            "undetected_chromedriver could not be imported"
        )

    with pytest.raises(SystemExit) as excinfo:
        main._run_oauth_flow_or_exit(_broken)

    assert excinfo.value.code == 1


def test_the_chain_surfaces_the_typed_failure_instead_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic "Failed to start ChromeDriver" advice does not address it.

    Raised only after every strategy and the webdriver-manager fallback have
    had their turn, so nothing that could still have worked is cut short.
    """

    from custom_components.googlefindmy import chrome_driver

    typed = browser_deps.BrowserPackagesUnusable("uc is installed but broken")

    # Every strategy, not just the ones that run here: which of them land in
    # the attempt list depends on whether a Chrome binary and a version could
    # be resolved, and that differs between a developer machine and the CI
    # runner, where Chrome exists. The first version of this test patched
    # three of the four and passed locally while failing in CI.
    for strategy in (
        "_try_strategy_default",
        "_try_strategy_explicit_path",
        "_try_strategy_no_version",
        "_try_strategy_headless",
    ):
        monkeypatch.setattr(chrome_driver, strategy, lambda **_: (None, typed))
    monkeypatch.setattr(chrome_driver, "_try_webdriver_manager_fallback", lambda: None)

    with pytest.raises(browser_deps.BrowserPackagesUnusable):
        chrome_driver._create_driver_inner(headless=True)


def _runtime_nodes(tree: ast.Module) -> list[ast.stmt]:
    """Every node that can run, function bodies included, `TYPE_CHECKING` not.

    `ast.walk` would report `if TYPE_CHECKING: import selenium` as reachable.
    That import never executes, and annotating against a CLI-only dependency
    is a legitimate pattern, so a scan that flags it fails a correct module.
    The guard polarity is read with `_type_checking_polarity`, the same helper
    `_module_level_imports` uses, rather than a second reading of it: the
    `else:` branch of such a guard *does* run, and `__init__.py` relies on that.
    """

    out: list[ast.stmt] = []

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            out.append(node)
            if isinstance(node, ast.If):
                guard = _type_checking_polarity(node.test)
                if guard is True:
                    walk(node.orelse)
                elif guard is False:
                    walk(node.body)
                else:
                    walk(node.body)
                    walk(node.orelse)
                continue
            for field in ("body", "orelse", "finalbody"):
                walk(getattr(node, field, []) or [])
            for handler in getattr(node, "handlers", []):
                walk(handler.body)

    walk(tree.body)
    return out


def _all_imports(path: Path, root: Path = PACKAGE_ROOT) -> tuple[set[str], set[str]]:
    """Return (external packages, internal modules) from *every* import in a file.

    Unlike `_module_level_imports`, this does not care whether an import runs
    at load time: it also reads function bodies, because Home Assistant calls
    those functions. Relative imports are resolved, so a callback that reaches
    another module with `from .discovery import ...` extends the search
    instead of ending it.

    Only literal import statements. The one deliberate dynamic route,
    `importlib.import_module` in `shared_key_retrieval`, is not one and stays
    with `_dynamic_edges`, which is what keeps `chrome_driver` out of this
    closure -- exactly where the browser packages are supposed to live.
    """

    prefix = PACKAGE_PREFIX
    external: set[str] = set()
    internal: set[str] = set()
    rel = path.relative_to(root)

    for node in _runtime_nodes(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(prefix):
                    internal.add(alias.name[len(prefix) :].strip("."))
                else:
                    external.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = rel.parent
                for _ in range(node.level - 1):
                    base = base.parent
                parts = [p for p in list(base.parts) if p]
                if node.module:
                    parts += node.module.split(".")
                internal.add(".".join(parts))
                # `from . import discovery` binds a submodule, not a name.
                internal.update(".".join(parts + [a.name]) for a in node.names)
            elif node.module and node.module.startswith(prefix):
                base = node.module[len(prefix) :].strip(".")
                internal.add(base)
                # `from custom_components.googlefindmy import chrome_driver`
                # binds a submodule, exactly as the relative form above does.
                internal.update(
                    ".".join(p for p in (base, a.name) if p) for a in node.names
                )
            elif node.module:
                external.add(node.module.split(".")[0])
    return external, internal


def _resolve_internal(module: str) -> Path | None:
    candidate = PACKAGE_ROOT.joinpath(*module.split("."))
    if candidate.with_suffix(".py").is_file():
        return candidate.with_suffix(".py").relative_to(PACKAGE_ROOT)
    if (candidate / "__init__.py").is_file():
        return (candidate / "__init__.py").relative_to(PACKAGE_ROOT)
    return None


def _modules_reached_including_function_bodies(entry: str) -> set[str]:
    """Every module reachable from *entry*, following imports wherever written.

    Wider than `_reachable`, which stops at module level: fifteen modules --
    `discovery.py`, `services.py`, `map_view.py` and the `fmdn_finder` family
    among them -- are reached only through an import inside a function that
    Home Assistant calls. A check that crawls module level alone never looks
    at them.

    A literal `import_module("custom_components.googlefindmy.x")` is followed
    as well: five modules -- `create_ble_device`, `DeviceUpdate_pb2` and the
    `upload_precomputed_public_key_ids` chain among them -- are reached that
    way and no other, so a browser import placed in one of them used to be
    invisible here. `INTENTIONAL_DYNAMIC_BROWSER_ROUTE` is the one target left
    out, because it is the route this file pins by name in
    `test_the_only_dynamic_route_to_the_browser_is_the_guarded_one`; enqueueing
    it would drag `chrome_driver` in and turn the check red on the very design
    it exists to protect. That omission is not blind: a *second* browser route
    turns that other test red.
    """

    seen: set[str] = set()
    queue = [Path(entry)]
    while queue:
        rel = queue.pop()
        if str(rel) in seen:
            continue
        seen.add(str(rel))
        _, internal = _all_imports(PACKAGE_ROOT / rel)
        targets = set(internal)
        for target in _dynamic_edges(PACKAGE_ROOT / rel):
            if (
                target.startswith(PACKAGE_PREFIX)
                and target != INTENTIONAL_DYNAMIC_BROWSER_ROUTE
            ):
                targets.add(target[len(PACKAGE_PREFIX) :].strip("."))
        for module in targets:
            resolved = _resolve_internal(module)
            if resolved is not None and str(resolved) not in seen:
                queue.append(resolved)
    return seen


def _closure_including_function_bodies(entry: str) -> dict[str, set[str]]:
    """Walk from one entry point, following imports wherever they are written."""

    offenders: dict[str, set[str]] = {}
    for rel in _modules_reached_including_function_bodies(entry):
        external, _ = _all_imports(PACKAGE_ROOT / Path(rel))
        if external & BROWSER_PACKAGES:
            offenders[rel] = external & BROWSER_PACKAGES
    return offenders


@pytest.mark.parametrize("entry", _ha_entry_points())
def test_no_browser_import_hides_in_a_function_home_assistant_calls(
    entry: str,
) -> None:
    """A function Home Assistant calls runs, wherever its imports are written."""

    offenders = _closure_including_function_bodies(entry)

    assert not offenders, (
        f"{sorted(offenders)} import a browser package, and Home Assistant "
        "reaches them; manifest.json no longer installs it"
    )


def test_the_closure_follows_a_relative_import_inside_a_function(
    tmp_path: Path,
) -> None:
    """Positive control for the edge that made the first version incomplete.

    `config_flow.py` reaches `discovery.py` through `from . import discovery`
    inside a callback. An import graph that drops relative edges written in
    function bodies would call that module unreachable and miss anything in
    it.
    """

    module = tmp_path / "caller.py"
    module.write_text(
        textwrap.dedent(
            """\
            def callback():
                from . import discovery
                from .helpers import thing
                return discovery, thing
            """
        ),
        encoding="utf-8",
    )

    _external, internal = _all_imports(module, tmp_path)

    assert "discovery" in internal
    assert "helpers" in internal or "helpers.thing" in internal


def test_the_closure_follows_an_absolute_import_of_a_submodule(
    tmp_path: Path,
) -> None:
    """The absolute spelling binds a submodule just like the relative one.

    `from custom_components.googlefindmy import chrome_driver` names the
    package in `node.module` and the submodule in `node.names`. Reading only
    `node.module` records the package itself, so the walk loops back to
    `__init__.py` and never opens the module that was actually imported --
    with a browser dependency inside it staying green.
    """

    module = tmp_path / "caller.py"
    module.write_text(
        textwrap.dedent(
            f"""\
            def callback():
                from {PACKAGE_PREFIX} import chrome_driver
                from {PACKAGE_PREFIX}.KeyBackup import shared_key_flow
                return chrome_driver, shared_key_flow
            """
        ),
        encoding="utf-8",
    )

    _external, internal = _all_imports(module, tmp_path)

    assert "chrome_driver" in internal
    assert "KeyBackup.shared_key_flow" in internal


def test_a_dynamically_loaded_module_is_part_of_the_closure() -> None:
    """`import_module("...x")` reaches x, and x is scanned like any other.

    Five modules are reached that way and no other way -- `create_ble_device`,
    `DeviceUpdate_pb2`, `upload_precomputed_public_key_ids` and the two
    `FMDNCrypto` helpers they pull in. A browser import placed in one of them
    used to leave every check in this file green.
    """

    reached: set[str] = set()
    for entry in _ha_entry_points():
        reached |= _modules_reached_including_function_bodies(entry)

    assert "SpotApi/CreateBleDevice/create_ble_device.py" in reached
    assert "ProtoDecoders/DeviceUpdate_pb2.py" in reached

    # The deliberate browser route stays outside, and stays *pinned* outside:
    # a second one turns
    # `test_the_only_dynamic_route_to_the_browser_is_the_guarded_one` red.
    assert "KeyBackup/shared_key_flow.py" not in reached
    assert "chrome_driver.py" not in reached


def test_the_function_body_scan_can_see_an_import(tmp_path: Path) -> None:
    """Positive control: without it the assertion above is a vacuum finding."""

    module = tmp_path / "deferred.py"
    module.write_text(
        textwrap.dedent(
            """\
            def setup():
                import selenium
                return selenium
            """
        ),
        encoding="utf-8",
    )

    external, _internal = _all_imports(module, tmp_path)

    assert external & BROWSER_PACKAGES
