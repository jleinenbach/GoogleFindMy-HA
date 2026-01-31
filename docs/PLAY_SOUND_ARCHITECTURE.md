# Play Sound Architecture

## Overview

The Play Sound feature allows users to ring a tracked device (FMDN tag, headphones,
Android phone) from Home Assistant. This document describes the current cloud-only
implementation, the upstream state (which is identical), and the future architecture
for direct BLE ringing.

**Key fact:** Neither upstream (leonboe1/GoogleFindMyTools) nor this fork parse the
Nova API response. Both implementations are fire-and-forget. The response format is
undocumented by Google and has never been decoded by any known open-source project.

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
       |  POST to https://android.googleapis.com/nova/{NOVA_ACTION_API_SCOPE}
       v
Google Cloud Server
       |  routes command via FCM push notification to device
       v
Target Device rings
       |  (device sends FCM push back to confirm — see "Two Confirmation Paths")
       v
fcm_receiver_ha.py  _handle_notification_async()
       |  receives DeviceUpdate protobuf via FCM
       |  BUT: no callback registered for sound events → falls through
       v
(Sound confirmation silently lost)
```

### Key Files

| File | Responsibility |
|------|---------------|
| `button.py` | `GoogleFindMyPlaySoundButton` / `StopSoundButton` — HA button entities |
| `api.py` | `async_play_sound()` / `async_stop_sound()` — entry point, FCM token resolution |
| `sound_request.py` | `create_sound_request()` — pure protobuf builder (no I/O) |
| `nbe_execute_action.py` | `create_action_request()` + `serialize_action_request()` — protobuf envelope |
| `start_sound_request.py` | `async_submit_start_sound_request()` — Nova submission |
| `stop_sound_request.py` | `async_submit_stop_sound_request()` — Nova submission |
| `nova_request.py` | `async_nova_request()` — HTTP transport, auth, retry |
| `fcm_receiver_ha.py` | FCM push receiver — handles ALL incoming notifications |
| `_cli_helpers.py` | CLI FCM token resolution for standalone testing |

### Protobuf Structure (Request Only — No Response Defined)

```protobuf
// DeviceUpdate.proto — only request messages exist
message ExecuteActionRequest {
    ExecuteActionScope scope = 1;              // SPOT_DEVICE + canonicId
    ExecuteActionType action = 2;              // startSound or stopSound
    ExecuteActionRequestMetadata requestMetadata = 3;  // requestUuid, gcmRegistrationId
}

message ExecuteActionType {
    ExecuteActionLocateTrackerType locateTracker = 30;
    ExecuteActionSoundType startSound = 31;    // component = DEVICE_COMPONENT_UNSPECIFIED
    ExecuteActionSoundType stopSound = 32;
}

// NO ExecuteActionResponse message exists — not in upstream, not here, not in
// Google's public proto repositories (googleapis/googleapis).
```

### Two Potential Confirmation Paths (Neither Currently Used)

There are two distinct mechanisms that could confirm a ring command succeeded:

#### Path A: Nova HTTP Response (unknown format)

```
nova_request.py:1407-1408
    if status == HTTP_OK:
        return cast(bytes, content).hex()   # ← raw hex, NEVER PARSED
