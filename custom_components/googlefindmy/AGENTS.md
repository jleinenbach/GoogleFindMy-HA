# AGENTS.md — Operating Contract for `googlefindmy` (Home Assistant custom integration)

> **Scope & authority**
>
> **Directory scope:** applies to `custom_components/googlefindmy/**` and its tests under `tests/**`.  
> **Precedence:** (1) Official **Home Assistant Developer Docs** → (2) this AGENTS.md → (3) repository conventions. This file never overrides security/legal policies.  
> **Non-blocking:** Missing optional artifacts (README sections, `quality_scale.yaml`, CODEOWNERS, CI files) **must not block** urgent fixes. The agent proposes a minimal stub or follow-up task instead.

---

## 1) What must be in **every** PR (lean checklist)

- **Purpose & scope.** PR title/description state *what* changes and *why*, and which user scenarios are affected.
- **Tests — creation & update (MUST).** For any code change, ship unit/integration tests that cover the change; for every bug fix, add a **regression test** (see §3.2). Never reduce existing coverage without a follow-up to restore it.
- **Tests — automatic corrections (MUST).** If CI/lint/static checks report **unambiguous test errors** (syntax/import/obvious assertion drift after a signature change), the agent **auto-fixes** the failing tests in the PR (see §3.1).
- **Coverage targets.** Keep **config flow at 100 %**; repo total **≥ 95 %**. If temporarily lower due to necessary code removal, **open a follow-up issue** to restore coverage and reference it in the PR.
- **Behavioral safety.** No secrets/PII in logs; user-visible errors use translated `translation_key`s; entities report `unavailable` on communication failures.
- **Docs/i18n (only when user-facing behavior changes).** Update `README.md` and `translations/*`; no hard-coded UI strings in Python.
- **Deprecation check (concise).** Add 2–4 bullets with links to HA release notes/dev docs that might affect this change (see §8).
- **Quality-scale evidence (lightweight).** If a Quality-Scale rule is touched, append one evidence bullet in `quality_scale.yaml` (or propose adding the file). **Do not block** if it’s missing—note this in the PR.

> **Local run (VERIFY)**
>
> **bash:**
> pre-commit run --all-files     # style, lint, markdown, typing checks (repo-defined)
> python3 -m script.hassfest     # manifest/translations/brands/structure validation
> pytest -q                      # must pass; config_flow 100 %, repo ≥ 95 %

---

## 2) Roles (right-sized)

### 2.1 Contributor (implementation) — **accountable for features/fixes/refactors**
- Deliver code **with matching tests** (new/updated) for the changed behavior.
- **Auto-correct trivial test failures** flagged by CI/lint/static checks (see §3.1).
- Use `DataUpdateCoordinator`; raise `UpdateFailed` (transient) and `ConfigEntryAuthFailed` (auth).
- Keep runtime objects on `entry.runtime_data` (typed); avoid module-global singletons.
- Inject the session via `async_get_clientsession(hass)`; never create raw `ClientSession`.
- Entities: stable `unique_id`, `_attr_has_entity_name = True`, correct `device_info` (identifiers/model), proper device classes & categories; noisy defaults disabled.
- **Docstrings & typing.** English docstrings for public classes/functions; full type hints; track the **current HA core baseline** for Python/typing strictness.

### 2.2 Reviewer (maintainer/agent) — **accountable for correctness**
- Verify the PR checklist, **test adequacy** (depth & quality), and token-cache safety (see §4).
- **Test adequacy includes:** presence **and** coverage of **core logic, edge cases, and error paths** relevant to the change (not just line coverage).
- Provide **actionable feedback** on missing/inadequate tests. The contributor fixes failing tests; reviewers may add minimal tests directly if quicker/obvious.
- May request **follow-ups** when repo-wide targets (coverage/typing/docs) are momentarily below goals, without blocking urgent bugfixes.

*(If you use “Code / QA / Docs” sub-roles internally, map them onto this Contributor/Reviewer model; do not require three separate formal sign-offs.)*

---

## 3) Test policy — explicit AI actions

### 3.1 Automatic **Test Corrections** (MUST)
The agent **must automatically fix** tests within the PR when failures are **unambiguous**:
- **Examples:** syntax errors, import/module path mistakes, straightforward assertion updates after renamed parameters/return types, typographical mistakes in test names or markers.
- **Boundaries:** Do **not** auto-change tests when behavior/requirements are unclear (e.g., semantic disagreements, flaky timing without a clear fix). In those cases, request targeted reviewer guidance.

### 3.2 Automatic **Regression Tests** for fixes (MUST)
When a change is a bug fix (**commit type** `fix:` or **branch** `fix/...`) and **no existing test** covers the failure mode:
- Create a **minimal regression test** that **fails without** the fix and **passes with** the fix, isolating the precise scenario (no extra scope).
- Prefer the closest relevant file/naming: `tests/test_<area>_...py`. For config-flow fixes, put it in `test_config_flow.py`; for token/cache issues, `test_token_cache.py`.
- If multiple permutations exist, cover the **single most representative** one; add more only if they catch distinct behaviors.

