# tests/test_coordinator_canonicless_stats.py
"""Coordinator wiring of the canonicless drop counters (P1-2b).

The decoder tallies the per-poll canonicless drops; the coordinator lifts the
three aggregates into ``self.stats`` so they (a) restore from the persisted
``integration_stats`` cache and (b) surface in diagnostics.

These tests pin:

* the three keys are part of the persisted/restored stat surface (the restore
  path iterates ``self.stats.keys()``),
* ``_refresh_canonicless_drop_stats`` reads the decoder accessor and sets the
  three values **absolutely** (not incrementally) and schedules persistence.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.googlefindmy.coordinator.main import GoogleFindMyCoordinator
from tests.helpers.config_entries_stub import make_config_entry
from tests.helpers.main_coordinator_stub import MainCoordinatorStub

_DROP_KEYS = (
    "canonicless_drop_total",
    "canonicless_drop_benign",
    "canonicless_drop_warn",
)


def _coordinator_with_stats(stats: dict[str, int]) -> MainCoordinatorStub:
    """Build a bypassed coordinator stub carrying the given ``stats`` dict."""
    entry = make_config_entry(entry_id="entry-A")
    coord = MainCoordinatorStub(config_entry=entry)
    coord.stats = stats
    coord._stats_save_task = None
    return coord


@pytest.mark.asyncio
async def test_restore_loads_canonicless_drop_keys() -> None:
    """A2: the restore path loads the three drop keys from the cache.

    ``_async_load_stats`` iterates ``self.stats.keys()``; seeding the init
    surface with the three keys (production default 0) lets a cached payload
    rehydrate them after a restart.
    """

    async def _cached(key: str) -> dict[str, int] | None:
        if key == "integration_stats":
            return {
                "canonicless_drop_total": 7,
                "canonicless_drop_benign": 4,
                "canonicless_drop_warn": 3,
            }
        return None

    coord = _coordinator_with_stats(
        {
            "canonicless_drop_total": 0,
            "canonicless_drop_benign": 0,
            "canonicless_drop_warn": 0,
        }
    )
    coord._cache.async_get_cached_value = AsyncMock(side_effect=_cached)

    await coord._async_load_stats()

    assert coord.stats["canonicless_drop_total"] == 7
    assert coord.stats["canonicless_drop_benign"] == 4
    assert coord.stats["canonicless_drop_warn"] == 3


def test_refresh_sets_counts_absolutely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2: ``_refresh_canonicless_drop_stats`` overwrites the stats from the accessor.

    The accessor returns the per-poll aggregate; the coordinator must SET (not
    increment) so a recovering count goes down rather than monotonically rising.
    """
    coord = _coordinator_with_stats(
        {
            "canonicless_drop_total": 99,  # stale prior value
            "canonicless_drop_benign": 50,
            "canonicless_drop_warn": 49,
        }
    )
    persist = MagicMock()
    coord._schedule_stats_persist = persist  # type: ignore[method-assign]

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.main.decoder.get_canonicless_counts",
        lambda entry_id: {"total": 3, "benign": 2, "warn": 1},
    )

    coord._refresh_canonicless_drop_stats("entry-A")  # type: ignore[attr-defined]

    assert coord.stats["canonicless_drop_total"] == 3
    assert coord.stats["canonicless_drop_benign"] == 2
    assert coord.stats["canonicless_drop_warn"] == 1
    persist.assert_called_once()


def test_refresh_noop_without_entry_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A2/defensive: a missing entry id leaves the stats untouched."""
    coord = _coordinator_with_stats(
        {
            "canonicless_drop_total": 5,
            "canonicless_drop_benign": 3,
            "canonicless_drop_warn": 2,
        }
    )
    persist = MagicMock()
    coord._schedule_stats_persist = persist  # type: ignore[method-assign]

    called: dict[str, Any] = {}

    def _spy(entry_id: str) -> dict[str, int]:
        called["hit"] = entry_id
        return {"total": 0, "benign": 0, "warn": 0}

    monkeypatch.setattr(
        "custom_components.googlefindmy.coordinator.main.decoder.get_canonicless_counts",
        _spy,
    )

    coord._refresh_canonicless_drop_stats(None)  # type: ignore[attr-defined]

    assert coord.stats["canonicless_drop_total"] == 5
    assert "hit" not in called
    persist.assert_not_called()


def test_refresh_helper_is_defined_on_real_class() -> None:
    """The wiring helper lives on the production coordinator class, not the stub."""
    assert hasattr(GoogleFindMyCoordinator, "_refresh_canonicless_drop_stats")
