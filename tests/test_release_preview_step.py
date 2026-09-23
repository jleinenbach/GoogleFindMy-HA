# tests/test_release_preview_step.py
"""Behavioural tests for the dry-run preview step of ``release.yml``.

The step ``Preview (dry run)`` calls ``semantic-release --noop version`` and
``semantic-release --noop changelog``. python-semantic-release logs one
``Couldn't parse tag`` WARNING per tag outside its ``tag_format`` (four-segment
maintenance tags, old beta tags, foreign tags); in this repository that was
1380 warnings in one preview (CI run 35692265353), most of the log. The step folds them into one
count line per call and keeps everything else:

* no line of a folded warning reaches the log, including the continuation
  lines of a record that rich wrapped;
* every other stderr line and all of stdout stay visible;
* a failing call stops the step with its exit code, an ``::error::`` line and
  its complete, unfiltered stderr, and the second call does not run.

These tests execute the REAL ``run`` block, extracted with ``yaml.safe_load``
and started as ``bash -e`` like the GitHub runner does for ``run`` steps without
an explicit ``shell``. ``poetry`` is a stub on ``PATH`` that prints fixture
stderr in the shape python-semantic-release 10.6.1 produces through rich 14.3.4
(a record wrapped onto continuation lines indented by 20 columns). Reach,
stated so it is not mistaken for more: the stub does not prove how the real
tools wrap; the folding was checked once against the real semantic-release
when it was introduced, and that check is not repeated here. One visible change is
intended: the filtered stderr of a call now appears after that call's stdout
instead of interleaved with it. The stub removes its own stderr capture
through ``/proc/self/fd/2``, so ``test_preview_stops_when_count_fails`` needs
Linux, like the CI runners of this repository.
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
_STEP_NAME = "Preview (dry run)"
_STEP_IF = "github.event.inputs.dry_run == 'true'"
# The env keys the step declares; the stub environment provides exactly these.
_STEP_ENV = {"GH_TOKEN": "stub-token"}

_INDENT = " " * 20
_FOREIGN_WARNING = "WARNING  The 'changelog.changelog_file' configuration"
_FOREIGN_CONTINUATION = f"{_INDENT}option is moving to"
_TOKEN_WARNING = "WARNING  Token value is missing!"

# Wording from a CI log of this workflow: one record wrapped once (with the
# timestamp column), one wrapped twice, one on a single line as it appears
# with a wide console. A foreign two-line warning sits between them, and a
# foreign line padded with more than 20 inner spaces (as rich pads it before
# the path column) follows a folded record directly: only continuation lines
# that START with the indent may be dropped.
_VERSION_STDERR = "\n".join(
    [
        "[05:51:05] WARNING  Couldn't parse tag 1.6-beta3 as as Version:  "
        "algorithm.py:49",
        f"{_INDENT}'1.6-beta3' is not a valid Version",
        f"[05:51:05] {_FOREIGN_WARNING}   config.py:195",
        _FOREIGN_CONTINUATION,
        "           WARNING  Couldn't parse tag 1.6-beta4_100 as as       "
        "algorithm.py:49",
        f"{_INDENT}Version: '1.6-beta4_100' is not a valid",
        f"{_INDENT}Version",
        "           WARNING  Couldn't parse tag 1.7.15.18 as as Version: "
        "'1.7.15.18' is not a valid Version algorithm.py:49",
        f"           {_TOKEN_WARNING}                        config.py:805",
    ]
)
_CHANGELOG_STDERR = "\n".join(
    [
        f"           {_TOKEN_WARNING}                        config.py:805",
        "           WARNING  Couldn't parse tag 1.7.15.19 as as       algorithm.py:49",
        f"{_INDENT}Version: '1.7.15.19' is not a valid",
        f"{_INDENT}Version",
    ]
)
# Text that only occurs on continuation lines of folded records.
_FOLDED_CONTINUATIONS = (
    "'1.6-beta3' is not a valid Version",
    "Version: '1.6-beta4_100' is not a valid",
    "Version: '1.7.15.19' is not a valid",
)

_POETRY_STUB = """#!/usr/bin/env bash
printf '%s COLUMNS=%s\\n' "$*" "${COLUMNS-unset}" >> "$STUB_POETRY_LOG"
case "$*" in
  "run semantic-release --noop version")
    fixture="$STUB_VERSION_ERR"; rc="$STUB_VERSION_RC"; cmd=version ;;
  "run semantic-release --noop changelog")
    fixture="$STUB_CHANGELOG_ERR"; rc="$STUB_CHANGELOG_RC"; cmd=changelog ;;
  *)
    printf 'poetry %s\\n' "$*" >> "$STUB_UNEXPECTED_LOG"
    exit 99 ;;
