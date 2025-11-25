<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [AGENTS.md — Operating Contract for `googlefindmy` (Home Assistant custom integration)](#agentsmd--operating-contract-for-googlefindmy-home-assistant-custom-integration)
      - [Quick reminder — path headers vs. future imports](#quick-reminder--path-headers-vs-future-imports)
  - [Linting reminders](#linting-reminders)
  - [Scoped guidance index](#scoped-guidance-index)
    - [Home Assistant helper signature changelog](#home-assistant-helper-signature-changelog)
    - [Documentation quick links](#documentation-quick-links)
      - [Deprecations & migrations](#deprecations--migrations)
    - [Runtime vs. type-checking import quick reference](#runtime-vs-type-checking-import-quick-reference)
  - [Environment verification](#environment-verification)
    - [Quickstart checks (fast path before `pytest -q`)](#quickstart-checks-fast-path-before-pytest--q)
    - [Module invocation primer](#module-invocation-primer)
  - [1) What must be in **every** PR (lean checklist)](#1-what-must-be-in-every-pr-lean-checklist)
  - [Home Assistant version & dependencies](#home-assistant-version--dependencies)
  - [Maintenance mode](#maintenance-mode)
    - [Config subentry maintenance helper](#config-subentry-maintenance-helper)
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
    - [3.6 Entity-registry migration checklist (reload-centric fixes)](#36-entity-registry-migration-checklist-reload-centric-fixes)
    - [3.7 Device registry healing checklist](#37-device-registry-healing-checklist)
  - [4) **Token cache handling** (hard requirement; regression-prevention)](#4-token-cache-handling-hard-requirement-regression-prevention)
  - [5) Security & privacy guards](#5-security--privacy-guards)
  - [6) Expected **test types & coverage focus**](#6-expected-test-types--coverage-focus)
  - [7) Quality scale (practical, non-blocking)](#7-quality-scale-practical-non-blocking)
  - [8) Deprecation check (concise, per PR)](#8-deprecation-check-concise-per-pr)
    - [Deprecation shim checklist (Home Assistant)](#deprecation-shim-checklist-home-assistant)
  - [9) Docs & i18n (minimal but strict)](#9-docs--i18n-minimal-but-strict)
  - [10) Local commands (VERIFY)](#10-local-commands-verify)
    - [10.1 Type-checking policy — mypy strict on edited Python files](#101-type-checking-policy--mypy-strict-on-edited-python-files)
  - [11) Clean & Secure Coding Standard (Python 3.13 + Home Assistant 2025.10)](#11-clean--secure-coding-standard-python-313--home-assistant-202510)
    - [11.1 Language & style (self-documenting)](#111-language--style-self-documenting)
      - [Logging formatting (ruff G004)](#logging-formatting-ruff-g004)
    - [11.2 Security baseline (OWASP / NIST / BSI)](#112-security-baseline-owasp--nist--bsi)
    - [11.3 Async, concurrency & cancellation](#113-async-concurrency--cancellation)
    - [11.4 File system & I/O (safe & gentle)](#114-file-system--io-safe--gentle)
    - [11.5 Guard catalog & error messages](#115-guard-catalog--error-messages)
    - [11.6 Performance without feature loss](#116-performance-without-feature-loss)
    - [11.7 Home Assistant specifics (must-haves)](#117-home-assistant-specifics-must-haves)
    - [11.8 Release & operations](#118-release--operations)
    - [11.9 Machine-checkable acceptance checklist (for the agent)](#119-machine-checkable-acceptance-checklist-for-the-agent)
  - [REFERENCES](#references)
    - [1) Python 3.13 — Language, Style, Typing, Safety](#1-python-313--language-style-typing-safety)
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
      - [Home Assistant regression helper](#home-assistant-regression-helper)
      - [Config flow registration fallbacks](#config-flow-registration-fallbacks)
    - [4. User as data source](#4-user-as-data-source)
    - [5. Communicate uncertainty](#5-communicate-uncertainty)
    - [6. Consequences for commits, diffs, and reviews](#6-consequences-for-commits-diffs-and-reviews)
    - [7. Review obligations](#7-review-obligations)
    - [8. Required workflow summary](#8-required-workflow-summary)
    - [9. Rationale](#9-rationale)
    - [10. Post-task feedback obligations](#10-post-task-feedback-obligations)
  - [X. PROTOCOL: Runtime API Validation (Single Source of Truth - SSoT)](#x-protocol-runtime-api-validation-single-source-of-truth---ssot)
  - [pip-audit workflow guidance (CORRECTION — April 2025)](#pip-audit-workflow-guidance-correction--april-2025)
    - [7) Type checking (mypy)](#7-type-checking-mypy)
      - [Auto-installed stub packages](#auto-installed-stub-packages)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# AGENTS.md — Operating Contract for `googlefindmy` (Home Assistant custom integration)

* [Scoped guidance index](#scoped-guidance-index)
* [Environment verification](#environment-verification)
* [What must be in every PR](#1-what-must-be-in-every-pr-lean-checklist)
* [Mandatory evidence](#2-mandatory-evidence)
* [Workflow for code changes](#3-workflow-for-code-changes)
* [Communicate uncertainty](#5-communicate-uncertainty)
* [Type checking (mypy)](#7-type-checking-mypy)

> **Language reminder:** Keep all inline comments, docstrings, and documentation updates in English. When user-provided snippets
> include other languages, translate or adapt them so the committed code remains English-only.

> **Environment reset reminder:** After a container or virtualenv reset, rerun `make test-stubs` so the Home Assistant and
> pytest stubs are reinstalled before invoking linting or pytest commands.

<a id="language-policy"></a>
> **Scope & authority**
>
> **Directory scope:** applies to the entire repository (with continued emphasis on `custom_components/googlefindmy/**` and tests under `tests/**`).
> **File headers:** Every Python file within scope must include a comment containing its repository-relative path (e.g., `# tests/test_example.py`). When a file has a shebang (`#!`), the shebang stays on the first line and the path comment immediately follows it. Some legacy files may still lack this header; reviewers should expect churn as contributors retrofit missing comments during unrelated fixes rather than blocking urgent work solely to add them.

#### Quick reminder — path headers vs. future imports

Always keep any `from __future__` imports immediately after the module docstring, even when the file starts with the repository-relative path header described above. This ordering prevents pytest's import hook from rejecting the file during rewrites.
> **Precedence:** (1) Official **Home Assistant Developer Docs** → (2) this AGENTS.md → (3) repository conventions. This file never overrides security/legal policies.
> **Language policy:** Keep the project consistently in English for documentation, inline code comments, and docstrings. (Translation files remain multilingual.)
> **Non-blocking:** Missing optional artifacts (README sections, `quality_scale.yaml`, CODEOWNERS, CI files) **must not block** urgent fixes. The agent proposes a minimal stub or follow-up task instead.
> **References:** This contract relies on the sources listed below; for a curated, extended list of links, see [BOOKMARKS.md](custom_components/googlefindmy/BOOKMARKS.md).
> **Upstream documentation hierarchy:** When consulting external guidance, prioritize Home Assistant's canonical domains in this order: developer portal (`https://developers.home-assistant.io`), user documentation (`https://www.home-assistant.io`), and the alerts/service bulletins site (`https://alerts.home-assistant.io`). If a required host is unreachable while the connectivity probe still confirms general internet access, pause implementation, request manual approval for that domain, and document the escalation before proceeding.
## Linting reminders

* After editing coordinator helpers (for example, `GoogleFindMyDataUpdateCoordinator` routines), rerun `ruff check` and `mypy --strict --install-types --non-interactive` before launching the full test suite so linting and typing regressions surface early.
* When moving entities between categories (for example, dropping `entity_category=EntityCategory.DIAGNOSTIC` to surface a control), re-run `ruff check --fix` to prune stale imports before the follow-up `ruff check` confirmation.
* Prefer importing container abstract base classes (for example, `Iterable`, `Mapping`, `Sequence`) from `collections.abc` instead of duplicating the names from `typing`. This mirrors the integration guidance under `custom_components/googlefindmy/AGENTS.md` (and the topical guides it references under `custom_components/googlefindmy/agents/`) and helps avoid ruff F811 redefinition warnings.
* Pytest warning filter: `pyproject.toml` configures `filterwarnings = ["ignore:Inheritance class HomeAssistantApplication from web.Application is discouraged:DeprecationWarning:homeassistant.components.http"]` to silence an upstream aiohttp deprecation emitted by the current Home Assistant release. Remove the filter only after Home Assistant stops subclassing `aiohttp.web.Application` (or drops the warning entirely) and a full `pytest` run verifies the warning no longer appears without the suppression.

## Scoped guidance index

> **Integration note:** Runtime, config-flow, and typing guidance for `custom_components/googlefindmy/**` now lives inside the topical files under `custom_components/googlefindmy/agents/`. The index file at `custom_components/googlefindmy/AGENTS.md` lists every available topic and should be the first stop before editing any integration module.

* [`tests/AGENTS.md`](tests/AGENTS.md) — Home Assistant config flow test stubs, helpers, discovery/update scaffolding details, **and** the package-layout note that requires package-relative imports now that `tests/` ships with an `__init__.py`. Also documents the coordinator device-registry expectations for `via_device` tuple handling so future stub updates remain aligned with Home Assistant 2025.10. The `_ensure_button_dependencies()` helper in `tests/test_button_setup.py` already prepares sufficient stubs to `import custom_components.googlefindmy.button`, so tests can reference coordinator attributes directly without reloading source snippets.
* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Full Home Assistant 2025.7+ handbook covering the architecture, config flow factories, lifecycle routing, discovery patterns, translation rules, and peer-review checklist for configuration subentries. Keep code and tests aligned with this contract. When adding concise checklists or reminders beneath an existing subsection, anchor the new block with a `####` heading so the handbook's navigation keeps the guidance grouped with its parent topic. Mirror this heading-level rule in other documentation unless a directory-specific `AGENTS.md` states otherwise.
* [`docs/AI_DEPRECATIONS_GUIDE.md`](docs/AI_DEPRECATIONS_GUIDE.md) — Core 2025.10/2025.11 technical migration playbook for deprecations, breaking changes, and behavioral shifts. Treat its critical checklist as mandatory when touching affected APIs.
  * Section VIII.D contains the new device/entity registry troubleshooting playbooks. Reference them whenever you touch `_async_setup_subentry`, registry rebuild services, or device cleanup helpers, and summarize the relevant diagnostics in your PR description.
* **Self-healing helpers:** `_async_self_heal_duplicate_entities()` in `custom_components/googlefindmy/__init__.py` documents the existing duplicate-entity cleanup flow; review it alongside the new `EntityRecoveryManager` when designing additional recovery logic.

### Home Assistant helper signature changelog

* **`ConfigEntry.async_create_background_task(hass, target, *, name=None, eager_start=True)` (Home Assistant 2025.7+)** — pass
  the `hass` instance explicitly as the first argument, followed by the awaitable/awaitable-returning target. Stubs under
  `tests/` must forward the `target` callable/awaitable to `HomeAssistant.async_create_task` so lifecycle handling mirrors the
  runtime integration. See `custom_components/googlefindmy/__init__.py` for the in-tree usage reference and keep this section
  updated whenever Home Assistant revises helper signatures relied upon by the integration.

### Documentation quick links

* [Docs & i18n requirements (§9)](#9-docs--i18n-minimal-but-strict)
* [Rule §9.DOC (documentation accuracy)](#rule-9doc-canonical)
* [Language policy (English-only docs)](#language-policy)
* [Docstrings & typing expectations (§11.1)](#docstrings--typing)
* **README developer guidance:** Place any new development workflow notes (linting, testing, tooling commands) directly under the `### Continuous integration checks` section in `README.md`. Use a top-level `###` heading for repository-wide topics and `####` subheadings for individual command lists so contributor documentation stays grouped in one location.
* **Tracker device linkage:** Tracker entities rely on Home Assistant's automatic parent-device assignment for the `TRACKER_SUBENTRY_KEY`. Do not introduce manual `via_device` pointers for tracker `DeviceInfo` payloads; only service-level entities may declare `via_device` tuples when modelling nested hardware chains documented upstream. When updating tracker devices via `async_update_device`, always include `add_config_entry_id` (or `config_entry_id` on legacy cores) whenever a tracker subentry identifier is present so Home Assistant keeps the device associated with its config entry.
* **Config entry version source of truth:** Use `custom_components/googlefindmy/const.py::CONFIG_ENTRY_VERSION` whenever flows, migrations, or tests need the integration's config-entry version. Do not introduce duplicate version constants in other modules.
* [`custom_components/googlefindmy/AGENTS.md`](custom_components/googlefindmy/AGENTS.md) — Top-level index for integration guidance. It links to the per-topic AGENT files under `custom_components/googlefindmy/agents/` (currently `config_flow`, `runtime_patterns`, and `typing_guidance`). Always consult the relevant topical file before editing `custom_components/googlefindmy/**` so you pick up the right runtime vs. typing rules (for example, tuple-based parent unloads versus per-platform subentry forwarding).
* [`custom_components/googlefindmy/ProtoDecoders/AGENTS.md`](custom_components/googlefindmy/ProtoDecoders/AGENTS.md) — Protobuf overlay structure requirements, including the mandate that generated message classes remain nominal subclasses of `google.protobuf.message.Message` so helper utilities typed against the concrete base keep accepting them.
* [`custom_components/googlefindmy/FMDNCrypto/AGENTS.md`](custom_components/googlefindmy/FMDNCrypto/AGENTS.md) — Cryptography helper typing contract that documents concrete `int` expectations for modular arithmetic and coordinate normalization across the decompression helpers.
* **Config entry reload states:** When updating `custom_components/googlefindmy/services.py` (notably the `SERVICE_REBUILD_REGISTRY` implementation), always review Home Assistant's `ConfigEntryState` enum for newly introduced states. Keep the allowed reload state set in sync so entries stuck in transitional or failure states (including `FAILED_UNLOAD`) remain recoverable via the service.
* **Platform subentry quick reference:**
  * Shared runtime data lives under `hass.data[DOMAIN]["entries"][entry_id]`. Always rebuild this mapping before child setup resumes and populate `entry.runtime_data` from the shared bucket instead of direct dictionary lookups.
  * Keep the Home Assistant service registration contract aligned with Section IV.I of `docs/CONFIG_SUBENTRIES_HANDBOOK.md`: register platform entity services inside `async_setup` and catch `ImplementationUnavailableError` during parent setup to raise `ConfigEntryNotReady`.
  * Service-level diagnostics (`binary_sensor`, diagnostic `sensor` entities, repairs counters, etc.) **must** pass `SERVICE_SUBENTRY_KEY` when constructing entities **and** expose `device_info = service_device_info(include_subentry_identifier=True)`. This guarantees every diagnostic entity binds to the same service device + config subentry pair so Home Assistant keeps the hub grouped in the device registry.
  * Per-device platforms (`device_tracker`, per-device `sensor` entities such as `last_seen`, `button` actions, future tracker-scoped entities) **must** pass `TRACKER_SUBENTRY_KEY`, publish tracker-specific identifiers in `device_info`, and rely on Home Assistant's automatic device association—do **not** add manual `via_device` tuples for tracker devices.
  * Subentry setup validation now lives alongside `_async_setup_new_subentries` in `custom_components/googlefindmy/__init__.py` with coverage in `tests/test_subentry_setup_trigger.py`; skim those guards when adjusting child setup or registry checks.
  * Registry updates **must** include the child `entry_id` in every `async_update_device` or equivalent call (`add_config_entry_id` for 2025.7+, `config_entry_id` for legacy cores). Log the `(entry_id, device_id, identifiers)` tuple in debug builds to catch mismatches early; mirror the Section VIII.D playbooks when triaging stuck or orphaned devices.
  * When `manifest.json` sets `"integration_type": "hub"`, expose an `async_step_hub` handler and register a `"hub"` mapping in `ConfigFlow.async_get_supported_subentry_types()` that points at the service/hub subentry flow handler. This keeps Home Assistant's "Add hub" button functional without custom UI patches.
  * Iterating `entry.subentries.items()` yields `(subentry_id, subentry)` tuples. Always select the child object's global `entry_id` when calling lifecycle helpers or emitting debug logs so identifiers stay aligned across unload fallbacks and cleanup paths.
    * Lifecycle helper checklist:
      - [`async_unload` parent workflow](docs/CONFIG_SUBENTRIES_HANDBOOK.md#f-parent-unload)
      - [`async_remove_subentry` cascade rules](docs/CONFIG_SUBENTRIES_HANDBOOK.md#h-cascading-removal)
  * Home Assistant's subentry helpers (`async_add_subentry`, `async_update_subentry`, `async_remove_subentry`) may return a `ConfigSubentry`, a sentinel boolean/`None`, or an awaitable yielding one of those values. Always normalize results through the shared `_await_subentry_result` helper (or an equivalent `inspect.isawaitable` guard) so asynchronous responses update the manager cache correctly.
  * **Race-condition mitigation:** When programmatically creating subentries, immediately yield control back to the event loop (for example, `await asyncio.sleep(0)`) before invoking `hass.config_entries.async_setup(...)`. This prevents `homeassistant.config_entries.UnknownEntry` errors while the config-entry registry finalizes the new child entry. Leave a short inline comment describing the yield so future contributors retain the guard.

#### Deprecations & migrations

* [`docs/AI_DEPRECATIONS_GUIDE.md`](docs/AI_DEPRECATIONS_GUIDE.md) — Core 2025.10/2025.11 migration playbook for breaking changes, API removals, and checklist-driven refactors.
* [`custom_components/googlefindmy/NovaApi`](custom_components/googlefindmy/NovaApi) — Nova helpers, request builders, and protobuf serializers. Keep protobuf imports inside `if TYPE_CHECKING:` guards when the dependency is only needed for type annotations so startup stays fast on constrained devices.
* `custom_components/googlefindmy/Auth/firebase_messaging/**` — Firebase messaging modules share the `JSONDict` and `MutableJSONMapping` aliases defined alongside the implementations. Prefer these aliases over ad-hoc `dict[str, Any]`/`Mapping[str, object]` annotations when describing payloads, responses, or task metadata. When new helpers consume nested JSON, extend the aliases or introduce additional `TypeAlias` definitions in the same module so every constructor or attribute keeps explicit container parameters for mypy strict runs. When referencing stub-only overlays (for example, `protobuf_typing`), wrap the import inside an `if TYPE_CHECKING:` guard and alias to the runtime class otherwise so production code never depends on non-existent modules.
* `custom_components/googlefindmy/NovaApi/**` — Prefer type annotations that reference protobuf-generated classes (for example, `DeviceUpdate_pb2.ExecuteActionRequest`) but import those modules exclusively under `if TYPE_CHECKING:` guards. At runtime, alias to the concrete protobuf package shipped with Home Assistant. This keeps Nova helpers importable during Home Assistant startup without incurring the protobuf import cost unless type checking is running.

### Runtime vs. type-checking import quick reference

Use the following patterns whenever a module only exists as a `.pyi` stub or when the runtime dependency must stay optional:

1. **Guard the stub import** so production code never tries to import the missing module:

   ```python
   from typing import TYPE_CHECKING

   if TYPE_CHECKING:
       from custom_components.googlefindmy.protobuf_typing import MessageProto
   else:
       from google.protobuf.message import Message as MessageProto
   ```

2. **Provide a runtime alias** to the concrete implementation (or a graceful fallback) so the rest of the module can use the shared name (`MessageProto` above) without knowing whether it came from the stub or the runtime module.

3. **Avoid work in the `TYPE_CHECKING` block.** Limit the guarded section to imports and type-only definitions; execute all runtime logic outside the guard so mypy and the interpreter share the same behavior.
4. **Catch only `ImportError` when providing runtime fallbacks.** Optional integration helpers should surface unexpected runtime exceptions immediately instead of masking them behind broad `except Exception:` guards. This keeps startup failures debuggable and prevents silent misconfiguration when a dependency is present but broken for other reasons.
* `google/protobuf/**` — Local type stub overlays that model the minimal subset of `google.protobuf` used by the integration. These stubs unblock strict mypy runs without depending on the upstream package’s incomplete type hints. Update them when generated protobuf code begins to reference additional APIs or when upstream ships first-party stubs that supersede these local helpers.
* **Documentation alignment:** When describing config entries or subentries in `docs/`, keep `docs/CONFIG_SUBENTRIES_HANDBOOK.md` as the canonical source of truth. Sync other documentation with the handbook and add inline citations back to the relevant sections whenever you reference the `homeassistant.config_entries` interface snapshot or platform-forwarding rules.

>
> | Domain | Primary use cases |
> | --- | --- |
> | `https://developers.home-assistant.io` | Integration architecture, helper APIs, config flow patterns, data update coordinator guidance, and other developer-facing contracts. |
> | `https://www.home-assistant.io` | User-facing behavior references, feature overviews, release notes, and configuration examples that affect documentation or UX communication. |
> | `https://alerts.home-assistant.io` | Service advisories, breaking changes, security or outage bulletins that may require mitigation steps or temporary workarounds. |

## Environment verification

### Quick-reference (per-task checklist)

* Connectivity probe: `python -m pip install --dry-run --no-deps pip` (record the chunk for citations).
* Dependency bootstrap: `python -m pip install --upgrade homeassistant pytest-homeassistant-custom-component` or run `make test-stubs` before linting or tests.
* Required checks (in order): `python -m ruff check --fix`, `python -m mypy --strict`, `python -m pytest --cov -q`.
* Registry helpers: when adjusting device/entity relink logic, reuse the shared helper contract documented near `_async_relink_entities_for_entry` instead of adding new lookup paths so registry fallbacks stay consistent.

> **Quickstart:** Run `make test-stubs` once per fresh environment to install `homeassistant` and `pytest-homeassistant-custom-component` before `pytest -q`, `mypy --strict`, or `ruff check`; the download/build step typically finishes in about five minutes, so plan lint/test runs with that buffer in mind.

### Quickstart checks (fast path before `pytest -q`)

* **Cache cleanup:** Run `make clean` to prune `__pycache__` directories and stale bytecode before rerunning tests.
* **Connectivity probe:** Confirm HTTP/HTTPS reachability with `python -m pip install --dry-run --no-deps pip` and capture the output for citations.
* **Test stub install:** Use `make test-stubs` to install `homeassistant` and `pytest-homeassistant-custom-component` when you need a minimal bootstrap immediately before `pytest -q`. Plan for several minutes of download time in the hosted environment because the Home Assistant wheels are large.
* **Strict mypy prep:** In a fresh environment, run `make test-stubs` before `mypy --install-types --non-interactive --strict` so the Home Assistant stubs are already present and strict type checking doesn't trip over missing packages.
* **Coverage reminder:** After adjusting registry helper fallbacks, rerun `pytest --cov -q` to confirm coverage stays intact before committing.
* **Pytest coverage guard:** `pytest -q --cov` will raise the conftest bootstrap error if the Home Assistant stubs are missing. Run `make test-stubs` first so coverage runs complete without runtime import failures.
* **Import normalization:** Run `python -m ruff check --select I --fix` to auto-sort import blocks (especially in `tests/`) instead of rearranging them manually. Import coroutine annotations (for example, `Coroutine`) from `collections.abc` alongside other container ABCs to avoid duplicate-definition lint errors when `typing` re-exports overlap.

* **Cache hygiene helper.** Run `make clean` from the repository root to prune `__pycache__` directories and stray `*.pyc` files after tests or whenever caches need to be refreshed. If you need to tidy up manually (for example after executing helper scripts), remove the bytecode caches with `find . -type d -name '__pycache__' -prune -exec rm -rf {} +` before committing.

* **Generated protobuf sampling.** Use `script/rg_proto_snippet.sh` to preview hits when searching massive generated files (for example, `script/rg_proto_snippet.sh encryptedMetadata custom_components/googlefindmy/ProtoDecoders`). The helper wraps `rg --max-count` and truncates each line via `cut` so shell output stays within the limit documented in this guide.

* **Requirement:** Determine the current connectivity status before every implementation cycle.
* **Preferred check:** Use `python -m pip install --dry-run --no-deps pip` so contributors document a consistent HTTP/HTTPS probe and capture the output in their summaries.
* **Citation helper:** When recording the connectivity probe, note the exact chunk identifier or log file path that contains the dry-run output so future summaries can reference it without rescanning the terminal history.
* **Dependency bootstrap timing:** Expect `pip install --upgrade homeassistant pytest-homeassistant-custom-component` to run for roughly five minutes in the hosted environment (downloads + wheel builds). Plan shell work accordingly so lengthy installs finish before kicking off lint or test runs.
* **Bootstrap helper:** Run `make bootstrap-base-deps` from the repo root to pre-install `homeassistant` and `pytest-homeassistant-custom-component` once per environment. The task drops a sentinel at `.bootstrap/homeassistant-preinstall.stamp`; delete it (or run `make clean`) if you need to force a reinstall. For a faster, test-focused bootstrap immediately before running `pytest -q`, prefer `make test-stubs`, which installs only these two stub packages without the broader base toolchain.
* **Stub refresh after upgrades:** When dependency versions change (for example, after rebasing or updating requirements), rerun `make test-stubs` so Home Assistant stubs stay in sync before invoking `pytest -q` or `mypy --strict`.
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
* **Translation schema guardrails.** Home Assistant option/config flow translations must stick to the documented schema. Under each `options.step.<id>` or `config.step.<id>` block, only use `title`, `description`, `data`, `data_description`, `menu_options`, `error`, `abort`, and `progress`. Avoid nested `select` objects or other ad-hoc keys—translation validation will reject them and the options UI will render placeholders instead of labels.
  * `data_description` entries must be **strings**. Validators reject dictionaries at that path, so encode select choice labels in plain text (for example, list the choices in the field description) and reserve option-specific labels for the selector definition in `config_flow.py`.
  * Keep localized translations in sync with the base `strings.json`. If you flatten a `data_description` value to a string in the source strings, mirror the same shape in every `translations/<lang>.json` file; a single lingering dictionary (for example, per-locale `options`) will cause hassfest to fail.
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
* **Ruff linting parity.** Treat `ruff check` as co-equal with `pytest -q` and `mypy --strict`; run it before presenting results and resolve every reported issue in-tree.
* **Deprecation remediation.** Investigate and resolve every `DeprecationWarning` observed during implementation, local verification, or CI. Prefer code changes over warning filters; if a warning must persist, document the upstream blocker in the PR description with a follow-up issue reference.
* **Coverage targets.** Keep **config flow at 100 %**; repo total **≥ 95 %**. If temporarily lower due to necessary code removal, **open a follow-up issue** to restore coverage and reference it in the PR.
* **Behavioral safety.** No secrets/PII in logs; user-visible errors use translated `translation_key`s; entities report `unavailable` on communication failures.
* **Docs/i18n (when user-facing behavior changes).** Update `README.md` and every relevant translation; avoid hard-coded UI strings in Python. Make sure placeholders/keys referenced in Python files match `strings.json` and `translations/en.json`. Afterwards synchronize every file in `custom_components/googlefindmy/translations/*.json` (for example, via `python script/sync_translations.py`, `pre-commit run translations-sync`, or `git diff -- custom_components/googlefindmy/translations`). The helper at [`script/sync_translations.py`](script/sync_translations.py) (callable with `python script/sync_translations.py`) will overwrite the base language with `strings.json` and backfill other locales (use `--check` during CI-style verification). Follow **Rule §9.DOC** so documentation and docstrings stay intact. Document any CI/test guidance adjustments directly in the PR description so automation notes remain accurate.
  * **Config subentry doctrine.** When touching README or docs about setup flows, reference Section 0 of `docs/CONFIG_SUBENTRIES_HANDBOOK.md` so guidance reflects the parent–child enforcement model, the March 2025 `_get_entry()` rename, and the expectation that `async_setup_entry` (not `async_setup`) owns instance bootstrap.
  * **Reference-style footnotes.** When introducing new reference-style links or numbered footnotes in Markdown, append the matching `[label]: URL` definitions so every callout resolves to a valid target.
  * **Pre-commit ready translations.** Run `python script/sync_translations.py` before committing any translation updates so the local pre-commit hook (`script/sync_translations.py --check`) passes without extra iterations.
* **Deprecation check (concise).** Add 2–4 bullets with links to HA release notes/dev docs that might affect this change (§8).
* **Quality-scale evidence (lightweight).** If a Quality-Scale rule is touched, append one evidence bullet in `quality_scale.yaml` (or propose adding the file). **Do not block** if it’s missing—note this in the PR.
* **Historical context.** For regressions, reference implementations, or suspected newly introduced bugs, inspect the relevant commit history (e.g., `git log -- <file>`, `git show <commit>`, `git diff <old>..<new>`).

> **Local run (VERIFY)**
> **Offline mode:**
> – pre-commit run --all-files *(mandatory, even if pre-commit.ci could supply autofixes)*
> – ruff format --check *(mandatory; capture the outcome in the "Testing" section)*
> – ruff check . *(mandatory; you may run `ruff check --fix` beforehand, but finish with a non-fixing run and document the result)*
> – mypy --strict --explicit-package-bases custom_components/googlefindmy tests *(mandatory for Python changes; capture the outcome)*
> – pytest -q *(mandatory; investigate and resolve every `DeprecationWarning`; capture the outcome)*
> – If Home Assistant stubs are missing, run `make install-ha-stubs` before rerunning pytest (see [README — Installing Home Assistant test dependencies on demand](README.md#installing-home-assistant-test-dependencies-on-demand)).
>
> **Online mode (in addition to the offline steps):**
> – pip install -r requirements-dev.txt *(install or update missing dependencies)*
> – pre-commit install *(make sure hooks are installed)*
> – pre-commit run --all-files *(run again if new hooks were installed)*
> – ruff format --check *(reconfirm that formatting is correct)*
> – ruff check . *(reconfirm that linting passes without autofixes; run `ruff check --fix` beforehand if needed and rerun without `--fix` before recording results)*
> – pytest -q *(reconfirm that the tests pass)*
> – mypy --strict --explicit-package-bases custom_components/googlefindmy tests *(full run across the entire codebase and tests)*
> – ruff check --fix --exit-non-zero-on-fix && ruff check *(optional helper when additional linting fixes are necessary; still conclude with the standalone `ruff check .` command above)*
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
* **Manifest compatibility (Jan 2025):** The shared CI still ships a `script.hassfest` build that rejects the `homeassistant` manifest key. Until upstream relaxes the schema for custom integrations, do **not** add `"homeassistant": "<version>"` to `custom_components/googlefindmy/manifest.json` or `hacs.json`. Track the minimum supported Home Assistant core release in documentation/tests instead.

## Maintenance mode

* **Activation:** Maintenance mode is triggered explicitly through a maintainer request, an issue label, or a direct user request. Confirm in your response or PR description that maintenance mode is active.
* **Required checks:** Run the full suite—`mypy --strict` across the entire repository (with no exclusions), complete test suites (`pytest` plus any integration/end-to-end tests), `pytest --cov` (or another `pytest-cov` invocation) to capture coverage whenever maintenance mode is active so my maintenance passes produce actionable data, `ruff check`, `ruff format --check`, `pre-commit run --all-files`, `pip-compile`, `pip-audit`, configuration/manifest synchronization (see "Home Assistant version & dependencies"), and documentation alignment. Launch CLI utilities with `python -m` (for example, `python -m pre_commit run --all-files`, `python -m piptools compile`, `python -m pip_audit`) to avoid PATH-related "command not found" failures when the virtualenv bin directory is not exported by default.
* **Coverage-driven follow-up:** Treat the maintenance-run coverage data as the prioritization source for test improvements, and schedule exactly one follow-up task per cycle that targets the highest-risk coverage gap highlighted by the report so the next increment closes the most critical hole first.
* **Error-to-test mandate:** When either I or the user encounters a defect, misbehavior, or regression during maintenance, treat it as a mandatory opportunity to extend an existing pytest or add a new one that captures the scenario before the cycle closes.
* **Connectivity caveat:** The managed environment occasionally blocks TLS handshakes for PyPI-hosted metadata, which causes `pip-audit` (and other HTTPS-dependent checks) to fail with `SSLError: CERTIFICATE_VERIFY_FAILED`. When this happens, document the failure in the PR/testing summary, capture the offending package URL, and proceed with the remaining maintenance tasks instead of repeatedly retrying the audit.
* **Configuration alignment:** Ensure configuration files (`pyproject.toml`, `.pre-commit-config.yaml`, `manifest.json`, requirement files) reflect the same dependency versions and document the synchronization.
* **Completion notice:** When finished, report whether another maintenance run is required (for example, due to pending upstream fixes) or maintenance mode can be closed. Capture any remaining TODOs or follow-up tasks.

### Config subentry maintenance helper

* `custom_components/googlefindmy/__init__.py::ConfigEntrySubEntryManager._deduplicate_subentries()` removes redundant config subentries while preserving a single canonical group/member pair. Call it when migrations or recovery paths encounter Home Assistant's `AbortFlow("already_configured")` errors to converge on a stable state before retrying updates.
* The helper is **idempotent** and refreshes the manager's internal `_managed` mapping after cleanup. Avoid creating new subentries inside the helper; it only removes duplicates reported by Home Assistant.

---

## 2) Roles (right-sized)

### 2.1 Contributor (implementation) — **accountable for features/fixes/refactors**

* Deliver code **with matching tests** (new/updated) for the changed behavior.
* **Auto-correct trivial test failures** flagged by CI/lint/static checks (§3.1).
* Use `DataUpdateCoordinator`; raise `UpdateFailed` (transient) and `ConfigEntryAuthFailed` (auth).
* Keep runtime objects on `entry.runtime_data` (typed); avoid module-global singletons.
* Inject the session via `async_get_clientsession(hass)`; never create raw `ClientSession`.
* Entities: stable `unique_id`, `_attr_has_entity_name = True`, correct `device_info` (identifiers/model), proper device classes & categories; noisy defaults disabled.
<a id="docstrings--typing"></a>
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

> **Reviewer reminder:** When service payload handling changes (for example, normalizing `entry_id` inputs from strings vs. lists), explicitly confirm or request regression coverage for each supported payload shape before approving the PR.

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

### 3.6 Entity-registry migration checklist (reload-centric fixes)

For any work that migrates entity-registry records during reload/startup flows, validate the following items before shipping:

1. **Device linkage** — assert that migrated entities reference the expected `device_id` and that identifier lookups exercise all candidate formats.
2. **Service exclusion** — include coverage showing service devices remain excluded (e.g., entries with `entry_type == SERVICE` are not used as targets).
3. **via_device integrity** — when helpers touch parent linkage, assert the fake registry preserves or updates `via_device` / `via_device_id` appropriately.
4. **Multi-entry safety** — demonstrate that entities belonging to other config entries stay untouched by the migration helper.
5. **Idempotence** — run the helper twice or on already-correct data to prove no extra registry updates occur.

### 3.7 Device registry healing checklist

1. **Trigger conditions.** Only heal when a registry lookup returns an existing `DeviceEntry` with an incorrect or missing `config_subentry_id`, name, or support flag. Read the latest object via `dev_reg.async_get_device` (or list iteration) immediately before scheduling the update so debug logs can reference the authoritative identifiers.
2. **Apply updates via `async_update_device`.** Do not rely on `async_get_or_create` to mutate existing devices. Instead, call `dev_reg.async_update_device(device.id, config_subentry_id=..., **extra_fields)` and always capture the returned entry (the helper returns a *new* object). Treat the previous `device` reference as stale and overwrite it with the returned instance before continuing.
3. **Bookkeeping and metrics.** Count each successful heal exactly once—after `async_update_device` returns—and log the `(entry_id, device_id, identifiers, config_subentry_id)` tuple at debug level. Surface aggregate counters in telemetry or diagnostics helpers when applicable so regression tests can assert how many devices were corrected.
4. **`_heal_tracker_device_subentry` contract.** The helper returns a tuple `(device_entry, healed)` where `device_entry` is the freshest registry object (even if no heal occurred) and `healed` flags whether an update was applied. Always propagate the refreshed `device_entry` for downstream work (name updates, via-device clearing, identifier merges) and base counters/logs on the boolean flag so heals do not masquerade as extra creations.

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

### Deprecation shim checklist (Home Assistant)

- [ ] `custom_components/googlefindmy/__init__.py` — `CONFIG_SCHEMA` retains
  the `config_entry_only_config_schema`/`no_yaml_config_schema` fallback chain,
  and `_apply_update_entry_fallback()` mirrors legacy `async_update_entry`
  keyword handling. Drop these once the minimum supported Core guarantees the
  modern helpers/signatures.
- [ ] `custom_components/googlefindmy/system_health.py` —
  `async_get_system_health_info()` still supports the pre-2024 signature of
  `ConfigEntries.async_entries()` that omitted the domain filter. Remove this
  guard when compatibility with that Core series is no longer required.

---

## 9) Docs & i18n (minimal but strict)

* No hard-coded UI strings in Python.
* Translation hygiene (MUST) — keep `strings.json` and `translations/*.json` in sync:
  * [ ] Update **every** locale file in `translations/*.json` **and** `strings.json` whenever translation keys or formatted placeholders change.
  * [ ] Cross-check locale files for missing keys, placeholder mismatches, or obsolete entries before submitting the PR.
  * [ ] Run the documented helper command (if available) to compare locales; otherwise perform a manual diff review to confirm parity and note the method in the PR.
  * ⚠️ Hassfest rejects `config.menu` objects. When a flow shows a menu, translate the options under `config.step.<step_id>.menu_options` (or the matching `options.*`/`config_subentries.*` section) instead of adding a top-level `menu` block.
* Translate service and exception texts (`translation_key`).
* Update README only when user-visible behavior/options change.
<a id="rule-9doc-canonical"></a>
* **Rule §9.DOC (canonical):** Keep documentation and docstrings accurate for existing features by correcting errors and augmenting missing details without shortening or deleting content. When functionality is intentionally removed or deprecated, remove or reduce the corresponding documentation to reflect that change while preserving historical clarity.
  * **Decision algorithm:** IF functionality is intentionally removed/deprecated → update or remove the related documentation to match the removal (include deprecation context as needed); ELSE → correct/augment the documentation without shortening it.
* Documentation-only updates must still record any prerequisite bootstraps performed during the edit (for example, `python -m pip install --upgrade homeassistant`) so reviewers can trace required environment steps alongside the content changes.

---

## 10) Local commands (VERIFY)

* `pre-commit run --all-files`
* `ruff check`
* `pytest -q`
* `mypy --strict --install-types --non-interactive custom_components/googlefindmy tests`

  > **Non-interactive flag REQUIRED.** Running mypy with
  > `--install-types --non-interactive` pre-approves the stub installations strict
  > mode requires, eliminating interactive prompts during local or CI runs. When
  > invoking mypy against a subset of files, append
  > `--install-types --non-interactive` as well (for example,
  > `mypy path/to/file.py --install-types --non-interactive`). CI logs are audited
  > for this flag to prevent hung jobs waiting for stub-install confirmation.

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
   *Fresh environment reminder:* Run
   `mypy --install-types --non-interactive --strict` once before the checks below
   to auto-install required stub packages without interactive prompts.
3. **Run the strict check.** Execute `mypy -q <changed-files>` when a
   configuration file is present; otherwise run `mypy -q --strict <changed-files>`.
   Install/upgrade mypy locally (`python -m pip install --upgrade mypy`) if it is
   missing.
   *Practical note:* invoking `mypy --strict` at the repository root can trip over
   namespace-package collisions between the integration and the `tests/`
   package. Prefer the package-targeted invocation
   `mypy --strict --install-types --non-interactive custom_components/googlefindmy tests`
   so the strict run completes reliably in CI and local shells.
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

## 11) Clean & Secure Coding Standard (Python 3.13 + Home Assistant 2025.10)

### 11.1 Language & style (self-documenting)

#### Logging formatting (ruff G004)

* **Default:** Use lazy logging with `%`-style placeholders so expensive formatting is deferred until the log level is enabled.
* **Permitted `# noqa: G004` usage:** Only on `_LOGGER.debug(...)` calls where the f-string formats existing variables without invoking additional functions.
* **Prohibited:** Never suppress `G004` for `_LOGGER.info()`, `_LOGGER.warning()`, `_LOGGER.error()`, or `_LOGGER.critical()`; these levels must always use lazy formatting.

* **PEP 8/PEP 257 mandatory.** Consistent formatting, clear docstrings; meaningful names.
* **Historic phase markers stay English.** When preserving multi-phase progress notes (for example, `Phase 1`, `Phase 2`, legacy cleanup steps), keep both the marker and its description in English so maintenance cycles do not reintroduce untranslated fragments.
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
* **Runtime payload invariants**: crypto helpers (`_ensure_bytes`, `WrappedLocation`) must enforce decrypted payloads remain `bytes`; coerce `bytearray` and drop non-byte responses before instantiating wrappers so downstream protobuf parsing stays type-safe.

### 11.6 Performance without feature loss

* Avoid busy-waiting; use backoff/jitter; coalesce duplicate work.
* Prefer **coordinator-based** data fetch (one fetch per resource/account per tick).
* Stream large I/O; avoid unnecessary (de)serialization; minimize filesystem churn.

### 11.7 Home Assistant specifics (must-haves)

* Network: **inject** the web session (`async_get_clientsession(hass)`); do not create ad-hoc sessions.
* Instance URL: use `homeassistant.helpers.network.get_url(hass, …)`.
* Data: centralize periodic fetch in a **DataUpdateCoordinator**; push > poll when available.
* Config flow: **test before configure**; localized errors; duplicate-account abort; reauth & reconfigure paths.
* Config subentries: `ConfigFlow.async_get_supported_subentry_types` must keep the `ServiceSubentryFlowHandler` and `TrackerSubentryFlowHandler` buckets aligned with the service/tracker feature constant sets so UI actions remain accurate.
* Config subentries (stub compatibility): `custom_components/googlefindmy/config_flow.py` ships a fallback `ConfigSubentryFlow` stub whose `async_update_and_abort(*, data, ...)` signature intentionally omits the upstream `(entry, subentry, *, data=...)` parameters; revisit when the core API evolves to avoid diverging behavior.
* Coordinator metadata probes: coordinator helpers that call `_refresh_subentry_index(...)` during early setup must pass `skip_manager_update=True` so Home Assistant stubs stay untouched while feature visibility is indexed. This avoids mutating the stubbed `ConfigEntrySubEntryManager` before the real manager is bound and keeps the visibility tests deterministic.
* Repairs/Diagnostics: provide both; redact aggressively.
* Storage: use `helpers.storage.Store` for tokens/state; throttle writes (batch/merge).
* System health: prefer the `SystemHealthRegistration` helper (`homeassistant.components.system_health.SystemHealthRegistration`) when available and keep the legacy component import only as a guarded fallback.

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
* [ ] Local verify commands passed (`pre-commit run --all-files`, `ruff check`, `pytest -q`, `mypy --strict --install-types --non-interactive custom_components/googlefindmy tests`); hassfest continues to run in CI.

---

## REFERENCES

### 1) Python 3.13 — Language, Style, Typing, Safety

* PEP 8 – Style Guide: [https://peps.python.org/pep-0008/](https://peps.python.org/pep-0008/)
* PEP 257 – Docstring Conventions: [https://peps.python.org/pep-0257/](https://peps.python.org/pep-0257/)
* PEP 695 – Type Parameter Syntax (Generics): [https://peps.python.org/pep-0695/](https://peps.python.org/pep-0695/)
* What’s New in Python 3.13: [https://docs.python.org/3/whatsnew/3.13.html](https://docs.python.org/3/whatsnew/3.13.html)
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
  * **Reminder for contributors:** Home Assistant's issue registry helper names use the `async_*` prefix even though they are synchronous callbacks. Call `issue_registry.async_get(hass)` and the returned registry's `async_*` methods without `await` in both production code and tests.
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

#### Home Assistant regression helper

* Use `make test-ha` to provision the `.venv` environment, install `requirements-dev.txt` (including `homeassistant` and `pytest-homeassistant-custom-component`), and execute the regression suite in one step.
* Append custom pytest flags with `make test-ha PYTEST_ARGS="…"` when you need markers, selection filters, or verbosity tweaks without editing the recipe.
* The `README.md` section ["Running Home Assistant integration tests locally"](#running-home-assistant-integration-tests-locally) mirrors this workflow so external contributors can follow the same command.

#### Config flow registration fallbacks

* Prefer the Home Assistant config flow metaclass to register flows. Manual assignments to `HANDLERS` are legacy fallbacks that should only be reintroduced when Home Assistant removes the metaclass hook or when a regression in upstream releases prevents automatic registration.
* If a fallback becomes necessary, document the affected core version, link to the upstream issue or breaking change notice, and mark the block with a TODO referencing the removal criteria so the workaround is pruned once the regression is resolved.
* When strict typing requires forwarding metaclass keywords (for example, `domain=DOMAIN`), reuse the `_DomainAwareConfigFlow` helper in `custom_components/googlefindmy/config_flow.py`—or introduce an analogous keyword-aware shim in the same module—so `mypy --strict` accepts the keyword without reintroducing manual `HANDLERS` fallbacks.

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

## X. PROTOCOL: Runtime API Validation (Single Source of Truth - SSoT)

**Core Principle:** AI training data is inherently outdated. Test stubs (e.g., in `conftest.py` or `tests/helpers/`) may be incorrect or obsolete. The only immutable "Single Source of Truth" (SSoT) for API contracts is the **runtime behavior of the packages installed in the `.venv`** (e.g., `homeassistant`, `pytest-homeassistant-custom-component`).

**Mandate:** Runtime errors encountered during validation (`TypeError`, `AttributeError`, `ImportError`, `NotImplementedError`) that contradict the AI's internal model *must* be treated as a failure of the AI's model, not a failure of the code or environment.

**MANDATORY SSoT VALIDATION WORKFLOW:**

If a patch fails validation with an error that implies a mismatch in API contracts (e.g., `TypeError: unexpected keyword argument 'config_subentry_id'`, `TypeError: 'Platform' object is not iterable`):

1.  **FORBIDDEN ACTION:** Do not retry the same patch. Do not guess. Do not assume the implementation code is wrong if the error originates from a test.

2.  **STEP 1: FORMULATE HYPOTHESIS:** State a precise hypothesis about the *true* API contract versus the *assumed* (or mocked) contract.
    * *Example Hypothesis:* "The error `TypeError: 'type' object is not iterable` suggests the `Platform` object in the installed `homeassistant.const` package is an `Enum` (which is iterable), but the test stub in `conftest.py` incorrectly defines it as a simple `class` (which is not)."

3.  **STEP 2: EXECUTE SSoT CHECK (MANDATORY):** Actively validate this hypothesis by executing code against the virtual environment's Python executable. This is the **only** way to confirm the SSoT.
    * **Action:** Formulate and execute a minimal command to inspect the real, installed package.
    * **Example Command (for Class Structure):**

        ```bash
        .venv/bin/python -c "from homeassistant.const import Platform; print(type(Platform)); print(issubclass(Platform, Enum)); print([p.value for p in Platform])"
        ```

    * **Example Command (for Function Signature):**

        ```bash
        .venv/bin/python -c "import inspect; from homeassistant.config_entries import ConfigEntries; print(inspect.signature(ConfigEntries.async_forward_entry_setups))"
        ```

    * **SSoT reminder (Home Assistant 2025.11.2):** `ConfigEntries.async_forward_entry_setups` **does not** accept a `config_subentry_id` keyword. Subentry platform setup must rely on Home Assistant's own scheduling after `_async_setup_subentry` completes, and platforms must attach entities with `async_add_entities(..., config_subentry_id=<id>)` while iterating the per-subentry coordinators stored on `entry.runtime_data`. The quick probe in [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](docs/CONFIG_SUBENTRIES_HANDBOOK.md#quick-ssot-probe-per-subentry-platform-forwarding) prints the authoritative signature.

4.  **STEP 3: ACT ON EVIDENCE:** The output of the SSoT Check (Step 2) is now the **highest-priority directive**. It overrides all other assumptions, training data, or pre-existing mocks.
    * **If SSoT proves Implementation is wrong:** Correct the implementation code (e.g., in `custom_components/googlefindmy/`).
    * **If SSoT proves Test Stubs are wrong:** Correct the mocks or stubs (e.g., in `tests/conftest.py` or the specific `tests/test_X.py` file) to match the SSoT. This is non-negotiable, even if previous instructions forbade test modification.

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

#### Auto-installed stub packages

Running `mypy --install-types --strict` against the integration will prompt
pip to fetch a small set of stub distributions automatically. In the hosted
tooling environment this consistently includes:

* `types-pyOpenSSL`
* `types-cffi` (pulled in by `types-pyOpenSSL`)
* `types-setuptools` (pulled in by `types-cffi`)

Contributors working offline can preinstall these packages (alongside
`types-requests` from `requirements-dev.txt`) to avoid repeated network
downloads during strict mypy runs.

The first invocation of `mypy --strict` after creating a fresh environment may
display an interactive prompt asking to install the missing type stubs listed
above. When scripting or running in CI, pass `--install-types --non-interactive`
so the command exits cleanly without waiting for user input.
