# tests/test_platinum_compliance.py
"""Automated compliance checks for the declared HA quality-scale tier.

The file name and the historical constant ``COVERAGE_PLATINUM_TARGET`` are
kept for git-blame continuity and to mark Platinum as the long-term target;
the currently declared tier is Bronze during the coverage sprint (see
``test_quality_scale_declares_bronze_status``).
"""

from __future__ import annotations

import os
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

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


_TEST_CITATION = re.compile(r"^(tests/[^:#]+\.py):(.*)$")
_TEST_NAME = re.compile(r"^:?([A-Za-z_]\w*(?:(?:::|:|\.)[A-Za-z_]\w*)*)(\[[^\]]*\])?$")
_COLLECT_TIMEOUT_S = 600
_STDERR_SEPARATOR = "\n--- stderr ---\n"


def _collected_node_ids(modules: list[str], root: Path) -> set[str]:
    """Return the node ids pytest collects from ``modules`` (paths relative to ``root``).

    pytest itself decides what a test item is, so the guard does not model the
    collection rules (naming, ``__init__``, fixtures, dataclasses, unittest,
    markers, aliases, inheritance). The run ignores ``addopts`` and the cache and
    drops the variables through which the outer run would reach it
    (``PYTEST_*``, such as ``PYTEST_ADDOPTS``; ``COVERAGE_PROCESS_*``, through
    which coverage.py measures subprocesses; ``COV_CORE_*`` for pytest-cov
    before 7). Only lines that start with a requested module (normalized, as
    pytest prints it) and ``::`` count as node ids, so an indented tree listing
    or a warning location cannot pass for one. Limits: an option the project adds
    to ``addopts`` that changes collection is not applied here, and an id
    containing ``]`` cannot be cited.
    A failed or timed-out collection, or one that lists no node id although it
    succeeded, fails the calling test with pytest's output.
    """

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("COV_CORE_", "COVERAGE_PROCESS_", "PYTEST_"))
    }
    command = [
        sys.executable,
        "-m",
        "pytest",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        "-o",
        "addopts=",
        "--rootdir",
        str(root),
        *modules,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=_COLLECT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as err:
        pytest.fail(
            f"pytest --collect-only timed out after {_COLLECT_TIMEOUT_S} s:\n"
            + _as_text(err.stdout)[-4000:]
            + _STDERR_SEPARATOR
            + _as_text(err.stderr)[-4000:]
        )
    prefixes = tuple(f"{posixpath.normpath(module)}::" for module in modules)
    node_ids = {
        line for line in result.stdout.splitlines() if line.startswith(prefixes)
    }
    if result.returncode not in (0, 5) or (result.returncode == 0 and not node_ids):
        pytest.fail(
            f"pytest --collect-only failed with rc {result.returncode}"
            f" and {len(node_ids)} node ids:\n"
            + result.stdout[-4000:]
            + _STDERR_SEPARATOR
            + result.stderr[-4000:]
        )
    return node_ids


def _as_text(output: str | bytes | None) -> str:
    """Return captured subprocess output as text.

    On POSIX, ``TimeoutExpired`` carries the output captured so far as ``bytes``
    even when the run used ``text=True``; on Windows it is already ``str``.
    """

    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def _unknown_test_citations(
    evidence_entries: list[str],
    repo_root: Path,
    node_ids: set[str] | None = None,
) -> tuple[int, list[str]]:
    """Return how many test citations were checked and which ones do not resolve.

    A citation is evidence that starts with ``tests/<module>.py:``. Its rest must
    be a name, separated by ``::``, ``:`` or ``.`` and optionally followed by a
    ``[param]`` id. It resolves if pytest collects an item with that node id;
    without an id it also resolves to the items of a parametrized test or of a
    class. Anything else after the module (prose, a broken separator) counts as
    not resolving. Citations of a missing module are left to the file check.
    ``node_ids`` replaces the collection run (for tests of this function).
    """

    cited: list[tuple[str, str]] = []
    for evidence in evidence_entries:
        citation = _TEST_CITATION.match(evidence)
        if citation is None or not (repo_root / citation.group(1)).exists():
            continue
        cited.append((evidence, citation.group(1)))
    if node_ids is None and cited:
        node_ids = _collected_node_ids(sorted({m for _, m in cited}), repo_root)

    unknown: list[str] = []
    for evidence, module in cited:
        name = _TEST_NAME.match(evidence[len(module) + 1 :])
        if name is None:
            unknown.append(evidence)
            continue
        node = module + "::" + re.sub(r"::|:|\.", "::", name.group(1))
        if name.group(2):
            found = node + name.group(2) in (node_ids or set())
        else:
            found = any(
                item == node or item.startswith((node + "[", node + "::"))
                for item in node_ids or set()
            )
        if not found:
            unknown.append(evidence)
    return len(cited), unknown


def test_quality_scale_test_evidence_names_exist(integration_root: Path) -> None:
    """Verify that test names cited in quality_scale.yaml are collected by pytest.

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
        "These test names cited in quality_scale.yaml are not collected:\n"
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
def _passthrough(func):
    return func


def _ignoring(_arg):
    def decorate(func):
        return func

    return decorate


@_passthrough(pytest.fixture)
def test_passthrough_fixture():
    pass


@_ignoring(pytest.fixture)
def test_wrapped_fixture_arg():
    pass


@pytest.mark.dataclass
class TestMarkedDataclassName:
    def test_m(self):
        pass


class TestClassAlias:
    mark = pytest.mark.parametrize("x", [1])

    @mark
    def test_class_alias(self, x):
        pass


rebound = pytest.mark.parametrize("x", [1])
rebound = pytest.mark.filterwarnings("ignore")


@rebound
def test_rebound():
    pass


@pytest.mark.parametrize("a, b", [(1, 2)], ids=["pair"])
def test_with_ids(a, b):
    pass
"""


@pytest.fixture(name="citation_module", scope="module")
def fixture_citation_module(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, set[str]]:
    """Write ``_CITATION_MODULE`` once and collect it with the real pytest."""

    root = tmp_path_factory.mktemp("citations")
    (root / "tests").mkdir()
    (root / "tests" / "t_mod.py").write_text(_CITATION_MODULE, encoding="utf-8")
    return root, _collected_node_ids(["tests/t_mod.py"], root)


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
        ("tests/t_mod.py::test_wrapped_fixture_arg", True),
        ("tests/t_mod.py::test_passthrough_fixture", False),
        ("tests/t_mod.py::TestMarkedDataclassName::test_m", True),
        ("tests/t_mod.py::TestClassAlias::test_class_alias[1]", True),
        ("tests/t_mod.py::test_rebound", True),
        ("tests/t_mod.py::test_rebound[1]", False),
        ("tests/t_mod.py::test_with_ids[pair]", True),
        ("tests/t_mod.py::test_with_ids[1-2]", False),
        ("tests/t_mod.py::TestSub", True),
    ],
)
def test_test_citation_predicate(
    citation_module: tuple[Path, set[str]], citation: str, resolves: bool
) -> None:
    """A citation resolves only to an item pytest collects from the module."""

    root, node_ids = citation_module
    checked, unknown = _unknown_test_citations([citation], root, node_ids)

    assert checked == 1
    assert (unknown == []) is resolves


