# tests/test_release_propose_step.py
"""Behavioural tests for the proposal step of ``.github/workflows/release.yml``.

The step ``Propose a version and open a draft release`` asks semantic-release
twice (``--print-last-released`` and ``--print``) and then opens a draft
release. It must keep two situations apart that look alike from the outside:

* semantic-release *answers* but has nothing to propose (nothing due, no
  release tag yet, a branch outside ``[tool.semantic_release.branches]``).
  semantic-release exits 0 in all of these, and the step stays green with a
  ``::notice::``.
* semantic-release *crashes*, rejects its command line or refuses to run
  (exit 1 for a crash, exit 2 for a click usage error, for example an option
  renamed by a dependency bump; exit 1 as well for the refusal "Detached HEAD
  ... no release will be made" on a run dispatched on a tag ref). The step
  must fail with an ``::error::`` line and the captured stderr, and must not
  open a draft.

These tests execute the REAL ``run`` block, extracted from the workflow with
``yaml.safe_load`` and started as ``bash -e`` like the GitHub runner does for
``run`` steps without an explicit ``shell``. ``poetry``, ``git`` and ``gh`` are
stubs on ``PATH``. Reach, stated so it is not mistaken for more: the stubs
simulate semantic-release, git and the GitHub CLI; they do not prove how the
real tools behave. The exit-code contract they encode is the one of
python-semantic-release 10.6.1 (``src/semantic_release/cli/commands/version.py``
and click): an answer exits 0 even when it is empty, a crash exits 1, a click
usage error exits 2. The runner's handling of workflow commands and of
``RUNNER_TEMP`` is outside this test as well.

Every stub logs its calls to a file, not to stdout: the stubs run inside
``$(...)``, so a marker on stdout would land in the captured variable rather
than in the step output, and an unexpected call could go unnoticed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release.yml"
_STEP_NAME = "Propose a version and open a draft release"
# The env keys the step declares; the stub environment provides exactly these.
_STEP_ENV = {"GH_TOKEN": "stub-token", "REF_NAME": "main"}

_LAST = "run semantic-release version --print-last-released"
_PRINT = "run semantic-release version --print"
_DESCRIBE = "describe --tags --abbrev=0 HEAD"

_POETRY_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_POETRY_LOG"
echo "STUB-NOISE" >&2
case "$*" in
  "run semantic-release version --print-last-released")
    out="$STUB_LAST_OUT"; rc="$STUB_LAST_RC" ;;
  "run semantic-release version --print")
    out="$STUB_PRINT_OUT"; rc="$STUB_PRINT_RC" ;;
  *)
    echo "STUB-UNEXPECTED: poetry $*" >&2
    printf 'poetry %s\\n' "$*" >> "$STUB_UNEXPECTED_LOG"
    exit 99 ;;
esac
if [ "$rc" -ne 0 ]; then
  echo "STUB-CRASH-TRACE" >&2
  exit "$rc"
fi
if [ -n "$out" ]; then
  printf '%s\\n' "$out"
fi
exit 0
"""

_GIT_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_GIT_LOG"
if [ "$*" != "describe --tags --abbrev=0 HEAD" ]; then
  echo "STUB-UNEXPECTED: git $*" >&2
  printf 'git %s\\n' "$*" >> "$STUB_UNEXPECTED_LOG"
  exit 99
fi
if [ -z "$STUB_TIP" ]; then
  echo "fatal: No names found, cannot describe anything." >&2
  exit 128
fi
printf '%s\\n' "$STUB_TIP"
"""

_GH_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$STUB_GH_LOG"
case "$1 $2" in
  "release view") exit "${STUB_GH_VIEW_RC:-1}" ;;
  "release create") exit 0 ;;
esac
echo "STUB-UNEXPECTED: gh $*" >&2
printf 'gh %s\\n' "$*" >> "$STUB_UNEXPECTED_LOG"
exit 99
"""


