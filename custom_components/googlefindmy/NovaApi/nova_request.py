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
import contextvars
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

import aiohttp

from custom_components.googlefindmy.Auth.adm_token_retrieval import (
    async_get_adm_token as async_get_adm_token_api,
)
from custom_components.googlefindmy.Auth.token_cache import (
    TokenCache,  # NEW: for entry-scoped cache access in async path
)
from custom_components.googlefindmy.Auth.token_retrieval import InvalidAasTokenError
from custom_components.googlefindmy.Auth.username_provider import (
    async_get_username,
    username_string,
)

from ..const import DATA_AAS_TOKEN, NOVA_API_USER_AGENT

if TYPE_CHECKING:
    from bs4 import BeautifulSoup as _BeautifulSoupType
    from homeassistant.core import HomeAssistant
else:
    _BeautifulSoupType = Any
    HomeAssistant = Any

_beautiful_soup_factory: Callable[[str, str], _BeautifulSoupType] | None
try:
    from bs4 import BeautifulSoup as _bs_factory
except (
    ImportError
):  # pragma: no cover - optional dependency, covered via fallback branch
    _beautiful_soup_factory = None
    _BS4_AVAILABLE = False
else:
    _beautiful_soup_factory = cast(
        "Callable[[str, str], _BeautifulSoupType]", _bs_factory
    )
    _BS4_AVAILABLE = True

_LOGGER = logging.getLogger(__name__)

if not _BS4_AVAILABLE:
    _LOGGER.debug(
        "BeautifulSoup4 not installed, error response beautification disabled."
    )

# --- Retry constants ---
NOVA_MAX_RETRIES = 6
NOVA_INITIAL_BACKOFF_S = 4.0
NOVA_BACKOFF_FACTOR = 2.0
NOVA_MAX_RETRY_AFTER_S = 60.0

# ---------------------------------------------------------------------------
# HTTP Status Codes (RFC 9110)
# ---------------------------------------------------------------------------
# Reference: https://httpwg.org/specs/rfc9110.html#status.codes
#
# Retry behavior follows industry best practices:
# - https://www.baeldung.com/cs/http-error-status-codes-retry
# - https://www.restapitutorial.com/advanced/responses/retries
# ---------------------------------------------------------------------------

# 2xx Success
HTTP_OK = 200

# 4xx Client Errors
HTTP_BAD_REQUEST = 400  # Malformed request syntax - DO NOT RETRY
HTTP_UNAUTHORIZED = 401  # Auth failed - RETRY after token refresh
HTTP_FORBIDDEN = 403  # Permission denied - DO NOT RETRY
HTTP_NOT_FOUND = 404  # Resource not found - DO NOT RETRY
HTTP_METHOD_NOT_ALLOWED = 405  # HTTP method not supported - DO NOT RETRY
HTTP_REQUEST_TIMEOUT = 408  # Server timeout waiting for request - RETRY OK
HTTP_CONFLICT = 409  # Request conflicts with server state - DO NOT RETRY
HTTP_GONE = 410  # Resource permanently removed - DO NOT RETRY
HTTP_PRECONDITION_FAILED = 412  # Precondition in headers not met - DO NOT RETRY
HTTP_PAYLOAD_TOO_LARGE = 413  # Request body too large - DO NOT RETRY
HTTP_UNPROCESSABLE_ENTITY = 422  # Semantic errors in request - DO NOT RETRY
HTTP_TOO_MANY_REQUESTS = 429  # Rate limited - RETRY with backoff

# 5xx Server Errors
HTTP_INTERNAL_SERVER_ERROR = 500  # Generic server error - RETRY OK
HTTP_NOT_IMPLEMENTED = 501  # Server doesn't support functionality - DO NOT RETRY
HTTP_BAD_GATEWAY = 502  # Invalid response from upstream - RETRY OK
HTTP_SERVICE_UNAVAILABLE = (
    503  # Server overloaded/maintenance - RETRY OK (check Retry-After)
)
HTTP_GATEWAY_TIMEOUT = 504  # Upstream timeout - RETRY OK
HTTP_VERSION_NOT_SUPPORTED = 505  # HTTP version not supported - DO NOT RETRY
HTTP_LOOP_DETECTED = 508  # Infinite loop in request processing - DO NOT RETRY

# ---------------------------------------------------------------------------
# Retry-eligible status codes for Nova API
# ---------------------------------------------------------------------------
# These codes indicate transient failures that may succeed on retry:
# - 408: Request Timeout (network issues)
# - 429: Too Many Requests (rate limiting)
# - 500: Internal Server Error (transient server issues)
# - 502: Bad Gateway (upstream issues)
# - 503: Service Unavailable (overload/maintenance)
# - 504: Gateway Timeout (upstream timeout)
#
# These codes should NOT be retried (permanent errors):
# - 400: Bad Request (malformed request)
# - 403: Forbidden (no permission)
# - 404: Not Found (resource doesn't exist)
# - 501: Not Implemented (server doesn't support this)
# - 505: HTTP Version Not Supported (config issue)
# - 508: Loop Detected (server config issue)
# ---------------------------------------------------------------------------
HTTP_RETRY_ELIGIBLE = frozenset(
    {
        HTTP_REQUEST_TIMEOUT,
        HTTP_TOO_MANY_REQUESTS,
        HTTP_INTERNAL_SERVER_ERROR,
        HTTP_BAD_GATEWAY,
        HTTP_SERVICE_UNAVAILABLE,
        HTTP_GATEWAY_TIMEOUT,
    }
)

RECENT_REFRESH_WINDOW_S = 2.0

MAX_PAYLOAD_BYTES = 512 * 1024  # 512 KiB

# --- Retry helpers ---


