# EID resolver refresh pipeline (deterministic, testable)

This note summarizes the refactored `_refresh_cache` pipeline inside `custom_components/googlefindmy/eid_resolver.py`. The goal is to make the resolver’s cache rebuild deterministic, observable, and testable without altering external behavior.

## Pipeline stages

1. **Collect identities** — `_collect_device_secrets` retrieves active `DeviceIdentity` objects. The new `_collect_work_items` wrapper normalizes each identity (canonical ID trimming, identity key bytes) and prepares an optional lock context.
2. **Lock hygiene** — `_prepare_work_item` validates stored locks:
   - Drops legacy/invalid locks (missing or non-integer rotation timestamps) and clears stale time-basis hints.
   - Updates stored canonical IDs when they diverge from the normalized identifier and schedules a persisted lock save.
3. **Time window computation** — `_compute_time_windows` derives rotation-aligned timestamps per strategy:
   - `lock_tracking` windows (±2 rotations from the persisted lock) when a valid rotation timestamp exists.
   - Absolute (`unix`) and relative (`pair_date`, `secrets_creation_date`) windows when no lock timestamp is available.
   - Drift allowances are derived from device “age” with configurable window sizes via `RotationParams`.
   - Stale time-basis hints that no longer match supported bases are dropped to avoid empty strategy sets.
4. **Variant selection** — `_compute_variants` yields variant specifications for each window. Persisted locks constrain the variant; otherwise, all supported variants are considered.
5. **EID generation** — `_generate_eids_from_spec` calls `_generate_variant` once per specification and emits both forward and reversed EID bytes (when enabled) as `GeneratedEid` objects. Defensive debug logging is kept around failures.
6. **Registration** — `CacheBuilder.register_eid` enforces collision policy (lowest absolute semantic offset wins), merges `timestamp_bases`, and guarantees lookup/metadata coherence.
7. **Finalization** — `CacheBuilder.finalize` validates invariants (`lookup` keys == `metadata` keys), patches discrepancies defensively, and hands the finalized dictionaries back to the resolver.

## Core invariants

- `set(_lookup.keys()) == set(_lookup_metadata.keys())` after every refresh.
- Each `EIDMatch.time_offset` reflects the semantic offset recorded in the metadata entry.
- Collision policy is deterministic: the match with the smallest absolute offset replaces prior entries, and all observed timestamp bases are preserved.
- Generation never registers partial entries: a metadata record is created alongside every lookup record.

## Observability

- Debug-level markers trace refresh stages: start, work-item count, per-device window groups, finalize counts, and overall cache size.
- Lock persistence scheduling warns when no task is created, avoiding silent failures.
- EID generation errors are logged at debug level with variant, basis, and timestamp context.

## Bug fixes uncovered during refactor

- Stale or invalid time-basis hints are cleared when a persisted lock is dropped as legacy/invalid, preventing empty strategy sets on refresh.
- Lock persistence scheduling now warns when a task helper returns `None`, eliminating silent failures that previously left locks unsaved.
- Finalization enforces lookup/metadata parity, patching (and logging) any drift detected during registration.

## Testing focus

- **Determinism**: Fixed `now_unix` with stubbed generation produces stable caches across refreshes.
- **Invariants**: Lookup and metadata keys match after refresh and collision resolution.
- **Candidate extraction**: Framed payloads surface the correct slices; truncated payloads are tolerated without crashes.
- **Collision policy**: Smallest absolute semantic offset wins, with merged timestamp bases retained in metadata.
- **Lock updates**: Successful matches schedule lock persistence and retain meaningful time-basis metadata.
