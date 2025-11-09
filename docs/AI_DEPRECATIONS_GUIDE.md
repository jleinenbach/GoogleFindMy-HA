# AI Deprecations: Technical Analysis and Migration Guide (Core 2025.10/2025.11)

## I. Introduction: Purpose and Scope

This document serves as a technical guide for developers of Home Assistant integrations, especially custom components. It analyzes the recent and upcoming breaking changes and API deprecations introduced with Core versions 2025.10 and 2025.11 or whose deadlines are approaching. The goal is to provide an in-depth analysis that goes beyond the official release notes. It documents the changes, the Home Assistant Core team's motivations, potential migration pitfalls, and recommended best practices to ensure compatibility and robustness. The analysis is based on developer blogs, Core changelogs, and a review of specific code changes and issue tickets.

Use this guide alongside the official release notes and Home Assistant developer documentation. Each section explains the technical background, shows legacy vs. migrated code, and closes with an explicit checklist hook so you can track the necessary work in Section V.

### How to use this guide

1. **Scan the quick-reference timeline** to prioritize work relative to the current Core release cadence.
2. **Jump to the relevant section** whenever you touch the affected API or behavior; the code snippets highlight safe migration patterns.
3. **Update the prioritized checklist in Section V** as you complete each migration so future audits have a single source of truth.

### Quick-reference timeline

