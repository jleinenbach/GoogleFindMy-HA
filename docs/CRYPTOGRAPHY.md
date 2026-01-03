# Cryptography in GoogleFindMy-HA

This document provides a comprehensive overview of the cryptographic primitives,
algorithms, and protocols used in the GoogleFindMy Home Assistant integration.
The goal is to enable developers to understand, maintain, and extend the
cryptographic components with confidence.

## Table of Contents

1. [Mathematical Foundations](#mathematical-foundations)
2. [Elliptic Curves Used](#elliptic-curves-used)
3. [Key Hierarchy and Derivation](#key-hierarchy-and-derivation)
4. [EID Generation](#eid-generation)
5. [Authenticated Encryption](#authenticated-encryption)
6. [ECDH Key Agreement](#ecdh-key-agreement)
7. [Module Reference](#module-reference)

---

## Mathematical Foundations

### Elliptic Curve Cryptography (ECC)

Elliptic curves used in cryptography are defined over finite fields. For a prime
field GF(p), the curve is defined by the short Weierstrass equation:

```
y² = x³ + ax + b  (mod p)
```

Where:
- `p` is a large prime defining the finite field
- `a` and `b` are curve parameters satisfying 4a³ + 27b² ≠ 0 (non-singular)
- Points (x, y) satisfying the equation, plus a "point at infinity" O, form a group

### Point Operations

**Point Addition**: Given points P = (x₁, y₁) and Q = (x₂, y₂):

```
λ = (y₂ - y₁) / (x₂ - x₁)  (mod p)
x₃ = λ² - x₁ - x₂  (mod p)
y₃ = λ(x₁ - x₃) - y₁  (mod p)
```

**Point Doubling** (P = Q):

```
λ = (3x₁² + a) / (2y₁)  (mod p)
```

**Scalar Multiplication**: Computing `kP` for scalar k uses double-and-add algorithms.

### Point Compression and Decompression

Since y² = x³ + ax + b, for any x there are at most two valid y values: y and -y (mod p).

**Compression**: Store only x plus one bit indicating whether y is even or odd.
- Format: `0x02 || x` (even y) or `0x03 || x` (odd y)

**Decompression** (used in `rx_to_ry`):

For primes where p ≡ 3 (mod 4), the modular square root is computed as:

```
y² = x³ + ax + b  (mod p)
y = (y²)^((p+1)/4)  (mod p)
```

**Why This Works** (Fermat's Little Theorem):

1. For non-zero a in GF(p): a^(p-1) ≡ 1 (mod p)
2. Therefore: a^((p-1)/2) ≡ ±1 (mod p)
3. For quadratic residues: a^((p-1)/2) ≡ 1 (mod p) [Euler's criterion]
4. Thus: (y²)^((p+1)/4) = y^((p+1)/2) = y^((p-1)/2) × y = 1 × y = y

FMDN uses the **even** y value by convention.

---

## Elliptic Curves Used

### SECP160r1 (Legacy FMDN)

```
Field:     GF(2¹⁶⁰ - 2³¹ - 1)
p:         0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF7FFFFFFF
a:         0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF7FFFFFFC
b:         0x1C97BEFC54BD7A8B65ACF89F81D4D4ADC565FA45
Order:     0x0100000000000000000001F4C8F927AED3CA752257
Cofactor:  1
Coord Len: 20 bytes (160 bits)
```

**Properties**:
- p ≡ 3 (mod 4): Enables efficient square root computation
- 160-bit security level (legacy, suitable for bandwidth-constrained beacons)
- Used for EID generation and location report encryption

### SECP256r1 / P-256 (Key Backup)

```
Field:     GF(2²⁵⁶ - 2²²⁴ + 2¹⁹² + 2⁹⁶ - 1)
Order:     0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
Coord Len: 32 bytes (256 bits)
```

**Properties**:
- NIST standard curve, widely implemented
- 128-bit security level
- Used for ECDH key agreement in cloud key backup

---

## Key Hierarchy and Derivation

The FMDN key backup system uses a hierarchical derivation chain:

```
                    LSKF (PIN/Password)
                           │
                           ▼ Scrypt
                    LSKF Hash (32 bytes)
                           │
                           ▼ SHA-256
        ┌──────────────────┴──────────────────┐
        │                                      │
        ▼ AES-GCM                              │
  Recovery Key                                 │
        │                                      │
        ▼ HKDF + AES-GCM                       │
  Application Key                              │
        │                                      │
        ▼ AES-GCM                              │
  Security Domain Key ─────────────────────────┤
        │                                      │
        ▼ ECDH + HKDF + AES-GCM                │
    Shared Key                                 │
        │                                      │
        ▼ AES-GCM                              │
    Owner Key                                  │
        │                                      │
        ├─────────────┬────────────────────────┘
        ▼             ▼
       EIK       Account Key
   (Identity)    (Per-Tracker)
```

### Scrypt (LSKF Hashing)

Scrypt is a memory-hard KDF designed to resist hardware attacks:

```
Scrypt(password, salt, N, r, p, dkLen) → derived_key
```

**FMDN Parameters**:
- N = 4096 (CPU/memory cost)
- r = 8 (block size)
- p = 1 (parallelization)
- dkLen = 32 (output length)

**Memory requirement**: 128 × N × r = 4 MB per hash operation

### HKDF-SHA256 (RFC 5869)

HKDF operates in two phases:

**Extract Phase** (key uniformity):
```
PRK = HMAC-SHA256(salt, IKM)
```

**Expand Phase** (key derivation):
```
T(1) = HMAC-SHA256(PRK, info || 0x01)
T(2) = HMAC-SHA256(PRK, T(1) || info || 0x02)
...
OKM = T(1) || T(2) || ... (truncated)
```

**Domain Separation Strings**:
- `"SECUREBOX" || VERSION` as salt
- `"SHARED HKDF-SHA-256 AES-128-GCM"` for shared key derivation
- `"P256 HKDF-SHA-256 AES-128-GCM"` for ECDH-derived keys

---

## EID Generation

Ephemeral Identifiers (EIDs) are rotating public keys that prevent tracking:

### Table 10 PRF Construction

```
prf_input = k || time_counter (padded to 16 bytes)
prf_output = AES-256-ECB(identity_key, prf_input)
```

### Scalar Derivation

```
r' = int(prf_output)  (big-endian)
r = (r' mod (order - 1)) + 1  (ensures r ≠ 0)
```

### EID Computation

```
R = r × G  (scalar multiplication with generator)
EID = R.x  (x-coordinate only, 20 bytes for SECP160r1)
```

### Time Counter

The time counter rotates periodically (typically every 15 minutes), causing the
EID to change. This prevents long-term tracking while allowing the owner to
compute future/past EIDs for location lookup.

---

## Authenticated Encryption

### AES-EAX-256 (Foreign Tracker Encryption)

AES-EAX provides authenticated encryption with associated data (AEAD):

**Encryption**:
```
(ciphertext, tag) = AES-EAX-256(key, nonce, plaintext)
output = ciphertext || tag  (16-byte tag)
```

**Nonce Construction**:
```
nonce = LRx(8) || LSx(8)  (16 bytes total)
```
Where LRx/LSx are the last 8 bytes of the x-coordinates of points R and S.

**Key Derivation**:
```
shared_point = s × R  (ECDH)
key = HKDF-SHA256(shared_point.x, salt=None, info="", length=32)
```

### AES-GCM (Key Backup)

AES-GCM is the standard AEAD used in the key backup system:

**Structure**: `IV || ciphertext || tag`
- IV: 12 bytes (randomly generated)
- Tag: 16 bytes (authentication tag)

**Key Sizes**:
- AES-128-GCM: 16-byte key (derived via HKDF)
- AES-256-GCM: 32-byte key

### AES-CBC (Legacy EIK)

Some legacy EIK blobs use AES-CBC without padding:

**Structure**: `IV || ciphertext`
- IV: 16 bytes
- Ciphertext: Block-aligned (16-byte blocks)

---

## ECDH Key Agreement

Elliptic Curve Diffie-Hellman enables two parties to establish a shared secret:

### Protocol

1. Alice has private key `a`, public key `A = aG`
2. Bob has private key `b`, public key `B = bG`
3. Shared secret: `S = a × B = b × A = abG`

### In FMDN

**Encryption** (sender knows receiver's public key R):
```
s ← random scalar
S = sG  (ephemeral public key)
shared = s × R  (ECDH with receiver's key)
key = HKDF(shared.x)
ciphertext = AES-EAX(key, message)
output = (ciphertext, S.x)
```

**Decryption** (receiver knows private key r):
```
R = rG  (receiver's public key, derivable from EID)
shared = r × S  (ECDH with sender's ephemeral key)
key = HKDF(shared.x)
plaintext = AES-EAX-decrypt(key, ciphertext)
```

---

## Module Reference

### FMDNCrypto/foreign_tracker_cryptor.py

| Function | Purpose |
|----------|---------|
| `rx_to_ry(Rx, curve)` | Point decompression (recover Y from X) |
| `encrypt(message, random, eid)` | ECDH + AES-EAX-256 encryption |
| `decrypt(identity_key, data, Sx, time)` | ECDH + AES-EAX-256 decryption |
| `calculate_r(identity_key, time)` | Derive scalar r for EID |

### FMDNCrypto/eid_generator.py

| Function | Purpose |
|----------|---------|
| `generate_eid(identity_key, time, variant)` | Generate EID for given time |
| `build_table10_prf_input(time, k)` | Construct PRF input block |
| `prf_aes_256_ecb(key, data)` | AES-256-ECB PRF |

### FMDNCrypto/sha.py

| Function | Purpose |
|----------|---------|
| `calculate_truncated_sha256(key, op)` | Domain-separated truncated hash |
| `calculate_hmac_sha256(key, message)` | HMAC-SHA-256 |

### KeyBackup/cloud_key_decryptor.py

| Function | Purpose |
|----------|---------|
| `derive_key_using_hkdf_sha256(ikm, salt, info)` | HKDF-SHA256 key derivation |
| `decrypt_aes_gcm(key, data, aad, iv_len)` | AES-GCM decryption |
| `encrypt_aes_gcm(key, plaintext, aad, iv_len)` | AES-GCM encryption |
| `decrypt_recovery_key(lskf_hash, data)` | Decrypt recovery key |
| `decrypt_application_key(recovery_key, data)` | Decrypt application key |
| `decrypt_security_domain_key(app_key, data)` | Decrypt security domain key |
| `decrypt_shared_key(sec_domain_key, data)` | Decrypt shared key (ECDH) |
| `decrypt_owner_key(shared_key, data)` | Decrypt owner key |
| `decrypt_eik(owner_key, data)` | Decrypt EIK (CBC or GCM) |
| `decrypt_account_key(owner_key, data)` | Decrypt per-tracker account key |

### KeyBackup/lskf_hasher.py

| Function | Purpose |
|----------|---------|
| `get_lskf_hash(pin, salt)` | Scrypt LSKF hashing |
| `ascii_to_bytes(string)` | ASCII string to bytes |

---

## Security Considerations

1. **Key Storage**: Identity keys and derived keys must be stored securely.
   Home Assistant's `config_entries` with encryption should be used.

2. **Timing Attacks**: Cryptographic comparisons should use constant-time
   functions (e.g., `hmac.compare_digest`).

3. **Random Number Generation**: Use `secrets` module for cryptographic
   randomness, never `random`.

4. **Memory Cleanup**: Sensitive key material should be zeroed after use
   where possible (Python makes this challenging).

5. **Error Handling**: Decryption failures should not leak information
   about why they failed (timing, error messages).

---

## References

- [RFC 5869](https://tools.ietf.org/html/rfc5869): HKDF
- [RFC 7914](https://tools.ietf.org/html/rfc7914): Scrypt
- [RFC 2104](https://tools.ietf.org/html/rfc2104): HMAC
- [NIST FIPS 186-4](https://csrc.nist.gov/publications/detail/fips/186/4/final): DSS (ECC curves)
- [NIST SP 800-38D](https://csrc.nist.gov/publications/detail/sp/800-38d/final): AES-GCM
- [SEC 2](https://www.secg.org/sec2-v2.pdf): Recommended Elliptic Curve Parameters
