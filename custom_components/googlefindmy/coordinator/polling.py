"""Polling operations for GoogleFindMyCoordinator.

This module will contain polling-related methods extracted from main.py
during Phase 4 of the refactoring.

Methods to be moved here (6 total, ~495 lines):
- _async_start_poll_cycle
- _get_predicted_poll_time
- is_polling
- force_poll_due
- last_poll_result
- _schedule_next_poll
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .main import GoogleFindMyCoordinator


class PollingOperations:
    """Polling operations mixin for GoogleFindMyCoordinator.

    This class will contain methods that manage the polling lifecycle,
    including poll cycle scheduling, predictive polling, and poll state
    management.

    Currently empty - methods will be extracted in Phase 4.
    """

    pass
