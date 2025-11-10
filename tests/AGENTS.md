# AGENTS.md — Test scaffolding guidance

> **Scope & authority**
>
> Applies to the entire `tests/` tree.

## Flow helper return modes

The bundled Home Assistant stubs intentionally mix synchronous and
asynchronous helper signatures to mirror real-core behavior. The
`ConfigFlow.async_show_form` implementation in
[`tests/conftest.py`](tests/conftest.py) is defined as an `async def`, so
calling it yields an awaitable that resolves to the response dict. Tests
may temporarily replace that attribute with synchronous callables when
exercising legacy code paths, while the `OptionsFlow.async_show_form`
helper remains synchronous by default. When adding new tests or stubs,
keep this split explicit so flows under test can safely handle both
awaitable and immediate responses.

## Package layout

The test suite is a Python package (`tests/__init__.py`). Use
package-relative imports (for example, `from tests.helpers import foo`)
when sharing utilities across modules so mypy resolves the canonical
module paths consistently.

When a test module depends on optional plugins such as
`pytest-homeassistant-custom-component`, wrap the import in
`pytest.skip(..., allow_module_level=True)` to keep import ordering
intact. The skip guard avoids littering files with inline import
fallbacks and ensures `ruff` continues to enforce top-level grouping.

## Async tests

`pytest-asyncio` ships with the repository and `pytest` manages the event
loop according to the [`asyncio_mode = "auto"`](../pyproject.toml)
configuration. Prefer decorating new coroutine tests with
`@pytest.mark.asyncio` instead of wrapping them in `asyncio.run(...)`; this
keeps event-loop handling centralized and avoids duplicated scaffolding in
each test module.

### Async setup entry harness expectations

The shared ``AsyncSetupEntryHarness`` in
``tests/test_hass_data_layout.py`` centralizes repeated patching for
``async_setup_entry`` coverage. Keep the documented attribute consumers in
sync with reality when refactoring:

* ``test_hass_data_layout`` relies on ``integration``, ``button_module``,
  ``map_view_module``, ``hass``, ``entry``, and ``cache``.
* ``test_async_setup_entry_propagates_subentry_registration`` consumes only
  ``integration``, ``hass``, and ``entry``.

If a test stops using an attribute, update both the harness comment and this
list so future cleanups can trim unused fields confidently.

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

When fabricating bare `hass` objects outside the shared fixtures, import the
stubbed `homeassistant.helpers.frame` module from `tests.conftest` and call
`frame.set_up(hass)` before assigning `config_entry`. The guard mirrors Home
Assistant's runtime expectations and prevents spurious `ValueError` failures
during options-flow tests.

When updating `_StubConfigEntries` in
`tests/test_hass_data_layout.py`, keep its lookup and registration
semantics aligned with `tests.helpers.homeassistant.FakeConfigEntriesManager`.
The stub intentionally mirrors the shared helper so subentry registration
retries in layout tests exercise the same pathways guarded by the reusable
manager.

### Alignment reminder cross-references {#alignment-reminder-cross-references}

<!-- snippet: alignment-reminder-references -->
<details id="alignment-reminder-reference" open>
<summary><strong>Reusable list</strong> — link to this anchor instead of duplicating the entries</summary>

* `tests/helpers/homeassistant.py` (`FakeConfigEntriesManager` inline
  reminder)
* `tests/test_hass_data_layout.py` (`_StubConfigEntries` docstring and
  lookup comment)

</details>
<!-- end snippet: alignment-reminder-references -->

#### Transient `UnknownEntry` simulation helpers

`tests.helpers.homeassistant.FakeConfigEntriesManager` now ships with
first-class controls for reproducing transient `UnknownEntry` races during
tests. Pass a `transient_unknown_entries` mapping when constructing the manager
or call `set_transient_unknown_entry()` on an existing instance to declare how
many initial `async_get_entry` lookups (`lookup_misses`) or `async_setup`
attempts (`setup_failures`) should raise or return `None`. The helper also
tracks `lookup_attempts` so assertions can confirm the integration retried
before child entries became visible. Prefer these controls over ad-hoc
monkeypatching whenever a regression exercises delayed subentry registration or
setup retries.

> **Reminder:** Because these helpers already model `UnknownEntry` behavior,
> new tests should avoid importing `custom_components.googlefindmy`'s
> `UnknownEntry` fallback directly. Use the provided knobs on
> `FakeConfigEntriesManager` instead so transient lookup coverage stays coupled
> to the shared stubs.

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
* When helpers raise `ConfigEntryNotReady`, companion regression tests should
  assert both the warning log and the exception so behavior stays aligned with
  the integration's automatic retry expectations.

### Adding new helpers

Extend the stubs only when a test requires additional Home Assistant behavior,
and document any new helpers or contract nuances here so future contributors can
quickly understand the supported surface area.

#### Deferred subentry identifier assignment helper