### 3.3 Opportunistic **Test Optimization** (SHOULD SUGGEST)
- Suggest improvements **only as a by-product** of other work (no dedicated optimization sweep): e.g., use `pytest.mark.parametrize`, simplify redundant mocks/fixtures, remove unreachable branches, replace sleeps with time freezing.
- Offer suggestions as **optional** PR comments/notes; avoid churn unless the gain is **significant** (e.g., **> 20 % speedup**, major readability/flake reduction).

### 3.4 Definition of Done for tests
- **Deterministic:** no sleeps/time-races; use time freezing/monkeypatching.
- **Isolated:** no live network; inject HA web sessions; mock external I/O at the boundary.
- **Readable:** clear arrange/act/assert, meaningful names, minimal fixture magic.
- **Value-dense:** each test protects a distinct behavior; avoid near-duplicates.
- **Fast:** prefer coordinator plumbing and targeted mocks over slow end-to-end paths.

### 3.5 Temporary coverage dips
If necessary changes reduce coverage below target, **open a follow-up issue** to restore it and reference it in the PR; do not allow repeated dips.

---

## 4) **Token cache handling** (hard requirement; regression-prevention)

- **Single source of truth:** Keep an **entry-scoped** `TokenCache` (HA Store if applicable). No extra globals.
- **Pass explicitly:** Thread the `TokenCache` through every call chain (`__init__` → API/clients → coordinator → entities). **No implicit lookups** or module-level fallbacks.
- **Refresh strategy:**  
  - Detect expiry proactively; refresh **synchronously on the calling path** or fail fast with a translated error.  
  - No background refreshes that can race with requests.  
  - On refresh failure, raise `ConfigEntryAuthFailed` to trigger reauth (avoid infinite loops).
- **Auditability:** All cache writes go through one adapter with structured debug logs (never secrets).
- **Tests:** Include regressions for stale tokens, cross-account bleed, and refresh races.

---

## 5) Expected **test types & coverage focus**

Prioritize a small but protective suite:

1. **Config flow** — user flow (success/invalid), duplicate abort (`async_set_unique_id` + `_abort_if_unique_id_configured`), connectivity pre-check, **reauth** (success/failure → reload on success), **reconfigure** step.  
2. **Lifecycle** — `async_setup_entry`, `async_unload_entry`, **reload** (no zombie listeners; entities reattach cleanly).  
3. **Coordinator & availability** — happy path; transient errors raise `UpdateFailed`; entities flip to `unavailable`; single “down/back” log.  
4. **Diagnostics** — `diagnostics.py` returns data with strict **redaction** (no tokens/emails/locations/IDs).  
5. **Services** — success/error paths with localized messages; throttling/rate-limits where applicable.  
6. **Discovery & dynamic devices** (if supported) — discovery announcement, IP update from discovery info, add/remove devices post-setup.  
7. **Token cache** — expiry detection, refresh, failure propagation, no hidden fallbacks (see §4).

---

## 6) Minimal scaffolds & fixtures (pragmatic defaults)

- Use core HA fixtures (`hass`, `enable_custom_integrations`, temp stores).  
- Avoid real network: inject sessions; mock HTTP/gRPC clients at the boundary.  
- Keep **one** lightweight helper to build test config entries/entities.  
- Prefer `pytest.mark.parametrize` over copy-pasted tests.  
- Stable names: `test_config_flow.py`, `test_diagnostics.py`, `test_entities.py`, `test_services.py`, `test_token_cache.py` (as needed).

---

## 7) Quality scale (practical, non-blocking)

- Maintain a **light** `custom_components/googlefindmy/quality_scale.yaml` to record **rule IDs** touched and a short **evidence** pointer (file+line / test name / PR link).  
- If the file is missing, **do not block**—propose adding a minimal stub or open a follow-up task.  
- Reviewers decide the final level when relevant; `hassfest` validates presence/schema in CI.

**Platinum hot-spots to double-check**
- Async dependency; injected web session; strict typing.  
- Config flow (user/duplicate/reauth/reconfigure); unload/reload robustness.  
- Diagnostics redaction; discovery/network info updates (if used).

---

## 8) Deprecation check (concise, per PR)

Add to the PR description:

- **Versions scanned:** current HA ± 2 releases.  
- **Notes found:** 2–4 bullets with links to relevant release notes/developer docs (or “none found”).  
- **Impact here:** “none”, or one-liners on code/tests/docs you adjusted.

*(When unsure, add a follow-up item; don’t block urgent fixes.)*

---

## 9) Docs & i18n (minimal but strict)

- No hard-coded UI strings in Python. Keep `strings.json` / `translations/*.json` in sync.  
- Translate service and exception texts (`translation_key`).  
- Update README only when user-visible behavior/options change; formatting/TOC/link checks live outside this contract.

---

## 10) Local commands (VERIFY)

### bash:
pre-commit run --all-files     # style, lint, markdown, typing checks (repo-defined)
python3 -m script.hassfest     # manifest/translations/brands/structure validation
pytest -q                      # must pass; config_flow 100 %, repo ≥ 95 %

---

## 11) References (authoritative, for implementers)

* Home Assistant Developers — **Integration quality scale (Rules & Checklist)**.
* Home Assistant Developers — **Config entries & flows** (config flow handler, reauth, reconfigure).
* Home Assistant Developers — **Diagnostics**, **Raising exceptions**, **Testing your code**.

*(Consult these first. Keep a bookmarks doc with the exact URLs.)*
