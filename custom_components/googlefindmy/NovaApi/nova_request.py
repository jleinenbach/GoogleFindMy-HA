# custom_components/googlefindmy/NovaApi/nova_request.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
"""
Nova API request helpers (async-first) for Google Find My Device.

This module exposes `async_nova_request(...)`, the non-blocking API consumed by
the Home Assistant integration.

Features
--------
- Optional reuse of the Home Assistant-managed aiohttp ClientSession to avoid
  per-call pools (`register_hass()` / `unregister_hass()`).
- Token TTL learning policy to proactively refresh ADM tokens while preventing
  thundering herds.
- Robust retry logic (401 -> refresh & retry; 5xx/429 -> exponential backoff).
- **Entry-scoped, multi-account ready**: when a `TokenCache` is supplied via
  `async_nova_request(..., cache=...)`, *all* username lookups, token retrieval
  (initial and refresh), and TTL metadata reads/writes are performed strictly
  against that cache. Optionally, a `namespace` (e.g., entry_id) can be used to
  prefix cache keys, preventing collisions when multiple entries share a cache.
"""

from __future__ import annotations

import asyncio
import binascii
import re
import time
import random
import logging
from typing import Awaitable, Callable, Optional, Any
from datetime import datetime, timezone

import aiohttp

from custom_components.googlefindmy.Auth.username_provider import (
    async_get_username,
    username_string,
)
from custom_components.googlefindmy.Auth.adm_token_retrieval import (
    async_get_adm_token as async_get_adm_token_api,
    # Isolated refresh (for config flow validation without global cache)
    async_get_adm_token_isolated,
)
from custom_components.googlefindmy.Auth.token_retrieval import InvalidAasTokenError
from custom_components.googlefindmy.Auth.token_cache import (
    async_set_cached_value,
    TokenCache,  # NEW: for entry-scoped cache access in async path
)
from ..const import DATA_AAS_TOKEN, NOVA_API_USER_AGENT


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


# --- Refresh Locks ---
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
        ns_prefix: str = "",
    ) -> None:
        """
        Args:
            username: Account name this policy applies to.
            logger: Logger to use.
            get_value/set_value: Cache I/O functions (sync).
            refresh_fn: Returns a fresh token (string) or None.
            set_auth_header_fn: Receives the full 'Authorization' header value.
            ns_prefix: Optional key namespace (e.g., config entry_id) to avoid collisions.
        """
        self.username = username
        self.log = logger
        self._get, self._set, self._refresh, self._set_auth = get_value, set_value, refresh_fn, set_auth_header_fn
        self._ns = (ns_prefix or "").strip()
        if self._ns and not self._ns.endswith(":"):
            self._ns += ":"

    # Cache keys for this username (optionally namespaced)
    @property
    def k_issued(self):
        return f"{self._ns}adm_token_issued_at_{self.username}"

    @property
    def k_bestttl(self):
        return f"{self._ns}adm_best_ttl_sec_{self.username}"

    @property
    def k_startleft(self):
        return f"{self._ns}adm_probe_startup_left_{self.username}"

    @property
    def k_probenext(self):
        return f"{self._ns}adm_probe_next_at_{self.username}"

    @property
    def k_armed(self):
        return f"{self._ns}adm_probe_armed_{self.username}"

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
                self._set(
                    self.k_probenext,
                    now + self._jitter_pct(self.PROBE_INTERVAL_SEC, self.probe_interval_jitter_pct),
                )
            elif now >= float(probenext):
                do_arm = True
                self._set(
                    self.k_probenext,
                    now + self._jitter_pct(self.PROBE_INTERVAL_SEC, self.probe_interval_jitter_pct),
                )

        if do_arm:
            self._set(self.k_armed, 1)
            return True
        return bool(self._get(self.k_armed))

    def _do_refresh(self, now: float) -> Optional[str]:
        """Refresh token, update header and issuance timestamp, bootstrap startup probes if missing."""
        try:
            self._set(f"{self._ns}adm_token_{self.username}", None)  # best-effort clear
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
            await self._set(f"{self._ns}adm_token_{self.username}", None)  # best-effort clear
        except Exception:
            pass
        try:
            tok = await self._refresh()  # async callable
        except InvalidAasTokenError as err:
            self.log.error(
                "ADM token refresh failed because the cached AAS token was rejected; re-authentication is required."
            )
            try:
                await self._set(f"{self._ns}{DATA_AAS_TOKEN}", None)
            except Exception:
                pass
            raise NovaAuthError(401, "AAS token invalid during ADM refresh") from err
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


