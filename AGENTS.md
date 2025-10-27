<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [AGENTS.md — Operating Contract for `googlefindmy` (Home Assistant custom integration)](#agentsmd--operating-contract-for-googlefindmy-home-assistant-custom-integration)
  - [Scoped guidance index](#scoped-guidance-index)
  - [Environment verification](#environment-verification)
    - [Module invocation primer](#module-invocation-primer)
  - [1) What must be in **every** PR (lean checklist)](#1-what-must-be-in-every-pr-lean-checklist)
  - [Home Assistant version & dependencies](#home-assistant-version--dependencies)
  - [Maintenance mode](#maintenance-mode)
  - [2) Roles (right-sized)](#2-roles-right-sized)
    - [2.1 Contributor (implementation) — **accountable for features/fixes/refactors**](#21-contributor-implementation--accountable-for-featuresfixesrefactors)
    - [2.2 Reviewer (maintainer/agent) — **accountable for correctness**](#22-reviewer-maintaineragent--accountable-for-correctness)
    - [2.3 History analysis best practices](#23-history-analysis-best-practices)
  - [3) Test policy — explicit AI actions](#3-test-policy--explicit-ai-actions)
    - [3.1 Automatic **Test Corrections** (MUST)](#31-automatic-test-corrections-must)
    - [3.2 Automatic **Regression Tests** for fixes (MUST)](#32-automatic-regression-tests-for-fixes-must)
    - [3.3 Opportunistic **Test Optimization** (SHOULD SUGGEST)](#33-opportunistic-test-optimization-should-suggest)
    - [3.4 Definition of Done for tests](#34-definition-of-done-for-tests)
    - [3.5 Temporary coverage dips](#35-temporary-coverage-dips)
  - [4) **Token cache handling** (hard requirement; regression-prevention)](#4-token-cache-handling-hard-requirement-regression-prevention)
  - [5) Security & privacy guards](#5-security--privacy-guards)
  - [6) Expected **test types & coverage focus**](#6-expected-test-types--coverage-focus)
  - [7) Quality scale (practical, non-blocking)](#7-quality-scale-practical-non-blocking)
  - [8) Deprecation check (concise, per PR)](#8-deprecation-check-concise-per-pr)
  - [9) Docs & i18n (minimal but strict)](#9-docs--i18n-minimal-but-strict)
  - [10) Local commands (VERIFY)](#10-local-commands-verify)
    - [10.1 Type-checking policy — mypy strict on edited Python files](#101-type-checking-policy--mypy-strict-on-edited-python-files)
  - [11) Clean & Secure Coding Standard (Python 3.12 + Home Assistant 2025.10)](#11-clean--secure-coding-standard-python-312--home-assistant-202510)
    - [11.1 Language & style (self-documenting)](#111-language--style-self-documenting)
    - [11.2 Security baseline (OWASP / NIST / BSI)](#112-security-baseline-owasp--nist--bsi)
    - [11.3 Async, concurrency & cancellation](#113-async-concurrency--cancellation)
    - [11.4 File system & I/O (safe & gentle)](#114-file-system--io-safe--gentle)
    - [11.5 Guard catalog & error messages](#115-guard-catalog--error-messages)
    - [11.6 Performance without feature loss](#116-performance-without-feature-loss)
    - [11.7 Home Assistant specifics (must-haves)](#117-home-assistant-specifics-must-haves)
    - [11.8 Release & operations](#118-release--operations)
    - [11.9 Machine-checkable acceptance checklist (for the agent)](#119-machine-checkable-acceptance-checklist-for-the-agent)
  - [REFERENCES](#references)
    - [1) Python 3.12 — Language, Style, Typing, Safety](#1-python-312--language-style-typing-safety)
    - [2) Home Assistant (Developer Docs, 2024–2025)](#2-home-assistant-developer-docs-20242025)
    - [3) Secure Development — Standards & Guidance](#3-secure-development--standards--guidance)
      - [OWASP Cheat Sheet Series (selected)](#owasp-cheat-sheet-series-selected)
    - [4) Software Supply Chain & Reproducibility](#4-software-supply-chain--reproducibility)
    - [5) Repo & Documentation Hygiene (GitHub/Markdown)](#5-repo--documentation-hygiene-githubmarkdown)
    - [6) In-repo Find My Device Network protocol reference](#6-in-repo-find-my-device-network-protocol-reference)
  - [High-confidence & sourcing standard for Codex](#high-confidence--sourcing-standard-for-codex)
    - [0. Scope](#0-scope)
    - [1. High-confidence mode (≥ 90 %)](#1-high-confidence-mode-%E2%89%A5-90-%25)
    - [2. Mandatory evidence](#2-mandatory-evidence)
    - [3. Workflow for code changes](#3-workflow-for-code-changes)
    - [4. User as data source](#4-user-as-data-source)
    - [5. Communicate uncertainty](#5-communicate-uncertainty)
    - [6. Consequences for commits, diffs, and reviews](#6-consequences-for-commits-diffs-and-reviews)
    - [7. Review obligations](#7-review-obligations)
    - [8. Required workflow summary](#8-required-workflow-summary)
    - [9. Rationale](#9-rationale)
    - [10. Post-task feedback obligations](#10-post-task-feedback-obligations)
  - [pip-audit workflow guidance (CORRECTION — April 2025)](#pip-audit-workflow-guidance-correction--april-2025)
    - [7) Type checking (mypy)](#7-type-checking-mypy)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# AGENTS.md — Operating Contract for `googlefindmy` (Home Assistant custom integration)

* [Scoped guidance index](#scoped-guidance-index)
* [Environment verification](#environment-verification)
* [What must be in every PR](#1-what-must-be-in-every-pr-lean-checklist)
* [Mandatory evidence](#2-mandatory-evidence)
* [Workflow for code changes](#3-workflow-for-code-changes)
* [Communicate uncertainty](#5-communicate-uncertainty)
* [Type checking (mypy)](#7-type-checking-mypy)

> **Scope & authority**
>
> **Directory scope:** applies to the entire repository (with continued emphasis on `custom_components/googlefindmy/**` and tests under `tests/**`).
> **File headers:** Every Python file within scope must include a comment containing its repository-relative path (e.g., `# tests/test_example.py`). When a file has a shebang (`#!`), the shebang stays on the first line and the path comment immediately follows it.
> **Precedence:** (1) Official **Home Assistant Developer Docs** → (2) this AGENTS.md → (3) repository conventions. This file never overrides security/legal policies.
> **Language policy:** Keep the project consistently in English for documentation, inline code comments, and docstrings. (Translation files remain multilingual.)
> **Non-blocking:** Missing optional artifacts (README sections, `quality_scale.yaml`, CODEOWNERS, CI files) **must not block** urgent fixes. The agent proposes a minimal stub or follow-up task instead.
> **References:** This contract relies on the sources listed below; for a curated, extended list of links, see [BOOKMARKS.md](custom_components/googlefindmy/BOOKMARKS.md).
> **Upstream documentation hierarchy:** When consulting external guidance, prioritize Home Assistant's canonical domains in this order: developer portal (`https://developers.home-assistant.io`), user documentation (`https://www.home-assistant.io`), and the alerts/service bulletins site (`https://alerts.home-assistant.io`). If a required host is unreachable while the connectivity probe still confirms general internet access, pause implementation, request manual approval for that domain, and document the escalation before proceeding.
## Scoped guidance index

* [`tests/AGENTS.md`](tests/AGENTS.md) — Home Assistant config flow test stubs, helpers, discovery/update scaffolding details, **and** the package-layout note that requires package-relative imports now that `tests/` ships with an `__init__.py`. Also documents the coordinator device-registry expectations for `via_device` tuple handling so future stub updates remain aligned with Home Assistant 2025.10.
* `google/protobuf/**` — Local type stub overlays that model the minimal subset of `google.protobuf` used by the integration. These stubs unblock strict mypy runs without depending on the upstream package’s incomplete type hints. Update them when generated protobuf code begins to reference additional APIs or when upstream ships first-party stubs that supersede these local helpers.

>
> | Domain | Primary use cases |
> | --- | --- |
> | `https://developers.home-assistant.io` | Integration architecture, helper APIs, config flow patterns, data update coordinator guidance, and other developer-facing contracts. |
> | `https://www.home-assistant.io` | User-facing behavior references, feature overviews, release notes, and configuration examples that affect documentation or UX communication. |
> | `https://alerts.home-assistant.io` | Service advisories, breaking changes, security or outage bulletins that may require mitigation steps or temporary workarounds. |

## Environment verification

* **Cache hygiene helper.** Run `make clean` from the repository root to prune `__pycache__` directories and stray `*.pyc` files after tests or whenever caches need to be refreshed. If you need to tidy up manually (for example after executing helper scripts), remove the bytecode caches with `find . -type d -name '__pycache__' -prune -exec rm -rf {} +` before committing.

* **Requirement:** Determine the current connectivity status before every implementation cycle.
* **Preferred check:** Use `python -m pip install --dry-run --no-deps pip` so contributors document a consistent HTTP/HTTPS probe and capture the output in their summaries.
* **Checks:** Run a quick internet-access probe that exercises the real package channels (for example, `python -m pip install --dry-run --no-deps pip`, `pip index versions pip`, or a package-manager metadata refresh such as `apt-get update`) and record the outcome in the summary. Avoid ICMP-only probes like `ping 8.8.8.8`, which are blocked in the managed environment and do not reflect HTTP/HTTPS reachability. When a tool installs command-line entry points into `~/.pyenv/versions/*/bin`, invoke it as `python -m <module>` so the connectivity probe also confirms module availability despite PATH differences.
* **Fallback reminder:** If a CLI helper such as `pre-commit` is not yet on the PATH, rerun the command via its module form (for example, `python -m pre_commit run --all-files`) so the initial check still succeeds.
* **Online mode:** When a network connection is available you may install or update missing development tooling (for example, `pip`, `pip-tools`, `pre-commit`, `rustup`, `node`, `jq`) whenever it is necessary for maintenance or local verification.
* **Offline mode:** When no connection is available, limit the work to local analysis only and call out any follow-up actions that must happen once connectivity is restored.

### Module invocation primer

Some developer tools register entry points inside isolated Python environments that differ from the shell `PATH`. When a command is missing, rerun it in module form to guarantee it resolves the correct interpreter:

* **Python packaging:** `python -m pip ...` (install, inspect, or run connectivity probes).
* **Pre-commit hooks:** `python -m pre_commit ...` (install, list, or run hooks when `pre-commit` is not yet on the PATH).
* **Formatters and linters:** Module-friendly tools (`python -m ruff`, `python -m pytest`, etc.) keep working even when scripts are shadowed by other environments.
* **Repository utilities:** Project helpers with `__main__` shims (`python -m script.sync_translations`, for example) follow the same pattern.

Prefer the executable name when it is available; fall back to the module form whenever onboarding, switching interpreters, or recovering from environment churn.

---

## 1) What must be in **every** PR (lean checklist)

* **PR template alignment.** Complete [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md) and keep the responses synchronized with the items listed below.
* **AGENTS upkeep.** Before opening a PR, review all applicable `AGENTS.md` files and update them when improvements or corrections are evident.
* **Contributor guidance hygiene.** Verify that root and scoped `AGENTS.md` files remain accurate. When code or tests touch related automation or guidance, review and update the impacted `.github` workflows/templates, shared test utilities, and documentation so they stay current.
  * **Mypy override ordering.** Append new strictly-typed modules to the override list in `pyproject.toml` in alphabetical order so future reviews can spot additions quickly.
  * **Test scaffolding reference.** The Home Assistant config flow stubs and helper behaviors for tests are documented in [`tests/AGENTS.md`](tests/AGENTS.md); point future contributors there whenever discovery/update helpers change.
  * **Task scheduling helpers.** Home Assistant-style test doubles for `async_create_task` may accept only `(coro)` without keyword arguments like `name`. Design scheduling wrappers so they gracefully handle both signatures and still attach error-handling callbacks when a task object is returned.
  * **Discovery callbacks.** Reuse `ha_typing.callback` for new discovery helper callbacks so strict mypy keeps enforcing the typed decorator instead of drifting back to untyped shims.
* **pre-commit.ci automation.** The GitHub App is enabled with permission to push formatting fixes to PR branches whenever the configured hooks (e.g., `ruff`, `ruff-format`) report autofixable issues; keep `.pre-commit-config.yaml` aligned with the enforced checks.
  * **TOC upkeep.** Generate the root `AGENTS.md` overview with `pre-commit run doctoc --files AGENTS.md` whenever headings change so the local Table of Contents stays synchronized.
* **Hassfest auto-sort workflow.** `.github/workflows/hassfest-auto-fix.yml` must remain present and operational so manifest key ordering issues are auto-corrected and pushed back to PR branches; update the workflow when hassfest or `git-auto-commit-action` inputs change upstream.
* **Purpose & scope.** PR title/description state *what* changes and *why*, and which user scenarios are affected.
* **Tests — creation & update (MUST).** Any code change ships unit/integration tests that cover the change; every bug fix includes a **regression test** (§3.2). Never reduce existing coverage without a follow-up to restore it.

  * **Auto-corrections applied:** trivial test failures (syntax, imports, obvious assertion drift) are automatically fixed when unambiguous (§3.1).
  * **Regression test added:** for `fix:` commits (or `fix/...` branches), add a minimal regression test if none existed (§3.2).
* **Deprecation remediation.** Investigate and resolve every `DeprecationWarning` observed during implementation, local verification, or CI. Prefer code changes over warning filters; if a warning must persist, document the upstream blocker in the PR description with a follow-up issue reference.
* **Coverage targets.** Keep **config flow at 100 %**; repo total **≥ 95 %**. If temporarily lower due to necessary code removal, **open a follow-up issue** to restore coverage and reference it in the PR.
* **Behavioral safety.** No secrets/PII in logs; user-visible errors use translated `translation_key`s; entities report `unavailable` on communication failures.
* **Docs/i18n (when user-facing behavior changes).** Update `README.md` and every relevant translation; avoid hard-coded UI strings in Python. Make sure placeholders/keys referenced in Python files match `strings.json` and `translations/en.json`. Afterwards synchronize every file in `custom_components/googlefindmy/translations/*.json` (for example, via `python script/sync_translations.py`, `pre-commit run translations-sync`, or `git diff -- custom_components/googlefindmy/translations`). The helper at `script/sync_translations.py` will overwrite the base language with `strings.json` and backfill other locales (use `--check` during CI-style verification). Follow **Rule §9.DOC** so documentation and docstrings stay intact. Document any CI/test guidance adjustments directly in the PR description so automation notes remain accurate.
  * **Pre-commit ready translations.** Run `python script/sync_translations.py` before committing any translation updates so the local pre-commit hook (`script/sync_translations.py --check`) passes without extra iterations.
* **Deprecation check (concise).** Add 2–4 bullets with links to HA release notes/dev docs that might affect this change (§8).
* **Quality-scale evidence (lightweight).** If a Quality-Scale rule is touched, append one evidence bullet in `quality_scale.yaml` (or propose adding the file). **Do not block** if it’s missing—note this in the PR.
* **Historical context.** For regressions, reference implementations, or suspected newly introduced bugs, inspect the relevant commit history (e.g., `git log -- <file>`, `git show <commit>`, `git diff <old>..<new>`).

> **Local run (VERIFY)**
> **Offline mode:**
> – pre-commit run --all-files *(mandatory, even if pre-commit.ci could supply autofixes)*
> – ruff format --check *(mandatory; capture the outcome in the "Testing" section)*
> – mypy --strict --explicit-package-bases --exclude 'custom_components/googlefindmy/NovaApi/' custom_components/googlefindmy tests *(mandatory for Python changes; capture the outcome)*
> – pytest -q *(mandatory; investigate and resolve every `DeprecationWarning`; capture the outcome)*
>
> **Online mode (in addition to the offline steps):**
> – pip install -r requirements-dev.txt *(install or update missing dependencies)*
> – pre-commit install *(make sure hooks are installed)*
> – pre-commit run --all-files *(run again if new hooks were installed)*
> – ruff format --check *(reconfirm that formatting is correct)*
> – pytest -q *(reconfirm that the tests pass)*
> – mypy --strict --explicit-package-bases --exclude 'custom_components/googlefindmy/NovaApi/' custom_components/googlefindmy tests *(full run across the entire codebase and tests)*
> – ruff check --fix --exit-non-zero-on-fix && ruff check *(optional when additional linting fixes are necessary)*
> – pip-compile requirements-dev.in && pip-compile custom_components/googlefindmy/requirements.in *(when the corresponding `*.in` inputs exist; otherwise, record that no compile targets are present and skip without creating ad-hoc `requirements.txt` files)*
> – pip-audit -r requirements-dev.txt -r custom_components/googlefindmy/requirements.txt *(security scan; exit code 1 is acceptable but must be noted)* — prefer `python -m pip_audit` so the tool resolves even when the entry point directory is absent from `$PATH`
> – review package/version updates and synchronize lock files/manifests as needed (see the "Home Assistant version & dependencies" section)
> – rerun the relevant tests/linters after dependency updates
>
> *(The helper `python script/local_verify.py` covers Ruff + Pytest as a quick pass; run it in addition to—but never instead of—the mandatory steps above. Document every component you run manually.)*
> *Hassfest validation now runs in CI via `.github/workflows/hassfest-auto-fix.yml`; rely on that workflow and re-run it from the PR UI whenever you need a fresh manifest check.*
>
> **optional escalation:** `PYTHONWARNINGS=error::DeprecationWarning pytest -q` *(turns new deprecations into hard failures so they cannot be overlooked—clear the root cause or document the upstream blocker before retrying without the flag).*

## Home Assistant version & dependencies

* **Compatibility target:** Keep the integration working with the latest Home Assistant stable release. When older releases become incompatible, document the oldest supported version in the README or release notes.
* **Synchronization points:** Keep `custom_components/googlefindmy/manifest.json`, `custom_components/googlefindmy/requirements.txt`, `pyproject.toml`, and `requirements-dev.txt` aligned. When bumping versions, check whether other files (for example, `hacs.json` or helpers under `script/`) must change as well.
* **Upgrade workflow:** With internet access, perform dependency maintenance via `pip install`, `pip-compile`, `pip-audit`, `poetry update` (if relevant), and `python -m pip list --outdated`. Afterwards rerun tests/linters and document the outcomes.
* **Change notes:** Record adjusted minimum versions or dropped legacy releases in the PR description and, when needed, in `CHANGELOG.md` or `README.md`.

## Maintenance mode

* **Activation:** Maintenance mode is triggered explicitly through a maintainer request, an issue label, or a direct user request. Confirm in your response or PR description that maintenance mode is active.
* **Required checks:** Run the full suite—`mypy --strict` across the entire repository (with no exclusions), complete test suites (`pytest` plus any integration/end-to-end tests), `ruff check`, `ruff format --check`, `pre-commit run --all-files`, `pip-compile`, `pip-audit`, configuration/manifest synchronization (see "Home Assistant version & dependencies"), and documentation alignment. Launch CLI utilities with `python -m` (for example, `python -m pre_commit run --all-files`, `python -m piptools compile`, `python -m pip_audit`) to avoid PATH-related "command not found" failures when the virtualenv bin directory is not exported by default.
* **Connectivity caveat:** The managed environment occasionally blocks TLS handshakes for PyPI-hosted metadata, which causes `pip-audit` (and other HTTPS-dependent checks) to fail with `SSLError: CERTIFICATE_VERIFY_FAILED`. When this happens, document the failure in the PR/testing summary, capture the offending package URL, and proceed with the remaining maintenance tasks instead of repeatedly retrying the audit.
* **Configuration alignment:** Ensure configuration files (`pyproject.toml`, `.pre-commit-config.yaml`, `manifest.json`, requirement files) reflect the same dependency versions and document the synchronization.
* **Completion notice:** When finished, report whether another maintenance run is required (for example, due to pending upstream fixes) or maintenance mode can be closed. Capture any remaining TODOs or follow-up tasks.

---

## 2) Roles (right-sized)

### 2.1 Contributor (implementation) — **accountable for features/fixes/refactors**

* Deliver code **with matching tests** (new/updated) for the changed behavior.
* **Auto-correct trivial test failures** flagged by CI/lint/static checks (§3.1).
* Use `DataUpdateCoordinator`; raise `UpdateFailed` (transient) and `ConfigEntryAuthFailed` (auth).
* Keep runtime objects on `entry.runtime_data` (typed); avoid module-global singletons.
* Inject the session via `async_get_clientsession(hass)`; never create raw `ClientSession`.
* Entities: stable `unique_id`, `_attr_has_entity_name = True`, correct `device_info` (identifiers/model), proper device classes & categories; noisy defaults disabled.
* **Docstrings & typing.** English docstrings for public classes/functions; full type hints; track the **current HA core baseline** for Python/typing strictness.
* **Strict typing for touched modules.** Run the repository mypy command (see "Local run") and resolve all `--strict` findings for every modified Python file, including tests, before finalizing a PR.

### 2.2 Reviewer (maintainer/agent) — **accountable for correctness**

* Verify the PR checklist, **test adequacy** (depth & quality), and token-cache safety (§4).
* **Test adequacy includes:** presence **and** coverage of **core logic, edge cases, and error paths** relevant to the change (not just line coverage).
* Provide **actionable feedback** on missing/inadequate tests. The contributor fixes failing tests; reviewers may add minimal tests directly if quicker/obvious.
* May request **follow-ups** when repo-wide targets (coverage/typing/docs) are momentarily below goals, without blocking urgent bugfixes.

*(If you use “Code / QA / Docs” sub-roles internally, map them onto this Contributor/Reviewer model; do not require three separate formal sign-offs.)*

### 2.3 History analysis best practices

* Review commit messages surrounding the affected modules to understand intent and regression windows.
* Compare diffs across the relevant commits or branches to pinpoint behavioral changes before coding fixes.
* Correlate suspect changes with coverage in `tests/`, updating or extending tests alongside code adjustments.

---

## 3) Test policy — explicit AI actions

### 3.1 Automatic **Test Corrections** (MUST)

The agent **must automatically fix** tests within the PR when failures are **unambiguous**:

* **Examples:** syntax errors, import/module path mistakes, straightforward assertion updates after renamed parameters/return types, typographical mistakes in test names or markers.
* **Boundaries:** Do **not** auto-change tests when behavior/requirements are unclear (e.g., semantic disagreements, flaky timing without a clear fix). In those cases, request targeted reviewer guidance.

### 3.2 Automatic **Regression Tests** for fixes (MUST)

When a change is a bug fix (**commit type** `fix:` or **branch** `fix/...`) and **no existing test** covers the failure mode:

* Create a **minimal regression test** that **fails without** the fix and **passes with** the fix, isolating the precise scenario (no extra scope).
* Prefer the closest relevant file/naming: `tests/test_<area>_...py`. For config-flow fixes, put it in `test_config_flow.py`; for token/cache issues, `test_token_cache.py`.
* If multiple permutations exist, cover the **single most representative** one; add more only if they catch distinct behaviors.

### 3.3 Opportunistic **Test Optimization** (SHOULD SUGGEST)

* Suggest improvements **only as a by-product** of other work (no dedicated optimization sweep): e.g., use `pytest.mark.parametrize`, simplify redundant mocks/fixtures, remove unreachable branches, replace sleeps with time freezing.
* Offer suggestions as **optional** PR comments/notes; avoid churn unless the gain is **significant** (e.g., **> 20 % speedup**, major readability/flake reduction).

### 3.4 Definition of Done for tests

* **Deterministic:** no sleeps/time-races; use time freezing/monkeypatching.
* **Isolated:** no live network; inject HA web sessions; mock external I/O at the boundary.
* **Readable:** clear arrange/act/assert, meaningful names, minimal fixture magic.
* **Value-dense:** each test protects a distinct behavior; avoid near-duplicates.
* **Fast:** prefer coordinator plumbing and targeted mocks over slow end-to-end paths.

### 3.5 Temporary coverage dips

If necessary changes reduce coverage below target, **open a follow-up issue** to restore it and reference it in the PR; do not allow repeated dips.

---

## 4) **Token cache handling** (hard requirement; regression-prevention)

**WARNING: Incorrect token cache handling can lead to severe security vulnerabilities (cross-account data exposure). Strict adherence to these rules is mandatory.**

* **Single source of truth:** Keep an **entry-scoped** `TokenCache` (HA Store if applicable). No extra globals.
* **Pass explicitly:** Thread the `TokenCache` through every call chain (`__init__` → API/clients → coordinator → entities). **No implicit lookups** or module-level fallbacks.
* **Refresh strategy:**

  * Detect expiry proactively; refresh **synchronously on the calling path** or fail fast with a translated error.
  * No background refreshes that can race with requests.
  * On refresh failure, raise `ConfigEntryAuthFailed` to trigger reauth (avoid infinite loops).
* **Auditability:** All cache writes go through one adapter with structured debug logs (never secrets).
* **Tests:** Include regressions for stale tokens, cross-account bleed, and refresh races.

---

## 5) Security & privacy guards

* **Never log** tokens, email addresses, precise coordinates, device IDs, or raw API payloads.
* **Diagnostics redaction:** use a central `TO_REDACT` list in `diagnostics.py`.
* **HTTP views & map tokens:** no secrets in URLs; server-side validation; short-lived, entry-scoped tokens.
* **Data minimization:** store only what is necessary (HA Store); document retention in README.
* **Network:** set timeouts; use backoff; fail closed on uncertainty.
* **Redact rigorously:** ensure not only direct secrets but also potentially identifying **derived information** (e.g., user-provided device names if sensitive, correlated external IDs) are redacted from logs and diagnostics.

---

## 6) Expected **test types & coverage focus**

Prioritize a small but protective suite:

1. **Config flow** — user flow (success/invalid), duplicate abort (`async_set_unique_id` + `_abort_if_unique_id_configured`), connectivity pre-check, **reauth** (success/failure → reload on success), **reconfigure** step.
2. **Lifecycle** — `async_setup_entry`, `async_unload_entry`, **reload** (no zombie listeners; entities reattach cleanly).
3. **Coordinator & availability** — happy path; transient errors raise `UpdateFailed`; entities flip to `unavailable`; single “down/back” log.
4. **Diagnostics** — `diagnostics.py` returns data with strict **redaction** (no tokens/emails/locations/IDs).
5. **Services** — success/error paths with localized messages; throttling/rate-limits where applicable.
6. **Discovery & dynamic devices** (if supported) — announcement, IP update, add/remove devices post-setup.
7. **Token cache** — expiry detection, refresh, failure propagation, no hidden fallbacks (§4).

---

## 7) Quality scale (practical, non-blocking)

* Maintain a **light** `custom_components/googlefindmy/quality_scale.yaml` to record **rule IDs** touched and a short **evidence** pointer (file+line / test name / PR link).
* If the file is missing, **do not block**—propose adding a minimal stub or open a follow-up task.
* Reviewers decide the final level when relevant; `hassfest` validates presence/schema in CI.

**Platinum hot-spots to double-check**

* Async dependency; injected web session; strict typing.
* Config flow (user/duplicate/reauth/reconfigure); unload/reload robustness.
* Diagnostics redaction; discovery/network info updates (if used).

---

## 8) Deprecation check (concise, per PR)

Add to the PR description:

* **Versions scanned:** current HA ± 2 releases.
* **Notes found:** 2–4 bullets with links to relevant release notes/developer docs (or “none found”).
* **Impact here:** “none”, or one-liners on code/tests/docs you adjusted.

*(When unsure, add a follow-up item; don’t block urgent fixes.)*

---

## 9) Docs & i18n (minimal but strict)

* No hard-coded UI strings in Python.
* Translation hygiene (MUST) — keep `strings.json` and `translations/*.json` in sync:
  * [ ] Update **every** locale file in `translations/*.json` **and** `strings.json` whenever translation keys or formatted placeholders change.
  * [ ] Cross-check locale files for missing keys, placeholder mismatches, or obsolete entries before submitting the PR.
  * [ ] Run the documented helper command (if available) to compare locales; otherwise perform a manual diff review to confirm parity and note the method in the PR.
* Translate service and exception texts (`translation_key`).
* Update README only when user-visible behavior/options change.
* **Rule §9.DOC (canonical):** Keep documentation and docstrings accurate for existing features by correcting errors and augmenting missing details without shortening or deleting content. When functionality is intentionally removed or deprecated, remove or reduce the corresponding documentation to reflect that change while preserving historical clarity.
  * **Decision algorithm:** IF functionality is intentionally removed/deprecated → update or remove the related documentation to match the removal (include deprecation context as needed); ELSE → correct/augment the documentation without shortening it.

---

## 10) Local commands (VERIFY)

* `pre-commit run --all-files`
* `pytest -q`

> **Hassfest runs in CI.** The `.github/workflows/hassfest-auto-fix.yml` workflow
> validates manifests on every push/PR and auto-commits any key ordering fixes.
> Review the workflow output instead of attempting a local run; when you need a
> fresh validation, use the **Run workflow** button in the Actions tab or re-run
> the job from the PR UI.

### 10.1 Type-checking policy — mypy strict on edited Python files

**Applicability:** Only the Python files touched by the current edit/PR (generated
artifacts remain exempt when explicitly flagged by repo configuration).

1. **Identify Python paths.** Use `git diff --name-only --relative -- '*.py'`
   against `HEAD` and narrow the list to files actually modified in this edit. If
   Git metadata is unavailable, derive the list directly from the edit context.
2. **Respect local configuration.** When `pyproject.toml`, `mypy.ini`,
   `.mypy.ini`, or `setup.cfg` exists, invoke `mypy` without extra overrides so the
   repository configuration remains authoritative.
3. **Run the strict check.** Execute `mypy -q <changed-files>` when a
   configuration file is present; otherwise run `mypy -q --strict <changed-files>`.
   Install/upgrade mypy locally (`python -m pip install --upgrade mypy`) if it is
   missing.
4. **Resolve every diagnostic.** Add precise type annotations, tighten `Optional`
   handling, and avoid blanket `Any`. Use `# type: ignore[...]` only when the
   specific error code cannot be eliminated and document the rationale nearby.
5. **Re-run until clean.** Repeat the command in step 3 until mypy exits with code
   0 for all edited files.

**Acceptance criteria:**

* Clean mypy run (exit code 0) covering every touched Python file.
* No new broad or unscoped `# type: ignore` directives.
* Repository-level mypy configuration is honored; fallback strict mode is used
  only when no configuration file exists.

---

## 11) Clean & Secure Coding Standard (Python 3.12 + Home Assistant 2025.10)

### 11.1 Language & style (self-documenting)

* **PEP 8/PEP 257 mandatory.** Consistent formatting, clear docstrings; meaningful names.
* **Typing is strict.** Use Python typing everywhere; prefer **PEP 695** generics where helpful.
* **Exceptions.** Raise precise types; use **`raise … from …`** to preserve causal chains; avoid broad `except:`; never swallow errors silently.
* **Docstrings.** Every public function/class has an English docstring (purpose first, then Args/Returns/Raises, short example) and follows **Rule §9.DOC**.
* **File header path line (REQUIRED).** Every Python file must begin with a single-line comment containing its repository path. Example first line: `# custom_components/googlefindmy/binary_sensor.py`

### 11.2 Security baseline (OWASP / NIST / BSI)

**Input validation & injection**

* **Never** use `eval`/`exec`. For literals, use `ast.literal_eval`.
* Subprocess: **no `shell=True`** for untrusted data; pass argv lists; use `shlex.quote` if you must touch shell.
* Use parameterized queries for SQL/LDAP/XML; sanitize file names and paths.

**(De)serialization**

* **Do not** use `pickle`/`marshal`/`yaml.load` on untrusted data; prefer JSON or `yaml.safe_load`.
* On archive extraction (`tarfile`/`zipfile`), normalize and validate target paths to prevent traversal.

**Cryptography & secrets**

* Use the `secrets` module for tokens/keys; never `random` for security.
* Follow **BSI TR-02102-1** for algorithms and key sizes; prefer library defaults that meet these constraints.

**Logging & privacy**

* **Redact** tokens, PII, coordinates, device IDs.
* Use a central redaction list in diagnostics; keep logs actionable yet non-sensitive.

**Supply chain**

* Pin dependencies and enable pip **hash checking** (`--require-hashes`).
* Generate an **SBOM** (CycloneDX) and scan it (e.g., Dependency-Track).
* Fail CI on known critical vulnerabilities.

### 11.3 Async, concurrency & cancellation

* **Async-first**: no event-loop blocking; for blocking work use `asyncio.to_thread`.
* Use **`asyncio.TaskGroup`** for structured concurrency where suitable.
* Cancel correctly (`task.cancel(); await task`) and handle `CancelledError`. Use `asyncio.shield` only for small critical sections.

### 11.4 File system & I/O (safe & gentle)

* Use `pathlib`; validate roots; prefer atomic writes (temp → `replace`).
* Batch/dedupe writes via coalescing; avoid chatty flush patterns.
* Cache pure computations with `functools.lru_cache`; define invalidation/TTL strategy where relevant.

### 11.5 Guard catalog & error messages

* **Existence/type/range** guards before access (`is None`, `isinstance`, length/bounds).
* **Path guards** (`Path.resolve()`, `is_relative_to`) to prevent traversal.
* **Network guards**: sane timeouts, retry with backoff/jitter, TLS verification enabled.
* **Deserialization guards**: format allow-list, schemas, safe loaders.
* **Error messages**: specific cause + actionable hint; no vague “failed”.

### 11.6 Performance without feature loss

* Avoid busy-waiting; use backoff/jitter; coalesce duplicate work.
* Prefer **coordinator-based** data fetch (one fetch per resource/account per tick).
* Stream large I/O; avoid unnecessary (de)serialization; minimize filesystem churn.

### 11.7 Home Assistant specifics (must-haves)

* Network: **inject** the web session (`async_get_clientsession(hass)`); do not create ad-hoc sessions.
* Instance URL: use `homeassistant.helpers.network.get_url(hass, …)`.
* Data: centralize periodic fetch in a **DataUpdateCoordinator**; push > poll when available.
* Config flow: **test before configure**; localized errors; duplicate-account abort; reauth & reconfigure paths.
* Repairs/Diagnostics: provide both; redact aggressively.
* Storage: use `helpers.storage.Store` for tokens/state; throttle writes (batch/merge).

### 11.8 Release & operations

* CI **security gate**: lint/type/tests/SBOM scan must pass.
* Logs are **incident-ready** but privacy-preserving (use OWASP vocabulary).
* All doc updates comply with **Rule §9.DOC**.

### 11.9 Machine-checkable acceptance checklist (for the agent)

* [ ] PEP 8/257 compliance; complete docstrings maintained per **Rule §9.DOC**.
* [ ] Strict typing incl. PEP 695 where relevant; no implicit `Any` in public APIs.
* [ ] No `eval/exec`; subprocess without `shell=True`; parameterized I/O; safe loaders.
* [ ] Archive extraction is traversal-safe; paths validated with `pathlib`.
* [ ] `secrets` used for tokens; cryptography aligns with BSI TR-02102-1 guidance.
* [ ] Logs/diagnostics redact tokens, PII, coordinates, device IDs, and derived identifiers.
* [ ] Dependencies pinned; pip `--require-hashes`; CycloneDX SBOM generated and scanned.
* [ ] Async: no loop blockers; `to_thread`/`TaskGroup`; proper cancel handling.
* [ ] I/O optimized (batch/atomic); caches with clear TTL/invalidations.
* [ ] HA-specific: Coordinator, injected session, `get_url`, config-flow test, Repairs/Diagnostics, HA Store.
* [ ] Tests: cover happy/edge/error paths; regressions for fixes; deterministic/time-safe.
* [ ] Local verify commands passed (`pre-commit`, `hassfest`, `pytest`).

---

## REFERENCES

### 1) Python 3.12 — Language, Style, Typing, Safety

* PEP 8 – Style Guide: [https://peps.python.org/pep-0008/](https://peps.python.org/pep-0008/)
* PEP 257 – Docstring Conventions: [https://peps.python.org/pep-0257/](https://peps.python.org/pep-0257/)
* PEP 695 – Type Parameter Syntax (Generics): [https://peps.python.org/pep-0695/](https://peps.python.org/pep-0695/)
* What’s New in Python 3.12: [https://docs.python.org/3/whatsnew/3.12.html](https://docs.python.org/3/whatsnew/3.12.html)
* Exceptions & `raise … from …` (tutorial): [https://docs.python.org/3/tutorial/errors.html](https://docs.python.org/3/tutorial/errors.html)
* `asyncio.TaskGroup` (structured concurrency): [https://docs.python.org/3/library/asyncio-task.html#taskgroups](https://docs.python.org/3/library/asyncio-task.html#taskgroups)
* `subprocess` — security considerations / avoid `shell=True`: [https://docs.python.org/3/library/subprocess.html#security-considerations](https://docs.python.org/3/library/subprocess.html#security-considerations)
* Shell escaping via `shlex.quote`: [https://docs.python.org/3/library/shlex.html#shlex.quote](https://docs.python.org/3/library/shlex.html#shlex.quote)
* `pickle` — security limitations (avoid for untrusted data): [https://docs.python.org/3/library/pickle.html#security-limitations](https://docs.python.org/3/library/pickle.html#security-limitations)
* Safe literal parsing via `ast.literal_eval`: [https://docs.python.org/3/library/ast.html#ast.literal_eval](https://docs.python.org/3/library/ast.html#ast.literal_eval)
* `tarfile` — extraction & path traversal note: [https://docs.python.org/3/library/tarfile.html#tarfile.TarFile.extractall](https://docs.python.org/3/library/tarfile.html#tarfile.TarFile.extractall)
* `zipfile` — untrusted archives & traversal note: [https://docs.python.org/3/library/zipfile.html#zipfile-objects](https://docs.python.org/3/library/zipfile.html#zipfile-objects)

### 2) Home Assistant (Developer Docs, 2024–2025)

* Fetching data (DataUpdateCoordinator): [https://developers.home-assistant.io/docs/integration_fetching_data/](https://developers.home-assistant.io/docs/integration_fetching_data/)
* Inject web session (`async_get_clientsession`/httpx): [https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/inject-websession/](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/inject-websession/)
* Test connection before configure (Config Flow): [https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/test-before-configure/](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/test-before-configure/)
* Blocking operations (keep event loop clean): [https://developers.home-assistant.io/docs/asyncio_blocking_operations](https://developers.home-assistant.io/docs/asyncio_blocking_operations)
* Appropriate polling intervals: [https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/appropriate-polling/](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/appropriate-polling/)
* Entity unavailable on errors: [https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable/](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/entity-unavailable/)
* Integration setup failures & reauth: [https://developers.home-assistant.io/docs/integration_setup_failures/](https://developers.home-assistant.io/docs/integration_setup_failures/)
* Integration file structure (coordinator.py, entity.py, …): [https://developers.home-assistant.io/docs/creating_integration_file_structure/](https://developers.home-assistant.io/docs/creating_integration_file_structure/)
* Diagnostics (redact sensitive data): [https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/diagnostics/)
* Integration diagnostics (`async_redact_data`): [https://developers.home-assistant.io/docs/core/integration_diagnostics/](https://developers.home-assistant.io/docs/core/integration_diagnostics/)
* Repairs platform (issue registry & flows): [https://developers.home-assistant.io/docs/core/platform/repairs/](https://developers.home-assistant.io/docs/core/platform/repairs/)
* Repairs (user docs): [https://www.home-assistant.io/integrations/repairs/](https://www.home-assistant.io/integrations/repairs/)
* Secrets (`!secret`): [https://www.home-assistant.io/docs/configuration/secrets/](https://www.home-assistant.io/docs/configuration/secrets/)

### 3) Secure Development — Standards & Guidance

* NIST SP 800-218 — Secure Software Development Framework (SSDF): [https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-218.pdf](https://nvlpubs.nist.gov/nistpubs/specialpublications/nist.sp.800-218.pdf)
* BSI TR-02102-1 — Cryptographic mechanisms & key lengths: [https://www.bsi.bund.de/SharedDocs/Downloads/DE/BSI/Publikationen/TechnischeRichtlinien/TR02102/BSI-TR-02102-1.pdf](https://www.bsi.bund.de/SharedDocs/Downloads/DE/BSI/Publikationen/TechnischeRichtlinien/TR02102/BSI-TR-02102-1.pdf)

#### OWASP Cheat Sheet Series (selected)

* Injection Prevention Cheat Sheet: [https://cheatsheetseries.owasp.org/cheatsheets/Injection_Prevention_Cheat_Sheet.html](https://cheatsheetseries.owasp.org/cheatsheets/Injection_Prevention_Cheat_Sheet.html)
* OS Command Injection Defense: [https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html](https://cheatsheetseries.owasp.org/cheatsheets/OS_Command_Injection_Defense_Cheat_Sheet.html)
* Deserialization Cheat Sheet: [https://cheatsheetseries.owasp.org/cheatsheets/Deserialization_Cheat_Sheet.html](https://cheatsheetseries.owasp.org/cheatsheets/Deserialization_Cheat_Sheet.html)
* Logging Cheat Sheet: [https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Cheat_Sheet.html)
* Application Logging Vocabulary: [https://cheatsheetseries.owasp.org/cheatsheets/Logging_Vocabulary_Cheat_Sheet.html](https://cheatsheetseries.owasp.org/cheatsheets/Logging_Vocabulary_Cheat_Sheet.html)
* OWASP Top 10 ↔ Cheat Sheets index: [https://cheatsheetseries.owasp.org/IndexTopTen.html](https://cheatsheetseries.owasp.org/IndexTopTen.html)

### 4) Software Supply Chain & Reproducibility

* pip — Secure installs (`--require-hashes`, `--only-binary`): [https://pip.pypa.io/en/stable/topics/secure-installs/](https://pip.pypa.io/en/stable/topics/secure-installs/)
* pip-tools — `pip-compile`: [https://pip-tools.readthedocs.io/en/latest/cli/pip-compile/](https://pip-tools.readthedocs.io/en/latest/cli/pip-compile/)
* CycloneDX Python SBOM Tool: [https://cyclonedx-bom-tool.readthedocs.io/](https://cyclonedx-bom-tool.readthedocs.io/)
* Dependency-Track (SBOM/SCA): [https://docs.dependencytrack.org/](https://docs.dependencytrack.org/)

### 5) Repo & Documentation Hygiene (GitHub/Markdown)

* GitHub — Community health files overview: [https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/about-community-profiles-for-public-repositories)
* Default community health files (`.github` repo): [https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file](https://docs.github.com/en/communities/setting-up-your-project-for-healthy-contributions/creating-a-default-community-health-file)
* Organization-wide health files (GitHub changelog): [https://github.blog/changelog/2019-02-21-organization-wide-community-health-files/](https://github.blog/changelog/2019-02-21-organization-wide-community-health-files/)
* Relative links in Markdown (GitHub blog): [https://github.blog/news-insights/product-news/relative-links-in-markup-files/](https://github.blog/news-insights/product-news/relative-links-in-markup-files/)
* CommonMark spec (current) — link reference definitions: [https://spec.commonmark.org/current/](https://spec.commonmark.org/current/)

### 6) In-repo Find My Device Network protocol reference

* [`custom_components/googlefindmy/FMDN.md`](custom_components/googlefindmy/FMDN.md) — canonical reference detailing cryptography, provisioning flows, BLE behavior, and failure modes underpinning modules such as [`custom_components/googlefindmy/api.py`](custom_components/googlefindmy/api.py), [`custom_components/googlefindmy/coordinator.py`](custom_components/googlefindmy/coordinator.py), and the BLE parsers in [`custom_components/googlefindmy/ProtoDecoders/`](custom_components/googlefindmy/ProtoDecoders/).

See also: [BOOKMARKS.md](custom_components/googlefindmy/BOOKMARKS.md) for additional, curated reference URLs.

## High-confidence & sourcing standard for Codex

This section originates from the maintainer request and governs every answer, patch, refactor, review, commit, diff proposal, and architectural recommendation produced by the AI (“Codex”) inside this repository. It supplements existing quality requirements (for example, mypy strict for touched files, Home Assistant best practices) and is mandatory.

### 0. Scope

The rules apply to the entire repository and every interaction where Codex proposes or reviews code.

### 1. High-confidence mode (≥ 90 %)

*Codex must only deliver concrete code when it is at least 90 % confident that:*

1. the change is syntactically valid;
2. the change matches the project’s architecture, patterns, and compatibility expectations;
3. the change respects all known constraints (Home Assistant Core guidelines, existing conventions, runtime behavior).

If confidence drops below 90 %:

* do **not** change code;
* do **not** invent new function signatures, imports, or APIs;
* respond with “I do not have reliable information for that—I do not know.”;
* immediately request the missing evidence from the user (see §4 “User as data source”).

Confidence is < 90 % whenever, for example:

* the relevant API version is unclear;
* a breaking change is suspected but cannot be demonstrated;
* project conventions (exception classes, logging patterns, helper utilities) are unknown;
* backward compatibility requirements are uncertain.

### 2. Mandatory evidence

Every recommendation—code, architecture, migration, best practice—must reference a verifiable source inside the project reality.

Acceptable sources include:

* existing repository code (including tests);
* previously established project standards (documented in `AGENTS.md`, `CONTRIBUTING.md`, docstrings, comments);
* explicit, user-confirmed instructions (for example, “Use `homeassistant.helpers.network.get_url(self.hass)` instead of the deprecated `async_get_url()`”);
* official upstream interfaces (Home Assistant helpers, `DataUpdateCoordinator`, config flow contracts) **when** Codex has direct access to them.

When the evidence lives inside this repository, cite the exact file and symbol. If the evidence only exists upstream (Home Assistant developer docs, changelog, breaking changes) and is not available here, name it explicitly and refrain from modifying code until the user supplies the relevant excerpt (see §4).

Assertions without sources imply confidence < 90 % → no code changes.

### 3. Workflow for code changes

Before proposing a change Codex must:

1. **Analyze the problem.** Summarize what must change (bug fix, refactor, API migration, new test) and highlight the affected files or sections.
2. **Compare with project standards.** Identify existing patterns to reuse and ensure no established rule is violated (for example, deprecated helpers, translation handling, config flow ordering).
3. **Propose only with ≥ 90 % confidence.** Provide a full, executable patch backed by the cited evidence.
4. **Verify consistency.** Explain how the patch remains backward compatible and why it should pass mypy strict for the modified files.

### 4. User as data source

When external information is missing (for example, an up-to-date Home Assistant helper signature, a breaking change notice, an upstream commit), Codex must:

1. Explicitly request the missing snippet from the user (“Please provide the current definition of `homeassistant.helpers.network.get_url`”, etc.).
2. Treat user-supplied excerpts as valid sources once delivered.
3. Resume high-confidence mode only after receiving the evidence.
4. If the user cannot provide the evidence, stay below 90 % confidence and avoid speculative code.

### 5. Communicate uncertainty

State uncertainty early and precisely: “I am <90 % confident because I lack the signature of `X` in your target Home Assistant version. I do not have reliable information—please provide the definition of `X`.” Do **not** rely on guesses or invented APIs.

### 6. Consequences for commits, diffs, and reviews

*With ≥ 90 % confidence:* Codex may deliver complete diffs, refactors, tests, migrations—always citing the supporting evidence.

*With < 90 % confidence:* Codex must not supply diffs. Instead, list the open information gaps and label any hypotheses as unverified. Do not commit or rely on speculative changes.

### 7. Review obligations

All critiques of existing code must also cite evidence. If a concern depends on an unverified deprecation or breaking change, immediately request the source instead of rewriting code blindly.

### 8. Required workflow summary

1. **Confidence check.** Confirm ≥ 90 % certainty for syntax, semantics, compatibility. If not, state the lack of information and request sources.
2. **Cite evidence.** Every change references a concrete repository location or confirmed directive.
3. **Deliver patch.** Provide a complete, runnable patch that follows all conventions (type hints, English docstrings, no deprecated APIs, mypy strict readiness).
4. **Post-verification.** Tell the user which commands to run locally (for example, `pytest -q`, `mypy --strict`, `ruff check`).

### 9. Rationale

The objective is reliability and auditability: every change must document its source, justification, and any unresolved assumptions. This protects code quality, compatibility, and long-term maintainability.

### 10. Post-task feedback obligations

After completing each task, Codex must finish with a brief, constructive note proposing improvements to this environment—preferably actionable suggestions for refining `AGENTS.md` or adjacent tooling—based on the experience gained during the most recent assignment.

## pip-audit workflow guidance (CORRECTION — April 2025)

**pip-audit reality check (v2.9.0)**

1. `--dry-run` **is supported**. In report-only mode (`--dry-run` without `--fix`), pip-audit skips the security audit and therefore emits no vulnerability JSON. In fix mode (`--fix --dry-run`), pip-audit performs the audit, prints the planned remediation, and makes no changes. Use the flag only when you intentionally want that behavior.
2. **PR audit step:** run a normal JSON audit (`pip-audit -r <file> -f json -o audit.json`) so downstream tooling (e.g., `jq`) receives real vulnerability data. Keep PR checks green by tolerating the `pip-audit` exit code `1` (vulnerabilities found) via the action’s `internal-be-careful-allow-failure` input or equivalent shell handling/`continue-on-error`.
3. **Scheduled/Manual autofix:** first generate a JSON report (no `--dry-run`) to classify fixable vs. unfixable findings, then invoke `pip-audit --fix` to apply upgrades. If you need a no-change preview, add an optional `pip-audit -r <file> --fix --dry-run` run before applying fixes.
4. **Exit codes:** pip-audit returns `0` when no vulnerabilities are present and `1` when vulnerabilities remain. Other failures return `>1`; keep these non-vulnerability errors fatal so the workflow surfaces real issues.
5. **Tooling note:** install `jq` with the runner package manager (for example, `sudo apt-get install -y jq`) so the CLI is available in the shell environment.

Sources: [pip-audit · PyPI](https://pypi.org/project/pip-audit/), [gh-action-pip-audit · GitHub](https://github.com/pypa/gh-action-pip-audit).

### 7) Type checking (mypy)

* mypy — Command line reference: [https://mypy.readthedocs.io/en/stable/command_line.html](https://mypy.readthedocs.io/en/stable/command_line.html)
* mypy — Configuration reference: [https://mypy.readthedocs.io/en/stable/config_file.html](https://mypy.readthedocs.io/en/stable/config_file.html)
* mypy — Getting started & strict mode: [https://mypy.readthedocs.io/en/stable/getting_started.html](https://mypy.readthedocs.io/en/stable/getting_started.html)
