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
* Type-check the **frozen** entry attributes — `ConfigEntry.subentries`, `.data`, `.options` and a `ConfigSubentry.data` — against `collections.abc.Mapping`, never against `dict`. The core hands all of them out as `MappingProxyType`, so a `dict` guard is false for every real entry and true only for the mutable test doubles: it does not fail, it silently skips. `_gather_subentry_options` carried such a guard from 2025-10-27 until the fix; the effect was not an error message but a subentry selection reduced to the synthetic fallback option, so no assignment could persist while the suite stayed green (`tests/AGENTS.md` now asks entry doubles to expose the read-only shape for the same reason). The rule is deliberately scoped to those attributes and is **not** a blanket ban on `dict` guards: `data_entry_flow.FlowContext` is a `TypedDict` and therefore a real `dict` at runtime, which is why the `isinstance(context, dict)` guards in this module are correct and must stay — several of them mutate the context afterwards (`setdefault`, `pop`, item assignment), which a `Mapping` would not permit.

### Integration module imports

* When a config flow needs helpers from the integration package, import the module via `importlib.import_module(__package__ or DOMAIN)` rather than dereferencing `__package__` attributes directly. Home Assistant may start flows with a `None` package hint, and relying on `__init__` attributes can regress when the package layout changes. Centralize this pattern so helper lookups stay robust across runtime and test stubs.
* Prefer the shared lazy import helpers in `custom_components/googlefindmy/integration_modules.py` (`import_integration_package`, `import_integration_api_module`) so the API and package modules stay lazily loaded while keeping import targets centralized for future changes.

### Subentry alias handling

