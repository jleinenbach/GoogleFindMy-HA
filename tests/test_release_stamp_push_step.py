# tests/test_release_stamp_push_step.py
"""Behavioural tests for the push step of ``.github/workflows/release-stamp.yml``.

The step ``Resolve the owning branch and push the stamp`` runs after the
workflow has checked out the *published tag* (a detached HEAD) and committed
the version stamp on top of it. It resolves the one branch that contains the
tag and has four outcomes:

* the direct push to that branch succeeds;
* branch rules reject the direct push, so the step pushes the stamp to a new
  branch ``release-stamp/<tag>`` and opens a stamp PR against the owning branch;
* the tag is behind the branch tip, so the branch stamp is skipped;
* no single branch owns the tag, so the branch stamp is skipped.

The second outcome failed in production for every release after the ruleset on
``main`` went live: with a detached HEAD, ``git push origin HEAD:<name>`` cannot
tell that a new ``<name>`` is meant to be a branch and stops with "The
destination you provided is not a full refname". Tags with and without a
leading ``v`` take the same path, so both are covered.

These tests execute the REAL ``run`` block, extracted with ``yaml.safe_load``
and started as ``bash -e`` like the GitHub runner does for ``run`` steps without
an explicit ``shell``. Reach, stated so it is not mistaken for more: ``git`` is
the real binary and the remote is a local bare repository, so ref resolution,
push refusal and fast-forward checks are real. ``gh`` is a stub that logs its
calls. The ``pre-receive`` hook only imitates the text of a GitHub ruleset
rejection (``GH013``); a change of that text on GitHub's side is outside this
test. The runner's evaluation of ``if: env.STAMP_COMMITTED == 'true'`` is
outside it as well; the test pins the condition's text instead. The step
reads no variable from ``GITHUB_ENV`` (``VERSION``, ``STAMP_COMMITTED``), so
none is provided; a future read would fail loudly under ``set -u``.

The checkout of ``actions/checkout`` is rebuilt as ``git init``, ``git fetch``
of all branches and tags, and ``git checkout --detach <tag>``; this yields the
same remote-tracking branches as a ``git clone`` for the step's owner lookup.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "release-stamp.yml"
_STEP_NAME = "Resolve the owning branch and push the stamp"
# The job-level env keys the step reads; the test environment provides these.
_JOB_ENV_KEYS = {"TAG_NAME", "GH_TOKEN"}
_STEP_IF = "env.STAMP_COMMITTED == 'true'"
_DIRECT_PUSH = 'git push origin "HEAD:${STAMP_BRANCH}"'

_IDENTITY = {
    "GIT_AUTHOR_NAME": "Stamp Test",
    "GIT_AUTHOR_EMAIL": "stamp-test@example.invalid",
    "GIT_COMMITTER_NAME": "Stamp Test",
    "GIT_COMMITTER_EMAIL": "stamp-test@example.invalid",
}

# Rejects every update of refs/heads/main with the wording GitHub uses for a
# repository ruleset; git prefixes each stderr line of the hook with "remote: ".
_RULESET_HOOK = """#!/bin/sh
while read -r old new ref; do
  if [ "$ref" = "refs/heads/main" ]; then
    echo "error: GH013: Repository rule violations found for refs/heads/main." >&2
    exit 1
  fi
done
exit 0
"""

_GH_STUB = """#!/usr/bin/env bash
if [ "$1 $2" = "pr create" ]; then
  printf '%s\\n' "$*" >> "$STUB_GH_LOG"
  if [ "${STUB_GH_CREATE_RC:-0}" -ne 0 ]; then
    echo "STUB-GH-FAILURE" >&2
    exit "$STUB_GH_CREATE_RC"
  fi
  echo "https://example.invalid/pr/1"
  exit 0
fi
printf 'gh %s\\n' "$*" >> "$STUB_UNEXPECTED_LOG"
exit 99
"""


def _push_step() -> dict[str, Any]:
    """Return the one push step of ``release-stamp.yml``; refuse anything else."""

    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    job = workflow["jobs"]["stamp"]
    # The test starts the block as `bash -e`, the runner's form for a run step
    # without an explicit shell; any shell setting would make that a different
    # interpreter from the one the runner uses.
    for scope, owner in (("workflow", workflow), ("job", job)):
        shell = (owner.get("defaults") or {}).get("run", {}).get("shell")
        assert shell is None, f"{scope} sets defaults.run.shell={shell!r}"
    assert "env" not in workflow, "a workflow-level env would have to be modelled"
    assert set(job["env"]) == _JOB_ENV_KEYS, (
        "the job's env changed; extend _JOB_ENV_KEYS and the test environment"
    )
    steps = [step for step in job["steps"] if step.get("name") == _STEP_NAME]
    assert len(steps) == 1, (
        f"expected exactly one step named {_STEP_NAME!r}, got {len(steps)}"
    )
    step: dict[str, Any] = steps[0]
    assert "shell" not in step, f"the step sets shell={step['shell']!r}"
    assert "env" not in step, "a step-level env would have to be modelled"
    assert step["if"] == _STEP_IF
    return step


def _push_run_block() -> str:
    """Return the ``run`` script of the push step, checked for runnability."""

    run = _push_step()["run"]
    assert isinstance(run, str)
    # An expression would be substituted by the runner before bash sees the
    # script; executing it verbatim here would test something else.
    assert "${{" not in run, "the run block must reach its inputs through env only"
    return run


def _base_env(tmp_path: Path) -> dict[str, str]:
    """Build a git environment from scratch, isolated from the caller's config."""

    global_config = tmp_path / "gitconfig"
    global_config.touch()
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(global_config),
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        **_IDENTITY,
    }


