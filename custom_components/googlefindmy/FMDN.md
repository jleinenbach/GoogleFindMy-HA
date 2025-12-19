# FMDN / Find Hub — Technical Reference for Implementers (RAG-friendly)

Version: **2025-12-19** • Status: **evidence-first synthesis** (public docs + peer-reviewed research + explicitly labeled implementation observations); **hypotheses are labeled**

Anchors: every section carries a stable token `⟦…⟧` for RAG retrieval.
IDs: section IDs like `S0`, `S3.2` are stable for internal linking.

---

## Table of Contents (RAG Index)

* S0. Overview & Roles — `⟦OVERVIEW⟧`
* S1. Cryptographic Primitives & Key Taxonomy — `⟦CRYPTO_PRIMITIVES⟧`
* S2. Provisioning (Fast Pair + EIK lifecycle) — `⟦PROV_FLOW⟧`
* S3. BLE Advertising & EID (timebase, jitter, curves) — `⟦BLE_EID⟧`
* S4. Finder Device Behavior (scan, upload, throttling) — `⟦FINDER_BEHAV⟧`
* S5. Server-side Behavior (aggregation & modes) — `⟦SERVER_BEHAV⟧`
* S6. Owner App / Client (fetch, unwrap, decrypt, aggregate) — `⟦OWNER_BEHAV⟧`
* S7. Unwanted-Tracking Protections (UT / DULT) — `⟦UT_PROTECTION⟧`
* S8. Message Formats & Fields — `⟦DATA_FORMATS⟧`
* S9. State Machines & Failure Models — `⟦STATE_MACHINES⟧`
* S10. Security Notes & Best Practices — `⟦SECURITY_NOTES⟧`
* S11. Open Points / Not Publicly Specified — `⟦OPEN_POINTS⟧`
* Appendix A. Evidence-anchored Pseudocode — `⟦APPX_PSEUDOCODE⟧`
* References (URLs)

---

## S0. Overview & Roles `⟦OVERVIEW⟧`

**Roles.** *Provider* (tracker/headphones), *Seeker* (owner’s phone during setup), *Finder* (any participating Android device), *Backend* (Google services), *Owner Client* (Find Hub / Find My Device UI or compatible client).

**Network model.** The Find Hub / FMDN system is an **offline finding network**: Providers broadcast BLE beacons; Finders upload *end-to-end encrypted* location reports; only the Owner Client can decrypt and interpret those reports. (Böttger et al., 2025). ([Pet Symposium][1])

**Evidence note.** The public ecosystem contains both (a) **official partner documentation** (partly summarized publicly) and (b) **peer-reviewed measurements** of deployed behavior. When these differ, this document marks: **Specified** (official), **Measured** (peer-reviewed), **Observed in implementation logs** (your integration), and **Hypothesis** (reasonable but not publicly confirmed).

---

## S1. Cryptographic Primitives & Key Taxonomy `⟦CRYPTO_PRIMITIVES⟧`

### S1.1 Master secret: EIK / EIDK (32 B)

**EIK (Ephemeral Identity Key, 32 bytes).** Generated on the Seeker (phone) and provisioned to the Provider; used as the root secret for rotating identities. (Böttger et al., 2025; Google, n.d.). ([Pet Symposium][1])

> Terminology: some materials use “EIDK”. This document uses **EIK** for the 32-byte root secret.

### S1.2 Purpose-scoped derived keys (short keys)

**Specified (public partner summary):** the Find Hub accessory documentation describes multiple **8-byte keys** derived from the EIK for distinct purposes (e.g., e2ee, recovery, ring). The exact derivation method is not always fully described in public summaries; treat “domain separation” as required design intent. (Google, n.d.). ([Google for Developers][2])

**Implementation guidance:** keep a strict separation between:

* **EIK (32 B)** — root secret for identity/cryptography
* **Derived short keys (8 B)** — purpose-bound keys
* **Wrapped/transport forms** — encrypted containers that must be unwrapped before use

### S1.3 Curves and AEADs (Measured vs. Specified)

