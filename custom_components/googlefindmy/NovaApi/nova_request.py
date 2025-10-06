#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

import binascii
import requests
import aiohttp
from bs4 import BeautifulSoup
import asyncio
import threading
import random
import time

from custom_components.googlefindmy.Auth.aas_token_retrieval import get_aas_token
from custom_components.googlefindmy.Auth.adm_token_retrieval import get_adm_token
from custom_components.googlefindmy.Auth.username_provider import get_username


# ------------------------ TTL policy helper (shared by sync/async) ------------------------
class TTLPolicy:
    """Token TTL/probe policy (synchronous I/O). Encapsulates probe arming, jitter and proactive refresh."""

    # Small, conservative defaults; keep behavior predictable and easy to reason about.
    TTL_MARGIN_SEC = 120                 # fixed buffer to absorb jitter/clock skew
    JITTER_SEC = 90                      # ± seconds jitter applied to threshold
    PROBE_INTERVAL_SEC = 6 * 60 * 60     # base probe cadence
    PROBE_INTERVAL_JITTER_PCT = 0.1      # ±10% jitter for probe schedule

    def __init__(self, username, logger, get_value, set_value, refresh_fn, set_auth_header_fn):
        """
        Dependencies are injected once per policy instance to avoid leaky abstractions and duplication.
        - get_value/set_value: cache I/O
        - refresh_fn: returns a fresh token (string) or None
        - set_auth_header_fn: receives the full 'Authorization' header value (e.g., 'Bearer <token>')
        """
        self.username = username
        self.log = logger
        self._get = get_value
        self._set = set_value
        self._refresh = refresh_fn
        self._set_auth = set_auth_header_fn

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
    def _jitter_sec(base, jitter_abs):
        """Apply symmetric ±jitter_abs (seconds); never return negative."""
        try:
            return max(0.0, base + random.uniform(-jitter_abs, +jitter_abs))
        except Exception:
            return max(0.0, base)

    @staticmethod
    def _jitter_pct(base, pct):
        """Apply symmetric ±pct jitter; never return negative."""
        try:
            return max(0.0, base + random.uniform(-pct * base, +pct * base))
        except Exception:
            return max(0.0, base)

    def _arm_probe_if_due(self, now):
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

    # expose jitter pct via property to allow subclass reuse without duplication
    @property
    def probe_interval_jitter_pct(self):
        return self.PROBE_INTERVAL_JITTER_PCT

    def _do_refresh(self, now):
        """Refresh token, update header and issuance timestamp, bootstrap startup probes if missing."""
        try:
            # clear potentially stale token for this user (best-effort)
            self._set(f"adm_token_{self.username}", None)
        except Exception:
            pass
        tok = self._refresh()
        if tok:
            self._set_auth("Bearer " + tok)
            self._set(self.k_issued, now)
            if not self._get(self.k_startleft):
                self._set(self.k_startleft, 3)  # bootstrap startup probes if missing
        return tok

    def pre_request(self):
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
                self.log.debug(f"Threshold check failed: {e}")

    def on_401(self, adaptive_downshift: bool = True):
        """
        Handle 401: measure TTL, accept probes as ground truth; adapt quickly on unexpected short TTLs.
        Also performs the refresh and returns the new token (or None on failure).
        """
        now = time.time()
        issued = self._get(self.k_issued)
        if issued is None:
            # Still attempt to refresh to recover, but log that TTL measurement was unavailable.
            self.log.warning("Got 401 – issued timestamp missing; attempting token refresh.")
            return self._do_refresh(now)

        age_sec = now - float(issued)
        age_min = age_sec / 60.0
        planned_probe = bool(self._get(self.k_armed))

        if planned_probe:
            self.log.info(f"Got 401 (forced probe) – measured TTL: {age_min:.1f} min.")
            self._set(self.k_bestttl, age_sec)   # always accept probe (up or down)
            self._set(self.k_armed, 0)          # coalesce multiple 401 in same probe window
            left = self._get(self.k_startleft)
            if left and int(left) > 0:
                try:
                    self._set(self.k_startleft, int(left) - 1)
                except (ValueError, TypeError):
                    self._set(self.k_startleft, 0)
        else:
            self.log.warning(f"Got 401 – token expired after {age_min:.1f} min (unplanned).")
            if adaptive_downshift:
                best = self._get(self.k_bestttl)
                try:
                    # If clearly shorter than our current model (>10% shorter), recalibrate immediately.
                    if best and (age_sec + self.TTL_MARGIN_SEC) < 0.9 * float(best):
                        self.log.warning("Unexpected short TTL – recalibrating best known TTL.")
                        self._set(self.k_bestttl, age_sec)
                except (ValueError, TypeError) as e:
                    self.log.debug(f"Recalibration check failed: {e}")

        # Always refresh after a 401 to resume normal operation.
        return self._do_refresh(now)


