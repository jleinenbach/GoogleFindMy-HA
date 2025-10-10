#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#

from __future__ import annotations

import asyncio
from typing import Optional

import gpsoauth

from custom_components.googlefindmy.Auth.aas_token_retrieval import get_aas_token


def request_token(username: str, scope: str, play_services: bool = False) -> str:
    """Synchronous token request via gpsoauth (CLI/tests).

    WARNING: This is blocking. In Home Assistant use `await async_request_token(...)`
    or call this from an executor thread.
    """
    aas_token = get_aas_token()  # sync path (may block)

    # Use a hardcoded android_id instead of FcmReceiver to avoid ChromeDriver
    # Android ID should be a large integer (16 hex digits)
    android_id = 0x38918A453D071993
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
        if not auth_response:
            raise ValueError("No response from gpsoauth.perform_oauth")

        if "Auth" not in auth_response:
            raise KeyError(f"'Auth' not found in response: {auth_response}")

        token = auth_response["Auth"]
        return token
    except Exception as e:
        raise RuntimeError(f"Failed to get auth token for scope '{scope}': {e}") from e


async def async_request_token(username: str, scope: str, play_services: bool = False) -> str:
    """Async wrapper for token request (HA-safe).

    Runs the blocking gpsoauth flow in a thread pool to avoid blocking the event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: request_token(username, scope, play_services))
