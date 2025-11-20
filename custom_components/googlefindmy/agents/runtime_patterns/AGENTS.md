# custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md

## Scope

Runtime contracts, platform forwarding rules, and HA lifecycle helper usage for every module under `custom_components/googlefindmy/`.

## Runtime integration patterns

* Collect runtime-contract reminders for integration touchpoints in this section so future contributors can find them without scanning unrelated guidance.
* View classes under `custom_components/googlefindmy/map_view.py` should expose constructors that accept `HomeAssistant` as the first argument. Register new views by instantiating them with the active `hass` instance (for example, `GoogleFindMyMapView(hass)`) instead of assigning `hass` after creation so the runtime contract stays consistent.
* **Platform forwarding reminder:**
  * The parent `async_setup_entry` handles coordinator/bootstrap work **and subentry creation only**. After creating child entries (and mirroring any parent-coordinator setup in `_async_setup_subentry`), return `True`—Home Assistant will automatically call each platform's `async_setup_entry` with the correct `config_subentry_id` for every subentry.
  * Do **not** call `async_forward_entry_setups` or any helper resembling `_async_ensure_subentries_are_setup` for subentries. Manual forwarding strips `config_subentry_id` and reintroduces the double-setup regression.
  * When forwarding or unloading the **parent entry's** own platforms, continue passing a `tuple` of platform names to `hass.config_entries.async_forward_entry_setups`/`async_forward_entry_unload`. Home Assistant caches these iterables and expects immutability during parent-level fan-out.
  * Each platform must iterate the subentry coordinators stored on `entry.runtime_data` and add entities per child via `async_add_entities(..., config_subentry_id=subentry_id)`. Validate the identifier before emitting entities and log a debug deferral when Home Assistant omits it.
  * Leave a short inline comment explaining which pattern you chose whenever platform lists move between parent and subentry paths.
  * The legacy `RuntimeData.legacy_forwarded_platforms`/`legacy_forward_notice` tracking remains in the codebase for historical builds but should stay dormant on modern Home Assistant releases.
* Use the shared `_async_setup_new_subentries` helper whenever subentries are created (during parent setup **and** at runtime) so every child receives a setup pass and Core can forward platforms with the correct `config_subentry_id`.
* Once subentries exist, invoke `_async_setup_new_subentries` **without** `enforce_registration=True`. The registration delay loop is reserved for missing IDs; removing the flag prevents an infinite `ConfigEntryNotReady` retry cycle when Home Assistant is already finalizing the registry.
  * See [`docs/CONFIG_SUBENTRIES_HANDBOOK.md#automatic-retry-queue-for-unknownentry`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md#automatic-retry-queue-for-unknownentry) for the bounded retry/backoff design that `_async_setup_new_subentries` enforces when `UnknownEntry` fires.
  * The retry queue stores its handles and per-subentry attempt counts on `entry.runtime_data` (for example, `RuntimeData.subentry_retry_handles` and `RuntimeData.subentry_retry_attempts`). Keep those runtime caches typed and cleaned up during unload so strict mypy runs continue to catch handle leaks or mismatched awaitables as the queue gains new states. After adjusting imports near these helpers, run `python -m ruff check --select I --fix` to keep the lint-driven sorting from masking future docstring or typing diffs.
* Home Assistant's subentry helpers (`async_add_subentry`, `async_update_subentry`, `async_remove_subentry`) may return a `ConfigSubentry`, a sentinel boolean/`None`, or an awaitable yielding one of those values. Always normalize results through the shared `_await_subentry_result` helper (or an equivalent `inspect.isawaitable` guard) so asynchronous responses update the manager cache correctly.
* **Race-condition mitigation:** When programmatically creating subentries, immediately yield control back to the event loop (for example, `await asyncio.sleep(0)`) before invoking `hass.config_entries.async_setup(...)`. This prevents `homeassistant.config_entries.UnknownEntry` errors while the config-entry registry finalizes the new child entry. Leave a short inline comment describing the yield so future contributors retain the guard.
* **Empty platform setups:** If a platform can legitimately start without entities (for example, when tracker discovery returns no devices on a cold boot), still call `async_add_entities` once with an empty list right after listener registration. Home Assistant treats this as a successful setup, ensuring unloads succeed even when the integration never surfaces entities during initial discovery.
* **Subentry-aware empty registrations:** For subentry platforms, schedule empty `async_add_entities`/`schedule_add_entities` callbacks only after confirming the forwarded `config_subentry_id` matches the one expected by the platform (for example, immediately after `ensure_config_subentry_id`). Skip registration entirely—and log a debug deferral—when Home Assistant forwards an unrelated `config_subentry_id`. When the subentry ID is correct but no entities are produced, register the empty callback immediately after listener setup so unload bookkeeping still completes.
  * **Log examples:**
    * Mismatch deferral: `Sensor setup skipped for unrelated subentry '<forwarded>' (expected one of: <known ids>)`.
    * Empty registration after listener: `Device tracker setup: subentry_key=<key>, config_subentry_id=<id>` (listener attached) followed by an empty `schedule_add_entities` call before returning.
* **Thread-safe retry scheduling:** When retry callbacks are dispatched from worker threads or callbacks that may run off the event loop, enqueue the coroutine using `hass.add_job` if available (or `loop.call_soon_threadsafe(...)` as a fallback) instead of `hass.async_create_task`. This preserves Home Assistant’s thread-safety guarantees and keeps retry handles compatible with the shared scheduler helpers.

### Regression note

An earlier fix removed manual platform forwarding but missed programmatic subentry creation after startup, leaving late tracker groups uninitialized. The `_async_setup_new_subentries` helper (and accompanying regression tests) closes that gap by ensuring every new subentry is explicitly set up so Core can forward platforms with the correct `config_subentry_id`.

## Subentry lifecycle references

* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Setup, unload, and removal playbooks (see especially the `ValueError: Config entry ... already been setup!` postmortem).
* [`tests/AGENTS.md`](../../../tests/AGENTS.md) — Discovery and reconfigure test stubs, including the lightweight `ConfigEntry` doubles referenced above.
