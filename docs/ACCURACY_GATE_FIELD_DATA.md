# Accuracy gate: field data behind the thresholds (2026-09-06)

This file is the provenance for the numbers in
`tests/test_cache_accuracy_gate.py` (`FIELD_CASES`) and for the two constants
`ACCURACY_GATE_MIN_M` (200 m) and `ACCURACY_GATE_RATIO` (4) in
`custom_components/googlefindmy/coordinator/helpers/geo.py`.

They are not invented and not taken from a phone-tracking convention. They are
the outcome of replaying the planned rule over a real production Home Assistant
instance running this integration. Without this record the eleven regression
cases are just numbers again in six months.

The data are reproduced here in redacted form: device identifiers are replaced
by stable labels, and no absolute coordinates are published. Everything the
thresholds rest on (accuracies, ages, ratios, jump distances, state changes) is
kept verbatim.

## How the data were collected

* Source: the Home Assistant recorder database of the maintainer's own
  instance, opened read-only. 13.5 GB, 56,912,676 state rows.
* Window: 2026-08-27 to 2026-09-06, 10.4 days. That is the full retention depth
  of that recorder, not a choice.
* Selection: `device_tracker` entities of this integration, identified by the
  `google_device_id` attribute.
* Deduplication: one *location event* is a change of the triple
  (`latitude`, `longitude`, `gps_accuracy`) per device. The mirror entities
  (`_last_location`) are excluded: they carry the same `google_device_id` and
  would double every case.
* The owner's phone is evaluated separately from the trackers; it otherwise
  dominates the distribution.

598,016 raw rows produced roughly 30,350 deduplicated tracker location events
across seven devices.

## Reported accuracy by source

`source_label` is assigned by `row_source_label()` in
`custom_components/googlefindmy/coordinator/helpers/cache.py`.