def _compute_delay(attempt: int, retry_after: str | None) -> float:
    """Return a retry delay honoring Retry-After headers with jittered exponential backoff fallback."""

    delay: float | None = None
    if retry_after:
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            try:
                retry_dt = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                retry_dt = None
            if retry_dt is not None:
                if retry_dt.tzinfo is None:
                    retry_dt = retry_dt.replace(tzinfo=UTC)
                now = datetime.now(UTC)
                delay = max(0.0, (retry_dt - now).total_seconds())

    if delay is None:
        exponent = max(0, attempt - 1)
        backoff = (NOVA_BACKOFF_FACTOR**exponent) * NOVA_INITIAL_BACKOFF_S
        next_backoff = backoff * NOVA_BACKOFF_FACTOR
        delay = random.uniform(backoff, next_backoff)

    return min(delay, NOVA_MAX_RETRY_AFTER_S)


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


_ERROR_SNIPPET_MAX = 512


def _beautify_text(resp_text: str) -> str:
    """Return a human-readable snippet of a response body for logging purposes."""

    if not resp_text:
        return ""

    if _BS4_AVAILABLE and _beautiful_soup_factory is not None:
        try:
            text_raw = _beautiful_soup_factory(resp_text, "html.parser").get_text(
                separator=" ", strip=True
            )
            text = str(text_raw)
        except Exception as err:  # pragma: no cover - defensive logging path
            _LOGGER.debug(
                "Failed to parse error response body via BeautifulSoup: %s", err
            )
        else:
            if text:
                return text[:_ERROR_SNIPPET_MAX]

    return resp_text[:_ERROR_SNIPPET_MAX]


# --- Custom Exceptions ---
class NovaError(Exception):
    """Base exception for Nova API errors."""


class NovaAuthError(NovaError):
    """Raised on 4xx client errors after retries.

    This is the base class for authentication errors. It may be transient
    (e.g., temporary 401 after token refresh due to backend propagation delay)
    or permanent (e.g., AAS token invalidated by Google).

    Attributes:
        status: HTTP status code (typically 401 or 403).
        detail: Human-readable error detail.
        is_permanent: If True, re-authentication is required. If False,
            the error may resolve on its own in subsequent poll cycles.
    """

    def __init__(
        self,
        status: int,
        detail: str | None = None,
        *,
        is_permanent: bool = False,
    ):
        super().__init__(f"HTTP Client Error {status}: {detail or ''}".strip())
        self.status = status
        self.detail = detail
        self.is_permanent = is_permanent


class NovaAuthPermanentError(NovaAuthError):
    """Raised when re-authentication is definitively required.

    This is raised when the underlying AAS token has been rejected by Google,
    meaning the user must re-authenticate through the config flow. There is
    no possibility of self-healing without new credentials.
    """

    def __init__(self, status: int, detail: str | None = None):
        super().__init__(status, detail, is_permanent=True)


class NovaRateLimitError(NovaError):
    """Raised on 429 rate-limiting errors after retries."""

    def __init__(self, detail: str | None = None):
        super().__init__(f"Rate limited by upstream API: {detail or ''}".strip())
        self.detail = detail


class NovaHTTPError(NovaError):
    """Raised for 5xx server errors after retries."""

    def __init__(self, status: int, detail: str | None = None):
        super().__init__(f"HTTP Server Error {status}: {detail or ''}".strip())
        self.status = status
        self.detail = detail


# ------------------------ Optional Home Assistant hooks ------------------------
# These hooks allow the integration to supply a shared aiohttp ClientSession.
_STATE: dict[str, Any] = {
    "hass": None,
    "async_refresh_lock": None,
    "async_refresh_lock_loop_id": None,
    "cache_provider": None,
}

# Key for storing auth retry deadline in cache (prevents parallel refresh storms)
AUTH_RETRY_DEADLINE_KEY = "auth_retry_deadline"


def register_hass(hass: HomeAssistant) -> None:
    """Register a Home Assistant instance to provide a shared ClientSession."""

    _STATE["hass"] = hass


def unregister_hass() -> None:
    """Unregister the Home Assistant instance reference."""

    _STATE["hass"] = None


# Cache provider hook for multi-entry support using contextvars for async safety
_CACHE_PROVIDER: contextvars.ContextVar[Callable[[], Any] | None] = (
    contextvars.ContextVar("_cache_provider", default=None)
)


def register_cache_provider(provider: Callable[[], Any]) -> None:
    """Register a callable that returns the entry-specific cache (context-local).

    Args:
        provider: Callable that returns a TokenCache instance.

    This allows multi-entry scenarios where each API instance uses its own cache
    instead of relying on the global cache facade. Uses contextvars to ensure
    concurrent async requests don't interfere with each other.
    """
    _STATE["cache_provider"] = provider
    _CACHE_PROVIDER.set(provider)


def unregister_cache_provider() -> None:
    """Unregister the cache provider for the current context."""
    _STATE["cache_provider"] = None
    _CACHE_PROVIDER.set(None)


def resolve_cache_from_provider() -> TokenCache | None:
    """Return the cache supplied by the registered provider, if any."""

    provider: Callable[[], TokenCache | None] | None = _CACHE_PROVIDER.get()
    if provider is None:
        provider = cast(
            Callable[[], TokenCache | None] | None, _STATE.get("cache_provider")
        )
    if provider is None:
        return None
    try:
        cache = provider()
        if cache is not None and getattr(cache, "_closed", False):
            return None
        return cache
    except Exception as exc:  # pragma: no cover - defensive guard
        _LOGGER.warning("Cache provider callable failed: %s", exc)
        return None


def _get_cache_provider() -> TokenCache | None:
    """Get the registered cache provider for the current context or None.

    Returns None if no provider is registered or if the provider's cache is closed.
    """
    return resolve_cache_from_provider()


# --- Refresh Locks ---


