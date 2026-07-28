# tests/test_guard_coordinator_identity.py
"""Self-test for the cross-test coordinator-identity guard in ``conftest.py``.

The guard itself lives in ``tests/conftest.py``: after every test,
``pytest_runtest_teardown`` calls
:func:`tests.conftest.detect_coordinator_identity_leaks` and fails the *causing*
test when an integration module still holds a foreign
``GoogleFindMyCoordinator``.  That mechanism is what turns a session-wide import
poisoning into a local, attributable failure instead of an avalanche of
``HomeAssistantError: googlefindmy coordinator not ready`` in unrelated files
minutes later.

A guard nobody tests is a guard nobody can trust, so this file pins the
properties it must have:

* it reports a module whose symbol has been replaced (otherwise it is decoration
  and the next leak passes unnoticed),
* it stays silent on a clean session (otherwise it reddens innocent tests and
  gets switched off), and
* it *restores* the production class, so the blame stays with one test instead
  of spreading over every test that follows.

The file also pins the *fix* statically: the consumer list the harnesses import
up front must still match the modules that actually bind the symbol, and those
imports must still happen before the first ``monkeypatch.setattr`` in **each**
harness that patches the symbol.  Both are
read out of the source with ``ast`` rather than reproduced at runtime.  An
earlier draft did reproduce it, by dropping seven modules from ``sys.modules``
and re-importing them inside a patch window; that test spread a second symbol
leak of its own (``entity.get_url``) and reddened
``test_metadata_helpers.py``.  A reproduction that has to practise the disease
in order to demonstrate it is the wrong instrument here.

Deliberately out of scope: whether ``pytest_runtest_teardown`` actually calls
the detector.  Verifying that inside the very session the hook governs would
mean leaving a real leak behind, which the hook would then, correctly, report.
That wiring is verified by mutation instead (drop the early imports from
``_prepare_async_setup_entry_harness`` and
``test_programmatic_subentry_creation_triggers_setup_and_entities`` errors at
teardown, naming ``device_tracker``), and it is recorded in
``tests/AGENTS.md``.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import sys

import pytest

from tests.conftest import (
    COORDINATOR_CONSUMER_MODULES,
    _restore_coordinator_identity,
    detect_coordinator_identity_leaks,
)

_PROBE_MODULE = "custom_components.googlefindmy.device_tracker"

#: Anchored on this file rather than on the process working directory.  A
#: CWD-relative path turns a wrong invocation directory into an empty scan and
#: therefore into a completeness assertion that fails with a seven-element set
#: difference, which reads like a real drift finding.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INTEGRATION_ROOT = _REPO_ROOT / "custom_components" / "googlefindmy"
_SYMBOL = "GoogleFindMyCoordinator"

#: Every harness that patches the symbol in module namespaces.  All three must
#: import the consumers first; pinning only the one that was repaired would let
#: the other two drift back into the same defect.
_HARNESSES: tuple[tuple[str, str], ...] = (
    ("tests/test_hass_data_layout.py", "_prepare_async_setup_entry_harness"),
    ("tests/test_device_entity_registration.py", "_patch_integration_runtime"),
    (
        "tests/test_entity_device_info_contract.py",
        "test_integration_device_info_uses_service_device",
    ),
)

#: The module that re-exports the symbol.  Harnesses patch this one on purpose,
#: so it is the source of the copies rather than a consumer of them.
_SYMBOL_SOURCE_MODULE = "custom_components.googlefindmy.coordinator"


def _module_level_consumers() -> set[str]:
    """Return integration modules binding the symbol at module level."""

    paths = sorted(_INTEGRATION_ROOT.rglob("*.py"))
    assert paths, (
        f"no sources found under {_INTEGRATION_ROOT}; the completeness check "
        "below would silently pass on an empty set"
    )

    consumers: set[str] = set()
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as err:
            # Skipping would shrink the expected set and turn the very drift
            # this test guards against into a false green.
            raise AssertionError(f"cannot parse {path}: {err}") from err
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.col_offset != 0:
                continue
            if not any(alias.name == _SYMBOL for alias in node.names):
                continue
            dotted = ".".join(path.relative_to(_REPO_ROOT).with_suffix("").parts)
            consumers.add(dotted.removesuffix(".__init__"))
    return consumers


def _calls_import_module(loop: ast.For) -> bool:
    """Return whether the loop body really imports its iteration variable.

    Without this, ``for consumer in COORDINATOR_CONSUMER_MODULES: pass`` would
    keep the ordering test green while the fix does nothing: naming the tuple is
    not the same as importing from it.
    """

    target = loop.target
    if not isinstance(target, ast.Name):
        return False
    for node in ast.walk(loop):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = isinstance(func, ast.Attribute) and func.attr == "import_module"
        if not called or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Name) and argument.id == target.id:
            return True
    return False


def test_consumer_list_covers_every_module_level_binding() -> None:
    """The harness's consumer tuple must not drift away from the sources.

    A module that starts importing ``GoogleFindMyCoordinator`` at module level
    silently joins the risk group.  Deriving the expected set from the sources
    makes that visible the moment it happens, instead of the next time the
    import order shifts.
    """

    expected = _module_level_consumers() - {_SYMBOL_SOURCE_MODULE}
    assert set(COORDINATOR_CONSUMER_MODULES) == expected


def test_the_consumer_list_has_no_duplicates() -> None:
    """A duplicate would slip past the set comparison above unnoticed."""

    assert len(COORDINATOR_CONSUMER_MODULES) == len(set(COORDINATOR_CONSUMER_MODULES))


@pytest.mark.parametrize(("relative_path", "function"), _HARNESSES)
def test_harness_imports_consumers_before_the_first_patch(
    relative_path: str, function: str
) -> None:
    """Importing the consumers after the first patch would defeat the fix.

    Order is the whole point: a consumer imported inside the patch window copies
    the stub.  This reads the harness source rather than its behaviour, because
    exercising the behaviour means poisoning the session on purpose.
    """

    tree = ast.parse((_REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    harness = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == function
        ),
        None,
    )
    assert harness is not None, f"{function} no longer exists in {relative_path}"

    import_line = min(
        (
            loop.lineno
            for loop in ast.walk(harness)
            if isinstance(loop, ast.For)
            and isinstance(loop.iter, ast.Name)
            and loop.iter.id == "COORDINATOR_CONSUMER_MODULES"
            and _calls_import_module(loop)
        ),
        default=None,
    )
    assert import_line is not None, (
        f"{relative_path}::{function} no longer imports the consumer modules; "
        "without a loop that actually calls importlib.import_module over "
        "COORDINATOR_CONSUMER_MODULES, a lazy first import inside the patch "
        "window keeps the stub"
    )

    first_patch_line = min(
        (
            node.lineno
            for node in ast.walk(harness)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setattr"
        ),
        default=None,
    )
    assert first_patch_line is not None, (
        f"{relative_path}::{function} no longer patches anything"
    )
    assert import_line < first_patch_line, (
        f"{relative_path}::{function} imports the coordinator consumers at line "
        f"{import_line}, after the first monkeypatch.setattr at line "
        f"{first_patch_line}; move the imports above it"
    )


def test_guard_is_silent_without_a_leak() -> None:
    """A clean session must not produce findings."""

    assert detect_coordinator_identity_leaks() == []


def test_guard_reports_a_replaced_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replacing the symbol in one module must surface exactly that module.

    Patched through ``monkeypatch`` on purpose: a bare assignment with a
    ``finally`` would leave the foreign class behind if anything above the
    ``try`` ever started raising, and the teardown guard would then report this
    very self-test as the culprit.
    """

    module = importlib.import_module(_PROBE_MODULE)

    class _ForeignCoordinator:
        """Stand-in for a stub that outlived its monkeypatch window."""

    with monkeypatch.context() as patcher:
        patcher.setattr(module, "GoogleFindMyCoordinator", _ForeignCoordinator)
        assert detect_coordinator_identity_leaks() == [_PROBE_MODULE]

    assert detect_coordinator_identity_leaks() == []


