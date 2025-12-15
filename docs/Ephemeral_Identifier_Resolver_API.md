# Google Find My Device – Ephemeral Identifier Resolver API

This document describes the **Ephemeral Identifier (EID) Resolver API** exposed by the `googlefindmy` Home Assistant integration. It is intended for developers of other integrations – for example, a local BLE–scanner integration such as **Bermuda** – that want to map **Find My Device Network** (FMDN) ephemeral identifiers to specific Home Assistant devices.

---

## Overview

Some Google Find My–compatible devices periodically broadcast **rotating BLE identifiers** (EIDs). These identifiers:

* Change on a fixed rotation period.
* Are derived from a per-device secret key.
* Are not directly stable device identifiers.

The `googlefindmy` integration already knows:

* Which Find My devices exist,
* Their identity keys (for EID derivation),
* Which devices are enabled and not ignored in Home Assistant.

The **EID Resolver API** exposes this knowledge to other integrations so that, given a raw EID from a BLE scan, you can efficiently resolve it to:

* A **Home Assistant device registry ID** (`device_id`), and
* The owning **config entry** and **canonical integration ID**.

The resolver precomputes EIDs for the **previous, current, and next rotation window** for all active trackers and keeps them in an in-memory lookup table. Resolution is a constant-time map lookup and never performs cryptographic work on the hot path.

---

## Technical Specification

This section details the cryptographic construction and frame layout of the EIDs handled by the resolver. This information is critical for BLE scanner implementations to correctly extract the payload before calling the resolver.

### Supported EID Formats

The resolver supports the **Find Hub Network Accessory (FHNA)** specification and handles various payload formats transparently.

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

#### 3. EID Derivation Algorithm

The EID is mathematically defined as the **x-coordinate ($R_x$)** of an Elliptic Curve point $R$.

1.  **PRF Input (Table 10):** A 32-byte buffer is constructed containing the Timestamp ($TS$) and the constant $K=10$ (rotation period).
    * `bytes[0..10] = 0xFF`
    * `byte[11] = 0x0A` (K=10)
    * `bytes[12..15] = TS` (Big-Endian `u32`, with the lowest 10 bits cleared)
    * `bytes[16..26] = 0x00`
    * `byte[27] = 0x0A` (K=10)
    * `bytes[28..31] = TS` (Same $TS$)

2.  **Encryption:** This buffer is encrypted using **AES-ECB-256** with the ephemeral identity key. The result is a 256-bit scalar $r'$.

3.  **Reduction:** $r = r' \pmod n$ (where $n$ is the order of the chosen curve).

4.  **Multiplication:** Calculate the point $R = r \cdot G$ (where $G$ is the curve's generator point).

5.  **Result:** The advertised EID is the x-coordinate of $R$ ($R_x$).
    * For SECP160R1: 20 bytes.
    * For SECP256R1: 32 bytes.

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
