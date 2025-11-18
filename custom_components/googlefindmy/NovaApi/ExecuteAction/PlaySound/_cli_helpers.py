# custom_components/googlefindmy/NovaApi/ExecuteAction/PlaySound/_cli_helpers.py
"""Shared helpers for Play Sound CLI entry points."""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib import import_module
from typing import Any, cast

from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA
from custom_components.googlefindmy.Auth.token_cache import TokenCache


def _resolve_receiver_provider() -> Callable[[], Any] | None:
    """Return the registered FCM receiver provider if available."""

    candidate_modules: list[Any] = []
    try:
        api_module = import_module("custom_components.googlefindmy.api")

        candidate_modules.append(api_module)
    except Exception:  # pragma: no cover - optional import
        candidate_modules.append(None)

    try:
        location_module = import_module(
            "custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.location_request"
        )

        candidate_modules.append(location_module)
    except Exception:  # pragma: no cover - optional import
        candidate_modules.append(None)

    for module in candidate_modules:
        if module is None:
            continue
        state = getattr(module, "_fcm_receiver_state", None)
        if isinstance(state, dict):
            getter = cast(Callable[[], Any] | None, state.get("getter"))
        else:
            getter = cast(
                Callable[[], Any] | None, getattr(module, "_FCM_ReceiverGetter", None)
            )
        if getter is not None and callable(getter):
            return getter
    return None


def _extract_token_from_receiver(receiver: Any, entry_id: str | None) -> str | None:
    """Return a token using the receiver's public accessor when available."""

    if receiver is None:
        return None

    token_candidate: Any
    try:
        token_candidate = (
            receiver.get_fcm_token(entry_id)
            if entry_id is not None
            else receiver.get_fcm_token()
        )
    except TypeError:
        try:
            token_candidate = receiver.get_fcm_token()
        except Exception:  # noqa: BLE001 - compatibility fallback
            return None
    except Exception:  # noqa: BLE001 - compatibility fallback
        return None

    if isinstance(token_candidate, str) and token_candidate:
        return token_candidate
    return None


async def _async_load_token_from_cache(
    cache: TokenCache, entry_id: str | None
) -> str | None:
    """Fallback token retrieval that mirrors FcmReceiverHA's cache behavior."""

    if entry_id is None:
        return None

    try:
        cached_value = await cache.async_get_cached_value("fcm_credentials")
    except Exception:  # pragma: no cover - defensive log noise avoided
        return None

    if isinstance(cached_value, str):
        try:
            cached_value = json.loads(cached_value)
        except json.JSONDecodeError:
            pass

    if not isinstance(cached_value, dict):
        return None

    creds: dict[str, Any] = cast(dict[str, Any], cached_value)
    receiver = FcmReceiverHA()
    receiver.creds[entry_id] = creds
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