```

Google returns a protobuf body on HTTP 200. Its format is unknown:
- No `ExecuteActionResponse` proto defined anywhere (upstream, Google, or us)
- `google.internal.spot.v1.SpotService` is deliberately excluded from public APIs
- `_decode_error_response()` only runs for non-200 statuses
- `NovaLogicError` is defined but never raised (dead code)
- Upstream (leonboe1) also ignores this response — return value is discarded

#### Path B: FCM Push Callback (infrastructure exists, not wired for sound)

The LocateTracker flow uses FCM callbacks:
1. Register callback via `fcm_receiver.async_register_for_location_updates(canonic_id, cb)`
2. Submit Nova request
3. Wait for FCM push containing `DeviceUpdate` protobuf with matching `requestUuid`

For sound events, **no callback is registered.** The FCM push arrives via
`_handle_notification_async()`, but with no registered callback, it falls through
to `_process_background_update()` which tries to decode it as a location response.

The `DeviceUpdate` protobuf includes `ExecuteActionRequestMetadata.requestUuid` which
could be matched against the UUID from `start_sound_request()` for correlation.

### What This Means

| What we know | What we don't know |
|---|---|
| HTTP 200 = Google accepted the HTTP request | Whether the command reached the device |
| Response body is non-empty protobuf | The response protobuf schema |
| FCM push arrives after successful commands | Whether sound-specific FCM pushes differ from location pushes |
| `requestUuid` is sent and echoed in FCM | Whether the HTTP response also echoes it |

### Upstream Parity

**Our code and upstream are functionally identical for PlaySound:**

| Aspect | Upstream (leonboe1) | This Fork |
|--------|---------------------|-----------|
| Nova HTTP response | `return response.content.hex()` — caller discards return value | `return cast(bytes, content).hex()` — caller stores but ignores `_response_hex` |
| Response parsing | None | None |
| FCM callback for sound | `lambda x: print(x)` (prints raw hex to stdout) | Not registered |
| BLE GATT ring code | **None** — only ring key derivation + registration | None |
| `ExecuteActionResponse` proto | Not defined | Not defined |

**Upstream has no BLE ring implementation.** The ring key derivation in
`key_derivation.py` and the `ringKey` field in `RegisterBleDeviceRequest` are used
during device registration to tell Google the ring key. No code exists to use that
key for direct BLE GATT writes. The BLE ring protocol was independently attempted by
community members in GitHub Issue #66.

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
    v  FCM Push (device confirms back via FCM)
Device
```

Token management is handled by `AsyncTTLPolicy` in `nova_request.py` with:
- Proactive refresh before expiry
- Entry-scoped caching (multi-account safe)
- Automatic 401 recovery with multi-step retry (ADM refresh → AAS+ADM refresh → cooldown)

---

## BLE Ring Protocol (FMDN Specification)

### FMDN Beacon Actions Characteristic

The FMDN specification at `developers.google.com/nearby/fast-pair/specifications/extensions/fmdn`
defines a GATT characteristic for direct device control:

- **UUID:** `FE2C1238-8366-4814-8EB0-01DE32100BEA` (Beacon Actions)
- **Protocol:** Read nonce → compute auth → write command → read notification

| Data ID | Operation | Description |
|---------|-----------|-------------|
| `0x05` | Ring | Start ringing the tracker |
| `0x06` | Read ringing state | Check if currently ringing |

**Important:** The FMDN spec documents only the BLE-level protocol. It says nothing
about server-side APIs, Nova endpoints, or cloud ring commands.

### BLE Ring Protocol Steps

```
Step 1: Read Beacon Actions characteristic
        → Receive 8-byte random nonce from tracker

Step 2: Compute auth key
        ring_key = SHA256(EIK || 0x02)[:8]     # 8-byte truncated
        auth_data = HMAC-SHA256(ring_key, nonce || data_id=0x05 || addl_data)[:8]

Step 3: Write to Beacon Actions characteristic
        Payload: [data_id=0x05] [data_len] [8-byte auth_key] [op_mask(1B)] [timeout(2B BE)] [volume(1B)]
        Where: addl_data = op_mask + timeout + volume = 4 bytes
               data_len = len(auth_key) + len(addl_data) = 8 + 4 = 12

Step 4: Read notification (Table 6 in FMDN spec)
        → Ring state byte:
            0x00 = Started successfully
            0x01 = Failed (auth or hardware)
            0x02 = Stopped (timeout)
            0x03 = Stopped (button press)
            0x04 = Stopped (GATT command)
        → Components bitmask + remaining time
```

### Community BLE Ring Attempt — Detailed Analysis (Issue #66)

Source: https://gist.github.com/mik-laj/4c1c363391115ccb14ee856a9c1c12a1

