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

## Cross-reference checklist

* [`docs/CONFIG_SUBENTRIES_HANDBOOK.md`](../../../docs/CONFIG_SUBENTRIES_HANDBOOK.md) — Documents where these strict-mypy fallbacks are applied in the runtime, including the new subentry cross-link list. Keep the handbook and this guide synchronized whenever typing guards or iterator requirements change.
