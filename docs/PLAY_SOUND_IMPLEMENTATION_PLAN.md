# Play Sound Implementation Plan

## Goal

Transform Play Sound from fire-and-forget cloud-only into a robust two-path system
with response validation (cloud) and direct BLE ringing (local fallback).

---

## Phase 1: Parse Nova Response (prerequisite for everything else)

### Problem

The Nova API returns a protobuf response on HTTP 200, but our code discards it:

```python
# nova_request.py:1407-1408
if status == HTTP_OK:
    return cast(bytes, content).hex()   # raw hex, never parsed

# api.py:1538-1540
_response_hex, request_uuid = result    # _response_hex is ignored
return (True, request_uuid)             # "True" = HTTP 200, nothing more
```

There is no `ExecuteActionResponse` message defined in `DeviceUpdate.proto`. The
response format is unknown and must be reverse-engineered from live traffic.

### Step 1.1: Capture and log the response payload

**Files:** `api.py`

Add structured debug logging of the raw response hex in `async_play_sound()` and
`async_stop_sound()` so users/developers can inspect what Google returns:

```python
# api.py — async_play_sound()
response_hex, request_uuid = result
_LOGGER.debug(
    "Play Sound response for %s (uuid=%s): %d bytes: %s",
    device_id,
    request_uuid[:8] if request_uuid else "none",
    len(response_hex) // 2,
    response_hex[:200],  # first 100 bytes hex
)
```

**Acceptance:** Response hex visible in HA debug logs when Play Sound is triggered.

### Step 1.2: Attempt generic protobuf decode of the response

**Files:** `api.py` or new `NovaApi/ExecuteAction/PlaySound/response_parser.py`

Since there is no `ExecuteActionResponse` proto definition, try:

1. **google.rpc.Status** — already vendored in `RpcStatus_pb2.py`. If the success
   response is also an rpc.Status with code=0, we get confirmation for free.
2. **Raw protobuf field scan** — use `google.protobuf.descriptor_pool` or raw varint
   parsing to identify field numbers and types without a schema.
3. **Upstream traffic analysis** — check if GoogleFindMyTools upstream has decoded
   the response in any branch or issue.

```python
def parse_action_response(response_hex: str) -> ActionResult:
    """Best-effort parse of Nova ExecuteAction response.

    Returns:
        ActionResult with status (ACCEPTED / REJECTED / UNKNOWN) and
        optional detail fields.
    """
```

**Acceptance:** For a successful Play Sound, the parser returns a structured result.
For an unknown format, it returns `UNKNOWN` with the raw hex for logging.

### Step 1.3: Surface response status to the HA entity

**Files:** `api.py`, `button.py`

Change `async_play_sound()` return type to carry the parsed result:

```python
# Before:
async def async_play_sound(self, device_id: str) -> tuple[bool, str | None]:

# After:
async def async_play_sound(self, device_id: str) -> PlaySoundResult:
    """Returns PlaySoundResult with .success, .request_uuid, .server_status"""
```

The button entity can then set `extra_state_attributes` with:
- `last_ring_status`: "accepted" / "submitted" / "rejected" / "error"
- `last_ring_uuid`: request UUID for correlation
- `last_ring_timestamp`: ISO timestamp

**Acceptance:** HA entity attributes reflect whether the command was server-acknowledged.

### Step 1.4: Define `ExecuteActionResponse` proto (if structure identified)

**Files:** `ProtoDecoders/DeviceUpdate.proto`, regenerate `DeviceUpdate_pb2.py`

Once the response structure is known from step 1.2, add the message definition:

```protobuf
message ExecuteActionResponse {
    int32 status = 1;           // hypothetical
    string requestUuid = 2;     // echo back
    // ... discovered fields
}
```

**Acceptance:** Response can be parsed into a typed protobuf message.

---

## Phase 2: Direct BLE Ringing via HA Bluetooth Stack

### Prerequisites

- Phase 1 complete (response parsing established)
- Understanding of current BLE MAC from EID resolution
- `bluetooth` dependency added to `manifest.json`

### Architecture Decision: HA Bluetooth, NOT Bermuda

