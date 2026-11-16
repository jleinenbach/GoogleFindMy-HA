# tests/test_precommit_config_contract.py
"""Keep the pre-commit hooks on the tool versions the CI runs.

The CI lint jobs install ruff, codespell and bandit from ``poetry.lock``; the
hooks in ``.pre-commit-config.yaml`` install them from their ``rev:``. When the
two differ, a local ``pre-commit run`` and the CI report different findings.
The ruff hook had drifted behind the lock twice before this test existed.
``script/local_verify.py`` prints the hook pins next to the local tool version,
so its ``RUFF_PIN`` and ``CODESPELL_PIN`` have to move with the revs as well.

The expected versions come from ``poetry.lock``, not from the hook file, so a
hook that is bumped alone fails just like a lock that is bumped alone. When a
dependency update raises one of the three tools, the same PR bumps the matching
``rev:`` and the pin in ``script/local_verify.py`` (root ``AGENTS.md``, bullet
"Configuration alignment").

Dependabot only raises the lock, so that follow-up is manual. To keep it from
turning the ``python-minor`` group PR red together with unrelated packages, the
pip entry of ``.github/dependabot.yml`` puts the three tools into a group of
their own. Dependabot assigns a dependency to the first group that matches, so
that group has to come before ``python-minor``; a further check pins this.

Further checks pin the configuration defects this file was written for: the
legacy ``ruff`` hook id, which received ``check`` twice and read it as a file
name, a ``check`` element in the ``args`` of the new ``ruff-check`` id, and
``additional_dependencies`` entries such as ``.[dev]``, which a ``repo: local``
hook cannot install because it has no project of its own. The ``mypy`` and
``pytest`` hooks therefore run from the project environment, and the ``mypy``
entry has to start the ``mypy`` executable.

Blind spot: the CI skips the ``mypy`` and ``pytest`` hooks because both run in
jobs of their own, so any other defect in those two entries goes unnoticed
there; only the forms checked here are covered (``language: system`` for both,
the executable of the ``mypy`` entry). The Dependabot check models pattern
matching, exclusions and group order, and rejects ``update-types``,
``applies-to`` and ``dependency-type`` on the tool group outright, also with
values that do not narrow it (``applies-to: version-updates``), because each of
them can send a tool update past that group. It models version updates only: a
group with ``applies-to: security-updates`` elsewhere in the entry is not
modelled (above the tool group it fails the check; security updates of the
tools are not checked). It does not model how Dependabot folds
case or normalises ``-`` and ``_`` in names; ``fnmatchcase`` is stricter, so a
mismatch fails rather than passes. It expects exactly one pip entry.
"""

from __future__ import annotations

import tomllib
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import pytest
import yaml

from script import local_verify

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"
LOCK_PATH = REPO_ROOT / "poetry.lock"
CI_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_BANDIT_STEP = "Run bandit (security)"
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"
# The catch-all pip group of .github/dependabot.yml.
CATCH_ALL_GROUP = "python-minor"
# Group keys that can narrow which updates a group takes. The check rejects them
# on the tool group by presence, since a narrowing value would let a tool update
# fall through to a later group.
NARROWING_GROUP_KEYS = ("update-types", "applies-to", "dependency-type")

# Hook repository of each tool whose rev must equal its version in poetry.lock.
LOCKED_HOOK_REPOS = {
    "ruff": "https://github.com/astral-sh/ruff-pre-commit",
    "codespell": "https://github.com/codespell-project/codespell",
    "bandit": "https://github.com/PyCQA/bandit",
}

# Pins in script/local_verify.py and the tool whose hook rev they mirror.
LOCAL_VERIFY_PINS = {
    "RUFF_PIN": "ruff",
    "CODESPELL_PIN": "codespell",
}

SYSTEM_HOOKS = ("mypy", "pytest")


def _config() -> dict[str, Any]:
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _hooks() -> list[dict[str, Any]]:
    return [hook for repo in _config()["repos"] for hook in repo.get("hooks", [])]


def _rev(repo_url: str) -> str:
    revs = [str(repo["rev"]) for repo in _config()["repos"] if repo["repo"] == repo_url]
    assert len(revs) == 1, f"expected one entry for {repo_url}, found {len(revs)}"
    return revs[0]


def _locked_version(package: str) -> str:
    lock = tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))
    versions = [
        str(entry["version"]) for entry in lock["package"] if entry["name"] == package
    ]
    assert len(versions) == 1, f"expected one {package} entry in poetry.lock"
    return versions[0]


