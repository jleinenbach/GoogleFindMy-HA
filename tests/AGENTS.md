# AGENTS.md — Test scaffolding guidance

> **Scope & authority**
>
> Applies to the entire `tests/` tree.

## Home Assistant stubs overview

The fixtures in `tests/conftest.py` provide lightweight stand-ins for key
Home Assistant modules so the integration can be imported and exercised in
isolation. The recent additions mirror enough of the `ConfigFlow` contract to
support discovery update tests and follow the real integration's behavior
closely enough for regression coverage.

### `ConfigFlow` helper methods

* `ConfigFlow.async_set_unique_id()` stores the provided ID on both
  `self.unique_id` and `self._unique_id` to match the attribute layout Home
  Assistant uses in production flows. This mirrors the real helper so tests can
  inspect either attribute without diverging from upstream behavior.
* `_abort_if_unique_id_configured(*, updates: Mapping[str, Any] | None = None)`
  updates the matched config entry and triggers `hass.config_entries`
  `async_update_entry` followed by `async_reload`. When an entry is updated, the
  helper raises `AbortFlow` with the `already_configured` reason, maintaining the
  same contract as Home Assistant.
* `_async_invoke_discovery_update()` and the exposed
  `async_step_discovery_update_info()` wrap these helpers to drive the discovery
  update path: normal payloads update the entry, malformed payloads abort with
  `invalid_discovery_info`, and ingestion errors propagate the appropriate
  `AbortFlow` reason (for example `invalid_auth`).

### Exception and sentinel behavior

* `ConfigEntryAuthFailed` mirrors Home Assistant's auth failure exception so the
  integration can convert authentication issues into the correct abort reasons.
* The custom `UNDEFINED` sentinel behaves like the upstream constant and is
  available for any tests that need to assert default/optional handling.

### Adding new helpers

Extend the stubs only when a test requires additional Home Assistant behavior,
and document any new helpers or contract nuances here so future contributors can
quickly understand the supported surface area.