class AsyncTTLPolicy(TTLPolicy):
    """Native async version (no nested loops, no run_until_complete)."""

    def __init__(self, username, logger, async_get_value, async_set_value, async_refresh_fn, set_auth_header_fn):
        super().__init__(username, logger, async_get_value, async_set_value, async_refresh_fn, set_auth_header_fn)

    async def _arm_probe_if_due_async(self, now):
        startup_left = await self._get(self.k_startleft)
        probenext = await self._get(self.k_probenext)

        do_arm = False
        if startup_left and int(startup_left) > 0:
            do_arm = True
        else:
            if probenext is None:
                await self._set(self.k_probenext, now + self._jitter_pct(self.PROBE_INTERVAL_SEC, self.probe_interval_jitter_pct))
            elif now >= float(probenext):
                do_arm = True
                await self._set(self.k_probenext, now + self._jitter_pct(self.PROBE_INTERVAL_SEC, self.probe_interval_jitter_pct))

        if do_arm:
            await self._set(self.k_armed, 1)
            return True
        return bool(await self._get(self.k_armed))

    async def _do_refresh_async(self, now):
        """Refresh token, update header and issuance timestamp, bootstrap startup probes if missing (async)."""
        try:
            await self._set(f"adm_token_{self.username}", None)
        except Exception:
            pass
        tok = await self._refresh()  # e.g., await asyncio.to_thread(get_adm_token, username)
        if tok:
            self._set_auth("Bearer " + tok)
            await self._set(self.k_issued, now)
            if not await self._get(self.k_startleft):
                await self._set(self.k_startleft, 3)
        return tok

    async def pre_request(self):
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
                self.log.debug(f"Threshold check failed (async): {e}")

    async def on_401(self, adaptive_downshift: bool = True):
        """
        Handle 401 (async): measure TTL, accept probes as ground truth; adapt quickly on unexpected short TTLs.
        Also performs the refresh and returns the new token (or None on failure).
        """
        now = time.time()
        issued = await self._get(self.k_issued)
        if issued is None:
            self.log.warning("Got 401 – issued timestamp missing; attempting token refresh (async).")
            return await self._do_refresh_async(now)

        age_sec = now - float(issued)
        age_min = age_sec / 60.0
        planned_probe = bool(await self._get(self.k_armed))

        if planned_probe:
            self.log.info(f"Got 401 (forced probe) – measured TTL: {age_min:.1f} min.")
            await self._set(self.k_bestttl, age_sec)  # always accept probe (up or down)
            await self._set(self.k_armed, 0)
            left = await self._get(self.k_startleft)
            if left and int(left) > 0:
                try:
                    await self._set(self.k_startleft, int(left) - 1)
                except (ValueError, TypeError):
                    await self._set(self.k_startleft, 0)
        else:
            self.log.warning(f"Got 401 – token expired after {age_min:.1f} min (unplanned).")
            if adaptive_downshift:
                best = await self._get(self.k_bestttl)
                try:
                    if best and (age_sec + self.TTL_MARGIN_SEC) < 0.9 * float(best):
                        self.log.warning("Unexpected short TTL – recalibrating best known TTL (async).")
                        await self._set(self.k_bestttl, age_sec)
                except (ValueError, TypeError) as e:
                    self.log.debug(f"Recalibration check failed (async): {e}")

        return await self._do_refresh_async(now)