def drift_message(tool: str, found: str, expected: str) -> str:
    """Name both places a contributor has to change, so the failure is the fix."""

    return (
        f"{tool}: found {found}, poetry.lock has {expected}. Bump the `rev:` of "
        f"the {tool} hook in .pre-commit-config.yaml and its pin in "
        f"script/local_verify.py together."
    )


@pytest.mark.parametrize("tool", sorted(LOCKED_HOOK_REPOS))
def test_hook_rev_matches_the_locked_version(tool: str) -> None:
    """A hook rev and the lock entry of the same tool name the same release."""

    rev = _rev(LOCKED_HOOK_REPOS[tool])
    expected = _locked_version(tool)

    assert rev.lstrip("v") == expected, drift_message(tool, f"rev {rev}", expected)


@pytest.mark.parametrize("pin_name", sorted(LOCAL_VERIFY_PINS))
def test_local_verify_pin_matches_the_hook_rev(pin_name: str) -> None:
    """The pin local_verify prints is the rev the hook really installs."""

    tool = LOCAL_VERIFY_PINS[pin_name]
    pin = getattr(local_verify, pin_name)
    rev = _rev(LOCKED_HOOK_REPOS[tool])

    assert pin == rev, drift_message(tool, f"{pin_name} {pin}", f"rev {rev}")


def test_drift_message_names_both_places() -> None:
    """A red dependency update must say what to change and where."""

    message = drift_message("ruff", "rev v0.1.0", "0.2.0")

    assert "`rev:`" in message
    assert ".pre-commit-config.yaml" in message
    assert "script/local_verify.py" in message


def test_no_legacy_ruff_hook_id() -> None:
    """The ``ruff`` alias plus ``args: [check, ...]`` passed ``check`` as a file."""

    ids = [hook["id"] for hook in _hooks()]

    assert "ruff" not in ids
    assert "ruff-check" in ids


def ruff_check_doubled_subcommand(hooks: list[dict[str, Any]]) -> list[str]:
    """Return the ``ruff-check`` hooks whose ``args`` contain ``check``.

    The ``ruff-check`` hook already runs ``ruff check``; a ``check`` anywhere in
    ``args`` is passed on as a file name, exactly the defect of the legacy id.
    """

    return [
        str(hook["id"])
        for hook in hooks
        if hook["id"] == "ruff-check" and "check" in list(hook.get("args", []))
    ]


def mypy_entry_without_mypy(hooks: list[dict[str, Any]]) -> list[str]:
    """Return the ``mypy`` hook entries whose executable is not ``mypy``.

    An entry such as ``true`` passes every run while checking nothing, and the
    CI skips this hook, so nothing else would notice.
    """

    return [
        str(hook.get("entry"))
        for hook in hooks
        if hook["id"] == "mypy" and str(hook.get("entry", "")).split()[:1] != ["mypy"]
    ]


def test_ruff_check_args_do_not_repeat_the_subcommand() -> None:
    """The real configuration passes no ``check`` to ``ruff-check``."""

    assert ruff_check_doubled_subcommand(_hooks()) == []


def test_mypy_entry_runs_mypy() -> None:
    """The real ``mypy`` hook starts the ``mypy`` executable."""

    assert mypy_entry_without_mypy(_hooks()) == []


@pytest.mark.parametrize(
    ("hook", "offending"),
    [
        ({"id": "ruff-check", "args": ["check", "--fix"]}, True),
        ({"id": "ruff-check", "args": ["--fix", "check"]}, True),
        ({"id": "ruff-check", "args": ["--select=check"]}, False),
        ({"id": "ruff-check"}, False),
        ({"id": "ruff-format", "args": ["check"]}, False),
    ],
)
def test_ruff_check_arm_flags_a_check_element(
    hook: dict[str, Any], offending: bool
) -> None:
    """Self-test of the arm: a ``check`` element on ``ruff-check`` counts."""

    assert bool(ruff_check_doubled_subcommand([hook])) is offending


@pytest.mark.parametrize(
    ("entry", "offending"),
    [
        ("mypy --strict -p custom_components.googlefindmy", False),
        ("mypy", False),
        ("true", True),
        ("", True),
        ("python -m mypy", True),
        ("mypyc --strict", True),
    ],
)
def test_mypy_arm_flags_another_executable(entry: str, offending: bool) -> None:
    """Self-test of the arm: the first word of the entry must be ``mypy``."""

    assert bool(mypy_entry_without_mypy([{"id": "mypy", "entry": entry}])) is offending