* Canonical service and tracker group keys now normalize legacy labels (for example, stray email-style identifiers) through the alias-aware subentry manager. When reconciling discovery or reconfigure payloads, prefer the canonical keys surfaced by the manager over any stored group labels so collisions realign to the correct service/tracker groups instead of amplifying drift.
* **The service and hub groups never carry `visible_device_ids`, and a device target is decided by a predicate, not by a bare key comparison.** Three production sites already write the invariant that way: `ServiceSubentryFlowHandler._visible_device_ids` and `HubSubentryFlowHandler._visible_device_ids` both return `()` unconditionally (unlike the `_BaseSubentryFlow` version they override, which reads the stored value), and `_async_sync_feature_subentries` builds its `service_payload` through `_build_subentry_payload` **without** the `visible_device_ids` argument while `tracker_payload` passes the stored ids. `tests/test_config_flow_subentry_sync.py` pins that result. The reading side is a fourth, separately tracked enforcement (see below), and `ConfigEntrySubEntryManager.update_visible_device_ids` a fifth; a test asserts, it does not write. None of the three was an authoritative checkpoint, which is why `_async_assign_devices_to_subentry` — the only writer **in this module** able to break the invariant — could store device ids on the service group anyway. It now refuses such a target, and the three steps that assign devices (`async_step_visibility`, `async_step_repairs_move`, and the `_FIELD_REPAIR_FALLBACK` field of `async_step_repairs_delete`) build their choices from `_device_target_choice_map` instead of `_subentry_choice_map`.
  * **Scope of that checkpoint, stated because an earlier version of this paragraph overstated it.** `ConfigEntrySubEntryManager.update_visible_device_ids` (`__init__.py`) is a second writer of `visible_device_ids`, outside this module. It is no longer outside a guard: it now refuses to write to a subentry whose `subentry_type` is in `NON_DEVICE_SUBENTRY_TYPES`, the writing-side mirror of `_accepts_device_assignment`. That check reads the **resolved subentry**, not the key, so it holds whichever key a caller passes and whatever the alias table makes of it; today the only production caller already folds such subentries away, which makes this a second barrier rather than the only one, and that is the point, because a future caller would otherwise be unguarded. The alias table it resolves through no longer answers with a foreign group either: a **core key never aliases at all** (`core_tracking` and `service` each already name a group of their own, so a `service`-typed subentry still storing `core_tracking` can no longer redirect every tracker write onto itself), and a key that **two subentries of different canonical identities claim resolves to nothing** rather than to whichever the entry yielded first. The second half only works because a subentry whose canonical identity *is* its stored key registers that claim as well: a `hub`-typed subentry (`SUBENTRY_TYPE_HUB`, a type this integration really does create) canonicalises onto its own key and registers no redirection, so counting redirections alone would have left it uncontested and let a service twin capture the key. The ambiguity is remembered rather than merely dropped, so a third subentry cannot re-establish the mapping later in the same pass. Its caller in `coordinator/subentry.py::_refresh_subentry_index` no longer depends on the key axis either, see the canonicalisation note below. Measured, not read: both iteration orders of the service/tracker pair are pinned for the core key and for a legacy key, together with the `hub` axis and the reserved service key, in `tests/test_subentry_manager_registry_resolution.py`.
  * Use `_accepts_device_assignment` rather than comparing against `SERVICE_SUBENTRY_KEY` by hand. It reads **both** axes, the stored `group_key` and `subentry_type`, because the alias rule above lets a legacy subentry keep an email-style `group_key` while being typed `service`; a key-only check would miss exactly that subentry.
  * **The key returned by `_gather_subentry_options` must be injective, and a predicate decided per option must be consumed per option.** Both halves are load-bearing and neither replaces the other. The stored `group_key` is not injective — the alias rule above is exactly what allows one legacy label to sit on a service and a tracker subentry at once — so as soon as **one** duplicate exists, `_gather_subentry_options` moves **every** option onto its `subentry_id`. The all-or-nothing shape is deliberate and the narrower rule is a trap: moving only the duplicates is not total, because a third subentry may store the very `subentry_id` a duplicate is about to take, and chaining that defeats any fixed number of passes. `subentry_id` is the key under which `entry.subentries` stores the subentry, so one pass over all options is injective by construction, and it avoids picking a winner, which would leave the outcome dependent on the `options.sort` by label. Downstream, `_subentry_choice_map` and `_device_target_choice_map` collapse the key into a `dict` and `async_step_repairs_delete` resolves the deletion target and its inherited devices through that mapping, so a duplicate key silently hides one subentry and can delete the other. `_async_assign_devices_to_subentry` additionally compares `option is target_option` rather than the key, which keeps the fan-out onto a non-assignable twin impossible even if the first half regresses.
  * **What that injectivity does *not* buy, stated so nobody reads more into it.** It is flow-local, and the runtime side is now carried by its own rule rather than by this one: `coordinator/subentry.py::_refresh_subentry_index` indexes a subentry of any `NON_DEVICE_SUBENTRY_TYPES` type under `SERVICE_SUBENTRY_KEY` whatever it stores, so the twins no longer collapse onto one slot and the service group can no longer be described by a synthesised placeholder while the real subentry sits under the tracker key. Three further points, named rather than implied. First, folding alone would have **stranded** the devices such a twin accumulated: the service branch forces its visible ids to empty while `stored_assigned_ids` would still count them as assigned, so the unassigned-device merge would leave them in no group at all and the entities would go unavailable. The fold therefore drops those ids from the *in-memory* view (the stored subentry is untouched), and the merge reclaims them into the tracker group. They were never a deliberate move onto the service group in the first place, because the flows do not offer such a subentry as a target. Second, that drop stays **bound to the fold**, which is not a detail: a subentry already storing the canonical key is a different case, because a device sitting there may be a move the user made while the service group was still an offered target, and `test_a_device_moved_to_the_service_subentry_is_left_alone` pins that it must not be reclaimed. Only the *mis-keyed* ids are residue. Third, the **stored** `group_key` of a mistyped subentry is not migrated by any of this. Where a key is ambiguous the manager writes nothing for it, so that group receives no visibility write-back; its metadata still carries the devices, so this is inert rather than invisible, which is the direction this pair fails towards.
  * **The synthesised fallback must not borrow an identity, and a move needs a real destination.** When no group accepts devices, `_device_target_choice_map` synthesises an option. Taking `TRACKER_SUBENTRY_KEY` outright borrowed a key: an entry whose only subentry is typed `service` but stores `core_tracking` was filtered out by its type, the fallback offered *its* key back, `_async_assign_devices_to_subentry` resolved that key against the **unfiltered** set, found the real non-assignable subentry and refused — and `async_step_repairs_move` mapped the empty result onto `subentry_move_success`, telling the user a device moved that did not. Measured. Three points now carry the repair, and each is needed:
    * `_unclaimed_fallback_key` picks a key no option holds. The search walks until it is free rather than trying one substitute, because a second subentry may store the very substitute the first is displaced to. Note that the two `if not options:` guards are **not** the same test: the one in `_gather_subentry_options` fires on the unfiltered list, where nothing exists that could hold the key, while this one fires on the filtered list with a possibly non-empty unfiltered set. Only the second can borrow.
    * `_async_assign_devices_to_subentry` stands down when *no* option carries the target, not just when a non-assignable one does. Its `else` branch strips the ids from every group that holds them; with a real target that is the second half of a move, without one it is a loss — and it would be reported as success, because a strip fills `changed`.
    * `async_step_repairs_move` drops the synthesised fallback from its target field, mirroring the `real_fallback_keys` line `async_step_repairs_delete` already draws, and aborts on the existing `repairs_no_subentries`. The earlier note claimed the honest repair needed a new abort string; that was wrong, the existing one carries it and `translations/` stays untouched. `async_step_visibility` keeps the fallback on purpose: there the request is to leave the ignore list, which the option write and the reload carry, and an id no subentry claims is merged into the tracker group by `coordinator/subentry.py`.
  * Narrow only the steps that assign devices. `_subentry_choice_map` fed all six steps before the split and still feeds four of them, each of which must keep seeing every group: `async_step_settings`, `async_step_credentials`, `async_step_repairs` (which only asks whether *any* subentry exists, so a shared filter could send an entry whose sole subentry is the service group into `repairs_no_subentries`) and the `removable_choices` of `async_step_repairs_delete`. For the same reason `_device_target_choice_map` keeps the `core_tracking` fallback of `_gather_subentry_options`: filtering must never hand `vol.In` an empty mapping.
  * Deletion is a separate question from assignment, but not an independent one. `async_step_repairs_delete` keeps its unfiltered `removable_choices`, so the service group stays removable; only the fallback that inherits the deleted group's devices is narrowed. The two sides are coupled through `fallback_key != target_key`, so a group is only offered for deletion while a **different** group with a real backing subentry can inherit its devices. Skipping that re-derivation left the common shape — one tracker group plus one service group — with a fallback field whose single value was always the deletion target, so every submission failed on `invalid_subentry`.
