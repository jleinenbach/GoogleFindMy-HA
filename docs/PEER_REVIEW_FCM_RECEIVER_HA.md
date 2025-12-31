# Peer Review: `fcm_receiver_ha.py`

**Reviewer**: Claude Code (Senior HA Integration Engineer)
**Date**: 2025-12-31
**Scope**: EID Pipeline Integration (Locate/FCM → `decrypt_locations.py` → `coordinator.py` → `eid_resolver.py`)

---

## 1) Architecture and Flow Validation

### A) Ingress Path: `_on_notification` (Sync Callback)

The ingress arrives through `_on_notification(...)` defined at line 983. This is a **synchronous callback** that:

1. Extracts the hex payload via `_extract_hex_payload` (line 994)
2. Extracts the canonical ID via `_extract_canonic_id_from_response` (line 998)
3. Determines routing targets via `_route_target_entries` (line 1004)
4. Either serves a Locate callback OR starts a background update (lines 1009-1040)

**Key Implication for EID/HIT/MISS**: If a per-request callback exists (`location_update_callbacks[canonic_id]`), the background decrypt/normalization path is bypassed, and only the callback receives `(canonic_id, hex_string)`. Therefore, any EID-resolver refresh/resolve that must happen from Locate must occur downstream (in the callback/Locate flow). `fcm_receiver_ha.py` is not itself the EID resolver; it is an ingress + routing/debounce layer.

### B) Background Push Path: `_process_background_update` → Debounce → `_flush`

Background push is started via `asyncio.create_task(self._process_background_update(...))` at line 1036.

`_process_background_update` (line 1339):
- Decodes/decrypts via `_decode_background_location_async`
- Injects `last_updated` timestamp
- Stores payload in `_pending` with routing targets in `_pending_targets`
- Triggers debounce flush via `_schedule_flush`

Debounce/flush is logically sound:
- Cancel + reschedule pattern (lines 1383-1399)
- Fan-out only to target coordinators (line 1414)

### C) Decode/Decrypt in `_decode_background_location_async`

At line 1446:
- Protobuf is parsed synchronously via `decoder_module.parse_device_update_protobuf`
- Locations are decrypted via `async_decrypt_location_response_locations(..., cache=cache)`
- A "best record" is chosen based on timestamps and coordinates/altitude (lines 1480-1508)

---

## 2) P0/P1 Findings (Risks and Recommendations)

### P0 — Event Loop / Thread Safety of Sync `_on_notification`

**Location**: Lines 983-1044

**Current Behavior**:
`_on_notification` is synchronous but calls `asyncio.create_task(...)` directly (lines 1012, 1036).

**Risk**: If the underlying FCM client invokes this callback from a non-event-loop thread, `asyncio.create_task` may:
1. Fail with "no running event loop" (RuntimeError)
2. Schedule onto a different loop (undefined behavior)

The current `try/except` at line 1042 may swallow this and effectively drop updates, manifesting as "push arrives but nothing happens."

**Verification Required**: Check `FcmPushClient` callback threading model. If it guarantees HA loop execution, this is not a P0.

**Recommendation**: Use loop-safe scheduling. Dispatch into the HA loop via `self._hass.loop.call_soon_threadsafe(...)` or `asyncio.run_coroutine_threadsafe(...)`.

**Acceptance Criterion**: A test that calls `_on_notification` from a worker thread must not raise and must still start exactly one background task.

#### Minimal Patch: Loop-Safe Dispatch + Task Tracking

