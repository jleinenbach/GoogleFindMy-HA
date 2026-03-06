# BLE Battery Sensor — Architecture & Lessons Learned

This document describes the **BLE battery sensor** feature: how battery
state flows from a Bluetooth Low Energy advertisement all the way to a
Home Assistant sensor entity.  It also records the lessons learned during
development so that future contributors avoid the same pitfalls.

---

## 1. High-level data flow

```
┌─────────────┐   BLE advert    ┌──────────┐  resolve_eid()  ┌─────────────────────┐
│  Tracker    │ ──────────────► │ Bermuda  │ ──────────────► │  EID Resolver       │
│  (FMDN)    │   0x40|EID|flags│ (scanner) │  raw payload    │  (googlefindmy)     │
└─────────────┘                 └──────────┘                 │                     │
                                                             │  _resolve_eid_…()   │
                                                             │  ↓                  │
                                                             │  _update_ble_battery│
                                                             │  ↓                  │
                                                             │  _ble_battery_state │
                                                             │  [canonical_id]     │
                                                             └────────┬────────────┘
                                                                      │
              ┌───────────────────────────────────────────────────────┘
              │ get_ble_battery_state(canonical_id)
              ▼
┌───────────────────────────┐   _build_entities()    ┌────────────────────────────┐
│  sensor.py                │ ◄────────────────────  │  Coordinator update loop   │
│  GoogleFindMyBLEBattery-  │                        │  (_add_new_devices listener)│
│  Sensor                   │                        └────────────────────────────┘
│  ._device_id = canonical  │
│  .native_value → battery% │
└───────────────────────────┘
```

### Step-by-step

| # | Where | What happens |
|---|-------|-------------|
| 1 | **Tracker** | Broadcasts a BLE advertisement with Frame Type `0x40`, a 20-byte EID, and an optional 1-byte hashed-flags field. |
| 2 | **Bermuda** (`fmdn/extraction.py`) | `extract_raw_fmdn_payloads()` extracts the **unmodified** service-data bytes (including the hashed-flags byte). |
| 3 | **Bermuda** (`fmdn/integration.py`) | `normalize_eid_bytes()` converts the type to `bytes` (no content stripping), then calls `resolver.resolve_eid(payload)` or `resolver.resolve_eid_all(payload)`. |
| 4 | **Resolver** (`eid_resolver.py`) | `_resolve_eid_internal()` looks up the EID in the precomputed cache. If found, returns `list[EIDMatch]` plus the raw payload and frame metadata. |
| 5 | **Resolver** (`_update_ble_battery()`) | Locates the hashed-flags byte by frame format, XOR-decodes it with `compute_flags_xor_mask()`, extracts 2-bit battery level and UWT mode. The FMDN spec uses MSB-first bit numbering, so battery is at standard bits 2:1 and UWT at standard bit 0. Stores a `BLEBatteryState` keyed by **`canonical_id`**. |
| 6 | **Sensor** (`sensor.py` → `_build_entities()`) | On every coordinator update, iterates devices and calls `resolver.get_ble_battery_state(dev_id)` where `dev_id = device["id"]` (the canonical_id). If non-None, creates a `GoogleFindMyBLEBatterySensor`. |
| 7 | **Sensor** (`native_value` property) | On each HA state poll, reads the latest `BLEBatteryState` from the resolver and returns `battery_pct`. |

---

## 2. Identity model — the three device IDs

Understanding the identity model is **critical** for this feature.
Three identifiers coexist in the system:

| Identifier | Source | Example | Used where |
|---|---|---|---|
| **`canonical_id`** | Google API (`device["id"]` in coordinator snapshot, `DeviceIdentity.canonical_id`) | `01KBBxxx:aaaaaaaa-…-bbbbbbbb` | Coordinator snapshots, sensor `_device_id`, `_ble_battery_state` key |
| **`registry_id`** | HA device registry (`device.id`, `DeviceIdentity.registry_id`) | `11b2838b4bb2ba2eb5f4f4b2c742cbf9` | `EIDMatch.device_id`, internal HA references |
| **`config_entry_id`** | HA config entry | `abcdef1234567890` | `EIDMatch.config_entry_id` |