**Measured (peer-reviewed):**

* EID / E2EE key agreement uses **NIST P-160R1** (i.e., *secp160r1*) by default in all tested trackers. (Böttger et al., 2025). ([Pet Symposium][1])
* **P-256 EIDs** exist but were observed as rare and device-specific (e.g., Sony WH-1000XM5 in the study). (Böttger et al., 2025). ([Pet Symposium][1])
* Finder → Owner E2EE location encryption in the measured design uses **HKDF-SHA-256** and **AES-EAX-256** (as described by the paper’s reconstruction). (Böttger et al., 2025). ([Pet Symposium][1])

**Observed in your integration context:**

* Your logs show an **encryptedIdentityKey wrapper of 60 bytes** being **unwrapped to 32 bytes** before EID generation, and your note indicates **AES-GCM** is involved in that unwrapping path. (Observed in your logs + code path).
* Treat that wrapper as *not* the EIK; it is a **transport/container** that must be unwrapped.

---

## S2. Provisioning (Fast Pair + EIK lifecycle) `⟦PROV_FLOW⟧`

### S2.1 Setup flow (Evidence)

**Measured:** The Seeker generates the **EIK** and **sends it to the tracker** during setup; the tracker then uses that EIK for EID rotation. (Böttger et al., 2025). ([Pet Symposium][1])

**Specified (public partner summary):** Find Hub accessories extend Fast Pair with additional characteristics/procedures for Find Hub network participation. (Google, n.d.). ([Google for Developers][2])

### S2.2 Clear separation: EIK vs. account keys vs. wrappers

**Do not conflate:**

* **Account Key** (Fast Pair pairing secret; used to authenticate/protect provisioning operations)
* **EIK (32 B)** (root secret for rotating identity and related crypto)
* **encryptedIdentityKey (wrapper)** (cloud/API transport blob; must be unwrapped to obtain the 32-byte EIK in your integration)

### S2.3 Hypothesis: why a 60-byte wrapper exists

**Hypothesis (integration-driven):** Some backend/API responses deliver *wrapped* secrets (e.g., `encryptedUserSecrets.encryptedIdentityKey`) that embed an EIK encrypted under a device-/account-bound key and authenticated (e.g., AES-GCM). This enables cloud sync without exposing the EIK in clear to the backend.
This is consistent with the general principle that the EIK remains within the owner trust boundary, but the exact wrapper format is not publicly specified in the sources cited here.

---

## S3. BLE Advertising & EID `⟦BLE_EID⟧`

### S3.1 Advertising payload (Measured)

**Measured:** Standard BLE advertising includes:

* **UT byte/state** (observed values in practice often include `0x40` / `0x41`, semantics device-dependent)
* **EID (typically 20 bytes)** in standard advertising
* **Hashed flags** (status bits, obfuscated)
  (Böttger et al., 2025). ([Pet Symposium][1])

### S3.2 Rotation cadence and jitter (Measured)

**Rotation base:** EIDs rotate on a cadence of **2^K seconds**, commonly **K = 10 → 1024 seconds**. (Böttger et al., 2025). ([Pet Symposium][1])

**Jitter (important):** The rotation time is **randomized**: the next rotation time is a multiple of the rotation period **plus a random offset (measured recommendation: 1–204 seconds)**; the study reports all tested trackers implement this. (Böttger et al., 2025). ([Pet Symposium][1])

**Resolver requirement:** any resolver that precomputes expected EIDs must keep **previous / current / next** windows (“rolling window”) to tolerate overlap caused by jitter.

### S3.3 Timebase: “seconds since setup”, not Unix epoch (Measured)

**Measured:** EID derivation uses a **time counter since tracker setup/provisioning** (not wall-clock time). (Böttger et al., 2025). ([Pet Symposium][1])

**Measured (operational detail):** Backend results may include a time value aligned to the **last multiple of 1024 seconds since setup** (rounded down). (Böttger et al., 2025). ([Pet Symposium][1])

### S3.4 EID derivation (Measured; Table-10 PRF pattern)