def nova_request(*_: object, **__: object) -> str:
    """Legacy synchronous interface removed in favor of the async implementation."""

    raise RuntimeError(
        "Legacy sync nova_request() has been removed. Use async_nova_request(..., cache=...) instead."
    )


async def async_nova_request(
    api_scope: str,
    hex_payload: str,
    *,
    username: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
    # Optional: initial token for config-flow isolation (bypass cache lookups)
    token: Optional[str] = None,
    # Optional overrides for flow-local validation (avoid global cache conflicts)
    cache_get: Optional[Callable[[str], Awaitable[Any]]] = None,
    cache_set: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    refresh_override: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
    # Optional: entry-specific namespace for cache keys (e.g., entry_id)
    namespace: Optional[str] = None,
    # Entry-scoped TokenCache for strict multi-account separation
    cache: TokenCache,
) -> str:
    """
    Asynchronous Nova API request for Home Assistant (entry-scoped capable).

    This is the preferred method for all communication with the Nova API from
    within Home Assistant, as it is non-blocking and integrates with HA's
    shared aiohttp session.

    Entry-scope & cache behavior:
        - All username lookups, token retrieval, and TTL metadata reads/writes use
          the provided entry-scoped cache exclusively.
        - `namespace` (e.g., config entry_id) is appended as a prefix to cache keys
          to avoid collisions when multiple entries share a backing store.
        - If flow-local `cache_get`/`cache_set` are provided, they override the
          default helpers but are expected to operate on the same entry scope.

    Args:
        api_scope: Nova API scope suffix (appended to the base URL).
        hex_payload: Hex string body.
        username: Optional username. If omitted, resolved from the entry-scoped cache.
        session: Optional aiohttp session to reuse.
        token: Optional, direct ADM token to bypass cache lookups (for config flow).
        cache_get/cache_set: Optional async functions for TTL metadata (flow-local overrides).
        refresh_override: Optional async function returning a fresh token. Use
            this to perform a *real* AAS→ADM refresh isolated from globals
            (e.g. via `async_get_adm_token_isolated(...)`).
        namespace: Optional key namespace (e.g., config entry_id) to avoid cache collisions.
        cache: Entry-scoped TokenCache for strict multi-account separation.

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

    # Use provided credentials if available (for config flow), otherwise fetch from cache.
    if token and username:
        user = username
        initial_token = token
    else:
        if username:
            user = username
        else:
            val = await cache.get(username_string)
            user = str(val) if isinstance(val, str) and val else None
            if not user:
                user = await async_get_username(cache=cache)  # type: ignore[arg-type]
        if not user:
            raise ValueError("Username is not available for async_nova_request.")
        initial_token = await _get_initial_token_async(user, _LOGGER, ns_prefix=(namespace or ""), cache=cache)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Authorization": f"Bearer {initial_token}",
        "Accept-Language": "en-US",
        "User-Agent": NOVA_API_USER_AGENT,
    }
    try:
        payload = binascii.unhexlify(hex_payload)
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"Nova payload too large: {len(payload)} bytes")
    except (binascii.Error, ValueError) as e:
        raise ValueError("Invalid hex payload for Nova request") from e

    # Select cache/refresh providers (entry-scoped TokenCache)
    ns_prefix = (namespace or "").strip()
    if ns_prefix and not ns_prefix.endswith(":"):
        ns_prefix += ":"

    async def _cache_get(key: str) -> Any:
        if cache_get is not None:
            return await cache_get(key)
        return await cache.get(key)

    async def _cache_set(key: str, value: Any) -> None:
        if cache_set is not None:
            await cache_set(key, value)
            return
        await cache.set(key, value)

    if refresh_override is not None:
        rf_fn: Callable[[], Awaitable[Optional[str]]] = refresh_override
    else:
        rf_fn = lambda: async_get_adm_token_api(user, cache=cache)  # noqa: E731

    policy = AsyncTTLPolicy(
        username=user,
        logger=_LOGGER,
        get_value=_cache_get,
        set_value=_cache_set,
        refresh_fn=rf_fn,
        set_auth_header_fn=lambda bearer: headers.update({"Authorization": bearer}),
        ns_prefix=(namespace or ""),
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
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=16, enable_cleanup_closed=True)
            )
            ephemeral_session = True

    try:
        refreshed_once = False
        retries_used = 0
        while True:
            attempt = retries_used + 1
            try:
                timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=30)
                async with session.post(
                    url,
                    headers=headers,
                    data=payload,
                    timeout=timeout,
                    allow_redirects=False,
                ) as response:
                    content = await response.read()
                    status = response.status
                    _LOGGER.debug("Nova API async request to %s: status=%d", api_scope, status)

                    if status == 200:
                        return content.hex()

                    text_snippet = _redact(_beautify_text(content.decode(errors="ignore")))

                    if status == 401:
                        lvl = logging.INFO if not refreshed_once else logging.WARNING
                        _LOGGER.log(lvl, "Nova API async request to %s: 401 Unauthorized. Refreshing token.", api_scope)
                        await policy.on_401()
                        if not refreshed_once:
                            refreshed_once = True
                            continue  # Free retry

                        raise NovaAuthError(status, "Unauthorized after token refresh")

                    if status in (408, 429) or 500 <= status < 600:
                        if retries_used < NOVA_MAX_RETRIES:
                            delay = _compute_delay(attempt, response.headers.get("Retry-After"))
                            _LOGGER.info(
                                "Nova API async request to %s failed with status %d. Retrying in %.2f seconds (attempt %d/%d)...",
                                api_scope,
                                status,
                                delay,
                                retries_used + 1,
                                NOVA_MAX_RETRIES,
                            )
                            retries_used += 1
                            await asyncio.sleep(delay)
                            continue
                        else:
                            _LOGGER.error(
                                "Nova API async request to %s failed after %d attempts with status %d.",
                                api_scope,
                                retries_used + 1,
                                status,
                            )
                            if status == 429:
                                raise NovaRateLimitError(f"Nova API rate limited after {NOVA_MAX_RETRIES} attempts.")
                            raise NovaHTTPError(status, f"Nova API failed after {NOVA_MAX_RETRIES} attempts.")

                    raise NovaAuthError(status, text_snippet)

            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if retries_used < NOVA_MAX_RETRIES:
                    delay = _compute_delay(attempt, None)
                    _LOGGER.info(
                        "Nova API async request to %s failed with %s. Retrying in %.2f seconds (attempt %d/%d)...",
                        api_scope,
                        type(e).__name__,
                        delay,
                        retries_used + 1,
                        NOVA_MAX_RETRIES,
                    )
                    retries_used += 1
                    await asyncio.sleep(delay)
                    continue
                else:
                    _LOGGER.error(
                        "Nova API async request to %s failed after %d attempts with %s.",
                        api_scope,
                        retries_used + 1,
                        type(e).__name__,
                    )
                    raise NovaError(f"Nova API request failed after retries: {e}") from e

    finally:
        if ephemeral_session and session:
            await session.close()
