# tests/test_platinum_compliance.py
"""Automated compliance checks for the declared HA quality-scale tier.

The file name and the historical constant ``COVERAGE_PLATINUM_TARGET`` are
kept for git-blame continuity and to mark Platinum as the long-term target;
the currently declared tier is Bronze during the coverage sprint (see
``test_quality_scale_declares_bronze_status``).
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from custom_components.googlefindmy.const import INTEGRATION_VERSION


def test_manifest_declares_expected_keys(manifest: dict[str, object]) -> None:
    """Manifest must advertise the required metadata for the declared tier (Bronze during the coverage sprint; Platinum is the long-term target)."""

    required_keys = {
        "domain",
        "name",
        "version",
        "after_dependencies",
        "codeowners",
        "config_flow",
        "dependencies",
        "documentation",
        "integration_type",
        "iot_class",
        "quality_scale",
        "issue_tracker",
        "loggers",
        "requirements",
    }
    assert required_keys.issubset(manifest.keys())
    assert manifest["domain"] == "googlefindmy"
    assert manifest["config_flow"] is True
    assert manifest["integration_type"] == "hub"
    assert manifest["iot_class"] == "cloud_polling"
    assert manifest["version"] == INTEGRATION_VERSION
    assert manifest["quality_scale"] == "bronze"
    assert "recorder" in manifest.get("after_dependencies", [])
    assert "http" in manifest.get("dependencies", [])
    assert "custom_components.googlefindmy" in manifest.get("loggers", [])
    assert "discovery" not in manifest
    assert "discovery_update_info" not in manifest


@pytest.fixture(name="quality_scale_text")
def fixture_quality_scale_text(integration_root: Path) -> str:
    """Return the raw text of the quality_scale.yaml file."""

    return (integration_root / "quality_scale.yaml").read_text(encoding="utf-8")


def test_quality_scale_declares_bronze_status(quality_scale_text: str) -> None:
    """The quality scale document must pin the bronze tier during the coverage sprint.

    Tier downshift rationale (Codex-Review 2026-06-04, Commit 7dab5450):
    HA Quality Scale demands that every rule of the declared tier and all
    lower tiers be ``done`` with no exceptions. ``test-coverage`` (a Silver
    rule, >=95 %) is held at ``todo`` during the coverage sprint
    (measured 64 %, sprint floor 60 %), so the declared tier must be Bronze.
    Once scoped coverage reaches ``COVERAGE_PLATINUM_TARGET`` (95 %),
    ``test-coverage`` flips to ``done`` and the tier is hiked back via the
    phase plan in ``PLAN_CI_WARNINGS_NODE24_PLACEHOLDER.md``.
    """

    assert "tier: bronze" in quality_scale_text
    statuses = re.findall(r"status:\s+(\w+)", quality_scale_text)
    assert statuses, "quality_scale.yaml should declare rule statuses"
    assert set(statuses) <= {"done", "todo"}, (
        "Only 'done' and 'todo' are allowed as rule statuses"
    )
    todo_count = statuses.count("todo")
    assert todo_count == 1, (
        "Exactly one rule ('test-coverage') is expected at 'todo' during the "
        f"coverage sprint; found {todo_count}"
    )
    # Anchor: the single 'todo' must be 'test-coverage'.
    test_coverage_block = re.search(
        r"- id: test-coverage\b[\s\S]*?status:\s+(\w+)", quality_scale_text
    )
    assert test_coverage_block is not None, (
        "quality_scale.yaml must declare a 'test-coverage' rule"
    )
    assert test_coverage_block.group(1) == "todo", (
        "test-coverage must be at 'todo' until scoped coverage reaches 95 %"
    )
    assert "id: runtime-data" in quality_scale_text
    assert "id: repair-issues" in quality_scale_text


def test_runtime_data_and_coordinator_usage(integration_root: Path) -> None:
    """__init__.py must wire RuntimeData and GoogleFindMyCoordinator correctly."""

    init_text = (integration_root / "__init__.py").read_text(encoding="utf-8")
    assert "class RuntimeData" in init_text
    assert "from .coordinator import GoogleFindMyCoordinator" in init_text
    assert "entry.runtime_data" in init_text
    assert "RuntimeData(" in init_text


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run a shell command and capture its output for assertions."""

    return subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_mypy_strict_compliance(integration_root: Path) -> None:
    """The integration must pass strict static type checking."""

    repo_root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "-m", "mypy", "custom_components/googlefindmy"]

    result = run_command(cmd, cwd=repo_root)

    error_msg = (
        "Mypy strict type checking failed.\n"
        f"Exit Code: {result.returncode}\n"
        f"Output:\n{result.stdout}\n"
        f"Errors:\n{result.stderr}"
    )

    assert result.returncode == 0, error_msg