@dataclass(frozen=True)
class _Scenario:
    """One stubbed situation and the step behaviour it must produce."""

    tip: str
    last_out: str
    last_rc: int
    print_out: str
    print_rc: int
    expected_rc: int
    poetry_calls: tuple[str, ...]
    gh_creates: tuple[str, ...]
    stdout_has: tuple[str, ...] = ()
    stdout_lacks: tuple[str, ...] = ()


_PROPOSE_2_0_0 = (
    "release create 2.0.0 --draft --target main --generate-notes --title 2.0.0"
)

# Scenario ids that contain "crash" are exactly the crash cases, so
# ``pytest -k crash`` selects them; no function, class or module name in this
# file may contain "crash", "non_release" or "static" except the static guard.
_SCENARIOS: dict[str, _Scenario] = {
    "a_propose": _Scenario(
        tip="1.7.15",
        last_out="1.7.15",
        last_rc=0,
        print_out="2.0.0",
        print_rc=0,
        expected_rc=0,
        poetry_calls=(_LAST, _PRINT),
        gh_creates=(_PROPOSE_2_0_0,),
        stdout_lacks=("::error::",),
    ),
    "b_nothing_due": _Scenario(
        tip="1.7.15",
        last_out="1.7.15",
        last_rc=0,
        print_out="1.7.15",
        print_rc=0,
        expected_rc=0,
        poetry_calls=(_LAST, _PRINT),
        gh_creates=(),
        stdout_has=("::notice::No release due",),
        stdout_lacks=("::error::",),
    ),
    "c_print_crash": _Scenario(
        tip="1.7.15",
        last_out="1.7.15",
        last_rc=0,
        print_out="",
        print_rc=1,
        expected_rc=1,
        poetry_calls=(_LAST, _PRINT),
        gh_creates=(),
        stdout_has=(
            "::error::'poetry run semantic-release version --print' failed with exit code 1",
        ),
        stdout_lacks=("::notice::No release due",),
    ),
    "d_last_released_crash": _Scenario(
        tip="1.7.15.18",
        last_out="",
        last_rc=1,
        print_out="",
        print_rc=0,
        expected_rc=1,
        poetry_calls=(_LAST,),
        gh_creates=(),
        stdout_has=(
            "::error::'poetry run semantic-release version --print-last-released' failed with exit code 1",
        ),
        stdout_lacks=("::notice::No release due", "hand-tag draft"),
    ),
    "d2_last_released_crash_no_tags": _Scenario(
        tip="",
        last_out="",
        last_rc=1,
        print_out="2.0.0",
        print_rc=0,
        expected_rc=1,
        poetry_calls=(_LAST,),
        gh_creates=(),
        stdout_has=(
            "::error::'poetry run semantic-release version --print-last-released' failed with exit code 1",
        ),
        stdout_lacks=("::notice::No release due", "Proposed next version"),
    ),
    "d3_last_released_usage_error_crash": _Scenario(
        tip="1.7.15.18",
        last_out="",
        last_rc=2,
        print_out="",
        print_rc=0,
        expected_rc=2,
        poetry_calls=(_LAST,),
        gh_creates=(),
        stdout_has=(
            "::error::'poetry run semantic-release version --print-last-released' failed with exit code 2",
        ),
        stdout_lacks=("::notice::No release due", "hand-tag draft"),
    ),
    "h_print_usage_error_crash": _Scenario(
        tip="1.7.15",
        last_out="1.7.15",
        last_rc=0,
        print_out="",
        print_rc=2,
        expected_rc=2,
        poetry_calls=(_LAST, _PRINT),
        gh_creates=(),
        stdout_has=(
            "::error::'poetry run semantic-release version --print' failed with exit code 2",
        ),
        stdout_lacks=("::notice::No release due",),
    ),
    "e_no_tags": _Scenario(
        tip="",
        last_out="",
        last_rc=0,
        print_out="1.0.0",
        print_rc=0,
        expected_rc=0,
        poetry_calls=(_LAST, _PRINT),
        gh_creates=(
            "release create 1.0.0 --draft --target main --generate-notes --title 1.0.0",
        ),
        stdout_lacks=("::error::",),
    ),
    "f_pep440_tip_hand": _Scenario(
        tip="1.7.15.18",
        last_out="1.7.15",
        last_rc=0,
        print_out="",
        print_rc=0,
        expected_rc=0,
        poetry_calls=(_LAST,),
        gh_creates=(
            "release create set-release-tag --draft --target main --generate-notes "
            "--title DRAFT - replace tag with your version (e.g. 1.7.14 or 1.7.14b1) "
            "before publishing",
        ),
        stdout_lacks=("::error::",),
    ),
    "g_non_release_branch": _Scenario(
        tip="1.7.15",
        last_out="1.7.15",
        last_rc=0,
        print_out="",
        print_rc=0,
        expected_rc=0,
        poetry_calls=(_LAST, _PRINT),
        gh_creates=(),
        stdout_has=("::notice::semantic-release proposes nothing for main",),
        stdout_lacks=("No release due", "::error::"),
    ),
}