def _get_async_refresh_lock() -> asyncio.Lock:
    """Lazily initialize and return the async refresh lock.

    Creates a new lock if the event loop has changed (e.g., after HA restart)
    to avoid 'attached to a different loop' errors.
    """
    try:
        current_loop = asyncio.get_running_loop()
        current_loop_id = id(current_loop)
    except RuntimeError:
        # No running loop - create lock anyway, will be used when loop starts
        current_loop_id = None

    if (
        _STATE["async_refresh_lock"] is None
        or _STATE["async_refresh_lock_loop_id"] != current_loop_id
    ):
        _STATE["async_refresh_lock"] = asyncio.Lock()
        _STATE["async_refresh_lock_loop_id"] = current_loop_id
    return cast(asyncio.Lock, _STATE["async_refresh_lock"])


async def _get_initial_token_async(
    username: str,
    logger: logging.Logger,
    *,
    ns_prefix: str = "",
    cache: TokenCache | None,
) -> str:
    """Return an ADM token for *username* from the entry-scoped cache or API."""

    resolved_cache = cache or resolve_cache_from_provider()
    if resolved_cache is None:
        raise ValueError("TokenCache instance is required for Nova requests.")

    normalized_user = (username or "").strip().lower()
    if not normalized_user:
        raise ValueError("Username is empty/invalid; cannot retrieve ADM token.")

    prefix = (ns_prefix or "").strip()
    if prefix and not prefix.endswith(":"):
        prefix += ":"

    cache_key = f"{prefix}adm_token_{normalized_user}"

    cached = await resolved_cache.get(cache_key)
    if isinstance(cached, str) and cached:
        return cached

    if prefix:
        fallback_key = f"adm_token_{normalized_user}"
        fallback = await resolved_cache.get(fallback_key)
        if isinstance(fallback, str) and fallback:
            try:
                await resolved_cache.set(cache_key, fallback)
            except Exception as err:  # pragma: no cover - defensive logging
                logger.debug(
                    "Failed to mirror ADM token to namespaced key",
                    extra={"cache_key": cache_key},
                    exc_info=err,
                )
            return fallback

    token = await async_get_adm_token_api(normalized_user, cache=resolved_cache)

    if prefix:
        try:
            await resolved_cache.set(cache_key, token)
        except Exception as err:  # pragma: no cover - defensive logging
            logger.debug(
                "Failed to persist ADM token to namespaced key",
                extra={"cache_key": cache_key},
                exc_info=err,
            )

    return token


# ------------------------ TTL policy (shared core) ------------------------
class TTLPolicy:
    """Token TTL/probe policy (synchronous I/O).

    Encapsulates probe arming, jitter and proactive refresh. The policy records
    the best-known TTL and times token issuance, allowing pre-emptive refreshes.
    """

    TTL_MARGIN_SEC, JITTER_SEC = 120, 90
    PROBE_INTERVAL_SEC, PROBE_INTERVAL_JITTER_PCT = 6 * 3600, 0.1
    # Minimum TTL threshold: ignore TTL learning for tokens that expire too quickly,
    # as this typically indicates a transient server issue (rate-limit, propagation
    # delay) rather than the actual token lifetime. 5 minutes is a safe minimum since
    # ADM tokens typically last 1-4 hours.
    MIN_TTL_FOR_LEARNING_SEC = 300  # 5 minutes

    def __init__(  # noqa: PLR0913
        self,
        username: str,
        logger: logging.Logger,
        get_value: Callable[[str], float | int | str | None],
        set_value: Callable[[str, object | None], None],
        refresh_fn: Callable[[], str | None],
        set_auth_header_fn: Callable[[str], None],
        ns_prefix: str = "",
    ) -> None:  # noqa: PLR0913
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
        self._get, self._set, self._refresh, self._set_auth = (
            get_value,
            set_value,
            refresh_fn,
            set_auth_header_fn,
        )
        self._ns = (ns_prefix or "").strip()
        if self._ns and not self._ns.endswith(":"):
            self._ns += ":"

    # Cache keys for this username (optionally namespaced)
    @property
    def k_issued(self) -> str:
        return f"{self._ns}adm_token_issued_at_{self.username}"

    @property
    def k_bestttl(self) -> str:
        return f"{self._ns}adm_best_ttl_sec_{self.username}"

    @property
    def k_startleft(self) -> str:
        return f"{self._ns}adm_probe_startup_left_{self.username}"

    @property
    def k_probenext(self) -> str:
        return f"{self._ns}adm_probe_next_at_{self.username}"

    @property
    def k_armed(self) -> str:
        return f"{self._ns}adm_probe_armed_{self.username}"

    def _key_variants(self, base: str) -> set[str]:
        """Return the set of cache key variants for a given base name."""
        if self._ns:
            return {f"{self._ns}{base}", base}
        return {base}

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
        elif probenext is None:
            self._set(
                self.k_probenext,
                now
                + self._jitter_pct(
                    self.PROBE_INTERVAL_SEC, self.probe_interval_jitter_pct
                ),
            )
        elif now >= float(probenext):
            do_arm = True
            self._set(
                self.k_probenext,
                now
                + self._jitter_pct(
                    self.PROBE_INTERVAL_SEC, self.probe_interval_jitter_pct
                ),
            )

        if do_arm:
            self._set(self.k_armed, 1)
            return True
        return bool(self._get(self.k_armed))

    def _do_refresh(self, now: float) -> str | None:
        """Refresh token, update header and issuance timestamp, bootstrap startup probes if missing."""
        bases_to_clear = (
            f"adm_token_{self.username}",
            f"adm_token_issued_at_{self.username}",
        )
        for base in bases_to_clear:
            for key in self._key_variants(base):
                try:
                    self._set(key, None)
                except Exception:
                    pass
        tok = self._refresh()
        if tok:
            token_base = f"adm_token_{self.username}"
            for key in self._key_variants(token_base):
                try:
                    self._set(key, tok)
                except Exception:
                    pass

            self._set_auth("Bearer " + tok)
            issued_base = f"adm_token_issued_at_{self.username}"
            for key in self._key_variants(issued_base):
                try:
                    self._set(key, now)
                except Exception:
                    pass
            if self._get(self.k_startleft) is None:
                probe_base = f"adm_probe_startup_left_{self.username}"
                for key in self._key_variants(probe_base):
                    try:
                        if self._get(key) is None:
                            self._set(key, 3)
                    except Exception:
                        pass
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
                    self.log.info(
                        "ADM token reached measured threshold – proactively refreshing..."
                    )
                    self._do_refresh(now)
            except (ValueError, TypeError) as e:
                self.log.debug("Threshold check failed: %s", e)

    async def async_pre_request(self) -> None:
        """Async wrapper for compatibility with AsyncTTLPolicy."""

        self.pre_request()

    def on_401(self, adaptive_downshift: bool = True) -> str | None:
        """Handle 401: measure TTL, adapt quickly on unexpected short TTLs, then refresh."""
        now = time.time()
        issued = self._get(self.k_issued)
        if issued is None:
            self.log.warning(
                "Got 401 – issued timestamp missing; attempting token refresh."
            )
            return self._do_refresh(now)

        age_sec = now - float(issued)
        age_min = age_sec / 60.0
        planned_probe = bool(self._get(self.k_armed))

        if planned_probe:
            self.log.info("Got 401 (forced probe) – measured TTL: %.1f min.", age_min)
            self._set(self.k_bestttl, age_sec)  # always accept probe (up or down)
            self._set(self.k_armed, 0)  # coalesce multiple 401 in same probe window
            left = self._get(self.k_startleft)
            if left and int(left) > 0:
                try:
                    self._set(self.k_startleft, int(left) - 1)
                except (ValueError, TypeError):
                    self._set(self.k_startleft, 0)
        else:
            self.log.warning(
                "Got 401 – token expired after %.1f min (unplanned).", age_min
            )
            if adaptive_downshift:
                best = self._get(self.k_bestttl)
                try:
                    # Skip TTL learning for very short lifetimes - these typically indicate
                    # transient server issues (rate-limit, propagation race) not actual TTL.
                    if age_sec < self.MIN_TTL_FOR_LEARNING_SEC:
                        self.log.debug(
                            "Ignoring TTL learning: observed %.1f min < %.1f min minimum threshold.",
                            age_min,
                            self.MIN_TTL_FOR_LEARNING_SEC / 60.0,
                        )
                    # If clearly shorter than our current model (>10% shorter), recalibrate.
                    elif best and (age_sec + self.TTL_MARGIN_SEC) < 0.9 * float(best):
                        self.log.warning(
                            "Unexpected short TTL – recalibrating best known TTL with safety buffer."
                        )
                        safe_calibration = age_sec * 0.95
                        self._set(self.k_bestttl, safe_calibration)
                except (ValueError, TypeError) as e:
                    self.log.debug("Recalibration check failed: %s", e)

        # Always refresh after a 401 to resume normal operation.
        return self._do_refresh(now)

    async def async_on_401(self, adaptive_downshift: bool = True) -> str | None:  # noqa: PLR0915
        """Async wrapper delegating to the synchronous policy."""

        return self.on_401(adaptive_downshift=adaptive_downshift)