| Deadline / status | Area | Breaking surface | Section |
| --- | --- | --- | --- |
| Released in 2025.10 | Device registry | `DeviceEntry.is_new` removed; refactor first-run logic | [IV.1](#1-device-registry-removal-of-is_new-from-deviceentry-core-202510) |
| Behavior change since 2025.8 | Entity lifecycle | `async_added_to_hass` runs for disabled entities; guard with `self.enabled` | [IV.2](#2-entity-lifecycle-changed-behavior-of-async_added_to_hass) |
| Deadline 2025.11 | Covers | Replace deprecated state constants, stop overriding `state` | [II.1](#1-coverentity-removal-of-deprecated-state-constants) |
| Deadline 2025.11 | Recorder statistics | Always pass `new_unit_class` to `async_update_statistics_metadata` | [II.2](#2-recorder-statistics-api-async_update_statistics_metadata) |
| Deadline 2025.11 | Core config typing | Import `Config` from `homeassistant.core_config` | [II.3](#3-core-configuration-deprecated-config-alias) |
| Enforcement 2025.12 | OAuth2 config flows | Catch `ImplementationUnavailableError`, raise `ConfigEntryNotReady` | [IV.4](#4-oauth2-error-handling-when-internet-connectivity-is-unavailable) |
| Deadline 2026.10 | Service helpers | Drop explicit `hass` argument from helper calls | [III.1](#1-service-helpers-deprecation-of-the-hass-argument-deadline-202610) |
| Active now | Temperature conversion | Use `TemperatureDeltaConverter` for deltas | [III.2](#2-temperature-conversion-temperatureconverterconvert_interval) |
| Released in 2025.10 | Entity services | Register platform services via `async_register_platform_entity_service` in `async_setup` | [IV.3](#3-registration-of-platform-entity-services-new-api-pattern) |
| Released in 2025.11 | API translations | `get_services` no longer returns action translations; fetch via `frontend/get_translations` | [IV.5](#5-api-endpoints-removal-of-service-translations-websocketrest) |

## II. Critical Deprecations with Deadline 2025.11

This section covers API changes that were marked as deprecated in Home Assistant Core 2025.11 (or slightly before) and whose compatibility shims will be removed. Code that is not migrated before this deadline will fail.

### 1. `CoverEntity`: Removal of Deprecated State Constants

The constants used in `CoverEntity` to return states have been deprecated since Core 2024.11. Support for these constants will be removed entirely in version 2025.11.

#### Problem Analysis

The problem has two facets. First, using imported constants (for example, `STATE_OPEN`) or hard-coded strings (for example, `"open"`) to define an entity state is deprecated. These have been replaced by the `CoverState` enum (for example, `CoverState.OPEN`) to improve type safety.

Second, and more importantly, the `CoverEntity` documentation generally advises against overriding the `state` property directly. The correct implementation is to provide the boolean properties `is_closed`, `is_opening`, and `is_closing`. The `CoverEntity` base class automatically derives the correct `CoverState` enum value (for example, `CoverState.OPENING`) from these properties.

#### Faulty Code (Deprecated)

```python
# DEPRECATED: Checking the state with an imported constant
from homeassistant.const import STATE_OPEN

if cover_entity.state == STATE_OPEN:
    # ... logic ...
    pass

# DEPRECATED: Direct string comparison
if cover_entity.state == "open":
    # ... logic ...
    pass
```

#### Correct Migration Path (New Code)

For code that checks the state, use the `CoverState` enum. For code that sets the state (that is, the entity implementation), migrate the logic to rely on the boolean properties instead.

```python
# CORRECT: Using the CoverState enum for checks
from homeassistant.components.cover import CoverEntity, CoverState

if cover_entity.state == CoverState.OPEN:
    # ... logic ...
    pass

# BETTER: Use the dedicated properties when available
if cover_entity.is_open:
    # ... logic ...
    pass
```

Developers who manually override `state` in their `CoverEntity` classes must refactor their code to implement the boolean properties instead. Migration requires refactoring the state logic—it is not a simple search-and-replace operation.

**Checklist tie-in:** Section V — High priority (CoverEntity constants and state implementation).

### 2. Recorder Statistics API: `async_update_statistics_metadata`

The Python function `recorder.async_update_statistics_metadata` updates metadata for long-term statistics. Omitting the `new_unit_class` argument is deprecated as of Core 2025.11 and will result in an error.

#### Problem Analysis

This change is part of a broader overhaul of the statistics API that also introduces `mean_type` (instead of `has_mean`) and `unit_class` for the WebSocket API. The `new_unit_class` argument is essential for correct unit conversion within the statistics system (for example, the energy dashboard).

Interestingly, the deadline for this specific Python function (2025.11) is much shorter than the deadline for the WebSocket API changes (2026.11). This indicates that the Core team prioritizes the data integrity of incoming statistics data (provided by integrations via the Python API) over external tools (which use the WebSocket API).

#### Faulty Example (Legacy Code)

```python
# DEPRECATED: Call without new_unit_class
await hass.components.recorder.async_update_statistics_metadata(
    statistic_id,
    new_unit_of_measurement="kWh"
)
```

#### Correct Migration Path (New Code)

Developers must explicitly supply `new_unit_class`, even if it is `None`.

```python
# CORRECT: Explicitly specify new_unit_class
# (for example, if the unit changes but there is no specific conversion class)
await hass.components.recorder.async_update_statistics_metadata(
    statistic_id,
    new_unit_of_measurement="kWh",
    new_unit_class=None  # Must be provided explicitly
)
```

**Checklist tie-in:** Section V — Medium priority (`async_update_statistics_metadata`).

### 3. Core Configuration: Deprecated `Config` Alias

A "silent" deprecation—found not in developer blogs but in GitHub issue logs related to the Frigate integration—is the deprecation of a type alias for `Config`.

#### Problem Analysis

A log warning from Home Assistant 2024.11.0 states: `Config was used from frigate, this is a deprecated alias which will be removed in HA Core 2025.11. Use homeassistant.core_config.Config instead....`

This is a classic example of a deprecation discovered only through careful review of Home Assistant logs when testing against a beta or development version. It underscores that developer blogs are necessary but not always sufficient.

#### Faulty Example (Legacy Code)

```python
# DEPRECATED: Import from homeassistant.core
from homeassistant.core import Config

def my_function(config: Config):
    # ...
    pass
```

#### Correct Migration Path (New Code)

Update the type hint (or import) to use `homeassistant.core_config.Config`.

```python
# CORRECT: Import from homeassistant.core_config
from homeassistant.core_config import Config

def my_function(config: Config):
    # ...
    pass
```

**Checklist tie-in:** Section V — Medium priority (Config alias migration).

## III. Ongoing API Deprecations and Recommended Migrations (Longer Deadlines)

This section covers changes that are deprecated but have longer transition periods. Although the code does not fail yet, these migrations should be performed now to guarantee future compatibility.

### 1. Service Helpers: Deprecation of the `hass` Argument (Deadline: 2026.10)

Passing the `hass` object (Home Assistant Core instance) as the first argument to various service helper functions is now deprecated. Affected helpers include:

* `verify_domain_control`
* `extract_entity_ids`
* `async_extract_entities`
* `async_extract_entity_ids`
* `async_extract_config_entry_ids`

#### Problem Analysis

Since Home Assistant 2025.1, the `hass` object is directly accessible as a property (`.hass`) of the `ServiceCall` object (`call`). Passing it separately is therefore redundant.

The extremely long transition period until 2026.10 signals that this API is widely used. The Core team is aware that thousands of custom components rely on it and wants to ensure a smooth migration. For developers, this is a lower-urgency refactor—but still important.

#### Faulty Example (Legacy Code)

```python
# DEPRECATED: Passing 'hass' explicitly to the helper
async def async_my_service_handler(self, call: ServiceCall):
    # ...
    entities = await async_extract_entities(self.hass, call, "domain_to_extract")
    # ...
```

#### Correct Migration Path (New Code)

Remove the `hass` argument from the helper call. The helper now accesses `call.hass` internally.

```python
# CORRECT: The helper uses 'call.hass' internally
async def async_my_service_handler(self, call: ServiceCall):
    # ...
    entities = await async_extract_entities(call, "domain_to_extract")
    # ...
```

**Checklist tie-in:** Section V — Low priority (service helper refactor).

### 2. Temperature Conversion: `TemperatureConverter.convert_interval`

The `convert_interval` method in `TemperatureConverter` is deprecated.

#### Problem Analysis

The rationale is the semantic ambiguity between a point temperature (for example, 20°C) and a temperature delta (for example, a rise of 5°C). The conversion formulas differ:

* Delta: \( F = C \times 1.8 \)
* Point: \( F = C \times 1.8 + 32 \)

`convert_interval` was designed for deltas, but the name lacked clarity and could lead to mistakes. For converting temperature deltas (intervals), developers should now use the semantically clear `TemperatureDeltaConverter` class and its `.convert()` method. This is more than a deprecation; it fixes a potential source of errors.

#### Faulty Example (Legacy Code)

```python
# DEPRECATED: Using TemperatureConverter for a delta
from homeassistant.util.temperature import TemperatureConverter

# Convert a 5-degree Celsius interval to Fahrenheit
delta_f = TemperatureConverter.convert_interval(5.0, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT)
# delta_f is 9.0
```

#### Correct Migration Path (New Code)

```python
# CORRECT: Use the new, semantically clear converter
from homeassistant.util.temperature import TemperatureDeltaConverter

# Convert a 5-degree Celsius delta to Fahrenheit
delta_f = TemperatureDeltaConverter.convert(5.0, UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT)
# delta_f is 9.0
```

**Checklist tie-in:** Section V — Low priority (`TemperatureConverter.convert_interval`).

## IV. Analysis of Breaking Changes and Behavioral Shifts (Core 2025.10 & 2025.11)

This section covers changes without long deprecation warnings. These are direct removals of APIs (breaking changes) or subtle behavioral shifts in Core that can cause immediate and hard-to-diagnose issues.

### 1. Device Registry: Removal of `is_new` from `DeviceEntry` (Core 2025.10)

The Core 2025.10 changelog explicitly states: "Remove is_new from device entry."

#### Impact (Breaking Change)

This is not a deprecation but an immediate removal. Any code that accesses `device_entry.is_new` has been failing with an `AttributeError` since 2025.10.

#### Affected Code

```python
# BROKEN SINCE 2025.10
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # ...
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        # ...
    )

    if device_entry.is_new:  # <-- NOW RAISES AttributeError
        # Run logic only on the first add
        await async_perform_first_time_setup(device_entry)

    # ...
```

#### Recommended Solution

The `is_new` flag was a fragile way to implement "first run" logic. Its removal pushes developers toward more robust patterns. Tie "first run" logic to the `ConfigEntry` rather than the `DeviceEntry`. When `async_setup_entry` runs, that is the starting point for the device. If logic truly must run only once, store a flag inside the config entry itself (for example, `entry.data["first_run_complete"] = True`).

**Checklist tie-in:** Section V — High priority (`is_new` removal).

### 2. Entity Lifecycle: Changed Behavior of `async_added_to_hass`

A GitHub issue highlights a critical, unannounced change since Core 2025.8: the `async_added_to_hass` hook now runs for disabled entities.

#### Problem Analysis

Previously, this hook apparently executed only for entities actively added to the state machine. A developer used this hook to log deprecation warnings for their integration, which resulted in users seeing warnings for entities they had intentionally disabled. The issue was closed as "not planned," meaning it is intentional behavior.

#### Impact (Breaking Change)

Any logic inside `async_added_to_hass` (for example, starting update listeners, subscribing to webhooks, sending "I am online" messages to a device) now runs for disabled entities too. This can cause unnecessary system load, API calls for disabled devices, and confusing log entries.

#### Recommended Solution (Mandatory)

Every `async_added_to_hass` implementation must now begin with a check to ensure the entity is enabled.

```python
# CORRECT, ROBUST IMPLEMENTATION
async def async_added_to_hass(self) -> None:
    """Handle entity which will be added."""

    # CRITICAL CHECK: Prevent execution for disabled entities
    if not self.enabled:
        return

    # Run the remaining logic only for ENABLED entities
    await super().async_added_to_hass()
    await self._subscribe_to_updates()
    # ...
```

**Checklist tie-in:** Section V — High priority (`async_added_to_hass` guard).

### 3. Registration of Platform Entity Services: New API Pattern

The process for registering platform-specific entity services (for example, `light.my_custom_service`) has changed.

#### Problem Analysis

Service registration used to happen within the platform setup function (for example, `async_setup_platform` or `async_setup_entry` for a platform such as `light`). This caused decoupling issues: if the platform was never loaded (for example, because no device was configured yet), the service was never registered.

#### Faulty Example (Legacy Code)

```python
# DEPRECATED (for example, in light.py)
async def async_setup_entry(hass, config_entry, async_add_entities):
    # ... set up entities ...

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(  # <-- DEPRECATED
        "my_custom_service",
        # ...
    )
```

#### Correct Migration Path (New Code)

Move service registration out of the platform setup and into the integration's `async_setup` function (in `__init__.py`). This ensures the service is available as soon as the integration loads, regardless of whether the platform has entities yet.

```python
# CORRECT (for example, in __init__.py)
from homeassistant.helpers import service

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    # ...

    await service.async_register_platform_entity_service(
        hass,
        "light",  # Platform domain that the service targets
        "my_custom_service",
        # ...
    )
    return True
```

**Checklist tie-in:** Section V — Low priority (entity service registration).

### 4. OAuth2 Error Handling When Internet Connectivity Is Unavailable

Integrations that use OAuth2 for configuration often failed when Home Assistant started without an internet connection.

#### Problem Analysis

Previously, `config_entry_oauth2_flow.async_get_config_entry_implementation` raised a `ValueError` when no connection was available. This non-retryable exception caused the config entry to enter a permanent error state, requiring manual reloads.

#### Correct Migration Path (New Code)

The flow now raises a specific `ImplementationUnavailableError`. Catch this exception and raise `ConfigEntryNotReady` instead. `ConfigEntryNotReady` tells Home Assistant that the issue is temporary and that it should retry later. This behavior becomes mandatory in Core 2025.12.

```python
# CORRECT: Implementation inside async_setup_entry
from homeassistant.config_entries import ConfigEntryNotReady
from homeassistant.helpers.config_entry_oauth2_flow import ImplementationUnavailableError

# ... inside async_setup_entry ...
try:
    implementation = await async_get_config_entry_implementation(hass, entry)
except ImplementationUnavailableError as err:
    raise ConfigEntryNotReady(
        "OAuth2 implementation temporarily unavailable, will retry"
    ) from err
```

This change is essential for reliable cloud integrations.

**Checklist tie-in:** Section V — Medium priority (OAuth2 resiliency).

### 5. API Endpoints: Removal of Service Translations (WebSocket/REST)

A developer blog post dated 24 October 2025 announced that "action translations" defined in `strings.json` would no longer appear in the responses of the WebSocket commands `get_services` or the REST endpoint `/api/services`.

#### Problem Analysis

The rationale is that these translations were incomplete and unused by the Home Assistant frontend itself. If a developer or third-party tool (for example, a custom dashboard card, Node-RED) relied on these translated fields, that functionality is now broken.

#### Recommended Solution

Fetch the official, complete translations separately via the WebSocket command `frontend/get_translations` whenever necessary.

**Checklist tie-in:** Section V — Low priority (service translations removal).

## V. Prioritized Migration Checklist and Next Steps

This section summarizes the findings in a prioritized checklist.

### Checklist (High Priority – Immediate Fixes)

- [ ] `is_new`: Search the entire codebase for `.is_new`, especially in conjunction with `DeviceEntry`. **Action:** Remove all usages. Refactor any "first run" logic to bind to the `ConfigEntry` lifecycle or `entry.data` instead (see Section IV.1).
- [ ] `async_added_to_hass`: Review every implementation of `async_added_to_hass`. **Action:** Add `if not self.enabled: return` as the first line unless the logic must also run for disabled entities (see Section IV.2).
- [ ] `CoverEntity` (constants): Search all cover-related files for `STATE_OPEN`, `STATE_CLOSED`, `"open"`, `"closed"`. **Action:** Replace all state checks with the `CoverState` enum (for example, `self.state == CoverState.OPEN`) (see Section II.1).
- [ ] `CoverEntity` (implementation): Check whether your `CoverEntity` classes override the `state` property. **Action:** Refactor the code to implement `is_closed`, `is_opening`, and `is_closing` instead. Remove manual overrides of `state` (see Section II.1).

### Checklist (Medium Priority – Required for Future Releases)

- [ ] OAuth2 error handling: If you use OAuth2, review your `async_setup_entry` function. **Action:** Implement the `try ... except ImplementationUnavailableError ... raise ConfigEntryNotReady` pattern (deadline: 2025.12) (see Section IV.4).
- [ ] `async_update_statistics_metadata`: If you provide statistics, locate calls to `async_update_statistics_metadata`. **Action:** Ensure that the `new_unit_class` argument is always supplied explicitly (for example, `new_unit_class=None`) (see Section II.2).
- [ ] `Config` alias: Search for imports of `Config` from `homeassistant.core`. **Action:** Change the import path to `from homeassistant.core_config import Config` (see Section II.3).

### Checklist (Low Priority – Good Code Hygiene)

- [ ] Service helpers (`hass` argument): Search for calls to `async_extract_entity_ids`, `async_extract_entities`, etc. **Action:** Remove the `hass` argument from calls (for example, `async_extract_entity_ids(self.hass, call)` becomes `async_extract_entity_ids(call)`) (deadline: 2026.10) (see Section III.1).
- [ ] Entity services: Review where you call `platform.async_register_entity_service`. **Action:** Move these calls out of platform setup files (for example, `light.py`) into the integration's `async_setup` function in `__init__.py` and use `service.async_register_platform_entity_service` (see Section IV.3).
- [ ] `TemperatureConverter.convert_interval`: Search for `convert_interval`. **Action:** Replace all calls with `TemperatureDeltaConverter.convert` and verify that the logic distinguishes between deltas and point temperatures (see Section III.2).
- [ ] API translations: If you have a custom frontend component that parses `/api/services`, **Action:** Confirm whether you rely on translated fields. If so, refactor to request translations via `frontend/get_translations` instead (see Section IV.5).

### Final Recommendation

The analysis (especially Sections II.3 and IV.2) shows that developer blogs do not capture every change and that behavioral shifts can occur without advance notice. The only reliable way to ensure compatibility is to create a test workflow that runs your integration against the beta or development versions of Home Assistant Core while actively monitoring system logs for new deprecation warnings and errors.