```python
from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

_LOGGER = logging.getLogger(__name__)


class FcmReceiverHA:
    # ... existing __init__ ...

    # Add to __init__:
    # self._active_tasks: set[asyncio.Task[Any]] = set()

    def _dispatch_to_hass_loop(self, coro: Coroutine[Any, Any, Any], *, label: str) -> None:
        """Dispatch a coroutine into the HA event loop from any thread safely."""
        try:
            loop = asyncio.get_running_loop()
            # Already in an event loop - schedule directly
            task = loop.create_task(coro, name=f"{DOMAIN}.{label}")
            self._track_task(task, label=label)
            return
        except RuntimeError:
            # No running loop in this thread; schedule onto HA loop
            pass

        hass_loop = getattr(self._hass, "loop", None) if self._hass else None
        if hass_loop is None:
            _LOGGER.error("FCM notification dropped: Home Assistant loop not available")
            return

        def _schedule() -> None:
            task = hass_loop.create_task(coro, name=f"{DOMAIN}.{label}")
            self._track_task(task, label=label)

        hass_loop.call_soon_threadsafe(_schedule)

    def _track_task(self, task: asyncio.Task[Any], *, label: str) -> None:
        """Ensure task exceptions are retrieved and logged (prevents 'never retrieved')."""
        self._active_tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._active_tasks.discard(t)
            try:
                exc = t.exception()
            except asyncio.CancelledError:
                return
            except Exception:
                _LOGGER.exception("Unhandled exception retrieving task result (%s)", label)
                return
            if exc:
                _LOGGER.exception("Background task failed (%s)", label, exc_info=exc)

        task.add_done_callback(_done)

    def _on_notification(
        self,
        entry_id: str,
        payload: Mapping[str, Any],
        persistent_id: str | None,
        context: Any | None,
    ) -> None:
        """Handle incoming FCM notification (sync callback from per-entry client)."""
        # Dispatch to async handler - thread-safe
        self._dispatch_to_hass_loop(
            self._handle_notification_async(entry_id, payload),
            label=f"fcm-notification[{entry_id[:8] if entry_id else 'unknown'}]",
        )

    async def _handle_notification_async(
        self, entry_id: str, payload: Mapping[str, Any]
    ) -> None:
        """Async handler executed in HA loop: parse, route, and schedule downstream work."""
        try:
            hex_string = self._extract_hex_payload(payload)
            if hex_string is None:
                return

            canonic_id = await self._extract_canonic_id_async(hex_string)
            if not canonic_id:
                _LOGGER.debug("FCM response has no canonical id")
                return

            token = self._extract_push_token(dict(payload))
            target_entries, route_src = self._route_target_entries(
                entry_id, canonic_id, token
            )
            target_coordinators = self._coordinators_for_entries(target_entries)

            cb = self.location_update_callbacks.get(canonic_id)
            if cb:
                self._log_push_received(canonic_id, target_entries, route_src, 1)
                await self._run_callback_async(cb, canonic_id, hex_string)
                return

            tracked = [
                c for c in target_coordinators if self._is_tracked(c, canonic_id)
            ]
            self._log_push_received(canonic_id, target_entries, route_src, len(tracked))

            if not tracked:
                _LOGGER.debug(
                    "No registered coordinator will process %s; dropping FCM update",
                    canonic_id[:8],
                )
                return

            await self._process_background_update(
                entry_id, canonic_id, hex_string, target_entries
            )

        except Exception:
            _LOGGER.exception("Failed to handle FCM notification safely")
```

---

### P0 — Unhandled Exceptions from Callback Tasks ("Task exception was never retrieved")

**Location**: Lines 1012-1014, 1036-1040, 1331-1335

**Current Behavior**:
Tasks are started via `asyncio.create_task(...)` without:
1. Awaiting (fire-and-forget)
2. Attaching done callbacks for exception retrieval

`_run_callback_async` (line 1331) may propagate exceptions. Since tasks are not tracked, exceptions surface as "Task exception was never retrieved" warnings, and failures may appear as missing downstream actions (including EID refresh triggers).

**Recommendation**: Either:
1. Catch and log exceptions inside `_run_callback_async`
2. Attach a done-callback to retrieve and log task exceptions (shown in P0 patch above)

**Acceptance Criterion**: A test with an intentionally failing callback must NOT emit unhandled-task warnings; it must generate a structured exception log.

#### Minimal Patch: Safe Callback Runner

```python
async def _run_callback_async(
    self, callback: Callable[[str, str], None], canonic_id: str, hex_string: str
) -> None:
    """Run a user callback safely without unhandled task exceptions."""
    try:
        await _call_in_executor(callback, canonic_id, hex_string)
    except Exception:
        # Do NOT log the full payload. Use length only for safety.
        _LOGGER.exception(
            "FCM locate callback failed (canonic_id=%s, payload_len=%d)",
            canonic_id[:8] if canonic_id else "unknown",
            len(hex_string) if hex_string else 0,
        )
```

---

### P1 — CPU-Bound Parsing in the Event Loop (Protobuf Parsing)

**Locations**:
- `_extract_canonic_id_from_response` (line 1307) - synchronous protobuf parse
- `_decode_background_location_async` (line 1446) - synchronous protobuf parse at line 1451

**Problem**: Both methods parse protobuf synchronously in the event loop. This violates the "blocking work runs in executors" design intent (stated in module docstring line 17) and can:
- Block HA's loop during push bursts
- Cause missed flush windows
- Delay coordinator updates
- Create downstream effects that look like missing EID activity

