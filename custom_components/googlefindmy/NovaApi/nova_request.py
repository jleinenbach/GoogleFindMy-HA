# custom_components/googlefindmy/NovaApi/nova_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Nova API request helpers (async-first) for Google Find My Device.

This module exposes:
- `async_nova_request(...)`: the primary, non-blocking API for Home Assistant.
- `nova_request(...)`: a guarded sync facade intended for CLI/testing only. It will
  raise a RuntimeError if called from within a running event loop.

Features
--------
- Optional reuse of the Home Assistant-managed aiohttp ClientSession to avoid
  per-call pools (`register_hass()` / `unregister_hass()`).
- Token TTL learning policy (sync/async variants) to proactively refresh ADM tokens.
- Robust retry logic (401 -> refresh & retry; 5xx/429 -> exponential backoff).
- All auth/cache access in the async path uses the async token cache API.

Notes
-----
- The sync path uses `httpx` and MUST NOT be called from within the event loop.
- The async path is preferred throughout the integration code.
"""

from __future__ import annotations

import asyncio
import binascii
import re
import time
import random
import logging
import threading
from typing import Callable, Optional
from datetime import datetime, timezone

import aiohttp
import httpx

from custom_components.googlefindmy.Auth.username_provider import get_username, async_get_username
from custom_components.googlefindmy.Auth.adm_token_retrieval import get_adm_token, async_get_adm_token as async_get_adm_token_api
from custom_components.googlefindmy.Auth.token_cache import (
    get_cached_value,
    set_cached_value,
    async_get_cached_value,
    async_set_cached_value,
)
from ..const import NOVA_API_USER_AGENT


_LOGGER = logging.getLogger(__name__)

# --- Retry constants ---
NOVA_MAX_RETRIES = 3
NOVA_INITIAL_BACKOFF_S = 1.0
NOVA_BACKOFF_FACTOR = 2.0
NOVA_MAX_RETRY_AFTER_S = 60.0
MAX_PAYLOAD_BYTES = 512 * 1024  # 512 KiB

# --- PII Redaction ---
_RE_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9\-\._~\+\/]+=*", re.I)
_RE_EMAIL = re.compile(r"([A-Za-z0-9._%+-])([A-Za-z0-9._%+-]*)(@[^,\s]+)")
_RE_HEX16 = re.compile(r"\b[0-9a-fA-F]{16,}\b")

def _redact(s: str) -> str:
    """Redact sensitive information from a string for safe logging."""
    s = _RE_BEARER.sub("Bearer <redacted>", s)
    s = _RE_EMAIL.sub(r"\1***\3", s)
    s = _RE_HEX16.sub("<hex-redacted>", s)
    return s

# --- Custom Exceptions ---
class NovaError(Exception):
    """Base exception for Nova API errors."""

class NovaAuthError(NovaError):
    """Raised on 4xx client errors after retries."""
    def __init__(self, status: int, detail: Optional[str] = None):
        super().__init__(f"HTTP Client Error {status}: {detail or ''}".strip())
        self.status = status
        self.detail = detail

class NovaRateLimitError(NovaError):
    """Raised on 429 rate-limiting errors after retries."""
    def __init__(self, detail: Optional[str] = None):
        super().__init__(f"Rate limited by upstream API: {detail or ''}".strip())
        self.detail = detail

class NovaHTTPError(NovaError):
    """Raised for 5xx server errors after retries."""
    def __init__(self, status: int, detail: Optional[str] = None):
        super().__init__(f"HTTP Server Error {status}: {detail or ''}".strip())
        self.status = status
        self.detail = detail

# ------------------------ Optional Home Assistant hooks ------------------------
# These hooks allow the integration to supply a shared aiohttp ClientSession.
_HASS_REF = None

def register_hass(hass) -> None:
    """Register a Home Assistant instance to provide a shared ClientSession."""
    global _HASS_REF
    _HASS_REF = hass

def unregister_hass() -> None:
    """Unregister the Home Assistant instance reference."""
    global _HASS_REF
    _HASS_REF = None

# Cache provider hook for multi-entry support
_CACHE_PROVIDER: Callable[[], any] | None = None

def register_cache_provider(provider: Callable[[], any]) -> None:
    """Register a callable that returns the entry-specific cache.

    Args:
        provider: Callable that returns a TokenCache instance.

    This allows multi-entry scenarios where each API instance uses its own cache
    instead of relying on the global cache facade.
    """
    global _CACHE_PROVIDER
    _CACHE_PROVIDER = provider

def unregister_cache_provider() -> None:
    """Unregister the cache provider."""
    global _CACHE_PROVIDER
    _CACHE_PROVIDER = None

def _get_cache_provider():
    """Get the registered cache provider or None.

    Returns None if no provider is registered or if the provider's cache is closed.
    """
    if not _CACHE_PROVIDER:
        return None
    try:
        cache = _CACHE_PROVIDER()
        # Check if cache is closed (has _closed attribute that's True)
        if cache and getattr(cache, '_closed', False):
            return None
        return cache
    except Exception:  # noqa: BLE001
        return None

# --- Refresh Locks ---
_sync_refresh_lock = threading.Lock()
_async_refresh_lock: asyncio.Lock | None = None

def _get_async_refresh_lock() -> asyncio.Lock:
    """Lazily initialize and return the async refresh lock."""
    global _async_refresh_lock
    if _async_refresh_lock is None:
        _async_refresh_lock = asyncio.Lock()
    return _async_refresh_lock

# ------------------------ TTL policy (shared core) ------------------------
class TTLPolicy:
    """Token TTL/probe policy (synchronous I/O).

    Encapsulates probe arming, jitter and proactive refresh. The policy records
    the best-known TTL and times token issuance, allowing pre-emptive refreshes.
    """
    TTL_MARGIN_SEC, JITTER_SEC = 120, 90
    PROBE_INTERVAL_SEC, PROBE_INTERVAL_JITTER_PCT = 6 * 3600, 0.1

    def __init__(
        self,
        username: str,
        logger,
        get_value: Callable[[str], Optional[float | int | str]],
        set_value: Callable[[str, object | None], None],
        refresh_fn: Callable[[], Optional[str]],
        set_auth_header_fn: Callable[[str], None],
    ) -> None:
        """
        Args:
            username: Account name this policy applies to.
            logger: Logger to use.
            get_value/set_value: Cache I/O functions (sync).
            refresh_fn: Returns a fresh token (string) or None.
            set_auth_header_fn: Receives the full 'Authorization' header value.
        """
        self.username = username
        self.log = logger
        self._get, self._set, self._refresh, self._set_auth = get_value, set_value, refresh_fn, set_auth_header_fn
    
    # Cache keys for this username
    @property
    def k_issued(self):    return f"adm_token_issued_at_{self.username}"
    @property
    def k_bestttl(self):   return f"adm_best_ttl_sec_{self.username}"
    @property
    def k_startleft(self): return f"adm_probe_startup_left_{self.username}"
    @property
    def k_probenext(self): return f"adm_probe_next_at_{self.username}"
    @property
    def k_armed(self):     return f"adm_probe_armed_{self.username}"

    @staticmethod
    def _jitter_sec(base: float, jitter_abs: float) -> float:
        """Apply symmetric ±jitter_abs (seconds); never return negative."""
        try:
            return max(0.0, base + random.uniform(-jitter_abs, +jitter_abs))
        except Exception:
            return max(0.0, base)

    @staticmethod
    def _jitter_pct(base: float, pct: float) -> float:
        """Apply symmetric ±pct jitter; never return negative."""
        try:
            return max(0.0, base + random.uniform(-pct * base, +pct * base))
        except Exception:
            return max(0.0, base)

    @property
    def probe_interval_jitter_pct(self) -> float:
        """Expose jitter percentage for subclasses without duplication."""
        return self.PROBE_INTERVAL_JITTER_PCT

    def _arm_probe_if_due(self, now: float) -> bool:
        """Arm a probe if startup probes remain or the (jittered) schedule is due."""
        startup_left = self._get(self.k_startleft)
        probenext = self._get(self.k_probenext)

        do_arm = False
        if startup_left and int(startup_left) > 0:
            do_arm = True
        else:
            if probenext is None:
                self._set(self.k_probenext, now + self._jitter_pct(self.PROBE_INTERVAL_SEC, self.probe_interval_jitter_pct))
            elif now >= float(probenext):
                do_arm = True
                self._set(self.k_probenext, now + self._jitter_pct(self.PROBE_INTERVAL_SEC, self.probe_interval_jitter_pct))

        if do_arm:
            self._set(self.k_armed, 1)
            return True
        return bool(self._get(self.k_armed))

    def _do_refresh(self, now: float) -> Optional[str]:
        """Refresh token, update header and issuance timestamp, bootstrap startup probes if missing."""
        try:
            self._set(f"adm_token_{self.username}", None)  # best-effort clear
        except Exception:
            pass
        tok = self._refresh()
        if tok:
            self._set_auth("Bearer " + tok)
            self._set(self.k_issued, now)
            if not self._get(self.k_startleft):
                self._set(self.k_startleft, 3)  # bootstrap startup probes if missing
        return tok

    def pre_request(self) -> None:
        """Arm probes and (if not armed) proactively refresh near the measured TTL."""
        now = time.time()
        issued_at = self._get(self.k_issued)
        best_ttl = self._get(self.k_bestttl)

        # Arm probe if needed; returns True if probe is armed.
        armed = self._arm_probe_if_due(now)

        # Proactive refresh only if NOT armed and we have a measured TTL
        if (not armed) and issued_at and best_ttl:
            try:
                age = now - float(issued_at)
                threshold = max(0.0, float(best_ttl) - self.TTL_MARGIN_SEC)
                threshold = self._jitter_sec(threshold, self.JITTER_SEC)
                if age >= threshold:
                    self.log.info("ADM token reached measured threshold – proactively refreshing...")
                    self._do_refresh(now)
            except (ValueError, TypeError) as e:
                self.log.debug("Threshold check failed: %s", e)

    def on_401(self, adaptive_downshift: bool = True) -> Optional[str]:
        """Handle 401: measure TTL, adapt quickly on unexpected short TTLs, then refresh."""
        now = time.time()
        issued = self._get(self.k_issued)
        if issued is None:
            self.log.warning("Got 401 – issued timestamp missing; attempting token refresh.")
            return self._do_refresh(now)

        age_sec = now - float(issued)
        age_min = age_sec / 60.0
        planned_probe = bool(self._get(self.k_armed))

        if planned_probe:
            self.log.info("Got 401 (forced probe) – measured TTL: %.1f min.", age_min)
            self._set(self.k_bestttl, age_sec)  # always accept probe (up or down)
            self._set(self.k_armed, 0)          # coalesce multiple 401 in same probe window
            left = self._get(self.k_startleft)
            if left and int(left) > 0:
                try:
                    self._set(self.k_startleft, int(left) - 1)
                except (ValueError, TypeError):
                    self._set(self.k_startleft, 0)
        else:
            self.log.warning("Got 401 – token expired after %.1f min (unplanned).", age_min)
            if adaptive_downshift:
                best = self._get(self.k_bestttl)
                try:
                    # If clearly shorter than our current model (>10% shorter), recalibrate immediately.
                    if best and (age_sec + self.TTL_MARGIN_SEC) < 0.9 * float(best):
                        self.log.warning("Unexpected short TTL – recalibrating best known TTL.")
                        self._set(self.k_bestttl, age_sec)
                except (ValueError, TypeError) as e:
                    self.log.debug("Recalibration check failed: %s", e)

        # Always refresh after a 401 to resume normal operation.
        return self._do_refresh(now)


class AsyncTTLPolicy(TTLPolicy):
    """Native async version of the TTL policy (no blocking calls)."""

    async def _arm_probe_if_due_async(self, now: float) -> bool:
        startup_left = await self._get(self.k_startleft)
        probenext = await self._get(self.k_probenext)
        do_arm = bool(startup_left and int(startup_left) > 0)
        if not do_arm:
            if probenext is None:
                await self._set(self.k_probenext, now + self._jitter_pct(self.PROBE_INTERVAL_SEC, self.PROBE_INTERVAL_JITTER_PCT))
            elif now >= float(probenext):
                do_arm = True
                await self._set(self.k_probenext, now + self._jitter_pct(self.PROBE_INTERVAL_SEC, self.PROBE_INTERVAL_JITTER_PCT))
        if do_arm:
            await self._set(self.k_armed, 1)
            return True
        return bool(await self._get(self.k_armed))

    async def _do_refresh_async(self, now: float) -> Optional[str]:
        try:
            await self._set(f"adm_token_{self.username}", None)  # best-effort clear
        except Exception:
            pass
        tok = await self._refresh()  # async callable
        if tok:
            self._set_auth("Bearer " + tok)
            await self._set(self.k_issued, now)
            if not await self._get(self.k_startleft):
                await self._set(self.k_startleft, 3)
        return tok

    async def pre_request(self) -> None:
        now = time.time()
        issued_at = await self._get(self.k_issued)
        best_ttl = await self._get(self.k_bestttl)
        armed = await self._arm_probe_if_due_async(now)
        if (not armed) and issued_at and best_ttl:
            try:
                age = now - float(issued_at)
                threshold = max(0.0, float(best_ttl) - self.TTL_MARGIN_SEC)
                threshold = self._jitter_sec(threshold, self.JITTER_SEC)
                if age >= threshold:
                    self.log.info("ADM token reached measured threshold – proactively refreshing (async)…")
                    await self._do_refresh_async(now)
            except (ValueError, TypeError) as e:
                self.log.debug("Threshold check failed (async): %s", e)

    async def on_401(self, adaptive_downshift: bool = True) -> Optional[str]:
        """Async 401 handling with stampede guard and async cache I/O."""
        lock = _get_async_refresh_lock()
        async with lock:
            # Skip duplicate refresh if someone just refreshed recently.
            current_issued = await self._get(self.k_issued)
            if current_issued and time.time() - float(current_issued) < 2:
                self.log.debug("Another task already refreshed the token; skipping duplicate refresh.")
                return None

            now = time.time()
            issued = await self._get(self.k_issued)
            if issued is None:
                self.log.info("Got 401 – issued timestamp missing; attempting token refresh (async).")
                return await self._do_refresh_async(now)

            age_sec = now - float(issued)
            age_min = age_sec / 60.0
            planned_probe = bool(await self._get(self.k_armed))

            if planned_probe:
                self.log.info("Got 401 (forced probe) – measured TTL: %.1f min.", age_min)
                await self._set(self.k_bestttl, age_sec)
                await self._set(self.k_armed, 0)
                left = await self._get(self.k_startleft)
                if left and int(left) > 0:
                    try:
                        await self._set(self.k_startleft, int(left) - 1)
                    except (ValueError, TypeError):
                        await self._set(self.k_startleft, 0)
            else:
                self.log.info("Got 401 – token expired after %.1f min (unplanned).", age_min)
                if adaptive_downshift:
                    best = await self._get(self.k_bestttl)
                    try:
                        if best and (age_sec + self.TTL_MARGIN_SEC) < 0.9 * float(best):
                            self.log.warning("Unexpected short TTL – recalibrating best known TTL (async).")
                            await self._set(self.k_bestttl, age_sec)
                    except (ValueError, TypeError) as e:
                        self.log.debug("Recalibration check failed (async): %s", e)
            return await self._do_refresh_async(now)


def _get_initial_token_sync(username: str, _logger) -> str:
    """Get or create the initial ADM token in sync path and ensure TTL metadata is recorded."""
    # Safe cache access - handle multi-entry scenarios during validation
    token = None
    try:
        token = get_cached_value(f"adm_token_{username}") or get_cached_value("adm_token")
    except Exception:  # noqa: BLE001
        # Cache not available during validation
        pass

    if not token:
        _logger.info("Attempting to generate new ADM token...")
        token = get_adm_token(username)
        if token:
            try:
                set_cached_value(f"adm_token_issued_at_{username}", time.time())
                probe_left = get_cached_value(f"adm_probe_startup_left_{username}")
                if not probe_left:
                    set_cached_value(f"adm_probe_startup_left_{username}", 3)
            except Exception:  # noqa: BLE001
                # Cache not available - skip TTL metadata
                pass
    if not token: raise ValueError("No ADM token available - please reconfigure authentication")
    return token


async def _get_initial_token_async(username: str, _logger) -> str:
    """Get or create the initial ADM token in async path and ensure TTL metadata is recorded."""
    # Try to use entry-specific cache if registered (multi-entry support)
    cache = _get_cache_provider()

    # Safe cache access - handle multi-entry scenarios during validation
    token = None
    try:
        if cache:
            # Use entry-specific cache
            token = await cache.get(f"adm_token_{username}") or await cache.get("adm_token")
        else:
            # Fall back to global facade
            token = await async_get_cached_value(f"adm_token_{username}") or await async_get_cached_value("adm_token")
    except Exception:  # noqa: BLE001
        # Cache not available during validation
        pass

    if not token:
        _logger.info("Attempting to generate new ADM token (async)...")
        token = await async_get_adm_token_api(username)
        if token:
            try:
                if cache:
                    await cache.set(f"adm_token_issued_at_{username}", time.time())
                    probe_left = await cache.get(f"adm_probe_startup_left_{username}")
                    if not probe_left:
                        await cache.set(f"adm_probe_startup_left_{username}", 3)
                else:
                    await async_set_cached_value(f"adm_token_issued_at_{username}", time.time())
                    probe_left = await async_get_cached_value(f"adm_probe_startup_left_{username}")
                    if not probe_left:
                        await async_set_cached_value(f"adm_probe_startup_left_{username}", 3)
            except Exception:  # noqa: BLE001
                # Cache not available - skip TTL metadata
                pass
    if not token: raise ValueError("No ADM token available - please reconfigure authentication")
    return token

def _beautify_text(resp_text: str) -> str:
    """
    Best-effort pretty-printing of an error response body.
    Optional dependency: BeautifulSoup4.
    """
    try:
        # local import only when needed
        from bs4 import BeautifulSoup 
        return BeautifulSoup(resp_text, "html.parser").get_text(strip=True)
    except Exception:
        return resp_text[:512]

def _compute_delay(attempt: int, retry_after: Optional[str]) -> float:
    """
    Computes retry delay, respecting Retry-After (RFC 9110) or using exponential backoff with full jitter.
    See: AWS Architecture Blog - Exponential Backoff And Jitter
    """
    delay = None
    if retry_after:
        try:
            # seconds case
            delay = float(retry_after)
        except ValueError:
            # HTTP-date case (RFC 7231 §7.1.3)
            from email.utils import parsedate_to_datetime
            try:
                retry_dt = parsedate_to_datetime(retry_after)
                delay = max(0.0, (retry_dt - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                pass # Fallback to backoff if date parsing fails
    
    if delay is None:
        # Exponential backoff with full jitter
        backoff = (NOVA_BACKOFF_FACTOR ** (attempt - 1)) * NOVA_INITIAL_BACKOFF_S
        delay = random.uniform(0.0, backoff)
    
    capped_delay = min(delay, NOVA_MAX_RETRY_AFTER_S)
    if delay is not None and delay > capped_delay:
        _LOGGER.info("Retry-After delay of %.2fs capped to %.2fs by client policy.", delay, capped_delay)
    return capped_delay

def nova_request(api_scope: str, hex_payload: str) -> str:
    """
    Synchronous Nova API request (CLI/testing only).

    IMPORTANT:
    This function is blocking and MUST NOT be called from within the Home Assistant
    event loop. It will raise a RuntimeError if such usage is detected.
    Use `async_nova_request` for all Home Assistant code.
    
    Args:
        api_scope: Nova API scope suffix (appended to the base URL).
        hex_payload: Hex string body.

    Returns:
        Hex-encoded response body.
    Raises:
        RuntimeError: if called from a running event loop.
        ValueError: if the hex_payload is invalid.
        NovaError (and subclasses): for API and network errors.
    """
    # Guard against misuse in the running event loop.
    try:
        if asyncio.get_running_loop().is_running():
            raise RuntimeError("Sync nova_request() must not be called from the event loop.")
    except RuntimeError:
        pass

    url = f"https://android.googleapis.com/nova/{api_scope}"

    # Safe username retrieval - handle cache errors during multi-entry validation
    try:
        username = get_username()
    except Exception:  # noqa: BLE001
        # Cache not available during validation or multi-entry scenario
        raise ValueError("Username is not available for nova_request.")

    token = _get_initial_token_sync(username, _LOGGER)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Authorization": f"Bearer {token}", "Accept-Language": "en-US",
        "User-Agent": NOVA_API_USER_AGENT,
    }
    try:
        payload = binascii.unhexlify(hex_payload)
        if len(payload) > MAX_PAYLOAD_BYTES: raise ValueError(f"Nova payload too large: {len(payload)} bytes")
    except (binascii.Error, ValueError) as e:
        raise ValueError("Invalid hex payload for Nova request") from e

    # Wrap cache functions to handle multi-entry scenarios gracefully during config flow validation
    def safe_get_cached_value(key: str):
        try:
            return get_cached_value(key)
        except Exception:  # noqa: BLE001
            # Cache unavailable (multi-entry validation) - return None to skip TTL logic
            return None

    def safe_set_cached_value(key: str, value):
        try:
            set_cached_value(key, value)
        except Exception:  # noqa: BLE001
            # Cache unavailable (multi-entry validation) - skip caching
            pass

    policy = TTLPolicy(
        username=username, logger=_LOGGER,
        get_value=safe_get_cached_value, set_value=safe_set_cached_value,
        refresh_fn=lambda: get_adm_token(username),
        set_auth_header_fn=lambda bearer: headers.update({"Authorization": bearer}),
    )
    policy.pre_request()

    refreshed_once = False
    retries_used = 0
    while True:
        attempt = retries_used + 1
        try:
            timeout = httpx.Timeout(30.0, connect=10.0, read=30.0, write=30.0, pool=30.0)
            response = httpx.post(url, headers=headers, content=payload, timeout=timeout, follow_redirects=False)
            _LOGGER.debug("Nova API sync request to %s: status=%d", api_scope, response.status_code)

            if response.status_code == 200:
                return response.content.hex()
            
            text_snippet = _redact(_beautify_text(response.text))

            if response.status_code == 401:
                lvl = logging.INFO if not refreshed_once else logging.WARNING
                _LOGGER.log(lvl, "Nova API sync request to %s: 401 Unauthorized. Refreshing token.", api_scope)
                with _sync_refresh_lock:
                    current_issued = get_cached_value(policy.k_issued)
                    if current_issued and time.time() - float(current_issued) < 2:
                        _LOGGER.debug("Another task already refreshed the token; re-evaluating.")
                    else:
                        policy.on_401()

                if not refreshed_once:
                    refreshed_once = True
                    continue # Free retry, do not increment retries_used

                raise NovaAuthError(response.status_code, "Unauthorized after token refresh")

            if response.status_code in (408, 429, 500, 502, 503, 504):
                if retries_used < NOVA_MAX_RETRIES:
                    delay = _compute_delay(attempt, response.headers.get("Retry-After"))
                    _LOGGER.info("Nova API sync request to %s failed with status %d. Retrying in %.2f seconds (attempt %d/%d)...",
                                 api_scope, response.status_code, delay, retries_used + 1, NOVA_MAX_RETRIES)
                    retries_used += 1
                    time.sleep(delay)
                    continue
                else:
                    _LOGGER.error("Nova API sync request to %s failed after %d attempts with status %d.", api_scope, retries_used + 1, response.status_code)
                    if response.status_code == 429:
                        raise NovaRateLimitError(f"Nova API rate limited after {NOVA_MAX_RETRIES} attempts.")
                    raise NovaHTTPError(response.status_code, f"Nova API failed after {NOVA_MAX_RETRIES} attempts.")
            
            raise NovaAuthError(response.status_code, text_snippet)

        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if retries_used < NOVA_MAX_RETRIES:
                delay = _compute_delay(attempt, None)
                _LOGGER.info("Nova API sync request to %s failed with %s. Retrying in %.2f seconds (attempt %d/%d)...",
                             api_scope, type(e).__name__, delay, retries_used + 1, NOVA_MAX_RETRIES)
                retries_used += 1
                time.sleep(delay)
                continue
            else:
                 _LOGGER.error("Nova API sync request to %s failed after %d attempts with %s.", api_scope, retries_used + 1, type(e).__name__)
                 raise NovaError(f"Nova API request failed after retries: {e}") from e

async def async_nova_request(
    api_scope: str,
    hex_payload: str,
    username: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
) -> str:
    """
    Asynchronous Nova API request for Home Assistant.

    This is the preferred method for all communication with the Nova API from
    within Home Assistant, as it is non-blocking and integrates with HA's
    shared aiohttp session.
    
    Args:
        api_scope: Nova API scope suffix (appended to the base URL).
        hex_payload: Hex string body.
        username: Optional username. If omitted, read from async cache.
        session: Optional aiohttp session to reuse.

    Returns:
        Hex-encoded response body.
    Raises:
        ValueError: if the hex_payload is invalid or username is unavailable.
        NovaAuthError: on 4xx client errors.
        NovaRateLimitError: on 429 errors after all retries.
        NovaHTTPError: on 5xx server errors after all retries.
        NovaError: on other unrecoverable errors like network issues after retries.
    """
    url = f"https://android.googleapis.com/nova/{api_scope}"

    # Safe username retrieval - handle cache errors during multi-entry validation
    if username:
        user = username
    else:
        try:
            user = await async_get_username()
        except Exception:  # noqa: BLE001
            # Cache not available during validation or multi-entry scenario
            user = None

    if not user:
        raise ValueError("Username is not available for async_nova_request.")

    token = await _get_initial_token_async(user, _LOGGER)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Authorization": f"Bearer {token}",
        "Accept-Language": "en-US",
        "User-Agent": NOVA_API_USER_AGENT,
    }
    try:
        payload = binascii.unhexlify(hex_payload)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"Nova payload too large: {len(payload)} bytes")
    except (binascii.Error, ValueError) as e:
        raise ValueError("Invalid hex payload for Nova request") from e

    # Wrap cache functions to handle multi-entry scenarios gracefully during config flow validation
    async def safe_get_cached_value(key: str):
        try:
            return await async_get_cached_value(key)
        except Exception:  # noqa: BLE001
            # Cache unavailable (multi-entry validation) - return None to skip TTL logic
            return None

    async def safe_set_cached_value(key: str, value):
        try:
            await async_set_cached_value(key, value)
        except Exception:  # noqa: BLE001
            # Cache unavailable (multi-entry validation) - skip caching
            pass

    policy = AsyncTTLPolicy(
        username=user, logger=_LOGGER,
        get_value=safe_get_cached_value, set_value=safe_set_cached_value,
        refresh_fn=lambda: async_get_adm_token_api(user),
        set_auth_header_fn=lambda bearer: headers.update({"Authorization": bearer}),
    )
    await policy.pre_request()

    ephemeral_session = False
    if session is None:
        if _HASS_REF:
            # Use the HA-managed shared session for best performance and resource management.
            from homeassistant.helpers.aiohttp_client import async_get_clientsession
            session = async_get_clientsession(_HASS_REF)
        else:
            # Fallback for environments without a shared session (e.g., standalone scripts).
            session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=16, enable_cleanup_closed=True))
            ephemeral_session = True

    try:
        refreshed_once = False
        retries_used = 0
        while True:
            attempt = retries_used + 1
            try:
                timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)
                async with session.post(url, headers=headers, data=payload, timeout=timeout, allow_redirects=False) as response:
                    content = await response.read()
                    status = response.status
                    _LOGGER.debug("Nova API async request to %s: status=%d", api_scope, status)

                    if status == 200:
                        return content.hex()
                    
                    text_snippet = _redact(_beautify_text(content.decode(errors='ignore')))
                    
                    if status == 401:
                        lvl = logging.INFO if not refreshed_once else logging.WARNING
                        _LOGGER.log(lvl, "Nova API async request to %s: 401 Unauthorized. Refreshing token.", api_scope)
                        await policy.on_401()
                        if not refreshed_once:
                            refreshed_once = True
                            continue # Free retry

                        raise NovaAuthError(status, "Unauthorized after token refresh")

                    if status in (408, 429) or 500 <= status < 600:
                        if retries_used < NOVA_MAX_RETRIES:
                            delay = _compute_delay(attempt, response.headers.get("Retry-After"))
                            _LOGGER.info(
                                "Nova API async request to %s failed with status %d. Retrying in %.2f seconds (attempt %d/%d)...",
                                api_scope, status, delay, retries_used + 1, NOVA_MAX_RETRIES
                            )
                            retries_used += 1
                            await asyncio.sleep(delay)
                            continue
                        else:
                            _LOGGER.error("Nova API async request to %s failed after %d attempts with status %d.", api_scope, retries_used + 1, status)
                            if status == 429:
                                raise NovaRateLimitError(f"Nova API rate limited after {NOVA_MAX_RETRIES} attempts.")
                            raise NovaHTTPError(status, f"Nova API failed after {NOVA_MAX_RETRIES} attempts.")
                    
                    raise NovaAuthError(status, text_snippet)

            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if retries_used < NOVA_MAX_RETRIES:
                    delay = _compute_delay(attempt, None)
                    _LOGGER.info("Nova API async request to %s failed with %s. Retrying in %.2f seconds (attempt %d/%d)...",
                                 api_scope, type(e).__name__, delay, retries_used + 1, NOVA_MAX_RETRIES)
                    retries_used += 1
                    await asyncio.sleep(delay)
                    continue
                else:
                    _LOGGER.error("Nova API async request to %s failed after %d attempts with %s.", api_scope, retries_used + 1, type(e).__name__)
                    raise NovaError(f"Nova API request failed after retries: {e}") from e

    finally:
        if ephemeral_session and session:
            await session.close()

