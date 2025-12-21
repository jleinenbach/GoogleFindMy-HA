# tests/test_eid_resolver_logic.py
from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from custom_components.googlefindmy.coordinator import DeviceIdentity
from custom_components.googlefindmy.eid_resolver import (
    ROTATION_PERIOD,
    GoogleFindMyEIDResolver,
)


@pytest.mark.asyncio
async def test_relative_time_calculation_uses_elapsed_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ensure relative bases compute now - anchor (v3.31 behavior)."""

    now_ts = 20_000
    pair_ts = 18_000
    expected_counter = now_ts - pair_ts
    aligned_expected = expected_counter - (expected_counter % ROTATION_PERIOD)

    identity = DeviceIdentity(
        registry_id="test-device",
        canonical_id="canonical",
        identity_key=b"123",
        config_entry_id="entry-id",
        pair_date=pair_ts,
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = MagicMock()
    resolver._locks = {}
    resolver._ensure_cache_defaults()
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    monkeypatch.setattr(
        GoogleFindMyEIDResolver,
        "_generate_variant",
        MagicMock(return_value=b"mocked_eid"),
    )

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(time, "time", lambda: float(now_ts))

    await resolver._refresh_cache()

    calls = [call.kwargs["time_counter"] for call in resolver._generate_variant.call_args_list]
    assert aligned_expected in calls, f"Expected counter {aligned_expected} not found in {calls}"


@pytest.mark.asyncio
async def test_unix_basis_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify absolute unix scans are skipped when the flag is False."""

    now_ts = 50_000
    identity = DeviceIdentity(
        registry_id="test-device",
        canonical_id="canonical",
        identity_key=b"123",
        config_entry_id="entry-id",
    )

    resolver = GoogleFindMyEIDResolver.__new__(GoogleFindMyEIDResolver)
    resolver.hass = MagicMock()
    resolver._locks = {}
    resolver._ensure_cache_defaults()
    resolver._refresh_lock = asyncio.Lock()
    resolver._pending_refresh = False
    resolver._load_task = None
    resolver._unsub_interval = None
    resolver._unsub_alignment = None
    monkeypatch.setattr(GoogleFindMyEIDResolver, "_generate_variant", MagicMock())

    async def _collect(_self: GoogleFindMyEIDResolver) -> list[DeviceIdentity]:
        return [identity]

    monkeypatch.setattr(GoogleFindMyEIDResolver, "_collect_device_secrets", _collect)
    monkeypatch.setattr(time, "time", lambda: float(now_ts))

    await resolver._refresh_cache()

    assert resolver._generate_variant.call_count == 0