def _propose_step() -> dict[str, Any]:
    """Return the one proposal step of ``release.yml``; refuse anything else."""

    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["release"]
    # The test starts the block as `bash -e`, the runner's form for a run step
    # without an explicit shell; any shell setting would make that a different
    # interpreter from the one the runner uses.
    for scope, owner in (("workflow", workflow), ("job", job)):
        shell = (owner.get("defaults") or {}).get("run", {}).get("shell")
        assert shell is None, f"{scope} sets defaults.run.shell={shell!r}"
    steps = [step for step in job["steps"] if step.get("name") == _STEP_NAME]
    assert len(steps) == 1, (
        f"expected exactly one step named {_STEP_NAME!r}, got {len(steps)}"
    )
    step: dict[str, Any] = steps[0]
    assert "shell" not in step, f"the step sets shell={step['shell']!r}"
    return step


def _propose_run_block() -> str:
    """Return the ``run`` script of the proposal step, checked for runnability."""

    step = _propose_step()
    run = step["run"]
    assert isinstance(run, str)
    # An expression would be substituted by the runner before bash sees the
    # script; executing it verbatim here would test something else.
    assert "${{" not in run, "the run block must reach its inputs through env only"
    assert set(step["env"]) == set(_STEP_ENV), (
        "the step's env changed; extend _STEP_ENV to match"
    )
    return run


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    stub = bin_dir / name
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)


def _log_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


@dataclass(frozen=True)
class _StepRun:
    """Outcome of one execution of the proposal step."""

    returncode: int
    output: str
    poetry_calls: list[str]
    git_calls: list[str]
    gh_creates: list[str]
    unexpected_calls: list[str]


