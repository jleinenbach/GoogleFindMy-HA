# AGENTS.md — Test scaffolding guidance

> **Scope & authority**
>
> Applies to the entire `tests/` tree.

## Package layout

The test suite is a Python package (`tests/__init__.py`). Use
package-relative imports (for example, `from tests.helpers import foo`)
when sharing utilities across modules so mypy resolves the canonical
module paths consistently.

## Async tests

`pytest-asyncio` ships with the repository and `pytest` manages the event
loop according to the [`asyncio_mode = "auto"`](../pyproject.toml)
configuration. Prefer decorating new coroutine tests with
`@pytest.mark.asyncio` instead of wrapping them in `asyncio.run(...)`; this
keeps event-loop handling centralized and avoids duplicated scaffolding in
each test module.

## ADM token retrieval contract

The ADM token helpers (`custom_components/googlefindmy/Auth/adm_token_retrieval.py`)
expect an **integer** `android_id`, matching the gpsoauth runtime contract. The
isolated config-flow path under test mirrors this requirement, so regression
tests (for example `tests/test_adm_token_retrieval.py::test_async_get_adm_token_isolated_*`)
must assert integer IDs sourced from secrets bundles, cache reads, or the
fallback constant. Keep expectations synchronized with the production helpers
to avoid drift between config-flow coverage and runtime behavior.

## Home Assistant stubs overview

The fixtures in `tests/conftest.py` provide lightweight stand-ins for key
Home Assistant modules so the integration can be imported and exercised in
isolation. The recent additions mirror enough of the `ConfigFlow` contract to
support discovery update tests and follow the real integration's behavior
closely enough for regression coverage.

## Migration coverage expectations

`tests/test_config_entry_migration.py` exercises the integration's migration
helpers directly. Keep assertions focused on
`custom_components.googlefindmy.__init__.async_migrate_entry` and the associated
duplicate-detection utilities:

* Authoritative entries must soft-migrate known options before the version bump
  and clear any lingering duplicate-account repair issues.
* Non-authoritative duplicates should halt migration, leave options untouched,
  and refresh their `duplicate_account_*` repair issue with the provided
  `migration_duplicate` cause.
* When account metadata cannot provide a usable email (for example, the
  username is missing or whitespace only), the migration should succeed without
  creating duplicate repair issues. The `issue_registry_capture` fixture will
  therefore assert that `created` stays empty and that any stale
  `duplicate_account_<entry_id>` entry is removed during the run.

### Duplicate disable fallbacks

Legacy-core scenarios are simulated in tests by monkeypatching
`hass.config_entries.async_set_disabled_by` to raise `TypeError`. This ensures
manual-action repair issues remain covered whenever the disable API is
unavailable.

### `ConfigFlow` helper methods

* `ConfigFlow.async_set_unique_id()` stores the provided ID on both
  `self.unique_id` and `self._unique_id` to match the attribute layout Home
  Assistant uses in production flows. This mirrors the real helper so tests can
  inspect either attribute without diverging from upstream behavior.
* `ConfigFlow.async_create_entry()` captures the created entry payload so tests
  can assert both the `title` and `data` returned by a flow. The helper records
  each call in order and returns the provided value unchanged to mimic Home
  Assistant's behavior.
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

## Config flow subentry support fixture

The :func:`subentry_support` fixture in ``tests/conftest.py`` centralizes the
monkeypatch scaffolding required to toggle between modern Home Assistant cores
that provide ``ConfigSubentry``/``ConfigSubentryFlow`` and legacy builds that do
not. Request the fixture in a test and call ``toggle.as_modern()`` (the default)
or ``toggle.as_legacy()`` before invoking the config flow under test to assert
the correct behavior in each environment.

### Config subentry factory contract

``ConfigFlow.async_get_supported_subentry_types`` **must** expose zero-argument
factories. Each factory returns a fresh subentry flow instance and the
integration injects the active ``config_entry`` into the instance. Returning
handler classes instead of factories will raise ``TypeError: missing
'config_entry'`` when Home Assistant calls ``factory()`` during the "Add"
interaction. Factories must always return **new** instances (no caching); tests
assert two consecutive calls return distinct objects. Home Assistant constructs
update flows itself using ``(config_entry, subentry)`` and does not expect the
integration's factories to bind existing subentries.

## AST extraction helper

The :mod:`tests.helpers.ast_extract` module exposes
``compile_class_method_from_module`` for compiling individual methods from
integration modules without importing Home Assistant. Import the helper with
``from tests.helpers import compile_class_method_from_module`` and provide the
module path, class name, and method name to retrieve a standalone function that
can be bound using :class:`types.MethodType`.

## Device registry expectations

The coordinator device-registry tests exercise Home Assistant's 2025.10
`via_device` tuple support. When extending the `_FakeDeviceRegistry`
implementation or adding new tests in `tests/test_coordinator_device_registry.py`,
ensure the helper continues to accept both the legacy `via_device_id` keyword
and the newer tuple form, recording the tuple in created-device metadata so the
tests can assert the parent linkage accurately.

### Update checklist for registry stub changes

When adjusting the registry stubs or adding new assertions, confirm the
following to preserve migration coverage:

1. **Identifier continuity** — Existing entries still match on the expected
   identifier tuples after the change.
2. **Config subentry alignment** — Legacy devices without a `config_subentry_id`
   are updated to reference the tracker subentry when migrations run.
3. **`device_id` verification** — Recorded updates are associated with the
   precise device entry under test so assertions catch accidental cross-device
   mutations.
4. **Dual service-device identifiers** — Tests exercising the service device
   must assert both the integration-level identifier and the derived
   service-subentry identifier (see `_service_subentry_identifier(...)`) so
   future expectations stay aligned with the coordinator's registry updates.

## Translation alignment checks

`tests/test_service_device_translation_alignment.py` loads every locale (including
`strings.json`) and asserts each defines the translation key referenced by
`custom_components.googlefindmy.const.SERVICE_DEVICE_TRANSLATION_KEY`. Update this
test whenever new locales or service-device translation keys are introduced so it
continues to guard localized device names.
