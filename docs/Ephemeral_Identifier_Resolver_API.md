# Google Find My Device – Ephemeral Identifier Resolver API

This document describes the **Ephemeral Identifier (EID) Resolver API** exposed by the `googlefindmy` Home Assistant integration. It is intended for developers of other integrations – for example, a local BLE–scanner integration such as **Bermuda** – that want to map **Find My Device Network** (FMDN) ephemeral identifiers to specific Home Assistant devices.

---

## Overview

Some Google Find My–compatible devices periodically broadcast **rotating BLE identifiers** (EIDs). These identifiers:

* Change on a fixed rotation period.
* Are derived from a per-device secret key.
* Are not directly stable device identifiers.

The `googlefindmy` integration already knows:

* Which Find My devices exist,
* Their identity keys (for EID derivation),
* Which devices are enabled and not ignored in Home Assistant.

The **EID Resolver API** exposes this knowledge to other integrations so that, given a raw EID from a BLE scan, you can efficiently resolve it to:

* A **Home Assistant device registry ID** (`device_id`), and
* The owning **config entry** and **canonical integration ID**.

The resolver precomputes EIDs for the **previous, current, and next rotation window** for all active trackers and keeps them in an in-memory lookup table. Resolution is a constant-time map lookup and never performs cryptographic work on the hot path.

---

## Before you begin

### Requirements

To use this API, your integration must run in the same Home Assistant instance as `googlefindmy` and meet these requirements:

* `googlefindmy` integration version **1.7.0-3 or later** (EID resolution support).
* The user has set up at least one Google Find My account and devices.
* You are able to obtain the **raw EID as bytes** from your BLE stack (or convert from a hex string).

### Recommended Home Assistant manifest settings (for Bermuda)

If your integration wants to use EID resolution when available, but remain optional, declare an **ordering dependency** on `googlefindmy`:

```jsonc
// custom_components/bermuda/manifest.json
{
  "domain": "bermuda",
  "name": "Bermuda BLE Scanner",
  // Ensure googlefindmy (if installed) is initialized before you access hass.data["googlefindmy"]
  "after_dependencies": ["googlefindmy"],
  // Do NOT list it in "dependencies" unless you want to hard-require it.
  "dependencies": []
}
```

This ensures:

* Your integration will be set up **after** `googlefindmy` if it is installed.
* Your code can safely read `hass.data["googlefindmy"]` without racing `async_setup_entry`.

---

## Key concepts

### Device registry ID vs. canonical ID

The resolver deals with two different device identifiers:

* **Device registry ID** (`device_id` in the resolver API)
  – The opaque identifier assigned by Home Assistant’s **Device Registry**.
  – Used when interacting with Home Assistant APIs (e.g. looking up entities).

* **Canonical ID** (`canonical_id` in the resolver API)
  – The integration-specific, namespaced identifier used internally by `googlefindmy` (for example, `"{entry_id}:{device_id}"`).
  – Used to correlate with the `googlefindmy` API payloads and internal caches.

Your integration will generally:

1. Use `device_id` to query the Device Registry / Entity Registry.
2. Optionally use `canonical_id` if you want to correlate with `googlefindmy`–specific diagnostics.

### Rotation windows

The resolver precalculates EIDs for **three time windows** per device:

* Previous rotation period,
* Current rotation period,
* Next rotation period.

This makes resolution robust to:

* Minor time skew between the BLE stack and Home Assistant.
* Boundary conditions during rotation.

You do not need to manage rotation windows yourself; just pass the current EID.

---

## Accessing the resolver from another integration

The resolver is exposed via `hass.data` under the `googlefindmy` domain.

### Location in `hass.data`

```python
bucket = hass.data.get("googlefindmy")
resolver = None
if isinstance(bucket, dict):
    resolver = bucket.get("eid_resolver")
```

If present, `eid_resolver` is an instance of `GoogleFindMyEIDResolver`.

### Optional type import (for IDEs / type checkers)

If you want static typing or isinstance checks, you can import the class:

```python
from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver

bucket = hass.data.get("googlefindmy")
resolver: GoogleFindMyEIDResolver | None = None

if isinstance(bucket, dict):
    candidate = bucket.get("eid_resolver")
    if isinstance(candidate, GoogleFindMyEIDResolver):
        resolver = candidate
```

> Best practice: Treat `GoogleFindMyEIDResolver` as a **stable API surface** for EID resolution, but avoid calling its internal methods (for example, `_collect_device_secrets` or `async_refresh`). External integrations should only call `resolve_eid` or `get_resolved_eid`.

