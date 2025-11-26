# AGENTS.md — Test scaffolding guidance

> **Scope & authority**
>
> Applies to the entire `tests/` tree.

## Cross-reference index

* [`custom_components/googlefindmy/AGENTS.md`](../custom_components/googlefindmy/AGENTS.md) — Config flow guidance describing optional `ConfigEntry` attributes and discovery safeguards that the shared stubs depend on.

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

When a test or helper needs access to Home Assistant's config-entry
exception classes, call
`tests.helpers.config_entries_stub.install_config_entries_stubs(module)`
first and then reference the exceptions directly off the populated module
(for example, `module.ConfigEntryAuthFailed`, `module.OperationNotAllowed`).
The helper installs every config-entry related export in one place,
including the `UnknownEntry` and `UnknownSubEntry` stubs added for
2025.11 parity, so new tests only need to import the helper once rather
than duplicating definitions across `tests/conftest.py` or other
fixtures.

See the modern registry TypeError propagation reminder in
[`tests/test_coordinator_device_registry.py::test_modern_registry_typeerror_does_not_trigger_legacy_retry`](tests/test_coordinator_device_registry.py#L863-L885)
when updating registry expectations so new coverage stays aligned with
Home Assistant's current registry keyword surface.

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

Whenever the local environment installs the real `homeassistant`
package (for example via `pip install -r requirements-dev.txt`), also
install `pytest-homeassistant-custom-component` and run `pytest -q`.
The plugin ships the canonical Home Assistant stubs that our contract
tests depend on; skipping this pairing risks exercising stale or
partial interfaces.

Expect the paired `pip install --upgrade homeassistant
pytest-homeassistant-custom-component` command to run for roughly five
minutes in the hosted environment. Plan regression runs and shell work
around that window so long installs finish before invoking `pytest`.

When those stubs are active, remember that the entity registry fixture
populates `hass.data["entity_registry"]`. Integration helpers under test
should consult that cached instance before calling
`entity_registry.async_get(...)` so recovery logic inspects the same
registry object as the fixture.

### Tracker discovery gating expectations

Refer to the canonical tracker registry guidance in
[`custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md#tracker-registry-gating`](../custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md#tracker-registry-gating).
Keep new tests aligned with that runtime contract instead of duplicating wording here.

### CoordinatorEntity stub overrides

The suite replaces `homeassistant.helpers.update_coordinator.CoordinatorEntity`
with a minimal stub in `tests/conftest.py` (see
`install_homeassistant_core_stubs`). The override removes availability
helpers such as `_handle_coordinator_update` and `_async_write_ha_state`
to keep tests independent of Home Assistant internals. When integration
code relies on those helpers, add explicit guards or local fallbacks so
entities remain importable under the stub; otherwise, `AttributeError`
failures will surface before the real coordinator features are present.

Keep the coordinator stubs aligned with runtime helpers introduced in
`custom_components.googlefindmy.coordinator` (for example, visibility-wait
helpers) so setup paths under test do not regress with missing attributes.
Update `tests/conftest.py` alongside new coordinator helpers to maintain
parity with production behavior.

When adding or adjusting tests, normalize imports with
`python -m ruff check --select I --fix` so `I001` findings resolve without
manual resorting.

### Config-entry mutation tracking lists

Config-entry manager doubles (for example, the `_ConfigEntries` stub in
`tests/test_config_flow_discovery.py`) expose `updated` tracking lists to
record every `async_update_entry` call. When tests emulate Home Assistant's
config-entry helpers, normalize those lists before asserting so duplicate
records introduced by legacy behaviors or task scheduling are collapsed to
the final update payload.

### Home Assistant callback decorator stub

Use `tests.helpers.install_homeassistant_core_callback_stub` whenever a
test needs to ensure `homeassistant.core.callback` exists. The helper
returns the resolved module, accepts an optional `monkeypatch` fixture so
pytest rolls the stub back automatically, and supports attaching to an
existing module via the `module=` argument (pass `overwrite=True` to
replace an existing decorator). Sharing the helper keeps the suite aligned
on the same identity-based implementation instead of scattering inline
`lambda` definitions across multiple tests.

Refer back to the root [`AGENTS.md`](../AGENTS.md#home-assistant-regression-helper)
section for the `make test-ha` shortcut and its `PYTEST_ARGS` override when
you need to install the full Home Assistant regression bundle before invoking
these tests with additional pytest markers or verbosity flags.

A guard in `tests/test_callback_stub_lint.py` fails if a new
``lambda func: func`` stub appears under `tests/`. Use the shared helper
instead of introducing inline decorators so the lint check stays green.

For a quicker bootstrap when you only need the options-flow regression
suite, run [`script/install_options_flow_test_deps.sh`](../script/install_options_flow_test_deps.sh).
The helper installs the minimal requirements bundle defined in
[`requirements-options-flow-tests.txt`](../requirements-options-flow-tests.txt)
so contributors can verify the targeted tests without recreating the
full development environment.

> **Note:** `pytest -q` buffers its trailing summary when the suite is
> quiet. Wait a moment for the command to exit cleanly, or rerun the
> module without `-q` if you need immediate progress output while
> verifying changes with the real stubs.

### Checklist — real Home Assistant stub pairing

Always review and update this checklist whenever modifying tests,
helpers, or fixtures. Every change must ensure the list stays current
and accurate so contributors can rely on it when the genuine
`homeassistant` package plus `pytest-homeassistant-custom-component`
are installed. **Review this checklist on every single change under
`tests/` and update the entries immediately if expectations shift.**

1. **Frame helper bootstrap** — confirm
   [`prepare_flow_hass_config_entries`](helpers/config_flow.py) still
   invokes the frame helper setup path and patches
   `homeassistant.helpers.frame` (including `report_usage`) so
   `ConfigFlow` instances and options flows operate without manual
   monkeypatching. The helper now registers an `importlib.reload`
   hook that re-applies the `OptionsFlow`/descriptor overrides after
   either `homeassistant.config_entries` or
   `custom_components.googlefindmy.config_flow` reloads; keep that
   hook intact so reloading modules during tests does not drop the
   frame helper patches. Refer to the table below for the regression
   tests that cover each patched helper.
2. **Handler default patch** — verify
   [`_ensure_flow_handler_default`](helpers/config_flow.py) continues to
   set a default `handler` on
   `custom_components.googlefindmy.config_flow.ConfigFlow` instances
   when the upstream implementation omits it.
3. **Service validation shim** — ensure the local
   [`_patch_service_validation_error`](helpers/config_flow.py)
   adjustments retain a descriptive `__str__`/`__repr__`
   implementation compatible with tests that assert translated
   messages.
4. **Config entries manager attachment** — double-check that
   [`prepare_flow_hass_config_entries`](helpers/config_flow.py) still
   assigns the fake manager to `hass.config_entries`, seeds
   `loop`/`loop_thread_id`, and works with the
   [`FakeConfigEntriesManager`](helpers/homeassistant.py).
5. **Options flow config-entry setter** — confirm the patched
   `config_entry` property on Home Assistant’s `OptionsFlow` stub keeps
   accepting assignment so options flow tests can store the entry
   reference (see [`helpers/config_flow.py`](helpers/config_flow.py)).

| Patched helper | Regression coverage |
| --- | --- |
| Frame setup proxy (`module.set_up` / `module.async_setup`) | `pytest tests/test_options_flow_* -q` (invoked via `tests/helpers/config_flow.py::prepare_flow_hass_config_entries` fixtures) |
| `report_usage` proxy (`module.report_usage`) | `pytest tests/test_options_flow_config_entry.py::test_options_flow_reuses_existing_hass` |
| Reload hook (`importlib.reload` patch) | `pytest tests/test_options_flow_reload.py::test_frame_helper_patches_survive_reload` |
6. **Module guards** — validate that tests importing optional Home
   Assistant components continue to wrap those imports in
   `pytest.skip(..., allow_module_level=True)` guards so the suite
   degrades gracefully when the plugin is absent.

Treat this checklist as a living document: if a new helper or guard
becomes necessary, add it here and verify each item before completing
any change under `tests/`.

Contract tests under `tests/test_entity_device_info_contract.py` expect
`pytest-homeassistant-custom-component`'s bundled `homeassistant`
stubs to be installed. Without that optional dependency, pytest will
skip the module-level guard and report a missing `homeassistant`
package error when the test collection runs.

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

### `ServiceValidationError` string representation contract

The `ServiceValidationError` stub derives a descriptive message from
`translation_domain`/`translation_key` when positional arguments are absent and
stores the resolved string on the instance. Both `__str__` and `__repr__`
return that stored message so assertions see the derived text while
`translation_placeholders` and the other translation metadata remain intact for
tests that inspect them directly. When extending the stub, preserve this
behavior to keep message-focused assertions and translation checks aligned.

When fabricating bare `hass` objects outside the shared fixtures, import the
stubbed `homeassistant.helpers.frame` module from `tests.conftest` and call
`frame.set_up(hass)` before assigning `config_entry`. The guard mirrors Home
Assistant's runtime expectations and prevents spurious `ValueError` failures
during options-flow tests. Prefer routing new helpers through
`tests.helpers.config_flow.prepare_flow_hass_config_entries` so the frame setup
and manager attachment stay consistent across modules.

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

### Quick reference — subentry registry race monkeypatch targets

Use these shortcuts when writing regression tests that need to emulate missing
or late subentry registrations:

* `"custom_components.googlefindmy._registered_subentry_ids"` — patch to
  return an empty set and simulate Home Assistant omitting the new subentry
  from its registry during setup.
* `FakeConfigEntriesManager.set_transient_unknown_entry(entry_id, *, lookup_misses=..., setup_failures=...)`
  — drive deterministic `UnknownEntry` responses for repeated
  `async_get_entry` or `async_setup` calls.
* `FakeConfigEntriesManager._registered_subentry_ids` — seed or clear the
  manager's cached registry snapshot before calling integration helpers that
  inspect the parent's registered subentries.

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

#### Config entries flow stub reminder

When fabricating config-entry manager stubs for tests, import
``tests.helpers.config_flow.config_entries_flow_stub`` and attach the returned
flow manager to the stub. The shared helper records flow initialization calls
so discovery-trigger tests (for example,
``tests/test_cloud_discovery_trigger.py``) continue to receive consistent
call-tracking behavior without recreating ad-hoc ``SimpleNamespace`` objects.
The helper returns a manager object exposing ``.flow`` for parity with Home
Assistant's runtime contract—reuse that attribute instead of constructing
inline wrappers in individual tests.

#### Cloud discovery discovery-key fallback coverage

``tests/test_cloud_discovery_trigger.py`` now asserts that
``custom_components.googlefindmy.discovery._trigger_cloud_discovery`` always
passes a structured discovery key to the helper, even when
``cf.DiscoveryKey`` is unavailable in stripped environments. The regression
uses the `_DiscoveryKeyCandidate` dataclass as the deterministic fallback and
asserts that ``namespace`` and ``stable_key`` still match the payload. When
updating the helper or its tests, keep the fallback object exposing a
``key`` attribute that returns the ``(namespace, stable_key)`` tuple so the
assertions remain valid without depending on Home Assistant internals.

### Config entries unique ID lookup helper

Stubs that need :meth:`ConfigEntries.async_entry_for_domain_unique_id` behavior
should import :func:`tests.helpers.config_flow.stub_async_entry_for_domain_unique_id`
and expose it on their manager shim. The helper already understands the variety
of storage layouts used across the test suite, so reusing it keeps new stubs
aligned with Home Assistant's matching semantics without reimplementing the
logic in each test module.

For convenience, ``tests.helpers.config_flow.ConfigEntriesDomainUniqueIdLookupMixin``
wraps the helper above and can be inherited by stubbed manager classes. Reuse
the mixin whenever possible so flow stubs across the suite converge on the same
lookup behavior without duplicating the glue code.

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

### Subentry platform forwarding expectations

Core 2025.11.2 does **not** expose a ``config_subentry_id`` keyword on
``async_forward_entry_setups``. Integration setup must therefore let Home
Assistant schedule subentry platforms automatically after
``_async_setup_subentry`` returns ``True``. Tests must enforce the following
contract:

* Parent setup tests should assert that subentries are created and **not**
  manually forwarded; any helper resembling ``_async_ensure_subentries_are_setup``
  should remain inert.
* Platform tests should continue to assert that Home Assistant invokes
  ``async_setup_entry`` with a populated ``config_subentry_id`` (delivered by
  Core) and that entities/devices refuse to load when the identifier is missing
  while accepting valid IDs. Platform coverage should also verify that entity
  factories iterate ``entry.runtime_data`` and pass ``config_subentry_id`` into
  ``async_add_entities`` per child, matching the WAQI SSoT pattern.
* When a regression needs deterministic ``config_subentry_id`` fallbacks without
  repeating monkeypatch boilerplate, depend on the
  ``deterministic_config_subentry_id`` fixture from
  [`tests/conftest.py`](tests/conftest.py#L230-L275). Adding the fixture to a test
  function signature automatically patches the integration platforms (button,
  sensor, binary_sensor, device_tracker, and entity helpers) so they synthesize
  ``"<entry_id>:<platform>"`` identifiers whenever Home Assistant omits the
  value.

When parent-unload rollbacks are exercised (for example,
``tests/test_unload_subentry_cleanup.py::test_async_unload_entry_rolls_back_when_parent_unload_fails``),
the helper now expects retries **per platform** as part of the cleanup
scheduling. Guard the recorded platform set so regressions neither skip
forwarding nor double-schedule retries.

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

For shared Home Assistant registry stubs referenced across multiple modules,
start with [`_StubDeviceRegistry` in `tests/conftest.py`](tests/conftest.py#L1035-L1196).
The helper documents the canonical keyword support (`add_config_entry_id`,
`remove_config_entry_id`, `add_config_subentry_id`, `remove_config_subentry_id`)
and records each payload so coordinator- and service-level tests observe the
same removal behavior.

When expanding purge or cleanup coverage, mirror Home Assistant's registry
helper API surface (including `async_entries_for_config_entry` and
`async_remove`) instead of inventing bespoke stubs so new tests continue to use
the shared registry doubles.

Reuse `tests.helpers.service_device_stub` whenever a test needs a
`SimpleNamespace`-based service-device object with
`config_entries_subentries` metadata. The shared factory keeps identifier
defaults aligned across modules and avoids drift between coordinator and
registry-rebuild coverage as new scenarios are added.

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
