# custom_components/googlefindmy/Auth/token_refresh.py
"""Token refresh utilities for manual regeneration of AAS and ADM tokens.

This module provides entry-scoped token regeneration with:
- Shared cooldown (3 minutes) across all token refresh operations
- Proper dependency handling (ADM depends on AAS)
- Info-level logging for visibility of regeneration operations

Token dependency chain:
    OAuth Token -> AAS Token -> ADM Token

When regenerating:
- AAS Token: Invalidates both AAS and ADM, regenerates AAS (ADM will be
  regenerated on next API call)
- ADM Token: Invalidates only ADM, regenerates ADM (uses existing AAS if valid,
  otherwise triggers AAS regeneration)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from ..const import DATA_AAS_TOKEN, TOKEN_REFRESH_COOLDOWN_S

if TYPE_CHECKING:
    from .token_cache import TokenCache

_LOGGER = logging.getLogger(__name__)

# Global cooldown tracker: maps entry_id to last refresh timestamp
_last_refresh_timestamps: dict[str, float] = {}
_refresh_lock = asyncio.Lock()


def _mask_email(email: str | None) -> str:
    """Return a privacy-friendly representation of an email for logs."""
    if not email or "@" not in email:
        return "<unknown>"
    local, domain = email.split("@", 1)
    if not local:
        return f"*@{domain}"
    masked_local = (local[0] + "***") if len(local) > 1 else "*"
    return f"{masked_local}@{domain}"


async def _get_entry_id(cache: TokenCache) -> str:
    """Extract entry_id from the cache for cooldown tracking."""
    # The cache typically has an entry_id attribute or we derive it from namespace
    entry_id = getattr(cache, "entry_id", None)
    if isinstance(entry_id, str) and entry_id:
        return entry_id
    namespace = getattr(cache, "_namespace", None)
    if isinstance(namespace, str) and namespace:
        return namespace
    return "default"


def is_refresh_on_cooldown(entry_id: str) -> tuple[bool, float]:
    """Check if token refresh is on cooldown for the given entry.

    Returns:
        Tuple of (is_on_cooldown, remaining_seconds)
    """
    last_refresh = _last_refresh_timestamps.get(entry_id, 0.0)
    elapsed = time.time() - last_refresh
    remaining = TOKEN_REFRESH_COOLDOWN_S - elapsed
    return (remaining > 0, max(0.0, remaining))


def get_cooldown_remaining(entry_id: str) -> float:
    """Get remaining cooldown time in seconds for the given entry."""
    _, remaining = is_refresh_on_cooldown(entry_id)
    return remaining


def _record_refresh(entry_id: str) -> None:
    """Record that a token refresh was performed for cooldown tracking."""
    _last_refresh_timestamps[entry_id] = time.time()


async def async_regenerate_aas_token(
    *,
    cache: TokenCache,
    username: str | None = None,
) -> bool:
    """Regenerate the AAS token, invalidating both AAS and ADM tokens.

    Since ADM depends on AAS, invalidating AAS requires ADM to be regenerated
    as well. This function:
    1. Checks and enforces the shared cooldown
    2. Invalidates both AAS and ADM tokens in the cache
    3. Triggers a fresh AAS token generation

    Args:
        cache: Entry-scoped TokenCache instance
        username: Optional username; resolved from cache if not provided

    Returns:
        True if regeneration succeeded, False otherwise

    Logs:
        INFO level for button press and success/failure
    """
    from .aas_token_retrieval import async_get_aas_token
    from .username_provider import async_get_username

    entry_id = await _get_entry_id(cache)

    async with _refresh_lock:
        on_cooldown, remaining = is_refresh_on_cooldown(entry_id)
        if on_cooldown:
            _LOGGER.info(
                "AAS token regeneration blocked: cooldown active (%.1fs remaining)",
                remaining,
            )
            return False

        # Resolve username for logging
        user = username
        if not user:
            user = await async_get_username(cache=cache)
        masked_user = _mask_email(user)

        _LOGGER.info(
            "AAS token regeneration requested for %s (entry: %s)",
            masked_user,
            entry_id,
        )

        try:
            # Invalidate AAS token
            await cache.set(DATA_AAS_TOKEN, None)
            _LOGGER.info("Invalidated AAS token for %s", masked_user)

            # Invalidate ADM token (depends on AAS)
            if user:
                adm_key = f"adm_token_{user.strip().lower()}"
                await cache.set(adm_key, None)
                _LOGGER.info(
                    "Invalidated ADM token for %s (AAS dependency)", masked_user
                )

            # Trigger AAS token regeneration
            # The existing function handles retries and backoff internally
            new_token = await async_get_aas_token(cache=cache)

            if new_token:
                _record_refresh(entry_id)
                _LOGGER.info(
                    "AAS token regeneration successful for %s",
                    masked_user,
                )
                return True

            _LOGGER.warning(
                "AAS token regeneration returned empty token for %s",
                masked_user,
            )
            return False

        except Exception as err:
            _LOGGER.error(
                "AAS token regeneration failed for %s: %s",
                masked_user,
                err,
            )
            return False


async def async_regenerate_adm_token(
    *,
    cache: TokenCache,
    username: str | None = None,
) -> bool:
    """Regenerate the ADM token.

    This function:
    1. Checks and enforces the shared cooldown
    2. Invalidates the ADM token in the cache
    3. Triggers a fresh ADM token generation (which may use existing AAS
       or regenerate it if needed)

    Args:
        cache: Entry-scoped TokenCache instance
        username: Optional username; resolved from cache if not provided

    Returns:
        True if regeneration succeeded, False otherwise

    Logs:
        INFO level for button press and success/failure
    """
    from .adm_token_retrieval import async_get_adm_token
    from .username_provider import async_get_username

    entry_id = await _get_entry_id(cache)

    async with _refresh_lock:
        on_cooldown, remaining = is_refresh_on_cooldown(entry_id)
        if on_cooldown:
            _LOGGER.info(
                "ADM token regeneration blocked: cooldown active (%.1fs remaining)",
                remaining,
            )
            return False

        # Resolve username for logging and cache key
        user = username
        if not user:
            user = await async_get_username(cache=cache)
        masked_user = _mask_email(user)

        if not user:
            _LOGGER.error("ADM token regeneration failed: no username available")
            return False

        _LOGGER.info(
            "ADM token regeneration requested for %s (entry: %s)",
            masked_user,
            entry_id,
        )

        try:
            # Invalidate ADM token only
            adm_key = f"adm_token_{user.strip().lower()}"
            await cache.set(adm_key, None)
            _LOGGER.info("Invalidated ADM token for %s", masked_user)

            # Trigger ADM token regeneration
            # This will use existing AAS token or trigger AAS regeneration if needed
            new_token = await async_get_adm_token(username=user, cache=cache)

            if new_token:
                _record_refresh(entry_id)
                _LOGGER.info(
                    "ADM token regeneration successful for %s",
                    masked_user,
                )
                return True

            _LOGGER.warning(
                "ADM token regeneration returned empty token for %s",
                masked_user,
            )
            return False

        except Exception as err:
            _LOGGER.error(
                "ADM token regeneration failed for %s: %s",
                masked_user,
                err,
            )
            return False


def clear_cooldown(entry_id: str) -> None:
    """Clear the cooldown for a specific entry (for testing purposes)."""
    _last_refresh_timestamps.pop(entry_id, None)


def clear_all_cooldowns() -> None:
    """Clear all cooldowns (for testing purposes)."""
    _last_refresh_timestamps.clear()
