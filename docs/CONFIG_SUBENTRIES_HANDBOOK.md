# Home Assistant Config Subentry Handbook (Home Assistant 2025.7+)

This handbook captures the Home Assistant 2025.7+ contract for configuration subentries. It expands on the architectural reasons, runtime data model, config flow mechanics, lifecycle management, translation rules, discovery patterns, and peer-review checklist required for error-free implementations. Treat it as the authoritative in-repo reference when working on Google Find My Device's subentry support.

## Quick summary

- **Global identifier handling:** Use `subentry.entry_id` for lifecycle helpers whenever Home Assistant exposes it, but Core 2025.11 often omits the attribute entirely. Always fall back to `subentry.subentry_id` (or the identifier returned by `_resolve_config_subentry_identifier`) so runtime code keeps a stable ULID across all builds.
- **Deferred lifecycle setup:** When a parent creates subentries in the same transaction, schedule `_async_ensure_subentries_are_setup` (or equivalent helpers) via `entry.async_create_background_task(hass, ...)` so Home Assistant can finish registering the children before setup begins while preserving ConfigEntry lifecycle error handling. See the inline race-condition commentary in `custom_components/googlefindmy/__init__.py` near the `_async_ensure_subentries_are_setup` scheduling block for the canonical implementation details.
- **Device/registry repairs:** Follow Section VIII.D for orphan detection and rebuild workflows; always include the child `entry_id` when updating tracker/service devices.
- **Style note for quick references:** When adding concise checklists or reminders inside a subsection, anchor them at the `####` level (for example, `#### Race-condition checklist`) beneath the owning `###` heading so the handbook's numbering remains stable and navigation panes keep related guidance grouped together.

## Section I: Architectural Mandate — Why Config Subentries Exist

### A. The problem solved by subentries

Home Assistant versions prior to 2025.7 enforced a 1:1 relationship between a `ConfigEntry` and the integration instance that owned it. Every additional configuration required re-creating the full integration, including re-entering credentials. Subentries decouple credentials from configuration payloads so a single parent entry can support many logical children. Common scenarios include:

- **AI and conversation agents (OpenAI, Google AI, Anthropic):** Each agent needs distinct prompts and settings but can share the same API key. The parent entry stores credentials; each agent becomes a subentry referencing the shared key.
- **"Building block" integrations (MQTT, KNX):** The parent entry stores the broker or bus connection. Devices configured through the UI become subentries instead of YAML blocks, making UI-driven creation and management possible.
- **Multi-site services (weather providers, WAQI, etc.):** One API key in the parent entry supports many per-location subentries so users no longer re-enter credentials for each city.

### B. The new hierarchy

Config subentries insert an optional layer between the parent config entry and the device/entity registries:

```
ConfigEntry (parent)
  └─ Config Subentry (child, optional)
        └─ Device Registry Entry
             └─ Entity Registry Entry
```

Lifecycle events cascade through this hierarchy: removing a parent unloads and removes its children, their devices, and entities in turn.

### C. Terminology

| Term | Definition |
| ---- | ---------- |
| `ConfigEntry` (parent) | Represents the integration instance. Stores shared credentials or connections and owns subentries. |
| `ConfigSubentry` (child) | A `ConfigEntry` instance with `parent_entry_id` set. Each subentry expresses a configuration that consumes the parent resources (for example, an AI agent or weather location). |
| Parent `ConfigFlow` | The standard flow that creates the parent entry (for example, prompts for API keys) by inheriting from `config_entries.ConfigFlow`. |
| `ConfigSubentryFlow` | A dedicated flow that inherits from `config_entries.ConfigSubentryFlow` and is responsible for creating or reconfiguring a child entry. |

### D. Subentry type strings and UI aggregation

Each subentry declares a **type string** (for example, `ai_agent`). Home Assistant aggregates subentries by type across all integrations. For instance, both the `openai_conversation` and `anthropic` integrations expose subentries of type `ai_agent`, letting the UI render a unified "AI Agents" page regardless of the parent integration.

The subentry architecture was explicitly chosen over storing child configuration inside `ConfigEntry.options`. Options cannot be aggregated across integrations, which would prevent Home Assistant from building feature-centric dashboards (for example, a global "AI Agents" view). Matching the `subentry_type` string in both `config_flow.py` and `strings.json` is therefore essential to keep the cross-integration UI functional.

## Section II: Data Model and Storage

### A. `core.config_entries`

