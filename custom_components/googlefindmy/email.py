# custom_components/googlefindmy/email.py
"""Shared helpers for normalizing account identifiers."""
from __future__ import annotations

from typing import Final

_CANONICAL_PREFIX: Final[str] = "acct:"


def normalize_email(raw: str | None) -> str | None:
    """Return canonical account identifier for duplicate detection.

    Normalization is intentionally conservative and limited to trimming surrounding
    whitespace and applying :meth:`str.casefold`. Provider specific alias handling
    (for example Gmail `+tag` stripping) is deliberately omitted so we do not
    conflate accounts managed by different Google Workspace tenants.
    """

    if not raw:
        return None
    normalized = raw.strip()
    if not normalized:
        return None
    return normalized.casefold()


def normalize_email_or_default(
    raw: str | None,
    *,
    fallback: str = "",
) -> str:
    """Return normalized email or the fallback when normalization fails."""

    normalized = normalize_email(raw)
    if normalized is None:
        return fallback
    return normalized


def unique_account_id(normalized_email: str | None) -> str | None:
    """Return the integration-wide unique identifier for an account.

    Args:
        normalized_email: Result from :func:`normalize_email`.

    Returns:
        Stable unique ID prefixed with ``acct:`` to avoid collisions with legacy
        identifiers. ``None`` when ``normalized_email`` is falsy.
    """

    if not normalized_email:
        return None
    return f"{_CANONICAL_PREFIX}{normalized_email}"
