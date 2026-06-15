# tests/helpers/fcm_handle_stub.py
# Mirror production attribute names read by FcmPushClient._handle_data_message
"""Additive composition stub for ``FcmPushClient._handle_data_message`` tests.

Binds the production unbound ``_handle_data_message`` (plus the two helpers it
calls, ``_app_data_by_key`` and the static ``_extract_header_param``) to a
lightweight container that mirrors only the attributes that method reads. This
exercises the real branch logic without the heavy production constructor --
the same composition discipline as ``fcm_poison_stub.FcmPushClientSlim``.

Why a *new* builder instead of reusing ``FcmPushClientSlim``: that stub mocks
``_handle_message`` away (``AsyncMock``) and therefore never runs
``_handle_data_message``. To cover the 8 branch groups of that method for real
(plan R2-R8) an additive builder is required; the poison stub is left
untouched.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    FcmPushClient,
)

# Sentinel distinguishing "caller did not pass credentials" (use the valid
# default) from an explicit ``None``/``{}`` (a real production state the
# missing-credentials guard must handle).
_CREDENTIALS_UNSET = object()


def make_data_message(
    *,
    persistent_id: str = "pid-1",
    crypto_key: str = "dh=BPK_value",
    encryption: str = "salt=SALT_value",
    subtype: str = "APPID",
    message_type: str | None = None,
    raw_data: bytes = b"raw-body",
) -> SimpleNamespace:
    """Build a ``DataMessageStanza``-shaped namespace.

    ``_app_data_by_key`` reads only ``x.key``/``x.value``; ``_handle_data_message``
    reads only ``stream_id``/``last_stream_id_received``/``status``/``raw_data``/
    ``persistent_id``. A ``SimpleNamespace`` is therefore structurally sufficient
    (duck typing), matching ``fcm_poison_stub.make_poison_data_message`` and the
    explicit ``SimpleNamespace`` fallback of plan AE4.

    Set ``message_type="deleted_messages"`` to exercise the early-return branch.
    """
    app_data = [
        SimpleNamespace(key="crypto-key", value=crypto_key),
        SimpleNamespace(key="encryption", value=encryption),
        SimpleNamespace(key="subtype", value=subtype),
    ]
    if message_type is not None:
        app_data.insert(
            0, SimpleNamespace(key="message_type", value=message_type)
        )
    return SimpleNamespace(
        app_data=app_data,
        persistent_id=persistent_id,
        raw_data=raw_data,
        stream_id=1,
        last_stream_id_received=0,
        status="ok",
    )


class FcmHandleSlim:
    """Composition stub binding the real ``_handle_data_message``.

    Mirrors the production attributes the method reads:
    ``logger``, ``credentials``, ``callback``, ``callback_context``,
    ``_log_warn_with_limit``, ``_log_verbose``, ``_reset_error_count``,
    ``_try_increment_error_count``. ``_decrypt_raw_data`` is left for the test
    to set per case (controlled ``bytes`` return), decoupling the JSON/callback
    branches from real crypto.
    """

    def __init__(self, *, credentials: Any = _CREDENTIALS_UNSET) -> None:
        self.logger = logging.getLogger(__name__ + ".FcmHandleSlim")
        self.logger.propagate = True
        self.credentials: Any = (
            {"gcm": {"app_id": "APPID"}, "keys": {}}
            if credentials is _CREDENTIALS_UNSET
            else credentials
        )
        self.callback = Mock()  # synchronous: production calls it un-awaited (AE7)
        self.callback_context = object()
        self._reset_error_count = Mock()
        self._try_increment_error_count = Mock()
        # Mirror the stale-key escalation counter ``_handle_data_message`` now
        # resets to 0 on a successful decrypt.
        self._consecutive_decrypt_failures = 0
        # Records every rate-limited warning message the method emits.
        self.warnings: list[str] = []

    def _log_warn_with_limit(self, msg: str, *args: object) -> None:
        self.warnings.append(msg)
        self.logger.warning(msg, *args)

    def _log_verbose(self, msg: str, *args: object) -> None:
        self.logger.debug(msg, *args)

    # Bind the real production functions. Instance methods bind ``self``
    # naturally; ``_extract_header_param`` is a @staticmethod and must be
    # re-wrapped so ``self._extract_header_param(header, param)`` does not bind
    # ``self`` as a third positional argument.
    _handle_data_message = FcmPushClient._handle_data_message  # type: ignore[assignment]
    _app_data_by_key = FcmPushClient._app_data_by_key  # type: ignore[assignment]
    _extract_header_param = staticmethod(FcmPushClient._extract_header_param)
