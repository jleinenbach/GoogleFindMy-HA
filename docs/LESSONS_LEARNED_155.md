# Lessons Learned — Issue #155 Bugfix Audit

## Background

A community user independently identified four code bugs using Gemini AI that
had gone unnoticed in the existing codebase. This document captures lessons
for future development.

---

## Lesson 1: Crypto code must be tested as round-trips, not in isolation

`calculate_r` (decryption) and `_derive_scalar` (EID generation) participate in
the same ECDH key agreement. Testing each function in isolation proved they
produced valid-looking outputs, but no test verified they produced **the same**
scalar for the same inputs.

**Action:** Every encrypt/decrypt pair and every EID-generate/EID-use pair must
have a round-trip integration test.

---

## Lesson 2: Enum values are not identities

`DEVICE_TYPE_BEACON = 1` is a **category** shared by multiple device types
(MCU trackers, Chipolo beacons, Pebblebee tags, etc.). Using it as a proxy for
"is MCU tracker" was a semantic error. The Fast Pair model ID (`"003200"`) is
the true identity marker.

**Action:** When detecting device capabilities, prefer device-specific
identifiers (model IDs, firmware signatures) over broad enum categories.

---

## Lesson 3: API field semantics may differ from field names

`lastHighTrafficEnablingTime` sounds like it records when a mode was enabled.
In practice, the server uses it to determine **which reports to return**. The
default of "now" caused silent data loss on every first poll.

**Action:** For undocumented API fields, default to the most permissive value
(0 / omit) rather than the most "reasonable-looking" one (`time.time()`).
Observe actual server behavior with different values before choosing defaults.

---

## Lesson 4: Guard patterns should be consistent within a class

`can_play_sound()` used cooldown-only blocking while `async_locate_device()`
used hard push-readiness blocking. Both methods serve the same purpose (user
actions requiring push transport) but applied inconsistent policies.

**Action:** When adding safety guards, grep for similar guards in the same
class/module and align behavior.

---

## Lesson 5: Community reports + AI tools can find bugs faster than reviews

The user combined their domain knowledge (observing the symptoms) with Gemini's
code analysis to pinpoint root causes across multiple files. This workflow —
"human identifies symptoms, AI traces through code" — is effective for finding
subtle cross-module bugs.

**Action:** Encourage community bug reports that include proposed fixes, even
if the fixes need refinement. The signal-to-noise ratio is high.
