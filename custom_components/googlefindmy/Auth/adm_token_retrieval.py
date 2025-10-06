#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import logging
import time
from typing import Optional

from custom_components.googlefindmy.Auth.token_retrieval import request_token
from custom_components.googlefindmy.Auth.username_provider import (
    get_username,
    username_string,
)
from custom_components.googlefindmy.Auth.token_cache import (
    get_cached_value,
    set_cached_value,
)

_LOGGER = logging.getLogger(__name__)


def _seed_username_in_cache(username: str) -> None:
    """
    Ensure the global username cache key is filled before calling lower layers.

    Rationale:
    Some token helper(s) still read the username from global cache for legacy reasons.
    Seeding here avoids cold-start races when callers pass the username explicitly.
    """
    try:
        cached = get_cached_value(username_string)
        if cached != username and isinstance(username, str) and username:
            set_cached_value(username_string, username)
            _LOGGER.debug("Seeded username cache key '%s' for user '%s'.", username_string, username)
    except Exception as e:  # defensive: never fail token flow on seeding
        _LOGGER.debug("Username cache seeding skipped: %s", e)


def _generate_adm_token(username: str) -> str:
    """
    Generate a new ADM token for the given user.

    NOTE:
    - We seed the global username cache first to keep legacy internals happy.
    - request_token(...) remains the single source for token acquisition.
    """
    _seed_username_in_cache(username)
    return request_token(username, "android_device_manager")


def get_adm_token(username: Optional[str] = None, *, retries: int = 2, backoff: float = 1.0) -> str:
    """
    Get (or create) an ADM token for a user. Cold-start robust.

    Behavior:
    - If username is provided -> use it and seed the username cache proactively.
    - If not provided -> fall back to username_provider.get_username().
    - Try cache first; on miss, try to generate with short exponential backoff.
    - On success, also stamp 'adm_token_issued_at_<user>' to aid TTL policy.

    Raises:
        Exception from the underlying token flow if all attempts fail.
    """
    # Resolve username
    user = username or get_username()
    if not isinstance(user, str) or not user:
        raise RuntimeError("Username is empty/invalid; cannot retrieve ADM token.")

    cache_key = f"adm_token_{user}"

    # 1) Fast path: cache hit
    token = get_cached_value(cache_key)
    if token:
        return token

    # 2) Miss -> bounded retries with backoff
    last_exc: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            tok = _generate_adm_token(user)
            if not tok:
                raise RuntimeError("request_token() returned empty result")

            # Persist token & issued-at metadata for TTL policy users
            set_cached_value(cache_key, tok)
            set_cached_value(f"adm_token_issued_at_{user}", time.time())
            # Bootstrap probe counter for TTL calibration on fresh installs (best-effort)
            if get_cached_value(f"adm_probe_startup_left_{user}") is None:
                set_cached_value(f"adm_probe_startup_left_{user}", 3)

            return tok
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if attempt < retries:
                sleep_s = backoff * (2 ** attempt)
                _LOGGER.info(
                    "ADM token generation failed (attempt %s/%s): %s — retrying in %.1fs",
                    attempt + 1, retries + 1, e, sleep_s
                )
                time.sleep(sleep_s)
                continue
            _LOGGER.error("ADM token generation failed after %s attempts: %s", retries + 1, e)

    # If we reach here, all attempts failed
    assert last_exc is not None
    raise last_exc


if __name__ == '__main__':
    print(get_adm_token(get_username()))