class AsyncTTLPolicy(TTLPolicy):
    """Native async version of the TTL policy (no blocking calls).

    This policy manages both ADM and AAS token TTL learning:
    - ADM tokens: proactive refresh based on measured TTL
    - AAS tokens: proactive refresh based on learned lifetime (when AAS+ADM chain fails)
    """

    # AAS token TTL learning constants
    AAS_TTL_MARGIN_SEC = 300  # 5 min margin before expiry
    AAS_DEFAULT_TTL_SEC = 7 * 24 * 3600  # 7 days default (conservative estimate)

    def __init__(  # noqa: PLR0913
        self,
        *,
        username: str,
        logger: logging.Logger,
        get_value: Callable[[str], Awaitable[Any]],
        set_value: Callable[[str, Any], Awaitable[None]],
        refresh_fn: Callable[[], Awaitable[str | None]],
        set_auth_header_fn: Callable[[str], None],
        ns_prefix: str = "",
    ) -> None:  # noqa: PLR0913
        super().__init__(
            username=username,
            logger=logger,
            get_value=lambda _: None,
            set_value=lambda _key, _value: None,
            refresh_fn=lambda: None,
            set_auth_header_fn=set_auth_header_fn,
            ns_prefix=ns_prefix,
        )
        self._aget: Callable[[str], Awaitable[Any]] = get_value
        self._aset: Callable[[str, Any], Awaitable[None]] = set_value
        self._arefresh: Callable[[], Awaitable[str | None]] = refresh_fn

    # --- AAS TTL learning cache keys ---
    @property
    def k_aas_issued(self) -> str:
        """Cache key for AAS token issuance timestamp."""
        return f"{self._ns}aas_token_issued_at_{self.username}"

    @property
    def k_aas_bestttl(self) -> str:
        """Cache key for best known AAS token TTL."""
        return f"{self._ns}aas_best_ttl_sec_{self.username}"

    async def async_record_aas_issued(self) -> None:
        """Record the issuance timestamp when a fresh AAS token is generated.

        This enables TTL learning for AAS tokens by tracking how long they
        remain valid before Google rejects them.
        """
        now = time.time()
        for key in self._key_variants(f"aas_token_issued_at_{self.username}"):
            try:
                await self._aset(key, now)
            except Exception:  # noqa: BLE001 - defensive cache write
                pass

    async def async_invalidate_aas_token(self) -> None:
        """Clear the cached AAS token and learn its actual TTL.

        This should be called when persistent 401 errors occur after a token
        refresh, as it indicates the underlying AAS token may be invalid even
        if gpsoauth didn't explicitly reject it.

        TTL Learning: When invalidating, we record the observed lifetime to
        enable proactive refresh before future tokens expire.
        """
        now = time.time()

        # Learn AAS TTL from observed lifetime
        issued_raw = await self._aget(self.k_aas_issued)
        if issued_raw is not None:
            try:
                issued_at = float(issued_raw)
                observed_ttl = now - issued_at
                observed_ttl_hours = observed_ttl / 3600
                self.log.info(
                    "AAS token for %s lived %.1f hours before invalidation.",
                    self.username,
                    observed_ttl_hours,
                )
                # Update best known TTL (conservative: take the shorter one)
                best_raw = await self._aget(self.k_aas_bestttl)
                if best_raw is None or observed_ttl < float(best_raw):
                    # Apply 5% safety margin
                    safe_ttl = observed_ttl * 0.95
                    for key in self._key_variants(f"aas_best_ttl_sec_{self.username}"):
                        try:
                            await self._aset(key, safe_ttl)
                        except Exception:  # noqa: BLE001 - defensive cache write
                            pass
                    self.log.info(
                        "Updated AAS best TTL for %s to %.1f hours (with 5%% margin).",
                        self.username,
                        safe_ttl / 3600,
                    )
            except (TypeError, ValueError) as e:
                self.log.debug("Failed to learn AAS TTL: %s", e)

        self.log.warning(
            "Invalidating cached AAS token for %s due to persistent 401 errors.",
            self.username,
        )
        for key in self._key_variants(DATA_AAS_TOKEN):
            try:
                await self._aset(key, None)
            except Exception:
                pass
        # Clear issued timestamp so next refresh records a fresh one
        for key in self._key_variants(f"aas_token_issued_at_{self.username}"):
            try:
                await self._aset(key, None)
            except Exception:
                pass
        # Also clear ADM issued timestamp to bypass stampede guard in async_on_401
        # This ensures the subsequent refresh actually happens instead of being skipped
        for key in self._key_variants(f"adm_token_issued_at_{self.username}"):
            try:
                await self._aset(key, None)
            except Exception:
                pass

    async def async_check_aas_proactive_refresh(self) -> bool:
        """Check if AAS token should be proactively refreshed based on learned TTL.

        Returns:
            True if AAS token was refreshed, False otherwise.
        """
        now = time.time()
        issued_raw = await self._aget(self.k_aas_issued)
        best_ttl_raw = await self._aget(self.k_aas_bestttl)

        if issued_raw is None or best_ttl_raw is None:
            return False

        try:
            issued_at = float(issued_raw)
            best_ttl = float(best_ttl_raw)
            age = now - issued_at
            threshold = max(0.0, best_ttl - self.AAS_TTL_MARGIN_SEC)
            threshold = self._jitter_sec(threshold, self.JITTER_SEC)

            if age >= threshold:
                self.log.info(
                    "AAS token for %s reached measured threshold (%.1f hours) – "
                    "proactively refreshing token chain.",
                    self.username,
                    best_ttl / 3600,
                )
                await self.async_invalidate_aas_token()
                # The next ADM refresh will trigger a fresh AAS token generation
                return True
        except (TypeError, ValueError) as e:
            self.log.debug("AAS proactive refresh check failed: %s", e)

        return False

    async def _arm_probe_if_due_async(self, now: float) -> bool:
        startup_left = await self._aget(self.k_startleft)
        probenext = await self._aget(self.k_probenext)
        do_arm = bool(startup_left and int(startup_left) > 0)
        if not do_arm:
            if probenext is None:
                await self._aset(
                    self.k_probenext,
                    now
                    + self._jitter_pct(
                        self.PROBE_INTERVAL_SEC, self.PROBE_INTERVAL_JITTER_PCT
                    ),
                )
            elif now >= float(probenext):
                do_arm = True
                await self._aset(
                    self.k_probenext,
                    now
                    + self._jitter_pct(
                        self.PROBE_INTERVAL_SEC, self.PROBE_INTERVAL_JITTER_PCT
                    ),
                )
        if do_arm:
            await self._aset(self.k_armed, 1)
            return True
        return bool(await self._aget(self.k_armed))

    async def _do_refresh_async(self, now: float) -> str | None:  # noqa: PLR0912
        bases_to_clear = (
            f"adm_token_{self.username}",
            f"adm_token_issued_at_{self.username}",
        )
        for base in bases_to_clear:
            for key in self._key_variants(base):
                try:
                    await self._aset(key, None)
                except Exception:
                    pass
        try:
            tok = await self._arefresh()  # async callable
        except InvalidAasTokenError as err:
            self.log.error(
                "ADM token refresh failed because the cached AAS token was rejected; re-authentication is required."
            )
            for key in self._key_variants(DATA_AAS_TOKEN):
                try:
                    await self._aset(key, None)
                except Exception:
                    pass
            raise NovaAuthPermanentError(
                401, "AAS token invalid during ADM refresh"
            ) from err
        if tok:
            token_base = f"adm_token_{self.username}"
            for key in self._key_variants(token_base):
                try:
                    await self._aset(key, tok)
                except Exception:
                    pass

            self._set_auth("Bearer " + tok)
            issued_base = f"adm_token_issued_at_{self.username}"
            for key in self._key_variants(issued_base):
                try:
                    await self._aset(key, now)
                except Exception:
                    pass
            if await self._aget(self.k_startleft) is None:
                probe_base = f"adm_probe_startup_left_{self.username}"
                for key in self._key_variants(probe_base):
                    try:
                        if await self._aget(key) is None:
                            await self._aset(key, 3)
                    except Exception:
                        pass
        return tok

    async def async_pre_request(self) -> None:
        """Perform proactive token refresh checks for both AAS and ADM tokens.

        This method checks:
        1. AAS token: if approaching learned TTL, invalidate to trigger fresh generation
        2. ADM token: if approaching measured TTL, proactively refresh
        """
        # Check AAS token proactive refresh first (longer-lived, less frequent)
        await self.async_check_aas_proactive_refresh()

        # Then check ADM token proactive refresh (shorter-lived, more frequent)
        now = time.time()
        issued_at = await self._aget(self.k_issued)
        best_ttl = await self._aget(self.k_bestttl)
        armed = await self._arm_probe_if_due_async(now)
        if (not armed) and issued_at and best_ttl:
            try:
                age = now - float(issued_at)
                threshold = max(0.0, float(best_ttl) - self.TTL_MARGIN_SEC)
                threshold = self._jitter_sec(threshold, self.JITTER_SEC)
                if age >= threshold:
                    self.log.info(
                        "ADM token reached measured threshold – proactively refreshing (async)…"
                    )
                    await self._do_refresh_async(now)
            except (ValueError, TypeError) as e:
                self.log.debug("Threshold check failed (async): %s", e)

    async def async_on_401(self, adaptive_downshift: bool = True) -> str | None:  # noqa: PLR0912,PLR0915
        """Async 401 handling with stampede guard and async cache I/O."""
        lock = _get_async_refresh_lock()
        async with lock:
            now = time.time()

            # Skip duplicate refresh if someone just refreshed recently.
            issued_raw = await self._aget(self.k_issued)
            issued: float | None
            try:
                issued = float(issued_raw) if issued_raw is not None else None
            except (TypeError, ValueError):
                self.log.debug(
                    "Cached issued timestamp is invalid (value=%r); forcing refresh.",
                    issued_raw,
                )
                issued = None

            if issued is not None and (now - issued) < RECENT_REFRESH_WINDOW_S:
                self.log.debug(
                    "Another task already refreshed the token; skipping duplicate refresh."
                )
                token_value: str | None = None
                token_base = f"adm_token_{self.username}"
                for key in self._key_variants(token_base):
                    try:
                        candidate = await self._aget(key)
                    except Exception as err:  # noqa: BLE001 - defensive cache read
                        self.log.debug(
                            "Failed to read cached token from key '%s': %s", key, err
                        )
                        continue
                    if isinstance(candidate, bytes):
                        candidate = candidate.decode()
                    if isinstance(candidate, str) and candidate:
                        token_value = candidate
                        break

                if token_value is not None:
                    self._set_auth("Bearer " + token_value)
                    return token_value

                self.log.debug(
                    "Recent refresh detected but no cached ADM token available; forcing refresh."
                )
                issued = None  # Ensure we fall through to refresh logic below.

            if issued is None:
                self.log.info(
                    "Got 401 – issued timestamp missing; attempting token refresh (async)."
                )
                return await self._do_refresh_async(now)

            age_sec = now - float(issued)
            age_min = age_sec / 60.0
            planned_probe = bool(await self._aget(self.k_armed))

            if planned_probe:
                self.log.info(
                    "Got 401 (forced probe) – measured TTL: %.1f min.", age_min
                )
                await self._aset(self.k_bestttl, age_sec)
                await self._aset(self.k_armed, 0)
                left = await self._aget(self.k_startleft)
                if left and int(left) > 0:
                    try:
                        await self._aset(self.k_startleft, int(left) - 1)
                    except (ValueError, TypeError):
                        await self._aset(self.k_startleft, 0)
            else:
                self.log.info(
                    "Got 401 – token expired after %.1f min (unplanned).", age_min
                )
                if adaptive_downshift:
                    best = await self._aget(self.k_bestttl)
                    try:
                        # Skip TTL learning for very short lifetimes - these typically indicate
                        # transient server issues (rate-limit, propagation race) not actual TTL.
                        if age_sec < self.MIN_TTL_FOR_LEARNING_SEC:
                            self.log.debug(
                                "Ignoring TTL learning: observed %.1f min < %.1f min minimum threshold.",
                                age_min,
                                self.MIN_TTL_FOR_LEARNING_SEC / 60.0,
                            )
                        elif best and (age_sec + self.TTL_MARGIN_SEC) < 0.9 * float(
                            best
                        ):
                            self.log.warning(
                                "Unexpected short TTL – recalibrating best known TTL with safety buffer (async)."
                            )
                            safe_calibration = age_sec * 0.95
                            await self._aset(self.k_bestttl, safe_calibration)
                    except (ValueError, TypeError) as e:
                        self.log.debug("Recalibration check failed (async): %s", e)
            return await self._do_refresh_async(now)


