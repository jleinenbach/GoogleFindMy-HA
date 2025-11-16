# custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md

## Scope

Runtime contracts, platform forwarding rules, and HA lifecycle helper usage for every module under `custom_components/googlefindmy/`.

## Runtime integration patterns

* Collect runtime-contract reminders for integration touchpoints in this section so future contributors can find them without scanning unrelated guidance.
* View classes under `custom_components/googlefindmy/map_view.py` should expose constructors that accept `HomeAssistant` as the first argument. Register new views by instantiating them with the active `hass` instance (for example, `GoogleFindMyMapView(hass)`) instead of assigning `hass` after creation so the runtime contract stays consistent.
* **Platform forwarding reminder:**
  * When forwarding or unloading the **parent entry's** own platforms, continue passing a `tuple` of platform names to `hass.config_entries.async_forward_entry_setup`/`async_forward_entry_unload`. Home Assistant caches these iterables and expects immutability during parent-level fan-out.
  * When dealing with config subentries (for example, `_async_ensure_subentries_are_setup` and `_unload_config_subentry` inside `__init__.py`), call the **singular** helpers once per platform and include `config_subentry_id` so HA associates each lifecycle hook with the correct child entry.
  * Leave a short inline comment explaining which pattern you chose whenever platform lists move between parent and subentry paths.
* Home Assistant's subentry helpers (`async_add_subentry`, `async_update_subentry`, `async_remove_subentry`) may return a `ConfigSubentry`, a sentinel boolean/`None`, or an awaitable yielding one of those values. Always normalize results through the shared `_await_subentry_result` helper (or an equivalent `inspect.isawaitable` guard) so asynchronous responses update the manager cache correctly.
* **Race-condition mitigation:** When programmatically creating subentries, immediately yield control back to the event loop (for example, `await asyncio.sleep(0)`) before invoking `hass.config_entries.async_setup(...)`. This prevents `homeassistant.config_entries.UnknownEntry` errors while the config-entry registry finalizes the new child entry. Leave a short inline comment describing the yield so future contributors retain the guard.

## Subentry lifecycle references

* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Setup, unload, and removal playbooks (see especially the `ValueError: Config entry ... already been setup!` postmortem).
* [`tests/AGENTS.md`](../../../tests/AGENTS.md) — Discovery and reconfigure test stubs, including the lightweight `ConfigEntry` doubles referenced above.
