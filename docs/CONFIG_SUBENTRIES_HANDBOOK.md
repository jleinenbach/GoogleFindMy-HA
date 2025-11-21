# Home Assistant Config Subentry Handbook (Corrected for Core 2025.11)

This handbook captures the validated Home Assistant 2025.11+ contract for configuration subentries. It corrects previous misconceptions regarding the object model and lifecycle management. Treat this as the authoritative reference for implementing subentries correctly.

## Quick summary

  - **Object Model:** A `ConfigSubentry` is a **dataclass**, not a full `ConfigEntry`. It does **not** have a global `entry_id`, a `state`, or a `runtime_data` attribute. It exists strictly as data within `entry.subentries`.
- **Lifecycle = Parent-Managed Forwarding:** You cannot "setup" or "unload" a subentry individually via `hass.config_entries`. Instead, the parent entry explicitly forwards platform setup for each subentry (using `_async_setup_new_subentries`) so Home Assistant can invoke `async_setup_entry` with the correct `config_subentry_id` context when supported.
  - **Identifier Handling:** Always use `subentry.subentry_id`. The attribute `entry_id` does not exist on the `ConfigSubentry` object.
  - **Platform Forwarding:** The parent entry forwards platforms once. Platforms must iterate over the subentry data provided in `entry.runtime_data` and create entities/devices linked to the specific subentry.
  - **Automatic Cleanup:** Using `hass.config_entries.async_remove_subentry` automatically invokes `async_clear_config_subentry` in the Device and Entity registries. No manual cleanup is required if devices are registered correctly.

## Section 0 supplement: The Reality of Core 2025.11

### A. Configuration vs. Data Model

In Core 2025.11, the `ConfigEntry` class contains a dictionary of subentries:

```python
# From config_entries.py
@dataclass(frozen=True, kw_only=True)
class ConfigSubentry:
    data: MappingProxyType[str, Any]
    subentry_id: str = field(default_factory=ulid_util.ulid_now)
    subentry_type: str
    title: str
    unique_id: str | None
    # Note: NO entry_id, NO state, NO runtime_data
```

Accessing `subentry.entry_id` will raise an `AttributeError` because it does not exist. The `ConfigSubentry` is purely configuration storage.

### B. The "Setup" Misconception

