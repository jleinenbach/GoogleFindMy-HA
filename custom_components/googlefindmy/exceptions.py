# custom_components/googlefindmy/exceptions.py
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
"""Custom exception types with translated fallbacks for Google Find My."""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN

_MISSING_CACHE_EN = (
    "Token cache missing. Provide the entry-specific TokenCache (for example, "
    "entry.runtime_data.token_cache)."
)
_MISSING_CACHE_DE = (
    "Token-Cache fehlt. Bitte den kontospezifischen TokenCache übergeben (zum "
    "Beispiel entry.runtime_data.token_cache)."
)

_MISSING_NAMESPACE_EN = (
    "Namespace missing. Pass the entry-specific namespace or ConfigEntry ID "
    "to keep cached metadata isolated."
)
_MISSING_NAMESPACE_DE = (
    "Namespace fehlt. Bitte den eintragsbezogenen Namespace bzw. die ConfigEntry-"
    "ID übergeben, um Metadaten sauber zu isolieren."
)


class MissingTokenCacheError(HomeAssistantError):
    """Raised when a TokenCache is required but not provided."""

    def __init__(self) -> None:
        super().__init__(f"{_MISSING_CACHE_EN} {_MISSING_CACHE_DE}")
        self.translation_domain = DOMAIN
        self.translation_key = "missing_token_cache"


class MissingNamespaceError(HomeAssistantError):
    """Raised when a namespace/entry ID is required but missing."""

    def __init__(self) -> None:
        super().__init__(f"{_MISSING_NAMESPACE_EN} {_MISSING_NAMESPACE_DE}")
        self.translation_domain = DOMAIN
        self.translation_key = "missing_namespace"
