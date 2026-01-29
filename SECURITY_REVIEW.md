# Security Review: GoogleFindMy-HA

**Date:** 2026-01-29
**Version Reviewed:** 1.7.0-3 (commit a5825ad)
**Scope:** Full codebase security audit

---

## Executive Summary

This report covers a comprehensive security review of the GoogleFindMy-HA Home
Assistant integration. The codebase is generally well-engineered with strong
security practices including TLS everywhere, PII redaction in logs, and
automated security scanning (Bandit, Semgrep, pip-audit). However, the review
identified **5 high-severity**, **12 medium-severity**, and **14 low-severity**
findings across authentication, cryptography, input validation, and network
communication.

The most critical issues are:
1. An operator-precedence bug in AES-CBC block alignment validation
2. Weak scrypt parameters making PIN brute-force trivial
3. A cross-account token isolation failure in multi-account setups
4. A shared key derived from raw DER bytes without a proper KDF
5. Use of non-cryptographic PRNG for Android ID generation

---

## Findings

### HIGH Severity

#### H1: Operator precedence bug in AES-CBC block alignment check
- **File:** `custom_components/googlefindmy/KeyBackup/cloud_key_decryptor.py:256`
- **Code:**
  ```python
  if len(ciphertext) % algorithms.AES.block_size // 8 != 0:
  ```
- **Issue:** Due to Python operator precedence, `%` and `//` have equal
  precedence and associate left-to-right. This evaluates as
  `(len(ciphertext) % 128) // 8 != 0` instead of the intended
  `len(ciphertext) % (128 // 8) != 0`. The `cryptography` library's
  `algorithms.AES.block_size` returns **128** (bits). The current expression
  incorrectly allows ciphertexts of length 1-7 bytes to pass validation
  (since `x // 8 == 0` for `x < 8`), while the correct check should reject
  any ciphertext whose length is not a multiple of 16 bytes.
- **Impact:** Malformed ciphertext could reach the CBC decryptor. The
  `cryptography` library will likely raise its own error, but relying on
  downstream validation is fragile.
- **Recommendation:** Add parentheses:
  `len(ciphertext) % (algorithms.AES.block_size // 8) != 0`

#### H2: Weak scrypt cost parameter makes PIN brute-force trivial
- **File:** `custom_components/googlefindmy/KeyBackup/lskf_hasher.py:127-129`
- **Code:**
  ```python
  log_n_cost = 4096  # CPU/memory cost parameter
  block_size = 8
  parallelization = 1
  ```
- **Issue:** The variable name `log_n_cost` is misleading -- the value 4096
  is passed directly as `N` (not `log2(N)`). With `N=4096` (2^12), the
  memory cost is only ~4 MB. OWASP recommends a minimum of `N=2^17`
  (131072). Combined with a 4-digit PIN space (10,000 values), the entire
  keyspace is exhaustible in seconds on commodity hardware.
- **Impact:** An attacker with access to the salt can recover the PIN
  through brute force in negligible time.
- **Note:** This may be dictated by the Google protocol specification. If so,
  the weakness is inherent to the protocol and cannot be fixed locally.
- **Recommendation:** If the protocol allows, increase `N` to at least
  `2^17`. If protocol-constrained, document the weakness prominently.

#### H3: Cross-account token isolation failure in global cache fallback
- **File:** `custom_components/googlefindmy/Auth/aas_token_retrieval.py:369-390`
- **Code:**
  ```python
  # Fallback 3: Try global cache for ADM tokens
  all_cached_global = await async_get_all_cached_values()
  for key, value in all_cached_global.items():
      if isinstance(key, str) and key.startswith("adm_token_"):
          oauth_token = value
          extracted_username = key.replace("adm_token_", "", 1)
  ```
- **Issue:** When an entry-scoped cache lacks an OAuth token, the code falls
  back to the **global/default** cache and uses any `adm_token_*` entry it
  finds. In a multi-account setup, Account A's token generation could use
  Account B's ADM token and even override the username. This directly
  contradicts the module's documented "strict multi-account isolation" design.
- **Impact:** Cross-account credential leakage. One account's API calls
  could use another account's tokens, potentially accessing the wrong
  account's devices.
- **Recommendation:** Remove the global cache fallback or scope it strictly
  to the requesting entry's credentials.

#### H4: Shared key derived from raw DER bytes without proper KDF
- **File:** `custom_components/googlefindmy/KeyBackup/shared_key_retrieval.py:172-178`
- **Code:**
  ```python
  shared = der[-SHARED_KEY_LEN:]
  return shared.hex()
  ```
- **Issue:** The shared key is derived by slicing the last 32 bytes of a
  DER-encoded private key. DER has ASN.1 structure; the tail bytes may
  overlap with padding, OIDs, or other overhead. No KDF (e.g., HKDF) is
  applied to ensure uniform key distribution. If the DER format changes
  across key types or encodings, the derived key silently changes.
- **Impact:** Key material may not have uniform distribution. Format changes
  could silently break key derivation, locking users out.
- **Recommendation:** Apply HKDF to the raw DER material before using it as
  key material, or extract the raw private key scalar first.