**Recommendation**: Offload protobuf parsing into `_call_in_executor(...)`. Avoid double-parsing by parsing once and reusing decoded data.

**Acceptance Criterion**: A load test with many notifications should not significantly block the event loop (no HA slow-callback warnings; optional timing assertions).

#### Minimal Patch: Executor Offload for Canonical ID Extraction

```python
async def _extract_canonic_id_async(self, hex_response: str) -> str | None:
    """Extract canonical ID via the decoder in an executor (non-blocking)."""
    return await _call_in_executor(
        self._extract_canonic_id_from_response, hex_response
    )
```

#### Enhanced Patch: Parse-Once Strategy

```python
from dataclasses import dataclass
from typing import Any

@dataclass(slots=True, frozen=True)
class ParsedDeviceUpdate:
    """Parsed protobuf result to avoid double-parsing."""
    canonic_id: str | None
    device_update: Any  # The parsed protobuf object
    raw_hex: str

async def _parse_device_update_async(self, hex_string: str) -> ParsedDeviceUpdate:
    """Parse device update protobuf once in executor."""
    def _parse() -> ParsedDeviceUpdate:
        try:
            device_update = decoder_module.parse_device_update_protobuf(hex_string)
            canonic_id = self._extract_canonic_id_from_parsed(device_update)
            return ParsedDeviceUpdate(
                canonic_id=canonic_id,
                device_update=device_update,
                raw_hex=hex_string,
            )
        except Exception as err:
            _LOGGER.debug("Failed to parse device update: %s", err)
            return ParsedDeviceUpdate(canonic_id=None, device_update=None, raw_hex=hex_string)

    return await _call_in_executor(_parse)

def _extract_canonic_id_from_parsed(self, device_update: Any) -> str | None:
    """Extract canonical ID from already-parsed protobuf."""
    try:
        if device_update.HasField("deviceMetadata"):
            info = device_update.deviceMetadata.identifierInformation
            if info.type == 1:
                ids = info.phoneInformation.canonicIds.canonicId
            else:
                ids = info.canonicIds.canonicId
            if ids:
                return ids[0].id
    except Exception as err:
        _LOGGER.debug("Failed to extract canonical id: %s", err)
    return None
```

---

### P1 — Cache Provider Global State vs ContextVars Race Condition

**Location**: `nova_request.py` lines 282-302, called from `fcm_receiver_ha.py` lines 1460, 1472

**Current Behavior**:
```python
# nova_request.py
_CACHE_PROVIDER: contextvars.ContextVar[...] = contextvars.ContextVar(...)

def register_cache_provider(provider):
    _STATE["cache_provider"] = provider  # GLOBAL dict!
    _CACHE_PROVIDER.set(provider)        # Context-local

def unregister_cache_provider():
    _STATE["cache_provider"] = None      # GLOBAL dict!
    _CACHE_PROVIDER.set(None)            # Context-local
```

**Risk**: The code uses BOTH `contextvars` (async-safe) AND a global `_STATE` dict (NOT async-safe). If two concurrent async operations from different entries call `register_cache_provider` followed by `unregister_cache_provider`:

1. Entry A registers its cache → `_STATE["cache_provider"] = cache_A`
2. Entry B registers its cache → `_STATE["cache_provider"] = cache_B` (overwrites!)
3. Entry A's decrypt uses `resolve_cache_from_provider()` → may get `cache_B` via fallback
4. Cross-contamination: wrong keys, wrong decryption

**Verification**: The `resolve_cache_from_provider` function checks contextvars FIRST (line 308), falling back to `_STATE` only if contextvars returns None. If the code always runs in proper async context, this is mitigated. But the fallback path is still risky.

**Recommendation**: Remove the `_STATE` fallback entirely OR use only explicit `cache=cache` passing (which is already done). The register/unregister pattern adds complexity without benefit if `cache=cache` is passed.

**Acceptance Criterion**: A concurrency test with two parallel entries decrypting simultaneously must deterministically produce correct, entry-specific results.

#### Minimal Patch: Scoped Context Manager (Defensive)

