# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/_cli_helpers.py
"""Shared helpers for Play Sound CLI entry points."""

from __future__ import annotations

import json
from typing import Any
from collections.abc import Callable

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.Auth.token_cache import TokenCache


def _resolve_receiver_provider() -> Callable[[], Any] | None:
    """Return the registered FCM receiver provider if available."""

    candidate_modules: list[Any] = []
    try:
        from custom_components.googlefindmy import api as api_module  # noqa: PLC0415

        candidate_modules.append(api_module)
    except Exception:  # pragma: no cover - optional import
        candidate_modules.append(None)

    try:
        from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker import (
            location_request as location_module,
        )  # noqa: PLC0415

        candidate_modules.append(location_module)
    except Exception:  # pragma: no cover - optional import
        candidate_modules.append(None)

    for module in candidate_modules:
        if module is None:
            continue
        getter = getattr(module, "_FCM_ReceiverGetter", None)
        if callable(getter):
            return getter
    return None


def _extract_token_from_receiver(receiver: Any, entry_id: str | None) -> str | None:
    """Return a token using the receiver's public accessor when available."""

    if receiver is None:
        return None

    try:
        if entry_id is not None:
            token = receiver.get_fcm_token(entry_id)
        else:
            token = receiver.get_fcm_token()
    except TypeError:
        try:
            token = receiver.get_fcm_token()
        except Exception:  # noqa: BLE001 - compatibility fallback
            return None
    except Exception:  # noqa: BLE001 - compatibility fallback
        return None

    if isinstance(token, str) and token:
        return token
    return None


async def _async_load_token_from_cache(
    cache: TokenCache, entry_id: str | None
) -> str | None:
    """Fallback token retrieval that mirrors FcmReceiverHA's cache behavior."""

    if entry_id is None:
        return None

    try:
        creds: Any = await cache.async_get_cached_value("fcm_credentials")
    except Exception:  # pragma: no cover - defensive log noise avoided
        return None

    if isinstance(creds, str):
        try:
            creds = json.loads(creds)
        except json.JSONDecodeError:
            pass

    if not isinstance(creds, dict):
        return None

    receiver = FcmReceiverHA()
    receiver.creds[entry_id] = creds  # type: ignore[assignment]
    return receiver.get_fcm_token(entry_id)


async def async_fetch_cli_fcm_token(
    cache: TokenCache, entry_id: str | None
) -> str | None:
    """Attempt to fetch an FCM token using the shared receiver or cache."""

    provider = _resolve_receiver_provider()
    if provider is not None:
        try:
            receiver = provider()
        except Exception:  # noqa: BLE001 - provider failures fall back to cache
            receiver = None
        token = _extract_token_from_receiver(receiver, entry_id)
        if token:
            return token

    return await _async_load_token_from_cache(cache, entry_id)


__all__ = ["async_fetch_cli_fcm_token"]
