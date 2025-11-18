# custom_components/googlefindmy/Auth/fcm_receiver.py
"""Backward-compatibility shim for legacy FCM receiver.

This module used to expose a standalone FCM stack via `FcmReceiver`. The integration
now runs a single, shared HA-managed receiver (`FcmReceiverHA`) that is acquired and
released in `__init__.py`. To avoid spawning a second stack (and to keep imports from
older code paths from breaking), this shim provides a minimal, non-invasive surface.

Notes
-----
- This shim does **not** start or own any FCM client.
- It reads already-persisted credentials (token cache) for `get_fcm_token()` /
  `get_android_id()`.
- `register_for_location_updates()` is accepted for compatibility but is a no-op; the
  new design routes device-scoped callbacks through the shared receiver only.
- `stop_listening()` is a no-op (the shared receiver lifecycle is owned by HA).

If you still call into this file directly, please migrate to the shared receiver:
`from custom_components.googlefindmy.Auth.fcm_receiver_ha import FcmReceiverHA`
"""

from __future__ import annotations

import json
import logging
import warnings
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

try:
    # Primary, explicit import path within the integration
    from custom_components.googlefindmy.Auth import token_cache as _token_cache
except Exception as err:  # noqa: BLE001 - defensive import for rare packaging layouts
    raise ImportError(
        "googlefindmy.Auth.fcm_receiver shim could not import token_cache; "
        "please ensure the integration is installed correctly."
    ) from err

get_cached_value = _token_cache.get_cached_value
set_cached_value = _token_cache.set_cached_value

if TYPE_CHECKING:
    from custom_components.googlefindmy.Auth.token_cache import TokenCache

_LOGGER = logging.getLogger(__name__)


class FcmReceiver:  # pragma: no cover - legacy surface kept for compatibility
    """Legacy class preserved as a thin adapter.

    Only supports token/ID access; start/stop and registration are no-ops to avoid
    interfering with the shared, HA-managed FCM lifecycle.
    """

    def __init__(
        self,
        *,
        entry_id: str | None = None,
        cache: TokenCache | None = None,
    ) -> None:
        warnings.warn(
            "custom_components.googlefindmy.Auth.fcm_receiver.FcmReceiver is deprecated "
            "and will be removed in the 2025.6 release. Import "
            "FcmReceiverHA via the shared provider instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Resolve the cache used for reads/writes in legacy contexts.
        self._cache: TokenCache | None = self._resolve_cache(
            entry_id=entry_id, cache=cache
        )
        # Cache a snapshot of persisted credentials to keep legacy callers functional.
        creds = self._read_cached_credentials()
        # Normalize common storage shapes (dict or JSON-serialized dict)
        if isinstance(creds, str):
            try:
                creds = json.loads(creds)
            except json.JSONDecodeError:
                # Keep raw string; accessors will handle missing structure gracefully.
                pass
        self._creds: Any = creds

    # ----------------------------
    # Legacy API (no-op / accessors)
    # ----------------------------
    def register_for_location_updates(self, callback: Callable[..., Any]) -> str | None:
        """Accept legacy registration but do not attach a callback.

        Rationale:
            The new architecture wires device-scoped callbacks through the shared
            FcmReceiverHA instance within Home Assistant. Here we simply return the
            current token (if any) and log a deprecation note.

        Returns:
            The FCM token if available (str), else None.
        """
        _LOGGER.debug(
            "Legacy FcmReceiver.register_for_location_updates() called. "
            "This is a no-op in the new architecture; please migrate to FcmReceiverHA."
        )
        return self.get_fcm_token()

    def get_fcm_token(self) -> str | None:
        """Return the current FCM token from the persisted credentials, if available."""
        creds = self._creds or self._read_cached_credentials()
        if isinstance(creds, str):
            # Late normalization if we were constructed before credentials were JSON.
            try:
                creds = json.loads(creds)
            except Exception:  # noqa: BLE001 - tolerate non-JSON values
                pass

        try:
            token = creds["fcm"]["registration"]["token"]
            if isinstance(token, str) and token:
                return token
        except Exception:  # noqa: BLE001 - tolerate missing keys/shape
            return None
        return None

    def stop_listening(self) -> None:
        """Legacy no-op.

        The shared FCM receiver is owned and stopped by the integration lifecycle
        (via `entry.async_on_unload` in `__init__.py`). Stopping from here could
        erroneously affect other config entries.
        """
        _LOGGER.debug(
            "Legacy FcmReceiver.stop_listening(): no-op in compatibility shim."
        )

    def get_android_id(self) -> str | None:
        """Return the Android ID from persisted credentials, if available."""
        creds = self._creds or self._read_cached_credentials()
        if isinstance(creds, str):
            try:
                creds = json.loads(creds)
            except Exception:  # noqa: BLE001
                pass

        try:
            aid = creds["gcm"]["android_id"]
            return str(aid) if aid is not None else None
        except Exception:  # noqa: BLE001
            return None

    # ----------------------------
    # Legacy setter passthrough (rare)
    # ----------------------------
    def _on_credentials_updated(self, creds: Any) -> None:
        """Legacy setter kept for callers that push new credentials.

        Stores to the shared token cache; does not touch any FCM client here.
        """
        try:
            if self._cache is not None:
                if creds is None:
                    self._cache._data.pop("fcm_credentials", None)
                else:
                    self._cache._data["fcm_credentials"] = creds
            else:
                set_cached_value("fcm_credentials", creds)
            self._creds = creds
            _LOGGER.debug("Legacy FcmReceiver: credentials snapshot updated via shim.")
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Legacy FcmReceiver: failed to persist credentials: %s", err)

    # ----------------------------
    # Internal helpers
    # ----------------------------
    @staticmethod
    def _resolve_cache(
        *, entry_id: str | None, cache: TokenCache | None
    ) -> TokenCache | None:
        """Return the TokenCache instance to use for legacy access."""

        if cache is not None:
            return cache

        if entry_id is not None:
            resolved = _token_cache._INSTANCES.get(entry_id)
            if resolved is not None:
                return resolved
            raise ValueError(
                "Legacy FcmReceiver shim received unknown entry_id; "
                "provide a registered entry_id or cache=..."
            )

        if not _token_cache._INSTANCES:
            return None

        entry_items = list(_token_cache._INSTANCES.items())
        if len(entry_items) > 1:
            raise ValueError(
                "Legacy FcmReceiver shim cannot auto-select between multiple TokenCache "
                "instances. Provide entry_id=... or cache=... to target the intended "
                "config entry."
            )

        return entry_items[0][1]

    def _read_cached_credentials(self) -> Any:
        """Return credentials from the selected cache without raising."""

        if self._cache is not None:
            return self._cache._data.get("fcm_credentials")
        return get_cached_value("fcm_credentials")
