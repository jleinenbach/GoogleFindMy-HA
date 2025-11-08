# Home Assistant Config Subentry Handbook (Home Assistant 2025.7+)

This handbook captures the Home Assistant 2025.7+ contract for configuration subentries. It expands on the architectural reasons, runtime data model, config flow mechanics, lifecycle management, translation rules, discovery patterns, and peer-review checklist required for error-free implementations. Treat it as the authoritative in-repo reference when working on Google Find My Device's subentry support.

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

### A. Routing setup

```python
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if entry.parent_entry_id:
        return await _async_setup_subentry(hass, entry)
    return await _async_setup_parent_entry(hass, entry)
```

### B. Parent setup

1. Create the shared client (for example, API session, MQTT connection). Raise `ConfigEntryNotReady` if the connection cannot be established.
2. Store the client in `hass.data[DOMAIN][entry.entry_id]` so children can retrieve it.
3. Enumerate children via `list(entry.subentries.values())` and `async_setup` each child. This ensures subentries created while the parent was unloaded are loaded on restart. Allow setup failures to propagate (do **not** pass `return_exceptions=True` to `asyncio.gather`) so Home Assistant correctly reflects parent/subentry health.
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

### C. Subentry setup

1. Read `parent_entry_id = entry.parent_entry_id`.
2. Retrieve the shared client from `hass.data[parent_entry_id]`. If the key is missing, raise `ConfigEntryNotReady` to retry later.
3. Attach any subentry-specific runtime data (for example, `entry.runtime_data = SubentryHandler(client, entry.data)`).
4. Forward setup to the platforms with `await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)`.

Update listeners must also operate exclusively on `entry.subentries`:

```python
async def _parent_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options updates on the parent entry."""

    subentries = list(entry.subentries.values())
    await asyncio.gather(
        hass.config_entries.async_reload(entry.entry_id),
        *(
            hass.config_entries.async_reload(subentry.entry_id)
            for subentry in subentries
        ),
    )
```

### D. Parent unload

1. Fetch all subentries via `list(entry.subentries.values())`.
2. `async_unload` every child and aggregate the boolean results.
3. When all children unload successfully, remove the shared client from `hass.data` and close connections.

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

### E. Subentry unload

1. Call `await hass.config_entries.async_unload_platforms(entry, PLATFORMS)`.
2. Delete `entry.runtime_data` (if present) after the platforms unload.

### F. Cascading removal

Home Assistant enforces cascade deletion: removing a parent entry triggers unload/remove for each child before finalizing the parent removal. Implement `async_remove_entry` only when external cleanups (for example, cloud webhooks) require it.

## Section V: Platform Files

When Home Assistant forwards a subentry to a platform (`sensor.py`, `conversation.py`, etc.), `config_entry` is the child entry.

- Retrieve per-subentry state from `entry.runtime_data` populated during `_async_setup_subentry`.
- Set entity unique IDs relative to the subentry unique ID (for example, `f"{entry.unique_id}_weather"`).
- Use `DeviceInfo` identifiers that reference the subentry, not the parent, so the device registry associates the device with the correct config entry.

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
- Parents store shared clients in `hass.data[entry.entry_id]`.
- Subentries raise `ConfigEntryNotReady` when the parent client is unavailable.
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
- Use update listeners to reload children whenever parent options change, preventing stale `hass.data` clients.

### D. Device and entity registry troubleshooting playbooks

Subentries succeed or fail based on how well they coordinate device and entity ownership. Use the following diagnostics when new
children fail to appear in the UI, refuse to unload, or leave orphaned registry entries:

1. **Creation: verify parent linkage before platform setup.**
   - Ensure `_async_setup_subentry` loads its shared coordinator from `hass.data[entry.parent_entry_id]` before calling
     `async_forward_entry_setups`. If the parent client is unavailable, raise `ConfigEntryNotReady`; Home Assistant will retry
     after the parent finishes restoring its runtime state.
   - Confirm `entry.runtime_data` stores any per-subentry helpers that platforms need to bind the correct device identifiers.
   - Add regression tests that instantiate the flow, create the child entry, and assert the coordinator exposes
     `async_update_device_registry` or similar helpers. See `tests/test_coordinator_device_registry.py` for examples that validate
     `async_update_device` calls include `add_config_entry_id` so the device registry links back to the subentry owner.

2. **Entity linkage: confirm identifiers and config entry IDs.**
   - Entity factories must source identifiers from the subentry (`entry.unique_id`) and pass them through `DeviceInfo`. Avoid
     copying parent identifiers; devices tied to the parent will not reload when only the child changes.
   - Whenever registry helpers run (for example, coordinator refreshes or service-driven cleanups), log the `device_id` and
     `config_entry_id` parameters. During debugging, temporarily enable debug logging around `DeviceRegistry.async_update_device`
     to observe which entries receive updates.
   - In tests, simulate both Home Assistant 2025.7+ (`add_config_entry_id`) and legacy keyword shapes to guarantee backward
     compatibility. The existing fakes in `tests/helpers/homeassistant.py` illustrate how to guard keyword names without suppressing
     mypy.

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
     preserving the `add_config_entry_id` parameter.

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