| | Bermuda | HA Bluetooth (`homeassistant.components.bluetooth`) |
|--|---------|------------------------------------------------------|
| GATT writes | No | Yes |
| Active connections | No | Yes (via `bleak-retry-connector`) |
| ESPHome proxy | RSSI only | Full GATT proxy (active mode) |
| Purpose | Room presence | Full BLE stack |

**Bermuda is not involved in BLE ringing.** It remains a passive location signal
source. Direct BLE ringing uses HA's built-in bluetooth integration, which:

- Wraps `bleak` (Python BLE library) with HA adapter management
- Supports transparent routing through ESPHome BLE proxies (active mode)
- Provides `async_ble_device_from_address()` to resolve BLE devices
- Is used by SwitchBot, Yale Lock, and other HA integrations for GATT writes

### Step 2.1: Add `bluetooth` dependency

**Files:** `manifest.json`

```json
{
    "dependencies": ["http", "bluetooth"],
    "requirements": [
        "bleak>=0.21.0",
        "bleak-retry-connector>=3.4.0",
        ...existing...
    ]
}
```

**Risk:** This makes the integration depend on a Bluetooth adapter being configured
in HA. Users without BLE should still work (cloud-only fallback). The `bluetooth`
dependency must be made optional or the BLE ring path must gracefully degrade.

**Mitigation:** Check `bluetooth` availability at runtime:

```python
try:
    from homeassistant.components.bluetooth import async_ble_device_from_address
    HAS_BLUETOOTH = True
except ImportError:
    HAS_BLUETOOTH = False
```

### Step 2.2: Capture current MAC during EID resolution

**Files:** `eid_resolver.py`

The EID resolver already processes BLE advertisements from Bermuda. Each
advertisement contains the current (rotated) MAC address. Store it:

```python
# During EID match:
match = EIDMatch(
    device_id=registry_id,
    canonical_id=canonical_id,
    ble_address=service_info.address,      # NEW: current MAC
    ble_address_timestamp=time.time(),     # NEW: when seen
)
```

**Challenge:** FMDN trackers rotate MAC every ~15 minutes. The stored MAC is valid
only within the rotation window. A ring attempt with a stale MAC will fail at the
BLE connection stage (device not found / wrong device).

**Mitigation:** Only attempt BLE ring if `ble_address_timestamp` is < 10 minutes old.

### Step 2.3: Implement FMDN Beacon Actions GATT client

**Files:** New `FMDNCrypto/beacon_actions.py`

Implement the FMDN ring protocol per the spec:

```python
BEACON_ACTIONS_UUID = "FE2C1238-8366-4814-8EB0-01DE32100BEA"

async def async_ring_via_ble(
    hass: HomeAssistant,
    ble_address: str,
    ring_key: bytes,        # 8 bytes from key_derivation.py
    *,
    volume: int = 3,        # 0-3
    timeout_ds: int = 100,  # deciseconds (10s default)
    component: int = 0xFF,  # all components
) -> BleRingResult:
    """Ring an FMDN tracker via direct BLE GATT write.

    Protocol:
        1. Connect to tracker
        2. Read Beacon Actions characteristic → 8-byte nonce
        3. Compute one-time auth: HMAC-SHA256(ring_key, nonce || 0x05 || addl)[:8]
        4. Write ring command: [0x05] [data_len=11] [8B auth] [component] [timeout] [volume]
        5. Read notification → status byte
        6. Disconnect

    IMPORTANT: data_len = len(auth_key) + len(addl_data) = 8 + 3 = 11
               (Upstream bug #66 used len(addl_data) only = 3, causing ATT Error 0x81)
    """
```

**Ring key is already derived** in `key_derivation.py:58`:
```python
self.ringing_key = calculate_truncated_sha256(identity_key_bytes, 0x02)  # 8 bytes
```

Currently only used during device registration. Must be stored/accessible for
runtime ring operations.

### Step 2.4: Orchestrate cloud + BLE ring with fallback

**Files:** `api.py`

```python
async def async_play_sound(self, device_id: str) -> PlaySoundResult:
    # 1. Always try cloud first (global reach)
    cloud_result = await self._async_play_sound_cloud(device_id)

    if cloud_result.confirmed:
        return cloud_result

    # 2. If cloud unconfirmed and BLE available, try direct
    if HAS_BLUETOOTH and self._has_recent_ble_address(device_id):
        ble_result = await self._async_play_sound_ble(device_id)
        if ble_result.confirmed:
            return ble_result

    # 3. Return best available result
    return cloud_result  # "submitted" but unconfirmed
```

