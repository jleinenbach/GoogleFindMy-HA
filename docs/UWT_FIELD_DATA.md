# Unwanted tracking protection mode: field data behind the frame/bit reading (2026-10-11)

## What this file is the provenance for

The finder reports the protection mode it observed (`unwantedTrackingModeEnabled`
in the upload) and derives it with `_effective_uwt_mode()` in
`custom_components/googlefindmy/eid_resolver.py`: the hashed-flags bit when the
flags byte was read at a known position, otherwise the frame type (`0x41` on,
`0x40` off), otherwise unknown. When that bit and a `0x40`/`0x41` frame type
disagree, the mode is unknown (source `conflict`) and nothing is uploaded. The
comment next to the frame/bit logging in the same file cites this file.

That rule rests on the Find Hub Network accessory specification (retrieved
2026-10-11), which says for the mode: "The frame type should be set to 0x41.
The state is also reflected in the hashed flags section." and for leaving it:
"The frame type should be set back to 0x40." This file records what real
installations showed when both signals were logged next to each other. It also
replaces an older reading in this repository that tied `0x41` to the device
generation (P-256, 32-byte EIDs) instead of the mode; that reading was not based
on a recorded observation.

## How the data were collected

* Source A: the maintainer's own Home Assistant instance running this
  integration. `FMDN_FLAGS_PROBE` lines (logged once per device per resolver
  instance, on the first decode attempt) from the current and the previous
  `home-assistant.log`, and the state history of the per-device
  "Unwanted Tracking Mode" binary sensors from the recorder
  (`/api/history/period`, 2026-09-30 to 2026-10-11, the full retention depth).
* Source B: a public report, BSkando#210, comment of 2026-09-24 (two Motorola
  tags on one account, both at home with the owner's phone).
* Redaction: devices are replaced by stable labels (`tracker-A` to
  `tracker-F`, `Tag A`/`Tag B` as in the report). Times are UTC, rounded to the
  minute; durations are rounded to the second. No device names, identifiers,
  EIDs or coordinates are reproduced.

## Probe lines (source A)

18 lines: three probe passes over the same six trackers. Two passes ran on
2026-10-09 at 16:22 UTC, 140 ms apart, during a start of Home Assistant; one ran
on 2026-10-11 at 08:12 UTC after a restart. The instance has two accounts
loaded, and each of the two 2026-10-09 passes comes right after its own
`FMDN BLE scanner registered` line. The probe line is logged once per device
per resolver instance, so two passes mean the line was emitted by two
resolver instances or after a reset of that state; which of the two happened
was not established.

| Pass | Trackers | `observed_frame` | `uwt_mode` | `decoded` | `payload_len` |
|---|---|---|---|---|---|
| 2026-10-09, 1st | A | `0x41` | `True` | `0x03` | 22 |
| 2026-10-09, 1st | B to F | `0x40` | `False` | `0x02` | 22 |
| 2026-10-09, 2nd | A | `0x41` | `True` | `0x03` | 22 |
| 2026-10-09, 2nd | B to F | `0x40` | `False` | `0x02` | 22 |
| 2026-10-11 | A to F | `0x40` | `False` | `0x02` | 22 |

Totals: 16 lines `0x40` with bit 0, 2 lines `0x41` with bit 1 (the same tracker
in both passes of 2026-10-09, `flags_byte=0x53 decoded=0x03`). Frame type and
bit agree on every line. `FMDN_FLAGS_CONFLICT` lines: 0 in both log files.

Cross-check against the sensor history below: tracker-A's two `0x41` lines
(16:22 UTC on 2026-10-09) fall inside its sensor episode that began at 15:28 UTC
that day and lasted 5548 s. On 2026-10-11 the same tracker advertised `0x40`
with bit 0, and its sensor was `off`.

## Public report BSkando#210 (source B)

Four probe lines, all `observed_frame=0x40`, `uwt_mode=False`,
`payload_len=22` (trees `1.7.15.14` and `1.7.15.18`). The reporter states that
frame and flag agree on every line and that neither surviving log has an
`FMDN_FLAGS_CONFLICT` line.

Sensor episodes as reported: Tag A 4 episodes (12m 35s, 2m 01s, 3m 35s,
17m 08s), Tag B 12 episodes (1m 00s to 2h 14m 02s). The reporter notes that
Tag B's battery read 5 % and that its episodes may have a different cause.
The issue text gives the overall range as 38 seconds to 3.5 hours.

## Sensor episodes (source A)

Seven protection-mode sensors: four had no `on` episode in the window, one was
unavailable throughout, two had episodes:

| Tracker | Episodes | Durations (s) |
|---|---|---|
| tracker-A | 4 | 1150, 5773, 4642, 5548 |
| tracker-B | 9 | 90, 60, 1150, 60, 67, 1079, 1297, 180, 120 |

tracker-B's first episode starts at the first retained row of the recorder,
so it may have begun earlier. On three days both sensors switched `on` in the
same coordinator write (2026-10-04 and 2026-10-09) or 1.2 s apart
(2026-10-07); the same write means the same coordinator run, not the same
event.

Many episodes last one to three minutes. This fits the model in the
specification, where the Find Hub service turns the mode on and only a holder
of the tracker's identity key can turn it off again; that this is the owner's
phone once it sees the tracker is an inference, not something the
specification states. It does not fit a tracker that enters the mode on its
own after 30 minutes of separation, the criterion of the IETF DULT draft that
the specification maps the mode to. This is a reading of the data against the
two documents, not a measurement of the service.

## What this measurement does not establish

* Coverage: only 20-byte EIDs have been observed so far; no 32-byte EID
  observed (`payload_len=22` on every line). For trackers with 32-byte EIDs
  the frame/bit equivalence rests on the specification and on the public
  accessory firmware of Nordic (`nrfconnect/sdk-nrf`,
  `subsys/bluetooth/fast_pair/fhn/state.c`) and Atmosic (`Atmosic/openair`,
  `subsys/bluetooth/services/gfp/fmdn/`), where frame type and bit derive from
  the same state; not on a recording.
* Only six trackers of one account (five Motorola trackers and one
  card-format tracker of another vendor) and the two tags of the
  public report are covered. Accessory firmware of most vendors is not public.
* The criteria by which Google's service turns the mode on are not published.
  Nothing here says why or when the service switches a tracker into the mode.
* The probe line fires once per device per resolver instance, so it shows the state at a
  few instants, not every change. Continuous change records need the
  transition log that is planned separately.
