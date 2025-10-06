# custom_components/googlefindmy/Auth/username_provider.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
from __future__ import annotations

import logging
from custom_components.googlefindmy.Auth.token_cache import get_cached_value, set_cached_value

_LOGGER = logging.getLogger(__name__)

# Single well-known cache key for the Google account e-mail
username_string = 'username'


def get_username() -> str:
    """
    Return the configured Google account e-mail from cache.

    IMPORTANT:
    - Callers that already *know* the username should avoid relying on this and
      pass the username explicitly (DI). This function is a legacy convenience.
    """
    username = get_cached_value(username_string)
    if username is not None:
        return username

    # Fail fast instead of using a placeholder that would cause PERMISSION_DENIED later.
    _LOGGER.error(
        "No Google username configured in cache key '%s'. Please configure the account in the UI.",
        username_string,
    )
    raise RuntimeError(
        "Google username is not configured. Open the integration UI and set the account."
    )


def set_username(username: str) -> None:
    """
    Explicitly seed/update the username cache.

    Rationale:
    During cold starts, higher layers may know the username before the UI/options
    flow persists it. Exposing a setter allows early seeding and avoids races.
    """
    if not isinstance(username, str) or not username:
        raise ValueError("Username must be a non-empty string.")
    set_cached_value(username_string, username)


if __name__ == '__main__':
    # Simple self-check: seed from env or leave as-is, then read.
    # (Kept minimal; real flows seed via API init or token retrieval paths.)
    print(get_username())
