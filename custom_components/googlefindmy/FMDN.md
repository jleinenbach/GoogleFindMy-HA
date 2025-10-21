# FMDN / Find Hub — Technical Reference for Implementers (RAG-friendly)
Version: 2025-10-21 • Status: synthesis of public docs + peer-reviewed research; RE-inferred parts are labeled

Anchors: every section carries a stable token `⟦…⟧` for RAG retrieval.
IDs: section IDs like `S0`, `S3.2` are stable for internal linking.

---

## Table of Contents (RAG Index)
- S0. Overview & Roles — `⟦OVERVIEW⟧`
- S1. Cryptographic Primitives & Keys — `⟦CRYPTO_PRIMITIVES⟧`
- S2. Provisioning (Fast Pair, EIK) — `⟦PROV_FLOW⟧`
- S3. BLE Advertising & EID — `⟦BLE_EID⟧`
- S4. Finder Device Behavior (Scan, Upload, Throttling) — `⟦FINDER_BEHAV⟧`
- S5. Server-side Behavior (Aggregation & Modes) — `⟦SERVER_BEHAV⟧`
- S6. Owner App (Fetch, Decrypt, Aggregate) — `⟦OWNER_BEHAV⟧`
- S7. Unwanted-Tracking Protections (UT / DULT) — `⟦UT_PROTECTION⟧`
- S8. Message Formats & Fields — `⟦DATA_FORMATS⟧`
- S9. State Machines & Failure Models — `⟦STATE_MACHINES⟧`
- S10. Security Notes & Best Practices — `⟦SECURITY_NOTES⟧`
- S11. Open Points / Not Publicly Specified — `⟦OPEN_POINTS⟧`
- Appendix A. Pseudocode (EID, E2EE, Upload) — `⟦APPX_PSEUDOCODE⟧`
- References (URLs)

---

## S0. Overview & Roles `⟦OVERVIEW⟧`
**Roles.** *Provider* (tracker/headphones), *Seeker* (owner’s phone), *Finder* (any Android device participating), *Backend* (Google cloud services), *Owner App* (Find Hub / Find My Device UI).
**Network model.** FMDN is an **offline finding network (OFN)**: Providers broadcast BLE beacons; Finders contribute encrypted location reports to the backend; only the Owner can decrypt results (end-to-end encryption, E2EE).
**Privacy defaults.** Participation by Finders is opt-in; aggregation and rate-limits reduce live-tracking risks.

---

## S1. Cryptographic Primitives & Keys `⟦CRYPTO_PRIMITIVES⟧`
- **EIK (Ephemeral Identity Key, 32 B).** Per-provider master secret, generated on the Seeker and never uploaded in clear.
- **Derived short keys.** From EIK, implementations derive distinct 8-byte keys for recovery/ring/UT (purpose-bound) via a KDF (conceptual: `KDF(EIK || domain)`).
- **Location encryption.** A Finder encrypts location to the Provider’s rotating public identity (EID) using **ECDH** → **HKDF-SHA-256** → **AEAD** (e.g., AES-EAX or comparable) so that only the Owner can decrypt on-device.
- **Note (RE-inferred, ≥90% confidence).** Exact curve/AEAD choices vary by accessory generation and spec version; see Appendix A for a consistent, implementable pattern.

---

## S2. Provisioning (Fast Pair, EIK) `⟦PROV_FLOW⟧`
**Fast Pair basis.** Provider and Seeker establish an **Account Key** during Fast Pair. FMDN extends this with a secured GATT flow to set/clear/read the **EIK** under user consent.
**Operations (conceptual):**
- `SetEIK` (write EIK encrypted under Account Key; requires proximity & consent),
- `ReadEIK` (only with explicit user consent on the device),
- `ClearEIK` (remove EIK when unlinking or factory reset).
**Errors (typical):** unauthenticated, invalid value/length, missing consent.
**Separation of duties.** Recovery/ring/UT short keys may be server-side; **EIK remains device/owner-side** and is not shared in clear with the backend.

---

## S3. BLE Advertising & EID `⟦BLE_EID⟧`
**Advertising frame.** Service UUID (Fast Pair extension); payload includes:
- **EID** (20 or 32 bytes, rotating identity),
- **UT byte/state**,
- **Hashed flags** (battery/UT status bits obfuscated; only Owner decodes).
**Rotation.** EID rotates on a fixed cadence (≈ 1024 s typical) with jitter; MAC randomization further limits correlation, especially in UT mode.
**EID derivation (high-level).** A counter derived from time since provisioning is combined with constants and transformed by a PRF keyed by EIK; the output is mapped to a public identity used for ECDH with Finders (see Appendix A).