Previous guides incorrectly stated that `hass.config_entries.async_setup(subentry_id)` should be called. **This is impossible** and will raise `UnknownEntry` because `async_setup` expects a global `ConfigEntry` ID (the parent's ID).

**Correct Pattern:**

1.  **Add/Update:** Call `hass.config_entries.async_add_subentry` or `async_update_subentry`.
2.  **Forward:** Explicitly trigger platform setup for the new subentries from the parent entry via `_async_setup_new_subentries(hass, parent_entry, [subentry])`. The helper forwards `async_forward_entry_setups` from the parent and passes `config_subentry_id` when the installed Home Assistant core accepts the keyword.
3.  **Execution:** Platforms receive `config_subentry_id` (when supported) during their normal `async_setup_entry` fan-out and must attach entities/devices to the forwarded subentry.

### C. Runtime Data Strategy

Since subentries are not `ConfigEntry` objects, they cannot hold `runtime_data`.
**Strategy:** The Parent Entry's `runtime_data` must hold a container (e.g., a dictionary) mapping `subentry_id` to specific runtime objects (Coordinators, API Clients).

## Section I: Architectural Mandate

### A. The Hierarchy

```
ConfigEntry (Parent) - ID: "a1b2..."
  └─ .subentries (Dict)
       └─ "x9y8..." -> ConfigSubentry (Data only)
```

### B. Terminology Refined

| Term | Definition |
| ---- | ---------- |
| `ConfigEntry` (Parent) | The actual integration instance. Has `state`, `entry_id`, and `runtime_data`. |
| `ConfigSubentry` (Child) | A lightweight dataclass inside the parent. Has `subentry_id` and `data`. **Not** a generic `ConfigEntry`. |
| `subentry_id` | The ULID identifying the subentry. Generated automatically via `ulid_now`. |

## Section II: Config Flow Implementation

### A. Parent Flow Support

The parent `ConfigFlow` declares support via `async_get_supported_subentry_types`.

```python
    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: config_entries.ConfigEntry,
    ) -> dict[str, config_entries.ConfigSubentryFlowFactory]:
        return {"location": lambda: LocationSubentryFlowHandler()}
```

### B. Subentry Flow Handler

Use `ConfigSubentryFlow`. The `async_create_entry` method returns a `SubentryFlowResult`.

```python
class LocationSubentryFlowHandler(config_entries.ConfigSubentryFlow):
    async def async_step_user(self, user_input=None):
        # self._get_entry() returns the PARENT ConfigEntry
        parent = self._get_entry()
        
        if user_input:
            return self.async_create_entry(
                title=user_input["name"],
                data=user_input,
                unique_id=f"{parent.entry_id}_{user_input['name']}" # Scope to parent
            )
        return self.async_show_form(...)
```

**Note:** When `async_create_entry` is called, the `ConfigSubentryFlowManager` in Core automatically calls `hass.config_entries.async_add_subentry`. The integration must then forward platform setup for the new subentry (and may still rely on parent reload listeners for other state changes).

## Section III: Lifecycle Management (`__init__.py`)

### A. Parent Setup (`async_setup_entry`)

This is the single entry point.

```python
@dataclass
class RuntimeData:
    client: APIClient
    subentry_coordinators: dict[str, DataUpdateCoordinator]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # 1. Setup Shared Client
    client = APIClient(entry.data["api_key"])
    
    # 2. Setup Subentries
    coordinators = {}
    for subentry in entry.subentries.values():
        # Initialize coordinator/logic for this subentry
        coordinator = DataUpdateCoordinator(..., name=subentry.title)
        # Initial refresh (optional, can be done in background)
        await coordinator.async_config_entry_first_refresh()
        coordinators[subentry.subentry_id] = coordinator

    # 3. Store Runtime Data
    entry.runtime_data = RuntimeData(client=client, subentry_coordinators=coordinators)

    # 4. Forward Platforms (per subentry)
    await _async_setup_new_subentries(hass, entry, entry.subentries.values())
    
    # 5. Listen for updates (to trigger reloads on subentry changes when configured)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    
    return True

async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
```

### B. Parent Unload (`async_unload_entry`)

```python
async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Unload platforms
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
```

**Correction:** There is no need to manually unload subentries because they are not loaded entries in `hass.config_entries`. Cleaning up the `entry.runtime_data` (garbage collection) is usually sufficient.

### C. Programmatic Subentry Management

If your integration discovers a new device and needs to add a subentry:

```python
async def _async_add_discovered_subentry(hass, parent_entry, discovery_data):
    # Check if exists
    for sub in parent_entry.subentries.values():
        if sub.unique_id == discovery_data.unique_id:
            return

    # Create Subentry Object
    subentry = config_entries.ConfigSubentry(
        data=MappingProxyType(discovery_data.config),
        subentry_type="location",
        title=discovery_data.name,
        unique_id=discovery_data.unique_id,
        # subentry_id is auto-generated if omitted, but you can generate one if needed
    )

    # Add to Core, then forward platform setup for the new subentry
    hass.config_entries.async_add_subentry(parent_entry, subentry)
    await _async_setup_new_subentries(hass, parent_entry, [subentry])
```

**Crucial:** Do **not** call `async_setup` manually. Forward platform setup via `_async_setup_new_subentries` so Home Assistant fans out the parent entry with the correct subentry context.

## Section IV: Platform Implementation & Entity Registry

This is where the "magic" of linking entities to subentries happens.

### A. Entity Creation

In `sensor.py` (or other platforms):

```python
async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime_data: RuntimeData = entry.runtime_data
    entities = []

    for subentry_id, coordinator in runtime_data.subentry_coordinators.items():
        # Retrieve the config object for metadata
        subentry = entry.subentries[subentry_id]
        
        entities.append(
            MySubentryEntity(
                coordinator=coordinator,
                subentry=subentry,
                parent_entry=entry
            )
        )

    # Pass entities to HA. 
    # Note: HA 2025.11 allows passing `config_subentry_id` if supported by the entity platform wrapper,
    # BUT usually, the link is established via DeviceInfo or the Entity Registry helper.
    async_add_entities(entities)
```

### B. Linking to the Registry (The Critical Part)

To ensure `async_remove_subentry` cleans up devices, the Device Registry entry must be associated with the `config_subentry_id`.

**Option 1: Via `async_add_entities` (If supported by EntityComponent)**
If the `AddEntitiesCallback` signature supports `config_subentry_id`, call `async_add_entities(entities, config_subentry_id=subentry.subentry_id)`.

**Option 2: Via Entity Properties (Standard)**
Core 2025.11's `config_entries.py` confirms that `device_registry` supports `async_clear_config_subentry`. To ensure the device is linked:

```python
class MySubentryEntity(CoordinatorEntity):
    def __init__(self, coordinator, subentry, parent_entry):
        super().__init__(coordinator)
        self._subentry = subentry
        self._parent = parent_entry
        self._attr_unique_id = f"{subentry.unique_id}_sensor"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._subentry.unique_id)},
            name=self._subentry.title,
            # Crucial: Explicitly link this device to the subentry
            # Note: Check strict typing for DeviceInfo in 2025.11. 
            # If `config_subentry_id` is not a valid DeviceInfo argument, 
            # the link is established via the EntityRegistry entry which points to the device.
        )
```

**Registry Cleanup Logic (Internal Core Behavior):**
When `hass.config_entries.async_remove_subentry(entry, subentry_id)` is called:

1.  It removes the data from `entry.subentries`.
2.  It calls `dev_reg.async_clear_config_subentry(entry.entry_id, subentry_id)`.
3.  It calls `ent_reg.async_clear_config_subentry(entry.entry_id, subentry_id)`.

**Therefore:** You must ensure your entities are registered with the `subentry_id` context. If `async_add_entities` does not accept `config_subentry_id` in your specific version context, you may need to manually update the registry in `async_setup_entry` for the platform:

```python
# Advanced / Manual linking if auto-link fails
ent_reg = er.async_get(hass)
# ... register entity ...
# ensure entity_entry.config_subentry_id is set.
```

*However, standard practice in 2025.11 is that `async_add_entities` handles this if the platform setup was triggered correctly or if you group entities by subentry.*

## Section V: Peer-Review Checklist

  - [ ] **No `hass.config_entries.async_setup(subentry_id)` calls.**
  - [ ] **No usage of `subentry.entry_id`** (it does not exist).
  - [ ] **Parent Forwarding Strategy:** Adding/removing a subentry relies on `_async_setup_new_subentries` to forward platforms for each child while keeping the parent entry as the setup target.
  - [ ] **Runtime Data:** `entry.runtime_data` is attached to the Parent `ConfigEntry` and contains a collection of subentry logic.
  - [ ] **Registry Safety:** Verify that removing a subentry via the UI removes the associated devices. If not, check that entities are being added with the correct context.

## Section VI: Troubleshooting `ValueError` & Regressions

**Symptom:** `ValueError: Config entry ... already been setup!`
**Cause:** Calling `async_forward_entry_setups` multiple times for the same parent entry, or trying to setup the parent recursively.
**Fix:** Call `async_forward_entry_setups` exactly **once** in the parent's `async_setup_entry`. Do not call it inside a loop for subentries.

**Symptom:** Subentry devices not deleting.
**Cause:** The devices are linked only to the Parent `entry_id`, not the `subentry_id`.
**Fix:** Ensure unique identifiers for devices are derived from the subentry `unique_id`, and verify that the entity registration process includes the subentry context.

-----

**Note on `config_entries.py` analysis:** The source code confirms `ConfigSubentry` is a passive data object. All active logic resides in `ConfigEntry` (Parent) and `ConfigEntries` (Manager). Adjust your mental model: **Subentries are just configuration data; the Parent Entry is the only running application logic.**