* **The reading side enforces the same rule for the canonical service key, so the two move together there.** In `coordinator/subentry.py::_refresh_subentry_index`, the branch on `group_key == SERVICE_SUBENTRY_KEY` forces `visible_ids`, `enabled_ids` and `manager_visible_ids` back to `()`. That branch still tests one key, but the key it tests is now type-authoritative: a subentry of **any** type in `NON_DEVICE_SUBENTRY_TYPES` is folded onto `SERVICE_SUBENTRY_KEY` before it, so the branch catches the email-style twin that a key-only test used to miss. The type set is imported from `const.py` rather than restated, so the side that refuses such a group as an assignment target and the side that indexes it cannot drift apart, and `hub` belongs in it for the same reason `service` does: `HubSubentryFlowHandler` sets `_group_key = SERVICE_SUBENTRY_KEY` and the service feature platforms, so a hub *is* the service group under a second entry point — **for the assignment predicate and this index**, not everywhere: the entity platforms still key off the literal type (`known_ids_for_subentry_type` matches `== "service"`), so a hub appears in neither the service nor the tracker id set there. A legacy hub carrying a tracker key, an email-style key or no key at all used to keep a device-bearing slot and receive the write-back; only the tracker-key shape *additionally* overwrote the tracker's metadata, the other two open a group of their own. All three shapes and both iteration orders are pinned in `tests/test_coordinator_subentry_visibility.py`; the order axis is load-bearing for the tracker-key shape alone.

  Two asymmetries follow from this and are named rather than left to be rediscovered. First, `ConfigEntrySubEntryManager._refresh_from_entry` canonicalises `service` and `tracker` by type but leaves `hub` on its **stored** key, so the two sides deliberately diverge: anyone reading a subentry out of `managed_subentries` **by key** must apply the type check themselves. The visibility write-back in `async_setup_entry` calls `async_update_subentry` directly and therefore does exactly that, because the guard inside `update_visible_device_ids` cannot see it. Second, `core_group_keys_present` keeps reading the stored key (see the deliberate exception below), so for a folded type it can report a core group as present while the index describes it with a synthesised placeholder; the devices are still reclaimed by the merge, so this is inert rather than lossy. Tracker subentries keep their stored key, because several tracker groups with distinct keys are a supported shape. Two service-typed subentries can end up folded onto the same key, since the repair path creates a canonically keyed one while a mis-keyed twin is still on disk; the one that already stored the canonical key wins, so the surviving description does not depend on the order `entry.subentries` yields, mirroring `ConfigEntrySubEntryManager._select_preferred_managed`. One deliberate exception sits above the fold: `core_group_keys_present`, which drives the missing-core-subentry repair, is still collected from the **stored** key, because what it answers is which core groups exist on disk. Moving that read to the canonical key would change which subentry the repair creates, and that is a decision of its own, not a side effect of this one. The same pass collects every stored id into `stored_assigned_ids` precisely so a device parked on the service group is *not* treated as unassigned and pulled back into the tracker. Anyone changing one side changes the other: relaxing the writer without the reader stores ids that are dropped on every refresh, and relaxing the reader without the writer resurrects assignments the flows no longer offer.