All config entries and subentries are stored in `<config_dir>/.storage/core.config_entries`. Never edit this file while Home Assistant is running. Parent and child entries are peers inside the JSON array, differentiated by `parent_entry_id`.

**Parent entry example:**

```json
{
  "entry_id": "a1b2c3d4e5f6",
  "version": 1,
  "domain": "openai",
  "title": "OpenAI",
  "data": { "api_key": "sk-..." },
  "options": {},
  "system_options": {},
  "source": "user",
  "connection_class": "cloud_push",
  "unique_id": "openai-unique-id"
}
```

**Subentry example:**

```json
{
  "entry_id": "f6e5d4c3b2a1",
  "version": 1,
  "domain": "openai",
  "title": "My personal AI agent",
  "data": { "prompt": "You are a helpful assistant." },
  "options": {},
  "system_options": {},
  "source": "user",
  "connection_class": "cloud_push",
  "unique_id": "openai-agent-1",
  "parent_entry_id": "a1b2c3d4e5f6"
}
```

### B. Runtime representation

Home Assistant loads both parents and subentries as `homeassistant.config_entries.ConfigEntry` instances. A `ConfigEntry` is a subentry when `entry.parent_entry_id` is set; otherwise it is the parent. Runtime logic and tests must branch on this property.

## Section III: Config Flow Implementation (`config_flow.py`)

### A. Declaring support in the parent flow

The parent `ConfigFlow` must implement `@classmethod @callback async_get_supported_subentry_types(cls, config_entry)` and return a mapping of subentry type strings to zero-argument factories that build `ConfigSubentryFlow` instances.

> **When to return an empty mapping.** Integrations that *only* manage subentries programmatically (for example, Google Find My Device after the hub and tracker entries are synchronized during `async_setup_entry`) **must** return `{}` here. Doing so prevents Home Assistant from exposing manual “Add subentry” buttons in the UI, keeping the UX aligned with the architecture that expects all children to be created automatically. See `tests/test_config_flow_basic.py::test_supported_subentry_types_disable_manual_flows` and `tests/test_config_flow_hub_entry.py::test_supported_subentry_types_disable_manual_hub_additions` for regression coverage that asserts the UI stays hidden in both basic and hub-specific flows.

```python
class ExampleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        if user_input:
            return self.async_create_entry(title="Example API", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("api_key"): str}),
        )

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: config_entries.ConfigEntry,
    ) -> dict[str, config_entries.ConfigSubentryFlowFactory]:
        return {"location": lambda: LocationSubentryFlowHandler()}
```

### B. Subentry flow handler

Subentry flows inherit from `ConfigSubentryFlow` and expose `async_step_user` plus optional `async_step_reconfigure` for editing existing children. Always access parent data via `self._get_entry()` and reconfigure targets via `self._get_reconfigure_subentry()`.

```python
class LocationSubentryFlowHandler(config_entries.ConfigSubentryFlow):
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        parent_entry = self._get_entry()
        api_key = parent_entry.data["api_key"]
        errors: dict[str, str] = {}

        if user_input:
            return self.async_create_entry(
                title=user_input["location_name"],
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required("location_name"): str}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        parent_entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()

        if user_input:
            return self.async_update_reload_and_abort(
                subentry,
                data={**subentry.data, **user_input},
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema({
                vol.Required(
                    "location_name",
                    default=subentry.data["location_name"],
                ): str,
            }),
        )
```

Avoid the deprecated helpers `_reconfigure_entry_id` and `_get_reconfigure_entry()`; Home Assistant renamed them to the methods shown above.

### C. Unique IDs

Subentries may set `unique_id`. The value only needs to be unique within the scope of the parent entry. Config flows should call `self.async_set_unique_id(...)` with a parent-scoped identifier before `self.async_create_entry(...)`.

## Section IV: Lifecycle Management (`__init__.py`)

Subentries change how integrations load and unload config entries. `async_setup_entry` and `async_unload_entry` must differentiate between parent and child entries.

### A. Identifier guardrails (`entry_id` vs. `subentry_id`)

Config subentries expose **two** identifiers. Misusing them leads to reload failures that surface as stuck entries or missing entities.