# -----------------------------------------------------------------------------------------


# Locks for policy sections
_TOKEN_POLICY_LOCK_SYNC = threading.Lock()
_token_policy_lock_async = None  # created lazily in async_nova_request()


# ------------------------ Small helpers to reduce duplication ------------------------
def _get_initial_token_sync(username, _logger):
    """Get or create the initial ADM token in sync path and ensure TTL metadata is recorded."""
    # Local import to keep external dependencies unchanged in module scope.
    from custom_components.googlefindmy.Auth.token_cache import get_cached_value, set_cached_value

    token = get_cached_value(f'adm_token_{username}')
    _logger.debug(f"ADM token for {username}: {'Found' if token else 'Not found'}")
    if not token:
        # Try alternative token name (kept for backward-compat)
        token = get_cached_value('adm_token')
        _logger.debug(f"Generic ADM token: {'Found' if token else 'Not found'}")

    if not token:
        _logger.info("Attempting to generate new ADM token...")
        token = get_adm_token(username)
        _logger.info(f"Generated ADM token: {'Success' if token else 'Failed'}")
        if token:
            set_cached_value(f"adm_token_issued_at_{username}", time.time())
            if not get_cached_value(f"adm_probe_startup_left_{username}"):
                set_cached_value(f"adm_probe_startup_left_{username}", 3)

    if not token:
        raise ValueError("No ADM token available - please reconfigure authentication")

    return token


async def _get_initial_token_async(username, _logger):
    """Get or create the initial ADM token in async path and ensure TTL metadata is recorded."""
    from custom_components.googlefindmy.Auth.token_cache import async_get_cached_value, async_set_cached_value

    token = await async_get_cached_value(f'adm_token_{username}')
    _logger.debug(f"ADM token for {username}: {'Found' if token else 'Not found'}")
    if not token:
        # Try alternative token name (kept for backward-compat)
        token = await async_get_cached_value('adm_token')
        _logger.debug(f"Generic ADM token: {'Found' if token else 'Not found'}")

    if not token:
        _logger.info("Attempting to generate new ADM token...")
        token = await asyncio.to_thread(get_adm_token, username)
        _logger.info(f"Generated ADM token: {'Success' if token else 'Failed'}")
        if token:
            await async_set_cached_value(f"adm_token_issued_at_{username}", time.time())
            if not await async_get_cached_value(f"adm_probe_startup_left_{username}"):
                await async_set_cached_value(f"adm_probe_startup_left_{username}", 3)

    if not token:
        raise ValueError("No ADM token available - please reconfigure authentication")

    return token
# -----------------------------------------------------------------------------------------


