<!--
Maintainer pull request body. GitHub does not offer this file as a template.
Copy it, fill the copy and pass it to `gh pr create --body-file <copy>`. Do not
commit the filled copy. Fields marked "after CI" are filled later with
`gh pr edit --body-file <copy>`.
-->

<!--
Thank you for contributing! Fill in every section that applies. Sections under
"Only if applicable" can be deleted when they do not apply. Check only what you
have actually done.

Title: Conventional Commits, `<type>(<scope>): <subject>`, for example
`fix(coordinator): keep entities available after a timeout`. Use the imperative
mood, start in lower case, no period at the end. The scope is optional.
Scopes: deps, auth, api, coordinator, config-flow, entities, services,
diagnostics, discovery, fmdn, spot, nova, translations, ci, release.
The title check runs on pull requests; it reports but does not block.
-->

## Breaking change
<!-- If existing users must change anything (configuration, entity IDs,
automations), say what breaks, how to fix it and why. This text may be used in
the release notes. Write "None" if nothing breaks. -->

## Proposed change
<!-- What changes and why, and which user scenarios are affected. One change per pull request. -->

## Type of change
<!-- Check exactly one. -->
- [ ] `fix`: bug fix
- [ ] `feat`: new feature
- [ ] `docs`: documentation only
- [ ] `style`: formatting only, no behaviour change
- [ ] `refactor`: neither fixes a bug nor adds a feature
- [ ] `perf`: performance improvement
- [ ] `test`: tests only
- [ ] `build`: build system or dependencies
- [ ] `ci`: CI configuration
- [ ] `chore`: other changes to neither source nor tests
- [ ] `revert`: reverts an earlier commit

## Linked issues
- Fixes #
- Related:

## Checklist
- [ ] I understand the code I am submitting and can explain how it works.
- [ ] If an AI tool generated any part of this change, I have reviewed that part for correctness and for compliance with `AGENTS.md`.
- [ ] Code changes come with tests, and `make preflight` passes locally (or at least `python script/local_verify.py`).
- [ ] For a bug fix: a regression test fails without the fix and passes with it.
- [ ] The pull request text, logs and screenshots contain no personal data: no e-mail addresses, locations or coordinates, Wi-Fi names, device or account IDs, tokens.
- [ ] User-visible text uses translation keys; `README.md` and the translations are updated if user-visible behaviour changed.
- [ ] The version in `manifest.json` is unchanged; it is set when a release is made.

<!-- Coverage: 100 % for config_flow.py and 95 % in total are targets, not gates.
The CI gate is 80 % (`fail_under` in pyproject.toml). Do not lower the total; if a
necessary removal does, link a follow-up issue. -->

## Deprecation check
<!-- 2 to 4 links to Home Assistant release notes or developer docs that may affect this change. -->

## Only if applicable

### Authentication and token cache
- [ ] One entry-scoped token cache, passed explicitly; no globals.
- [ ] A permanent authentication failure raises `ConfigEntryAuthFailed`; tests cover stale tokens and cross-account bleed.

### Config flow
- [ ] Tests cover the touched steps (user, duplicate abort, reauth, reconfigure).

### Dependencies
- [ ] One dependency change per pull request, upstream changelog linked, `poetry.lock` updated.

## Maintainer verification
- Local preflight: <stages passed / failed>; full test run: `<n> passed, <m> skipped`
- Coverage: total <x> % (gate 80 %); changed lines <covered>/<coverable>
- Independent diff review before push: <PASS | PASS-WITH-FOLLOWUP> at `<head sha>`
- Live check on real devices: <what>, n = <count> (or "not applicable: <reason>")
- Personal data check of text, logs and screenshots: <method>, <result>
- Version in `manifest.json`: unchanged
- Codex review (after CI): <state> at `<head sha>`
- CI (after CI): run approved in the `home-assistant-tests` environment; <result>

## Evidence (optional)
<details>
<summary>Logs, screenshots, measurements (with personal data removed)</summary>

</details>