| Identifier | Scope | When to use |
| ---------- | ----- | ----------- |
| `subentry.subentry_id` | Key inside `parent_entry.subentries` | Iterating or mutating the parent mapping (for example, to look up a specific child in memory). |
| `subentry.entry_id` | Global config entry registry key (removed in many Core 2025.11+ builds) | **Every lifecycle helper** (`async_setup`, `async_reload`, `async_unload`, `async_remove_subentry`, etc.). Guard access with `getattr` and fall back to `subentry.subentry_id`. |

**Critical rule:** Always pass the subentry's global ULID to Home Assistant's config entry helpers. In steady state this is exposed via `subentry.entry_id`; when a child has just been created in the same transaction and Home Assistant has not yet populated `entry_id`, fall back to `subentry.subentry_id`. Passing a blank identifier or mixing in logical keys (for example, `core_tracking`) will raise `UnknownEntry` internally and prevent reload-driven rebuild services from completing.

> **Attribute expectations (The Setup Race Condition)**
>
> * Home Assistant 2025.7 through 2025.10 exposed both `entry_id` and `subentry_id`, but Core 2025.11 removed `entry_id` from the runtime object. Treat `subentry.subentry_id` as the canonical ULID and only use `entry_id` when it exists.
> * **CAUTION (Race Condition):** When a parent programmatically creates a subentry and immediately triggers setup in the same transaction, Home Assistant may not have populated `.entry_id` yet. The resulting `AttributeError` (or `None`) indicates that `.subentry_id` is currently the only reliable attribute holding the ULID.
> * **CAUTION (Timing Race Condition):** Even if the ULID is retrieved (from `.entry_id` or `.subentry_id`), calling `hass.config_entries.async_setup(ULID)` in the *same transaction* may fail with `UnknownEntry` because Home Assistant Core has not finished registering the new child entry.
> * **Implementation rule:** Parent workflows that call lifecycle helpers right after creating children (such as `_async_ensure_subentries_are_setup`) MUST (1) try `getattr(subentry, "entry_id")` first and fall back to `getattr(subentry, "subentry_id")` when the primary attribute is missing, and (2) defer the setup call (for example, using `entry.async_create_background_task(hass, ...)`) so the event loop can finish the registration before the helper runs and ConfigEntryNotReady handling stays attached to the entry lifecycle.
>
> **Quick reference**
> * Use `subentry.entry_id` (or the fallback to `subentry.subentry_id`) with Home Assistant lifecycle helpers.
> * Use `subentry.subentry_id` when indexing `entry.subentries`.
> * The warning "Never mix identifiers" applies to confusing the global ULID (`entry_id`/`subentry_id`) with logical keys (`core_tracking`, `service`, etc.).

#### Race-condition checklist

When spawning lifecycle work for freshly created children, confirm the config entry registry has finalized the record before calling helpers such as `async_setup`, `async_reload`, or `async_remove_subentry`:

1. Yield to the event loop (for example, `await asyncio.sleep(0)`) from the background task that will invoke the lifecycle helper.
2. Validate registry visibility **after** yielding by fetching the child entry via `hass.config_entries.async_get_entry(child_ulid)` (or equivalent) and ensure the result is not `None`.
3. Only call the lifecycle helper once the registry lookup succeeds; if it fails, continue yielding and re-checking instead of assuming a single `await asyncio.sleep(0)` resolved the race.

### B. Routing setup

```python
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.parent_entry_id:
        return await _async_setup_subentry(hass, entry)
    return await _async_setup_parent_entry(hass, entry)
```

### C. Parent setup

1. Create the shared client (for example, API session, MQTT connection). Raise `ConfigEntryNotReady` if the connection cannot be established.
2. Store the runtime data container in the shared entries bucket: `hass.data.setdefault(DOMAIN, {}).setdefault("entries", {})[entry.entry_id] = RuntimeData(...)`. Keeping every parent under `hass.data[DOMAIN]["entries"]` is mandatory so `_async_setup_subentry` can read the parent's runtime data from the canonical path.
3. Enumerate children via `list(entry.subentries.values())` and `async_setup` each child. This ensures subentries created while the parent was unloaded are loaded on restart. Allow setup failures to propagate (do **not** pass `return_exceptions=True` to `asyncio.gather`) so Home Assistant correctly reflects parent/subentry health.
   - Log a warning when any child returns `False` so platform owners can spot skipped subentry initializations in production logs. The integration's regression tests assert this behavior; keep the warning intact to preserve visibility into partial setup failures.
4. Register an update listener that reloads children when the parent options change.