**Measured algorithm (core idea):**

1. Compute `ts = seconds_since_setup`
2. Mask lower `K` bits to align to the rotation window
3. Build a **32-byte block** with fixed padding + `K` + masked timestamp repeated
4. Compute `seed = AES-ECB(EIK, block)`
5. Interpret `seed` as integer, reduce/modulo the curve order, compute `R = r·G`
6. **EID = x(R)**
   (Böttger et al., 2025). ([Pet Symposium][1])

**Curve selection (Measured):**

* Standard advertising corresponds to **20-byte EID** on **P-160R1 / secp160r1** (x-coordinate width).
* **32-byte EID** on **P-256** is possible but measured as rare/device-specific.
  (Böttger et al., 2025). ([Pet Symposium][1])

### S3.5 MAC rotation vs. UT mode (Measured)

**Measured:** During normal operation, the BLE MAC address rotates **with the EID** (~1024 s). If **UT mode** is enabled, MAC rotation is **reduced to one rotation per 24 hours**, while EID rotation remains unchanged. (Böttger et al., 2025). ([Pet Symposium][1])

---

## S4. Finder Device Behavior (scan, upload, throttling) `⟦FINDER_BEHAV⟧`

### S4.1 Scanning and match inputs (Measured)

**Measured:** Finder devices scan for these BLE beacons and can upload reports keyed by a **truncated EID (10 bytes)** (used as an index/selector in the protocol reconstruction). (Böttger et al., 2025). ([Pet Symposium][1])

### S4.2 Upload gating / throttling (Measured where stated; otherwise hypothesis)

**Measured (qualitative):** The paper describes client-side behaviors that reduce noise and abuse risk (e.g., delays and throttling considerations). (Böttger et al., 2025). ([Pet Symposium][1])

**Hypothesis (implementation pattern):** distance/accuracy/time heuristics and battery/screen-off backoffs can be expected in production clients, but exact thresholds may vary by OS release.

### S4.3 Double encryption and integrity (Measured)

**Measured:** Location reports are end-to-end encrypted for the owner, and additionally protected for transport with further encryption/signing mechanisms involving Google keys (e.g., P-256 + AES-GCM in the measured reconstruction), plus platform integrity mechanisms. (Böttger et al., 2025). ([Pet Symposium][1])

---

## S5. Server-side Behavior (aggregation & modes) `⟦SERVER_BEHAV⟧`

### S5.1 High-Traffic mode (Measured)

**Measured:** The backend only delivers “High Traffic” location reports to the owner client if **at least two reports from at least two distinct finder devices** are available; older locations may be purged and caching may keep only a limited number of recent points. (Böttger et al., 2025). ([Pet Symposium][1])

### S5.2 “In All Areas” mode (Measured)

**Measured:** “In All Areas” enables less-aggregated reporting behavior compared to High-Traffic, subject to policy and platform constraints. (Böttger et al., 2025). ([Pet Symposium][1])

---

## S6. Owner App / Client (fetch, unwrap, decrypt, aggregate) `⟦OWNER_BEHAV⟧`

### S6.1 Fetch + decrypt pipeline (Measured)

**Measured:** The owner device performs aggregation locally because the server cannot aggregate E2EE ciphertext. (Böttger et al., 2025). ([Pet Symposium][1])

### S6.2 Critical missing piece for implementers: **unwrap before EID** (Observed in your integration)

**Observed (your logs + code path):**

* `encryptedUserSecrets.encryptedIdentityKey` is **60 bytes** (Moto Tag example)
* Your implementation logs: **“Successfully unwrapped 60-byte EIK to 32 bytes”**
* Your note indicates this unwrapping involves **AES-GCM** (and is present in code and logs)

**Normative requirement for your codebase:**

> **EID derivation MUST consume the 32-byte raw EIK, never the 60-byte wrapper.**

**Documentation requirement (add explicitly):**

* Define the wrapper as **`EIK_WRAPPED` (bytes, length 60 observed)**
* Define the output as **`EIK` (32 bytes)**
* Make the unwrap function a first-class step in the Owner Client pipeline (before EID generation, before any derived keys)

