# Play Sound Architecture

## Overview

The Play Sound feature allows users to ring a tracked device (FMDN tag, headphones,
Android phone) from Home Assistant. This document describes the current cloud-only
implementation, explains why direct BLE ringing does not exist yet, and outlines the
future architecture that combines both paths.

---

## Current Architecture: Cloud-Only (Nova API)

### Request Flow

```
User presses "Play Sound" button in HA
       |
       v
button.py  GoogleFindMyPlaySoundButton.async_press()
       |  calls hass.services.async_call(DOMAIN, SERVICE_PLAY_SOUND, ...)
       v
api.py  async_play_sound()
       |  validates push readiness, resolves FCM token
       v
start_sound_request.py  async_submit_start_sound_request()
       |  builds protobuf payload, submits to Nova
       v
sound_request.py  create_sound_request(should_start=True, ...)
       |  creates ExecuteActionRequest protobuf
       v
nbe_execute_action.py  create_action_request() + serialize_action_request()
       |  sets scope=SPOT_DEVICE, action=startSound, component=UNSPECIFIED
       |  serializes to hex
       v
nova_request.py  async_nova_request(NOVA_ACTION_API_SCOPE, hex_payload)
       |  authenticates (AAS -> ADM token chain)
       |  POST to Google's Nova endpoint
       v
Google Cloud Server
       |  routes command via FCM push notification
       v
Target Device rings
```

### Key Files

| File | Responsibility |
|------|---------------|
| `button.py:1043-1130` | `GoogleFindMyPlaySoundButton` — HA button entity, calls service |
| `button.py:1133-1226` | `GoogleFindMyStopSoundButton` — HA button entity for stop |
| `api.py:1499-1585` | `async_play_sound()` — entry point, FCM token resolution |
| `api.py:1587-1684` | `async_stop_sound()` — stop counterpart |
| `api.py:1438-1456` | `can_play_sound()` — capability check |
| `api.py:1376-1431` | `is_push_ready()` — FCM transport readiness |
| `sound_request.py:44-98` | `create_sound_request()` — pure protobuf builder |
| `nbe_execute_action.py:36-64` | `create_action_request()` — protobuf envelope |
| `nbe_execute_action.py:66-69` | `serialize_action_request()` — hex serialization |
| `start_sound_request.py:62-169` | `async_submit_start_sound_request()` — Nova submission |
| `stop_sound_request.py:63-142` | `async_submit_stop_sound_request()` — Nova submission |
| `nova_request.py:1202-1632` | `async_nova_request()` — HTTP transport, auth, retry |
| `_cli_helpers.py` | CLI FCM token resolution for standalone testing |

### Protobuf Structure

```protobuf
message ExecuteActionRequest {
    DeviceScope scope = 1;        // SPOT_DEVICE + canonicId
    RequestMetadata metadata = 2; // requestUuid, fmdClientUuid, gcmRegistrationId
    DeviceAction action = 3;      // startSound or stopSound
}

message DeviceAction {
    StartSound startSound = 1;    // component = DEVICE_COMPONENT_UNSPECIFIED
    StopSound stopSound = 2;
}
```

### What Happens After Submission

1. `async_nova_request()` returns `response_hex` (hex-encoded protobuf)
2. `async_submit_start_sound_request()` returns `(response_hex, request_uuid)`
3. `api.py` logs success and returns `(True, request_uuid)` to the caller

**Critical gap: The response is never parsed.** The hex payload likely contains a
Google `rpc.Status` or `ExecuteActionResponse` protobuf, but the code treats any
non-None response as success. There is no confirmation that:

- Google accepted the command
- The FCM push was delivered
- The device actually rang

### Authentication Chain

```
AAS Token (Google Account Sign-In)
    |
    v  async_nova_request() exchanges AAS -> ADM
ADM Token (Android Device Management)
    |
    v  Authorization: Bearer {ADM_TOKEN}
Nova API Endpoint (NOVA_ACTION_API_SCOPE)
    |
    v  FCM Push
Device
```

