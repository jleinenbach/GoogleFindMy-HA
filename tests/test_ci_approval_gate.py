# tests/test_ci_approval_gate.py
"""Lock the manual approval gate of the CI workflow (fork-specific).

Pull request runs of ``ci.yml`` wait in the job ``approval_gate`` for a manual
approval in the environment ``ci-freigabe`` before any test job starts. The
point is the Home Assistant test load: it should only happen when it is wanted.
Pushes to ``main`` run without the gate. The contract is documented in the root
``AGENTS.md`` (bullet "PR CI runs only after manual approval").

The gate only works as a whole. Each check below guards one link:

* the gate job exists, is bound to the environment and only runs on pull requests;
* every other job states its condition explicitly. Without that, a skipped gate
  on ``push`` would skip all jobs behind ``hassfest`` (a skipped ancestor skips
  its dependants under the default ``success()``);
* ``hassfest`` accepts a skipped gate only on ``push``, so a future mistake in the
  gate condition cannot let tests run on a pull request without approval;
* ``CI Success`` depends on the gate and fails a pull request run whose gate was
  not approved (second lock behind the required status check);
* one live run per pull request: a new push cancels the previous run, push runs
  on ``main`` never share a group.

Fork-specific: do not upstream this test together with the gate.
BSkando/GoogleFindMy-HA has no ``ci-freigabe`` environment.

``CI_YML_PATH`` overrides the workflow path; it exists for red probes against an
older or mutated copy of the file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"

GATE = "approval_gate"
SUMMARY = "ci-success"


def _load_ci() -> dict[str, Any]:
    path = Path(os.environ.get("CI_YML_PATH", str(DEFAULT_CI_YML)))
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _jobs() -> dict[str, dict[str, Any]]:
    jobs = _load_ci()["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return [needs]
    assert isinstance(needs, list)
    return [str(item) for item in needs]


def _condition(name: str, job: dict[str, Any]) -> str:
    condition = job.get("if")
    assert isinstance(condition, str), f"job {name} needs an explicit if condition"
    return condition


def test_gate_job_waits_for_environment_on_pull_requests() -> None:
    """The gate job is bound to ci-freigabe and only runs for pull requests."""

    jobs = _jobs()
    assert GATE in jobs, "ci.yml must define the approval_gate job"
    gate = jobs[GATE]
    assert gate.get("environment") == "ci-freigabe"
    assert (
        _condition(GATE, gate).replace(" ", "") == "github.event_name=='pull_request'"
    )
    assert gate.get("permissions") == {}, "the gate job needs no token permissions"
    assert not _needs(gate), "the gate must be the first job"


def test_one_live_run_per_pull_request() -> None:
    """A new push cancels the previous PR run; push runs never share a group."""

    concurrency = _load_ci().get("concurrency")
    assert isinstance(concurrency, dict), "ci.yml must define workflow concurrency"
    group = str(concurrency.get("group", ""))
    assert "github.event.pull_request.number" in group
    assert "github.run_id" in group
    assert concurrency.get("cancel-in-progress") is True


def test_hassfest_runs_only_behind_an_approved_gate_or_on_push() -> None:
    """hassfest accepts a skipped gate only for push events."""

    hassfest = _jobs()["hassfest"]
    assert GATE in _needs(hassfest)
    condition = _condition("hassfest", hassfest)
    assert condition.startswith("${{") and condition.endswith("}}")
    assert "!cancelled()" in condition
    assert "needs.approval_gate.result == 'success'" in condition
    assert (
        "github.event_name == 'push' && needs.approval_gate.result == 'skipped'"
        in condition
    )


def test_every_job_requires_its_needs_explicitly() -> None:
    """Each job behind the gate checks every direct need for success."""

    jobs = _jobs()
    checked = 0
    for name, job in jobs.items():
        if name in (GATE, SUMMARY):
            continue
        condition = _condition(name, job)
        assert condition.startswith("${{") and condition.endswith("}}"), (
            f"job {name}: a condition with a status function belongs in ${{{{ }}}}"
        )
        assert "!cancelled()" in condition, f"job {name} must not run after cancel"
        needs = _needs(job)
        assert needs, f"job {name} must depend on the gate chain"
        for need in needs:
            assert f"needs.{need}.result == 'success'" in condition, (
                f"job {name} must require needs.{need}.result == 'success'"
            )
        checked += 1
    assert checked >= 7, "expected hassfest and the six jobs behind it"


def test_ci_success_includes_the_gate() -> None:
    """CI Success covers every job and fails an unapproved pull request run."""

    jobs = _jobs()
    summary = jobs[SUMMARY]
    assert summary.get("if") == "always()"
    expected = {name for name in jobs if name != SUMMARY}
    assert expected <= set(_needs(summary))

    check = next(step for step in summary["steps"] if step.get("id") == "check")
    env = check.get("env", {})
    assert env.get("GATE") == "${{ needs.approval_gate.result }}"
    assert env.get("EVENT") == "${{ github.event_name }}"
    assert '"$EVENT" == "pull_request" && "$GATE" != "success"' in check["run"]
