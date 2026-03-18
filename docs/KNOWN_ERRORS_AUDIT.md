# Known Errors Discovered in Audit (Issue #155)

This document records bugs that existed in the codebase but were **not caught** by
prior code reviews or automated testing. Each entry explains why the bug was
hard to detect and what safeguards would have prevented it.

---

## 1. `calculate_r` modular arithmetic mismatch (CRITICAL)

**File:** `FMDNCrypto/foreign_tracker_cryptor.py:251`

**Bug:** `(r_dash_int % (order - 1)) + 1` instead of `r_dash_int % order`

**Why it was missed:**
- The function looked mathematically reasonable — `(mod (n-1)) + 1` is a
  standard technique to exclude zero from the output range, which is valid for
  P-256 scalars where `r = 0` produces the point at infinity.
- However, the EID generator (`eid_generator.py:_derive_scalar`) uses
  `include_zero_endpoint=True` for legacy SECP160r1, meaning `r % n` (allowing
  zero). The two functions must agree because they participate in the same ECDH
  key agreement: one derives the EID (`R = r * G`), the other derives the
  decryption scalar (`r` for `r * S`).
- The mismatch only manifests on **crowdsourced/foreign** location reports
  (ECDH path). Own-device reports use AES-GCM with `SHA256(identity_key)` and
  never call `calculate_r`, so they decrypt correctly — masking the bug.
- No integration test compares the round-trip of `generate_eid_variant` →
  `encrypt` → `decrypt` using the same identity key and time counter.

**Prevention:**
- Add a round-trip test: `decrypt(eik, encrypt(msg, rand, generate_eid(eik, t)), Sx, t) == msg`
- Add a unit test asserting `calculate_r(eik, t) == _derive_scalar(eik, t, include_zero_endpoint=True, ...)`

---

## 2. `is_mcu_tracker` false positive on `device_type == 1` (MEDIUM)

**File:** `FMDNCrypto/mcu_utils.py:49-50`

**Bug:** `device_type == _MCU_DEVICE_TYPE` returned `True` for all
`DEVICE_TYPE_BEACON` (1) devices, not just MCU trackers.

**Why it was missed:**
- The docstring says "MCU trackers register as DEVICE_TYPE_BEACON" — implying
  the check was intentional.
- In most call sites (`decrypt_locations.py`, `upload_precomputed_public_key_ids.py`),
  `device_type` is not passed as a keyword argument, so the early return never
  fires.
- The problematic call site (`eid_resolver.py:1792`) tries both flip variants
  (`candidates = [suggested_mcu, not suggested_mcu]`), so the wrong guess only
  reorders attempts without immediately failing.
- The bug becomes visible only when a non-MCU beacon device is present AND the
  incorrect flip variant happens to produce a key that passes AES-GCM tag
  verification (rare but possible with corrupted data).

**Prevention:**
- The `device_type` check should always be combined with a `fast_pair_model_id`
  check — `DEVICE_TYPE_BEACON` is a category, not a device identity.
- Test with a mock `device_type=1` device that has a non-MCU `fastPairModelId`.

---

## 3. `last_mode_switch` defaulting to `time.time()` (MEDIUM)

**File:** `NovaApi/ExecuteAction/LocateTracker/location_request.py:169-170, 597-598`

**Bug:** When no cached `last_mode_switch` exists, the fallback `int(time.time())`
tells Google's backend the contributor mode was just enabled, causing it to
return only reports from "now" forward — dropping all historical data between
polling intervals.

**Why it was missed:**
- The field name `lastHighTrafficEnablingTime` suggests it records when the
  user switched modes, not "since when should the server return reports."
- The semantics of this field are not publicly documented by Google.
- The bug is invisible when polling is frequent enough that no reports
  accumulate between intervals, or when the integration has been running long
  enough that the cached value persists.

**Prevention:**
- Default to 0 (epoch) or omit the field entirely to let the server decide.
- Add a test verifying that the first poll after integration setup receives
  historical reports.

---

## 4. `_api_push_ready()` over-blocking manual locate (LOW)

**File:** `coordinator/locate.py:287-292`

**Bug:** The hard block prevented manual locate requests whenever push transport
status was uncertain, even though the HTTP API call doesn't require push
readiness. The `can_play_sound()` method in the same file already implemented
the correct pattern (only block during active cooldown).

**Why it was missed:**
- The guard was added as a safety measure and the warning message clearly
  explained why locate was blocked.
- Manual locate is rarely used compared to polling, so fewer users hit this
  path.
- The inconsistency with `can_play_sound()` was not flagged because the two
  methods were written at different times.

**Prevention:**
- When adding guards, check if similar guards exist elsewhere in the same class
  and use a consistent pattern.