#### H5: Non-cryptographic PRNG for Android ID generation
- **File:** `custom_components/googlefindmy/Auth/token_retrieval.py:149`
- **Code:**
  ```python
  android_id = random.randint(0x1000000000000000, 0xFFFFFFFFFFFFFFFF)
  ```
- **Issue:** Uses Python's `random` module (Mersenne Twister), which is
  predictable. Other files (`adm_token_retrieval.py:216`,
  `aas_token_retrieval.py:195`) correctly use `secrets.randbelow()`. The
  Android ID is used as a device identifier in Google authentication token
  exchanges.
- **Impact:** An attacker who observes PRNG output could predict future
  Android IDs and impersonate the device identity in token exchanges.
- **Recommendation:** Replace with `secrets.randbelow()` to match the
  pattern used elsewhere in the codebase.

---

### MEDIUM Severity

#### M1: AES-EAX decrypt and verify are not atomic
- **File:** `custom_components/googlefindmy/FMDNCrypto/foreign_tracker_cryptor.py:233-236`
- **Code:**
  ```python
  plaintext: bytes = cipher.decrypt(m_dash)
  cipher.verify(tag)
  return plaintext
  ```
- **Issue:** `decrypt()` returns plaintext before `verify()` checks the
  authentication tag. If an exception is caught between these calls, or if
  a caller ignores the verification error, unauthenticated plaintext could
  be used.
- **Recommendation:** Use `cipher.decrypt_and_verify(m_dash, tag)` for
  atomic authenticated decryption.

#### M2: AES-CBC decryption without authentication
- **File:** `custom_components/googlefindmy/KeyBackup/cloud_key_decryptor.py:259-261`
- **Issue:** AES-CBC provides confidentiality but not integrity. No MAC or
  authentication tag is verified, making the decryption vulnerable to
  ciphertext manipulation. This may be protocol-dictated.

#### M3: HKDF called with null salt and empty info
- **File:** `custom_components/googlefindmy/FMDNCrypto/foreign_tracker_cryptor.py:294-296`
- **Issue:** `HKDF(salt=None, info=b"")` provides no domain separation.
  Two different protocol contexts using the same ECDH shared secret would
  derive identical AES keys.

#### M4: OAuth fallback temporarily mutates shared cache state
- **File:** `custom_components/googlefindmy/Auth/adm_token_retrieval.py:446-482`
- **Issue:** The fallback path sets `DATA_AUTH_METHOD` to
  `"individual_tokens"` in the shared cache and restores it in a `finally`
  block. If the process crashes or the coroutine is cancelled between mutation
  and restoration, the auth method is left in the wrong state.

#### M5: AAS token reused without validation when prefix matches
- **File:** `custom_components/googlefindmy/Auth/aas_token_retrieval.py:334-347`
- **Issue:** If a cached value starts with `"aas_et/"`, it is returned as a
  valid AAS token without any expiration check or validation. An attacker
  with cache write access could plant an arbitrary string.

#### M6: OAuth token from browser cookie without domain/flag validation
- **File:** `custom_components/googlefindmy/Auth/auth_flow.py:46-57`
- **Issue:** The OAuth token is extracted from a browser cookie without
  verifying `Secure`/`HttpOnly` flags or that the cookie domain is strictly
  `accounts.google.com`.

#### M7: No CSRF or session integrity in embedded setup flow
- **File:** `custom_components/googlefindmy/Auth/auth_flow.py:41`
- **Issue:** The EmbeddedSetup authentication flow has no CSRF token, state
  parameter, or verification that the resulting token corresponds to the
  intended account.

#### M8: Verbose logging exposes credentials in FCM client
- **File:** `custom_components/googlefindmy/Auth/firebase_messaging/fcmpushclient.py:155-156, 226-228, 403, 542-544`
- **Issue:** When `log_debug_verbose=True`, full `LoginRequest` messages
  (containing `security_token`, `android_id`) and decrypted FCM payloads are
  written to logs. The flag defaults to `False` but is user-configurable.

#### M9: All tokens stored as plaintext JSON on disk
- **File:** `custom_components/googlefindmy/Auth/token_cache.py:93`
- **Issue:** Tokens are persisted to HA's `.storage` directory as plaintext
  JSON. Any process with filesystem access can extract credentials. This is a
  limitation of the HA `Store` API.

#### M10: Full secrets.json blob persisted in config entry data
- **File:** `custom_components/googlefindmy/config_flow.py:2888-2889`
- **Issue:** The complete secrets bundle (FCM credentials, private keys,
  registration tokens) is stored in `config_entry.data`, which HA persists
  as JSON in `.storage/core.config_entries`.

#### M11: Map view token has only 64-bit entropy from predictable seed
- **File:** `custom_components/googlefindmy/const.py:512-520`
- **Code:**
  ```python
  return hashlib.sha256(seed.encode()).hexdigest()[:16]
  ```
- **Issue:** The seed is `<uuid>:<entry_id>:<week_or_static>`, making the
  token fully predictable to anyone who knows the HA UUID and entry ID.
  The 16-hex-char output provides 64 bits of entropy. Default configuration
  disables expiration (`DEFAULT_MAP_VIEW_TOKEN_EXPIRATION = False`), so a
  leaked token provides indefinite access.

