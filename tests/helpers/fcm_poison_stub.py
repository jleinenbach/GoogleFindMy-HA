# tests/helpers/fcm_poison_stub.py
# Mirror production attribute names from FcmPushClient.__init__ and DataMessageStanza verbatim
"""Slim composition stub for FcmPushClient Defense 1 tests (CA-CASCADING-FAILURE-001).

Binds the production ``FcmPushClient._listen`` method to a lightweight container
that mirrors only the attributes the worker loop touches. This avoids the heavy
production constructor (``FcmRegisterConfig`` / Firebase wiring) while exercising
the real Inner-Loop code path, so Defense 1 is verified end-to-end and not
re-implemented in the stub.

The stub is *not* a subclass of ``FcmPushClient`` (composition only). Production
attribute names (``logger``, ``do_listen``, ``run_state``, ``config``,
``log_warn_counters``) are mirrored verbatim from ``FcmPushClient.__init__``;
test instrumentation fields (``_loop_iteration_count``, ``_last_handled_id``,
``_after_iter_hook``) are exposed for the Aggregate-Anti-Cascading test.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import AsyncMock

from custom_components.googlefindmy.Auth.firebase_messaging.fcmpushclient import (
    FcmPushClient,
    FcmPushClientConfig,
    FcmPushClientRunState,
)


def make_poison_data_message(
    persistent_id: str, kind: str = "padding"
) -> SimpleNamespace:
    """Build a DataMessageStanza-shaped namespace for Inner-Loop tests.

    ``kind`` selects the failure class the test will raise on decrypt:
      - ``padding`` -> ``binascii.Error("Incorrect padding")``
      - ``value``   -> ``RuntimeError("couldn't find in app_data crypto-key")``
      - ``valid``   -> message that should pass through without raising
    """
    if kind not in {"padding", "value", "valid"}:
        raise ValueError(f"unknown kind: {kind!r}")
    return SimpleNamespace(
        app_data=[],
        persistent_id=persistent_id,
        stream_id=1,
        last_stream_id_received=0,
        status="ok",
        _kind=kind,
    )


def make_valid_data_message(persistent_id: str) -> SimpleNamespace:
    """Convenience wrapper for a pass-through (non-poison) message."""
    return make_poison_data_message(persistent_id, kind="valid")


class FcmPushClientSlim:
    """Composition stub for Defense 1 tests.

    Binds ``FcmPushClient._listen`` to ``self`` so the production worker loop
    runs against a controlled set of mocked dependencies. Only the attributes
    the loop reads or writes are mirrored; everything else is left out.
    """

    def __init__(self) -> None:
        # Mirror production attribute names from FcmPushClient.__init__ verbatim
        self.logger = logging.getLogger(__name__ + ".FcmPushClientSlim")
        self.logger.propagate = True
        self.do_listen = True
        self.run_state: FcmPushClientRunState = FcmPushClientRunState.STARTED
        self.config = FcmPushClientConfig()  # log_warn_limit default = 5
        self.log_warn_counters: dict[str, int] = {}
        self.persistent_ids: list[str] = []
        self.writer = None  # _do_writer_close short-circuits on None

        # Async-stubbed production methods (overridable per test).
        self._receive_msg = AsyncMock(return_value=None)
        self._handle_message = AsyncMock(return_value=None)
        self._send_selective_ack = AsyncMock(return_value=None)
        self._connect_with_retry = AsyncMock(return_value=True)
        self._login = AsyncMock(return_value=None)
        self._do_writer_close = AsyncMock(return_value=None)

        # Test instrumentation (NOT production attributes).
        self._loop_iteration_count = 0
        self._last_handled_id: str | None = None
        self._after_iter_hook: Callable[[], Any] = lambda: None

    def _log_warn_with_limit(self, msg: str, *args: object) -> None:
        """Production-verbatim rate-limited WARN helper (KV-H7)."""
        if msg not in self.log_warn_counters:
            self.log_warn_counters[msg] = 0
        if (
            self.config.log_warn_limit
            and self.config.log_warn_limit > self.log_warn_counters[msg]
        ):
            self.log_warn_counters[msg] += 1
            self.logger.warning(msg, *args)

    # Bind the real production _listen unbound function to this slim instance.
    # When invoked via ``self._listen()`` it resolves ``self`` to the stub.
    _listen = FcmPushClient._listen  # type: ignore[assignment]
