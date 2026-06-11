# tests/helpers/polling_branches_stub.py
"""Branch-coverage test stub for :class:`PollingOperations`.

Mirror production attribute names from PollingOperations / _MixinBase verbatim.
Properties read these by exact name; semantic-equivalent renames cause AttributeError.

Extends :class:`tests.helpers.polling_mixin_stub.PollingStub` (Phase 2) with the
extra cross-mixin hook ``async_request_refresh`` that the branch APIs under test
in Phase 4 AP-J exercise:

- ``_dispatch_async_request_refresh`` (loop detection + awaitable scheduling
  + exception swallowing)
- ``_schedule_short_retry`` (coalesce, delay clamping, callback dispatch)
- ``_handle_dr_event`` (reindex + dispatch fan-out)
- ``_is_fcm_ready_soft`` (3-tier priority: API / receiver / push-client)
- ``_note_fcm_deferral`` (escalation timeline at 0/120/300s)

The Phase 2 ``PollingStub`` deliberately omitted ``async_request_refresh``
because the simple mixin methods do not touch it. CA-F6 (no in-place edit of a
gemerged stub) requires the addition to live in a new helper module.

Why subclass instead of monkeypatching?
- Tests can rebind ``async_request_refresh`` per-case (sync ``MagicMock``, async
  coroutine, raising ``MagicMock``) without leaking state across test methods.
- A subclass keeps the production isinstance/mro structure of ``PollingStub``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from tests.helpers.polling_mixin_stub import PollingStub


class PollingBranchesStub(PollingStub):
    """Phase 4 AP-J branch-coverage stub for :class:`PollingOperations`."""

    def __init__(
        self,
        hass: Any | None = None,
        config_entry: Any | None = None,
        *,
        api: Any | None = None,
        location_poll_interval: int = 120,
        min_poll_interval: int = 60,
    ) -> None:
        super().__init__(
            hass,
            config_entry,
            api=api,
            location_poll_interval=location_poll_interval,
            min_poll_interval=min_poll_interval,
        )

        # Cross-mixin hook exercised by ``_dispatch_async_request_refresh``
        # and indirectly by ``_schedule_short_retry`` and ``_handle_dr_event``.
        # Default is a no-op ``MagicMock`` so tests that only need the
        # callable-check branch can ignore it. Tests override per-case to a
        # coroutine factory or a raising MagicMock.
        self.async_request_refresh = MagicMock(return_value=None)


__all__ = ["PollingBranchesStub"]