def _git(env: dict[str, str], *args: str, cwd: Path | None = None) -> str:
    """Run git with ``check=True`` and return its stripped stdout."""

    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=True,
    )
    return proc.stdout.decode("utf-8").strip()


@dataclass(frozen=True)
class _World:
    """A bare remote plus a detached checkout of the tag with a stamp commit."""

    env: dict[str, str]
    remote: Path
    work: Path
    seed_main: str
    stamp_sha: str


def _seed(tmp_path: Path, tag: str, layout: str) -> _World:
    """Create the remote, the tag and the stamped detached checkout.

    ``layout`` is one of ``ruleset`` (a hook rejects pushes to ``main``),
    ``open`` (no hook), ``extra_commit_on_main`` (``main`` moved past the tag)
    and ``second_owner_branch`` (a second branch also contains the tag).
    """

    env = _base_env(tmp_path)
    # Resolved against the PATH the git calls below use, not the caller's.
    assert shutil.which("git", path=env["PATH"]) is not None, (
        "git is required for these tests"
    )
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    work = tmp_path / "work"

    _git(env, "init", "--quiet", "--bare", "-b", "main", str(remote))
    _git(env, "init", "--quiet", "-b", "main", str(seed))
    version_file = seed / "VERSION"
    version_file.write_text("0.0.0\n", encoding="utf-8")
    _git(env, "add", "VERSION", cwd=seed)
    _git(env, "commit", "--quiet", "-m", "init", cwd=seed)
    _git(env, "tag", tag, cwd=seed)
    _git(env, "remote", "add", "origin", str(remote), cwd=seed)
    if layout == "extra_commit_on_main":
        (seed / "OTHER").write_text("later\n", encoding="utf-8")
        _git(env, "add", "OTHER", cwd=seed)
        _git(env, "commit", "--quiet", "-m", "later work", cwd=seed)
    if layout == "second_owner_branch":
        _git(env, "branch", "maintenance", tag, cwd=seed)
        _git(env, "push", "--quiet", "origin", "maintenance", cwd=seed)
    _git(env, "push", "--quiet", "origin", "main", tag, cwd=seed)
    seed_main = _git(env, "--git-dir", str(remote), "rev-parse", "refs/heads/main")

    if layout == "ruleset":
        hook = remote / "hooks" / "pre-receive"
        hook.write_text(_RULESET_HOOK, encoding="utf-8")
        hook.chmod(0o755)

    # Rebuild of the actions/checkout step (ref: <tag>, fetch-depth: 0).
    _git(env, "init", "--quiet", "-b", "main", str(work))
    _git(env, "remote", "add", "origin", str(remote), cwd=work)
    _git(
        env,
        "fetch",
        "--quiet",
        "origin",
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/tags/*:refs/tags/*",
        cwd=work,
    )
    _git(env, "checkout", "--quiet", "--detach", tag, cwd=work)
    (work / "VERSION").write_text(f"{tag}\n", encoding="utf-8")
    _git(env, "commit", "--quiet", "-am", f"chore(release): {tag} [skip ci]", cwd=work)
    stamp_sha = _git(env, "rev-parse", "HEAD", cwd=work)

    detached = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=work,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    assert detached.returncode != 0, "the stamp must sit on a detached HEAD"
    return _World(env, remote, work, seed_main, stamp_sha)


@dataclass(frozen=True)
class _StepRun:
    """Outcome of one execution of the push step."""

    returncode: int
    output: str
    gh_calls: list[str]
    unexpected_calls: list[str]


def _log_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _run_step(
    tmp_path: Path, world: _World, tag: str, gh_create_rc: int = 0
) -> _StepRun:
    """Execute the real push step under ``bash -e`` inside the checkout."""

    bash = shutil.which("bash")
    # Deliberately no skip: a skipped behaviour test would be a silent pass.
    assert bash is not None, "bash is required to execute the workflow step"

    script = tmp_path / "step.sh"
    script.write_text(_push_run_block(), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(_GH_STUB, encoding="utf-8")
    stub.chmod(0o755)
    gh_log = tmp_path / "gh.log"
    unexpected_log = tmp_path / "unexpected.log"

    env = {
        **world.env,
        "PATH": f"{bin_dir}:{world.env['PATH']}",
        "TAG_NAME": tag,
        "GH_TOKEN": "stub-token",
        "STUB_GH_LOG": str(gh_log),
        "STUB_GH_CREATE_RC": str(gh_create_rc),
        "STUB_UNEXPECTED_LOG": str(unexpected_log),
    }
    proc = subprocess.run(
        [bash, "-e", str(script)],
        cwd=world.work,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    return _StepRun(
        returncode=proc.returncode,
        output=proc.stdout.decode("utf-8", errors="replace"),
        gh_calls=_log_lines(gh_log),
        unexpected_calls=_log_lines(unexpected_log),
    )


def _remote_ref(world: _World, ref: str) -> str:
    return _git(world.env, "--git-dir", str(world.remote), "rev-parse", ref)


def _stamp_branches(world: _World) -> str:
    return _git(
        world.env,
        "--git-dir",
        str(world.remote),
        "for-each-ref",
        "--format=%(refname)",
        "refs/heads/release-stamp/",
    )


@pytest.mark.parametrize("tag", ["1.7.15.18", "v1.7.15.18"])
def test_fallback_push_creates_stamp_branch(tmp_path: Path, tag: str) -> None:
    """A ruleset rejection leads to a new stamp branch and a stamp PR.

    Covers tags with and without a leading ``v``; both reach the fallback
    push with the full tag name in the branch name.
    """

    world = _seed(tmp_path, tag, "ruleset")
    run = _run_step(tmp_path, world, tag)

    assert not run.unexpected_calls, run.output
    assert run.returncode == 0, run.output
    assert "rejected by branch rules; opening the stamp PR" in run.output
    assert _remote_ref(world, f"refs/heads/release-stamp/{tag}") == world.stamp_sha
    assert _remote_ref(world, "refs/heads/main") == world.seed_main
    assert len(run.gh_calls) == 1, run.gh_calls
    assert run.gh_calls[0].startswith(
        f"pr create --base main --head release-stamp/{tag} "
    ), run.gh_calls
    assert "::notice::stamp PR opened: https://example.invalid/pr/1" in run.output


def test_fallback_names_manual_pr_command_when_pr_creation_fails(
    tmp_path: Path,
) -> None:
    """A failing ``gh pr create`` stops the step and names the manual command.

    The stamp branch is already on the remote at that point. A rerun would
    find two branches containing the tag and skip with a notice, so the step
    must say how to complete the stamp by hand.
    """

    tag = "1.7.15.18"
    world = _seed(tmp_path, tag, "ruleset")
    run = _run_step(tmp_path, world, tag, gh_create_rc=1)

    assert not run.unexpected_calls, run.output
    assert run.returncode == 1, run.output
    assert "STUB-GH-FAILURE" in run.output
    assert (
        f"::error::pushed release-stamp/{tag}, but opening the stamp PR failed; "
        f"open it by hand: gh pr create --base main --head release-stamp/{tag}"
    ) in run.output
    assert "::notice::stamp PR opened" not in run.output
    assert _remote_ref(world, f"refs/heads/release-stamp/{tag}") == world.stamp_sha
    assert _remote_ref(world, "refs/heads/main") == world.seed_main


def test_direct_push_updates_owning_branch(tmp_path: Path) -> None:
    """Without branch rules the stamp lands on the owning branch directly."""

    tag = "1.7.15.18"
    world = _seed(tmp_path, tag, "open")
    run = _run_step(tmp_path, world, tag)

    assert not run.unexpected_calls, run.output
    assert run.returncode == 0, run.output
    assert "direct push to main succeeded" in run.output
    assert _remote_ref(world, "refs/heads/main") == world.stamp_sha
    assert run.gh_calls == [], run.output
    assert _stamp_branches(world) == "", run.output


def test_behind_tag_skips_branch_stamp(tmp_path: Path) -> None:
    """A tag behind the branch tip is not stamped onto the branch."""

    tag = "1.7.15.18"
    world = _seed(tmp_path, tag, "extra_commit_on_main")
    run = _run_step(tmp_path, world, tag)

    assert not run.unexpected_calls, run.output
    assert run.returncode == 0, run.output
    assert f"::notice::tag {tag} is behind main" in run.output
    assert _remote_ref(world, "refs/heads/main") == world.seed_main
    assert run.gh_calls == [], run.output
    assert _stamp_branches(world) == "", run.output


def test_ambiguous_owner_skips_branch_stamp(tmp_path: Path) -> None:
    """Two branches containing the tag mean no guess and no push."""

    tag = "1.7.15.18"
    world = _seed(tmp_path, tag, "second_owner_branch")
    run = _run_step(tmp_path, world, tag)

    assert not run.unexpected_calls, run.output
    assert run.returncode == 0, run.output
    assert (
        f"::notice::could not unambiguously resolve the branch owning tag {tag}"
        in run.output
    )
    assert _remote_ref(world, "refs/heads/main") == world.seed_main
    assert _remote_ref(world, "refs/heads/maintenance") == world.seed_main
    assert run.gh_calls == [], run.output
    assert _stamp_branches(world) == "", run.output


def test_direct_push_stays_unqualified() -> None:
    """Text check: the direct push keeps its short, unqualified destination.

    This is a text check, not a behaviour test. The owning branch exists when
    the step resolves it; if it were deleted before the push, the short form
    fails loudly, while ``HEAD:refs/heads/<branch>`` would silently recreate
    the deleted branch.
    """

    assert _push_run_block().count(_DIRECT_PUSH) == 1