```python
from contextlib import contextmanager
from typing import Iterator, Callable, Any

@contextmanager
def _scoped_cache_provider(
    self, provider: Callable[[], Any]
) -> Iterator[contextvars.Token[Callable[[], Any] | None]]:
    """Context manager for cache provider with guaranteed cleanup."""
    token: contextvars.Token[Callable[[], Any] | None] | None = None
    try:
        # Only use contextvars, skip global state
        from custom_components.googlefindmy.NovaApi.nova_request import _CACHE_PROVIDER
        token = _CACHE_PROVIDER.set(provider)
        yield token
    finally:
        if token is not None:
            _CACHE_PROVIDER.reset(token)

async def _decode_background_location_async(
    self, entry_id: str, hex_string: str
) -> JSONDict:
    """Decode background location using protobuf decoders (CPU-bound)."""
    try:
        device_update = await _call_in_executor(
            decoder_module.parse_device_update_protobuf, hex_string
        )
        cache = self._entry_caches.get(entry_id)
        if cache is None:
            _LOGGER.error(
                "No TokenCache available for entry %s during background decrypt",
                entry_id,
            )
            return {}

        # Use scoped context instead of global register/unregister
        with self._scoped_cache_provider(lambda: cache):
            try:
                raw_locations = await async_decrypt_location_response_locations(
                    device_update, cache=cache
                )
            except StaleOwnerKeyError:
                _LOGGER.info(
                    "Background location update skipped (stale key) for entry %s",
                    entry_id,
                )
                return {}

        # ... rest of method ...
    except Exception as err:
        _LOGGER.error("Failed to decode background location data: %s", err)
        return {}
```

---

## 3) Routing/Debounce Logic: Strong Baseline

### Positive Observations

- **Token→entry routing** with fallback sources ("token", "client", "owner_index", "fallback") is clear and observable (lines 1068-1082)
- **Debounce per `(entry_id, device_id)`** is sound: cancel/reschedule pattern with targeted fan-out (lines 1381-1442)
- **Routing context preservation**: `_pending_targets` stores entry sets alongside payloads (line 1372)

### Improvement Opportunity

"Dropping FCM update" logs (line 1030-1034) are correct but operationally opaque. For field debugging, add structured reasons without revealing secrets:

```python
_LOGGER.debug(
    "Dropping FCM update for %s: reason=%s, route=%s, tracked_count=%d",
    canonic_id[:8],
    "no_tracked_coordinators" if not tracked else "ignored_by_filter",
    route_src,
    len(tracked),
)
```

---

## 4) Security/PII/Secrets Assessment

### Positive
- Raw `hex_string` and FCM tokens are NOT logged in full
- Token logs are truncated to 8 characters (e.g., line 1235: `token[:8]`)
- `canonic_id` is truncated in logs (e.g., line 1103: `canonic_id[:8]`)

### Note
- `api_key` constant exists at line 255. This appears to be the public Firebase project API key (not a secret). Document this explicitly:

```python
# Firebase project configuration for Google Find My Device
# NOTE: This API key is public by design (Firebase Web API key pattern).
# It identifies the project but does not grant privileged access.
self.api_key = "AIzaSyD_gko3P392v6how2H7UpdeXQ0v2HLettc"
```

---

## 5) EID/HIT/MISS Context: What This File Can and Cannot Explain

`fcm_receiver_ha.py`:
- Delivers raw response to locate callback (Locate flow) OR
- Decrypts and sends normalized payloads to coordinators (background push)
- Does NOT implement the EID resolver or report matching itself

**Therefore**: It is plausible to see decryption/refresh logs but NO HIT/MISS near push reception if:
1. EID resolve occurs later in a different pipeline stage (e.g., Bermuda calling `resolve_eid()`)
2. The resolve path was not triggered in that scenario
3. Resolve is logging under a different level/condition

**Operational Conclusion**: If decrypt/refresh is visible but HIT/MISS is not, the most likely explanation is that the EID resolve path was not triggered (or is logging at DEBUG level). This is consistent with the responsibility boundaries of this file.

---

## 6) Concrete, Executable Test Recommendations

### Test 1: Thread-Safety Test

**Objective**: Verify `_on_notification` can be safely called from a worker thread.

```python
import asyncio
import threading
from unittest.mock import MagicMock, patch

def test_on_notification_thread_safety():
    """Call _on_notification from a worker thread; must dispatch safely."""
    receiver = FcmReceiverHA()
    receiver._hass = MagicMock()
    receiver._hass.loop = asyncio.new_event_loop()

    tasks_created = []
    original_create_task = receiver._hass.loop.create_task

    def tracking_create_task(coro, **kwargs):
        task = original_create_task(coro, **kwargs)
        tasks_created.append(task)
        return task

    receiver._hass.loop.create_task = tracking_create_task

    payload = {"data": {"com.google.android.apps.adm.FCM_PAYLOAD": "..."}}

    # Call from worker thread
    thread = threading.Thread(
        target=receiver._on_notification,
        args=("entry123", payload, None, None),
    )
    thread.start()
    thread.join(timeout=2.0)

    # PASS: No exception raised, exactly one task created
    assert len(tasks_created) == 1
    assert not thread.is_alive()
```

