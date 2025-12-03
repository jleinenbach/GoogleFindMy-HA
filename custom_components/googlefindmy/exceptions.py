# custom_components/googlefindmy/exceptions.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
"""Custom exception types with translated fallbacks for Google Find My."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.exceptions import HomeAssistantError as HassHomeAssistantError

from .const import DOMAIN

_MISSING_CACHE = (
    "Token cache missing. Provide the entry-specific TokenCache (for example, "
    "entry.runtime_data.token_cache)."
)

_MISSING_NAMESPACE = (
    "Namespace missing. Pass the entry-specific namespace or ConfigEntry ID "
    "to keep cached metadata isolated."
)


if TYPE_CHECKING:

    class HomeAssistantError(Exception):
        """Type checker placeholder matching the Home Assistant error base."""

        ...

else:
    HomeAssistantError = HassHomeAssistantError


class MissingTokenCacheError(HomeAssistantError):
    """Raised when a TokenCache is required but not provided."""

    def __init__(self) -> None:
        super().__init__(_MISSING_CACHE)
        self.translation_domain = DOMAIN
        self.translation_key = "missing_token_cache"


class MissingNamespaceError(HomeAssistantError):
    """Raised when a namespace/entry ID is required but missing."""

    def __init__(self) -> None:
        super().__init__(_MISSING_NAMESPACE)
        self.translation_domain = DOMAIN
        self.translation_key = "missing_namespace"


class FatalRegistrationError(HomeAssistantError):
    """Raised when FCM registration fails with a fatal status code."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