def test_test_citation_predicate_skips_other_evidence(tmp_path: Path) -> None:
    """Evidence outside ``tests/`` and citations of a missing module are not counted."""

    checked, unknown = _unknown_test_citations(
        ["README.md#x", "custom_components/x.py:f", "tests/absent.py::test_x"],
        tmp_path,
    )

    assert (checked, unknown) == (0, [])


def test_collected_node_ids_fails_loudly(tmp_path: Path) -> None:
    """A module pytest cannot collect fails the guard instead of resolving nothing."""

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_bad.py").write_text("def test_x(:\n", encoding="utf-8")

    with pytest.raises(pytest.fail.Exception, match="rc 2"):
        _collected_node_ids(["tests/t_bad.py"], tmp_path)


def test_collected_node_ids_ignores_outer_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Project ``addopts``, ``PYTEST_ADDOPTS`` and subprocess coverage do not reach the run.

    Both ``COVERAGE_PROCESS_START`` and ``COVERAGE_PROCESS_CONFIG`` (set by
    coverage.py 7.10.3+ with ``patch = subprocess``) start a measurement on their
    own through the ``.pth`` file coverage.py ships from 7.12.1 on. Before that
    neither variable starts one here, so the probe has nothing to catch there.
    """

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_ok.py").write_text(
        "def test_x():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts = ["--no-such-option-addopts"]\n',
        encoding="utf-8",
    )
    rcfile = tmp_path / "coveragerc"
    rcfile.write_text(
        f"[run]\ndata_file = {tmp_path / 'cov' / 'data'}\n", encoding="utf-8"
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "--no-such-option-env")
    monkeypatch.setenv("COVERAGE_PROCESS_START", str(rcfile))
    config_value = _coverage_process_config(tmp_path / "cov" / "data")
    if config_value is not None:
        monkeypatch.setenv("COVERAGE_PROCESS_CONFIG", config_value)

    assert _collected_node_ids(["tests/t_ok.py"], tmp_path) == {"tests/t_ok.py::test_x"}
    assert not (tmp_path / "cov").exists()


def _coverage_process_config(data_file: Path) -> str | None:
    """Return a ``COVERAGE_PROCESS_CONFIG`` value for ``data_file``, if supported.

    ``coverage.config`` is not a documented API and ``serialize`` exists from
    coverage.py 7.10.3 on, so the import stays local to this test bench and a
    missing piece returns ``None`` instead of breaking the whole module.
    """

    try:
        from coverage.config import CoverageConfig
    except ImportError:
        return None
    config = CoverageConfig()
    if not hasattr(config, "serialize"):
        return None
    config.data_file = str(data_file)
    return str(config.serialize())


def test_collected_node_ids_normalizes_the_module_path(tmp_path: Path) -> None:
    """A module given as ``tests/./x.py`` still matches pytest's ``tests/x.py`` ids."""

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_ok.py").write_text(
        "def test_x():\n    pass\n", encoding="utf-8"
    )

    assert _collected_node_ids(["tests/./t_ok.py"], tmp_path) == {
        "tests/t_ok.py::test_x"
    }


