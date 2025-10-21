# custom_components/googlefindmy/Auth/token_retrieval.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

import gpsoauth

# Use the async-first API; the legacy sync wrapper is intentionally unsupported.
from custom_components.googlefindmy.Auth.aas_token_retrieval import async_get_aas_token
from custom_components.googlefindmy.Auth.token_cache import TokenCache
from custom_components.googlefindmy.exceptions import MissingTokenCacheError


_LOGGER = logging.getLogger(__name__)


class InvalidAasTokenError(RuntimeError):
    """Raised when the cached AAS token is rejected by gpsoauth."""


def _is_invalid_aas_error_text(text: str) -> bool:
    """Return True when the error string indicates an invalid AAS token."""

    lowered = text.lower()
    if "badauthentication" in lowered:
        return True
    if "needsbrowser" in lowered:
        return True
    if "unauthorized" in lowered or "forbidden" in lowered:
        return True
    if "invalid" in lowered:
        if "token" in lowered or "auth" in lowered or "credential" in lowered:
            return True
    return False


# Constants used by gpsoauth for the OAuth exchange flow.
# Keep aligned with other modules in this integration.
_ANDROID_ID: int = 0x38918A453D071993
_CLIENT_SIG: str = "38918a453d07199354f8b19af05ec6562ced5788"


def _extract_android_id_from_credentials(fcm_creds: Any) -> int | None:
    """Parse the android_id from an FCM credential bundle."""

    if not isinstance(fcm_creds, dict):
        return None

    gcm_block = fcm_creds.get("gcm")
    candidate: Any = None
    if isinstance(gcm_block, dict):
        candidate = gcm_block.get("android_id")

    if isinstance(candidate, int):
        return candidate
    if isinstance(candidate, str):
        try:
            return int(candidate, 0)
        except (TypeError, ValueError):
            _LOGGER.debug("android_id value from FCM credentials is not numeric")
            return None
    if candidate is not None:
        _LOGGER.debug("Unsupported android_id type in FCM credentials: %s", type(candidate))
    return None


async def _resolve_android_id(*, cache: TokenCache) -> int:
    """Resolve the android_id tied to the provided cache, with fallback."""

    try:
        fcm_creds = await cache.get("fcm_credentials")
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Failed to read FCM credentials from cache: %s", err)
        return _ANDROID_ID

    android_id = _extract_android_id_from_credentials(fcm_creds)
    if android_id is None:
        _LOGGER.warning(
            "FCM credentials missing android_id; falling back to static identifier. "
            "Generate fresh secrets.json if authentication fails."
        )
        return _ANDROID_ID

    return android_id


def _perform_oauth_sync(
    username: str,
    aas_token: str,
    scope: str,
    play_services: bool,
    *,
    android_id: int = _ANDROID_ID,
) -> str:
    """Blocking gpsoauth.perform_oauth call, factored for reuse.

    Args:
        username: Google account email (for request context).
        aas_token: AAS token to authorize the OAuth scope exchange.
        scope: OAuth scope suffix (e.g., "android_device_manager").
        play_services: If True, use the Play Services app id; else ADM app id.
        android_id: Device-specific Android ID used for the OAuth exchange.

    Returns:
        The OAuth access token (string) for the requested scope.

    Raises:
        InvalidAasTokenError: If gpsoauth explicitly rejects the supplied AAS token.
        RuntimeError: If the gpsoauth exchange fails or returns an invalid response.
    """
    request_app = "com.google.android.gms" if play_services else "com.google.android.apps.adm"
    try:
        auth_response = gpsoauth.perform_oauth(
            username,
            aas_token,
            android_id,
            service="oauth2:https://www.googleapis.com/auth/" + scope,
            app=request_app,
            client_sig=_CLIENT_SIG,
        )
        if not auth_response:
            raise ValueError("No response from gpsoauth.perform_oauth")

        token_value = auth_response.get("Token")
        if not isinstance(token_value, str) or not token_value:
            legacy_value = auth_response.get("Auth")
            if isinstance(legacy_value, str) and legacy_value:
                token_value = legacy_value

        if isinstance(token_value, str) and token_value:
            return token_value

        error_detail = str(auth_response.get("Error", "")).strip()
        if error_detail and _is_invalid_aas_error_text(error_detail):
            raise InvalidAasTokenError(
                f"gpsoauth rejected the AAS token while requesting scope '{scope}': {error_detail}"
            )
        raise KeyError("Neither 'Token' nor 'Auth' found in gpsoauth response")
    except InvalidAasTokenError:
        raise
    except Exception as e:  # noqa: BLE001
        message = str(e)
        if message and _is_invalid_aas_error_text(message):
            raise InvalidAasTokenError(
                f"gpsoauth rejected the AAS token while requesting scope '{scope}': {message}"
            ) from e
        raise RuntimeError(f"Failed to get auth token for scope '{scope}': {e}") from e