async def async_nova_request(  # noqa: PLR0913,PLR0912,PLR0915
    api_scope: str,
    hex_payload: str,
    *,
    username: str | None = None,
    session: aiohttp.ClientSession | None = None,
    # Optional: initial token for config-flow isolation (bypass cache lookups)
    token: str | None = None,
    # Optional overrides for flow-local validation (avoid global cache conflicts)
    cache_get: Callable[[str], Awaitable[Any]] | None = None,
    cache_set: Callable[[str, Any], Awaitable[None]] | None = None,
    refresh_override: Callable[[], Awaitable[str | None]] | None = None,
    # Optional: entry-specific namespace for cache keys (e.g., entry_id)
    namespace: str | None = None,
    # Entry-scoped TokenCache for strict multi-account separation
    cache: TokenCache | None = None,
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
        cache: Optional entry-scoped TokenCache for strict multi-account separation.

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

    ns_raw = (namespace or "").strip()
    ns_prefix = f"{ns_raw}:" if ns_raw and not ns_raw.endswith(":") else ns_raw

    resolved_cache = cache or resolve_cache_from_provider()
    if resolved_cache is None:
        raise ValueError("TokenCache instance is required for Nova requests.")

    async def _cache_get(key: str) -> Any:
        if cache_get is not None:
            return await cache_get(key)
        return await resolved_cache.get(key)

    async def _cache_set(key: str, value: Any) -> None:
        if cache_set is not None:
            await cache_set(key, value)
            return
        await resolved_cache.set(key, value)

    user: str | None
    if isinstance(username, str) and username.strip():
        user = username.strip()
    else:
        val = await resolved_cache.get(username_string)
        user = str(val).strip() if isinstance(val, str) and val else None
        if not user:
            fetched = await async_get_username(cache=resolved_cache)
            user = fetched.strip() if isinstance(fetched, str) and fetched else None
    if user is None:
        raise ValueError("Username is not available for async_nova_request.")

    if (
        isinstance(token, str)
        and token
        and isinstance(username, str)
        and username.strip()
    ):
        try:
            await resolved_cache.set(DATA_AAS_TOKEN, token)
            if ns_prefix:
                await resolved_cache.set(f"{ns_prefix}{DATA_AAS_TOKEN}", token)
        except Exception as err:  # noqa: BLE001 - defensive caching
            _LOGGER.debug("Failed to seed provided flow token into cache", exc_info=err)

    initial_token = await _get_initial_token_async(
        user, _LOGGER, ns_prefix=(namespace or ""), cache=resolved_cache
    )

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
    if refresh_override is not None:
        rf_fn: Callable[[], Awaitable[str | None]] = refresh_override
    else:
        rf_fn = lambda: async_get_adm_token_api(user, cache=resolved_cache)  # noqa: E731

    policy = AsyncTTLPolicy(
        username=user,
        logger=_LOGGER,
        get_value=_cache_get,
        set_value=_cache_set,
        refresh_fn=rf_fn,
        set_auth_header_fn=lambda bearer: headers.update({"Authorization": bearer}),
        ns_prefix=(namespace or ""),
    )
    await policy.async_pre_request()

    # --- Pre-request gate: wait if auth refresh is in progress ---
    # This prevents unnecessary 401s when another request is already refreshing.
    # Instead of each request hitting Google's API and getting 401, they wait here.
    ns_deadline_key = (
        f"{ns_prefix}{AUTH_RETRY_DEADLINE_KEY}"
        if ns_prefix
        else AUTH_RETRY_DEADLINE_KEY
    )
    deadline_raw = await _cache_get(ns_deadline_key)
    if deadline_raw is not None:
        try:
            deadline = float(deadline_raw)
            now = time.time()
            if now < deadline:
                wait_time = min(deadline - now + 1.0, 120.0)
                _LOGGER.info(
                    "Nova API: auth refresh in progress. Waiting %.1fs before request to %s.",
                    wait_time,
                    api_scope,
                )
                await asyncio.sleep(wait_time)
                # Reload token after waiting (may have been refreshed)
                token_key = f"adm_token_{user}"
                if ns_prefix:
                    token_key = f"{ns_prefix}adm_token_{user}"
                fresh_token = await _cache_get(token_key)
                if fresh_token:
                    headers["Authorization"] = f"Bearer {fresh_token}"
                    _LOGGER.debug(
                        "Nova API: using refreshed token for request to %s.", api_scope
                    )
        except (ValueError, TypeError):
            pass  # Invalid deadline, proceed normally

    ephemeral_session = False
    if session is None:
        hass_ref = _STATE.get("hass")
        if hass_ref:
            # Use the HA-managed shared session for best performance and resource management.
            async_get_clientsession = import_module(
                "homeassistant.helpers.aiohttp_client"
            ).async_get_clientsession

            session = async_get_clientsession(hass_ref)
        else:
            # Fallback for environments without a shared session (e.g., standalone scripts).
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=16, enable_cleanup_closed=True)
            )
            ephemeral_session = True

    try:
        retries_used = 0
        auth_retries_used = 0  # Counter for 401 retries after token refresh
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
                    _LOGGER.debug(
                        "Nova API async request to %s: status=%d", api_scope, status
                    )

                    if status == HTTP_OK:
                        return cast(bytes, content).hex()

                    text_snippet = _redact(
                        _beautify_text(content.decode(errors="ignore"))
                    )

                    if status == HTTP_UNAUTHORIZED:
                        # 401 Retry Sequence (OAuth → AAS → ADM token chain):
                        # ----------------------------------------------------------
                        # Step 0 (initial): Request fails with 401
                        # Step 1: Refresh ADM only → wait 6s (propagation) → retry
                        # Step 2: If 401: wait 61s → invalidate AAS, refresh AAS+ADM → wait 6s → retry
                        # Step 3: If 401: wait 501s (long cooldown) → retry
                        # Step 4: If 401: permanent error, re-auth required
                        #
                        # Delays use odd numbers to avoid hitting rate-limit boundaries.
                        # The 6s propagation delay allows Google's backend to sync new tokens.
                        _PROPAGATION_DELAY_S = 6.0
                        _SHORT_COOLDOWN_S = 61.0  # ~1 min
                        _LONG_COOLDOWN_S = 501.0  # ~8 min
                        # Auth retry step constants
                        _AUTH_STEP_ADM_REFRESH = 0
                        _AUTH_STEP_AAS_ADM_REFRESH = 1
                        _AUTH_STEP_LONG_COOLDOWN = 2
                        max_auth_retries = 4

                        # --- Race condition guard ---
                        # Check if another request is already handling auth refresh.
                        # If so, wait for it to complete instead of starting parallel refreshes.
                        ns_deadline_key = (
                            f"{ns_prefix}{AUTH_RETRY_DEADLINE_KEY}"
                            if ns_prefix
                            else AUTH_RETRY_DEADLINE_KEY
                        )
                        deadline_raw = await _cache_get(ns_deadline_key)
                        now = time.time()
                        if deadline_raw is not None:
                            try:
                                deadline = float(deadline_raw)
                                if now < deadline:
                                    # Another request is handling refresh - wait and retry
                                    wait_time = min(deadline - now + 1.0, 30.0)
                                    _LOGGER.info(
                                        "Nova API: auth refresh in progress by another request. "
                                        "Waiting %.1fs before retry.",
                                        wait_time,
                                    )
                                    await asyncio.sleep(wait_time)
                                    # Reload token from cache (may have been refreshed)
                                    token_key = f"adm_token_{user}"
                                    if ns_prefix:
                                        token_key = f"{ns_prefix}adm_token_{user}"
                                    fresh_token = await _cache_get(token_key)
                                    if fresh_token:
                                        headers["Authorization"] = (
                                            f"Bearer {fresh_token}"
                                        )
                                    continue  # Retry with possibly fresh token
                            except (ValueError, TypeError):
                                pass  # Invalid deadline, proceed normally

                        # Log all auth retries as WARNING for visibility.
                        # Previously, attempt 1/4 was logged as INFO, making it invisible
                        # in WARNING-level logs and causing confusion ("only 2/4 appears").
                        _LOGGER.warning(
                            "Nova API async request to %s: 401 Unauthorized (attempt %d/%d).",
                            api_scope,
                            auth_retries_used + 1,
                            max_auth_retries,
                        )

                        if auth_retries_used == _AUTH_STEP_ADM_REFRESH:
                            # Step 1: First 401 - refresh ADM token only, wait for propagation
                            # Set deadline to prevent parallel refresh storms
                            await _cache_set(
                                ns_deadline_key,
                                time.time() + _PROPAGATION_DELAY_S + 2.0,
                            )
                            _LOGGER.info(
                                "Nova API: refreshing ADM token (AAS unchanged)."
                            )
                            try:
                                await policy.async_on_401()
                            except NovaAuthPermanentError:
                                # AAS token explicitly rejected - no point retrying
                                await _cache_set(
                                    ns_deadline_key, None
                                )  # Clear deadline
                                raise
                            _LOGGER.info(
                                "Nova API: waiting %.0fs for token propagation.",
                                _PROPAGATION_DELAY_S,
                            )
                            await asyncio.sleep(_PROPAGATION_DELAY_S)
                            await _cache_set(ns_deadline_key, None)  # Clear deadline
                            auth_retries_used += 1
                            continue

                        if auth_retries_used == _AUTH_STEP_AAS_ADM_REFRESH:
                            # Step 2: Second 401 - ADM refresh didn't help
                            # Wait cooldown, then invalidate AAS and refresh entire chain
                            # Set deadline for entire cooldown + refresh period
                            await _cache_set(
                                ns_deadline_key,
                                time.time()
                                + _SHORT_COOLDOWN_S
                                + _PROPAGATION_DELAY_S
                                + 2.0,
                            )
                            _LOGGER.warning(
                                "Nova API: ADM refresh failed. Waiting %.0fs before "
                                "refreshing entire token chain (AAS+ADM).",
                                _SHORT_COOLDOWN_S,
                            )
                            await asyncio.sleep(_SHORT_COOLDOWN_S)
                            await policy.async_invalidate_aas_token()
                            _LOGGER.info("Nova API: refreshing AAS+ADM token chain.")
                            try:
                                await policy.async_on_401()
                            except NovaAuthPermanentError:
                                await _cache_set(
                                    ns_deadline_key, None
                                )  # Clear deadline
                                raise
                            _LOGGER.info(
                                "Nova API: waiting %.0fs for token propagation.",
                                _PROPAGATION_DELAY_S,
                            )
                            await asyncio.sleep(_PROPAGATION_DELAY_S)
                            await _cache_set(ns_deadline_key, None)  # Clear deadline
                            auth_retries_used += 1
                            continue

                        if auth_retries_used == _AUTH_STEP_LONG_COOLDOWN:
                            # Step 3: Third 401 - even fresh AAS+ADM didn't help
                            # Wait long cooldown, then retry once more
                            await _cache_set(
                                ns_deadline_key, time.time() + _LONG_COOLDOWN_S + 2.0
                            )
                            _LOGGER.warning(
                                "Nova API: AAS+ADM refresh failed. Waiting %.0fs "
                                "(long cooldown) before final retry.",
                                _LONG_COOLDOWN_S,
                            )
                            await asyncio.sleep(_LONG_COOLDOWN_S)
                            await _cache_set(ns_deadline_key, None)  # Clear deadline
                            auth_retries_used += 1
                            continue

                        # Step 4: Fourth 401 - all retries exhausted
                        # Total wait: 6s + 61s + 6s + 501s = 574s (~9.5 min)
                        total_wait = (
                            _PROPAGATION_DELAY_S
                            + _SHORT_COOLDOWN_S
                            + _PROPAGATION_DELAY_S
                            + _LONG_COOLDOWN_S
                        )
                        _LOGGER.error(
                            "Nova API async request to %s: 401 persists after %d retries "
                            "(total wait: %.0fs). Authentication is invalid; re-auth required.",
                            api_scope,
                            max_auth_retries,
                            total_wait,
                        )
                        raise NovaAuthError(
                            status,
                            "Unauthorized after token refresh; re-authentication required",
                            is_permanent=True,
                        )

                    if status in HTTP_RETRY_ELIGIBLE:
                        if retries_used < NOVA_MAX_RETRIES:
                            delay = _compute_delay(
                                attempt, response.headers.get("Retry-After")
                            )
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
                            if status == HTTP_TOO_MANY_REQUESTS:
                                raise NovaRateLimitError(
                                    f"Nova API rate limited after {NOVA_MAX_RETRIES} attempts."
                                )
                            raise NovaHTTPError(
                                status,
                                f"Nova API failed after {NOVA_MAX_RETRIES} attempts.",
                            )

                    # Non-retryable errors: distinguish between client (4xx) and server (5xx)
                    # - 4xx: Client errors (e.g., 403 Forbidden, 404 Not Found) → NovaAuthError
                    # - 5xx: Server errors (e.g., 501 Not Implemented, 505) → NovaHTTPError
                    if status >= HTTP_INTERNAL_SERVER_ERROR:
                        raise NovaHTTPError(status, text_snippet)
                    raise NovaAuthError(status, text_snippet)

            except asyncio.CancelledError:
                raise
            except (TimeoutError, aiohttp.ClientError) as e:
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
                    raise NovaError(
                        f"Nova API request failed after retries: {e}"
                    ) from e

    finally:
        if ephemeral_session and session:
            await session.close()