---

## S4. Finder Device Behavior (Scan, Upload, Throttling) `⟦FINDER_BEHAV⟧`
**Scanning.** Google Play services scan BLE even when the screen is off. Matches are made by truncated EID prefixes.
**Upload gating (heuristics).** To avoid noise and stalking risks, a Finder delays uploads unless **distance increased**, **accuracy improved**, or **sufficient time** has passed. Screen-off and battery-saver modes add extra delays.
**Encrypted upload.** The Finder serializes location (e.g., `lat/lon` scaled integers, optional altitude, accuracy, timestamp), encrypts with the EID public identity (ECDH→KDF→AEAD), and POSTs over HTTPS to an upload endpoint; client integrity attestation headers may be included. Minimal metadata includes truncated EID, time, accuracy, UT state.

---

## S5. Server-side Behavior (Aggregation & Modes) `⟦SERVER_BEHAV⟧`
**Default: High-Traffic Areas.** Owners see **aggregated** locations (e.g., require multiple independent Finder sources) rather than single-Finder points, protecting private places and Finder anonymity.
**Opt-in: In All Areas.** Owners may opt into a mode that allows more direct location display (subject to Finder participation and policy).
**Caching & purge.** Aggregated reports and single-point reports use different retention and rounding rules; timestamps are rounded (e.g., 10-minute buckets).
**Rate-limiting.** Both Finder uploads and Owner fetches are throttled.

---

## S6. Owner App (Fetch, Decrypt, Aggregate) `⟦OWNER_BEHAV⟧`
**Delivery model.** The Owner app receives identifiers via push (e.g., FCM) and fetches encrypted payloads from the backend.
**On-device decryption.** The app reconstructs the ECDH shared secret corresponding to each EID rotation, derives the AEAD key, decrypts, and aggregates ≥ N reports in High-Traffic mode; in direct mode it may show the last known location.
**Device migration.** EIK migration to a new phone uses OS-level protected backup/restore mechanisms (e.g., secrets derived from the lock screen factor), so Google does not see EIK in clear.

---

## S7. Unwanted-Tracking Protections (UT / DULT) `⟦UT_PROTECTION⟧`
**Provider UT mode.** A Provider can enter UT mode: faster/randomized MAC rotation, clear UT state signaling in the advert, preserved EID rotation cadence.
**Platform alerts & pauses.** Android/iOS UT alerts (per the cross-platform DULT work) notify users if an unfamiliar tracker travels with them; Android can pause uploads from the finder-side for a window to reduce stalking risk.
**Private Set Membership (principle).** Some UT checks can use PSM-style techniques to test membership without revealing identities (design principle; usage depends on platform version).

---

## S8. Message Formats & Fields `⟦DATA_FORMATS⟧`
**BLE advert (conceptual):**
```

Service: Fast Pair (FMDN extension)
Payload: EID(20|32) | UT_state | hashed_flags
Rotation: cadence ≈ 1024 s + jitter; MAC randomized (esp. UT)

```
**Encrypted upload (finder → backend):**
```

Fields (pre-encryption): lat_s32e7 | lon_s32e7 | alt? | acc | ts | truncatedEID(10) | UT_state
Crypto: ECDH(EID_pub, eph_priv) → HKDF → AEAD(plaintext, nonce, AAD?)
Transport: HTTPS POST; includes integrity/attestation headers (implementation-defined)

```
**GATT provisioning (provider ↔ seeker):**
```

Ops: SetEIK | ReadEIK(consent) | ClearEIK
Protections: AccountKey-bound encryption; user proximity flow; explicit consent
Errors: unauthenticated | invalid value/length | missing consent

```

---