| Source | n | Median | p90 | Maximum | Share above 200 m |
|---|---|---|---|---|---|
| `owner` (the device itself) | 1004 | 20.0 m | 20.0 m | **176.5 m** | 0.0 % |
| `aggregated` (someone else's finder device) | 137 | 62.3 m | 288.5 m | **481.0 m** | 19.7 % |
| `crowdsourced` | 7 | 65.8 m | 303.8 m | 303.8 m | 14.3 % |
| `semantic/unknown` | 29,196 | 6.8 m | 20.0 m | 200.0 m | 0.0 % |

The owner's phone, for comparison (n=4560): median 5.0 m, p99 100.0 m, max
400.0 m.

**This is why the lower bound is 200 m and not 100 m.** No `owner` fix in ten
days exceeded 176.5 m, so a 200 m floor never touches a fix the device reported
about itself. The removed predecessor (`min_accuracy_threshold`, default 100 m)
sat below that ceiling and therefore discarded the device's own reports.

## Sensitivity of the two parameters

Rule as simulated: discard when `new_acc > MIN` **and**
`new_acc > RATIO * existing_acc` **and** the cached fix is younger than the
staleness threshold.

Effect of the staleness threshold (MIN 200 m, ratio 4):

| Staleness threshold | discarded | of which `owner` |
|---|---|---|
| 300 s | 0 | 0 |
| 900 s | 5 | 0 |
| 1800 s | 9 | 0 |
| **3900 s** (`DEFAULT_STALE_THRESHOLD`) | **11** | **0** |
| 7200 s | 14 | 0 |
| 86400 s | 17 | 0 |

Effect of floor and ratio (staleness threshold 3900 s):

| | ratio 2 | ratio 3 | ratio 4 | ratio 6 | ratio 8 |
|---|---|---|---|---|---|
| MIN 100 m | 39 | 33 | **30** (7 of them `owner`) | 23 | 22 |
| MIN 150 m | 25 | 20 | 18 | 13 | 13 |
| **MIN 200 m** | 16 | 13 | **11** (0 of them `owner`) | 9 | 9 |
| MIN 300 m | 8 | 5 | 5 | 4 | 4 |
| MIN 500 m | 0 | 0 | 0 | 0 | 0 |

Two readings of that grid decided the constants:

* MIN 100 m discards seven of the device's own fixes; MIN 200 m discards none.
* MIN 500 m discards nothing at all on this instance, so a higher floor would
  make the gate inert here.

## The eleven discarded fixes

All eleven carry `source_label: aggregated` and `location_status: current`;
none is an `owner` fix. `tracker-A` and `tracker-B` are two distinct trackers;
the remaining five devices produced no fix above 200 m at all.

| Device | `new_acc` | cached acc | ratio | age | jump | state change |
|---|---|---|---|---|---|---|
| tracker-A | 404.9 m | 94.5 m | **4.3** | 343 s | **18,294 m** | not_home to not_home |
| tracker-B | 458.8 m | 25.8 m | 17.8 | 345 s | 5,542 m | **not_home to home** |
| tracker-B | 208.8 m | 5.5 m | 38.1 | 972 s | 4,782 m | not_home to not_home |
| tracker-A | 319.0 m | 3.0 m | 105.3 | 687 s | 1,856 m | not_home to not_home |
| tracker-A | 331.8 m | 32.2 m | 10.3 | 918 s | 1,126 m | not_home to not_home |
| tracker-B | 456.3 m | 18.3 m | 25.0 | 403 s | 1,113 m | not_home to not_home |
| tracker-A | 202.4 m | 3.8 m | 53.2 | 342 s | 750 m | not_home to not_home |
| tracker-A | 273.7 m | 8.9 m | 30.9 | 2,734 s | 413 m | not_home to not_home |
| tracker-B | 204.5 m | 10.1 m | 20.2 | 902 s | 402 m | not_home to not_home |
| tracker-A | 281.1 m | 62.5 m | 4.5 | 1,521 s | 399 m | not_home to not_home |
| tracker-A | 210.8 m | 15.8 m | 13.3 | 2,851 s | 281 m | not_home to not_home |

**Why the ratio is 4 and not 6.** The worst case in this table, an 18 km jump,
sits at ratio 4.3. A ratio of 4 catches it; a ratio of 6 lets it through. The
number is pinned by its own boundary case, not chosen for roundness.

## The documented zone false positive

The single `not_home -> home` row above is the damage this gate exists to
prevent, and it is a recorded incident, not a construction:

* The previous fix had 25.8 m accuracy and put the tracker 5,603 m from
  `zone.home`, state `not_home`.
* The coarse fix had 458.8 m accuracy, `source_label: aggregated`, and was in
  truth 345.9 m from `zone.home` - so the tracker was **not** home.
* `zone.home` has a 32 m radius on that instance.
* Home Assistant evaluates `zone_dist - zone_radius < accuracy`, that is
  `345.9 - 32 = 313.9 < 458.8`, and writes `home`. The recorder stores
  `in_zones: ["zone.home"]` for that state.
* Without the accuracy tolerance (`345.9 < 32`) it would correctly have stayed
  `not_home`.

The harm is not a wrong dot on a map. It is a wrong presence, and presence
drives automations.

## What this measurement does not establish

* **The extreme case is missing.** The maximum over all events is 481 m. The
  1600 m radius reported in the upstream issue does not occur here. This
  instance sits in an area with a dense finder network; the data say nothing
  about rural coverage. That is why the integration also counts the accuracy
  distribution at runtime (`accuracy_bucket` statistics) instead of treating
  these thresholds as settled.
* **Two devices carry the findings.** The eleven cases fall on two trackers.
* **Ten days is the ceiling**, not a choice: the recorder held no more.
* **One neighbouring finding, out of scope here:** one device produced 29,639
  of the events at consistently 20 m or better, i.e. position jitter without any
  accuracy change. That is material for the jitter issues, not for this gate.