The mapping lives in `coordinator/identity.py`:

```python
registry_map[canonical_id] = (device.id, None)
#            ^^^^^^^^^^^^     ^^^^^^^^^
#            Google API ID    HA registry ID
```

### Key rule

> **`_ble_battery_state` MUST be keyed by `canonical_id`** because
> the sensor entity queries it with `device["id"]` from the coordinator
> snapshot, which is always the canonical_id.

The storage key is computed as:
```python
storage_key = match.canonical_id or match.device_id
```

The `or match.device_id` fallback handles the (theoretical) case where
`canonical_id` is empty, but in practice it is always set.

---

## 3. Hashed-flags byte — decoding

The FMDN hashed-flags byte is XOR-obfuscated per rotation window.
The resolver computes the XOR mask during EID precomputation
(`compute_flags_xor_mask()` in `FMDNCrypto/eid_generator.py`).

```
Raw flags byte:    0xAB
XOR mask:          0x73   (derived from EIK + time counter)
Decoded:           0xAB ^ 0x73 = 0xD8

Bit layout (decoded):
  Bits 0-4:   Reserved / implementation-specific
  Bits 5-6:   Battery level (2-bit enum)
  Bit  7:     UWT mode (Unwanted Tracking protection active)

Battery mapping:
  0 → UNSUPPORTED → None (unknown)
  1 → NORMAL      → 100%
  2 → LOW         →  25%
  3 → CRITICALLY_LOW → 5%
```

The XOR mask is computed for **all** EID windows (previous, current,
next) so the resolver can decode the flags regardless of which rotation
window the tracker is currently broadcasting.

---

## 4. Lazy sensor creation

The battery sensor is **not** created at integration startup.  It is
created **lazily** when the first BLE battery state arrives:

1. `_build_entities()` runs on every coordinator data update.
2. It checks `resolver.get_ble_battery_state(dev_id)`.
3. If the result is `None` (no BLE data yet), no sensor is created.
4. Once Bermuda resolves a BLE advertisement and the resolver stores the
   battery state, the next coordinator update detects non-None state and
   creates the sensor entity.
5. After creation, the sensor is tracked in `known_battery_ids` to avoid
   duplicate creation.

This means the sensor only appears after Bermuda (or another BLE
scanner) has resolved at least one advertisement for the device.

---

## 5. Logging strategy

| Log | Level | When | Purpose |
|---|---|---|---|
| `FMDN_FLAGS_PROBE` (first decode) | **INFO** | Once per device (lifetime of resolver instance) | Confirms the BLE battery pipeline works end-to-end |
| `FMDN_FLAGS_PROBE CANNOT_DECODE` | DEBUG | Once per device when decode fails | Diagnostics for missing XOR mask or truncated payload |
| `BLE battery changed` | DEBUG | On every battery level change | Track battery transitions |
| `BLE battery sensor created` | **INFO** | Once per device when entity is created | Confirms sensor appeared in HA |

The first-decode `FMDN_FLAGS_PROBE` is at **INFO** level intentionally.
It fires exactly once per device per HA session, so it is not spammy.
Users need to see this in default HA logs to confirm the pipeline works.

---

## 6. Lessons Learned

### 6.1  Device ID mismatch (root cause bug)

**Symptom:** Battery sensor never appeared despite Bermuda correctly
resolving EIDs and `_update_ble_battery()` being called.

**Root cause:** `_ble_battery_state` was keyed by `match.device_id`
(HA device registry ID), but `get_ble_battery_state()` was called with
`device["id"]` from the coordinator snapshot (Google API canonical_id).
These are **always** different identifiers.

```
Resolver stored:  _ble_battery_state["11b2838b4bb2ba2eb5…"]  ← registry_id
Sensor queried:   get_ble_battery_state("01KBBxxx:aaaa…")    ← canonical_id
→ NEVER equal → lookup ALWAYS returned None → sensor NEVER created
```

**Fix:** Key `_ble_battery_state` by `match.canonical_id` (with
fallback to `match.device_id`).

