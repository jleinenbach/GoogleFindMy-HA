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


def _flat_defs(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return the definitions in ``body``, looking into ``if`` and ``try`` blocks."""

    found: list[ast.stmt] = []
    for node in body:
        if isinstance(node, _DEF_NODES):
            found.append(node)
        elif isinstance(node, ast.If):
            found += _flat_defs(node.body) + _flat_defs(node.orelse)
        elif isinstance(node, ast.Try):
            found += _flat_defs(node.body) + _flat_defs(node.orelse)
            found += _flat_defs(node.finalbody)
            for handler in node.handlers:
                found += _flat_defs(handler.body)
    return found


def _defined_test_names(path: Path) -> set[str]:
    """Return every dotted name pytest can collect from ``path``.

    Functions and classes at module level (also inside ``if``/``try``), nested
    classes, and methods, including the ones a class inherits from a base class
    defined in the same module. Bases from other modules are not followed.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    classes: dict[str, ast.ClassDef] = {}
    names: set[str] = set()

    def _members(cls: ast.ClassDef, seen: frozenset[str]) -> list[ast.stmt]:
        own = _flat_defs(cls.body)
        inherited: list[ast.stmt] = []
        for base in cls.bases:
            if (
                isinstance(base, ast.Name)
                and base.id in classes
                and base.id not in seen
            ):
                inherited += _members(classes[base.id], seen | {base.id})
        return own + inherited

    def _walk(defs: list[ast.stmt], prefix: str) -> None:
        for node in defs:
            assert isinstance(node, _DEF_NODES)
            dotted = f"{prefix}{node.name}"
            names.add(dotted)
            if isinstance(node, ast.ClassDef):
                _walk(_members(node, frozenset({node.name})), f"{dotted}.")

    top = _flat_defs(tree.body)
    classes.update((n.name, n) for n in top if isinstance(n, ast.ClassDef))
    _walk(top, "")
    return names


_TEST_CITATION = re.compile(r"^(tests/[^:#]+\.py):(.*)$")
_TEST_NAME = re.compile(
    r"^:?([A-Za-z_]\w*(?:(?:::|:|\.)[A-Za-z_]\w*)*)(?:\[[^\]]*\])?$"
)


def _unknown_test_citations(
    evidence_entries: list[str], repo_root: Path
) -> tuple[int, list[str]]:
    """Return how many test citations were checked and which ones do not resolve.

    A citation is evidence that starts with ``tests/<module>.py:``. Its rest must
    be a name, separated by ``::``, ``:`` or ``.`` and optionally followed by a
    ``[param]`` id, and that name must be defined in the module. Anything else
    after the module (prose, a broken separator) counts as not resolving rather
    than being skipped. Citations of a missing module are left to the file
    check.
    """

    cache: dict[Path, set[str]] = {}
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
        if dotted not in cache[module]:
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
import sys


class TestBase:
    def test_inh(self):
        pass


class TestSub(TestBase):
    pass


class TestOuter:
    class TestInner:
        def test_n(self):
            pass


if sys.version_info >= (3, 0):

    def test_cond():
        pass


async def test_async():
    pass
"""


@pytest.mark.parametrize(
    ("citation", "resolves"),
    [
        ("tests/t_mod.py::TestSub::test_inh", True),
        ("tests/t_mod.py::TestSub.test_inh", True),
        ("tests/t_mod.py:TestSub:test_inh", True),
        ("tests/t_mod.py::TestOuter::TestInner::test_n", True),
        ("tests/t_mod.py::test_cond", True),
        ("tests/t_mod.py::test_async[case-1]", True),
        ("tests/t_mod.py::TestBase::test_inh trailing prose", False),
        ("tests/t_mod.py::TestSub::test_missing", False),
        ("tests/t_mod.py::test_missing", False),
        ("tests/t_mod.py::", False),
    ],
)
def test_test_citation_predicate(tmp_path: Path, citation: str, resolves: bool) -> None:
    """Each citation form pytest accepts resolves; prose or a typo does not."""

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