Token management is handled by `AsyncTTLPolicy` in `nova_request.py` with:
- Proactive refresh before expiry
- Entry-scoped caching (multi-account safe)
- Automatic 401 recovery with one retry

---

## What Upstream Has That We Don't: Direct BLE Ringing

### FMDN Beacon Actions Characteristic

The FMDN specification defines a GATT characteristic for direct device control:

- **UUID:** `FE2C1238-8366-4814-8EB0-01DE32100BEA` (Beacon Actions)
- **Protocol:** Read nonce -> compute auth -> write command -> read notification

| Data ID | Operation | Description |
|---------|-----------|-------------|
| `0x05` | Ring | Start ringing the tracker |
| `0x06` | Read ringing state | Check if currently ringing |

### BLE Ring Protocol (FMDN Spec)

```
Step 1: Read Beacon Actions characteristic
        -> Receive 8-byte random nonce from tracker

Step 2: Compute auth key
        ring_key = SHA256(EIK || 0x02)[:8]     # 8-byte truncated
        auth_data = HMAC-SHA256(ring_key, nonce || data_id=0x05 || addl_data)[:8]

Step 3: Write to Beacon Actions characteristic
        Payload: [data_id=0x05] [data_len] [8-byte auth_key] [ring_bitmask] [timeout] [volume]
        Where: data_len = len(auth_key) + len(addl_data)  # = 8 + 3 = 11

Step 4: Read notification
        -> Receive authentication result + status
        -> GATT response confirms whether command was accepted
```

### Known Bug in Upstream BLE Implementation (Issue #66)

The upstream GoogleFindMyTools library has a bug in step 3:

```
BUG:     data_len = len(addl_data)              # = 3 (missing auth key length!)
CORRECT: data_len = len(addl_data) + len(auth_key)  # = 3 + 8 = 11
```

This causes ATT Error `0x81` (application-level rejection). Our integration is not
affected because we do not implement BLE GATT ringing. However, if BLE ringing is
added in the future, this calculation must be correct.

### Why Our Integration Is Cloud-Only

1. **Design context:** HA servers are typically not BLE-adjacent to tracked devices.
   The Nova cloud API works globally regardless of physical proximity.
2. **No `bleak` dependency:** The `manifest.json` declares no `bluetooth` dependency
   and does not include `bleak` or `bleak-retry-connector` in requirements.
3. **Upstream focus:** The upstream library's BLE code targets CLI/desktop use where
   a Bluetooth adapter is directly available.

---

## Comparison: Cloud vs. BLE Ringing

| Aspect | Cloud (Nova API) | Direct BLE (GATT) |
|--------|------------------|--------------------|
| **Latency** | 2-15 seconds (FCM push) | < 1 second |
| **Range** | Global (any crowdsource reporter) | ~30m BLE range |
| **Prerequisites** | Google auth + FCM token | BLE adapter in proximity |
| **Confirmation** | None (fire-and-forget) | GATT response = hardware ack |
| **Reliability** | Depends on FCM delivery | Direct, deterministic |
| **HA compatibility** | Works everywhere | Requires `bluetooth` integration |
| **ESPHome proxy** | N/A | Supported (active mode) |
| **MAC rotation** | N/A (server-side routing) | Must resolve current MAC from EID |

---

## HA Bluetooth Stack for Future BLE Ringing

### Architecture Layers

```
+-------------------------------------------------+
|  GoogleFindMy-HA (this integration)             |
|  Uses: establish_connection + write_gatt_char   |
+-------------------------------------------------+
|  bleak-retry-connector                          |
|  Handles: retries, backoff, service caching     |
+-------------------------------------------------+
|  Bleak (BleakClient)                            |
|  Handles: GATT protocol, platform abstraction   |
+-------------------------------------------------+
|  homeassistant.components.bluetooth             |
|  Handles: adapter discovery, scanner sharing,   |
|  ESPHome proxy routing, adapter failover        |
+-------------------------------------------------+
|  BlueZ (local USB) OR ESPHome BLE Proxy         |
+-------------------------------------------------+
```

