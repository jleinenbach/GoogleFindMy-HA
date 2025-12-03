# 🛠️ MODULE: CONTINUOUS QUALITY ASSURANCE (Continuous QA)

Standing assignment for the AI: apply these checks alongside the Platinum rules whenever repository changes are made.

## Core checks to enforce proactively

### 1. Manifest & Metadata Hygiene
- **Check:** For each release, confirm that the `requirements` in `manifest.json` exactly match the versions in `requirements.txt`.
- **Check:** Ensure `integration_type` is explicitly set in `manifest.json` (for example: `hub`, `service`, or `device`) because the default (`hub`) will be deprecated.
- **Check:** Validate that `iot_class` is set correctly (for example: `cloud_push`, `local_polling`) and reflects the actual behavior in the code.

### 2. HACS & Community Standards
- **Check:** Confirm there is a valid `hacs.json`. Verify that `render_readme` is enabled so users get a proper preview in the HACS store.
- **Check:** Make sure brand assets (icons/logos) exist and meet the required dimensions for dark mode support, including transparent backgrounds for PNG files.

### 3. Translation Discipline (Completeness)
- **Strike:** Search Python code for hardcoded strings in `async_create_entry(title=...)` or `abort(reason=...)`.
- **Directive:** Every user-facing string must use a `translation_key`.
- **Extended Check:** For every new key in `strings.json`, ensure matching keys exist in `translations/en.json` (and ideally `translations/de.json`).

### 4. Performance & Blocking I/O Guard
- **Directive:** Scan new code for synchronous file operations (`open()`, `json.load()`, `shutil`) or network calls (`requests`, `urllib`) inside `async def` functions.
- **Remediation:** Immediately propose wrapping such operations with `await hass.async_add_executor_job(...)` when you find blocking calls. This is a common source of instability in Home Assistant.

### 5. Service Integrity & Argument Validation
- **Check (Argument usage):** Compare `services.yaml` with the handler in `services.py`.
  - *Strike:* If a field is defined in YAML (for example, `mode`, `device_ids`), it **must** be read via `call.data.get(...)` and acted on in the Python handler. Remove or implement unused arguments.
- **Check (Logic matching):** Ensure the `description` in YAML does not promise behaviors (for example, "Migrates data") that the Python code omits (for example, a bare `async_reload`).
- **Check (Examples):** Make sure complex selectors in `services.yaml` (for example, lists of IDs) include valid `example` values.

### 6. Defensive Programming & Registry Hygiene
- **Check (Repair logic):** For repair services (`rebuild_*`), confirm they check for the *existence* of data (for example, `isinstance(..., Mapping)`) before accessing it.
- **Check (Error wrapping):** Translate external API failures in `coordinator.py` into `UpdateFailed` or `ConfigEntryAuthFailed`.

### 7. File Consistency
- **Check:** Verify that evidence paths referenced in `quality_scale.yaml` actually exist.

---

## Immediate application: `rebuild_registry` service audit (rule 5)
- **Finding (YAML promises):** `services.yaml` declares `mode` and `device_ids` fields for `rebuild_registry`, along with an extended description about rebuild vs. migrate behaviors and optional device scoping.【F:custom_components/googlefindmy/services.yaml†L96-L123】
- **Resolution (Python behavior):** `async_rebuild_registry_service` now parses `mode`, scopes work to `device_ids` when provided, runs migration helpers for `migrate` requests, and reloads the selected entries for `rebuild` runs so the UI contract matches backend behavior.【F:custom_components/googlefindmy/services.py†L1176-L1253】
- **Regression guard:** Keep unit tests that assert `mode` routing, migration helper invocation, and device-scoped reloads to prevent future drift between `services.yaml` and handler logic.【F:tests/test_services_rebuild_registry.py†L46-L102】【F:tests/test_services_rebuild_registry.py†L119-L150】