Use :func:`tests.helpers.homeassistant.deferred_subentry_entry_id_assignment` to
model Home Assistant cores that publish a child subentry's global ``entry_id``
after the parent has already started setup. The helper schedules the
``entry_id`` update after an optional delay and automatically registers the
provided :class:`FakeConfigEntry` with the shared
``FakeConfigEntriesManager``. Reuse it instead of ad-hoc ``asyncio.sleep``
coroutines so regression tests remain concise and consistent. See
``tests/test_subentry_manager_registry_resolution.py`` for a concrete example of
coordinating the helper with provisional runtime objects.

#### Custom config entries manager subclasses

When a regression needs specialized lookup or creation behavior, subclass
``tests.helpers.homeassistant.FakeConfigEntriesManager`` and hand the custom
instance to the ``FakeHass`` fixture. The snippet below mirrors
``DeferredRegistryConfigEntriesManager``, which simulates Home Assistant cores
that lack ``async_create_subentry`` and delay child visibility until a later
lookup:

```python
from types import SimpleNamespace

from tests.helpers.homeassistant import (
    DeferredRegistryConfigEntriesManager,
    FakeConfigEntry,
    FakeHass,
)

parent_entry = FakeConfigEntry(entry_id="parent")
child = SimpleNamespace(entry_id="child", subentry_id="child-subentry", data={})
manager = DeferredRegistryConfigEntriesManager(parent_entry, child)
hass = FakeHass(config_entries=manager)
```

Attach the configured ``hass`` to the integration under test so registry
publication timing and lookup retries match the scenario being exercised.

#### `config_entry_with_subentries` factory

The :func:`config_entry_with_subentries` helper in
``tests.helpers.homeassistant`` builds a :class:`FakeConfigEntry` prepopulated
with subentries. Provide keyword arguments mapping each subentry key to a
``ConfigSubentry`` payload dictionary—mirroring the runtime helper contract—so
tests can focus on their assertions instead of recreating the boilerplate
structure. The factory normalizes identifiers, attaches the entry to the fake
registry cache, and returns the configured ``FakeConfigEntry`` ready for use in
setup and reload assertions.

#### Device registry listing helper

The shared :func:`device_registry_async_entries_for_config_entry` helper in
``tests.helpers.homeassistant`` returns a deterministic list of
``FakeDeviceEntry`` instances for the provided config entry ID. Prefer importing
and patching this helper in registry-focused tests instead of reimplementing
ad-hoc stubs—doing so keeps device-registry expectations synchronized across
the suite and ensures new assertions automatically benefit from future
enhancements to the shared fake registry.

## Config flow subentry support fixture

The :func:`subentry_support` fixture in ``tests/conftest.py`` centralizes the
monkeypatch scaffolding required to toggle between modern Home Assistant cores
that provide ``ConfigSubentry``/``ConfigSubentryFlow`` and legacy builds that do
not. Request the fixture in a test and call ``toggle.as_modern()`` (the default)
or ``toggle.as_legacy()`` before invoking the config flow under test to assert
the correct behavior in each environment.

### Config flow unique_id helper

Use :func:`tests.helpers.config_flow.set_config_flow_unique_id` whenever a test
needs to assign ``unique_id`` on a ``ConfigFlow`` instance. Home Assistant's
metaclass exposes ``unique_id`` as a read-only descriptor on recent cores, and
the helper mirrors the runtime registration path by storing the identifier in
``flow.context``. Example usage:

```python
from tests.helpers.config_flow import set_config_flow_unique_id

flow = ConfigFlow()
set_config_flow_unique_id(flow, "test-id")
```

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

The coordinator device-registry tests retain Home Assistant's 2025.10
`via_device` tuple support for completeness even though tracker devices no
longer link to the service device. When extending the `_FakeDeviceRegistry`
implementation or adding new tests in `tests/test_coordinator_device_registry.py`,
ensure the helper continues to accept both the legacy `via_device_id` keyword
and the newer tuple form so regression tests can assert that the integration
leaves both fields unset for tracker entries.

### DeviceInfo assertion style

When asserting `DeviceInfo` contents, prefer attribute access (for example,
`info.config_entry_id`) over subscript-style lookups. Home Assistant models
`DeviceInfo` as a dataclass, so attribute reads reflect the production API and
avoid accidental reliance on the helper's mapping-like fallbacks.

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
5. **Playbook parity** — Whenever a regression test targets creation, reload,
   or cleanup logic, mirror the diagnostics from Section VIII.D of
   `docs/CONFIG_SUBENTRIES_HANDBOOK.md`. Assert that registry helpers capture
   the `(entry_id, device_id, identifiers)` tuple and that `add_config_entry_id`
   (or `config_entry_id`) is forwarded, ensuring the documentation and tests
   stay synchronized.

## Translation alignment checks

`tests/test_service_device_translation_alignment.py` loads every locale (including
`strings.json`) and asserts each defines the translation key referenced by
`custom_components.googlefindmy.const.SERVICE_DEVICE_TRANSLATION_KEY`. Update this
test whenever new locales or service-device translation keys are introduced so it
continues to guard localized device names.
