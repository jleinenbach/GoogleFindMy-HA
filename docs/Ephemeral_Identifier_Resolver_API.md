# Google Find My Device – Ephemeral Identifier Resolver API

This document describes the **Ephemeral Identifier (EID) Resolver API** exposed by the `googlefindmy` Home Assistant integration. It is intended for developers of other integrations – for example, a local BLE scanner such as **Bermuda** – that want to map **Find My Device Network** (FMDN) ephemeral identifiers to specific Home Assistant devices.

---

## Stable device identifier (state API)

Third-party consumers must anchor on the `google_device_id` state attribute when they want to correlate Google Find My trackers with external data sources.

* **Why?** BLE MAC addresses and other transport identifiers rotate for privacy and are intentionally omitted from state. Relying on them causes duplicate devices whenever a tracker reboots or rotates.
* **What to use instead?** `google_device_id` is the stable, obfuscated identifier already used in the Home Assistant device registry. It does not change across reboots or rotation cycles.
* **Availability.** Every tracker entity publishes `google_device_id` in its state attributes alongside diagnostic metadata.

### Example: template usage

Use `google_device_id` as the join key when correlating Google Find My Device trackers with other data sources:

```jinja
{% set tracker_id = state_attr("device_tracker.find_my_keys", "google_device_id") %}
{% if tracker_id %}
  {{ "Found stable tracker ID: " ~ tracker_id }}
{% else %}
  {{ "Tracker not yet initialized" }}
{% endif %}
```

When deduplicating devices or wiring interoperability (for example, a Bermuda BLE scanner that resolves EIDs to trackers), prefer `google_device_id` over `mac`, `source_type`, or any other rotating attribute.

---

## Overview

Some Google Find My–compatible devices periodically broadcast **rotating BLE identifiers** (EIDs). These identifiers:

* Change on a fixed rotation period with device-chosen jitter.
* Are derived from a per-device secret key (EIK).
* Are not directly stable device identifiers.

The `googlefindmy` integration already knows:

* Which Find My devices exist.
* Their identity keys (wrapped or raw).
* Which devices are enabled and not ignored in Home Assistant.

The **EID Resolver API** exposes this knowledge to other integrations so that, given a raw EID from a BLE scan, you can efficiently resolve it to:

* A **Home Assistant device registry ID** (`device_id`).
* The owning **config entry** (`config_entry_id`).
* The device’s **canonical integration ID** (`canonical_id`).

The resolver precomputes EIDs for sliding windows around the current rotation (past, present, and future) to absorb rotation jitter and clock drift, then stores them in an in-memory lookup table. Resolution is a constant-time map lookup and never performs cryptographic work on the hot path.

---

## Python API Usage

### Accessing the resolver from Home Assistant

```python
from custom_components.googlefindmy.const import DATA_EID_RESOLVER, DOMAIN
from custom_components.googlefindmy.eid_resolver import EIDMatch, GoogleFindMyEIDResolver

bucket = hass.data.get(DOMAIN)
resolver: GoogleFindMyEIDResolver | None = None
if isinstance(bucket, dict):
    candidate = bucket.get(DATA_EID_RESOLVER)
    if isinstance(candidate, GoogleFindMyEIDResolver):
        resolver = candidate
```

### Resolving an EID

```python
if resolver:
    # eid_bytes is the raw BLE payload slice, e.g., bytes.fromhex("ab…")
    match: EIDMatch | None = resolver.resolve_eid(eid_bytes)
    if match:
        _LOGGER.debug(
            "Matched EID to device_id=%s (entry=%s, canonical=%s, offset=%ss, reversed=%s)",
            match.device_id,
            match.config_entry_id,
            match.canonical_id,
            match.time_offset,
            match.is_reversed,
        )
```

### Return value

`resolve_eid(eid_bytes: bytes) -> EIDMatch | None`

* `device_id` — Home Assistant device registry identifier.
* `config_entry_id` — Config entry owning the device.
* `canonical_id` — Stable device identifier within the integration.
* `time_offset` — Seconds between the rotation timestamp used for the match and the current wall time (negative for past windows, positive for future/precomputed windows).
* `is_reversed` — `True` when the advertisement used reversed byte order; the resolver stores both forward and reversed variants.

### Example usage inside another integration (Bermuda)

```python
# custom_components/bermuda/ble_scanner.py
from __future__ import annotations

from typing import Any

from custom_components.googlefindmy.const import DATA_EID_RESOLVER, DOMAIN
from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver

async def async_process_eid(hass, eid_bytes: bytes) -> dict[str, Any] | None:
    bucket = hass.data.get(DOMAIN)
    if not isinstance(bucket, dict):
        return None

    resolver = bucket.get(DATA_EID_RESOLVER)
    if not isinstance(resolver, GoogleFindMyEIDResolver):
        return None

    match = resolver.resolve_eid(eid_bytes)
    if match is None:
        return None

    return {
        "device_id": match.device_id,
        "config_entry_id": match.config_entry_id,
        "canonical_id": match.canonical_id,
    }
```

---

## Technical Specification

This section details the cryptographic construction and frame layout of the EIDs handled by the resolver. This information is critical for BLE scanner implementations to correctly extract the payload before calling the resolver.

