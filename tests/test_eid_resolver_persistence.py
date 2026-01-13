# tests/test_eid_resolver_persistence.py
"""Persistence scheduling for EID resolver state."""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    EIDGenerationLock,
    GoogleFindMyEIDResolver,
)


@pytest.mark.asyncio
async def test_refresh_triggers_persistence_on_id_update(hass) -> None:
    """Canonical ID updates should schedule lock persistence."""

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = hass
    resolver._store = MagicMock()
    resolver._store.async_load = AsyncMock(return_value=None)
    resolver._store.async_save = AsyncMock()
    resolver._lookup = {}
    resolver._lookup_metadata = {}
    resolver._known_offsets = {}
    resolver._known_advertisement_reversed = {}
    resolver._known_timebases = {}
    resolver._persisted_locks = {}
    resolver._decryption_status = {}
    resolver._last_lock_confirmation = {}
    resolver._provisioning_warn_at = {}
    resolver._locks = {}
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None

    resolver._locks["dev_1"] = EIDGenerationLock(
        device_id="dev_1",
        canonical_id="OLD_UUID",
        variant="legacy",
        advertisement_reversed=False,
        eid_length=20,
        rotation_timestamp=1000,
    )

    identity = DeviceIdentity(
        registry_id="dev_1",
        canonical_id="account:NEW_UUID",
        identity_key=b"\x00" * 32,
        encrypted_identity_key=None,
        owner_key_version=None,
        device_type=None,
        config_entry_id="entry_1",
        fast_pair_model_id=None,
    )

    hass.async_create_background_task = MagicMock()
    hass.async_create_task = MagicMock()

    with (
        patch.object(
            GoogleFindMyEIDResolver,
            "_collect_device_secrets",
            AsyncMock(return_value=[identity]),
        ),
        patch.object(
            GoogleFindMyEIDResolver, "_generate_variant", return_value=b"\x01" * 20
        ),
    ):
        await resolver._refresh_cache()

    assert resolver._locks["dev_1"].canonical_id == "NEW_UUID"
    assert hass.async_create_background_task.called
    hass.async_create_task.assert_not_called()
    scheduled_coro = hass.async_create_background_task.call_args.args[0]
    assert inspect.iscoroutine(scheduled_coro)
    assert scheduled_coro.cr_code.co_name == resolver._async_save_locks.__name__