def _run_step(tmp_path: Path, scenario: _Scenario, runner_temp: Path) -> _StepRun:
    """Execute the real proposal step under ``bash -e`` against the stubs."""

    bash = shutil.which("bash")
    # Deliberately no skip: a skipped behaviour test would be a silent pass.
    assert bash is not None, "bash is required to execute the workflow step"

    script = tmp_path / "step.sh"
    script.write_text(_propose_run_block(), encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir, "poetry", _POETRY_STUB)
    _write_stub(bin_dir, "git", _GIT_STUB)
    _write_stub(bin_dir, "gh", _GH_STUB)

    logs = tmp_path / "logs"
    logs.mkdir()
    poetry_log = logs / "poetry.log"
    git_log = logs / "git.log"
    gh_log = logs / "gh.log"
    unexpected_log = logs / "unexpected.log"

    # Built from scratch, without os.environ, so nothing from the calling shell
    # or from CI (BASH_ENV, GITHUB_*) reaches the step.
    env = {
        **_STEP_ENV,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(runner_temp),
        "STUB_TIP": scenario.tip,
        "STUB_LAST_OUT": scenario.last_out,
        "STUB_LAST_RC": str(scenario.last_rc),
        "STUB_PRINT_OUT": scenario.print_out,
        "STUB_PRINT_RC": str(scenario.print_rc),
        "STUB_POETRY_LOG": str(poetry_log),
        "STUB_GIT_LOG": str(git_log),
        "STUB_GH_LOG": str(gh_log),
        "STUB_UNEXPECTED_LOG": str(unexpected_log),
    }

    proc = subprocess.run(
        [bash, "-e", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
        env=env,
    )
    return _StepRun(
        returncode=proc.returncode,
        output=proc.stdout.decode("utf-8", errors="replace"),
        poetry_calls=_log_lines(poetry_log),
        git_calls=_log_lines(git_log),
        gh_creates=[
            line for line in _log_lines(gh_log) if line.startswith("release create ")
        ],
        unexpected_calls=_log_lines(unexpected_log),
    )


@pytest.mark.parametrize("scenario_id", list(_SCENARIOS))
def test_propose_step_scenario(tmp_path: Path, scenario_id: str) -> None:
    """Run the real proposal step against stubs and check its outcome.

    Beyond the exit code this checks the step output (``::error::`` naming the
    failing call, the captured stderr on failure, no stderr noise on success)
    and the exact call sequence of every stub, so a stub that is never reached
    cannot make a scenario pass.
    """

    scenario = _SCENARIOS[scenario_id]
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    run = _run_step(tmp_path, scenario, runner_temp)
    output = run.output

    assert not run.unexpected_calls, (
        f"the step called a stub in an unexpected way: {run.unexpected_calls}\n{output}"
    )
    assert run.returncode == scenario.expected_rc, (
        f"exit code {run.returncode}, expected {scenario.expected_rc}\n{output}"
    )
    assert run.poetry_calls == list(scenario.poetry_calls), output
    assert run.git_calls == [_DESCRIBE], output
    assert run.gh_creates == list(scenario.gh_creates), output

    for needle in scenario.stdout_has:
        assert needle in output, f"missing {needle!r} in step output\n{output}"
    for needle in scenario.stdout_lacks:
        assert needle not in output, f"unexpected {needle!r} in step output\n{output}"

    if scenario.expected_rc == 0:
        assert "STUB-NOISE" not in output, (
            f"semantic-release stderr must stay out of the log on success\n{output}"
        )
    else:
        assert "STUB-CRASH-TRACE" in output, (
            f"a failing call must show the captured semantic-release stderr\n{output}"
        )


def test_propose_step_stops_when_stderr_capture_fails(tmp_path: Path) -> None:
    """Without a place for the semantic-release stderr the step stops loudly.

    ``mktemp`` cannot create a file in a missing ``RUNNER_TEMP``. The step must
    fail with its own ``::error::`` line before semantic-release runs, instead
    of the bare ``mktemp`` message that ``set -e`` alone would leave.
    """

    run = _run_step(tmp_path, _SCENARIOS["a_propose"], tmp_path / "missing-dir")

    assert not run.unexpected_calls, run.output
    assert run.returncode == 1, run.output
    assert "::error::mktemp for the semantic-release stderr capture failed" in (
        run.output
    )
    assert run.poetry_calls == [], run.output
    assert run.gh_creates == [], run.output


def test_static_no_or_true_after_semantic_release() -> None:
    """Static ratchet: no ``|| true`` may follow a semantic-release call.

    This is a text check and no substitute for the behaviour tests above; it
    only catches the one spelling that used to swallow crashes. Retire it
    together with the step if the proposal logic moves out of shell.
    """

    run = _propose_run_block()
    assert re.search(r"semantic-release[^\n]*\|\|\s*true", run) is None
