# custom_components/googlefindmy/Auth/token_retrieval.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import asyncio
from typing import Optional

import gpsoauth

# Use the async-first API; the legacy sync wrapper is intentionally unsupported.
from custom_components.googlefindmy.Auth.aas_token_retrieval import async_get_aas_token


# Constants used by gpsoauth for the OAuth exchange flow.
# Keep aligned with other modules in this integration.
_ANDROID_ID: int = 0x38918A453D071993
_CLIENT_SIG: str = "38918a453d07199354f8b19af05ec6562ced5788"


def _perform_oauth_sync(username: str, aas_token: str, scope: str, play_services: bool) -> str:
    """Blocking gpsoauth.perform_oauth call, factored for reuse.

    Args:
        username: Google account email (for request context).
        aas_token: AAS token to authorize the OAuth scope exchange.
        scope: OAuth scope suffix (e.g., "android_device_manager").
        play_services: If True, use the Play Services app id; else ADM app id.

    Returns:
        The OAuth access token (string) for the requested scope.

    Raises:
        RuntimeError: If the gpsoauth exchange fails or returns an invalid response.
    """
    request_app = "com.google.android.gms" if play_services else "com.google.android.apps.adm"
    try:
        auth_response = gpsoauth.perform_oauth(
            username,
            aas_token,
            _ANDROID_ID,
            service="oauth2:https://www.googleapis.com/auth/" + scope,
            app=request_app,
            client_sig=_CLIENT_SIG,
        )
        if not auth_response:
            raise ValueError("No response from gpsoauth.perform_oauth")

        if "Auth" not in auth_response:
            raise KeyError(f"'Auth' not found in response: {auth_response}")

        return auth_response["Auth"]
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to get auth token for scope '{scope}': {e}") from e


def request_token(username: str, scope: str, play_services: bool = False) -> str:
    """Synchronous token request via gpsoauth (CLI/tests only).

    IMPORTANT:
    - This resolves the AAS token via the **async** cache API using a dedicated event loop.
      If you are inside Home Assistant (or any running event loop), **do not** call this
      function directly — use `await async_request_token(...)` or run `request_token`
      from an executor thread.
    """
    # Detect misuse in an async context and fail fast with a clear message.
    try:
        asyncio.get_running_loop()
        # If we got here, a loop is running in the current thread.
        raise RuntimeError(
            "request_token() was called while an event loop is running. "
            "Use `await async_request_token(...)` in async contexts."
        )
    except RuntimeError:
        # No running loop in this thread → safe to create a temporary one.
        pass

    # Resolve the AAS token with the async-first helper in an isolated event loop.
    aas_token = asyncio.run(async_get_aas_token())

    # Perform the blocking OAuth exchange.
    return _perform_oauth_sync(username, aas_token, scope, play_services)


async def async_request_token(username: str, scope: str, play_services: bool = False) -> str:
    """Async wrapper for the OAuth token request (HA-safe).

    Behavior:
    - Awaits the entry-scoped AAS token via `async_get_aas_token()` (non-blocking).
    - Runs the blocking `gpsoauth.perform_oauth` in a thread pool to keep the event loop responsive.
    """
    # Get the AAS token from the async cache/provider.
    aas_token = await async_get_aas_token()

    # Offload the blocking OAuth exchange to a worker thread.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: _perform_oauth_sync(username, aas_token, scope, play_services)
    )
