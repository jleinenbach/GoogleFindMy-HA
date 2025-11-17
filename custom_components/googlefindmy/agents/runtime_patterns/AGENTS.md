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
* Home Assistant's subentry helpers (`async_add_subentry`, `async_update_subentry`, `async_remove_subentry`) may return a `ConfigSubentry`, a sentinel boolean/`None`, or an awaitable yielding one of those values. Always normalize results through the shared `_await_subentry_result` helper (or an equivalent `inspect.isawaitable` guard) so asynchronous responses update the manager cache correctly.
* **Race-condition mitigation:** When programmatically creating subentries, immediately yield control back to the event loop (for example, `await asyncio.sleep(0)`) before invoking `hass.config_entries.async_setup(...)`. This prevents `homeassistant.config_entries.UnknownEntry` errors while the config-entry registry finalizes the new child entry. Leave a short inline comment describing the yield so future contributors retain the guard.

### Regression note

An earlier fix removed manual platform forwarding but missed programmatic subentry creation after startup, leaving late tracker groups uninitialized. The `_async_setup_new_subentries` helper (and accompanying regression tests) closes that gap by ensuring every new subentry is explicitly set up so Core can forward platforms with the correct `config_subentry_id`.

## Subentry lifecycle references

* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Setup, unload, and removal playbooks (see especially the `ValueError: Config entry ... already been setup!` postmortem).
* [`tests/AGENTS.md`](../../../tests/AGENTS.md) — Discovery and reconfigure test stubs, including the lightweight `ConfigEntry` doubles referenced above.
