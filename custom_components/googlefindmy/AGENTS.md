# custom_components/googlefindmy/AGENTS.md

## Cross-reference index

* [`tests/AGENTS.md`](../../tests/AGENTS.md) — Discovery and reconfigure test stubs, including the lightweight `ConfigEntry` doubles referenced below.

## Config flow registration expectations

* `ConfigFlow.domain` **must** remain explicitly declared in `config_flow.py`. This guards against future upstream changes that might stop injecting the attribute via metaclass magic.
* Do **not** hand-register the flow through `config_entries.HANDLERS` unless Home Assistant drops automatic registration. If a regression forces a manual fallback, document the affected core versions, link the upstream issue, and add a TODO describing when to remove the workaround.
* Tests under `tests/test_config_flow_registration.py` cover both the domain attribute and automatic handler registration. Update them whenever the runtime behavior changes so the expectations stay enforced.
* Reference the Home Assistant developer docs on [config flow registries and handlers](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/#config-flow-handler-registration) when validating upstream behavior; keep this section aligned with any future changes noted there.
* When config flows iterate existing entries, guard optional Home Assistant attributes (for example, `ConfigEntry.source`) so discovery update stubs and other test doubles without those attributes keep working during local runs.

#### Optional `ConfigEntry` attributes in tests

Local discovery and reconfigure tests instantiate lightweight `ConfigEntry` doubles that frequently omit optional attributes Home Assistant adds at runtime.

* `source` — prefer `getattr(entry, "source", None)` before accessing the field so `async_step_discovery` continues to work with the stubs in `tests/test_config_flow_discovery.py`.
* `pref_disable_new_entities` / `pref_disable_polling` — guard these through `getattr(..., False)` when feature toggles depend on them, because flow helpers in the test suite never populate the preferences block.
* `state` — normalize through `getattr(entry, "state", None)` before checking reload eligibility; the discovery update fixtures only set `entry_id`, `data`, and sometimes `unique_id`.

Add similar guards whenever a new optional attribute becomes relevant so future config flow helpers remain compatible with the suite's minimal stubs.

## Service validation fallbacks

* When raising `ServiceValidationError`, always include both the translation metadata (`translation_domain`, `translation_key`, and `translation_placeholders`) **and** a sanitized `message` that reuses the same placeholders. This keeps UI translations working while ensuring Home Assistant surfaces a readable fallback when translations are unavailable.

#### Fallback verification checklist

1. Run `pytest tests/test_hass_data_layout.py::test_service_no_active_entry_placeholders -q` to confirm placeholder usage remains stable.
2. Add new translation-focused tests alongside updates so each fallback path has coverage.

## Typing reminders

* Prefer importing container ABCs (for example, `Iterable`, `Mapping`, `Sequence`) from `collections.abc` rather than `typing` so runtime imports stay lightweight and ruff avoids duplicate definition warnings.
* When adding iterable-type annotations inside `config_flow.py`, reuse the existing `CollIterable` alias to keep type hints consistent with the options-flow helpers and avoid reintroducing stray `typing.Iterable` imports.
* When annotating Firebase Cloud Messaging helpers, reference the `FcmReceiverHAType` alias exported from `ha_typing`. Guard values retrieved from `hass.data` as `object | None` and validate them with `_resolve_fcm_receiver_class()` before returning them so both ruff (undefined name) **and** mypy strict (no `Any` leakage) keep passing while the HTTP stack stays lazily imported.
* When listening for Home Assistant state changes (for example, in `google_home_filter.py`), reuse the module's lazy `_async_track_state_change_event` proxy instead of importing `homeassistant.helpers.event.async_track_state_change_event` at module import time. The proxy keeps pytest stubs effective and avoids forcing Home Assistant's HTTP stack (and its deprecation warnings) to load during integration startup.
* When iterating config flow schemas, always extract the real key from voluptuous markers (`marker.schema`) before using it. Several markers behave like iterables and will yield characters one-by-one if treated as strings, so unwrap before building dictionaries or merging option payloads. See the helper showcased in [`ConfigFlow.async_step_options` (`_resolve_marker_key`)](./config_flow.py) for the canonical extraction pattern.
* When awaiting discovery flow creation results, normalize the outcome through [`_async_resolve_flow_result`](./config_flow.py#L2192-L2211) (the `_resolve_flow_result` helper mentioned in review notes) instead of open-coding `inspect.isawaitable` checks. The helper already mirrors Home Assistant's flow contract and keeps strict mypy runs happy—reuse it so discovery fallbacks stay consistent across handlers.
* When interpreting Home Assistant registry mappings (for example, `DeviceEntry.config_entries_subentries` inside `services.py`), normalize iterable values to concrete `str` members before using them. Treat lone strings as one-item collections and discard non-string placeholders so strict mypy runs keep accepting tuple or set conversions.

## Config entry options persistence reminder

* Treat `ConfigEntry.options` as immutable during reconfigure flows. Build a new dictionary (for example, `existing_options = dict(entry.options or {})`) and pass it directly to `async_update_entry` instead of mutating `entry.options` in place. Home Assistant only persists option changes when it detects a new mapping, so keep the original object untouched until `async_update_entry` returns. See the [`async_step_reconfigure` options-copy pattern](./config_flow.py#L2929-L2943) for a concrete implementation:

  ```python
  existing_options = dict(getattr(entry_for_update, "options", {}) or {})
  existing_options.update(options_payload)
  self.hass.config_entries.async_update_entry(
      entry_for_update,
      data=merged_data,
      options=existing_options,
  )
  ```

## Runtime integration patterns

* Collect runtime-contract reminders for integration touchpoints in this section so future contributors can find them without scanning unrelated guidance.
* View classes under `custom_components/googlefindmy/map_view.py` should expose constructors that accept `HomeAssistant` as the first argument. Register new views by instantiating them with the active `hass` instance (for example, `GoogleFindMyMapView(hass)`) instead of assigning `hass` after creation so the runtime contract stays consistent.
* When forwarding platform unloads via `hass.config_entries.async_forward_entry_unload`, pass the platforms as a `tuple`. Home Assistant caches the provided iterable in hashing structures, and mutable lists trigger `TypeError: unhashable type: 'list'` during subentry unloads.