### Standard Pattern for GATT Writes in HA

```python
from homeassistant.components.bluetooth import async_ble_device_from_address
from bleak_retry_connector import establish_connection, BleakClientWithServiceCache

# 1. Obtain BLEDevice from HA's bluetooth component
ble_device = async_ble_device_from_address(hass, current_mac)

# 2. Connect with retry logic
client = await establish_connection(
    BleakClientWithServiceCache,
    ble_device,
    name="fmdn_tracker",
    max_attempts=3,
)

# 3. Read nonce, compute auth, write ring command
nonce = await client.read_gatt_char(BEACON_ACTIONS_UUID)
payload = build_ring_payload(ring_key, nonce)
await client.write_gatt_char(BEACON_ACTIONS_UUID, payload)

# 4. Read response notification for confirmation
# ...

await client.disconnect()
```

### Bermuda vs. HA Bluetooth for BLE Ringing

| Aspect | Bermuda | HA Bluetooth (`homeassistant.components.bluetooth`) |
|--------|---------|------------------------------------------------------|
| **Purpose** | Passive room presence / trilateration | Full BLE stack (scan + GATT) |
| **GATT writes** | No | Yes |
| **Active connections** | No | Yes (via bleak-retry-connector) |
| **ESPHome proxy** | Reads RSSI only | Full GATT proxy (active mode) |
| **Role in this project** | Location signal source, EID advertisement relay | Required for future BLE ringing |

**Bermuda is not needed for BLE ringing.** The HA bluetooth integration provides
everything required. Bermuda's role remains passive location tracking.

### MAC Address Rotation Challenge

FMDN trackers rotate their BLE MAC address for privacy. To connect via GATT, the
current MAC must be known. Two resolution paths exist:

1. **From HA scanner data:** `async_ble_device_from_address(hass, current_mac)` — but
   this requires knowing the rotated MAC, which changes every ~15 minutes.
2. **From EID resolution:** The `eid_resolver.py` already maps EIDs to device
   identities. The BLE advertisement that contained the matched EID also carries the
   current MAC address (in `BluetoothServiceInfoBleak.address`). This address can be
   captured during EID resolution and stored for the connection window.

### Required Manifest Changes for BLE Support

```json
{
    "dependencies": ["http", "bluetooth"],
    "requirements": [
        "bleak>=0.21.0",
        "bleak-retry-connector>=3.4.0",
        ...existing requirements...
    ]
}
```

---

## Key Derivation for Ringing

The ring authentication key is derived from the Ephemeral Identity Key (EIK):

```
EIK (32 bytes, from device registration)
    |
    v  SHA256(EIK || 0x02)[:8]
Ring Key (8 bytes)
    |
    v  HMAC-SHA256(ring_key, nonce || 0x05 || addl_data)[:8]
One-Time Auth Key (8 bytes, sent in GATT write)
```

This derivation is already implemented in `key_derivation.py:58`:
```python
self.ringing_key = calculate_truncated_sha256(identity_key_bytes, 0x02)
```

The ring key is currently only used during device registration
(`create_ble_device.py:110`), but will be reused for direct BLE ring commands.

---

## Glossary

| Term | Definition |
|------|-----------|
| **Nova API** | Google's server-side API for Find My Device actions |
| **FCM** | Firebase Cloud Messaging — push notification transport |
| **FMDN** | Find My Device Network — Google's crowdsource tracker protocol |
| **EIK** | Ephemeral Identity Key — 32-byte root key for tracker crypto |
| **EID** | Ephemeral Identifier — rotating BLE address derived from EIK |
| **Beacon Actions** | GATT characteristic for direct tracker commands (ring, UTP) |
| **GATT** | Generic Attribute Profile — BLE protocol for read/write operations |
| **ADM** | Android Device Management — Google auth token type |
| **AAS** | Android Account Sign-In — Google auth token type |
| **Bermuda** | Third-party HA integration for BLE room presence |
| **bleak** | Python BLE library used by HA's bluetooth integration |
