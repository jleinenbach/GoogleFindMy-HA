# tests/test_api_transient_owner_key.py
"""RED contract: the API layer must not swallow a transient owner-key failure.

``GoogleFindMyAPI.async_get_device_location`` re-raises ``DecryptionError`` (its
audit-finding A1 guard) so the coordinator can escalate, but
``OwnerKeyLookupTransientError`` has base ``Exception`` (NOT ``DecryptionError`` and
NOT ``RuntimeError``), so it falls through to the broad ``except Exception`` and is
turned into ``return {}``. The coordinator then cannot distinguish a transient
owner-key miss from "no data". This test pins the intended propagation; it is RED
until the API layer grows an explicit transient re-raise branch.

Patch point: ``api.py`` binds ``get_location_data_for_device`` via ``from ... import``
at module scope (lines ~51-52), so the API-local name
``custom_components.googlefindmy.api.get_location_data_for_device`` must be patched,
NOT the source module.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import custom_components.googlefindmy.api as api_module
from custom_components.googlefindmy.api import GoogleFindMyAPI
from custom_components.googlefindmy.NovaApi.ExecuteAction.LocateTracker.decrypt_locations import (
    OwnerKeyLookupTransientError,
)

pytestmark = pytest.mark.asyncio


def _make_api() -> GoogleFindMyAPI:
    """Build an API instance without running its normal __init__ wiring."""
    api = GoogleFindMyAPI.__new__(GoogleFindMyAPI)
    api._session = None
    api._cache = MagicMock()
    api._cache.entry_id = "entry-transient"
    api._contributor_mode = None
    api._contributor_mode_switch_epoch = 0
    api._namespace = lambda: "entry-transient"
    return api


async def test_async_get_device_location_reraises_transient_owner_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED: a transient owner-key failure from the locate layer must propagate.

    Today ``async_get_device_location`` returns ``{}`` because the broad
    ``except Exception`` catches ``OwnerKeyLookupTransientError`` (base ``Exception``).
    """
    transient = OwnerKeyLookupTransientError("owner key lookup timed out")

    async def _raise_transient(*_a: object, **_k: object) -> list[dict[str, Any]]:
        raise transient

    # Patch the API-LOCAL binding, not the source module (api.py imported the name).
    monkeypatch.setattr(api_module, "get_location_data_for_device", _raise_transient)

    api = _make_api()

    with pytest.raises(OwnerKeyLookupTransientError):
        await api.async_get_device_location("device-xyz", "Tracker")