def nova_request(api_scope, hex_payload, session: aiohttp.ClientSession | None = None):
    """
    Synchronous Nova API request.

    NOTE:
    - Accepts an optional aiohttp.ClientSession for call-site compatibility, but **ignores** it here,
      because the sync path uses 'requests'. This prevents TypeErrors from callers that pass 'session='.
    - If you need true async & shared session, use 'async_nova_request(...)'.
    """
    url = "https://android.googleapis.com/nova/" + api_scope

    # Try to get ADM token from cache first, then generate if needed
    from custom_components.googlefindmy.Auth.token_cache import get_cached_value, set_cached_value, get_all_cached_values  # noqa: F401 (keep for debug listing)
    from custom_components.googlefindmy.Auth.username_provider import get_username
    import logging
    
    _logger = logging.getLogger(__name__)
    username = get_username()
    
    # Debug: Print all available cached values
    try:
        all_cached = get_all_cached_values()
        _logger.debug(f"Available cached tokens: {list(all_cached.keys())}")
    except Exception:
        pass
    _logger.debug(f"Username: {username}")
    
    # Initial token retrieval (centralized helper)
    android_device_manager_oauth_token = _get_initial_token_sync(username, _logger)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Authorization": "Bearer " + android_device_manager_oauth_token,
        "Accept-Language": "en-US",
        "User-Agent": "fmd/20006320; gzip"
    }

    payload = binascii.unhexlify(hex_payload)

    _logger.debug(f"Making Nova API request to: {url}")
    _logger.debug(f"Request headers: {headers}")
    _logger.debug(f"Payload length: {len(payload)} bytes")

    # --- Create and reuse a single policy instance per request ---
    policy = TTLPolicy(
        username=username,
        logger=_logger,
        get_value=get_cached_value,
        set_value=set_cached_value,
        refresh_fn=lambda: get_adm_token(username),
        set_auth_header_fn=lambda bearer: headers.__setitem__("Authorization", bearer),
    )

    # --- Pre-request TTL policy (sync) ---
    try:
        with _TOKEN_POLICY_LOCK_SYNC:
            policy.pre_request()
    except Exception as _policy_e:
        _logger.debug(f"Pre-request policy skipped: {_policy_e}")
    
    # Add retry logic for transient failures (401 path now re-enters the loop)
    max_retries = 3
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, data=payload, timeout=30)

            _logger.debug(f"Nova API response status: {response.status_code} (attempt {attempt + 1}/{max_retries})")
            _logger.debug(f"Response content length: {len(response.content)} bytes")

            if response.status_code == 200:
                result_hex = response.content.hex()
                _logger.debug(f"Nova API success - returning {len(result_hex)} characters of hex data")
                return result_hex
            elif response.status_code == 401:
                # Centralized 401 handling: refresh via policy and re-enter retry loop.
                try:
                    with _TOKEN_POLICY_LOCK_SYNC:
                        policy.on_401(adaptive_downshift=True)
                except Exception:
                    _logger.warning("Got 401 Unauthorized - ADM token likely expired, attempting recovery...")

                if attempt < max_retries - 1:
                    _logger.info("Token refreshed after 401, re-entering retry loop.")
                    continue
                else:
                    _logger.error("Token refreshed, but subsequent request is out of retries.")
                    raise RuntimeError("Failed to get a valid response even after token refresh.")
            elif response.status_code in [500, 502, 503, 504]:
                # Server errors - retry with exponential backoff
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    _logger.warning(f"Got {response.status_code} error, retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                    continue
                else:
                    _logger.error(f"Nova API failed with {response.status_code} after {max_retries} attempts")
            else:
                # Other client errors – surface error body for diagnostics
                soup = BeautifulSoup(response.text, 'html.parser')
                error_message = soup.get_text() if soup else response.text
                _logger.debug(f"Nova API failed: status={response.status_code}, error='{error_message[:200]}...'")
                raise RuntimeError(f"Nova API request failed with status {response.status_code}: {error_message}")
        except requests.Timeout:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                _logger.warning(f"Request timeout, retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            else:
                raise RuntimeError(f"Nova API request timed out after {max_retries} attempts")
        except requests.ConnectionError as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                _logger.warning(f"Connection error: {e}, retrying in {wait_time} seconds...")
                time.sleep(wait_time)
                continue
            else:
                raise RuntimeError(f"Connection error after {max_retries} attempts: {e}")

    # If we exit the loop without returning, surface a generic failure.
    raise RuntimeError("Nova API request failed: no successful attempt within retry budget")


async def async_nova_request(api_scope, hex_payload, username=None, session: aiohttp.ClientSession = None):
    """Async version of nova_request for Home Assistant compatibility. Reuses provided ClientSession if passed."""
    url = "https://android.googleapis.com/nova/" + api_scope

    # Try to get ADM token from cache first, then generate if needed
    from custom_components.googlefindmy.Auth.token_cache import async_get_cached_value, async_set_cached_value
    from custom_components.googlefindmy.Auth.username_provider import username_string
    import logging

    _logger = logging.getLogger(__name__)

    # Use provided username or get from cache; fail fast if not available.
    if not username:
        username = await async_get_cached_value(username_string)
    if not username:
        raise ValueError("Username is not available - cannot proceed with async_nova_request.")

    # Initial token retrieval (centralized helper)
    android_device_manager_oauth_token = await _get_initial_token_async(username, _logger)

    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Authorization": "Bearer " + android_device_manager_oauth_token,
        "Accept-Language": "en-US",
        "User-Agent": "fmd/20006320; gzip"
    }

    payload = binascii.unhexlify(hex_payload)

    _logger.debug(f"Making async Nova API request to: {url}")
    _logger.debug(f"Payload length: {len(payload)} bytes")

    # Lazily create async policy lock
    global _token_policy_lock_async
    if _token_policy_lock_async is None:
        _token_policy_lock_async = asyncio.Lock()

    # --- Create and reuse a single policy instance per request ---
    policy = AsyncTTLPolicy(
        username=username,
        logger=_logger,
        async_get_value=async_get_cached_value,
        async_set_value=async_set_cached_value,
        async_refresh_fn=lambda: asyncio.to_thread(get_adm_token, username),
        set_auth_header_fn=lambda bearer: headers.__setitem__("Authorization", bearer),
    )

    # --- Pre-request TTL policy (async) ---
    async with _token_policy_lock_async:
        try:
            await policy.pre_request()
        except Exception as _policy_e:
            _logger.debug(f"Pre-request policy (async) skipped due to: {_policy_e}")
    
    # Add retry logic for transient failures (401 path now re-enters the loop)
    max_retries = 3
    retry_delay = 1
    response = None
    content = None
    status = None

    # Prefer a provided session (integration-owned) to avoid per-call session overhead.
    ephemeral = False
    if session is None:
        _logger.warning("No aiohttp.ClientSession provided; creating a temporary session (suboptimal).")
        session = aiohttp.ClientSession()
        ephemeral = True
    try:
        for attempt in range(max_retries):
            try:
                async with session.post(url, headers=headers, data=payload, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    content = await response.read()
                    status = response.status

                    _logger.debug(f"Nova API response status: {status} (attempt {attempt + 1}/{max_retries})")
                    _logger.debug(f"Response content length: {len(content)} bytes")

                    if status == 200:
                        result_hex = content.hex()
                        _logger.debug(f"Nova API success - returning {len(result_hex)} characters of hex data")
                        return result_hex
                    elif status == 401:
                        # Centralized 401 handling: refresh via policy and re-enter retry loop.
                        async with _token_policy_lock_async:
                            try:
                                await policy.on_401(adaptive_downshift=True)
                            except Exception:
                                _logger.warning("Got 401 Unauthorized - ADM token likely expired, attempting recovery (async)...")

                        if attempt < max_retries - 1:
                            _logger.info("Token refreshed after 401, re-entering retry loop.")
                            continue
                        else:
                            _logger.error("Token refreshed, but subsequent request is out of retries.")
                            raise RuntimeError("Failed to get a valid response even after token refresh.")
                    elif status in [500, 502, 503, 504]:
                        # Server errors - retry with exponential backoff
                        if attempt < max_retries - 1:
                            wait_time = retry_delay * (2 ** attempt)
                            _logger.warning(f"Got {status} error, retrying in {wait_time} seconds...")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            _logger.error(f"Nova API failed with {status} after {max_retries} attempts")
                    else:
                        # Other client errors – surface error body for diagnostics
                        error_text = content.decode('utf-8', errors='ignore') if content else ""
                        soup = BeautifulSoup(error_text, 'html.parser')
                        error_message = soup.get_text() if soup else error_text
                        _logger.debug(f"Nova API failed: status={status}, error='{error_message[:200]}...'")
                        raise RuntimeError(f"Nova API request failed with status {status}: {error_message}")

            except asyncio.TimeoutError:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    _logger.warning(f"Request timeout, retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    raise RuntimeError(f"Nova API request timed out after {max_retries} attempts")
            except aiohttp.ClientError as e:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (2 ** attempt)
                    _logger.warning(f"Client error: {e}, retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    raise RuntimeError(f"Client error after {max_retries} attempts: {e}")

        # If we exit the loop without returning, surface a generic failure.
        raise RuntimeError("Nova API request failed: no successful attempt within retry budget")
    finally:
        if ephemeral:
            await session.close()
