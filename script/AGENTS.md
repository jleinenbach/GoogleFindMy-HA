# Script utilities guidelines

These conventions apply to every file within this directory tree.

## CLI expectations

* Provide a descriptive module-level docstring outlining the command's role
  and any noteworthy flags. Mention how to preview output when inputs are
  missing so reviewers can understand the UX quickly.
* Implement a `main()` function that returns an exit status integer and wrap
  execution in the `if __name__ == "__main__":` guard via `raise SystemExit`.
* Prefer `argparse.ArgumentParser` for option handling. Document default
  values in the `help` strings so the command remains self-documenting.
* When generating or applying large patches, prefer the `quiet_apply_patch.py`
  wrapper in this directory to truncate diff echoes and keep interactive
  sessions responsive.
* Print human-facing summaries with `print()` using UTF-8–safe f-strings.
  Avoid manual string concatenation when formatting values from multiple
  sources.

## Formatting conventions

* Keep line length within 99 characters unless an argparse description requires
  more space for clarity.
* Normalize filesystem interactions to use `pathlib.Path` objects and explicit
  UTF-8 encoding whenever reading text files.
* When iterating collections for display, sort deterministic output so diffs
  remain stable between runs.


## The declared minimum core version

`script/check_ha_compatibility.py` answers one question: is the minimum Home
Assistant version declared by this repository actually satisfiable by the pinned
requirements? Run `--check-declared-minimum` to verify the declared floor and
`--find-minimum` to discover the oldest satisfying release. The declared floor is
**`2025.9.1`**, recorded in `hacs.json` and in `pyproject.toml` (the comment there
explains why it is not `2025.8`).

That floor is the reason the integration carries a signature switch in its
device-registry code rather than a single modern call path. The replacement
device-registry API (`new_config_entry_id`, `new_config_subentry_id`,
`async_get_device_by_identifier`, `async_get_devices`) exists only from Core
`2026.8.0`; every release up to and including `2026.7.0` has none of it. So the code
probes the installed signature at runtime and keeps a **legacy ownership branch**,
and that branch is live code, not dead code, as long as this script reports
`2025.9.1`. (A second, differently named legacy branch in the same area serves only
test doubles; the two are told apart in `docs/AI_DEPRECATIONS_GUIDE.md`, section VI.) If you raise
the floor, the switch may be removed in the same change and not before. Background:
`docs/AI_DEPRECATIONS_GUIDE.md`, section VI.

## Gate scripts that can turn CI red

A script that fails a build and is documented nowhere is a riddle at the moment
it fires. Every such script is listed here with its purpose, its invocation, and
the condition under which it goes red.

* `vendor_leaflet.py` — keeps the Leaflet copy embedded by the map view in step
  with the version pinned in `package.json`.
  * CI runs `python script/vendor_leaflet.py --check` in the `vendored_assets`
    job. No network and no Node are needed for that mode.
  * It goes **red** when the pinned version differs from
    `custom_components/googlefindmy/vendor/leaflet/VERSION`, when a vendored file
    no longer matches its recorded SHA-256, or when the `LICENSE` file is gone
    (BSD-2-Clause attribution).
  * Fix: `npm install && python script/vendor_leaflet.py`, then commit the
    refreshed files and `VERSION`.
  * Why it exists: Dependabot reads manifests, never checked-in files. Without
    this gate a bump of the `leaflet` devDependency would leave the embedded copy
    behind silently, which is precisely how a vendored library ages into a
    vulnerability.

## The local preflight

`script/local_verify.py` has two modes. Without arguments it runs
`ruff format --check` and pytest, which is what `README.md` and the helper note
in the root `AGENTS.md` point at; the commands are the same as before, but both
modes now run with the repository root as their working directory, so a call
from a subdirectory no longer inspects only that subtree. `--all` runs the full
preflight instead and prints a stage report.

The process exit is `0` when nothing failed and everything that could run did,
`1` when a stage failed, and `3` when a stage that was supposed to run did not.
S7 and S8 do not reach `3`: they cannot run on a developer machine at all, and a
target that is permanently red is a target that gets ignored.

Every stage reports `OK`, `FAILED`, `NOTE` or `NOT CHECKED` together with a
reason and the tool version it used. The four values are the point of the
report: a stage that could not run must never look like a stage that passed.

| Stage | Command | Verdict it can reach |
| --- | --- | --- |
| S1 format | `script/precommit_hooks/ruff_format.py --check` | `OK`, `FAILED`, `NOT CHECKED` with `--skip-ruff` or without ruff |
| S2 lint | `python -m ruff check .` | `OK`, `FAILED`, `NOT CHECKED` without ruff |
| S3 types | `python -m mypy --strict --install-types --non-interactive` | `OK`, `FAILED`, `NOT CHECKED` without mypy |
| S4 spelling | `python -m codespell_lib` | `OK`, `NOTE`, `NOT CHECKED` when the tool is absent |
| S5 suite and project coverage | `pytest --cov` per track; no own `-q`, because `addopts` already carries one and a second would drop the summary line | `OK`, `FAILED`, `NOT CHECKED` |
| S6 patch coverage | `script/diff_coverage.py` | `OK`, `NOTE`, `FAILED`, `NOT CHECKED` |
| S7 security scans | bandit, semgrep, pip-audit | `NOT CHECKED`, with the measured presence of each tool |
| S8 manifest | hassfest | `NOT CHECKED`, with the measured presence of Docker |

