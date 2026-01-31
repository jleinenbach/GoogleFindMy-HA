# Play Sound Implementation Plan

## Goal

Transform Play Sound from fire-and-forget cloud-only into a robust two-path system
with response validation (cloud) and direct BLE ringing (local fallback).

## Context: What Upstream and Google Give Us

Before planning, these facts constrain the design:

1. **No `ExecuteActionResponse` proto exists** — not in upstream, not in Google's
   public proto repos (`googleapis/googleapis`). The `google.internal.spot.v1.SpotService`
   namespace is deliberately excluded from public APIs.
2. **Upstream discards the Nova HTTP response** — `nova_request()` returns hex, but
   the caller in `start_sound_request.py` never assigns the return value.
3. **Our code stores but ignores the response** — `_response_hex` is unpacked at
   `api.py:1538` but never read or validated.
4. **FCM callback infrastructure exists** — `fcm_receiver_ha.py` can register
   per-device callbacks (used by LocateTracker), but no callback is registered for
   sound events. Sound FCM pushes arrive and fall through silently.
5. **Google's FMDN spec documents only BLE-level ringing** — the Beacon Actions
   characteristic protocol is well-specified, but the cloud-side API is not.
6. **Neither upstream nor any known project has BLE GATT ring code** — only community
   members attempted it independently (Issue #66), finding a `data_len` bug.

---

## Phase 1: Response Capture and Parsing

### Step 1.1: Log raw Nova HTTP response and FCM sound pushes

**Files:** `api.py`, `fcm_receiver_ha.py`

Two independent data sources must be captured:

#### 1.1a: Nova HTTP response hex (in `api.py`)

```python
# api.py — async_play_sound(), replace lines 1538-1540
response_hex, request_uuid = result
_LOGGER.debug(
    "Play Sound Nova response for %s (uuid=%s): %d bytes: %s",
    device_id,
    request_uuid[:8] if request_uuid else "none",
    len(response_hex) // 2,
    response_hex[:200],  # first 100 bytes hex
)
```

#### 1.1b: FCM push for sound events (in `fcm_receiver_ha.py`)

Currently, FCM notifications with no registered callback fall through to
`_process_background_update()` which tries to parse them as location responses.
Add diagnostic logging before the fallthrough:

```python
# fcm_receiver_ha.py — _handle_notification_async(), after callback check
if not cb:
    _LOGGER.debug(
        "FCM notification for %s has no registered callback "
        "(may be sound confirmation): payload_len=%d, hex_prefix=%s",
        canonic_id[:8],
        len(hex_string),
        hex_string[:100],
    )
```

**Acceptance:** Both data streams visible in HA debug logs during Play Sound.
Users with real devices can provide sample payloads for schema analysis.

### Step 1.2: Attempt generic protobuf decode

**Files:** New `NovaApi/ExecuteAction/PlaySound/response_parser.py`

Since the response schema is unknown, the parser must be speculative:

```python
from enum import Enum
from dataclasses import dataclass

class ActionStatus(Enum):
    ACCEPTED = "accepted"        # Server confirmed command routing
    SUBMITTED = "submitted"      # HTTP 200 but response unparseable
    REJECTED = "rejected"        # Server returned error in response body
    UNKNOWN = "unknown"          # Could not determine

@dataclass(frozen=True)
class ActionResult:
    status: ActionStatus
    request_uuid: str | None = None
    raw_hex: str = ""
    detail: str = ""

def parse_action_response(response_hex: str) -> ActionResult:
    """Best-effort parse of Nova ExecuteAction response.

    Strategy (ordered by likelihood):
    1. Try google.rpc.Status — already vendored in RpcStatus_pb2.py.
       If code=0, that means OK. If code>0, extract error message.
    2. Try DeviceUpdate — the FCM response format. If the HTTP response
       echoes the same structure, we get requestUuid for correlation.
    3. Raw varint field scan — identify field numbers and wire types
       without a schema. Log discovered structure for future proto def.
    4. Fall through to SUBMITTED with raw hex for manual inspection.
    """
```

**Why google.rpc.Status first:** It's the standard Google API response wrapper.
It's already imported in `nova_request.py:259`. If the success response is
`Status{code: 0}`, we have confirmation with zero reverse engineering.

**Why DeviceUpdate second:** The FCM callback response is a `DeviceUpdate` protobuf.
If the HTTP response uses the same message type, `parse_device_update_protobuf()`
already works and we get `requestUuid` matching.

**Acceptance:** Parser returns structured `ActionResult` for any input.

### Step 1.3: Wire FCM callback for sound event correlation

**Files:** `api.py`, `fcm_receiver_ha.py`

This is the higher-value confirmation path. The infrastructure already exists:

```python
# Pattern from location_request.py (already working):
callback = _make_location_callback(...)
await fcm_receiver.async_register_for_location_updates(canonic_id, callback)
# ... submit Nova request ...
await asyncio.wait_for(ctx.event.wait(), timeout=30)
# ... unregister callback ...
```

For sound events, register a lightweight callback that:
1. Receives the FCM push containing `DeviceUpdate` with `requestUuid`
2. Validates `requestUuid` matches the submitted request
3. Signals an `asyncio.Event` to confirm delivery

```python
# api.py — async_play_sound() conceptual flow:
async def async_play_sound(self, device_id: str) -> PlaySoundResult:
    request_uuid = generate_random_uuid()

    # Register short-lived FCM callback for this device
    confirmation = asyncio.Event()
    await fcm_receiver.async_register_for_sound_updates(
        device_id, lambda cid, hex_resp: confirmation.set()
    )

    try:
        # Submit cloud command
        response_hex = await self._submit_sound_request(device_id, request_uuid)
        nova_result = parse_action_response(response_hex)

        # Wait briefly for FCM confirmation (non-blocking, best-effort)
        try:
            await asyncio.wait_for(confirmation.wait(), timeout=10.0)
            return PlaySoundResult(status=CONFIRMED, ...)
        except asyncio.TimeoutError:
            return PlaySoundResult(status=SUBMITTED, ...)  # no FCM ack
    finally:
        await fcm_receiver.async_unregister_for_sound_updates(device_id)
```

**Key difference from LocateTracker:** The sound callback is fire-and-forget with a
short timeout. LocateTracker blocks for up to 30s waiting for location data. Sound
confirmation is optional — the command already went out.

**Acceptance:** Play Sound returns `CONFIRMED` when FCM push arrives within timeout,
`SUBMITTED` when only HTTP 200 was received.

### Step 1.4: Define proto and surface to entity (once schema is known)

**Blocked until** sample payloads from step 1.1 are collected and analyzed.

**Files (when ready):**
- `ProtoDecoders/DeviceUpdate.proto` — add `ExecuteActionResponse`
- `api.py` — change return type to `PlaySoundResult`
- `button.py` — expose `extra_state_attributes`:
  - `last_ring_status`: "confirmed" / "submitted" / "rejected" / "error"
  - `last_ring_uuid`: request UUID
  - `last_ring_timestamp`: ISO timestamp

---

## Phase 2: Direct BLE Ringing via HA Bluetooth Stack

### Prerequisites

- Phase 1 steps 1.1-1.3 complete (confirmation infrastructure established)
- Understanding of current BLE MAC from EID resolution
- `bluetooth` dependency added to `manifest.json`

### Architecture Decision: HA Bluetooth, NOT Bermuda

| | Bermuda | HA Bluetooth (`homeassistant.components.bluetooth`) |
|--|---------|------------------------------------------------------|
| GATT writes | No | Yes |
| Active connections | No | Yes (via `bleak-retry-connector`) |
| ESPHome proxy | RSSI only | Full GATT proxy (active mode) |
| Purpose | Room presence | Full BLE stack |
| Used by | This integration (passive EID relay) | SwitchBot, Yale Lock, HomeKit BLE |

**Bermuda is not involved in BLE ringing.** It remains a passive location signal
source. Direct BLE ringing uses HA's built-in bluetooth integration, which wraps
`bleak` with adapter management, ESPHome proxy routing, and connection retry logic.

### Step 2.1: Add optional `bluetooth` dependency

**Files:** `manifest.json`

```json
{
    "dependencies": ["http"],
    "after_dependencies": ["bluetooth"],
    "requirements": [
        "bleak>=0.21.0",
        "bleak-retry-connector>=3.4.0",
        ...existing...
    ]
}
```

Using `after_dependencies` instead of `dependencies` ensures HA loads the bluetooth
integration first if available, but does not fail if it's not configured.

Runtime check:

```python
try:
    from homeassistant.components.bluetooth import async_ble_device_from_address
    HAS_BLUETOOTH = True
except ImportError:
    HAS_BLUETOOTH = False
```

### Step 2.2: Capture current MAC during EID resolution

**Files:** `eid_resolver.py`

The EID resolver processes BLE advertisements from Bermuda/HA scanner. Each
advertisement contains the current (rotated) MAC address. Store it alongside the
EID match:

```python
@dataclass
class EIDMatch:
    device_id: str          # registry_id
    canonical_id: str       # canonical_id
    ble_address: str | None = None        # NEW: current rotated MAC
    ble_address_seen: float | None = None # NEW: monotonic timestamp
```

**Freshness constraint:** FMDN trackers rotate MAC every ~15 minutes. Only attempt
BLE ring if `monotonic() - ble_address_seen < 600` (10 minutes).

### Step 2.3: Implement FMDN Beacon Actions GATT client

**Files:** New `FMDNCrypto/beacon_actions.py`

```python
BEACON_ACTIONS_UUID = "FE2C1238-8366-4814-8EB0-01DE32100BEA"

DATA_ID_RING = 0x05
DATA_ID_READ_RING_STATE = 0x06

class RingState(Enum):
    STARTED = 0x00
    FAILED = 0x01
    STOPPED_TIMEOUT = 0x02
    STOPPED_BUTTON = 0x03
    STOPPED_GATT = 0x04

@dataclass(frozen=True)
class BleRingResult:
    success: bool
    state: RingState | None = None
    detail: str = ""

async def async_ring_via_ble(
    hass: HomeAssistant,
    ble_address: str,
    ring_key: bytes,        # 8 bytes from SHA256(EIK || 0x02)[:8]
    *,
    volume: int = 3,        # 0=silent, 3=max
    timeout_ds: int = 100,  # deciseconds (10s default)
    component: int = 0xFF,  # all components
) -> BleRingResult:
    """Ring tracker via direct BLE GATT write.

    IMPORTANT: data_len = len(auth_key) + len(addl_data) = 8 + 3 = 11
               (Issue #66 bug used len(addl_data)=3, causing ATT Error 0x81)
    """
```

**Ring key derivation** is already in `key_derivation.py:58` — derive on-the-fly
from EIK at ring time: `SHA256(EIK || 0x02)[:8]`.

### Step 2.4: Orchestrate cloud + BLE ring

**Files:** `api.py`

```python
async def async_play_sound(self, device_id: str) -> PlaySoundResult:
    # 1. Always try cloud first (global reach, no proximity needed)
    cloud_result = await self._async_play_sound_cloud(device_id)

    if cloud_result.confirmed:
        return cloud_result

    # 2. If cloud unconfirmed and BLE available, try direct
    if HAS_BLUETOOTH and self._has_fresh_ble_address(device_id):
        ble_result = await self._async_play_sound_ble(device_id)
        if ble_result.success:
            return PlaySoundResult(
                status=ActionStatus.CONFIRMED,
                source="ble",
                ble_state=ble_result.state,
            )

    # 3. Return best available result
    return cloud_result  # "submitted" but unconfirmed
```

---

## Phase 3: Entity UX improvements (future)

### Step 3.1: Ring status sensor
- `idle` / `ringing_cloud` / `ringing_ble` / `confirmed` / `failed`

### Step 3.2: Component selection for multi-component devices
- LEFT, RIGHT, CASE via `DeviceComponent` enum (already in proto)

### Step 3.3: Auto-stop and configurable timeout
- BLE: explicit `timeout_ds` parameter
- Cloud: schedule `async_stop_sound()` after configurable duration

---

## Dependency Graph

```
Phase 1.1a  Log Nova HTTP response hex
Phase 1.1b  Log FCM sound pushes
    |           |
    v           v
Phase 1.2   Generic protobuf decode attempt
    |
    +-------+
    |       |
    v       v
Phase 1.3  FCM sound callback    Phase 1.4  Define response proto
    |       (async confirmation)     (blocked until samples collected)
    |
    +-----> Phase 2.1  Add optional bluetooth dependency
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
            Phase 3    UX improvements
```

---

## Risk Assessment

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Nova response is empty/opaque protobuf | Phase 1.2 inconclusive | Medium | FCM callback (Phase 1.3) provides independent confirmation path |
| FCM sound push has different format | Phase 1.3 needs adjustment | Low | `DeviceUpdate` is the only FCM message type; sound pushes likely use same structure |
| MAC rotation staleness | BLE ring fails | High | 10-min freshness check, cloud fallback always runs first |
| ESPHome proxy connection slots | BLE ring contention | Medium | Short-lived connections (~2s), immediate disconnect |
| `bluetooth` as hard dependency | Breaks non-BLE installs | High | `after_dependencies` + runtime import check |
| Ring key not available at runtime | BLE ring impossible | Low | Derive from EIK on-the-fly (~1ms) |
| Upstream bug #66 data_len | ATT Error 0x81 | Eliminated | Correct formula documented: data_len = 8 + 3 = 11 |

---

## Files Affected Summary

| Phase | File | Change |
|-------|------|--------|
| 1.1a | `api.py` | Debug-log Nova response hex |
| 1.1b | `fcm_receiver_ha.py` | Debug-log unhandled FCM pushes (sound candidates) |
| 1.2 | New: `PlaySound/response_parser.py` | Generic response decoder (rpc.Status → DeviceUpdate → raw scan) |
| 1.3 | `api.py`, `fcm_receiver_ha.py` | FCM sound callback registration + correlation |
| 1.4 | `DeviceUpdate.proto`, `DeviceUpdate_pb2.py` | Add `ExecuteActionResponse` (when schema known) |
| 2.1 | `manifest.json` | Add `bluetooth` to `after_dependencies` |
| 2.2 | `eid_resolver.py` | Store BLE address + timestamp on EID match |
| 2.3 | New: `FMDNCrypto/beacon_actions.py` | GATT ring client |
| 2.4 | `api.py` | Cloud + BLE orchestration |