def test_no_local_install_of_the_project() -> None:
    """A ``repo: local`` hook has no project, so ``.[dev]`` installs nothing."""

    offending = [
        (hook["id"], dependency)
        for hook in _hooks()
        for dependency in hook.get("additional_dependencies", [])
        if str(dependency).startswith(".")
    ]

    assert offending == []


@pytest.mark.parametrize("hook_id", SYSTEM_HOOKS)
def test_environment_hooks_use_the_project_environment(hook_id: str) -> None:
    """mypy and pytest need the project and its plugins, so they run on PATH."""

    matches = [hook for hook in _hooks() if hook["id"] == hook_id]

    assert len(matches) == 1
    assert matches[0]["language"] == "system"


def _ci_bandit_targets() -> list[str]:
    """Return the paths after ``-r`` in the CI bandit step, in order."""

    workflow = yaml.safe_load(CI_PATH.read_text(encoding="utf-8"))
    steps = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == CI_BANDIT_STEP
    ]
    assert len(steps) == 1, f"expected one step named {CI_BANDIT_STEP!r}"
    commands = [
        line.split()
        for line in str(steps[0]["run"]).splitlines()
        if "bandit -r" in line and not line.lstrip().startswith("#")
    ]
    assert len(commands) == 1, "expected one bandit command in the CI step"
    words = commands[0]
    start = words.index("-r") + 1
    targets: list[str] = []
    for word in words[start:]:
        if word.startswith("-"):
            break
        targets.append(word)
    return targets


def _hook_bandit_targets(args: list[str]) -> list[str]:
    """Return the paths after ``-r`` in the bandit hook arguments."""

    start = args.index("-r") + 1
    targets: list[str] = []
    for word in args[start:]:
        if word.startswith("-"):
            break
        targets.append(word)
    return targets


def test_bandit_hook_scans_the_ci_tree_only() -> None:
    """The hook runs the CI job's command: same tree, same configuration.

    With file names appended, bandit also scanned every other Python file and
    reported findings the CI job never sees. With a configuration file of its
    own, the hook skipped other directories and tests than the CI job does.
    Both scan the shipped integration and the developer scripts under
    ``script/``.
    """

    matches = [hook for hook in _hooks() if hook["id"] == "bandit"]

    assert len(matches) == 1
    assert matches[0].get("pass_filenames") is False
    assert matches[0]["args"] == [
        "-c",
        "pyproject.toml",
        "-r",
        "custom_components",
        "script",
    ], (
        "Keep the bandit hook arguments equal to the CI bandit job in "
        ".github/workflows/ci.yml (bandit -r custom_components script -c "
        "pyproject.toml)."
    )


def test_ci_bandit_job_scans_the_hook_tree() -> None:
    """The CI step and the hook name the same trees, read from both files."""

    hook = next(hook for hook in _hooks() if hook["id"] == "bandit")

    assert _ci_bandit_targets() == _hook_bandit_targets(list(hook["args"]))
    assert _ci_bandit_targets() == ["custom_components", "script"]


def _pip_entry(config: dict[str, Any]) -> dict[str, Any]:
    entries = [
        entry
        for entry in config.get("updates", [])
        if entry.get("package-ecosystem") == "pip"
    ]
    assert len(entries) == 1, f"expected one pip entry, found {len(entries)}"
    return entries[0]


def first_pip_group(config: dict[str, Any], package: str) -> str | None:
    """Return the first pip group whose name patterns take ``package``.

    Dependabot puts a dependency into the first group whose ``patterns`` match
    and whose ``exclude-patterns`` do not. Only the name patterns are modelled
    here; the keys that narrow a group further are checked by
    ``pinned_tool_group_defects``.
    """

    for name, spec in (_pip_entry(config).get("groups") or {}).items():
        patterns = [str(pattern) for pattern in spec.get("patterns", [])]
        excluded = [str(pattern) for pattern in spec.get("exclude-patterns", [])]
        if any(fnmatchcase(package, pattern) for pattern in patterns) and not any(
            fnmatchcase(package, pattern) for pattern in excluded
        ):
            return str(name)
    return None