**Lesson:** When multiple identifier namespaces coexist, document and
test the keying contract explicitly.  The `EIDMatch` dataclass carries
both `device_id` (registry) and `canonical_id` (Google API), and it is
easy to pick the wrong one.

### 6.2  Log level demotion hides diagnostics

**Symptom:** User demoted `FMDN_FLAGS_PROBE` from INFO to DEBUG.
Afterward, the log message disappeared entirely from the HA log viewer
(which defaults to INFO level).  User concluded the code was broken.

**Root cause:** Not a code bug — HA's default log level filters out
DEBUG messages.  The demotion was structurally correct but made the
one-time diagnostic probe invisible.

**Fix:** Reverted the first-decode `FMDN_FLAGS_PROBE` to INFO.  It
fires once per device per session, so it is acceptable at INFO.  All
subsequent/repeated logs remain at DEBUG.

**Lesson:** One-time-per-device diagnostic logs should stay at INFO.
They are essential for confirming end-to-end functionality and do not
create log noise.  Only repeated / per-advertisement logs should be
demoted to DEBUG.

### 6.3  `resolve_eid()` is a public API with no internal callers

**Symptom:** During initial debugging, it looked like `resolve_eid()`
was never called because no callers existed within the googlefindmy
codebase.

**Root cause:** `resolve_eid()` is intentionally a **public API** for
external consumers (e.g., Bermuda).  The integration itself never calls
it.  The EID resolution path is driven entirely by the external BLE
scanner.

**Lesson:** When tracing a data path, check external consumers (other
integrations) in addition to internal callers.  The
`Ephemeral_Identifier_Resolver_API.md` documents this contract.

### 6.4  Google API `battery_level` is always None

The `battery_level` field in the coordinator data model (from
`ProtoDecoders/decoder.py`) is always `None` — Google's API does not
populate it for FMDN trackers.  Battery data is only available via the
BLE hashed-flags byte decoded locally.  Do not confuse
`device["battery_level"]` (always None) with
`BLEBatteryState.battery_level` (from BLE).

### 6.5  Bermuda passes full raw payloads

Bermuda's `extract_raw_fmdn_payloads()` returns the **unmodified**
payload bytes including the hashed-flags byte.  The
`normalize_eid_bytes()` helper only normalizes the Python type
(`bytearray`/`memoryview`/`str` → `bytes`) without stripping content.
This is correct and expected — the resolver needs the full payload to
extract the flags byte.

---

## 7. Test coverage

Tests live in `tests/test_ble_battery_sensor.py` and cover:

- Battery state storage and retrieval via canonical_id
- All battery levels (UNSUPPORTED, NORMAL, LOW, CRITICALLY_LOW)
- UWT mode detection
- Sensor creation, availability, and restore behavior
- Shared-device propagation (multiple matches per advertisement)
- XOR mask computation and flags byte decoding
- Frame format detection (service-data vs raw-header)

The `_match()` test helper defaults `canonical_id` to `device_id` so
that the storage key matches the lookup key in test scenarios.

---

## 8. File reference

| File | Role |
|---|---|
| `eid_resolver.py:2300–2443` | `_update_ble_battery()`, `get_ble_battery_state()` |
| `eid_resolver.py:150–173` | `BLEBatteryState` dataclass, `FMDN_BATTERY_PCT` mapping |
| `eid_resolver.py:132–139` | `EIDMatch` (carries both `device_id` and `canonical_id`) |
| `sensor.py:500–567` | `_build_entities()` — lazy battery sensor creation |
| `sensor.py:1270–1400` | `GoogleFindMyBLEBatterySensor` class |
| `coordinator/identity.py:457` | `registry_map[canonical_id] = (device.id, None)` |
| `coordinator/main.py:505–520` | `DeviceIdentity` dataclass |
| `FMDNCrypto/eid_generator.py:256` | `compute_flags_xor_mask()` |
| `ProtoDecoders/decoder.py:334` | `"id": canonic_id` in device stub |
| `tests/test_ble_battery_sensor.py` | Full test suite |