#### M12: Silent failure swallows cryptographic errors
- **File:** `custom_components/googlefindmy/FMDNCrypto/key_derivation.py:61-65`
- **Issue:** If key derivation fails, keys are silently set to `None` and
  execution continues. This is a fail-open pattern; callers that don't check
  for `None` could proceed without cryptographic protection.

---

### LOW Severity

#### L1: Legacy SECP160r1 path allows scalar=0 (point at infinity)
- **File:** `custom_components/googlefindmy/FMDNCrypto/eid_generator.py:322-334`

#### L2: TOCTOU race in legacy file migration
- **File:** `custom_components/googlefindmy/Auth/token_cache.py:158-168`

#### L3: Cooldown bypass functions exposed in production
- **File:** `custom_components/googlefindmy/Auth/token_refresh.py:259-266`

#### L4: Debug logs expose android_id in hex
- **File:** `custom_components/googlefindmy/Auth/aas_token_retrieval.py:235-242`

#### L5: Error messages may leak scope and server error details
- **File:** `custom_components/googlefindmy/Auth/token_retrieval.py:214-228`
- **File:** `custom_components/googlefindmy/Auth/adm_token_retrieval.py:626`

#### L6: No response body size limit on API reads
- **File:** `custom_components/googlefindmy/NovaApi/nova_request.py:1400`
- **File:** `custom_components/googlefindmy/Auth/firebase_messaging/fcmpushclient.py:358`

#### L7: No size limit on secrets.json paste input in config flow
- **File:** `custom_components/googlefindmy/config_flow.py:1147-1154`

#### L8: Token plausibility check accepts arbitrary non-whitespace strings
- **File:** `custom_components/googlefindmy/config_flow.py:941`

#### L9: Sync facade bypasses async write lock in TokenCache
- **File:** `custom_components/googlefindmy/Auth/token_cache.py:576-580`

#### L10: Test-only brute-force harness included in production source
- **File:** `custom_components/googlefindmy/KeyBackup/lskf_hasher.py:162-174`

#### L11: HMAC returns hex string; callers must ensure constant-time comparison
- **File:** `custom_components/googlefindmy/FMDNCrypto/sha.py:139-140`

#### L12: Hardcoded client signature duplicated across files
- **File:** `custom_components/googlefindmy/Auth/token_retrieval.py:62`
- **File:** `custom_components/googlefindmy/Auth/adm_token_retrieval.py:74`

#### L13: Chrome launched with `--disable-web-security` and `--no-sandbox`
- **File:** `custom_components/googlefindmy/chrome_driver.py:297-300`
- **Note:** Required for the Google EmbeddedSetup auth flow. Used only during
  initial config, not during production polling. Scoped to a temporary
  Chrome instance.

#### L14: `_GpsoauthProxy.__setattr__` allows runtime mutation of auth module
- **File:** `custom_components/googlefindmy/Auth/gpsoauth_loader.py:69-70`

---

## Positive Security Practices

The codebase demonstrates several strong security practices:

- **TLS everywhere:** All external connections use HTTPS/TLS with
  `ssl.create_default_context()`. No certificate validation bypass was found.
- **PII redaction:** Bearer tokens, emails, and long hex strings are redacted
  before logging via dedicated `_redact()` helpers.
- **Redirect following disabled:** Nova API requests set
  `allow_redirects=False`, mitigating SSRF via open redirect.
- **Bounded retries with jitter:** All API clients use exponential backoff
  with jitter to prevent thundering herd.
- **Automated security scanning:** CI pipelines include Bandit, Semgrep,
  pip-audit, and mypy strict mode.
- **Error truncation:** Error messages are bounded to 300-512 characters
  before logging.
- **Canonical ID truncation:** Device identifiers are logged truncated to
  8 characters throughout the FCM receiver.
- **Request payload size validation:** 512 KiB outbound payload limit in
  Nova API.
- **Entry-scoped token caches:** Multi-account support uses per-entry
  namespaced caches (with the exception noted in H3).
- **Property-based testing:** Hypothesis is used for fuzz testing.
- **Type safety:** mypy strict mode is enforced across the codebase.

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

## Recommendations Summary

### Immediate (High Priority)
1. Fix the operator precedence bug in `cloud_key_decryptor.py:256`
2. Replace `random.randint` with `secrets.randbelow` in `token_retrieval.py:149`
3. Remove or scope the global cache fallback in `aas_token_retrieval.py:369-390`

### Short-term
4. Use `cipher.decrypt_and_verify()` instead of separate decrypt/verify in
   `foreign_tracker_cryptor.py`
5. Apply HKDF to DER key material in `shared_key_retrieval.py`
6. Add input size limits to the config flow secrets.json field
7. Increase map view token entropy and enable expiration by default

### Long-term
8. Document the scrypt weakness in LSKF hashing (if protocol-constrained)
9. Add response body size limits for all HTTP reads
10. Remove the PIN brute-force harness from production source
11. Add upper bounds to dependency version constraints for security-critical
    packages
