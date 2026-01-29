# Security Review: GoogleFindMy-HA

**Date:** 2026-01-29
**Version Reviewed:** 1.7.0-3 (commit a5825ad)
**Scope:** Full codebase security audit
**Revision:** v2 — re-assessed in context of FMDN protocol constraints

---

## Executive Summary

This report covers a comprehensive security review of the GoogleFindMy-HA Home
Assistant integration, **re-assessed against the constraints of Google's FMDN
(Find My Device Network) protocol**.

The initial review identified 5 high-severity findings. After re-evaluation,
**3 of those were protocol-dictated or mischaracterized** and have been
downgraded. The remaining **2 genuine code bugs have been fixed** in this commit.

### Re-Assessment Summary

| Original | Finding | Root Cause | New Severity | Action |
|----------|---------|-----------|--------------|--------|
| H1 | AES-CBC block alignment bug | **Integration bug** (Python operator precedence) | HIGH | **Fixed** |
| H2 | Scrypt N=4096 | **FMDN protocol** (Google-dictated parameters) | Informational | None required |
| H3 | Cross-account global cache fallback | **Integration bug** (logic error) | HIGH | **Fixed** |
| H4 | DER slicing for shared key | **FMDN protocol** (GoogleFindMyTools compatibility) | Informational | None required |
| H5 | `random.randint` for Android ID | **Mischaracterized** (identifier, not secret) | LOW | **Fixed** (consistency) |

---

## Findings

### HIGH Severity (Fixed)

#### H1: Operator precedence bug in AES-CBC block alignment check
- **File:** `custom_components/googlefindmy/KeyBackup/cloud_key_decryptor.py:256`
- **Root cause:** Integration code bug (Python operator precedence)
- **Code (before fix):**
  ```python
  if len(ciphertext) % algorithms.AES.block_size // 8 != 0:
  ```
- **Issue:** `%` and `//` have equal precedence and associate left-to-right.
  This evaluates as `(len(ciphertext) % 128) // 8 != 0` instead of the
  intended `len(ciphertext) % (128 // 8) != 0`. Ciphertexts of length 1-7
  bytes incorrectly pass the check.
- **Fix applied:**
  ```python
  if len(ciphertext) % (algorithms.AES.block_size // 8) != 0:
  ```
- **FMDN impact:** None. This is purely a validation guard in the
  integration's own code. The fix does not change any protocol behavior.

#### H3: Cross-account global cache fallback (removed)
- **File:** `custom_components/googlefindmy/Auth/aas_token_retrieval.py:369-390`
- **Root cause:** Integration logic error
- **Issue:** When an entry-scoped cache lacked an OAuth token, "Fallback 3"
  scanned the **global** cache for any `adm_token_*` entry, potentially
  using Account B's token for Account A's exchange. This contradicted the
  module's documented multi-account isolation design.
