# custom_components/googlefindmy/agents/typing_guidance/AGENTS.md

## Scope

Type-hinting, import-guard, and strict mypy reminders that apply to every module under `custom_components/googlefindmy/`.

## Typing reminders

* Prefer importing container ABCs (for example, `Iterable`, `Mapping`, `Sequence`) from `collections.abc` rather than `typing` so runtime imports stay lightweight and ruff avoids duplicate definition warnings. Import coroutine annotations (for example, `Coroutine`) from `collections.abc` as well to prevent redundant definitions that trigger duplicate-import lint errors.
* When adding iterable-type annotations inside `config_flow.py`, reuse the existing `CollIterable` alias to keep type hints consistent with the options-flow helpers and avoid reintroducing stray `typing.Iterable` imports.
* When annotating Firebase Cloud Messaging helpers, reference the `FcmReceiverHAType` alias exported from `ha_typing`. Guard values retrieved from `hass.data` as `object | None` and validate them with `_resolve_fcm_receiver_class()` before returning them so both ruff (undefined name) **and** mypy strict (no `Any` leakage) keep passing while the HTTP stack stays lazily imported.
* When listening for Home Assistant state changes (for example, in `google_home_filter.py`), reuse the module's lazy `_async_track_state_change_event` proxy instead of importing `homeassistant.helpers.event.async_track_state_change_event` at module import time. The proxy keeps pytest stubs effective and avoids forcing Home Assistant's HTTP stack (and its deprecation warnings) to load during integration startup.
* When iterating config flow schemas, always extract the real key from voluptuous markers (`marker.schema`) before using it. Several markers behave like iterables and will yield characters one-by-one if treated as strings, so unwrap before building dictionaries or merging option payloads. See the helper showcased in [`ConfigFlow.async_step_options` (`_resolve_marker_key`)](../../config_flow.py) for the canonical extraction pattern.
* When awaiting discovery flow creation results, normalize the outcome through [`_async_resolve_flow_result`](../../config_flow.py#L2192-L2211) (the `_resolve_flow_result` helper mentioned in review notes) instead of open-coding `inspect.isawaitable` checks. The helper already mirrors Home Assistant's flow contract and keeps strict mypy runs happy—reuse it so discovery fallbacks stay consistent across handlers.
* When interpreting Home Assistant registry mappings (for example, `DeviceEntry.config_entries_subentries` inside `services.py`), normalize iterable values to concrete `str` members before using them. Treat lone strings as one-item collections and discard non-string placeholders so strict mypy runs keep accepting tuple or set conversions.

## Runtime vs. type-checking import quick reference

Use the following patterns whenever a module only exists as a `.pyi` stub or when the runtime dependency must stay optional:

1. **Guard the stub import** so production code never tries to import the missing module:

   ```python
   from typing import TYPE_CHECKING

   if TYPE_CHECKING:
       from custom_components.googlefindmy.protobuf_typing import MessageProto
   else:
       from google.protobuf.message import Message as MessageProto
   ```

2. **Provide a runtime alias** to the concrete implementation (or a graceful fallback) so the rest of the module can use the shared name without knowing whether it came from the stub or the runtime module.

3. **Avoid work in the `TYPE_CHECKING` block.** Limit the guarded section to imports and type-only definitions; execute all runtime logic outside the guard so mypy and the interpreter share the same behavior.

4. **Catch only `ImportError` when providing runtime fallbacks.** Optional integration helpers should surface unexpected runtime exceptions immediately instead of masking them behind broad `except Exception:` guards. This keeps startup failures debuggable and prevents silent misconfiguration when a dependency is present but broken for other reasons.

## Optional import fallback pattern (`type()` guard)

When Home Assistant introduces a new helper or exception (for example, `OperationNotAllowed`), the integration must remain importable on legacy cores that do not yet ship that attribute. Guard those imports with a `try/except ImportError` block and construct a typed fallback via `type()` so both ruff (import ordering) and `mypy --strict` accept the shim:

```python
try:
    from homeassistant.config_entries import OperationNotAllowed
except ImportError:  # Pre-2025.5 HA builds do not expose the helper.
    from homeassistant.exceptions import HomeAssistantError

    OperationNotAllowed = type("OperationNotAllowed", (HomeAssistantError,), {})
```

The dynamically created fallback must inherit from an existing Home Assistant error (usually `HomeAssistantError`) and be assigned immediately after the guarded import so downstream modules can reference the shared symbol without additional `# type: ignore` comments. Prefer short inline comments that state which Home Assistant versions lack the helper so future contributors know when the guard can be removed.

## Coordinator mixin typing — `_MixinBase` pattern

The coordinator uses a **mixin composition pattern**: six Operations classes
(`RegistryOperations`, `SubentryOperations`, `LocateOperations`,
`IdentityOperations`, `PollingOperations`, `CacheOperations`) are composed into
the final `GoogleFindMyCoordinator` via multiple inheritance.

### Problem

Mypy cannot resolve cross-mixin attribute and method references (e.g.
`self.hass`, `self.config_entry`, or a call from `PollingOperations` into a
`CacheOperations` method) because each mixin class does not individually
inherit from the coordinator or `DataUpdateCoordinator`.

The earlier workaround — annotating `self: GoogleFindMyCoordinator` on every
mixin method — is rejected by mypy `--strict` with `[misc]` errors because
`GoogleFindMyCoordinator` is a *subtype* (child) of each mixin, not a
*supertype* (parent), violating mypy's requirement that the self-type
annotation must be a supertype of the enclosing class.

### Solution

`coordinator/_mixin_typing.py` defines `_MixinBase`, a **type-declaration-only
base class** that declares the union of all attributes and method signatures
from `DataUpdateCoordinator`, `GoogleFindMyCoordinator.__init__`, and every
cross-mixin method. All six mixin classes inherit from `_MixinBase`:

```python
from ._mixin_typing import _MixinBase

class RegistryOperations(_MixinBase):
    ...
```

At runtime `_MixinBase` is essentially empty: attribute annotations create no
instance state, and method stubs raise `NotImplementedError` (immediately
shadowed by the real implementations in the composed class hierarchy). Mypy,
however, gains full visibility into the coordinator interface when type-checking
any mixin.

### Maintenance rules

* When adding a **new attribute** to `GoogleFindMyCoordinator.__init__`, add a
  matching annotation to `_MixinBase`.
* When adding a **new method** that is called across mixin boundaries, add a
  stub to `_MixinBase` with the same signature and `raise NotImplementedError`.
* Keep `_MixinBase` free of any runtime logic — it exists purely for static
  analysis.

## Explicit re-export pattern

Under `mypy --strict` (specifically `no_implicit_reexport`), a bare
`from .module import x` is **not** considered a public re-export. Modules that
re-export symbols for use by other packages must use the explicit form:

```python
from .shared_helpers import (
    known_ids_for_subentry_type as known_ids_for_subentry_type,
    normalize_fcm_entry_snapshot as normalize_fcm_entry_snapshot,
)
```

The `as x` suffix signals to mypy that the import is intentionally public.
Without it, downstream imports trigger `[attr-defined]` errors.

## `cast()` for Home Assistant API returns

Because `pyproject.toml` sets `follow_imports = "skip"` for all `homeassistant`
modules, every HA API call returns `Any` from mypy's perspective. When
`warn_return_any` is active (included in `--strict`), returning such values
from typed functions triggers `[no-any-return]`. Use `cast()` to assert the
expected type:

```python
from typing import cast

result: str = await hass.async_add_executor_job(_get_local_ip_sync)
return result
```

Or for optional lookups:

```python
return cast("GoogleFindMyEIDResolver | None", domain_data.get(DATA_EID_RESOLVER))
```

Prefer `cast()` over `# type: ignore[no-any-return]` so the expected type is
documented and future regressions are caught if the return type changes.

## Exception variable scoping

Python 3 deletes exception variables after the `except` block exits. Do not
reuse the same variable name for a manually constructed exception within the
same scope:

```python
# BAD — auth_exc is deleted after the except block
except ConfigEntryAuthFailed as auth_exc:
    ...
auth_exc = ConfigEntryAuthFailed("manual reason")  # NameError at runtime

# GOOD — use a different name
except ConfigEntryAuthFailed as auth_exc:
    ...
reauth_exc = ConfigEntryAuthFailed("manual reason")
```

## Cross-reference checklist

* [`coordinator/_mixin_typing.py`](../../coordinator/_mixin_typing.py) — Canonical `_MixinBase` type-declaration base for coordinator mixins.
* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Documents where these strict-mypy fallbacks are applied in the runtime, including the new subentry cross-link list. Keep the handbook and this guide synchronized whenever typing guards or iterator requirements change.
