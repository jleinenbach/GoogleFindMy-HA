# custom_components/googlefindmy/agents/config_flow/AGENTS.md

## Scope

Runtime + config flow guidance for every module under `custom_components/googlefindmy/`. Apply these instructions whenever
flows, service schemas, or reconfigure hooks are updated.

## Config flow registration expectations

* Keep `ConfigFlow.domain` explicitly declared in `config_flow.py`. This guards against future upstream changes that might stop injecting the attribute via metaclass magic.
* Never hand-register the flow through `config_entries.HANDLERS` unless Home Assistant drops automatic registration. If a regression forces a manual fallback, document the affected core versions, link the upstream issue, and add a TODO describing when to remove the workaround.
* Tests under `tests/test_config_flow_registration.py` cover both the domain attribute and automatic handler registration. Update them whenever the runtime behavior changes so the expectations stay enforced.
* Reference the Home Assistant developer docs on [config flow registries and handlers](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/#config-flow-handler-registration) when validating upstream behavior; keep this section aligned with any future changes noted there.
* When config flows iterate existing entries, guard optional Home Assistant attributes (for example, `ConfigEntry.source`) so discovery update stubs and other test doubles without those attributes keep working during local runs.

### Integration module imports

* When a config flow needs helpers from the integration package, import the module via `importlib.import_module(__package__ or DOMAIN)` rather than dereferencing `__package__` attributes directly. Home Assistant may start flows with a `None` package hint, and relying on `__init__` attributes can regress when the package layout changes. Centralize this pattern so helper lookups stay robust across runtime and test stubs.

### Subentry alias handling

* Canonical service and tracker group keys now normalize legacy labels (for example, stray email-style identifiers) through the alias-aware subentry manager. When reconciling discovery or reconfigure payloads, prefer the canonical keys surfaced by the manager over any stored group labels so collisions realign to the correct service/tracker groups instead of amplifying drift.

### Optional `ConfigEntry` attributes in tests

Local discovery and reconfigure tests instantiate lightweight `ConfigEntry` doubles that frequently omit optional attributes Home Assistant adds at runtime.

* `source` — prefer `getattr(entry, "source", None)` before accessing the field so `async_step_discovery` continues to work with the stubs in `tests/test_config_flow_discovery.py`.
* `pref_disable_new_entities` / `pref_disable_polling` — guard these through `getattr(..., False)` when feature toggles depend on them, because flow helpers in the test suite never populate the preferences block.
* `state` — normalize through `getattr(entry, "state", None)` before checking reload eligibility; the discovery update fixtures only set `entry_id`, `data`, and sometimes `unique_id`.

Add similar guards whenever a new optional attribute becomes relevant so future config flow helpers remain compatible with the suite's minimal stubs.

* **Preserve parent-platform forwarding state across retries.** When a setup, reconfigure, or auth retry path short-circuits the normal flow, retain the `_gfm_parent_platforms_forwarded` flag on `entry.runtime_data` so unload handlers can skip subentry teardowns when parent platforms were never forwarded. This prevents `ValueError: Config entry was never loaded!` noise after partial setups.
* **Reconfigure context markers.** Home Assistant populates `flow.context["entry_id"]` when a reconfigure flow starts (for example via `config_entries.async_start_reconfigure`). Treat that value as authoritative when routing the user step so the flow stays bound to the existing entry instead of tripping duplicate-account guards. Inline comments should call out this guard relaxation to preserve the single-parent-entry rule for new setups while keeping reconfigure detours safe.
* **Manual token UI path disabled.** The `_AUTH_METHOD_INDIVIDUAL` choice in `STEP_USER_DATA_SCHEMA` remains commented out because the manual token + email path is broken. Keep the commented line (with its inline note) intact until the workflow is fixed and ready to re-enable. The reauth form’s manual token field is similarly commented out with a matching note so the broken path stays hidden there as well. The options flow credential refresher (`async_step_credentials`) also keeps the manual token input commented out; only `secrets.json` uploads should be exposed until the manual path is fixed end-to-end.

## Service validation fallbacks

* When raising `ServiceValidationError`, always include both the translation metadata (`translation_domain`, `translation_key`, and `translation_placeholders`) **and** a sanitized `message` that reuses the same placeholders. This keeps UI translations working while ensuring Home Assistant surfaces a readable fallback when translations are unavailable.

### Fallback verification checklist

1. Run `pytest tests/test_hass_data_layout.py::test_service_no_active_entry_placeholders -q` to confirm placeholder usage remains stable.
2. Add new translation-focused tests alongside updates so each fallback path has coverage.

## Config entry options persistence reminder

* Treat `ConfigEntry.options` as immutable during reconfigure flows. Build a new dictionary (for example, `existing_options = dict(entry.options or {})`) and pass it directly to `async_update_entry` instead of mutating `entry.options` in place. Home Assistant only persists option changes when it detects a new mapping, so keep the original object untouched until `async_update_entry` returns. See the [`async_step_reconfigure` options-copy pattern](../../config_flow.py#L2929-L2943) for a concrete implementation:

  ```python
  existing_options = dict(getattr(entry_for_update, "options", {}) or {})
  existing_options.update(options_payload)
  self.hass.config_entries.async_update_entry(
      entry_for_update,
      data=merged_data,
      options=existing_options,
  )
  ```

## Cross-reference checklist

* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Mirrors this guide’s subentry-flow reminders and now tracks every AGENT link. Update both documents together whenever setup/unload contracts, discovery affordances, or reconfigure hooks change.