```python
async def _async_setup_parent_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the parent entry and its children."""

    subentries = list(entry.subentries.values())
    if subentries:
        await asyncio.gather(
            *(
                hass.config_entries.async_setup(subentry.entry_id)
                for subentry in subentries
            )
        )

    # ... continue with parent setup logic ...
```

The `hass.config_entries.async_get_subentries` helper referenced in older drafts of the handbook does **not** exist. Always enumerate children from the parent entry's `subentries` mapping as shown above.

### D. Subentry setup

1. Read `parent_entry_id = entry.parent_entry_id`.
2. Retrieve the shared runtime data from `hass.data[DOMAIN]["entries"][parent_entry_id]`. If the key is missing, raise `ConfigEntryNotReady` to retry later so the parent can rebuild its shared container.
3. Attach any subentry-specific runtime data (for example, `_async_setup_subentry` should assign `entry.runtime_data` from the parent's `entries` bucket before platforms load).
4. Forward setup to the platforms with `await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)`.

Avoid the obsolete lookup pattern `hass.data[DOMAIN][parent_entry_id]`. The direct dictionary path predates the shared `"entries"` bucket and will fail once multiple parents coexist under the integration namespace.

### E. Parent update listener and reload sequencing

Update listeners must operate exclusively on `entry.subentries`. When options change, reload the parent entry first so the integration rebuilds `hass.data[DOMAIN]["entries"][entry.entry_id]` before children resume setup. After the parent completes reloading, iterate through `entry.subentries` and reload each child sequentially. Avoid `asyncio.gather` here—the parent rebuild must finish before the subentries read the refreshed runtime data bucket:

```python
async def _parent_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates on the parent entry."""

    subentries = list(entry.subentries.values())

    await hass.config_entries.async_reload(entry.entry_id)
    for subentry in subentries:
        await hass.config_entries.async_reload(subentry.entry_id)

```

#### Concurrency pitfalls

* Do not schedule parent and child reloads concurrently—the shared runtime data
  in `hass.data[DOMAIN]["entries"][entry.entry_id]` must be rebuilt before any
  child setup runs.
* Avoid reloading subentries in parallel unless every child can safely handle
  a missing client and retry later. Sequential reloads keep the listener simple
  and prevent `KeyError`/`ConfigEntryNotReady` loops when shared data is still
  initializing.

### F. Parent unload

1. Fetch all subentries via `list(entry.subentries.values())`.
2. `async_unload` every child and aggregate the boolean results.
3. When all children unload successfully, remove the shared client from `hass.data[DOMAIN]["entries"]` and close connections.

```python
async def _async_unload_parent_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the parent entry and every child."""

    subentries = list(entry.subentries.values())
    unload_results = await asyncio.gather(
        *(
            hass.config_entries.async_unload(subentry.entry_id)
            for subentry in subentries
        ),
        return_exceptions=True,
    )

    # ... continue with parent unload logic ...
```

### G. Subentry unload

1. Call `await hass.config_entries.async_unload_platforms(entry, PLATFORMS)`.
2. Delete `entry.runtime_data` (if present) after the platforms unload.

### H. Cascading removal

Home Assistant enforces cascade deletion: removing a parent entry triggers unload/remove for each child before finalizing the parent removal. Implement `async_remove_entry` only when external cleanups (for example, cloud webhooks) require it.

### I. Robustness (post-2025.10+ lifecycle changes)

- Register platform entity services from the integration-level `async_setup` via `service.async_register_platform_entity_service`. Avoid registering services from platform modules during `async_setup_entry`.
- For OAuth2-based integrations, wrap parent setup in a `try`/`except ImplementationUnavailableError` block and re-raise as `ConfigEntryNotReady`. This keeps network or provider outages from breaking the config entry permanently.

## Section V: Platform Files

When Home Assistant forwards a subentry to a platform (`sensor.py`, `conversation.py`, etc.), `config_entry` is the child entry.

### A. Platform `async_setup_entry`

Platform setup functions must consume the runtime data that `_async_setup_subentry` attached to the child entry:

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    handler = entry.runtime_data
    async_add_entities([ExampleEntity(handler, entry)])
```

### B. Device and entity linkage

- Set entity unique IDs relative to the subentry unique ID (for example, `f"{entry.unique_id}_weather"`).
- Use `DeviceInfo` identifiers that reference the subentry, not the parent, so the device registry associates the device with the correct config entry.

```python
self._attr_device_info = DeviceInfo(
    identifiers={(DOMAIN, entry.unique_id)},
    name=entry.title,
)
```

### C. `async_added_to_hass`

Home Assistant Core 2025.8+ invokes `Entity.async_added_to_hass` even for disabled entities. Guard every implementation:

```python
async def async_added_to_hass(self) -> None:
    if not self.enabled:
        return
    await super().async_added_to_hass()
    await self._subscribe_to_updates()
```

### D. First-run logic and registry migrations

`DeviceEntry.is_new` was removed in Core 2025.10. Store "first run" flags on the config entry (for example, inside `entry.data` or `entry.runtime_data`) instead of relying on device registry attributes.

## Section VI: Translations (`strings.json` and locale files)

### A. `config_subentries` key

Define subentry translations under the top-level `config_subentries` key. The first-level key must match the subentry type string returned by `async_get_supported_subentry_types`.

```json
{
  "config_subentries": {
    "location": {
      "title": "Example location",
      "step": {
        "user": {
          "title": "Add location",
          "description": "Configure a weather location using the Example API.",
          "data": {
            "location_name": "Location name"
          }
        },
        "reconfigure": {
          "title": "Update location",
          "description": "Change the location name.",
          "data": {
            "location_name": "Location name"
          }
        }
      },
      "error": {
        "invalid_location": "The provided location is invalid.",
        "unknown": "An unknown error occurred."
      },
      "abort": {
        "already_configured": "This location is already configured for this account."
      }
    }
  }
}
```

If the type string differs between code and translations (including casing or whitespace), the "Add" button will not appear.

### B. Existing translation checks

`tests/test_service_device_translation_alignment.py` loads every locale and asserts each defines the translation key referenced by `SERVICE_DEVICE_TRANSLATION_KEY`. Add similar regression tests when you introduce new subentry translation keys.

## Section VII: Discovery Patterns

### A. Parent discovery

Traditional discovery (Zeroconf, SSDP, DHCP) still creates parent entries through the main config flow.

### B. Manual subentry creation

Users can click "Add" on the parent entry card. Home Assistant invokes the registered `ConfigSubentryFlow` factory and runs `async_step_user` to create the child. MQTT now supports this flow for adding devices directly through the UI.

### C. Programmatic subentry discovery

Integrations can create subentries programmatically when runtime discovery finds new devices:

1. Detect the device (for example, MQTT discovery message, Zeroconf advertisement).
2. Locate the parent config entry and enumerate existing subentries via `list(parent_entry.subentries.values())` (or inspect the `parent_entry.subentries` mapping directly).
3. Create a new subentry if one does not already exist. Home Assistant automatically calls `async_setup_entry` for the new child.

## Section VIII: Peer-Review Checklist and Troubleshooting

### A. Checklist

Use this list during reviews:

- Parent flow implements `async_get_supported_subentry_types`.
- Factories return new `ConfigSubentryFlow` instances.
- Subentry flows call `self._get_entry()` and avoid deprecated helpers.
- `self.async_create_entry(...)` is used to persist children.
- Subentry unique IDs are scoped to the parent.
- Translations define `config_subentries` with keys matching the type strings.
- `async_setup_entry` routes parents versus children correctly.
- Parents store shared runtime data in `hass.data[DOMAIN]["entries"][entry.entry_id]`.
- Subentries raise `ConfigEntryNotReady` when `hass.data[DOMAIN]["entries"][parent_entry_id]` is unavailable.
- Subentries forward platform setup with `async_forward_entry_setups`.
- Parent unload waits for child unload success before cleaning shared clients.
- Platform entities pull runtime data from `entry.runtime_data` and bind devices to the subentry.

### B. Common errors

- **"Config flow could not be loaded: {"message": "Invalid handler specified"}"** — Usually caused by syntax errors or missing dependencies preventing Home Assistant from importing `config_flow.py`.
- **`AttributeError: module 'openai' has no attribute 'AsyncOpenAI'`** — Indicates dependency mismatch; fix the integration `requirements`.
- **Add button missing in UI** — Ensure `async_get_supported_subentry_types` returns the type, translations define `config_subentries`, and the keys match exactly.
- **`KeyError` when loading a subentry** — Parent data not yet available; make sure `_async_setup_subentry` raises `ConfigEntryNotReady` on missing parent clients.

### C. Advanced pitfalls

- Avoid synchronous I/O inside `async_setup_entry`; wrap blocking code with `hass.async_add_executor_job`.
- Always use `async_forward_entry_setups` rather than `async_setup_platforms`.
- Register Home Assistant services in `async_setup` instead of `async_setup_entry` so automations remain valid when entries reload.
- Use update listeners to reload children whenever parent options change, ensuring `hass.data[DOMAIN]["entries"]` is rebuilt before child setup resumes.

### D. Device and entity registry troubleshooting playbooks

Subentries succeed or fail based on how well they coordinate device and entity ownership. Use the following diagnostics when new
children fail to appear in the UI, refuse to unload, or leave orphaned registry entries:

1. **Creation: verify parent linkage before platform setup.**
   - Ensure `_async_setup_subentry` loads its shared coordinator from `hass.data[DOMAIN]["entries"][entry.parent_entry_id]` and
     assigns it to `entry.runtime_data` before calling `async_forward_entry_setups`. If the parent runtime data is unavailable,
     raise `ConfigEntryNotReady`; Home Assistant will retry after the parent rebuilds the shared mapping.
   - Confirm `entry.runtime_data` stores any per-subentry helpers that platforms need to bind the correct device identifiers.
    - Add regression tests that instantiate the flow, create the child entry, and assert the coordinator exposes
      `async_update_device_registry` or similar helpers. See `tests/test_coordinator_device_registry.py` for examples that validate
      `async_update_device` calls pair `add_config_subentry_id` with `remove_config_entry_id` to keep tracker and service devices linked
      solely through their subentries while stripping redundant hub associations.

2. **Entity linkage: confirm identifiers and config entry IDs.**
   - Entity factories must source identifiers from the subentry (`entry.unique_id`) and pass them through `DeviceInfo`. Avoid
     copying parent identifiers; devices tied to the parent will not reload when only the child changes.
   - Whenever registry helpers run (for example, coordinator refreshes or service-driven cleanups), log the `device_id`,
     `remove_config_entry_id`, and `add_config_subentry_id` parameters. During debugging, temporarily enable debug logging around
     `DeviceRegistry.async_update_device` to observe which entries receive updates.
   - In tests, simulate both Home Assistant 2025.7+ (`add_config_subentry_id`/`remove_config_entry_id`) and legacy keyword shapes
     to guarantee backward compatibility. The existing fakes in `tests/helpers/homeassistant.py` illustrate how to guard keyword
     names without suppressing mypy.

3. **Removal: cascade deletes in the correct order.**
   - Parent unload handlers must wait for every subentry to unload successfully before tearing down shared clients. Use
     `list(entry.subentries.values())` to enumerate children and `async_unload` each one before dropping the parent from
     `hass.data`.
   - Custom cleanup services (for example, registry rebuild tasks) should aggregate both subentry IDs and parent IDs when pruning
     legacy devices. Inspect `custom_components/googlefindmy/services.py` for the reference implementation that collects
     `managed_entry_ids` from both buckets before removing stale registry rows.
   - When a device or entity remains after the parent is removed, query `.storage/core.device_registry` and
     `.storage/core.entity_registry` for the lingering identifiers. A missing `config_entries` reference usually means one of the
     update calls omitted the child `entry_id`.

4. **Reconfigure flows: keep registries in sync.**
   - `async_step_reconfigure` must call `async_update_reload_and_abort` with merged data and rely on setup logic to rebuild runtime
     data. Afterwards, trigger `async_reload` on the child entry to refresh device registry metadata such as `name` or
     `configuration_url`.
   - Write regression tests that edit a subentry and assert that `DeviceRegistry.async_update_device` receives new labels while
     leaving the tracker-only linkage intact (no unexpected `add_config_entry_id` calls).

5. **Instrumenting debug logs:**
   - Wrap registry writes in helper functions that emit structured logs (entry ID, device ID, identifiers). This makes it easier to
     spot when a subentry accidentally targets the parent device or omits identifiers entirely.
   - During QA, enable the logger `custom_components.googlefindmy` at debug level to capture the full lifecycle of registry updates
     and confirm each handler runs in the expected order: parent setup → child setup → platform setup → device/entity registration.

## Section IX: Migration Case Studies

### A. `openai_conversation`

- Parent flow collects the API key and declares `{ "ai_agent": OpenAIAgentFlowFactory }`.
- Subentry flow prompts for prompt/model parameters and uses the parent API key for validation.
- Parent setup stores the `OpenAIClient` and loads subentries.
- Subentry setup retrieves the shared client and forwards to the `conversation` platform.
- Result: one API key, many AI agents, all manageable through subentries.

### B. `mqtt`

- Parent flow still collects broker details but now also exposes `{ "mqtt_device": MqttDeviceFlowFactory }`.
- Subentry flow lets users configure MQTT devices manually through the UI.
- Runtime discovery (for example, MQTT `homeassistant/.../config` topics) creates subentries programmatically when devices announce themselves.
- Both manual and automatic paths converge on subentries that load through `_async_setup_subentry` and forward to the appropriate platforms.

---

Keep this handbook synchronized with upstream Home Assistant releases. When new subentry features ship (for example, additional lifecycle hooks or translation keys), update this document and add links in `AGENTS.md` so every contributor can find the latest requirements quickly.

### Cross-reference checklist

* [`custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md`](../custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md) — Platform forwarding, unload fan-out, and the legacy tracker warning flow. Mirror changes between this handbook and that runtime guide so unsupported-core fallbacks stay documented in both places.
* [`custom_components/googlefindmy/agents/config_flow/AGENTS.md`](../custom_components/googlefindmy/agents/config_flow/AGENTS.md) — Config-flow registration, discovery updates, and service validation fallbacks. Add or update subentry-specific reminders in tandem with the handbook to keep the UI + runtime expectations aligned.
* [`custom_components/googlefindmy/agents/typing_guidance/AGENTS.md`](../custom_components/googlefindmy/agents/typing_guidance/AGENTS.md) — Strict-mypy import guards and iterator typing conventions referenced by the handbook’s registry sections. Revisit this list whenever new helpers land so every AGENT mentions the handbook (and vice versa).

---

## Postmortem: `ValueError` on Setup/Unload (Regression 1.6.0)

A regression was introduced that caused catastrophic loading failures (`ValueError: Config entry ... already been setup!`) and non-recoverable unload failures (`ConfigEntryState.FAILED_UNLOAD`).

### Root Cause Analysis

The core issue was a flawed implementation of platform forwarding for config subentries in `__init__.py`.

1. **Setup Failure:** The `_async_ensure_subentries_are_setup` function attempted to feature-detect the singular `async_forward_entry_setup` helper and fell back to the *plural* `async_forward_entry_setups` API whenever the attribute probe failed. When this fallback path executed (for example, on HA 2025.10.1), it repeated the original bug: the plural helper does not accept `config_subentry_id`, so `_invoke_with_optional_keyword` logged the rejection and re-ran the call without the ID. As a result, all platforms were registered on the **parent** entry, corrupting every child entry's registry state and eventually raising `ValueError: Config entry ... already been setup!` as soon as a second child reused the same platform.
2. **Unload Failure:** The historical `_unload_config_subentry` helper mirrored the same mistake. It passed a tuple of platforms to the singular unload helper, causing Home Assistant to complain that subentries had never been loaded. This poisoned the entry lifecycle and left the parent stuck in `FAILED_UNLOAD` after the unload attempt.

### The Solution

The only correct method to load or unload platforms for a child entry is to use the singular helpers (`async_forward_entry_setup` / `async_forward_entry_unload`) **one platform at a time** while supplying the child's `config_subentry_id`.

Fixing the regression required replacing the entire body of `_async_ensure_subentries_are_setup` so it always:

1. Resolves the singular helper (`hass.config_entries.async_forward_entry_setup`) and aborts with an error if it is missing.
2. Iterates each managed subentry, filters disabled entries, and determines the relevant platforms.
3. Loops over the platforms **per subentry** and calls the singular helper with `(entry, platform_name)` plus `config_subentry_id=<child_id>`.
4. Aggregates the awaitables via `asyncio.gather`, mirroring Home Assistant's parent-entry setup fan-out but scoped to the child entry identifiers.

The `_unload_config_subentry` helper already follows the same per-platform singular pattern (see the 1.6-beta3 bugfixes), so both setup and unload paths now share the same mental model: one helper invocation per platform, always tagged with the correct `config_subentry_id`.

For Home Assistant cores that still expose `async_forward_entry_setup` without a `config_subentry_id` parameter, the integration logs a single warning and records the affected platforms through the weakref-backed tracker described in [`custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md`](../custom_components/googlefindmy/agents/runtime_patterns/AGENTS.md). Refer back to that guidance whenever the legacy tracker surfaces in logs so the unsupported-core fallback stays documented alongside this postmortem.