def test_ruff_linting_compliance() -> None:
    """The integration must pass ruff linting checks."""

    repo_root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, "-m", "ruff", "check", "."]

    result = run_command(cmd, cwd=repo_root)

    error_msg = (
        "Ruff linting failed.\n"
        "Run 'ruff check . --fix' to automatically fix some of these issues.\n"
        f"Output:\n{result.stdout}\n"
        f"Errors:\n{result.stderr}"
    )

    assert result.returncode == 0, error_msg


def test_quality_scale_evidence_existence(integration_root: Path) -> None:
    """Verify that evidence paths in quality_scale.yaml point to real files."""

    repo_root = Path(__file__).resolve().parent.parent
    quality_yaml_path = integration_root / "quality_scale.yaml"

    if not quality_yaml_path.exists():
        pytest.skip("quality_scale.yaml not found")

    quality_data = yaml.safe_load(quality_yaml_path.read_text(encoding="utf-8"))

    rules = quality_data.get("rules", {}) if isinstance(quality_data, dict) else {}
    missing_files: list[str] = []

    for tier_rules in rules.values():
        if not isinstance(tier_rules, list):
            continue
        for rule in tier_rules:
            evidence_entries = (
                rule.get("evidence", []) if isinstance(rule, dict) else []
            )
            for evidence in evidence_entries:
                if not isinstance(evidence, str):
                    continue
                file_reference = evidence.split(":", 1)[0].split("#", 1)[0]
                full_path = repo_root / file_reference
                if not full_path.exists():
                    missing_files.append(file_reference)

    assert not missing_files, (
        "The following evidence files listed in quality_scale.yaml do not exist:\n"
        + "\n".join(sorted(set(missing_files)))
    )


_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


