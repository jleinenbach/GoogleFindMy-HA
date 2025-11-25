# custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md

## Scope

Runtime contracts, platform forwarding rules, and HA lifecycle helper usage for every module under `custom_components/googlefindmy/`.

## Runtime integration patterns

* Collect runtime-contract reminders for integration touchpoints in this section so future contributors can find them without scanning unrelated guidance.
* View classes under `custom_components/googlefindmy/map_view.py` should expose constructors that accept `HomeAssistant` as the first argument. Register new views by instantiating them with the active `hass` instance (for example, `GoogleFindMyMapView(hass)`) instead of assigning `hass` after creation so the runtime contract stays consistent.
* **Service-to-coordinator propagation:** When a service handler invokes a coordinator method that returns a boolean (for example, play/stop sound helpers that suppress requests when push is unavailable), raise `ServiceValidationError` when the result is `False` so callers and UI surfaces receive an explicit failure instead of silent success logs.
* **Platform forwarding reminder:**
  * The parent `async_setup_entry` handles coordinator/bootstrap work and explicitly triggers platform setup for any new subentries by calling `_async_setup_new_subentries`. Home Assistant subentry flows do not automatically fan out to platforms.
  * Forward setups **once per platform set** via `_async_setup_new_subentries` by gathering the union of required platforms and calling `hass.config_entries.async_forward_entry_setups(parent_entry, platforms)` **without** `config_subentry_id` or other kwargs (see the interface snapshot in [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md#quick-ssot-probe-per-subentry-platform-forwarding)).
  * Immediately after forwarding, emit dispatcher signals for each subentry so platforms receive the full `ConfigSubentry` object and can attach entities per child.
  * When forwarding or unloading the **parent entry's** own platforms, continue passing a `tuple` of platform names to `hass.config_entries.async_forward_entry_setups`/`async_forward_entry_unload`. Home Assistant caches these iterables and expects immutability during parent-level fan-out.
  * Track whether parent platforms were actually forwarded on setup using `_gfm_parent_platforms_forwarded` in `RuntimeData` and skip unload calls when the flag is `False` to avoid `ValueError: Config entry was never loaded!` noise during retries.
  * Extend the same guard to subentry unload logic: only fan out unload calls (for example, tracker and service subentries) when `_gfm_parent_platforms_forwarded` is `True` so Home Assistant never attempts to tear down platforms that were never loaded.
  * Each platform must iterate the subentry coordinators stored on `entry.runtime_data` and add entities per child when signaled by the dispatcher. Deduplicate entities per subentry to avoid `_2`/`_3` suffix churn on reconnects or retries.
  * Leave a short inline comment explaining which pattern you chose whenever platform lists move between parent and subentry paths.
  * The legacy `RuntimeData.legacy_forwarded_platforms`/`legacy_forward_notice` tracking remains in the codebase for historical builds but should stay dormant on modern Home Assistant releases.
* Use the shared `_async_setup_new_subentries` helper whenever subentries are created (during parent setup **and** at runtime) so every child receives a dispatcher signal after the single forward pass.
* Once subentries exist, invoke `_async_setup_new_subentries` **without** `enforce_registration=True`. The registration delay loop is reserved for missing IDs; removing the flag prevents an infinite `ConfigEntryNotReady` retry cycle when Home Assistant is already finalizing the registry.
  * See [`docs/CONFIG_SUBENTRIES_HANDBOOK.md#automatic-retry-queue-for-unknownentry`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md#automatic-retry-queue-for-unknownentry) for the bounded retry/backoff design that `_async_setup_new_subentries` enforces when `UnknownEntry` fires.
  * The retry queue stores its handles and per-subentry attempt counts on `entry.runtime_data` (for example, `RuntimeData.subentry_retry_handles` and `RuntimeData.subentry_retry_attempts`). Keep those runtime caches typed and cleaned up during unload so strict mypy runs continue to catch handle leaks or mismatched awaitables as the queue gains new states. After adjusting imports near these helpers, run `python -m ruff check --select I --fix` to keep the lint-driven sorting from masking future docstring or typing diffs.
* Home Assistant's subentry helpers (`async_add_subentry`, `async_update_subentry`, `async_remove_subentry`) may return a `ConfigSubentry`, a sentinel boolean/`None`, or an awaitable yielding one of those values. Always normalize results through the shared `_await_subentry_result` helper (or an equivalent `inspect.isawaitable` guard) so asynchronous responses update the manager cache correctly.
* Tracker device healing should clear any stray `config_subentry_id` links for the current entry before applying the expected tracker link so only the parent/child pair remains associated with a hub. Log the removed subentry IDs to aid debugging of multi-hub migrations and keep tests aligned with the coordinator helpers that enforce this cleanup. Add a short reminder near new healing helpers when they mutate registry links so future contributors retain the cleanup-and-log contract, and document the cleanup in related changes so future maintenance keeps the coordinator behavior in sync with the guidance here.
* **Hub-level name collision handling:**
  * Before creating or updating tracker devices, collect the existing device names linked to the hub (for example, via `async_get_device` lookups using the hub identifiers) and store them in a local set.
  * When choosing `use_name`, reuse a matching device entry on collisions; otherwise deterministically disambiguate the name (for example, suffixing with the subentry key or short identifier) and log the collision resolution at debug level.
  * Keep the debug log format stable so tests can assert the disambiguation path when multiple trackers share a hub.
* **Race-condition mitigation:** When programmatically creating subentries, immediately yield control back to the event loop (for example, `await asyncio.sleep(0)`) before invoking `hass.config_entries.async_setup(...)`. This prevents `homeassistant.config_entries.UnknownEntry` errors while the config-entry registry finalizes the new child entry. Leave a short inline comment describing the yield so future contributors retain the guard.
* **Paced discovery guardrails:** When throttling the lightweight device-list refresh, avoid advancing the pacing baseline while we are still retrying a transient empty response (`_EMPTY_LIST_QUORUM` backoff). Keep the cached list and allow the short retry to run before starting a new interval so recovery is not delayed by the long discovery cadence.
* **Empty platform setups:** If a platform can legitimately start without entities (for example, when tracker discovery returns no devices on a cold boot), still call `async_add_entities` once with an empty list right after listener registration. Home Assistant treats this as a successful setup, ensuring unloads succeed even when the integration never surfaces entities during initial discovery.
* **Tracker registry gating:** Keep registry-aware discovery gating centralized **after** entities are scheduled. Use the coordinator’s tracker registry helper (for example, `find_tracker_entity_entry`) inside the post-scheduling discovery guard to skip cloud discovery when every scheduled tracker already exists. Avoid pre-scheduling registry probes that double-count work without changing behavior.
* **Service-device fallback scan:** `_resolve_service_device` now includes a registry scan to detect legacy service identifiers when the primary identifiers are missing. Preserve this fallback when updating relink helpers so service sensors keep targeting the correct device across migrations.
* **Subentry-aware empty registrations:** For subentry platforms, schedule empty `async_add_entities`/`schedule_add_entities` callbacks only after confirming the forwarded `config_subentry_id` matches the one expected by the platform (for example, immediately after `ensure_config_subentry_id`). Skip registration entirely—and log a debug deferral—when Home Assistant forwards an unrelated `config_subentry_id`. When the subentry ID is correct but no entities are produced, register the empty callback immediately after listener setup so unload bookkeeping still completes.
* **Visibility metadata shapes:** Coordinators may expose `_subentry_metadata` entries either as mapping-like objects or attribute containers. Normalize `visible_device_ids` through helper functions before writing them back via `async_update_subentry` so both shapes remain compatible with strict mypy runs and runtime subentry managers.
  * **Log examples:**
    * Mismatch deferral: `Sensor setup skipped for unrelated subentry '<forwarded>' (expected one of: <known ids>)`.
    * Empty registration after listener: `Device tracker setup: subentry_key=<key>, config_subentry_id=<id>` (listener attached) followed by an empty `schedule_add_entities` call before returning.
  * Empty deferrals still need to report as a loaded platform: log the mismatch and call `schedule_add_entities` with an empty list so Home Assistant marks the forwarded subentry as initialized even when no entities apply.
* **Never-loaded platform cleanup:** Whenever a subentry platform unload path is skipped because the platform never loaded, invoke the purge helper that clears tracker entity and device registry links for that subentry before returning. This prevents stale entities from surviving reload attempts and keeps registry hygiene aligned with the teardown patterns above.
* **Thread-safe retry scheduling:** When retry callbacks are dispatched from worker threads or callbacks that may run off the event loop, enqueue the coroutine using `hass.add_job` if available (or `loop.call_soon_threadsafe(...)` as a fallback) instead of `hass.async_create_task`. This preserves Home Assistant’s thread-safety guarantees and keeps retry handles compatible with the shared scheduler helpers.
* **Post-refresh visibility sync:** After `coordinator.async_config_entry_first_refresh()` completes, reconcile each managed subentry’s stored `visible_device_ids` with the coordinator metadata and persist corrections via `async_update_subentry` before refreshing the coordinator’s subentry index. This keeps platform setup aligned with the latest visibility map.

## Quick checklist (runtime patterns)

Use this list as a fast regression aid before or after changing runtime helpers:

* **Subentry hygiene:** Tracker healing must strip stray `config_subentry_id` links for the current entry, apply the correct tracker link, and log the removed IDs.
* **Hub naming:** Collect hub-linked device names before creating trackers, reuse matching entries on collisions, and deterministically disambiguate (with debug logs) when another hub device already owns the label.
* **Setup fan-out:** Forward parent platforms once via `_async_setup_new_subentries`, then dispatch subentry signals so platforms attach entities with the forwarded `config_subentry_id`.
* **Empty registrations:** When a platform has nothing to add (including subentry-aware cases), still register an empty entity list after listener setup so Home Assistant considers the platform initialized.

### Dispatcher pattern example (single forward + per-subentry signal)

Use this pattern to keep platform forwarding (once) separate from per-subentry entity creation:

```python
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send


async def _async_setup_new_subentries(
    hass: HomeAssistant, parent_entry: ConfigEntry, subentries: Iterable[ConfigSubentry]
) -> None:
    platforms = {subentry.data.get("platform") for subentry in subentries}
    await hass.config_entries.async_forward_entry_setups(parent_entry, platforms)

    for subentry in subentries:
        async_dispatcher_send(
            hass, f"{DOMAIN}_subentry_setup_{parent_entry.entry_id}", subentry
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    seen: set[str] = set()

    async def async_add_subentry(subentry: ConfigSubentry) -> None:
        if subentry.subentry_id in seen:
            return
        seen.add(subentry.subentry_id)
        entities = build_entities_for_subentry(subentry)
        await async_add_entities(entities)

    for subentry in entry.runtime_data.subentry_manager.managed_subentries.values():
        await async_add_subentry(subentry)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, f"{DOMAIN}_subentry_setup_{entry.entry_id}", async_add_subentry
        )
    )
```

The forward call runs only once per platform set, while each subentry is delivered via dispatcher with its full context, aligning with the SSoT interface snapshot in [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md#quick-ssot-probe-per-subentry-platform-forwarding).

### Regression note

An earlier fix removed manual platform forwarding but missed programmatic subentry creation after startup, leaving late tracker groups uninitialized. The `_async_setup_new_subentries` helper (and accompanying regression tests) closes that gap by ensuring every new subentry is explicitly set up so Core can forward platforms with the correct `config_subentry_id`.

## Subentry lifecycle references

* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Setup, unload, and removal playbooks (see especially the `ValueError: Config entry ... already been setup!` postmortem).
* [`tests/AGENTS.md`](../../../tests/AGENTS.md) — Discovery and reconfigure test stubs, including the lightweight `ConfigEntry` doubles referenced above.