---

## Core API

### `EIDMatch`

The resolver returns an `EIDMatch` object when an EID is recognized:

```python
from typing import NamedTuple

class EIDMatch(NamedTuple):
    """Resolved mapping between an EID and a Home Assistant device.

    The `device_id` corresponds to the Home Assistant device registry
    identifier; `canonical_id` retains the integration-specific identifier
    used by the API payloads.
    """

    device_id: str          # Home Assistant device registry ID
    config_entry_id: str    # Config entry ID that owns this device
    canonical_id: str       # Integration-specific device identifier
```

You can treat `EIDMatch` as immutable and safe to cache for the duration of a scan session.

---

### `GoogleFindMyEIDResolver`

#### Method: `resolve_eid(eid_bytes: bytes) -> EIDMatch | None`

Resolves a raw EID to a Home Assistant device.

* **Parameters**

  * `eid_bytes`: The raw ephemeral identifier exactly as seen in the BLE advertisement, as a `bytes` object.

* **Returns**

  * An `EIDMatch` if the EID corresponds to a known, active, non-ignored tracker.
  * `None` if the EID is unknown or currently not mapped to any active tracker.

* **Behavior**

  * Performs a constant-time lookup in an in-memory cache.
  * Does **not** perform crypto or network I/O.
  * Does **not** log the EID value; only the matched device ID at debug level.

**Example**

```python
from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver

def handle_ble_advertisement(hass, eid_hex: str) -> None:
    """Callback invoked by Bermuda for each Find My EID advertisement."""

    # Convert hex string to bytes, if that is how your BLE stack exposes it.
    try:
        eid_bytes = bytes.fromhex(eid_hex)
    except ValueError:
        # Not a valid hex EID; ignore.
        return

    bucket = hass.data.get("googlefindmy")
    if not isinstance(bucket, dict):
        return

    resolver = bucket.get("eid_resolver")
    if not isinstance(resolver, GoogleFindMyEIDResolver):
        # googlefindmy not loaded or no EID resolver available
        return

    match = resolver.resolve_eid(eid_bytes)
    if match is None:
        # Unknown EID; either not a Google Find My device known to this account,
        # or the device is disabled/ignored in HA.
        return

    device_id = match.device_id
    config_entry_id = match.config_entry_id
    canonical_id = match.canonical_id

    # From here, you can fetch additional info via the device registry.
    device_reg = hass.helpers.device_registry.async_get(hass)
    device = device_reg.async_get(device_id)
    if device is None:
        return

    # Example: log/annotate which tracker was seen.
    name = device.name or canonical_id
    _LOGGER.debug(
        "Bermuda detected Google Find My tracker: device_id=%s, name=%s, entry=%s",
        device_id,
        name,
        config_entry_id,
    )
```

---

#### Method: `get_resolved_eid(eid_bytes: bytes) -> str | None`

Convenience wrapper that returns only the Home Assistant **device registry ID**.

* **Parameters**

  * `eid_bytes`: Raw ephemeral identifier in bytes.

* **Returns**

  * `device_id: str` if the EID matches a known tracker.
  * `None` otherwise.

This mirrors the legacy “string only” behavior and is sufficient when you only need the device registry ID.

**Example**

```python
device_id = resolver.get_resolved_eid(eid_bytes)
if device_id is None:
    return

device_reg = hass.helpers.device_registry.async_get(hass)
device = device_reg.async_get(device_id)
# ...
```

---

## How the resolver works (for integration authors)

You normally do not have to manage the resolver lifecycle, but understanding it will help you design robust integrations.

### Lifecycle and caching

* The resolver is created and owned by the `googlefindmy` integration.
* A single shared instance is stored under `hass.data["googlefindmy"]["eid_resolver"]`.
* On startup and each relevant change, the resolver:

  * Collects **active** device identities from all loaded `googlefindmy` coordinators (per account).
  * Precomputes EIDs for the previous, current, and next rotation window.
  * Stores them in an internal map: `Map[EID bytes -> EIDMatch]`.

### Which devices are included

The resolver only includes devices that are:

* Associated with a `googlefindmy` config entry,
* Not disabled in the Home Assistant Device Registry (`device.disabled_by is None`),
* Not marked as ignored in `googlefindmy` options,
* Currently **eligible for polling** (`_enabled_poll_device_ids` in the coordinator).

This ensures that:

* Bermuda (or other consumers) only sees **user-visible, active** trackers.
* Ignored or disabled devices are treated as “unknown EIDs”.

