# gpsoauth.pyi
#
#  GoogleFindMyTools - A set of tools to interact with the Google Find My API
#  Copyright © 2024 Leon Böttger. All rights reserved.
#
# gpsoauth.pyi
#
"""Type stubs for the third-party :mod:`gpsoauth` library used for token exchange."""

from __future__ import annotations

from types import ModuleType
from typing import Any


class AuthError(Exception):
    """Raised when the gpsoauth library signals an authentication failure."""


class _ExceptionsModule(ModuleType):
    """Typed representation of :mod:`gpsoauth.exceptions`."""

    AuthError: type[AuthError]


exceptions: _ExceptionsModule


def exchange_token(
    email: str,
    master_token: str,
    android_id: int,
    service: str = ...,
    app: str = ...,
    client_sig: str = ...,
    locale: str | None = ...,
    **kwargs: Any,
) -> dict[str, Any]:
    """Exchange an OAuth token for an Android AuthSub (AAS) token."""


def perform_oauth(
    email: str,
    aas_token: str,
    android_id: int,
    service: str,
    app: str = ...,
    client_sig: str = ...,
    locale: str | None = ...,
    **kwargs: Any,
) -> dict[str, Any]:
    """Request an OAuth token for the provided scope using gpsoauth."""


def perform_master_login(
    email: str,
    password: str,
    android_id: int,
    service: str = ...,
    app: str = ...,
    client_sig: str = ...,
    locale: str | None = ...,
    **kwargs: Any,
) -> dict[str, Any]:
    """Perform the legacy master login flow using gpsoauth."""