### Supported EID Formats

The resolver supports the **Find Hub Network Accessory (FHNA)** specification and handles various payload formats transparently, including explicit `EidVariant` profiles from `custom_components/googlefindmy/FMDNCrypto/eid_generator.py`.

#### 1. Frame Types

The Frame Type (Packet Prefix) is located at **Octet 7** of the Service Data.

* **0x40**: Legacy / 160-bit frame.
* **0x41**: Modern / 256-bit frame.

#### 2. EID Lengths & Structure

* **160-bit Variant (BLE 4.x Legacy):**
  * **EID (20 bytes):** Octets 8–27.
  * **Hashed Flags (1 byte, optional):** Octet 28.
  * *Curve used:* SECP160R1.

* **256-bit Variant (BLE 5 Extended):**
  * **EID (32 bytes):** Octets 8–39.
  * **Hashed Flags (1 byte, optional):** Octet 40.
  * *Curve used:* SECP256R1 (NIST P-256).

#### 3. EID Variants (explicit)

The resolver iterates all supported formats unless a per-device lock is present:

* `LEGACY_SECP160R1_X20_BE` — 20-byte legacy EID, big-endian scalar on secp160r1.
* `MODERN_P256_X32_BE` — 32-byte modern EID, big-endian scalar on P-256.
* `MODERN_P256_X20_TRUNC_BE` — 20-byte truncated x-coordinate derived from P-256 (big-endian scalar).
* `MODERN_P256_X32_LE_SCALAR` — 32-byte EID with little-endian scalar input on P-256.
* `MODERN_P256_X20_TRUNC_LE` — 20-byte truncated x-coordinate derived from P-256 with little-endian scalar input.

The resolver also records both forward and reversed advertisements so scanners do not need to normalize byte order.

#### 4. EID Derivation Algorithm

The EID is mathematically defined as the **x-coordinate ($R_x$)** of an Elliptic Curve point $R$.

1. **PRF Input (Table 10):** A 32-byte buffer is constructed containing the Timestamp ($TS$) and the constant $K=10$ (rotation period).
   * `bytes[0..10] = 0xFF`
   * `byte[11] = 0x0A` (K=10)
   * `bytes[12..15] = TS` (Big-Endian `u32`, with the lowest 10 bits cleared)
   * `bytes[16..26] = 0x00`
   * `byte[27] = 0x0A` (K=10)
   * `bytes[28..31] = TS` (Same $TS$)

2. **Encryption:** This buffer is encrypted using **AES-ECB-256** with the ephemeral identity key. The result is a 256-bit scalar $r'$.

3. **Reduction:** $r = r' \pmod n$ (where $n$ is the order of the chosen curve), with projection rules that match each `EidVariant`.

4. **Multiplication:** Calculate the point $R = r \cdot G$ (where $G$ is the curve's generator point).

5. **Result:** The advertised EID is the x-coordinate of $R$ ($R_x$).
   * For SECP160R1: 20 bytes.
   * For SECP256R1: 32 bytes (truncated to 20 bytes for the `*_X20_*` variants).

### Key material handling (wrapped vs. raw EIKs)

Cloud responses may deliver **wrapped identity keys** (for example, 60-byte envelopes) instead of the 32-byte raw EIK required for cryptography. The resolver automatically:

* Fetches owner and shared wrapping keys.
* Tries Fast Pair MCU bit-flip handling when appropriate.
* Unwraps AES-GCM envelopes to the 32-byte raw EIK before EID generation.

Callers of `resolve_eid` do **not** need to distinguish between wrapped and raw EIK inputs; the resolver normalizes them internally.

### Robustness and rotation windows

To tolerate real-world behavior (rotation jitter, clock drift, DST shifts, and timebase differences between devices that track Unix time vs. “seconds since provisioning”), the resolver:

* Precomputes **previous, current, and future** rotation windows around each anchor (Unix, `pair_date`, `secrets_creation_date`).
* Expands the window count based on device age (drift), timezone offsets, and configurable safety nets.
* Caches both forward and reversed advertisements for each variant.

This sliding-window approach makes `resolve_eid` resilient to sub-minute jitter and larger offsets without requiring callers to schedule their own precomputation.

---

## Before you begin

### Requirements

To use this API, your integration must run in the same Home Assistant instance as `googlefindmy` and meet these requirements:

* `googlefindmy` integration version **1.7.0-3 or later** (EID resolution support).
* The user has set up at least one Google Find My account and devices.
* You are able to obtain the **raw EID as bytes** from your BLE stack (or convert from a hex string).

### Recommended Home Assistant manifest settings (for Bermuda)

If your integration wants to use EID resolution when available, but remain optional, declare an **ordering dependency** on `googlefindmy`:

```jsonc
// custom_components/bermuda/manifest.json
{
  "domain": "bermuda",
  "name": "Bermuda BLE Scanner",
  // Ensure googlefindmy (if installed) is initialized before you access hass.data["googlefindmy"]
  "after_dependencies": ["googlefindmy"],
  // Do NOT list it in "dependencies" unless you want to hard-require it.
  "dependencies": []
}
```