### Test 2: Callback Exception Test

**Objective**: Verify callback exceptions are caught and logged, not "never retrieved."

```python
import asyncio
import logging

def test_callback_exception_handling(caplog):
    """Callback throws → no unhandled-task warnings; structured error log."""
    receiver = FcmReceiverHA()

    def failing_callback(canonic_id: str, hex_string: str) -> None:
        raise ValueError("Intentional test failure")

    receiver.location_update_callbacks["device123"] = failing_callback

    with caplog.at_level(logging.ERROR):
        asyncio.run(receiver._run_callback_async(
            failing_callback, "device123", "deadbeef"
        ))

    # PASS: Exception logged with structured format
    assert "FCM locate callback failed" in caplog.text
    assert "device12" in caplog.text  # truncated ID
    # FAIL: "Task exception was never retrieved" in warnings
```

### Test 3: CPU-Load Test

**Objective**: Verify protobuf parsing doesn't block the event loop.

```python
import asyncio
import time

async def test_protobuf_parsing_non_blocking():
    """Many notifications → event loop must not block significantly."""
    receiver = FcmReceiverHA()
    # ... setup ...

    start = time.monotonic()

    # Simulate 100 concurrent notifications
    tasks = [
        receiver._extract_canonic_id_async(hex_payload)
        for _ in range(100)
    ]

    # This should complete quickly if parsing is in executor
    await asyncio.gather(*tasks)

    elapsed = time.monotonic() - start

    # PASS: All complete within reasonable time (< 5s for executor)
    # FAIL: Blocking causes sequential execution (> 30s for sync)
    assert elapsed < 5.0, f"Parsing took {elapsed:.1f}s - likely blocking"
```

### Test 4: Cache Provider Concurrency Test

**Objective**: Verify two parallel entries decrypt with correct, entry-specific caches.

```python
import asyncio
from unittest.mock import MagicMock

async def test_cache_provider_isolation():
    """Two parallel entries decrypt → deterministic, entry-correct results."""
    receiver = FcmReceiverHA()

    cache_a = MagicMock()
    cache_a.entry_id = "entry_a"
    cache_b = MagicMock()
    cache_b.entry_id = "entry_b"

    receiver._entry_caches["entry_a"] = cache_a
    receiver._entry_caches["entry_b"] = cache_b

    used_caches = []

    async def mock_decrypt(device_update, cache):
        used_caches.append(cache.entry_id)
        await asyncio.sleep(0.1)  # Simulate work
        return [{"last_seen": 1234}]

    with patch.object(receiver, "async_decrypt_location_response_locations", mock_decrypt):
        await asyncio.gather(
            receiver._decode_background_location_async("entry_a", "hex_a"),
            receiver._decode_background_location_async("entry_b", "hex_b"),
        )

    # PASS: Each entry used its own cache
    assert used_caches.count("entry_a") == 1
    assert used_caches.count("entry_b") == 1
    # FAIL: Cross-contamination (e.g., both used same cache)
```

---

## 7) Summary of Findings

| ID | Severity | Issue | Status |
|----|----------|-------|--------|
| P0-1 | Critical | Event loop thread safety in `_on_notification` | **To verify**: Check FCM client threading model |
| P0-2 | Critical | Unhandled task exceptions ("never retrieved") | Fix with `_track_task` or try/except in callback |
| P1-1 | High | CPU-bound protobuf parsing blocks event loop | Offload to executor |
| P1-2 | Medium | Cache provider global state race condition | Use scoped context or explicit cache passing only |
| Info | Low | "Dropping FCM update" logs lack structured reason | Add reason field to debug logs |

---

## 8) Verification Checklist

Before implementing patches:

- [ ] **Verify FCM client threading model**: Check if `FcmPushClient` guarantees callbacks run in HA loop
- [ ] **Verify cache provider usage**: Confirm `cache=cache` explicit passing makes register/unregister unnecessary
- [ ] **Verify protobuf parse cost**: Profile `parse_device_update_protobuf` to confirm it's CPU-bound (> 1ms)
- [ ] **Check existing test coverage**: Review if any tests already exercise these paths

---

*End of Peer Review*