**Hypothesis (format):** the 60-byte wrapper likely encodes `{nonce | ciphertext | tag}` or a closely related AEAD container; confirm by reading the decryptor module that performs AES-GCM and documenting the exact layout once verified by test vectors.

### S6.3 Owner-device encryption (Measured; separate from tracker E2EE)

**Measured:** In the paper’s reconstruction, owner-device location report encryption uses **SHA-256(EIK)** to derive a 32-byte key and then **AES-GCM** with a 12-byte IV and 128-bit tag (for owner location reporting). (Böttger et al., 2025). ([Pet Symposium][1])

---

## S7. Unwanted-Tracking Protections (UT / DULT) `⟦UT_PROTECTION⟧`

**Public product/security communications:** Google describes unwanted-tracking protections and cross-platform alerting concepts (with ecosystem coordination). (Google, 2024). ([Google Online Security Blog][3])

**User-facing behavior:** Android provides functionality to detect unknown trackers and take actions (scan, alerts, etc.). (Google, n.d.). ([Pet Symposium][4])

**Measured (protocol behavior):** UT mode affects MAC rotation as described in S3.5. (Böttger et al., 2025). ([Pet Symposium][1])

**Hypothesis:** The detailed UT byte semantics (`0x40` vs. `0x41`) and hashed-flags bit meanings are accessory-generation specific and should be treated as “not publicly fixed” unless backed by the official partner spec revision you target.

---

## S8. Message Formats & Fields `⟦DATA_FORMATS⟧`

### S8.1 BLE advert (Measured; conceptual layout)

```text
Service: Fast Pair / Find Hub extension (UUID + service data)
Payload (standard advertising, typical):
  UT_byte (1) | EID_x (20) | hashed_flags (0..1)

Notes:
- EID_x is x-coordinate of R = r·G on secp160r1 (measured default).
- Rotation ~1024s with additional jitter in rotation timing.
- UT mode affects MAC rotation (measured: 24h MAC rotation).
```

### S8.2 Finder upload (Measured elements; some fields remain open)

```text
Indexing:
- truncatedEID (10 bytes) used as selector/index (measured)

Crypto (measured reconstruction):
- ECDH on P-160R1 with keys derived from EID rotation
- HKDF-SHA-256
- AES-EAX-256 for E2EE payload
- additional transport protection using Google keys (e.g., P-256 + AES-GCM)
```

### S8.3 Owner secrets container (Observed in your integration; not a public spec)

```text
encryptedUserSecrets.encryptedIdentityKey:
- observed length: 60 bytes (Moto Tag example)
- MUST be unwrapped to 32 bytes before EID derivation
- unwrapping observed to involve AES-GCM in your codebase
```

---

## S9. State Machines & Failure Models `⟦STATE_MACHINES⟧`

**Provisioning (conceptual):**

```text
Unpaired → FastPair(AccountKey) → Provisionable → SetEIK → Beaconing(EID rotation)
Failure classes:
- wrong wrapper vs. raw EIK (integration error)
- missing setup_epoch / wrong timebase anchor
- clock skew handling if using wall time instead of "seconds since setup"
```

**Finder upload path (conceptual):**

```text
Scan → Observe advert(UT,EID,flags) → Compute truncatedEID → Gate/Throttle → Encrypt → Upload → Retry
```

**Owner retrieval path (conceptual):**

```text
Fetch secrets → Unwrap EIK (if wrapped) → Precompute rolling EIDs → Fetch ciphertext → Decrypt → Aggregate → Display
```

---

## S10. Security Notes & Best Practices `⟦SECURITY_NOTES⟧`