**Why cloud first:** Cloud works globally. BLE only works if tracker is in range.
Most users won't have BLE proximity. Cloud-first means the common case is fast.

**Why BLE fallback matters:** When the tracker is at home (most common "where are my
keys?" scenario), BLE is faster and provides hardware confirmation.

### Step 2.5: Ring key availability at runtime

**Files:** `coordinator/identity.py`, `api.py`

The ring key must be accessible when `async_play_sound_ble()` is called.
Options:

1. **Derive on-the-fly** from stored EIK: `SHA256(EIK || 0x02)[:8]`
   - EIK is already available via `async_retrieve_identity_key()`
   - Adds ~1ms computation, negligible
   - Preferred: no additional storage needed

2. **Store during registration:** Already done in `create_ble_device.py:110`
   but only sent to Google, not stored locally.

**Recommendation:** Option 1 — derive from EIK at ring time.

---

## Phase 3: Entity UX improvements (future)

### Step 3.1: Ring status sensor

Add a `sensor` entity that reflects the ring state:

- `idle` — no active ring
- `ringing_cloud` — cloud command submitted, awaiting timeout
- `ringing_ble` — BLE ring confirmed active
- `failed` — ring attempt failed

### Step 3.2: Component selection for multi-component devices

Headphones have LEFT, RIGHT, and CASE components. The proto already supports:

```protobuf
enum DeviceComponent {
    DEVICE_COMPONENT_UNSPECIFIED = 0;  // ring all
    DEVICE_COMPONENT_RIGHT = 1;
    DEVICE_COMPONENT_LEFT = 2;
    DEVICE_COMPONENT_CASE = 3;
}
```

Add a service parameter or separate buttons for component-specific ringing.

### Step 3.3: Auto-stop and timeout

- BLE ring has explicit timeout (`timeout_ds` parameter)
- Cloud ring has no known timeout — may ring indefinitely
- Add HA automation-friendly auto-stop after configurable duration

---

## Dependency Graph

```
Phase 1.1  Capture response hex (logging)
    |
    v
Phase 1.2  Decode response format
    |
    v
Phase 1.3  Surface status to entity
    |
    v
Phase 1.4  Define response proto (if applicable)
    |
    +-----> Phase 2.1  Add bluetooth dependency
    |           |
    |           v
    |       Phase 2.2  Capture MAC from EID resolution
    |           |
    |           v
    |       Phase 2.3  Implement GATT ring client
    |           |
    |           v
    +-----> Phase 2.4  Cloud + BLE orchestration
                |
                v
            Phase 2.5  Ring key availability
                |
                v
            Phase 3    UX improvements
```

---

## Risk Assessment

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Unknown response format | Phase 1.2 blocked | Medium | Generic protobuf scan, raw hex logging |
| MAC rotation staleness | BLE ring fails | High | 10-min freshness check, cloud fallback |
| ESPHome proxy connection slots | BLE ring contention | Medium | Short-lived connections only |
| `bluetooth` as hard dependency | Breaks non-BLE installs | High | Runtime import check, graceful degrade |
| Ring key not available | BLE ring impossible | Low | Derive from EIK on-the-fly |
| Upstream bug #66 data_len | ATT Error 0x81 | Eliminated | Correct formula: data_len = 8 + 3 = 11 |

---

## Files Affected Summary

| Phase | File | Change |
|-------|------|--------|
| 1.1 | `api.py` | Debug-log response hex |
| 1.2 | New: `PlaySound/response_parser.py` | Generic response decoder |
| 1.3 | `api.py`, `button.py` | Return/expose parsed status |
| 1.4 | `DeviceUpdate.proto` | Add `ExecuteActionResponse` |
| 2.1 | `manifest.json` | Add `bluetooth` dependency |
| 2.2 | `eid_resolver.py` | Store BLE address on EID match |
| 2.3 | New: `FMDNCrypto/beacon_actions.py` | GATT ring client |
| 2.4 | `api.py` | Cloud + BLE orchestration |
| 2.5 | `coordinator/identity.py` | Ring key derivation at runtime |