def pinned_tool_group_defects(config: dict[str, Any]) -> list[str]:
    """Return the tools whose Dependabot group setup lets them mix with others.

    A tool must land in a group other than the catch-all, and that group may
    only name tools from ``LOCKED_HOOK_REPOS``: a wildcard there would pull
    unrelated updates back into the PR that needs the manual pin follow-up.
    The group must not set a key from ``NARROWING_GROUP_KEYS``: a group for
    security updates only, for production dependencies only or for some
    SemVer levels only would let a tool update reach ``python-minor`` again.
    """

    groups = _pip_entry(config).get("groups") or {}
    defects: list[str] = []
    for tool in sorted(LOCKED_HOOK_REPOS):
        group = first_pip_group(config, tool)
        if group is None or group == CATCH_ALL_GROUP:
            defects.append(f"{tool}: in group {group!r}")
            continue
        foreign = [
            str(pattern)
            for pattern in groups[group].get("patterns", [])
            if str(pattern) not in LOCKED_HOOK_REPOS
        ]
        if foreign:
            defects.append(f"{tool}: group {group!r} also matches {foreign}")
            continue
        narrowing = [key for key in NARROWING_GROUP_KEYS if key in groups[group]]
        if narrowing:
            defects.append(
                f"{tool}: group {group!r} sets {narrowing}; leave these keys off"
            )
    return defects


def _dependabot() -> dict[str, Any]:
    loaded = yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize("tool", sorted(LOCKED_HOOK_REPOS))
def test_dependabot_keeps_the_tool_out_of_python_minor(tool: str) -> None:
    """Each pinned tool gets its update PR apart from the catch-all group."""

    defects = [
        defect
        for defect in pinned_tool_group_defects(_dependabot())
        if defect.startswith(f"{tool}:")
    ]

    assert defects == [], (
        f"{defects}. Give ruff, codespell and bandit a pip group of their own "
        f"above {CATCH_ALL_GROUP!r} in .github/dependabot.yml (AGENTS.md, "
        f'"Configuration alignment").'
    )


def _pip_config(groups: dict[str, Any]) -> dict[str, Any]:
    return {"updates": [{"package-ecosystem": "pip", "groups": groups}]}


_CATCH_ALL = {"patterns": ["*"], "update-types": ["minor", "patch"]}
_TOOLS = {"patterns": ["ruff", "codespell", "bandit"]}


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        ({CATCH_ALL_GROUP: _CATCH_ALL}, ["bandit", "codespell", "ruff"]),
        (
            {CATCH_ALL_GROUP: _CATCH_ALL, "pre-commit-tools": _TOOLS},
            ["bandit", "codespell", "ruff"],
        ),
        (
            {"pre-commit-tools": {"patterns": ["*"]}, CATCH_ALL_GROUP: _CATCH_ALL},
            ["bandit", "codespell", "ruff"],
        ),
        (
            {
                "pre-commit-tools": {"patterns": ["ruff", "codespell"]},
                CATCH_ALL_GROUP: _CATCH_ALL,
            },
            ["bandit"],
        ),
        (
            {
                "pre-commit-tools": {**_TOOLS, "exclude-patterns": ["ruff"]},
                CATCH_ALL_GROUP: _CATCH_ALL,
            },
            ["ruff"],
        ),
        (
            {
                "pre-commit-tools": {**_TOOLS, "update-types": ["major"]},
                CATCH_ALL_GROUP: _CATCH_ALL,
            },
            ["bandit", "codespell", "ruff"],
        ),
        (
            {
                "pre-commit-tools": {**_TOOLS, "applies-to": "security-updates"},
                CATCH_ALL_GROUP: _CATCH_ALL,
            },
            ["bandit", "codespell", "ruff"],
        ),
        (
            {
                "pre-commit-tools": {**_TOOLS, "dependency-type": "production"},
                CATCH_ALL_GROUP: _CATCH_ALL,
            },
            ["bandit", "codespell", "ruff"],
        ),
        ({"pre-commit-tools": _TOOLS, CATCH_ALL_GROUP: _CATCH_ALL}, []),
    ],
    ids=[
        "catch-all-only",
        "tool-group-below-catch-all",
        "wildcard-tool-group",
        "tool-missing",
        "tool-excluded",
        "tool-group-update-types",
        "tool-group-security-only",
        "tool-group-dependency-type",
        "target-form",
    ],
)
def test_pinned_tool_group_arm(groups: dict[str, Any], expected: list[str]) -> None:
    """Self-test of the arm: order, wildcards, gaps, exclusions, narrowing."""

    defects = pinned_tool_group_defects(_pip_config(groups))

    assert [defect.split(":")[0] for defect in defects] == expected