def _flat_defs(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return the definitions in ``body``, looking into every compound block.

    ``if``, ``try``/``try*`` (with their handlers), ``with``, ``for``, ``while``
    and ``match`` can all hold a definition at module or class level.
    """

    found: list[ast.stmt] = []
    for node in body:
        if isinstance(node, _DEF_NODES):
            found.append(node)
            continue
        for field in _BLOCK_FIELDS:
            for child in getattr(node, field, None) or []:
                if isinstance(child, ast.excepthandler | ast.match_case):
                    found += _flat_defs(child.body)
                elif isinstance(child, ast.stmt):
                    found += _flat_defs([child])
    return found


def _decorator_names(node: ast.stmt) -> set[str]:
    """Return every name and attribute name used in the decorators of ``node``."""

    found: set[str] = set()
    for decorator in getattr(node, "decorator_list", []):
        for sub in ast.walk(decorator):
            if isinstance(sub, ast.Attribute):
                found.add(sub.attr)
            elif isinstance(sub, ast.Name):
                found.add(sub.id)
    return found


def _names_parametrize(expr: ast.expr, aliases: set[str]) -> bool:
    """Return whether ``expr`` is ``parametrize`` or an alias of it.

    Only the root of the expression counts, after stripping calls:
    ``pytest.mark.parametrize(...)``, ``VARIANTS`` and ``p(...)`` with ``p`` an
    alias do, ``pytest.mark.slow`` does not, even if an alias is called ``mark``.
    """

    while isinstance(expr, ast.Call):
        expr = expr.func
    if isinstance(expr, ast.Attribute):
        return expr.attr == "parametrize"
    return isinstance(expr, ast.Name) and expr.id in aliases


def _module_assigns(body: list[ast.stmt]) -> list[ast.Assign | ast.AnnAssign]:
    """Return the assignments of a module body, also inside compound blocks.

    Function and class bodies are left out: a name bound there is not visible
    to a decorator at module level.
    """

    found: list[ast.Assign | ast.AnnAssign] = []
    for node in body:
        if isinstance(node, ast.Assign | ast.AnnAssign):
            if node.value is not None:
                found.append(node)
            continue
        if isinstance(node, _DEF_NODES):
            continue
        for field in _BLOCK_FIELDS:
            for child in getattr(node, field, None) or []:
                if isinstance(child, ast.excepthandler | ast.match_case):
                    found += _module_assigns(child.body)
                elif isinstance(child, ast.stmt):
                    found += _module_assigns([child])
    return found


def _parametrize_aliases(tree: ast.Module) -> set[str]:
    """Return the module-level names bound to ``parametrize`` or a call of it.

    Covers ``VARIANTS = pytest.mark.parametrize(...)`` and ``p =
    pytest.mark.parametrize``, also chained (``q = p``).
    """

    assigns = _module_assigns(tree.body)
    aliases: set[str] = {"parametrize"}
    while True:
        grown = set(aliases)
        for node in assigns:
            assert node.value is not None
            if _names_parametrize(node.value, aliases):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                grown |= {
                    target.id for target in targets if isinstance(target, ast.Name)
                }
        if grown == aliases:
            return aliases
        aliases = grown


def _assigned_names(body: list[ast.stmt]) -> dict[str, ast.expr]:
    """Return the names a class body binds by plain assignment, with their values."""

    bound: dict[str, ast.expr] = {}
    for node in body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            if isinstance(node.target, ast.Name):
                bound[node.target.id] = node.value
    return bound


def _defined_test_names(path: Path) -> dict[str, tuple[bool, bool]]:
    """Return the test items of ``path`` that pytest's default naming collects.

    The result maps each dotted name to ``(is_class, parametrized)``. Items are
    ``test*`` functions that are not fixtures, ``Test*`` classes and
    ``unittest.TestCase`` subclasses, their ``test*`` methods and their nested
    test classes, also inside compound blocks, including methods and marks a
    class inherits from a base class defined in the same module. A ``Test*``
    class is left out when it or a base defines ``__init__`` or ``__new__``,
    sets ``__test__ = False`` or is a dataclass. ``parametrize`` counts as a
    decorator, also through a module-level alias. A base class name resolves to
    the last class of that name in the module. Not modelled, so cite such a
    test without an id or not at all: branches are not evaluated, bases from
    other modules are not followed (any base whose name ends in ``TestCase``
    counts as ``unittest``), ``__test__`` and abstract classes do not change a
    ``unittest.TestCase`` subclass, its nested classes or a class not named
    ``Test*``, a ``__test__ = True`` in a subclass does not undo a base, an id
    on a ``unittest`` method is not rejected, aliases of a function decorator
    other than ``parametrize`` are not followed, and ids from ``pytestmark`` or
    a parametrized fixture are not seen.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    aliases = _parametrize_aliases(tree)
    classes: dict[str, ast.ClassDef] = {}
    names: dict[str, tuple[bool, bool]] = {}

    def _marked(node: ast.stmt) -> bool:
        return any(
            _names_parametrize(decorator, aliases)
            for decorator in getattr(node, "decorator_list", [])
        )

    def _bases(cls: ast.ClassDef, seen: frozenset[str]) -> list[ast.ClassDef]:
        chain: list[ast.ClassDef] = []
        for base in cls.bases:
            if (
                isinstance(base, ast.Name)
                and base.id in classes
                and base.id not in seen
            ):
                parent = classes[base.id]
                chain += [parent, *_bases(parent, seen | {base.id})]
        return chain

    def _is_unittest(cls: ast.ClassDef, chain: list[ast.ClassDef]) -> bool:
        return any(
            (isinstance(base, ast.Name) and base.id.endswith("TestCase"))
            or (isinstance(base, ast.Attribute) and base.attr.endswith("TestCase"))
            for owner in [cls, *chain]
            for base in owner.bases
        )

    def _skipped(cls: ast.ClassDef, chain: list[ast.ClassDef]) -> bool:
        for owner in [cls, *chain]:
            bound = _assigned_names(owner.body)
            defined = {
                node.name
                for node in _flat_defs(owner.body)
                if not isinstance(node, ast.ClassDef)
            }
            if {"__init__", "__new__"} & (defined | set(bound)):
                return True
            test_flag = bound.get("__test__")
            if isinstance(test_flag, ast.Constant) and not test_flag.value:
                return True
            if "dataclass" in _decorator_names(owner):
                return True
        return False

    def _walk(
        defs: list[ast.stmt], prefix: str, marked: bool, inside: frozenset[int]
    ) -> None:
        for node in defs:
            assert isinstance(node, _DEF_NODES)
            dotted = f"{prefix}{node.name}"
            if isinstance(node, ast.ClassDef):
                if id(node) in inside:
                    continue
                chain = _bases(node, frozenset({node.name}))
                unittest_case = _is_unittest(node, chain)
                if not unittest_case and (
                    not node.name.startswith("Test") or _skipped(node, chain)
                ):
                    continue
                class_marked = marked or any(_marked(owner) for owner in [node, *chain])
                names[dotted] = (True, class_marked)
                members = _flat_defs(node.body)
                for owner in chain:
                    members += _flat_defs(owner.body)
                _walk(members, f"{dotted}.", class_marked, inside | {id(node)})
            elif node.name.startswith("test") and "fixture" not in _decorator_names(
                node
            ):
                names.setdefault(dotted, (False, marked or _marked(node)))

    top = _flat_defs(tree.body)
    classes.update((n.name, n) for n in top if isinstance(n, ast.ClassDef))
    _walk(top, "", False, frozenset())
    return names


_TEST_CITATION = re.compile(r"^(tests/[^:#]+\.py):(.*)$")
_TEST_NAME = re.compile(r"^:?([A-Za-z_]\w*(?:(?:::|:|\.)[A-Za-z_]\w*)*)(\[[^\]]*\])?$")


def _unknown_test_citations(
    evidence_entries: list[str], repo_root: Path
) -> tuple[int, list[str]]:
    """Return how many test citations were checked and which ones do not resolve.

    A citation is evidence that starts with ``tests/<module>.py:``. Its rest must
    be a name, separated by ``::``, ``:`` or ``.`` and optionally followed by a
    ``[param]`` id, and that name must be a test item of the module (see
    ``_defined_test_names`` for what it models and what it does not). A
    ``[param]`` id needs a parametrized test function or method; the id itself
    is not checked, because it is only known once pytest collects.
    Anything else after the module (prose, a broken separator) counts as not
    resolving rather than being skipped. Citations of a missing module are left
    to the file check.
    """

    cache: dict[Path, dict[str, tuple[bool, bool]]] = {}
    checked = 0
    unknown: list[str] = []
    for evidence in evidence_entries:
        citation = _TEST_CITATION.match(evidence)
        if citation is None:
            continue
        module = repo_root / citation.group(1)
        if not module.exists():
            continue
        checked += 1
        name = _TEST_NAME.match(citation.group(2))
        if name is None:
            unknown.append(evidence)
            continue
        dotted = re.sub(r"::|:", ".", name.group(1))
        if module not in cache:
            cache[module] = _defined_test_names(module)
        item = cache[module].get(dotted)
        if item is None:
            unknown.append(evidence)
        elif name.group(2) and (item[0] or not item[1]):
            unknown.append(evidence)
    return checked, unknown


def test_quality_scale_test_evidence_names_exist(integration_root: Path) -> None:
    """Verify that test names cited in quality_scale.yaml are defined.

    The file check above passes as long as the test module exists, so a renamed
    test kept its stale citation. Every ``tests/<module>.py:<name>`` citation
    must resolve (see ``_unknown_test_citations``).
    """

    repo_root = Path(__file__).resolve().parent.parent
    quality_yaml_path = integration_root / "quality_scale.yaml"
    if not quality_yaml_path.exists():
        pytest.skip("quality_scale.yaml not found")

    quality_data = yaml.safe_load(quality_yaml_path.read_text(encoding="utf-8"))
    rules = quality_data.get("rules", {}) if isinstance(quality_data, dict) else {}
    evidence_entries = [
        evidence
        for tier_rules in rules.values()
        if isinstance(tier_rules, list)
        for rule in tier_rules
        if isinstance(rule, dict)
        for evidence in rule.get("evidence", [])
        if isinstance(evidence, str)
    ]

    checked, unknown = _unknown_test_citations(evidence_entries, repo_root)

    assert checked, "no test-name evidence found; the pattern no longer matches"
    assert not unknown, (
        "These test names cited in quality_scale.yaml are not defined:\n"
        + "\n".join(sorted(set(unknown)))
    )


_CITATION_MODULE = """
import contextlib
import dataclasses
import sys
import unittest

import pytest

VARIANTS = pytest.mark.parametrize("x", [1])
p = pytest.mark.parametrize
q = p


class TestBase:
    def test_inh(self):
        pass

    def _helper(self):
        pass


class TestSub(TestBase):
    pass


class TestGrandchild(TestSub):
    pass


class TestOuter:
    class TestInner:
        def test_n(self):
            pass


class TestWithInit:
    def __init__(self):
        pass

    def test_never_collected(self):
        pass


@pytest.mark.parametrize("x", [1])
class TestMarked:
    def test_in_marked(self, x):
        pass


if sys.version_info >= (3, 0):

    def test_cond():
        pass


try:

    def test_in_try():
        pass

except ImportError:
    pass

try:
    import _gfmy_absent_module  # noqa: F401
except ImportError:

    def test_in_handler():
        pass


with contextlib.suppress(Exception):

    def test_in_with():
        pass


for _ in range(1):

    def test_in_for():
        pass


_once = True
while _once:

    def test_in_while():
        pass

    _once = False


match sys.platform:
    case _:

        def test_in_match():
            pass


@pytest.mark.parametrize("x", [1])
async def test_async(x):
    pass


def test_plain():
    pass


def helper():
    pass


class TestAliased:
    @VARIANTS
    def test_var(self, x):
        pass


@q("x", [1])
def test_chained(x):
    pass


class TestMarkedSub(TestMarked):
    def test_own(self, x):
        pass


class TestNew:
    def __new__(cls):
        return super().__new__(cls)

    def test_new(self):
        pass


@dataclasses.dataclass
class TestDC:
    def test_dc(self):
        pass


class TestHidden:
    __test__ = False

    def test_h(self):
        pass


class TestLambdaInit:
    __init__ = lambda self: None  # noqa: E731

    def test_l(self):
        pass


class Case(unittest.TestCase):
    def test_u(self):
        pass


@pytest.fixture
def test_fixture_named():
    return 1


local = pytest.mark.filterwarnings("ignore")


def _local_alias():
    local = pytest.mark.parametrize("x", [1])
    return local


mark = pytest.mark.parametrize("y", [1])


@pytest.mark.filterwarnings("ignore")
def test_slow():
    pass


@local
def test_local_named():
    pass


if sys.version_info >= (3, 0):
    IF_VARIANTS = pytest.mark.parametrize("x", [1])


@IF_VARIANTS
def test_if_alias(x):
    pass


class TestRedefined:
    pass


class TestRedefined:  # noqa: F811
    class TestNested(TestRedefined):
        def test_c(self):
            pass
"""


@pytest.mark.parametrize(
    ("citation", "resolves"),
    [
        ("tests/t_mod.py::TestSub::test_inh", True),
        ("tests/t_mod.py::TestSub.test_inh", True),
        ("tests/t_mod.py:TestSub:test_inh", True),
        ("tests/t_mod.py::TestGrandchild::test_inh", True),
        ("tests/t_mod.py::TestOuter::TestInner::test_n", True),
        ("tests/t_mod.py::TestMarked::test_in_marked[1]", True),
        ("tests/t_mod.py::test_cond", True),
        ("tests/t_mod.py::test_in_try", True),
        ("tests/t_mod.py::test_in_handler", True),
        ("tests/t_mod.py::test_in_with", True),
        ("tests/t_mod.py::test_in_for", True),
        ("tests/t_mod.py::test_in_while", True),
        ("tests/t_mod.py::test_in_match", True),
        ("tests/t_mod.py::test_async[1]", True),
        ("tests/t_mod.py::test_plain", True),
        ("tests/t_mod.py::test_plain[case-1]", False),
        ("tests/t_mod.py::helper", False),
        ("tests/t_mod.py::TestBase::_helper", False),
        ("tests/t_mod.py::TestWithInit", False),
        ("tests/t_mod.py::TestWithInit::test_never_collected", False),
        ("tests/t_mod.py::TestAliased::test_var[1]", True),
        ("tests/t_mod.py::test_chained[1]", True),
        ("tests/t_mod.py::TestMarkedSub::test_own[1]", True),
        ("tests/t_mod.py::TestMarkedSub::test_in_marked[1]", True),
        ("tests/t_mod.py::Case::test_u", True),
        ("tests/t_mod.py::TestRedefined::TestNested::test_c", True),
        ("tests/t_mod.py::TestNew::test_new", False),
        ("tests/t_mod.py::TestDC::test_dc", False),
        ("tests/t_mod.py::TestHidden::test_h", False),
        ("tests/t_mod.py::TestLambdaInit::test_l", False),
        ("tests/t_mod.py::test_fixture_named", False),
        ("tests/t_mod.py::TestMarked[1]", False),
        ("tests/t_mod.py::test_slow", True),
        ("tests/t_mod.py::test_slow[x]", False),
        ("tests/t_mod.py::test_local_named[1]", False),
        ("tests/t_mod.py::test_if_alias[1]", True),
        ("tests/t_mod.py::TestBase::test_inh trailing prose", False),
        ("tests/t_mod.py::TestSub::test_missing", False),
        ("tests/t_mod.py::test_missing", False),
        ("tests/t_mod.py::", False),
    ],
)
def test_test_citation_predicate(tmp_path: Path, citation: str, resolves: bool) -> None:
    """A citation resolves only to a test item pytest collects by default naming."""

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_mod.py").write_text(_CITATION_MODULE, encoding="utf-8")

    checked, unknown = _unknown_test_citations([citation], tmp_path)

    assert checked == 1
    assert (unknown == []) is resolves


def test_test_citation_predicate_skips_other_evidence(tmp_path: Path) -> None:
    """Evidence outside ``tests/`` and citations of a missing module are not counted."""

    checked, unknown = _unknown_test_citations(
        ["README.md#x", "custom_components/x.py:f", "tests/absent.py::test_x"],
        tmp_path,
    )

    assert (checked, unknown) == (0, [])


# Sprint baseline for the test-coverage quality-scale rule.
# Real coverage on scoped integration source was 64 % (CI run on b9ca59cd).
# The Platinum target remains >= 95 %; this floor only prevents regression
# while incremental test expansion lands. Raise as the sprint phase-gates
# advance (60 -> 65 -> 75 -> 85 -> 90 -> 95).
COVERAGE_SPRINT_FLOOR = 60
COVERAGE_PLATINUM_TARGET = 95


def test_coverage_threshold_enforced() -> None:
    """Sprint floor for quality-scale rule ``test-coverage`` must be wired.

    Two orthogonal gates must declare a numeric coverage floor of at least
    ``COVERAGE_SPRINT_FLOOR``:

    * ``pyproject.toml`` ``[tool.coverage.report] fail_under`` (engine side)
    * ``.github/workflows/ci.yml`` ``--cov-fail-under`` (CI side)

    Both must NOT exceed ``COVERAGE_PLATINUM_TARGET`` either, so a future
    drift cannot silently re-introduce a 95 % gate above the actual baseline
    and break CI on an unrelated PR. Empirical enforcement happens at CI
    runtime via the pytest flag; this test only verifies the wiring.
    """

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py<3.11 path
        import tomli as tomllib  # type: ignore[no-redef]

    repo_root = Path(__file__).resolve().parent.parent
    pyproject_data = tomllib.loads(
        (repo_root / "pyproject.toml").read_text(encoding="utf-8")
    )
    ci_text = (repo_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    coverage_report = (
        pyproject_data.get("tool", {}).get("coverage", {}).get("report", {})
    )
    fail_under = coverage_report.get("fail_under")
    assert fail_under is not None, (
        "pyproject.toml [tool.coverage.report] is missing fail_under "
        "(required by quality-scale rule test-coverage)"
    )
    assert COVERAGE_SPRINT_FLOOR <= int(fail_under) <= COVERAGE_PLATINUM_TARGET, (
        f"pyproject.toml fail_under must be within "
        f"[{COVERAGE_SPRINT_FLOOR}, {COVERAGE_PLATINUM_TARGET}] "
        f"(current sprint floor; raise as test expansion lands)"
    )

    ci_match = re.search(r"--cov-fail-under=(\d+)", ci_text)
    assert ci_match, (
        ".github/workflows/ci.yml is missing --cov-fail-under flag in the "
        "pytest invocation (required as a CI-side gate)"
    )
    ci_floor = int(ci_match.group(1))
    assert COVERAGE_SPRINT_FLOOR <= ci_floor <= COVERAGE_PLATINUM_TARGET, (
        f"CI --cov-fail-under must be within "
        f"[{COVERAGE_SPRINT_FLOOR}, {COVERAGE_PLATINUM_TARGET}]"
    )
    assert ci_floor == int(fail_under), (
        "pyproject.toml fail_under and CI --cov-fail-under must agree "
        "(both gates must enforce the same sprint floor)"
    )