* **Key hygiene:** treat EIK as a root secret; isolate derived keys by purpose; never reuse AEAD nonces across domains. (Google, n.d.; Böttger et al., 2025). ([Google for Developers][2])
* **Implementation pitfall (observed):** do not log raw keys/wrappers outside diagnostics; when diagnosing, prefer *lengths, hashes, and offsets* over full hex dumps.
* **Timebase correctness:** implement “seconds since setup” as the authoritative counter for EID derivation; treat Unix epoch only as an outer reference. (Böttger et al., 2025). ([Pet Symposium][1])
* **Resolver robustness:** rolling window (prev/current/next) is mandatory due to jittered rotations. (Böttger et al., 2025). ([Pet Symposium][1])

---

## S11. Open Points / Not Publicly Specified `⟦OPEN_POINTS⟧`

* Exact semantics and cryptographic definition of **UT byte** and **hashed flags** across accessory generations (unless you hold the applicable partner spec revision).
* Exact format of **60-byte EIK wrapper** (nonce/tag layout, AAD rules, versioning) until you document it from the decryptor implementation and test vectors.
* Precise REST endpoints, headers, attestation details, and server retention policies (subject to change).

---

## Appendix A — Evidence-anchored Pseudocode `⟦APPX_PSEUDOCODE⟧`

### A1. EID derivation (Measured pattern; secp160r1 default)

```text
input:
  EIK (32B), K (e.g., 10), t = seconds_since_setup

1  ts_u32   = t mod 2^32
2  ts_mask  = ts_u32 & ~((1<<K)-1)           # clear lower K bits
3  B        = 0xFF*11 || byte(K) || u32be(ts_mask) || 0x00*11 || byte(K) || u32be(ts_mask)   # 32 bytes
4  seed     = AES-ECB-256(key=EIK, plaintext=B)     # 32 bytes
5  r        = int_be(seed) mod n_curve
6  if r == 0: r = 1
7  R        = r * G
8  EID      = x(R) encoded big-endian, width = 20 bytes (secp160r1) or 32 bytes (p256)
```

### A2. Rolling-window resolver (Measured necessity due to jitter)

```text
for each device:
  for window in {prev, current, next}:
     t_rel = seconds_since_setup_at(window_anchor)
     eid   = EID(EIK, t_rel)
     cache[eid] = device_id
```

### A3. Unwrap before EID (Observed in your integration)

```text
input:
  encryptedIdentityKey (len observed: 60 bytes)
output:
  EIK (32 bytes)

1  EIK = AES-GCM-Unwrap(owner/account-bound key material, encryptedIdentityKey, aad?, nonce?, tag?)
2  assert len(EIK) == 32
3  proceed with A1
```

---

## References (URLs)

```text
Peer-reviewed research:
- Böttger et al. (2025). PoPETs 2025 paper (PETS): https://petsymposium.org/popets/2025/popets-2025-0147.pdf

Official / product communications:
- Google Security Blog (2024). Unwanted tracker alerts / protections: https://security.googleblog.com/2024/05/multi-layered-user-protections-from-unwanted-tracker-alerts.html
- Android Help (n.d.). Find unknown trackers: https://support.google.com/android/answer/13658562?hl=en

Developer documentation (public summaries / partner docs landing):
- Google Developers (n.d.). Find Hub / Fast Pair extensions (Find Hub network specs): https://developers.google.com/nearby/fast-pair/specifications
- Google Developers (n.d.). Find Hub landing page: https://developers.google.com/nearby/fast-pair/landing-page-find-hub

Standards context:
- IETF DULT WG: https://datatracker.ietf.org/group/dult/

Marketing context:
- Android Find Hub overview: https://www.android.com/learn-find-hub/
```

[1]: https://petsymposium.org/popets/2025/popets-2025-0147.pdf "Okay Google, Where’s My Tracker? Security, Privacy, and Performance Evaluation of Google's Find My Device Network"
[2]: https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn "Find Hub Network Accessory Specification  |  Fast Pair  |  Google for Developers"
[3]: https://security.googleblog.com/2024/04/find-my-device-network-security-privacy-protections.html "
Google Online Security Blog: How we built the new Find Hub network with user security and privacy in mind
"
[4]: https://petsymposium.org/2025/paperlist.php "Privacy Enhancing Technologies Symposium 2025"
