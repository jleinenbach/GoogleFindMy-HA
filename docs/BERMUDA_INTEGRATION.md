# Bermuda BLE Integration

This document explains how Google Find My Device (GoogleFindMy-HA) cooperates
with the [Bermuda BLE Trilateration](https://github.com/agittins/bermuda)
integration on Home Assistant, what each side contributes, and how to enable
the bidirectional flow.

Two independent capabilities are described:

1. **EID Resolver API** — Bermuda detects FMDN BLE advertisements locally and
   asks GoogleFindMy to map them to the trackers you already own. This path is
   read-only on the Find My side and is always available.
2. **FMDN Finder uploads** — When Bermuda decides that a tracker has moved to a
   new area, GoogleFindMy can push that semantic location to Google's Find Hub
   network as if your Home Assistant were a regular Finder. This path is
   opt-in and disabled by default.

> [!NOTE]
> The Bermuda integration on the other side must be the
> [jleinenbach/bermuda](https://github.com/jleinenbach/bermuda) fork. Upstream
> Bermuda does not yet ship the GoogleFindMy-specific consumer code.

## Why a bridge at all?

Google's Find Hub network is sparse outside dense urban areas. If you already
have several ESPHome BLE proxies or Bluetooth-Low-Energy scanners running at
home, Bermuda can detect the same FMDN frames that Find Hub sees, locally and
without contacting Google. Combining both sides gives you:

| Need | Source |
| :--- | :----- |
| Live coordinates while at home (no cloud roundtrip) | Bermuda |
| Coordinates while away (Find Hub network) | GoogleFindMy |
| Stable Home Assistant device that survives entity rename / MAC rotation | GoogleFindMy (`google_device_id`) |
| Optional contribution of your scanners back into Find Hub | GoogleFindMy FMDN Finder |

## Architecture

```
   ┌───────────────────────────────┐
   │  BLE scanner (ESPHome / hci)  │
   └───────────────┬───────────────┘
                   │ FMDN advertisements
                   ▼
   ┌───────────────────────────────┐                ┌────────────────────────────┐
   │  Bermuda (jleinenbach fork)   │  EID  query    │  GoogleFindMy-HA           │
   │  device_tracker.*_bermuda_*   │ ─────────────► │  eid_resolver.py           │
   │                               │ ◄───────────── │  resolves EID → device id  │
   │  attaches its tracker entity  │  device id     │                            │
   │  to the SAME HA device via    │                │  serves stable             │
   │  registry congealment         │                │  `google_device_id`        │
   └───────────────┬───────────────┘                └─────────────┬──────────────┘
                   │ area change                                  ▲
                   │ (state attribute "area")                     │ FMDN upload
                   ▼                                              │ (semantic location)
   ┌───────────────────────────────┐                              │
   │  EVENT_STATE_CHANGED listener │ ─────────────────────────────┘
   │  fmdn_finder/bermuda_listener │
   └───────────────────────────────┘
```

Key implementation pointers (1.7.0-5):

- `custom_components/googlefindmy/eid_resolver.py` — Computes ephemeral
  identifiers (EID) from your trackers' identity keys and exposes a lookup
  service to other integrations.
- `custom_components/googlefindmy/fmdn_finder/bermuda_listener.py` — Subscribes
  to `EVENT_STATE_CHANGED` on `device_tracker.*_bermuda_tracker*`, debounces
  area changes for 30 s, and triggers an FMDN upload.
- `custom_components/googlefindmy/fmdn_finder/location_uploader.py` and
  `google_uploader.py` — Encrypt and send the semantic location to Google.
- `custom_components/googlefindmy/const.py` — `FEATURE_FMDN_FINDER_ENABLED`
  feature flag (default `False`).

## Device matching — read this before you debug

Bermuda attaches its tracker entity to the **existing** Home Assistant device
that GoogleFindMy already created (a process Bermuda calls "congealment"):

```
┌─────────────────────────────────────────────────────────────────┐
│  ONE Home Assistant device (e.g. "moto tag Schlüsselbund")      │
│  device_id: 11b2838b4bb2ba2eb5f4f4b2c742cbf9                    │
│                                                                 │
│  ┌─────────────────────────┐  ┌─────────────────────────────┐   │
│  │ GoogleFindMy entity     │  │ Bermuda entity              │   │
│  │ device_tracker.moto_... │  │ device_tracker.moto_..._2   │   │
│  │ platform: googlefindmy  │  │ platform: bermuda           │   │
│  └─────────────────────────┘  └─────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

The matcher in `bermuda_listener.py` resolves the Bermuda entity to its HA
`device_id`, walks the entity registry for that device, and selects the
GoogleFindMy `device_tracker` entity to read the canonical
`google_device_id` from. **It must not** match on:

- Entity names — users rename them.
- Bluetooth MAC addresses — FMDN MACs rotate for privacy.
- Bermuda-side device identifiers — they do not carry `googlefindmy` tuples.

If your trackers are not being matched, check that both entities really sit
under the same HA device, not two separate ones.

## Capability 1 — EID Resolver API (always on)

The EID resolver is a stable service interface other integrations can call to
translate an ephemeral identifier emitted over the air into the
`google_device_id` you see in Home Assistant. It is the contract Bermuda relies
on. See [Ephemeral_Identifier_Resolver_API.md](Ephemeral_Identifier_Resolver_API.md)
for the public surface and [EID_RESOLVER_PIPELINE.md](EID_RESOLVER_PIPELINE.md)
for the internal cache pipeline.

No user action is needed beyond installing both integrations and pairing your
trackers. Bermuda discovers GoogleFindMy automatically.

## Capability 2 — FMDN Finder uploads (opt-in)

When `FEATURE_FMDN_FINDER_ENABLED = True`, GoogleFindMy registers an event
listener that:

1. Observes Bermuda's `device_tracker.*_bermuda_tracker*` state changes.
2. Waits 30 seconds (`AREA_STABILIZATION_SECONDS`) for the new area to stay
   stable, suppressing flapping.
3. Enforces a 60-second minimum interval per device
   (`MIN_UPLOAD_INTERVAL_SECONDS`) to respect FMDN throttling.
4. Encrypts the semantic location end-to-end and uploads a Finder report to
   Google's FMDN backend on your behalf.

**Privacy posture.** Uploaded reports are end-to-end encrypted with your
account keys; only the device owner can decrypt them. Your Home Assistant then
acts like any other Finder phone in the Find Hub crowd. The feature is
off by default precisely because contributing as a Finder is a deliberate
choice, not a side effect of installing the integration.

### Enabling FMDN Finder

The feature flag is currently a compile-time constant. To enable it on
1.7.0-5:

1. Edit `custom_components/googlefindmy/const.py`:
   ```python
   FEATURE_FMDN_FINDER_ENABLED: bool = True
   ```
2. Restart Home Assistant.
3. Watch the log for the line
   `Setting up FMDN Finder integration` followed by
   `FMDN Finder setup complete`.
4. Trigger an area change in Bermuda (move a tag between two known areas) and
   confirm an upload entry appears in the GoogleFindMy log section.

A future release may promote this to a runtime option in the config flow. Until
then, treat enabling FMDN Finder as a power-user opt-in.

## Setup checklist

1. Install GoogleFindMy-HA via HACS or manual copy and pair at least one
   tracker.
2. Install the [jleinenbach/bermuda](https://github.com/jleinenbach/bermuda)
   fork as a custom repository in HACS.
3. Ensure at least one BLE scanner is reachable by Home Assistant (ESPHome
   `bluetooth_proxy:`, Shelly Pro, Pi with a working `hci0`, etc.).
4. Reload both integrations after pairing. The same physical tag should appear
   as **one** HA device with **two** `device_tracker` entities.
5. (Optional) Flip `FEATURE_FMDN_FINDER_ENABLED` to `True` to contribute area
   updates back to Google's Find Hub network.

## Troubleshooting

| Symptom | Likely cause | Resolution |
| :------ | :----------- | :--------- |
| Bermuda entity appears as a separate HA device next to GoogleFindMy. | Pairing happened before the EID resolver had populated the cache, so Bermuda could not congeal. | Reload Bermuda, or in stubborn cases manually merge the two devices via Settings → Devices → "merge". |
| No FMDN upload log entry after enabling the feature. | The feature flag is still `False` in `const.py`, or no Bermuda area change has stabilized for 30 s. | Re-check the flag; trigger a clear area transition (e.g. move tag from `Office` to `Hallway`). |
| EID resolver returns no match. | Identity keys not yet computed for the device, or the device is in a fresh-pair state. | Wait for the next coordinator refresh cycle (`location_poll_interval`, default 300 s) and try again. |
| Multiple Google devices match one EID. | Account contains duplicate paired trackers (e.g. test entries). | Resolve duplicates in the Find Hub app on your phone first. |

## References

- Bermuda fork that consumes this API: <https://github.com/jleinenbach/bermuda>
- EID Resolver public API: [Ephemeral_Identifier_Resolver_API.md](Ephemeral_Identifier_Resolver_API.md)
- EID Resolver cache pipeline: [EID_RESOLVER_PIPELINE.md](EID_RESOLVER_PIPELINE.md)
- FMDN protocol background: [FMDN.md](FMDN.md), [FMDN_ENDPOINT_DISCOVERY.md](FMDN_ENDPOINT_DISCOVERY.md), [FMDN_UPLOAD_LIMITATION.md](FMDN_UPLOAD_LIMITATION.md)
- Cryptography overview: [CRYPTOGRAPHY.md](CRYPTOGRAPHY.md)
- Source modules: `custom_components/googlefindmy/fmdn_finder/`