Seven properties are deliberate and each of them has a reason worth keeping:

* **S1 to S4 run on the first `--python`, not on the interpreter that executes
  this file.** `make preflight` deliberately does not go through `poetry run`,
  so `sys.executable` there is the one interpreter that is certainly not one of
  the tracks. A stage whose tool does not answer `--version` reports
  `NOT CHECKED` rather than `FAILED`: a missing tool sends the reader looking
  for lint errors that do not exist.
* **S7 and S8 state a measured reason, not an assumed one.** `bandit` and
  `pip-audit` are dev dependencies of this repository, so "not installed" is
  false on any machine that ran `make install-dev`. The stages therefore probe
  for the tools and for `docker` and print what they found. A report that prints
  an unverified environment claim as fact is defective by the same standard as a
  green stage that never ran.

* **`ruff check` runs without `--fix`.** A stage that rewrites its own subject
  and then reports red would only reach "exit 0" in a second run, and that
  second run would be approving a different revision than the one inspected.
  `--fix` belongs in the editing loop, not in an acceptance run.
* **`S4` never blocks, but it does report when it did not run.** The
  `codespell` step in `.github/workflows/ci.yml` carries
  `continue-on-error: true`, so a blocking stage here would predict CI as
  stricter than it is. The report instead names how many findings sit on files
  this branch touches, so an own finding stays visible without becoming a false
  gate. An absent `codespell` is `NOT CHECKED`: zero findings from a tool that
  never started is the one outcome that must not read as clean spelling. The
  finding paths are normalised before that count, because `codespell` prints
  `./file` where git prints `file`, and comparing the two raw makes the counter
  structurally zero.
* **`S5` runs once per `--python` and only reports `OK` when every track is
  green.** The script drives pytest through one interpreter at a time, so a
  single invocation covers a single Home Assistant version. Pass `--python`
  once per track. The first one supplies the coverage report `S6` reads, and
  only that one: if the first track is red, `S6` is `NOT CHECKED` rather than
  quietly measuring a different track than the sentence above names.
  `S5` passes no `--cov-fail-under`; `[tool.coverage.report] fail_under` in
  `pyproject.toml` already applies, and `tests/test_platinum_compliance.py`
  clamps that value against the CI flag. A fourth copy of the floor here would
  sit outside that clamp and would keep passing locally after the floor was
  raised everywhere else.
* **`S5` brackets every run with a digest clamp.** The tracked files that differ
  from `HEAD` are hashed before and after; if a hash moved, the run is discarded
  rather than interpreted. `git diff HEAD` rather than `git diff` on purpose:
  the second one omits staged files, and a newly added file is exactly the one
  being edited while the suite runs. The path list is collected a second time
  afterwards, so a file that was untouched at the start is not invisible. A run whose sources were rewritten while it measured
  once reported 79.23% as 77.71%, a plausible number that is indistinguishable
  from a real drop.
* **The tracks run sequentially.** Under a parallel second run,
  `tests/test_main.py::TestDunderMain::test_entry_flag_subprocess_never_attempts_a_process_kill`
  has gone red once on a tree where it was green twice; it starts a subprocess
  and is time sensitive.

`make preflight` wraps the same call. The interpreters live in the variable
`PREFLIGHT_PYTHONS` (space separated, empty by default) rather than in the
Makefile, because their paths are machine local. Unlike the other targets this
one does not go through `poetry run`: each track is its own virtualenv.

```
make preflight PREFLIGHT_PYTHONS="/path/to/track-a/bin/python /path/to/track-b/bin/python"
```

## Patch coverage without a new dependency

`script/diff_coverage.py` answers the question Codecov asks on a pull request:
of the lines this branch changed, how many were executed? It reads the
Cobertura `coverage.xml` of a run over the current tree and
`git diff --unified=0 <merge-base>..HEAD`, then intersects the two. `diff-cover`
is not a dependency of this repository, and a new runtime dependency for one
measurement would be out of proportion.

Three decisions keep the number comparable rather than merely plausible:

* Files outside `[tool.coverage.run] source` have no entry in the XML at all.
  They are reported on their own counted line and never enter the quotient.
  Counting them as uncovered would produce nonsense; dropping them silently
  would make the quotient unexplainable.
* The base is `git merge-base origin/main HEAD`, not a fixed commit, so the
  number does not wander when `main` moves.
* The measurement is statement based. `coverage.xml` also carries a branch rate,
  and the terminal report of `pytest --cov` combines both; a patch coverage is
  per line, so the branch rate is neither used nor mixed in. The default
  threshold is therefore the *statement* rate of the baseline run and not the
  combined project percentage of the same run.

Exit status: `0` at or above the threshold, `2` below it, `3` when no
instrumented statement changed at all, `1` when the measurement could not be
taken. `3` has its own value because "nothing to measure" must not share an exit
code with "measured and fine": a diff that only touches tests and docs would
otherwise be reported as a passing patch coverage by every caller that reads the
status rather than the text. `S6` maps them to `OK`, `NOTE`, `NOT CHECKED` and
`FAILED` in that order.

Falling below the threshold is deliberately not an abort: the report names the
uncovered lines so the choice between adding a test and justifying the line is
made at the line. A coverage report from a different checkout, whose `<source>`
elements resolve nowhere under this repository, is an error rather than a
measurement in which every file happens to be uninstrumented.