esac
cat "$fixture" >&2
echo "STUB-STDOUT $cmd"
if [ "${STUB_DROP_STDERR_FILE:-}" = "$cmd" ]; then
  rm -f "$(readlink /proc/self/fd/2)"
fi
exit "$rc"
"""


def _preview_step() -> dict[str, Any]:
    """Return the one preview step of ``release.yml``; refuse anything else."""

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
    assert step["if"] == _STEP_IF
    return step


def _preview_run_block() -> str:
    """Return the ``run`` script of the preview step, checked for runnability."""

    step = _preview_step()
    run = step["run"]
    assert isinstance(run, str)
    # An expression would be substituted by the runner before bash sees the
    # script; executing it verbatim here would test something else.
    assert "${{" not in run, "the run block must reach its inputs through env only"
    assert set(step["env"]) == set(_STEP_ENV), (
        "the step's env changed; extend _STEP_ENV to match"
    )
    return run


def _log_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


@dataclass(frozen=True)
class _StepRun:
    """Outcome of one execution of the preview step."""

    returncode: int
    output: str
    poetry_calls: list[str]
    unexpected_calls: list[str]


def _run_step(
    tmp_path: Path,
    *,
    version_err: str = _VERSION_STDERR,
    changelog_err: str = _CHANGELOG_STDERR,
    version_rc: int = 0,
    changelog_rc: int = 0,
    runner_temp: Path | None = None,
    drop_stderr_file: str = "",
    failing_awk: bool = False,
) -> _StepRun:
    """Execute the real preview step under ``bash -e`` against the stub."""

    bash = shutil.which("bash")
    # Deliberately no skip: a skipped behaviour test would be a silent pass.
    assert bash is not None, "bash is required to execute the workflow step"

    script = tmp_path / "step.sh"
    script.write_text(_preview_run_block(), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "poetry"
    stub.write_text(_POETRY_STUB, encoding="utf-8")
    stub.chmod(0o755)
    if failing_awk:
        awk_stub = bin_dir / "awk"
        awk_stub.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
        awk_stub.chmod(0o755)

    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    version_file = fixtures / "version.err"
    version_file.write_text(version_err + "\n" if version_err else "", "utf-8")
    changelog_file = fixtures / "changelog.err"
    changelog_file.write_text(changelog_err + "\n" if changelog_err else "", "utf-8")
    poetry_log = tmp_path / "poetry.log"
    unexpected_log = tmp_path / "unexpected.log"
    if runner_temp is None:
        runner_temp = tmp_path / "runner-temp"
        runner_temp.mkdir()

    # Built from scratch, without os.environ, so nothing from the calling shell
    # or from CI (BASH_ENV, GITHUB_*, COLUMNS) reaches the step.
    env = {
        **_STEP_ENV,
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "RUNNER_TEMP": str(runner_temp),
        "STUB_VERSION_ERR": str(version_file),
        "STUB_CHANGELOG_ERR": str(changelog_file),
        "STUB_VERSION_RC": str(version_rc),
        "STUB_CHANGELOG_RC": str(changelog_rc),
        "STUB_DROP_STDERR_FILE": drop_stderr_file,
        "STUB_POETRY_LOG": str(poetry_log),
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
        unexpected_calls=_log_lines(unexpected_log),
    )


def _count_line(cmd: str, count: int) -> str:
    return f"semantic-release --noop {cmd}: {count} unparseable tag warning(s) folded"


_CALLS = [
    "run semantic-release --noop version COLUMNS=1000",
    "run semantic-release --noop changelog COLUMNS=1000",
]


def test_preview_folds_parse_warnings_per_call(tmp_path: Path) -> None:
    """Folded warnings leave no line behind; each call gets its own count."""

    run = _run_step(tmp_path)
    lines = run.output.splitlines()

    assert not run.unexpected_calls, run.output
    assert run.returncode == 0, run.output
    assert run.poetry_calls == _CALLS, run.output
    assert "Couldn't parse tag" not in run.output
    for text in _FOLDED_CONTINUATIONS:
        assert text not in run.output, f"continuation {text!r} leaked\n{run.output}"
    assert f"{_INDENT}Version" not in lines, run.output
    assert lines.count(_count_line("version", 3)) == 1, run.output
    assert lines.count(_count_line("changelog", 1)) == 1, run.output


def test_preview_keeps_other_stderr_and_stdout(tmp_path: Path) -> None:
    """Foreign warnings, their continuation lines and stdout stay visible."""

    run = _run_step(tmp_path)
    lines = run.output.splitlines()

    assert run.returncode == 0, run.output
    assert _FOREIGN_WARNING in run.output
    assert _FOREIGN_CONTINUATION in lines, run.output
    assert run.output.count(_TOKEN_WARNING) == 2, run.output
    assert "STUB-STDOUT version" in lines, run.output
    assert "STUB-STDOUT changelog" in lines, run.output


def test_preview_counts_zero_when_nothing_to_fold(tmp_path: Path) -> None:
    """Without tag warnings each call still reports its count, as 0."""

    run = _run_step(tmp_path, version_err="", changelog_err="")
    lines = run.output.splitlines()

    assert run.returncode == 0, run.output
    assert lines.count(_count_line("version", 0)) == 1, run.output
    assert lines.count(_count_line("changelog", 0)) == 1, run.output


@pytest.mark.parametrize(
    ("failing", "rc"),
    [("version", 1), ("changelog", 2)],
)
def test_preview_crash_stops_with_full_stderr(
    tmp_path: Path, failing: str, rc: int
) -> None:
    """A failing call stops the step and shows its unfiltered stderr."""

    run = _run_step(
        tmp_path,
        version_rc=rc if failing == "version" else 0,
        changelog_rc=rc if failing == "changelog" else 0,
    )
    lines = run.output.splitlines()

    assert not run.unexpected_calls, run.output
    assert run.returncode == rc, run.output
    assert (
        f"::error::'poetry run semantic-release --noop {failing}' failed with "
        f"exit code {rc}"
    ) in run.output
    assert not any(
        line.startswith(f"semantic-release --noop {failing}:") for line in lines
    ), run.output
    if failing == "version":
        assert run.poetry_calls == _CALLS[:1], run.output
        assert run.output.count("Couldn't parse tag") == 3, run.output
        assert "'1.6-beta3' is not a valid Version" in run.output
        assert f"{_INDENT}Version" in lines, run.output
    else:
        assert run.poetry_calls == _CALLS, run.output
        assert lines.count(_count_line("version", 3)) == 1, run.output
        assert run.output.count("Couldn't parse tag") == 1, run.output
        assert "Version: '1.7.15.19' is not a valid" in run.output


def test_preview_stops_when_stderr_capture_fails(tmp_path: Path) -> None:
    """Without a place for the stderr capture the step stops loudly.

    ``mktemp`` cannot create a file in a missing ``RUNNER_TEMP``. The step must
    fail with its own ``::error::`` line before semantic-release runs.
    """

    run = _run_step(tmp_path, runner_temp=tmp_path / "missing-dir")

    assert not run.unexpected_calls, run.output
    assert run.returncode == 1, run.output
    assert "::error::mktemp for the semantic-release stderr capture failed" in (
        run.output
    )
    assert run.poetry_calls == [], run.output


def test_preview_stops_when_count_fails(tmp_path: Path) -> None:
    """A failed count stops the step instead of reporting 0 folded warnings.

    The stub removes its own stderr capture file, so the count cannot read it.
    """

    run = _run_step(tmp_path, drop_stderr_file="version")
    lines = run.output.splitlines()

    assert not run.unexpected_calls, run.output
    assert run.returncode == 1, run.output
    assert "::error::counting the folded tag warnings failed" in run.output
    assert not any("unparseable tag warning(s) folded" in line for line in lines), (
        run.output
    )
    assert run.poetry_calls == _CALLS[:1], run.output


def test_preview_stops_when_filter_fails(tmp_path: Path) -> None:
    """A failing filter stops the step and shows the unfiltered stderr.

    ``awk`` is replaced by a stub that exits 2 without output.
    """

    run = _run_step(tmp_path, failing_awk=True)
    lines = run.output.splitlines()

    assert not run.unexpected_calls, run.output
    assert run.returncode == 1, run.output
    assert (
        "::error::filtering the semantic-release stderr failed (awk exit 2)"
        in run.output
    )
    assert run.output.count("Couldn't parse tag") == 3, run.output
    assert not any("unparseable tag warning(s) folded" in line for line in lines), (
        run.output
    )
    assert run.poetry_calls == _CALLS[:1], run.output


def test_static_no_or_true_after_semantic_release_in_preview() -> None:
    """Static ratchet: no ``|| true`` may follow a semantic-release call.

    This is a text check and no substitute for the behaviour tests above; it
    only catches the one spelling that would swallow a crash of the preview.
    """

    run = _preview_run_block()
    assert re.search(r"semantic-release[^\n]*\|\|\s*true", run) is None