def test_collected_node_ids_rejects_an_empty_listing(tmp_path: Path) -> None:
    """A collection that succeeds but lists no node id fails instead of resolving nothing."""

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_ok.py").write_text(
        "def test_x():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\nverbosity_test_cases = 1\n", encoding="utf-8"
    )

    with pytest.raises(pytest.fail.Exception, match="rc 0 and 0 node ids"):
        _collected_node_ids(["tests/t_ok.py"], tmp_path)


def test_collected_node_ids_ignores_an_indented_tree_listing(tmp_path: Path) -> None:
    """Neither a tree line with ``::`` in its id nor a warning location counts as a node id."""

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_ok.py").write_text(
        "import pytest\n\n\n"
        '@pytest.mark.parametrize("v", ["a::b"])\n'
        "def test_x(v):\n    pass\n\n\n"
        "class TestWarns:\n"
        "    def __init__(self):\n        pass\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\nverbosity_test_cases = 1\n", encoding="utf-8"
    )

    with pytest.raises(pytest.fail.Exception, match="rc 0 and 0 node ids"):
        _collected_node_ids(["tests/t_ok.py"], tmp_path)


def test_collected_node_ids_timeout_shows_both_streams_as_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The timeout message decodes the captured ``bytes`` and includes stderr."""

    def _timeout(*args: object, **kwargs: object) -> NoReturn:
        raise subprocess.TimeoutExpired(
            cmd="pytest", timeout=1, output=b"out-marker", stderr=b"err-marker"
        )

    monkeypatch.setattr(subprocess, "run", _timeout)

    with pytest.raises(pytest.fail.Exception) as excinfo:
        _collected_node_ids(["tests/t_ok.py"], tmp_path)

    message = str(excinfo.value)
    assert "out-marker" in message
    assert "err-marker" in message
    assert "b'" not in message
    assert "out-marker" + _STDERR_SEPARATOR + "err-marker" in message


def test_as_text_accepts_every_capture_type() -> None:
    """``bytes`` is decoded, ``str`` passes through and ``None`` becomes empty."""

    assert _as_text(b"a\xffb") == "a\ufffdb"
    assert _as_text("text") == "text"
    assert _as_text(None) == ""


def test_collected_node_ids_reports_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A collection that runs into the time limit fails with its own message."""

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "t_ok.py").write_text(
        "def test_x():\n    pass\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "_COLLECT_TIMEOUT_S", 0.001)

    with pytest.raises(pytest.fail.Exception, match="timed out after"):
        _collected_node_ids(["tests/t_ok.py"], tmp_path)


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