### Optional `ConfigEntry` attributes in tests

Local discovery and reconfigure tests instantiate lightweight `ConfigEntry` doubles that frequently omit optional attributes Home Assistant adds at runtime.

* `source` — prefer `getattr(entry, "source", None)` before accessing the field so `async_step_discovery` continues to work with the stubs in `tests/test_config_flow_discovery.py`.
* `pref_disable_new_entities` / `pref_disable_polling` — guard these through `getattr(..., False)` when feature toggles depend on them, because flow helpers in the test suite never populate the preferences block.
* `state` — normalize through `getattr(entry, "state", None)` before checking reload eligibility; the discovery update fixtures only set `entry_id`, `data`, and sometimes `unique_id`.

Add similar guards whenever a new optional attribute becomes relevant so future config flow helpers remain compatible with the suite's minimal stubs.

* **Preserve parent-platform forwarding state across retries.** When a setup, reconfigure, or auth retry path short-circuits the normal flow, retain the `_gfm_parent_platforms_forwarded` flag on `entry.runtime_data` so unload handlers can skip subentry teardowns when parent platforms were never forwarded. This prevents `ValueError: Config entry was never loaded!` noise after partial setups.
* **Reconfigure context markers.** Home Assistant populates `flow.context["entry_id"]` when a reconfigure flow starts (for example via `config_entries.async_start_reconfigure`). Treat that value as authoritative when routing the user step so the flow stays bound to the existing entry instead of tripping duplicate-account guards. Inline comments should call out this guard relaxation to preserve the single-parent-entry rule for new setups while keeping reconfigure detours safe.
* **Manual token UI path disabled.** The `_AUTH_METHOD_INDIVIDUAL` choice in `STEP_USER_DATA_SCHEMA` remains commented out because the manual token + email path is broken. Keep the commented line (with its inline note) intact until the workflow is fixed and ready to re-enable. The options flow credential refresher (`async_step_credentials`) also keeps the manual token input commented out. The reauth form no longer has a manual token field to keep: that surface was removed, see the amendment below.
  * **Removed: the container-login credential fetch (PR #1218, 2026-07).** The existing-entry forms once carried a narrow carve-out from the "secrets-only" wording above: three fields that pulled a freshly minted bundle out of the login container over a host-published port. That transport was unencrypted, so the whole path was removed together with its error keys, its translations and its tests; `tests/test_track_b_removal_guard.py` keeps its vocabulary out of the tree. The "secrets-only" rule for `async_step_reauth_confirm` and `async_step_credentials` therefore holds again without exception, and re-introducing a credential method needs a new amendment here, not a revival of the old one.
  * **Amendment: local-bundle pre-flight is an additional credential path.** `async_step_found_local_bundle` offers a `secrets.json` bundle that was found on disk before the method form is shown. It is a `secrets.json` import, not a new manual-token surface, and it exists because the file watcher is armed in `async_setup`, which never runs on a fresh install without a config entry. The offer is skipped when every bundle found belongs to an account that is already configured (compared through `normalize_email` + `unique_account_id` against existing entry unique IDs), so multi-account setups keep working; declining it returns to the regular method form.
  * **Removed: the manual token reauth branch (PR #1229, 2026-07).** `async_step_reauth_confirm` once accepted a raw OAuth token pasted into the reauth form as a second credential path, in the success case and again in the multi-entry-guard deferral. Its form field was commented out, and `_interpret_reauth_choice` has no `return` that can produce `"manual"` either, so the branch was unreachable from the form **and** from the code. It was removed rather than covered by a test, because a test feeding the method directly would have rebuilt this contract instead of checking it. `tests/test_manual_reauth_removal_guard.py` keeps it out on three axes: lexically, by sweeping `config_flow.py` for the branch vocabulary; structurally, by pinning that `_interpret_reauth_choice` can only hand back `None` or `"secrets"` as its method element; and by wiring, by pinning that the reauth step reads no interpreter other than that one, because `_interpret_credentials_choice` does hand back `"manual"` as a method element and a call site pointed at it would pass the other two axes untouched. That function still *reads* `_REAUTH_FIELD_TOKEN`, so a token which reaches it anyway is rejected with `choose_one` rather than silently ignored while the bundle half proceeds; that read is a rejection, not a path. Reviving a manual reauth surface needs a new amendment here. The two other surfaces named above are untouched by this removal and their commented lines stay, but note that they are *not* in the same state: the `STEP_USER_DATA_SCHEMA` choice only hides a menu entry, whereas `async_step_credentials` keeps a **dormant but live** manual-token branch behind its commented-out field (`config_flow.py`, `if has_token:` in the credential refresher), which still validates and persists an individually pasted token. The "secrets-only" wording further up therefore holds without exception for `async_step_reauth_confirm` after this removal, but for `async_step_credentials` it describes the form, not the code beneath it.
* **Guard fallbacks assume secrets-only inputs.** When the multi-entry guard trips inside `async_step_credentials`, rebuild the fallback payload solely from `secrets.json` inputs. Avoid reintroducing dormant manual-token branches in the guard handler while the UI path remains disabled so UnboundLocalError-style regressions do not resurface.
* **Token validation exhaustion warning.** When every token candidate fails validation, the flow emits a warning instructing the user to re-enter credentials so expired bundles are refreshed. Keep this log intact (see `_log_token_validation_failure`) so support teams can direct users back through reauthentication instead of silently accepting bad tokens.

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

* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Mirrors this guide's subentry-flow reminders and now tracks every AGENT link. Update both documents together whenever setup/unload contracts, discovery affordances, or reconfigure hooks change.

## Subentry handler registration (HA 2026.x compatibility)

### `async_get_supported_subentry_types` MUST return empty dict

**Critical:** This method MUST return `{}` to hide unwanted UI buttons in the config entry panel.

**Why empty dict is required:**
- Returning handler classes causes HA to display "+ Add hub feature group" and "+ Add service feature group" buttons
- These manual subentry buttons should NOT be visible to users
- Subentries are provisioned **programmatically** by the integration coordinator, not manually

**Correct implementation:**
```python
@classmethod
@callback
def async_get_supported_subentry_types(
    cls,
    _config_entry: ConfigEntry,
) -> dict[str, type[ConfigSubentryFlow]]:
    """Return empty dict to hide subentry UI buttons."""
    return {}  # MUST be empty to hide manual add buttons
```

**Wrong implementation (exposes unwanted UI):**
```python
# DON'T DO THIS - exposes "Add hub/service feature group" buttons!
return {
    SUBENTRY_TYPE_HUB: HubSubentryFlowHandler,
    SUBENTRY_TYPE_SERVICE: ServiceSubentryFlowHandler,
}
```

### `async_step_hub` must instantiate handlers directly

Since `async_get_supported_subentry_types` returns empty, the "Add Hub" flow entry point (`async_step_hub`) must instantiate the handler class directly:

```python
async def async_step_hub(self, user_input=None):
    # Don't use async_get_supported_subentry_types - it returns {}
    # Instantiate the handler directly instead
    handler = HubSubentryFlowHandler(config_entry)
    setattr(handler, "hass", hass)
    setattr(handler, "context", {"entry_id": config_entry.entry_id})
    result = handler.async_step_user(user_input)
    return await self._async_resolve_flow_result(result)
```

### Lazy `config_entry` resolution for handler compatibility

Subentry handlers use a `config_entry` property with lazy resolution to support both direct instantiation and potential future HA flow manager usage:

```python
@property
def config_entry(self) -> ConfigEntry:
    if self._config_entry_cache is not None:
        return self._config_entry_cache

    # Fallback: try HA's _get_entry() method
    get_entry_method = getattr(self, "_get_entry", None)
    if callable(get_entry_method):
        entry = get_entry_method()
        if entry is not None:
            self._config_entry_cache = entry
            return entry

    raise RuntimeError("Cannot resolve config_entry")
```

### Test expectations

Tests must verify:
1. `async_get_supported_subentry_types` returns empty dict `{}`
2. `async_step_hub` creates entries successfully (via direct handler instantiation)

