#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

import asyncio
from typing import Dict, Any

import gpsoauth
from custom_components.googlefindmy.Auth.aas_token_retrieval import get_aas_token


def request_token(username: str, scope: str, play_services: bool = False) -> str:
    """Synchronous token request (legacy).
    WARNING: This function performs blocking I/O and MUST NOT be called from the HA event loop.
    Keep for backward compatibility in threads/executors only.
    """
    aas_token = get_aas_token()
    android_id = 0x38918A453D071993  # 16-hex digit Android ID
    request_app = "com.google.android.gms" if play_services else "com.google.android.apps.adm"

    try:
        auth_response = gpsoauth.perform_oauth(
            username,
            aas_token,
            android_id,
            service="oauth2:https://www.googleapis.com/auth/" + scope,
            app=request_app,
            client_sig="38918a453d07199354f8b19af05ec6562ced5788",
        )
        if not auth_response or "Auth" not in auth_response:
            raise RuntimeError(f"OAuth response missing 'Auth': {auth_response}")
        return auth_response["Auth"]
    except Exception as e:  # noqa: E722 kept generic to preserve upstream behavior
        raise RuntimeError(f"Failed to get auth token for scope '{scope}': {e}") from e


def _perform_oauth_sync(username: str, scope: str, play_services: bool) -> Dict[str, Any]:
    """Run the blocking gpsoauth.perform_oauth() synchronously (for threading)."""
    aas_token = get_aas_token()
    android_id = 0x38918A453D071993
    request_app = "com.google.android.gms" if play_services else "com.google.android.apps.adm"
    return gpsoauth.perform_oauth(
        username,
        aas_token,
        android_id,
        service="oauth2:https://www.googleapis.com/auth/" + scope,
        app=request_app,
        client_sig="38918a453d07199354f8b19af05ec6562ced5788",
    )


async def async_request_token(username: str, scope: str, play_services: bool = False) -> str:
    """Asynchronous wrapper that runs the blocking gpsoauth call in a worker thread.
    Safe to call from Home Assistant's event loop.
    """
    try:
        auth_response: Dict[str, Any] = await asyncio.to_thread(
            _perform_oauth_sync, username, scope, play_services
        )
        if not auth_response or "Auth" not in auth_response:
            raise RuntimeError(f"OAuth response missing 'Auth': {auth_response}")
        return auth_response["Auth"]
    except Exception as e:  # noqa: E722
        raise RuntimeError(f"Failed to get auth token for scope '{scope}': {e}") from e