---

## Best practices for Bermuda

### 1. Resolve only on the HA event loop

Both `resolve_eid` and `get_resolved_eid` are **synchronous** and designed to be called on the Home Assistant event loop thread. Typical usage:

* BLE callbacks scheduled via `hass.add_job` or `hass.add_executor_job` should *hop* back to the event loop before calling into Home Assistant APIs.

### 2. Treat the resolver as optional

Always code defensively:

```python
bucket = hass.data.get("googlefindmy")
if not isinstance(bucket, dict):
    # googlefindmy is not loaded; skip EID resolution
    return

resolver = bucket.get("eid_resolver")
if not isinstance(resolver, GoogleFindMyEIDResolver):
    return
```

Your integration should continue functioning (with reduced features) when:

* `googlefindmy` is not installed,
* Or the user has no Find My devices configured.

### 3. Avoid calling internal methods

Do **not** call:

* `async_refresh`
* `_refresh_cache`
* `_collect_device_secrets`
* Or any private attributes/methods on the resolver.

The `googlefindmy` integration manages cache lifetime and refresh cadence. External callers should only use:

* `resolve_eid(eid_bytes)`
* `get_resolved_eid(eid_bytes)`

### 4. Don’t log or persist raw EIDs

To protect user privacy:

* Avoid logging EIDs in plaintext or storing them in long-term logs.
* If you must log for debugging, prefer:

  * The `device_id`,
  * The `config_entry_id`,
  * Or an anonymized hash of the EID.

The resolver itself never logs raw EIDs.

---

## Error handling and edge cases

### Unknown EIDs

`resolve_eid`/`get_resolved_eid` return `None` when:

* The EID belongs to a device not known to any configured Google Find My account.
* The device exists but is disabled or ignored.
* There is transient state (e.g., resolver just started and hasn’t cached the device yet).

Your integration should **treat `None` as “no match”** and quietly move on.

### Resolver not present

If the resolver is not found in `hass.data`, you should:

* Skip resolution,
* Avoid raising errors or breaking core functionality.

This is expected when:

* The user has not installed `googlefindmy`,
* Or uses an older version without the resolver API.

---

## End-to-end example: Integrating Bermuda with EID resolution

Below is a simplified sketch for a BLE-scanner integration like Bermuda that wants to map EIDs to Home Assistant devices.

```python
from __future__ import annotations

import logging
from typing import Optional

from homeassistant.core import HomeAssistant, callback
from custom_components.googlefindmy.eid_resolver import GoogleFindMyEIDResolver

_LOGGER = logging.getLogger(__name__)


class BermudaScanner:
    """Example BLE scanner that consumes FMDN EIDs and uses googlefindmy for resolution."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    def _get_eid_resolver(self) -> Optional[GoogleFindMyEIDResolver]:
        bucket = self.hass.data.get("googlefindmy")
        if not isinstance(bucket, dict):
            return None
        candidate = bucket.get("eid_resolver")
        if isinstance(candidate, GoogleFindMyEIDResolver):
            return candidate
        return None

    @callback
    def handle_fmdn_advertisement(self, eid_bytes: bytes) -> None:
        """Handle a single FMDN BLE advertisement."""
        resolver = self._get_eid_resolver()
        if resolver is None:
            return

        match = resolver.resolve_eid(eid_bytes)
        if match is None:
            # No known Find My tracker behind this EID
            return

        device_id = match.device_id
        device_reg = self.hass.helpers.device_registry.async_get(self.hass)
        device = device_reg.async_get(device_id)
        if device is None:
            return

        # Example: update an internal map of "seen trackers" for UX or unwanted-tracking tooling.
        _LOGGER.debug(
            "Bermuda detected Google Find My tracker: device_id=%s, name=%s, entry=%s",
            device_id,
            device.name or match.canonical_id,
            match.config_entry_id,
        )
```

---

## Versioning and compatibility

The EID Resolver API is designed to be **stable for external consumers**:

* `resolve_eid(eid_bytes)` and `get_resolved_eid(eid_bytes)` are the primary supported entry points.
* `EIDMatch` may gain additional fields in the future; external code should:

  * Access known attributes by name,
  * Ignore unknown attributes.

If a future version of `googlefindmy` changes the internal representation, it will preserve the external contract of these methods.

For maximum robustness:

* Guard access to the resolver (feature-detection).
* Do not rely on internal attributes or undocumented behavior.
* Treat failure to resolve as “no match” rather than an error.