- **Fix applied:** Removed the global cache fallback entirely. The
  entry-scoped fallback (Fallback 2, scanning the entry's own cache) remains
  intact and is sufficient.
- **FMDN impact:** None. The entry-scoped ADM token fallback still works.
  Only the cross-account leakage path was removed.

---

### Informational (Protocol-Dictated — Not Fixable)

#### Former H2: Scrypt parameters (N=4096, r=8, p=1)
- **File:** `custom_components/googlefindmy/KeyBackup/lskf_hasher.py:127-129`
- **Root cause:** **Google's FMDN Key Backup protocol** dictates these exact
  scrypt parameters. The LSKF hash must match what Google's servers expect
  to derive the correct recovery key decryption key.
- **Why it cannot be changed:** Changing N, r, or p would produce a
  different derived key, making it impossible to decrypt the recovery key
  blob received from Google's cloud backup. The PIN's small keyspace (10,000
  for 4-digit) is a protocol-level weakness, not an integration bug.
- **Mitigation:** The salt is per-device and stored server-side. An attacker
  would need both the encrypted recovery key blob AND the salt to attempt a
  brute-force. Access to these requires a compromised Google account.

#### Former H4: Shared key from last 32 bytes of DER private key
- **File:** `custom_components/googlefindmy/KeyBackup/shared_key_retrieval.py:172-178`
- **Root cause:** **GoogleFindMyTools compatibility requirement.** The
  derivation pattern (`der[-32:]`) replicates the behavior of the upstream
  GoogleFindMyTools project by Leon Böttger. For P-256 keys, the last 32
  bytes of the DER encoding ARE the raw private key scalar — this is not
  arbitrary slicing but a known property of the DER/ASN.1 structure for
  ECDSA P-256 private keys.
- **Why it cannot be changed:** Applying HKDF or changing the extraction
  would produce a different shared key, breaking decryption of all existing
  E2EE payloads. The shared key must match what GoogleFindMyTools produces.

---

### LOW Severity (Fixed — Consistency)

#### Former H5: `random.randint` for Android ID generation
- **File:** `custom_components/googlefindmy/Auth/token_retrieval.py:149`
- **Root cause:** Inconsistency, not a security vulnerability
- **Re-assessment:** The Android ID is a **device identifier** (comparable
  to a MAC address or IMEI), not a cryptographic secret. It is sent in
  plaintext to Google's servers in every gpsoauth request. Its
  unpredictability is irrelevant to security — the authentication comes from
  the OAuth/AAS tokens, not from the Android ID.
- **Fix applied:** Replaced `random.randint` with `secrets.randbelow` to
  match the pattern in `aas_token_retrieval.py:195`. This is a consistency
  improvement, not a security fix.

---

### MEDIUM Severity (Unchanged)

#### M1: AES-EAX decrypt and verify are not atomic
- **File:** `custom_components/googlefindmy/FMDNCrypto/foreign_tracker_cryptor.py:233-236`
- **Issue:** `decrypt()` returns plaintext before `verify()` checks the tag.
- **Note:** PyCryptodome's EAX mode supports `decrypt_and_verify()` for
  atomic operation. This is a code improvement, not protocol-constrained.

#### M2: AES-CBC decryption without authentication
- **File:** `custom_components/googlefindmy/KeyBackup/cloud_key_decryptor.py:259-261`
- **Note:** **Protocol-dictated.** Google's EIK format uses AES-CBC without
  authentication for legacy 48-byte blobs. Cannot be changed.

#### M3: HKDF with null salt and empty info
- **File:** `custom_components/googlefindmy/FMDNCrypto/foreign_tracker_cryptor.py:294-296`
- **Note:** **Protocol-dictated.** The FMDN tracker encryption spec uses
  these HKDF parameters. Cannot be changed.

#### M4: OAuth fallback temporarily mutates shared cache state
- **File:** `custom_components/googlefindmy/Auth/adm_token_retrieval.py:446-482`
- **Note:** Integration logic. The cache mutation/restore pattern is fragile
  but functional. Low probability of crash between mutation and restore.

#### M5: AAS token reused without validation when prefix matches
- **File:** `custom_components/googlefindmy/Auth/aas_token_retrieval.py:334-347`
- **Note:** Integration logic. The `"aas_et/"` prefix check is a heuristic
  to avoid redundant gpsoauth exchanges. Exploitation requires cache write
  access (i.e., filesystem compromise).

#### M6/M7: Browser auth flow without cookie validation or CSRF
- **File:** `custom_components/googlefindmy/Auth/auth_flow.py`
- **Re-assessment:** These are **inherent to the FMDN secret retrieval
  process.** The integration MUST use Google's EmbeddedSetup endpoint via a
  real browser to obtain FMDN secrets — there is no API alternative.
  Google's EmbeddedSetup is designed for Android device provisioning and
  does not support CSRF tokens or OAuth state parameters. The
  `--disable-web-security` flag is required for the CORS bypass needed by
  this endpoint. These are architectural constraints, not integration bugs.

#### M8: Verbose logging exposes credentials in FCM client
- **File:** `custom_components/googlefindmy/Auth/firebase_messaging/fcmpushclient.py`
- **Note:** Integration logic. Flag defaults to `False`. Risk only when user
  explicitly enables verbose debug logging.

#### M9: Plaintext token storage via HA Store API
- **File:** `custom_components/googlefindmy/Auth/token_cache.py:93`
- **Note:** **HA platform limitation.** Home Assistant's `Store` API does not
  provide at-rest encryption. All HA integrations that use `Store` have this
  property. Cannot be fixed in the integration alone.

#### M10: Full secrets.json in config entry data
- **File:** `custom_components/googlefindmy/config_flow.py:2888-2889`
- **Note:** **HA platform convention.** Config entries are the standard HA
  mechanism for persisting integration credentials. Same limitation as M9.

#### M11: Map view token predictability
- **File:** `custom_components/googlefindmy/const.py:512-520`
- **Note:** Integration logic. Could be improved with a random component
  in the seed.

#### M12: Silent failure in key derivation
- **File:** `custom_components/googlefindmy/FMDNCrypto/key_derivation.py:61-65`
- **Note:** Integration logic. Fail-open pattern is intentional (graceful
  degradation for optional key types).

---

### LOW Severity (Unchanged)

#### L1: Legacy SECP160r1 path allows scalar=0
- **File:** `custom_components/googlefindmy/FMDNCrypto/eid_generator.py:322-334`
- **Note:** Protocol-dictated. Legacy curve behavior must be preserved.

#### L2–L14: Remaining low-severity findings
(Unchanged from initial report — see full list in commit history.)

---

## Positive Security Practices

The codebase demonstrates several strong security practices:

- **TLS everywhere:** All external connections use HTTPS/TLS with
  `ssl.create_default_context()`. No certificate validation bypass found.
- **PII redaction:** Bearer tokens, emails, and long hex strings are redacted
  before logging via dedicated `_redact()` helpers.
- **Redirect following disabled:** Nova API requests set
  `allow_redirects=False`, mitigating SSRF via open redirect.
- **Bounded retries with jitter:** All API clients use exponential backoff
  with jitter to prevent thundering herd.
- **Automated security scanning:** CI pipelines include Bandit, Semgrep,
  pip-audit, and mypy strict mode.
- **Error truncation:** Error messages bounded to 300-512 characters.
- **Canonical ID truncation:** Device IDs logged truncated to 8 characters.
- **Request payload size validation:** 512 KiB outbound payload limit.
- **Entry-scoped token caches:** Multi-account isolation per config entry.
- **Property-based testing:** Hypothesis used for fuzz testing.
- **Type safety:** mypy strict mode enforced across the codebase.

---

## Dependencies

| Package | Version | Purpose | Notes |
|---------|---------|---------|-------|
| aiohttp | >=3.11.8 | Async HTTP | HA-managed sessions |
| beautifulsoup4 | >=4.12.3 | HTML parsing | Auth flow only |
| cryptography | >=43.0.3 | Modern crypto | Well-maintained |
| ecdsa | >=0.19.1 | ECDSA operations | Pure Python |
| gpsoauth | >=2.0.0 | Google OAuth | Niche; monitor closely |
| h2 | >=4.1.0 | HTTP/2 | For gRPC transport |
| http-ece | >=1.2.1 | Encrypted Content Encoding | FCM push decryption |
| httpx | >=0.28.0 | HTTP client | HTTP/2 extras |
| protobuf | >=6.32.0 | Protocol Buffers | Google API protocol |
| pycryptodomex | >=3.23.0 | AES-EAX | Avoids Crypto conflicts |
| pyscrypt | >=1.6.2 | Scrypt KDF | PIN hashing |
| selenium | >=4.37.0 | Browser automation | Auth flow only |
| undetected-chromedriver | >=3.5.5 | Chrome driver | Bot detection bypass |

**Dependency risks:**
- `gpsoauth` and `pyscrypt` are small-community packages with fewer
  maintainers. Monitor for abandonment or supply chain compromise.
- `undetected-chromedriver` is inherently adversarial (anti-detection);
  updates may lag behind Chrome releases.
- All version constraints use `>=` (minimum only), meaning untested newer
  versions could be installed. Consider adding upper bounds for
  security-critical packages.

---

## Remaining Recommendations

### Immediate
All immediate items have been fixed in this commit:
1. ~~Operator precedence bug~~ — **Fixed**
2. ~~Cross-account global cache fallback~~ — **Fixed**
3. ~~`random.randint` inconsistency~~ — **Fixed**

### Short-term (Integration Improvements)
4. Use `cipher.decrypt_and_verify()` in `foreign_tracker_cryptor.py` (M1)
5. Add input size limits to config flow secrets.json field (L7)
6. Increase map view token entropy with a random seed component (M11)

### Long-term
7. Add response body size limits for all HTTP reads (L6)
8. Remove the PIN brute-force harness from production source (L10)
9. Add upper bounds to dependency versions for security-critical packages