mik-laj published a standalone `bleak`-based ring script (`ring_nearby.py`). The
script scans for FMDN advertisements, connects via GATT, and writes ring commands.
DefenestratingWizard identified **two bugs** (both in `data_len` handling):

#### Bug 1: Payload `data_len` field

```python
# BUG (mik-laj):
payload = bytes([DATA_ID_RING, len(addl)]) + auth8 + addl
#                               ^^^^^^^^  = 4 (only addl, missing auth key!)

# CORRECT:
payload = bytes([DATA_ID_RING, len(auth8) + len(addl)]) + auth8 + addl
#                               ^^^^^^^^^^^^^^^^^^^^^^^^  = 8 + 4 = 12
```

#### Bug 2: HMAC input `data_len` (causes wrong auth key!)

```python
# BUG (mik-laj):
data_len = len(addl)       # = 4, but HMAC sees wrong length → wrong auth

# CORRECT:
data_len = len(addl) + 8   # auth key (8B) IS counted in data_len for HMAC too
```

Both bugs together cause ATT Error `0x81`. Fixing only one is insufficient because
`data_len` appears in both the wire format AND the HMAC input — a wrong value
produces both a malformed payload and an incorrect authentication.

#### Verified Wire Format (from Wireshark capture of real Find Hub app)

```
Ring command (successful, captured from official Google Find Hub app):

  05 0c a7 25 03 f0 6a 9d d4 2a ff 02 58 00
  ── ── ──────────────────────── ── ───── ──
  │  │           │                │    │    │
  │  │           │                │    │    └── volume (0x00 = default)
  │  │           │                │    └── timeout (0x0258 = 600 deciseconds = 60s)
  │  │           │                └── op_mask (0xFF = ring all components)
  │  │           └── 8-byte HMAC-SHA256 one-time auth key
  │  └── data_len = 0x0c = 12 = 8 (auth) + 4 (addl)
  └── data_id = 0x05 (Ring)

Nonce/challenge read (from Beacon Actions characteristic):

  01 f3 be eb 39 9d 61 cf a0
  ── ────────────────────────
  │           │
  │           └── 8-byte random nonce
  └── proto_major = 0x01
```

#### Corrected HMAC Computation

```python
def make_auth(ring_key, proto_major, nonce8, data_id, addl):
    data_len = len(addl) + 8     # MUST include auth key length
    msg = bytes([proto_major]) + nonce8 + bytes([data_id, data_len]) + addl
    return hmac.new(ring_key, msg, hashlib.sha256).digest()[:8]
```

#### Corrected Payload Construction

```python
def build_ring_message(ring_key, nonce8, proto_major,
                       op_mask=0xFF, timeout_s=60.0, volume=0x00):
    t_ds = min(int(timeout_s * 10), 6000)
    addl = bytes([op_mask]) + struct.pack(">H", t_ds) + bytes([volume])
    auth8 = make_auth(ring_key, proto_major, nonce8, 0x05, addl)
    return bytes([0x05, len(auth8) + len(addl)]) + auth8 + addl
```

#### Open Question: Ring Key Derivation

mik-laj reported that keys from `FMDNOwnerOperations.generate_keys()` did not match
the keys observed in the Wireshark capture. He was uncertain whether the EIK (after
AES decryption with the owner key) or the raw encrypted identity key should be used.

Our code derives: `ring_key = SHA256(decrypted_EIK || 0x02)[:8]`, which matches the
FMDN spec. mik-laj may have used the encrypted key by mistake (he logged both).
DefenestratingWizard's fix resolved the `data_len` bug but did not confirm whether
the ring key derivation was also corrected — the issue remains open.

### Why Neither Codebase Has BLE Ringing

1. **Upstream is CLI-focused** — designed for OAuth + Nova API interactions, not BLE
2. **This fork is HA-focused** — HA servers are typically not BLE-adjacent to trackers
3. **Ring key is registered, not used** — `key_derivation.py` derives the ring key
   and `create_ble_device.py` sends it to Google during registration, but no code
   uses it for local BLE commands
4. **Community attempt unfinished** — mik-laj's script has bugs, no confirmed success

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