## S9. State Machines & Failure Models `⟦STATE_MACHINES⟧`
**Provisioning:**
```

Unpaired → FastPair(AccountKey) → Provisionable → SetEIK → Beaconing(EID rotation)
Failures: stale nonce, wrong length, no user consent

```
**Finder upload path:**
```

Scan → Match(EIDtrunc) → Gate(distance/accuracy/time) → Encrypt → Upload → Ack/Retry

```
**Owner retrieval path:**
```

PushHint → FetchEncrypted → Decrypt → Aggregate(≥N) → RoundTimestamp → PurgeOld

````

---

## S10. Security Notes & Best Practices `⟦SECURITY_NOTES⟧`
- **EIK never leaves the owner’s trust boundary** in clear; short derived keys can be server-side but are purpose-scoped.
- **Prefer stronger curves/AEAD where supported** (e.g., P-256/X25519; AES-GCM/EAX). Accessory constraints may dictate choices—follow the current spec revision.
- **Mitigate live-tracking risks** with aggregation-by-default, timestamp rounding, rate-limits, and UT pause windows.
- **UI/Policy.** Keep “In All Areas” as an explicit opt-in for owners; document privacy trade-offs.
- **Logging.** Never log plaintext coordinates or full tokens; mask IDs.

---

## S11. Open Points / Not Publicly Specified `⟦OPEN_POINTS⟧`
- **Exact REST endpoints and headers** are not fully public; reverse-engineered names may change. Treat client attestation headers as implementation-defined.
- **On-device UT heuristics** (trigger thresholds, durations) evolve with OS releases and are not exhaustively documented in one place.
- **Accessory generations** may differ in EID length, curves, and advert fields; consult the spec revision applicable to your hardware.

---

## Appendix A — Pseudocode (Implementable Patterns) `⟦APPX_PSEUDOCODE⟧`

### A1. EID derivation (provider)
```text
input: EIK (32B), rotation_exponent K (e.g., 10), t = seconds_since_provisioning

1  ts = u64be(t)
2  B  = build_struct(flags=0xFF11, K, ts, K, ts)    # spec-driven layout; constant fields per revision
3  R  = PRF_AES_256(EIK, B)                         # KDF/PRF keyed by EIK
4  r  = int_be(R) mod curve_order
5  EID_public = r * G                               # public identity point (exported in compressed form if supported)
6  advertise EID_public || UT_state || hashed_flags(EIK, local_status_bits)
````

### A2. Finder-side E2EE location upload

```text
input: EID_public, loc_pb = serialize(lat_s32e7, lon_s32e7, alt?, acc, ts)

1  (de, Pe) = ephemeral_keypair()
2  S  = ECDH(de, EID_public)
3  k  = HKDF_SHA256(S, info="FMDN-Loc/v1")
4  nonce = concat( last8(x(Pe)), first8(x(EID_public)) )   # 16B example
5  C = AEAD_Encrypt(key=k, nonce=nonce, plaintext=loc_pb, aad=truncatedEID)
6  POST Upload { Pe.x, C, tag, truncatedEID, ts, acc, ut_state, client_attestation? }
```

### A3. Upload gating (heuristics)

```text
if not (distance_grew or accuracy_improved or elapsed >= threshold):
    backoff(delay=Δ)
if screen_off: backoff(min_delay=5m); if battery_saver: backoff(≥15m)
enqueue_secure_upload()
```

### A4. Owner fetch & aggregation

```text
on PushHint(ids):
  for id in ids:
     blob = fetch_ciphertext(id)
     loc  = AEAD_Decrypt(k_derived_by_ECDH, blob)
  if mode == "High-Traffic": show aggregate(loc, min_sources=4)
  else: show last_known(loc)
  round_timestamps(step=10m)
  purge_history(policy)
```

---

## References (URLs)

* PoPETs 2025 paper on Google’s Find My Device network: [https://petsymposium.org/popets/2025/popets-2025-0147.pdf](https://petsymposium.org/popets/2025/popets-2025-0147.pdf)
* Google Security Blog (Find Hub/FMDN privacy & security): [https://security.googleblog.com/2024/04/find-my-device-network-security-privacy-protections.html](https://security.googleblog.com/2024/04/find-my-device-network-security-privacy-protections.html)
* Google Developers — Find Hub Network (Fast Pair extension): [https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn](https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn)
* Google Developers — Partner integration (Find Hub): [https://developers.google.com/nearby/fast-pair/landing-page-find-hub](https://developers.google.com/nearby/fast-pair/landing-page-find-hub)
* Android Help — Find unknown trackers: [https://support.google.com/android/answer/13658562](https://support.google.com/android/answer/13658562)
* IETF DULT (working group & drafts): [https://datatracker.ietf.org/group/dult/](https://datatracker.ietf.org/group/dult/) and [https://www.ietf.org/archive/id/draft-detecting-unwanted-location-trackers-01.html](https://www.ietf.org/archive/id/draft-detecting-unwanted-location-trackers-01.html)
* Android overview (marketing/feature context): [https://www.android.com/learn-find-hub/](https://www.android.com/learn-find-hub/)