def test_restoring_a_poisoned_module_ends_the_finding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reporting without restoring would redden every following test.

    The teardown hook restores before it fails, so exactly one test carries the
    blame. Without the restore, the stub stays in the consumer namespace and the
    next test, and the one after it, fail in teardown for a leak they did not
    cause.
    """

    module = importlib.import_module(_PROBE_MODULE)

    class _ForeignCoordinator:
        """Stand-in for a stub that outlived its monkeypatch window."""

    with monkeypatch.context() as patcher:
        patcher.setattr(module, "GoogleFindMyCoordinator", _ForeignCoordinator)
        assert detect_coordinator_identity_leaks() == [_PROBE_MODULE]

        _restore_coordinator_identity([_PROBE_MODULE])

        assert module.GoogleFindMyCoordinator is not _ForeignCoordinator
        assert detect_coordinator_identity_leaks() == []


def test_restoring_tolerates_a_module_that_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name that vanished from ``sys.modules`` must not raise in teardown.

    The list of leaks is taken before the restore runs; a test that removes a
    module in between would otherwise turn a diagnostic into a crash.
    """

    monkeypatch.delitem(sys.modules, _PROBE_MODULE, raising=False)

    _restore_coordinator_identity([_PROBE_MODULE])


def test_guard_ignores_the_lazily_bound_package_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The integration package rebinds its symbol lazily and is exempt.

    ``custom_components.googlefindmy`` starts with a placeholder class and swaps
    in the real one via ``_ensure_runtime_imports()``.  The exemption carries
    this case on its own, independently of the healing helper in the teardown
    hook; counting it as a leak would fail tests over a benign, self-repairing
    condition.
    """

    package = sys.modules["custom_components.googlefindmy"]
    if getattr(package, "GoogleFindMyCoordinator", None) is None:
        pytest.skip("integration package has not bound the symbol yet")

    class _PlaceholderCoordinator:
        """Mimics the package's own placeholder class."""

    with monkeypatch.context() as patcher:
        patcher.setattr(package, "GoogleFindMyCoordinator", _PlaceholderCoordinator)
        assert detect_coordinator_identity_leaks() == []