def request_token(
    username: str,
    scope: str,
    play_services: bool = False,
    *,
    aas_token: Optional[str] = None,
    cache: TokenCache | None = None,
) -> str:
    """Synchronous token request via gpsoauth (CLI/tests only).

    IMPORTANT:
    - This function is blocking. If you are inside Home Assistant (or any running
      event loop), **do not** call this function directly — use
      `await async_request_token(...)` or run this function in an executor thread.
    - You may inject an `aas_token` to avoid touching any global/async cache (e.g., for
      entry-scoped tests or isolated tooling).

    Args:
        username: Google account email.
        scope: OAuth scope suffix.
        play_services: Use the Play Services app id instead of ADM when True.
        aas_token: Optional AAS token to shortcut async cache lookup.
        cache: Entry-scoped TokenCache instance used for resolving AAS tokens when
            `aas_token` is not provided. Falls back to the default cache in
            single-entry CLI/test scenarios.

    Raises:
        RuntimeError: if called while an event loop is running.
    """
    # Guard against misuse in a running event loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # No running loop in this thread → safe to continue.
        pass
    else:
        # A loop exists and is running → disallow direct sync call.
        raise RuntimeError(
            "request_token() was called while an event loop is running. "
            "Use `await async_request_token(...)` in async contexts."
        )

    # Resolve the TokenCache (required for multi-account isolation).
    if cache is None:
        raise MissingTokenCacheError()

    # Resolve the AAS token: prefer injected token; otherwise use async provider in an isolated loop.
    if aas_token is None:
        aas_token = asyncio.run(async_get_aas_token(cache=cache))

    android_id = asyncio.run(_resolve_android_id(cache=cache))

    # Perform the blocking OAuth exchange.
    return _perform_oauth_sync(
        username,
        aas_token,
        scope,
        play_services,
        android_id=android_id,
    )


async def async_request_token(
    username: str,
    scope: str,
    play_services: bool = False,
    *,
    cache: TokenCache,
    aas_provider: Optional[Callable[[], Awaitable[str]]] = None,
    aas_token: Optional[str] = None,
) -> str:
    """Async wrapper for the OAuth token request (HA-safe).

    Behavior:
    - Uses an injected `aas_token` if provided; otherwise awaits the supplied
      `aas_provider()`; otherwise falls back to the default `async_get_aas_token()`.
    - Runs the blocking `gpsoauth.perform_oauth` in a thread pool to keep the
      event loop responsive.
    - This arrangement allows entry-scoped token resolution (e.g., via
      `entry.runtime_data.get_aas_token`) without global singletons.

    Args:
        username: Google account email.
        scope: OAuth scope suffix.
        play_services: Use the Play Services app id instead of ADM when True.
        cache: Entry-scoped TokenCache to read/write intermediate credentials.
        aas_provider: Optional async callable that returns an AAS token.
        aas_token: Optional pre-fetched AAS token (takes precedence over provider).

    Returns:
        OAuth access token string for the requested scope.

    Raises:
        InvalidAasTokenError: If gpsoauth rejects the cached AAS token during the exchange.
    """
    # Get the AAS token from injected token → injected provider → default provider.
    if cache is None:
        raise MissingTokenCacheError()

    if aas_token is None:
        if aas_provider is not None:
            aas_token = await aas_provider()
        else:
            async def _default_aas_provider() -> str:
                return await async_get_aas_token(cache=cache)

            aas_provider = _default_aas_provider
            aas_token = await aas_provider()

    # Offload the blocking OAuth exchange to a worker thread.
    loop = asyncio.get_running_loop()
    android_id = await _resolve_android_id(cache=cache)

    try:
        return await loop.run_in_executor(
            None,
            lambda: _perform_oauth_sync(
                username,
                aas_token,
                scope,
                play_services,
                android_id=android_id,
            ),
        )
    except InvalidAasTokenError:
        _LOGGER.warning(
            "gpsoauth rejected the cached AAS token while requesting scope '%s'; a fresh AAS token will be required.",
            scope,
        )
        raise
